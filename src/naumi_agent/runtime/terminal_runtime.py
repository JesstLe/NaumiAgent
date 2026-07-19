"""Shared terminal runtime lifecycle assembled outside UI adapters."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from naumi_agent.config.settings import RuntimeHeartbeatRetentionConfig
from naumi_agent.harness.heartbeat_retention_periodic import (
    RuntimeHeartbeatRetentionPolicy,
    RuntimeHeartbeatRetentionService,
    RuntimeHeartbeatRetentionSnapshot,
)
from naumi_agent.harness.heartbeat_runtime import (
    FailureCallback,
    RuntimeHeartbeatProducer,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore

TerminalSurface = Literal["new_ui", "tui"]


class TerminalRuntimeState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TerminalRuntimeSnapshot:
    surface: TerminalSurface
    subject_id: str
    state: TerminalRuntimeState
    heartbeat_phase: str
    heartbeat_failure_code: str
    retention: RuntimeHeartbeatRetentionSnapshot | None
    last_error_code: str


class TerminalRuntimeLifecycle:
    """Coordinate heartbeat and retention boundaries for one terminal frontend."""

    def __init__(
        self,
        *,
        surface: TerminalSurface,
        producer: RuntimeHeartbeatProducer,
        retention: RuntimeHeartbeatRetentionService | None,
    ) -> None:
        self.surface = surface
        self.subject_id = producer.subject_id
        self._producer = producer
        self._retention = retention
        self._state = TerminalRuntimeState.CREATED
        self._last_error_code = ""
        self._terminal_closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        async with self._lock:
            if self._state is not TerminalRuntimeState.CREATED:
                return False
            self._state = TerminalRuntimeState.STARTING
            try:
                await self._producer.start()
            except Exception:
                self._state = TerminalRuntimeState.FAILED
                self._last_error_code = "heartbeat_start_failed"
                raise
            if self._retention is not None:
                try:
                    self._retention.start()
                except Exception:
                    self._last_error_code = "retention_start_failed"
                    try:
                        await self._producer.begin_draining()
                        await self._producer.close()
                    except Exception:
                        pass
                    self._state = TerminalRuntimeState.FAILED
                    raise
            self._state = TerminalRuntimeState.RUNNING
            return True

    async def begin_draining(self) -> bool:
        async with self._lock:
            if self._state is not TerminalRuntimeState.RUNNING:
                return False
            await self._stop_retention()
            try:
                await self._producer.begin_draining()
            except Exception:
                self._state = TerminalRuntimeState.FAILED
                self._last_error_code = "heartbeat_draining_failed"
                raise
            self._state = TerminalRuntimeState.DRAINING
            return True

    async def close(self, *, failed: bool = False) -> bool:
        async with self._lock:
            if self._terminal_closed:
                return False
            await self._stop_retention()
            try:
                committed = (
                    await self._producer.fail()
                    if failed
                    else await self._producer.close()
                )
            except Exception:
                self._state = TerminalRuntimeState.FAILED
                self._last_error_code = "heartbeat_terminal_failed"
                self._terminal_closed = True
                raise
            self._terminal_closed = True
            self._state = (
                TerminalRuntimeState.FAILED
                if failed
                else TerminalRuntimeState.STOPPED
            )
            return committed

    def snapshot(self) -> TerminalRuntimeSnapshot:
        phase = self._producer.phase
        return TerminalRuntimeSnapshot(
            surface=self.surface,
            subject_id=self.subject_id,
            state=self._state,
            heartbeat_phase=phase.value if phase is not None else "",
            heartbeat_failure_code=self._producer.failure_code,
            retention=(
                self._retention.snapshot()
                if self._retention is not None
                else None
            ),
            last_error_code=self._last_error_code,
        )

    async def _stop_retention(self) -> None:
        if self._retention is None:
            return
        try:
            await self._retention.stop()
        except Exception:
            self._last_error_code = "retention_stop_failed"


class TerminalRuntimeLifecycleFactory:
    """Create isolated frontend lifecycles from composition-owned dependencies."""

    def __init__(
        self,
        *,
        store: HarnessStore,
        workspace_root: str | Path,
        retention_config: RuntimeHeartbeatRetentionConfig,
        heartbeat_interval_seconds: float = 10.0,
        heartbeat_timeout_seconds: int = 30,
        now_provider: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("store 必须是 HarnessStore。")
        if not isinstance(retention_config, RuntimeHeartbeatRetentionConfig):
            raise TypeError(
                "retention_config 必须是 RuntimeHeartbeatRetentionConfig。"
            )
        self.store = store
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.retention_config = retention_config.model_copy(deep=True)
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._now = now_provider

    def create(
        self,
        *,
        surface: TerminalSurface,
        identity: str | None = None,
        on_heartbeat_failure: FailureCallback | None = None,
    ) -> TerminalRuntimeLifecycle:
        if surface not in {"new_ui", "tui"}:
            raise ValueError("terminal surface 必须是 new_ui 或 tui。")
        runtime_id = identity or f"{surface}-{uuid.uuid4().hex}"
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", runtime_id):
            raise ValueError("terminal runtime identity 无效。")
        producer = RuntimeHeartbeatProducer(
            port=self.store,
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id=runtime_id,
            instance_id=runtime_id,
            interval_seconds=self.heartbeat_interval_seconds,
            timeout_seconds=self.heartbeat_timeout_seconds,
            now_provider=self._now,
            on_failure=on_heartbeat_failure,
        )
        retention = None
        if self.retention_config.enabled:
            retention = RuntimeHeartbeatRetentionService(
                port=self.store,
                workspace_root=self.workspace_root,
                policy=RuntimeHeartbeatRetentionPolicy(
                    interval_seconds=self.retention_config.interval_seconds,
                    standby_retry_seconds=(
                        self.retention_config.standby_retry_seconds
                    ),
                    retention_seconds=self.retention_config.retention_days * 86_400,
                    lease_seconds=self.retention_config.lease_seconds,
                    scan_limit=self.retention_config.scan_limit,
                    catalog_limit=self.retention_config.catalog_limit,
                ),
                protected_subject_ids=lambda: (runtime_id,),
                now=lambda: datetime.fromisoformat(self._now()),
            )
        return TerminalRuntimeLifecycle(
            surface=surface,
            producer=producer,
            retention=retention,
        )


__all__ = [
    "TerminalRuntimeLifecycle",
    "TerminalRuntimeLifecycleFactory",
    "TerminalRuntimeSnapshot",
    "TerminalRuntimeState",
    "TerminalSurface",
]
