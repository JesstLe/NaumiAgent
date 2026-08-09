"""Shared terminal runtime lifecycle assembled outside UI adapters."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
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
from naumi_agent.harness.runtime_release_binding import (
    HarnessRuntimeReleaseBinding,
    build_runtime_release_binding,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import (
    ReleaseRuntimeIdentity,
    ReleaseRuntimeIdentityError,
)

TerminalSurface = Literal["new_ui", "tui"]
RuntimeIdentityProvider = Callable[[], ReleaseRuntimeIdentity | None]
_RUN_RELEASE_BINDING_CONTEXT: ContextVar[HarnessRuntimeReleaseBinding | None] = (
    ContextVar("naumi_run_release_binding", default=None)
)


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
    managed_release: bool
    release_binding_id: str
    release_identity_id: str
    release_binding_error_code: str


class TerminalRuntimeLifecycle:
    """Coordinate heartbeat and retention boundaries for one terminal frontend."""

    def __init__(
        self,
        *,
        surface: TerminalSurface,
        producer: RuntimeHeartbeatProducer,
        retention: RuntimeHeartbeatRetentionService | None,
        runtime_identity_provider: RuntimeIdentityProvider | None = None,
        binding_now_provider: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        self.surface = surface
        self.subject_id = producer.subject_id
        self._producer = producer
        self._retention = retention
        self._runtime_identity_provider = runtime_identity_provider
        self._binding_now = binding_now_provider
        self._release_binding: HarnessRuntimeReleaseBinding | None = None
        self._release_binding_error_code = ""
        self._state = TerminalRuntimeState.CREATED
        self._last_error_code = ""
        self._terminal_closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        async with self._lock:
            if self._state is not TerminalRuntimeState.CREATED:
                return False
            self._state = TerminalRuntimeState.STARTING
            startup_binding = self._resolve_release_binding()
            try:
                await self._producer.start(startup_binding=startup_binding)
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
            managed_release=self._release_binding is not None,
            release_binding_id=(
                "" if self._release_binding is None else self._release_binding.binding_id
            ),
            release_identity_id=(
                ""
                if self._release_binding is None
                else self._release_binding.runtime_identity.identity_id
            ),
            release_binding_error_code=self._release_binding_error_code,
        )

    def release_binding(self) -> HarnessRuntimeReleaseBinding | None:
        """Return the immutable managed-release source fact, if startup created one."""
        return self._release_binding

    @contextmanager
    def run_release_context(self):
        """Scope this lifecycle's exact binding to one task-local engine run."""
        binding = (
            self._release_binding
            if self._state is TerminalRuntimeState.RUNNING and not self._terminal_closed
            else None
        )
        token = _RUN_RELEASE_BINDING_CONTEXT.set(binding)
        try:
            yield binding
        finally:
            _RUN_RELEASE_BINDING_CONTEXT.reset(token)

    def _resolve_release_binding(self) -> HarnessRuntimeReleaseBinding | None:
        provider = self._runtime_identity_provider
        if provider is None:
            return None
        try:
            identity = provider()
            if identity is None:
                return None
            binding = build_runtime_release_binding(
                workspace_root=self._producer.workspace_root,
                surface=self.surface,
                subject_id=self._producer.subject_id,
                instance_id=self._producer.instance_id,
                epoch=self._producer.epoch,
                runtime_identity=identity,
                bound_at=self._binding_now(),
            )
        except ReleaseRuntimeIdentityError as exc:
            self._release_binding_error_code = exc.code
            return None
        except (OSError, TypeError, ValueError):
            self._release_binding_error_code = "runtime_release_binding_invalid"
            return None
        self._release_binding = binding
        return binding

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
        runtime_identity_provider: RuntimeIdentityProvider | None = None,
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
        self._runtime_identity_provider = runtime_identity_provider

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
            runtime_identity_provider=self._runtime_identity_provider,
            binding_now_provider=self._now,
        )


def current_terminal_run_release_binding() -> HarnessRuntimeReleaseBinding | None:
    """Read the task-local release source without leaking it to concurrent runs."""
    return _RUN_RELEASE_BINDING_CONTEXT.get()


@contextmanager
def terminal_run_release_context(
    lifecycle: TerminalRuntimeLifecycle | None,
):
    """Bind one optional lifecycle while keeping concurrent task contexts isolated."""
    if lifecycle is not None and not isinstance(lifecycle, TerminalRuntimeLifecycle):
        raise TypeError("terminal run release context 需要 TerminalRuntimeLifecycle。")
    if lifecycle is not None:
        with lifecycle.run_release_context() as binding:
            yield binding
        return
    token = _RUN_RELEASE_BINDING_CONTEXT.set(None)
    try:
        yield None
    finally:
        _RUN_RELEASE_BINDING_CONTEXT.reset(token)


__all__ = [
    "TerminalRuntimeLifecycle",
    "TerminalRuntimeLifecycleFactory",
    "TerminalRuntimeSnapshot",
    "TerminalRuntimeState",
    "TerminalSurface",
    "RuntimeIdentityProvider",
    "current_terminal_run_release_binding",
    "terminal_run_release_context",
]
