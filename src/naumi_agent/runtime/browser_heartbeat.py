"""Durable heartbeat lifecycle for one browser task execution."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from naumi_agent.harness.heartbeat_runtime import (
    FailureCallback,
    HeartbeatLifecycleDetailCodes,
    RuntimeHeartbeatProducer,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore


class BrowserExecutionHeartbeatState(StrEnum):
    """Coordinator state for one browser heartbeat producer."""

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BrowserExecutionHeartbeatSnapshot:
    """Bounded public projection safe to persist with a browser run."""

    subject_id: str
    instance_id: str
    epoch: int
    state: BrowserExecutionHeartbeatState
    phase: str
    failure_code: str


class BrowserExecutionHeartbeatLifecycle:
    """Own one producer across running, waiting, resume, and terminal states."""

    def __init__(self, *, producer: RuntimeHeartbeatProducer) -> None:
        if producer.subject_kind is not HarnessRunKind.BROWSER:
            raise ValueError("Browser execution heartbeat 必须使用 browser subject kind。")
        self._producer = producer
        self._state = BrowserExecutionHeartbeatState.CREATED
        self._failure_code = ""
        self._terminal = False

    async def start(self) -> bool:
        if self._state is not BrowserExecutionHeartbeatState.CREATED:
            return False
        try:
            await self._producer.start()
        except Exception:
            self._state = BrowserExecutionHeartbeatState.FAILED
            self._failure_code = "browser_heartbeat_start_failed"
            raise
        self._state = BrowserExecutionHeartbeatState.RUNNING
        return True

    async def enter_waiting(self, *, mode: str = "instruction") -> bool:
        if self._terminal:
            return False
        normalized = str(mode or "instruction").strip().lower()
        detail_code = (
            "browser_manual_control"
            if normalized == "manual_control"
            else "browser_waiting_instruction"
        )
        try:
            await self._producer.enter_waiting(detail_code=detail_code)
        except Exception:
            self._failure_code = "browser_heartbeat_waiting_failed"
            raise
        self._state = BrowserExecutionHeartbeatState.WAITING
        return True

    async def resume(self) -> bool:
        if self._terminal or self._state is not BrowserExecutionHeartbeatState.WAITING:
            return False
        try:
            await self._producer.resume_running(detail_code="browser_resumed")
        except Exception:
            self._failure_code = "browser_heartbeat_resume_failed"
            raise
        self._state = BrowserExecutionHeartbeatState.RUNNING
        return True

    async def finish(self, result_status: str) -> bool:
        """Persist one terminal state without changing the browser task outcome."""
        if self._terminal:
            return False
        self._terminal = True
        normalized = str(result_status or "failed").strip().lower()
        try:
            await self._producer.begin_draining()
            if normalized == "completed":
                committed = await self._producer.close(
                    detail_code="browser_completed",
                )
                self._state = BrowserExecutionHeartbeatState.STOPPED
            elif normalized == "aborted":
                committed = await self._producer.close(
                    detail_code="browser_aborted",
                )
                self._state = BrowserExecutionHeartbeatState.STOPPED
            else:
                detail_code = (
                    "browser_runtime_interrupted"
                    if normalized == "interrupted"
                    else "browser_failed"
                )
                committed = await self._producer.fail(detail_code=detail_code)
                self._state = BrowserExecutionHeartbeatState.FAILED
            return committed
        except Exception:
            self._state = BrowserExecutionHeartbeatState.FAILED
            self._failure_code = "browser_heartbeat_terminal_failed"
            raise

    def snapshot(self) -> BrowserExecutionHeartbeatSnapshot:
        phase = self._producer.phase
        return BrowserExecutionHeartbeatSnapshot(
            subject_id=self._producer.subject_id,
            instance_id=self._producer.instance_id,
            epoch=self._producer.epoch,
            state=self._state,
            phase=phase.value if phase is not None else "",
            failure_code=self._failure_code or self._producer.failure_code,
        )


class BrowserExecutionHeartbeatFactory:
    """Create isolated, restart-safe browser execution heartbeats."""

    def __init__(
        self,
        *,
        store: HarnessStore,
        workspace_root: str | Path,
        interval_seconds: float = 10.0,
        timeout_seconds: int = 30,
        now_provider: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
        auto_pulse: bool = True,
    ) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("store 必须是 HarnessStore。")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 3 <= timeout_seconds <= 86_400
        ):
            raise ValueError("Browser heartbeat timeout 必须在 3 到 86400 秒之间。")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or not 0 < interval < timeout_seconds:
            raise ValueError("Browser heartbeat interval 必须大于 0 且小于 timeout。")
        if not isinstance(auto_pulse, bool):
            raise TypeError("auto_pulse 必须是布尔值。")
        self.store = store
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.interval_seconds = interval
        self.timeout_seconds = timeout_seconds
        self._now = now_provider
        self._auto_pulse = auto_pulse

    async def create(
        self,
        *,
        run_id: str,
        on_failure: FailureCallback | None = None,
    ) -> BrowserExecutionHeartbeatLifecycle:
        subject_id = _browser_execution_subject_id(
            workspace_root=self.workspace_root,
            run_id=run_id,
        )
        current = await self.store.get_heartbeat(
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.BROWSER,
            subject_id=subject_id,
        )
        epoch = 1 if current is None else current.epoch + 1
        producer = RuntimeHeartbeatProducer(
            port=self.store,
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.BROWSER,
            subject_id=subject_id,
            instance_id=f"browser-instance-{uuid.uuid4().hex}",
            epoch=epoch,
            interval_seconds=self.interval_seconds,
            timeout_seconds=self.timeout_seconds,
            now_provider=self._now,
            on_failure=on_failure,
            auto_pulse=self._auto_pulse,
            detail_codes=HeartbeatLifecycleDetailCodes(
                starting="browser_starting",
                running="browser_running",
                alive="browser_alive",
                waiting="browser_waiting_instruction",
                waiting_alive="browser_waiting_alive",
                resumed="browser_resumed",
                draining="browser_draining",
                stopped="browser_stopped",
                failed="browser_failed",
            ),
        )
        return BrowserExecutionHeartbeatLifecycle(producer=producer)

    async def record_interrupted(
        self,
        *,
        run_id: str,
    ) -> BrowserExecutionHeartbeatSnapshot:
        """Supersede an abandoned epoch with an explicit failed terminal record."""
        lifecycle = await self.create(run_id=run_id)
        await lifecycle.start()
        await lifecycle.finish("interrupted")
        return lifecycle.snapshot()


def _browser_execution_subject_id(*, workspace_root: Path, run_id: str) -> str:
    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("Browser heartbeat run_id 不能为空。")
    payload = "\x00".join((str(workspace_root), normalized)).encode("utf-8")
    return f"browser-execution-{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "BrowserExecutionHeartbeatFactory",
    "BrowserExecutionHeartbeatLifecycle",
    "BrowserExecutionHeartbeatSnapshot",
    "BrowserExecutionHeartbeatState",
]
