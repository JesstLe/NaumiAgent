"""Reusable lifecycle producer for durable Harness heartbeats."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import (
    HarnessRuntimeReleaseBinding,
)

NowProvider = Callable[[], str]
SleepProvider = Callable[[float], Awaitable[None]]
FailureCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HeartbeatLifecycleDetailCodes:
    """Finite public reason codes emitted by one lifecycle producer family."""

    starting: str = "runtime_starting"
    running: str = "runtime_ready"
    alive: str = "runtime_alive"
    waiting: str = "runtime_waiting"
    waiting_alive: str = "runtime_waiting_alive"
    resumed: str = "runtime_resumed"
    draining: str = "runtime_draining"
    stopped: str = "runtime_stopped"
    failed: str = "runtime_shutdown_failed"


class HeartbeatProducerPort(Protocol):
    """Minimum persistence port needed by a heartbeat producer."""

    async def record_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
        instance_id: str,
        epoch: int,
        sequence: int,
        phase: HarnessHeartbeatPhase | str,
        observed_at: str,
        timeout_seconds: int,
        detail_code: str = "ok",
    ) -> HarnessHeartbeat: ...

    async def record_runtime_release_binding_startup(
        self,
        *,
        binding: HarnessRuntimeReleaseBinding,
        observed_at: str,
        timeout_seconds: int,
        detail_code: str,
    ) -> HarnessHeartbeat: ...


class RuntimeHeartbeatProducer:
    """Publish one independently identified runtime lifecycle and keep it fresh."""

    def __init__(
        self,
        *,
        port: HeartbeatProducerPort,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind,
        subject_id: str,
        instance_id: str,
        epoch: int = 1,
        interval_seconds: float = 10.0,
        timeout_seconds: int = 30,
        now_provider: NowProvider,
        sleep_provider: SleepProvider = asyncio.sleep,
        on_failure: FailureCallback | None = None,
        auto_pulse: bool = True,
        detail_codes: HeartbeatLifecycleDetailCodes | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 3 <= timeout_seconds <= 86_400
        ):
            raise ValueError("Heartbeat timeout 必须在 3 到 86400 秒之间。")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or not 0 < interval < timeout_seconds:
            raise ValueError("Heartbeat interval 必须大于 0 且小于 timeout。")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("Heartbeat epoch 必须是大于或等于 1 的整数。")
        codes = detail_codes or HeartbeatLifecycleDetailCodes()
        if not isinstance(codes, HeartbeatLifecycleDetailCodes):
            raise TypeError("detail_codes 必须是 HeartbeatLifecycleDetailCodes。")
        self._port = port
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.instance_id = instance_id
        self.epoch = epoch
        self.interval_seconds = interval
        self.timeout_seconds = timeout_seconds
        self.detail_codes = codes
        self._now = now_provider
        self._sleep = sleep_provider
        self._on_failure = on_failure
        self._auto_pulse = auto_pulse
        self._sequence = 0
        self._phase: HarnessHeartbeatPhase | None = None
        self._pulse_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._failure_code = ""

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def phase(self) -> HarnessHeartbeatPhase | None:
        return self._phase

    @property
    def failure_code(self) -> str:
        return self._failure_code

    async def start(
        self,
        *,
        startup_binding: HarnessRuntimeReleaseBinding | None = None,
    ) -> HarnessHeartbeat:
        """Persist startup and ready boundaries before scheduling pulses."""
        if self._started or self._closed:
            raise RuntimeError("Heartbeat producer 不能重复启动。")
        self._started = True
        try:
            if startup_binding is None:
                await self._record(
                    HarnessHeartbeatPhase.STARTING,
                    self.detail_codes.starting,
                )
            else:
                await self._record_starting_with_binding(startup_binding)
            heartbeat = await self._record(
                HarnessHeartbeatPhase.RUNNING,
                self.detail_codes.running,
            )
        except Exception:
            self._failure_code = "heartbeat_start_failed"
            self._closed = True
            raise
        if self._auto_pulse:
            self._pulse_task = asyncio.create_task(
                self._pulse_loop(),
                name=f"naumi-heartbeat-{self.subject_id}",
            )
        return heartbeat

    async def _record_starting_with_binding(
        self,
        binding: HarnessRuntimeReleaseBinding,
    ) -> HarnessHeartbeat:
        if not (
            binding.workspace_root == str(self.workspace_root)
            and binding.subject_id == self.subject_id
            and binding.instance_id == self.instance_id
            and binding.epoch == self.epoch
        ):
            raise ValueError("Runtime Release Binding 与 heartbeat producer 不一致。")
        self._sequence += 1
        heartbeat = await self._port.record_runtime_release_binding_startup(
            binding=binding,
            observed_at=self._now(),
            timeout_seconds=self.timeout_seconds,
            detail_code=self.detail_codes.starting,
        )
        self._phase = heartbeat.phase
        return heartbeat

    async def pulse_now(self) -> HarnessHeartbeat:
        """Write one liveness observation without erasing a waiting boundary."""
        if not self._started or self._closed:
            raise RuntimeError("Heartbeat producer 尚未运行。")
        if self._phase not in {
            HarnessHeartbeatPhase.RUNNING,
            HarnessHeartbeatPhase.WAITING,
        }:
            raise RuntimeError("只有 running 或 waiting Heartbeat 可以继续 pulse。")
        if self._phase is HarnessHeartbeatPhase.WAITING:
            return await self._record(
                HarnessHeartbeatPhase.WAITING,
                self.detail_codes.waiting_alive,
            )
        return await self._record(
            HarnessHeartbeatPhase.RUNNING,
            self.detail_codes.alive,
        )

    async def enter_waiting(
        self,
        *,
        detail_code: str | None = None,
    ) -> HarnessHeartbeat:
        """Persist a live waiting boundary while periodic pulses continue."""
        if not self._started or self._closed:
            raise RuntimeError("Heartbeat producer 尚未运行。")
        if self._phase not in {
            HarnessHeartbeatPhase.RUNNING,
            HarnessHeartbeatPhase.WAITING,
        }:
            raise RuntimeError("只有 running 或 waiting Heartbeat 可以进入等待。")
        return await self._record(
            HarnessHeartbeatPhase.WAITING,
            detail_code or self.detail_codes.waiting,
        )

    async def resume_running(
        self,
        *,
        detail_code: str | None = None,
    ) -> HarnessHeartbeat:
        """Persist an explicit waiting-to-running transition."""
        if not self._started or self._closed:
            raise RuntimeError("Heartbeat producer 尚未运行。")
        if self._phase is HarnessHeartbeatPhase.RUNNING:
            return await self._record(
                HarnessHeartbeatPhase.RUNNING,
                detail_code or self.detail_codes.resumed,
            )
        if self._phase is not HarnessHeartbeatPhase.WAITING:
            raise RuntimeError("只有 waiting Heartbeat 可以恢复运行。")
        return await self._record(
            HarnessHeartbeatPhase.RUNNING,
            detail_code or self.detail_codes.resumed,
        )

    async def begin_draining(self) -> HarnessHeartbeat | None:
        """Stop periodic pulses and persist the shutdown boundary once."""
        if not self._started or self._closed:
            return None
        await self._stop_pulse_task()
        if self._phase is HarnessHeartbeatPhase.DRAINING:
            return None
        return await self._record(
            HarnessHeartbeatPhase.DRAINING,
            self.detail_codes.draining,
        )

    async def close(self, *, detail_code: str | None = None) -> bool:
        """Persist a graceful terminal state; repeated close is idempotent."""
        if self._closed:
            return False
        self._closed = True
        await self._stop_pulse_task()
        if not self._started:
            return False
        heartbeat = await self._record(
            HarnessHeartbeatPhase.STOPPED,
            detail_code or self.detail_codes.stopped,
        )
        return heartbeat.phase is HarnessHeartbeatPhase.STOPPED

    async def fail(self, *, detail_code: str | None = None) -> bool:
        """Persist a terminal failure when graceful shutdown cannot complete."""
        if self._closed:
            return False
        self._closed = True
        await self._stop_pulse_task()
        if not self._started:
            return False
        heartbeat = await self._record(
            HarnessHeartbeatPhase.FAILED,
            detail_code or self.detail_codes.failed,
        )
        return heartbeat.phase is HarnessHeartbeatPhase.FAILED

    async def _pulse_loop(self) -> None:
        while not self._closed:
            try:
                await self._sleep(self.interval_seconds)
                if self._closed:
                    return
                await self.pulse_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failure_code = "heartbeat_write_failed"
                if self._on_failure is not None:
                    try:
                        await self._on_failure(self._failure_code)
                    except Exception:
                        pass
                return

    async def _stop_pulse_task(self) -> None:
        task = self._pulse_task
        self._pulse_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _record(
        self,
        phase: HarnessHeartbeatPhase,
        detail_code: str,
    ) -> HarnessHeartbeat:
        self._sequence += 1
        heartbeat = await self._port.record_heartbeat(
            workspace_root=self.workspace_root,
            subject_kind=self.subject_kind,
            subject_id=self.subject_id,
            instance_id=self.instance_id,
            epoch=self.epoch,
            sequence=self._sequence,
            phase=phase,
            observed_at=self._now(),
            timeout_seconds=self.timeout_seconds,
            detail_code=detail_code,
        )
        self._phase = heartbeat.phase
        return heartbeat


__all__ = [
    "HeartbeatLifecycleDetailCodes",
    "HeartbeatProducerPort",
    "RuntimeHeartbeatProducer",
]
