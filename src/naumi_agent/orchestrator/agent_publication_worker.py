"""Bounded periodic recovery for durable Agent result publications."""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from naumi_agent.orchestrator.subagent_manager import (
    AgentPublicationRecoverySummary,
    SubAgentManager,
)


class AgentPublicationWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class AgentPublicationWorkerPolicy:
    interval_seconds: float = 30.0
    max_empty_backoff_seconds: float = 300.0
    max_failure_backoff_seconds: float = 300.0
    scan_limit: int = 100
    max_attempts: int = 5
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if not _finite_number(self.interval_seconds, minimum=0.1, maximum=86_400):
            raise ValueError("Agent publication worker 周期间隔无效。")
        if not _finite_number(
            self.max_empty_backoff_seconds,
            minimum=float(self.interval_seconds),
            maximum=604_800,
        ):
            raise ValueError("Agent publication worker 空轮退避无效。")
        if not _finite_number(
            self.max_failure_backoff_seconds,
            minimum=float(self.interval_seconds),
            maximum=604_800,
        ):
            raise ValueError("Agent publication worker 失败退避无效。")
        if (
            isinstance(self.scan_limit, bool)
            or not isinstance(self.scan_limit, int)
            or not 1 <= self.scan_limit <= 1000
        ):
            raise ValueError("Agent publication worker scan limit 无效。")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("Agent publication worker retry budget 无效。")
        if not _finite_number(self.jitter_ratio, minimum=0, maximum=0.5):
            raise ValueError("Agent publication worker jitter 无效。")


@dataclass(frozen=True, slots=True)
class AgentPublicationWorkerSnapshot:
    state: AgentPublicationWorkerState
    pass_count: int
    scanned_count: int
    delivered_count: int
    notification_failure_count: int
    quarantined_count: int
    failure_count: int
    consecutive_empty_passes: int
    consecutive_failure_passes: int
    next_delay_seconds: float
    last_failure_codes: tuple[str, ...]
    started_at: str
    last_pass_at: str


class AgentPublicationRecoveryWorker:
    """Periodically drain a bounded FIFO prefix through Store-fenced claims."""

    def __init__(
        self,
        *,
        manager: SubAgentManager,
        policy: AgentPublicationWorkerPolicy,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if not isinstance(manager, SubAgentManager):
            raise TypeError("manager 必须是 SubAgentManager。")
        if not isinstance(policy, AgentPublicationWorkerPolicy):
            raise TypeError("policy 必须是 AgentPublicationWorkerPolicy。")
        self._manager = manager
        self._policy = policy
        self._now = now
        self._random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = AgentPublicationWorkerState.STOPPED
        self._pass_count = 0
        self._scanned_count = 0
        self._delivered_count = 0
        self._notification_failure_count = 0
        self._quarantined_count = 0
        self._failure_count = 0
        self._consecutive_empty_passes = 0
        self._consecutive_failure_passes = 0
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
        self._state = AgentPublicationWorkerState.WAITING
        self._task = asyncio.create_task(
            self._run_loop(),
            name="naumi-agent-publication-recovery",
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = AgentPublicationWorkerState.STOPPING
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

    async def run_once(self) -> AgentPublicationRecoverySummary:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> AgentPublicationRecoverySummary:
        self._state = AgentPublicationWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        try:
            result = await self._manager.recover_pending_publications(
                limit=self._policy.scan_limit,
                max_attempts=self._policy.max_attempts,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = AgentPublicationRecoverySummary(
                failed=1,
                failure_codes=("agent_publication_worker_pass_failed",),
            )

        self._pass_count += 1
        self._scanned_count += result.scanned
        self._delivered_count += result.delivered
        self._notification_failure_count += result.notification_failures
        self._quarantined_count += result.quarantined
        self._failure_count += result.failed
        self._last_failure_codes = result.failure_codes

        if result.failed:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes += 1
            delay = min(
                self._policy.max_failure_backoff_seconds,
                self._policy.interval_seconds
                * (2 ** min(20, self._consecutive_failure_passes - 1)),
            )
        elif result.scanned == 0:
            self._consecutive_failure_passes = 0
            self._consecutive_empty_passes += 1
            delay = min(
                self._policy.max_empty_backoff_seconds,
                self._policy.interval_seconds
                * (2 ** min(20, self._consecutive_empty_passes)),
            )
        else:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes = 0
            delay = self._policy.interval_seconds

        self._next_delay_seconds = self._jittered(delay)
        self._state = (
            AgentPublicationWorkerState.STOPPING
            if self._stop_event.is_set()
            else AgentPublicationWorkerState.WAITING
        )
        return result

    def snapshot(self) -> AgentPublicationWorkerSnapshot:
        return AgentPublicationWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            scanned_count=self._scanned_count,
            delivered_count=self._delivered_count,
            notification_failure_count=self._notification_failure_count,
            quarantined_count=self._quarantined_count,
            failure_count=self._failure_count,
            consecutive_empty_passes=self._consecutive_empty_passes,
            consecutive_failure_passes=self._consecutive_failure_passes,
            next_delay_seconds=self._next_delay_seconds,
            last_failure_codes=self._last_failure_codes,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._wait(self._next_delay_seconds)
                if self._stop_event.is_set():
                    break
                await self.run_once()
        finally:
            self._state = AgentPublicationWorkerState.STOPPED

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

    def _jittered(self, delay: float) -> float:
        sample = self._random_value()
        if not _finite_number(sample, minimum=0, maximum=1):
            raise ValueError("Agent publication worker random source 必须在 0..1。")
        factor = 1 + self._policy.jitter_ratio * (2 * sample - 1)
        return max(0.0, delay * factor)

    def _timestamp(self) -> str:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Agent publication worker 时钟必须包含时区。")
        return value.astimezone(UTC).isoformat()


def _finite_number(value: object, *, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


__all__ = [
    "AgentPublicationRecoveryWorker",
    "AgentPublicationWorkerPolicy",
    "AgentPublicationWorkerSnapshot",
    "AgentPublicationWorkerState",
]
