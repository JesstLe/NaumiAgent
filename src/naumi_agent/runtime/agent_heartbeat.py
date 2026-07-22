"""Durable heartbeat lifecycle for one delegated Agent execution."""

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
    HeartbeatLifecycleDetailCodes,
    RuntimeHeartbeatProducer,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore


class AgentExecutionHeartbeatState(StrEnum):
    """Local coordinator state; durable truth remains in HarnessStore."""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentExecutionHeartbeatSnapshot:
    subject_id: str
    instance_id: str
    epoch: int
    state: AgentExecutionHeartbeatState
    phase: str
    failure_code: str


class AgentExecutionHeartbeatLifecycle:
    """Own exactly one producer from admission through a terminal result."""

    def __init__(
        self,
        *,
        producer: RuntimeHeartbeatProducer,
    ) -> None:
        if producer.subject_kind is not HarnessRunKind.AGENT:
            raise ValueError("Agent execution heartbeat 必须使用 agent subject kind。")
        self._producer = producer
        self._state = AgentExecutionHeartbeatState.CREATED
        self._failure_code = ""
        self._terminal = False

    async def start(self) -> bool:
        if self._state is not AgentExecutionHeartbeatState.CREATED:
            return False
        try:
            await self._producer.start()
        except Exception:
            self._state = AgentExecutionHeartbeatState.FAILED
            self._failure_code = "agent_heartbeat_start_failed"
            raise
        self._state = AgentExecutionHeartbeatState.RUNNING
        return True

    async def finish(self, result_status: str) -> bool:
        """Persist one deterministic terminal phase without changing task outcome."""
        if self._terminal:
            return False
        self._terminal = True
        normalized = str(result_status or "error").strip().lower()
        try:
            await self._producer.begin_draining()
            if normalized == "completed":
                committed = await self._producer.close(
                    detail_code="agent_completed",
                )
                self._state = AgentExecutionHeartbeatState.STOPPED
            elif normalized == "cancelled":
                committed = await self._producer.close(
                    detail_code="agent_cancelled",
                )
                self._state = AgentExecutionHeartbeatState.STOPPED
            else:
                detail = {
                    "timeout": "agent_timeout",
                    "max_turns": "agent_max_turns",
                }.get(normalized, "agent_failed")
                committed = await self._producer.fail(detail_code=detail)
                self._state = AgentExecutionHeartbeatState.FAILED
            return committed
        except Exception:
            self._state = AgentExecutionHeartbeatState.FAILED
            self._failure_code = "agent_heartbeat_terminal_failed"
            raise

    def snapshot(self) -> AgentExecutionHeartbeatSnapshot:
        phase = self._producer.phase
        return AgentExecutionHeartbeatSnapshot(
            subject_id=self._producer.subject_id,
            instance_id=self._producer.instance_id,
            epoch=self._producer.epoch,
            state=self._state,
            phase=phase.value if phase is not None else "",
            failure_code=self._failure_code or self._producer.failure_code,
        )


class AgentExecutionHeartbeatFactory:
    """Create isolated, restart-safe Agent execution heartbeat lifecycles."""

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
            raise ValueError("Agent heartbeat timeout 必须在 3 到 86400 秒之间。")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or not 0 < interval < timeout_seconds:
            raise ValueError("Agent heartbeat interval 必须大于 0 且小于 timeout。")
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
        session_id: str,
        task_id: str,
        agent_name: str,
    ) -> AgentExecutionHeartbeatLifecycle:
        """Allocate the next epoch for a stable, non-sensitive execution subject."""
        subject_id = _agent_execution_subject_id(
            workspace_root=self.workspace_root,
            session_id=session_id,
            task_id=task_id,
            agent_name=agent_name,
        )
        current = await self.store.get_heartbeat(
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.AGENT,
            subject_id=subject_id,
        )
        epoch = 1 if current is None else current.epoch + 1
        instance_id = f"agent-instance-{uuid.uuid4().hex}"
        producer = RuntimeHeartbeatProducer(
            port=self.store,
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.AGENT,
            subject_id=subject_id,
            instance_id=instance_id,
            epoch=epoch,
            interval_seconds=self.interval_seconds,
            timeout_seconds=self.timeout_seconds,
            now_provider=self._now,
            auto_pulse=self._auto_pulse,
            detail_codes=HeartbeatLifecycleDetailCodes(
                starting="agent_starting",
                running="agent_running",
                alive="agent_alive",
                draining="agent_draining",
                stopped="agent_stopped",
                failed="agent_failed",
            ),
        )
        return AgentExecutionHeartbeatLifecycle(producer=producer)


def _agent_execution_subject_id(
    *,
    workspace_root: Path,
    session_id: str,
    task_id: str,
    agent_name: str,
) -> str:
    if not str(task_id or "").strip():
        raise ValueError("Agent heartbeat task_id 不能为空。")
    payload = "\x00".join((
        str(workspace_root),
        str(session_id or ""),
        str(task_id).strip(),
        str(agent_name or "").strip(),
    )).encode("utf-8")
    return f"agent-execution-{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "AgentExecutionHeartbeatFactory",
    "AgentExecutionHeartbeatLifecycle",
    "AgentExecutionHeartbeatSnapshot",
    "AgentExecutionHeartbeatState",
]
