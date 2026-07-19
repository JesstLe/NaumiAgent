"""Reusable lifecycle producer for durable Harness heartbeats."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind

NowProvider = Callable[[], str]
SleepProvider = Callable[[float], Awaitable[None]]
FailureCallback = Callable[[str], Awaitable[None]]


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
        interval_seconds: float = 10.0,
        timeout_seconds: int = 30,
        now_provider: NowProvider,
        sleep_provider: SleepProvider = asyncio.sleep,
        on_failure: FailureCallback | None = None,
        auto_pulse: bool = True,
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
        self._port = port
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.instance_id = instance_id
        self.interval_seconds = interval
        self.timeout_seconds = timeout_seconds
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

    async def start(self) -> HarnessHeartbeat:
        """Persist startup and ready boundaries before scheduling pulses."""
        if self._started or self._closed:
            raise RuntimeError("Heartbeat producer 不能重复启动。")
        self._started = True
        try:
            await self._record(HarnessHeartbeatPhase.STARTING, "runtime_starting")
            heartbeat = await self._record(
                HarnessHeartbeatPhase.RUNNING,
                "runtime_ready",
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

    async def pulse_now(self) -> HarnessHeartbeat:
        """Write one explicit liveness observation while the worker is running."""
        if not self._started or self._closed:
            raise RuntimeError("Heartbeat producer 尚未运行。")
        if self._phase is not HarnessHeartbeatPhase.RUNNING:
            raise RuntimeError("只有 running Heartbeat 可以继续 pulse。")
        return await self._record(HarnessHeartbeatPhase.RUNNING, "runtime_alive")

    async def begin_draining(self) -> HarnessHeartbeat | None:
        """Stop periodic pulses and persist the shutdown boundary once."""
        if not self._started or self._closed:
            return None
        await self._stop_pulse_task()
        if self._phase is HarnessHeartbeatPhase.DRAINING:
            return None
        return await self._record(
            HarnessHeartbeatPhase.DRAINING,
            "runtime_draining",
        )

    async def close(self) -> bool:
        """Persist a graceful terminal state; repeated close is idempotent."""
        if self._closed:
            return False
        self._closed = True
        await self._stop_pulse_task()
        if not self._started:
            return False
        heartbeat = await self._record(
            HarnessHeartbeatPhase.STOPPED,
            "runtime_stopped",
        )
        return heartbeat.phase is HarnessHeartbeatPhase.STOPPED

    async def fail(self) -> bool:
        """Persist a terminal failure when graceful shutdown cannot complete."""
        if self._closed:
            return False
        self._closed = True
        await self._stop_pulse_task()
        if not self._started:
            return False
        heartbeat = await self._record(
            HarnessHeartbeatPhase.FAILED,
            "runtime_shutdown_failed",
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
            epoch=1,
            sequence=self._sequence,
            phase=phase,
            observed_at=self._now(),
            timeout_seconds=self.timeout_seconds,
            detail_code=detail_code,
        )
        self._phase = heartbeat.phase
        return heartbeat


__all__ = ["HeartbeatProducerPort", "RuntimeHeartbeatProducer"]
