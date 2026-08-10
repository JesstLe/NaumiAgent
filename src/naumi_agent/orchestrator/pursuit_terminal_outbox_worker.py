"""Bounded automatic recovery for Pursuit terminal outbox records."""

from __future__ import annotations

import asyncio
import math
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from naumi_agent.orchestrator.pursuit_recovery_reconcile import (
    PursuitRecoveryReconcileAuthority,
    reconcile_pursuit_recovery_attempt,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.orchestrator.pursuit_terminal_dead_letter import (
    PursuitTerminalOutboxFailureDisposition,
)
from naumi_agent.orchestrator.pursuit_terminal_outbox import (
    PursuitTerminalOutboxState,
)


class PursuitTerminalWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


class PursuitTerminalFailureClass(StrEnum):
    SAFE_WAIT = "safe_wait"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


_SAFE_WAIT_CODES = frozenset({
    "grace_period_active",
    "live_heartbeat",
    "live_lease",
    "lease_claim_conflict",
})
_RETRYABLE_FAILURE_CODES = frozenset({
    "attempt_ledger_unavailable",
    "authority_unavailable",
    "authority_read_failed",
    "authority_mutation_failed",
    "clock_regression",
    "dispatch_reconcile_failed",
    "reconcile_store_failed",
    "terminal_evidence_from_future",
})


@dataclass(frozen=True, slots=True)
class PursuitTerminalOutboxWorkerPolicy:
    interval_seconds: float = 30.0
    max_empty_backoff_seconds: float = 300.0
    claim_lease_seconds: int = 60
    scan_limit: int = 20
    reconcile_grace_seconds: float = 30.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 8
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if not _finite_number(self.interval_seconds, minimum=0.1, maximum=86_400):
            raise ValueError("terminal worker 周期间隔无效。")
        if not _finite_number(
            self.max_empty_backoff_seconds,
            minimum=float(self.interval_seconds),
            maximum=604_800,
        ):
            raise ValueError("terminal worker 空轮退避无效。")
        if (
            isinstance(self.claim_lease_seconds, bool)
            or not isinstance(self.claim_lease_seconds, int)
            or not 3 <= self.claim_lease_seconds <= 300
        ):
            raise ValueError("terminal worker claim 租约无效。")
        if (
            isinstance(self.scan_limit, bool)
            or not isinstance(self.scan_limit, int)
            or not 1 <= self.scan_limit <= 1000
        ):
            raise ValueError("terminal worker scan limit 无效。")
        if not _finite_number(
            self.reconcile_grace_seconds,
            minimum=1,
            maximum=86_400,
        ):
            raise ValueError("terminal worker 对账宽限期无效。")
        if not _finite_number(
            self.retry_base_seconds,
            minimum=1,
            maximum=3600,
        ) or not _finite_number(
            self.retry_max_seconds,
            minimum=float(self.retry_base_seconds),
            maximum=3600,
        ):
            raise ValueError("terminal worker retry 退避无效。")
        if not _finite_number(self.jitter_ratio, minimum=0, maximum=0.5):
            raise ValueError("terminal worker jitter 无效。")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("terminal worker 最大失败次数无效。")


@dataclass(frozen=True, slots=True)
class PursuitTerminalOutboxPassResult:
    claimed: int = 0
    delivered: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    failures: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PursuitTerminalOutboxWorkerSnapshot:
    state: PursuitTerminalWorkerState
    pass_count: int
    claimed_count: int
    delivered_count: int
    retry_scheduled_count: int
    dead_lettered_count: int
    failure_count: int
    consecutive_empty_passes: int
    next_delay_seconds: float
    last_failure_codes: tuple[str, ...]
    started_at: str
    last_pass_at: str


class PursuitTerminalOutboxWorker:
    """Recover a bounded FIFO prefix while Store claims fence peer scanners."""

    def __init__(
        self,
        *,
        store: PursuitStore,
        authority: PursuitRecoveryReconcileAuthority,
        workspace_root: str | Path,
        policy: PursuitTerminalOutboxWorkerPolicy,
        owner_id: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._authority = authority
        self._workspace_root = Path(workspace_root).resolve()
        self._policy = policy
        self._owner_id = (owner_id or f"terminal-outbox-{uuid.uuid4().hex}").strip()
        if not self._owner_id or len(self._owner_id) > 256:
            raise ValueError("terminal worker owner_id 必须为 1 到 256 个字符。")
        self._now = now
        self._random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = PursuitTerminalWorkerState.STOPPED
        self._pass_count = 0
        self._claimed_count = 0
        self._delivered_count = 0
        self._retry_scheduled_count = 0
        self._dead_lettered_count = 0
        self._failure_count = 0
        self._consecutive_empty_passes = 0
        self._next_delay_seconds = policy.interval_seconds
        self._last_failure_codes: tuple[str, ...] = ()
        self._started_at = ""
        self._last_pass_at = ""

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="naumi-pursuit-terminal-outbox",
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = PursuitTerminalWorkerState.STOPPING
        self._stop_event.set()
        self._wake_event.set()
        try:
            await task
        finally:
            self._task = None
        return True

    def wake(self) -> bool:
        if self._task is None or self._task.done() or self._stop_event.is_set():
            return False
        self._wake_event.set()
        return True

    async def run_once(self) -> PursuitTerminalOutboxPassResult:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> PursuitTerminalOutboxPassResult:
        self._state = PursuitTerminalWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        claimed = delivered = retry_scheduled = dead_lettered = failures = 0
        failure_codes: list[str] = []
        for _ in range(self._policy.scan_limit):
            if self._stop_event.is_set():
                break
            claim_now = self._now_value()
            try:
                claim = self._store.claim_next_terminal_outbox(
                    owner_id=self._owner_id,
                    now=claim_now.timestamp(),
                    lease_seconds=self._policy.claim_lease_seconds,
                    scan_limit=self._policy.scan_limit,
                )
            except Exception:
                failures += 1
                failure_codes.append("dispatch_claim_failed")
                break
            if claim is None:
                break
            claimed += 1
            try:
                result = await reconcile_pursuit_recovery_attempt(
                    store=self._store,
                    authority=self._authority,
                    workspace_root=self._workspace_root,
                    attempt_id=claim.outbox.attempt_id,
                    now=claim_now.isoformat(),
                    grace_seconds=self._policy.reconcile_grace_seconds,
                    lease_seconds=min(30, self._policy.claim_lease_seconds),
                )
                current = self._store.get_terminal_outbox(claim.outbox.outbox_id)
                if (
                    current is not None
                    and current.state is PursuitTerminalOutboxState.DELIVERED
                ):
                    delivered += 1
                    continue
                failure_code = result.code or "reconcile_incomplete"
                delay = self._retry_delay(claim.dispatch.attempt_count)
                release_now = self._now_value().timestamp()
                failure_class = classify_terminal_outbox_failure(failure_code)
                if failure_class is PursuitTerminalFailureClass.SAFE_WAIT:
                    self._store.release_terminal_outbox_claim(
                        claim.outbox.outbox_id,
                        owner_id=self._owner_id,
                        claim_epoch=claim.dispatch.claim_epoch,
                        now=release_now,
                        retry_delay_seconds=delay,
                        failure_code=failure_code,
                    )
                    retry_scheduled += 1
                else:
                    event, _ = self._store.record_terminal_outbox_failure(
                        claim.outbox.outbox_id,
                        owner_id=self._owner_id,
                        claim_epoch=claim.dispatch.claim_epoch,
                        now=release_now,
                        retry_delay_seconds=delay,
                        failure_code=failure_code,
                        max_failures=self._policy.max_attempts,
                        permanent=(
                            failure_class is PursuitTerminalFailureClass.PERMANENT
                        ),
                    )
                    failures += 1
                    if (
                        event.disposition
                        is PursuitTerminalOutboxFailureDisposition.RETRYABLE
                    ):
                        retry_scheduled += 1
                    else:
                        dead_lettered += 1
                failure_codes.append(failure_code)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                failure_codes.append("dispatch_reconcile_failed")
                try:
                    release_now = self._now_value().timestamp()
                    event, _ = self._store.record_terminal_outbox_failure(
                        claim.outbox.outbox_id,
                        owner_id=self._owner_id,
                        claim_epoch=claim.dispatch.claim_epoch,
                        now=release_now,
                        retry_delay_seconds=self._retry_delay(
                            claim.dispatch.attempt_count
                        ),
                        failure_code="dispatch_reconcile_failed",
                        max_failures=self._policy.max_attempts,
                    )
                    if (
                        event.disposition
                        is PursuitTerminalOutboxFailureDisposition.RETRYABLE
                    ):
                        retry_scheduled += 1
                    else:
                        dead_lettered += 1
                except Exception:
                    failure_codes.append("dispatch_failure_record_failed")

        result = PursuitTerminalOutboxPassResult(
            claimed=claimed,
            delivered=delivered,
            retry_scheduled=retry_scheduled,
            dead_lettered=dead_lettered,
            failures=failures,
            failure_codes=tuple(sorted(set(failure_codes))),
        )
        self._pass_count += 1
        self._claimed_count += claimed
        self._delivered_count += delivered
        self._retry_scheduled_count += retry_scheduled
        self._dead_lettered_count += dead_lettered
        self._failure_count += failures
        self._last_failure_codes = result.failure_codes
        if claimed == 0:
            self._consecutive_empty_passes += 1
            delay = min(
                self._policy.max_empty_backoff_seconds,
                self._policy.interval_seconds
                * (2 ** min(20, self._consecutive_empty_passes)),
            )
        else:
            self._consecutive_empty_passes = 0
            delay = self._policy.interval_seconds
        self._next_delay_seconds = self._jittered(delay)
        self._state = (
            PursuitTerminalWorkerState.STOPPING
            if self._stop_event.is_set()
            else PursuitTerminalWorkerState.WAITING
        )
        return result

    def snapshot(self) -> PursuitTerminalOutboxWorkerSnapshot:
        return PursuitTerminalOutboxWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            claimed_count=self._claimed_count,
            delivered_count=self._delivered_count,
            retry_scheduled_count=self._retry_scheduled_count,
            dead_lettered_count=self._dead_lettered_count,
            failure_count=self._failure_count,
            consecutive_empty_passes=self._consecutive_empty_passes,
            next_delay_seconds=self._next_delay_seconds,
            last_failure_codes=self._last_failure_codes,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self.run_once()
                if self._stop_event.is_set():
                    break
                await self._wait(self._next_delay_seconds)
        finally:
            self._state = PursuitTerminalWorkerState.STOPPED

    async def _wait(self, delay: float) -> None:
        stop = asyncio.create_task(self._stop_event.wait())
        wake = asyncio.create_task(self._wake_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop, wake},
                timeout=max(0.0, delay),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wake in done and wake.result():
                self._wake_event.clear()
        finally:
            for task in (stop, wake):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = min(20, max(0, attempt_count - 1))
        return min(
            self._policy.retry_max_seconds,
            self._policy.retry_base_seconds * (2**exponent),
        )

    def _jittered(self, delay: float) -> float:
        spread = self._policy.jitter_ratio
        sample = self._random_value()
        if not _finite_number(sample, minimum=0, maximum=1):
            raise ValueError("terminal worker random source 必须在 0..1。")
        factor = 1 + spread * (2 * sample - 1)
        return max(0.0, delay * factor)

    def _timestamp(self) -> str:
        return self._now_value().isoformat()

    def _now_value(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("terminal worker 时钟必须包含时区。")
        return value.astimezone(UTC)


def _finite_number(value: object, *, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def classify_terminal_outbox_failure(code: str) -> PursuitTerminalFailureClass:
    """Classify known reconcile outcomes without consuming budget for safe waits."""
    normalized = str(code or "").strip()
    if normalized in _SAFE_WAIT_CODES:
        return PursuitTerminalFailureClass.SAFE_WAIT
    if normalized in _RETRYABLE_FAILURE_CODES:
        return PursuitTerminalFailureClass.RETRYABLE
    return PursuitTerminalFailureClass.PERMANENT


__all__ = [
    "PursuitTerminalFailureClass",
    "PursuitTerminalOutboxPassResult",
    "PursuitTerminalOutboxWorker",
    "PursuitTerminalOutboxWorkerPolicy",
    "PursuitTerminalOutboxWorkerSnapshot",
    "PursuitTerminalWorkerState",
    "classify_terminal_outbox_failure",
]
