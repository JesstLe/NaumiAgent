"""Bounded automatic delivery of stable-finalization packages to installations."""

from __future__ import annotations

import asyncio
import math
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryAck,
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationDeliveryPackage,
    EvolutionStableRemoteFinalizationDeliveryService,
    EvolutionStableRemoteFinalizationDeliveryStore,
    EvolutionStableRemoteFinalizationTargetJournal,
    encode_stable_remote_finalization_delivery_ack,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.release.population_registry import ReleaseManagedInstallationCredential
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlTrustPolicyDocument,
)


class EvolutionStableRemoteFinalizationDeliveryWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


class EvolutionStableRemoteFinalizationTransportError(RuntimeError):
    """Typed transport failure with an explicit retry disposition."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        normalized = str(code or "").strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("安装传输 failure code 无效。")
        self.code = normalized
        self.retryable = bool(retryable)


@runtime_checkable
class EvolutionStableRemoteFinalizationInstallationTransport(Protocol):
    """Authenticated installation endpoint; ACK remains non-execution evidence."""

    async def receive(
        self,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
    ) -> EvolutionStableRemoteFinalizationDeliveryAck: ...


class LocalStableRemoteFinalizationInstallationTransport:
    """Real local installation adapter using Trust Policy, journal and install key."""

    def __init__(
        self,
        *,
        journal: EvolutionStableRemoteFinalizationTargetJournal,
        trust_policy: ReleaseRolloutControlTrustPolicyDocument,
        credential: ReleaseManagedInstallationCredential,
        installation_key_service: ReleaseInstallationKeyService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if installation_key_service.release_root != journal.release_root:
            raise ValueError("本机安装传输的 Journal 与 installation key root 不一致。")
        if credential.payload.member_id == "":
            raise ValueError("本机安装传输缺少 installation member。")
        self.journal = journal
        self.trust_policy = trust_policy
        self.credential = credential
        self.installation_key_service = installation_key_service
        self.clock = clock

    async def receive(
        self,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
    ) -> EvolutionStableRemoteFinalizationDeliveryAck:
        if package.installation_member_id != self.credential.payload.member_id:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_delivery_transport_member_mismatch",
                "Delivery 目标与本机 installation member 不一致。",
                retryable=False,
            )
        # The local journal operation is transaction-bounded and intentionally runs in
        # the caller task so cancellation cannot leave an unobserved background write.
        return self.journal.receive(
            package=package,
            trust_policy=self.trust_policy,
            credential=self.credential,
            installation_key_service=self.installation_key_service,
            clock=self.clock,
        )


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationDeliveryWorkerPolicy:
    interval_seconds: float = 30.0
    max_empty_backoff_seconds: float = 300.0
    max_failure_backoff_seconds: float = 300.0
    claim_lease_seconds: int = 60
    scan_limit: int = 20
    ack_timeout_seconds: float = 20.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 8
    shutdown_drain_seconds: float = 25.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if not _finite(self.interval_seconds, 0.1, 86_400):
            raise ValueError("Remote Finalization Worker 周期间隔无效。")
        if not _finite(self.max_empty_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Remote Finalization Worker 空轮退避无效。")
        if not _finite(self.max_failure_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Remote Finalization Worker 失败退避无效。")
        if (
            isinstance(self.claim_lease_seconds, bool)
            or not isinstance(self.claim_lease_seconds, int)
            or not 3 <= self.claim_lease_seconds <= 300
        ):
            raise ValueError("Remote Finalization Worker claim lease 无效。")
        if (
            isinstance(self.scan_limit, bool)
            or not isinstance(self.scan_limit, int)
            or not 1 <= self.scan_limit <= 1000
        ):
            raise ValueError("Remote Finalization Worker scan limit 无效。")
        if not _finite(self.ack_timeout_seconds, 0.1, self.claim_lease_seconds - 0.1):
            raise ValueError("Remote Finalization Worker ACK timeout 必须小于 claim lease。")
        if not _finite(self.retry_base_seconds, 0.1, 3600) or not _finite(
            self.retry_max_seconds, self.retry_base_seconds, 3600
        ):
            raise ValueError("Remote Finalization Worker retry backoff 无效。")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("Remote Finalization Worker retry budget 无效。")
        if not _finite(self.shutdown_drain_seconds, 0.1, 3600):
            raise ValueError("Remote Finalization Worker shutdown drain 无效。")
        if not _finite(self.jitter_ratio, 0, 0.5):
            raise ValueError("Remote Finalization Worker jitter 无效。")


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationDeliveryPassResult:
    claimed: int = 0
    acknowledged: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    failures: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationDeliveryWorkerSnapshot:
    state: EvolutionStableRemoteFinalizationDeliveryWorkerState
    pass_count: int
    claimed_count: int
    acknowledged_count: int
    retry_scheduled_count: int
    dead_lettered_count: int
    failure_count: int
    forced_shutdown_count: int
    consecutive_empty_passes: int
    consecutive_failure_passes: int
    next_delay_seconds: float
    last_failure_codes: tuple[str, ...]
    started_at: str
    last_pass_at: str


class EvolutionStableRemoteFinalizationDeliveryWorker:
    """Drain a bounded FIFO prefix through Store-fenced installation delivery."""

    def __init__(
        self,
        *,
        service: EvolutionStableRemoteFinalizationDeliveryService,
        store: EvolutionStableRemoteFinalizationDeliveryStore,
        transport: EvolutionStableRemoteFinalizationInstallationTransport,
        policy: EvolutionStableRemoteFinalizationDeliveryWorkerPolicy,
        owner_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if service.store.db_path != store.db_path:
            raise ValueError("Remote Finalization Worker Service/Store 必须同源。")
        if not isinstance(transport, EvolutionStableRemoteFinalizationInstallationTransport):
            raise TypeError("transport 必须实现 authenticated installation transport。")
        self.service = service
        self.store = store
        self.transport = transport
        self.policy = policy
        self.owner_id = (owner_id or f"stable-finalization-{uuid.uuid4().hex}").strip()
        if not 1 <= len(self.owner_id) <= 256:
            raise ValueError("Remote Finalization Worker owner_id 无效。")
        self.clock = clock
        self.random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = EvolutionStableRemoteFinalizationDeliveryWorkerState.STOPPED
        self._pass_count = 0
        self._claimed_count = 0
        self._acknowledged_count = 0
        self._retry_scheduled_count = 0
        self._dead_lettered_count = 0
        self._failure_count = 0
        self._forced_shutdown_count = 0
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
        self._state = EvolutionStableRemoteFinalizationDeliveryWorkerState.WAITING
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(), name="naumi-stable-finalization-delivery"
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = EvolutionStableRemoteFinalizationDeliveryWorkerState.STOPPING
        self._stop_event.set()
        self._wake_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.policy.shutdown_drain_seconds)
        except TimeoutError:
            self._forced_shutdown_count += 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
        return True

    def wake(self) -> bool:
        if self._task is None or self._task.done() or self._stop_event.is_set():
            return False
        self._wake_event.set()
        return True

    async def run_once(
        self,
    ) -> EvolutionStableRemoteFinalizationDeliveryPassResult:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(
        self,
    ) -> EvolutionStableRemoteFinalizationDeliveryPassResult:
        self._state = EvolutionStableRemoteFinalizationDeliveryWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        claimed = acknowledged = retry_scheduled = dead_lettered = failures = 0
        failure_codes: list[str] = []
        for _ in range(self.policy.scan_limit):
            if self._stop_event.is_set():
                break
            now = self._now()
            try:
                view = await self.store.claim(
                    owner_id=self.owner_id,
                    now=now.isoformat(),
                    lease_seconds=self.policy.claim_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                failure_codes.append("stable_remote_delivery_claim_failed")
                break
            if view is None:
                break
            claimed += 1
            event = view.latest_event
            try:
                ack = await asyncio.wait_for(
                    self.transport.receive(view.package),
                    timeout=self.policy.ack_timeout_seconds,
                )
                await self.service.acknowledge(
                    delivery_id=view.package.delivery_id,
                    ack_base64=encode_stable_remote_finalization_delivery_ack(ack),
                )
                acknowledged += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, retryable = _failure(exc)
                failure_codes.append(code)
                failures += 1
                try:
                    if not retryable or event.attempt_count >= self.policy.max_attempts:
                        await self.store.dead_letter(
                            delivery_id=view.package.delivery_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._now().isoformat(),
                        )
                        dead_lettered += 1
                    else:
                        await self.store.retry(
                            delivery_id=view.package.delivery_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._now().isoformat(),
                            base_seconds=self.policy.retry_base_seconds,
                            max_seconds=self.policy.retry_max_seconds,
                        )
                        retry_scheduled += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failure_codes.append("stable_remote_delivery_failure_settlement_failed")
                    failures += 1

        result = EvolutionStableRemoteFinalizationDeliveryPassResult(
            claimed=claimed,
            acknowledged=acknowledged,
            retry_scheduled=retry_scheduled,
            dead_lettered=dead_lettered,
            failures=failures,
            failure_codes=tuple(failure_codes),
        )
        self._record(result)
        return result

    def snapshot(self) -> EvolutionStableRemoteFinalizationDeliveryWorkerSnapshot:
        return EvolutionStableRemoteFinalizationDeliveryWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            claimed_count=self._claimed_count,
            acknowledged_count=self._acknowledged_count,
            retry_scheduled_count=self._retry_scheduled_count,
            dead_lettered_count=self._dead_lettered_count,
            failure_count=self._failure_count,
            forced_shutdown_count=self._forced_shutdown_count,
            consecutive_empty_passes=self._consecutive_empty_passes,
            consecutive_failure_passes=self._consecutive_failure_passes,
            next_delay_seconds=self._next_delay_seconds,
            last_failure_codes=self._last_failure_codes,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    def _record(self, result: EvolutionStableRemoteFinalizationDeliveryPassResult) -> None:
        self._pass_count += 1
        self._claimed_count += result.claimed
        self._acknowledged_count += result.acknowledged
        self._retry_scheduled_count += result.retry_scheduled
        self._dead_lettered_count += result.dead_lettered
        self._failure_count += result.failures
        self._last_failure_codes = result.failure_codes
        if result.failures:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes += 1
            delay = min(
                self.policy.max_failure_backoff_seconds,
                self.policy.interval_seconds * 2 ** min(20, self._consecutive_failure_passes - 1),
            )
        elif result.claimed == 0:
            self._consecutive_failure_passes = 0
            self._consecutive_empty_passes += 1
            delay = min(
                self.policy.max_empty_backoff_seconds,
                self.policy.interval_seconds * 2 ** min(20, self._consecutive_empty_passes),
            )
        else:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes = 0
            delay = self.policy.interval_seconds
        self._next_delay_seconds = self._jittered(delay)
        self._state = (
            EvolutionStableRemoteFinalizationDeliveryWorkerState.STOPPING
            if self._stop_event.is_set()
            else EvolutionStableRemoteFinalizationDeliveryWorkerState.WAITING
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._wait(self._next_delay_seconds)
                if not self._stop_event.is_set():
                    await self.run_once()
        finally:
            self._state = EvolutionStableRemoteFinalizationDeliveryWorkerState.STOPPED

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

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Remote Finalization Worker 时钟必须包含时区。")
        return value.astimezone(UTC)

    def _timestamp(self) -> str:
        return self._now().isoformat()

    def _jittered(self, delay: float) -> float:
        sample = self.random_value()
        if not _finite(sample, 0, 1):
            raise ValueError("Remote Finalization Worker random source 必须在 0..1。")
        factor = 1 + self.policy.jitter_ratio * (2 * float(sample) - 1)
        return max(0.0, delay * factor)


def _failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, TimeoutError):
        return "stable_remote_delivery_ack_timeout", True
    if isinstance(exc, EvolutionStableRemoteFinalizationTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, EvolutionStableRemoteFinalizationDeliveryError):
        permanent = exc.code in {
            "stable_remote_delivery_ack_binding_invalid",
            "stable_remote_delivery_ack_conflict",
            "stable_remote_delivery_artifact_invalid",
        }
        return exc.code, not permanent
    if isinstance(exc, (OSError, ConnectionError)):
        return "stable_remote_delivery_transport_unavailable", True
    return "stable_remote_delivery_transport_failed", True


def render_stable_remote_finalization_delivery_pass(
    result: EvolutionStableRemoteFinalizationDeliveryPassResult,
) -> str:
    return "\n".join(
        (
            "## Remote Finalization Delivery Worker",
            "",
            "- 状态：**本轮完成**",
            f"- Claimed：`{result.claimed}`",
            f"- ACK：`{result.acknowledged}`",
            f"- Retry：`{result.retry_scheduled}`",
            f"- Dead letter：`{result.dead_lettered}`",
            f"- Failures：`{result.failures}`",
            f"- Failure codes：`{', '.join(result.failure_codes) or 'none'}`",
        )
    )


def render_stable_remote_finalization_delivery_worker(
    snapshot: EvolutionStableRemoteFinalizationDeliveryWorkerSnapshot,
) -> str:
    return "\n".join(
        (
            "## Remote Finalization Delivery Worker",
            "",
            f"- 状态：**{snapshot.state.value}**",
            f"- Passes：`{snapshot.pass_count}`",
            f"- Claimed：`{snapshot.claimed_count}`",
            f"- ACK：`{snapshot.acknowledged_count}`",
            f"- Retry：`{snapshot.retry_scheduled_count}`",
            f"- Dead letter：`{snapshot.dead_lettered_count}`",
            f"- Failures：`{snapshot.failure_count}`",
            f"- Forced shutdown：`{snapshot.forced_shutdown_count}`",
            f"- Next delay：`{snapshot.next_delay_seconds:.3f}s`",
            f"- Failure codes：`{', '.join(snapshot.last_failure_codes) or 'none'}`",
        )
    )


def _finite(value: object, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


__all__ = [
    "EvolutionStableRemoteFinalizationDeliveryPassResult",
    "EvolutionStableRemoteFinalizationDeliveryWorker",
    "EvolutionStableRemoteFinalizationDeliveryWorkerPolicy",
    "EvolutionStableRemoteFinalizationDeliveryWorkerSnapshot",
    "EvolutionStableRemoteFinalizationDeliveryWorkerState",
    "EvolutionStableRemoteFinalizationInstallationTransport",
    "EvolutionStableRemoteFinalizationTransportError",
    "LocalStableRemoteFinalizationInstallationTransport",
    "render_stable_remote_finalization_delivery_pass",
    "render_stable_remote_finalization_delivery_worker",
]
