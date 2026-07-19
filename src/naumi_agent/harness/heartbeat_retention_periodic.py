"""Lease-coordinated periodic retention for runtime heartbeat snapshots."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from naumi_agent.harness.heartbeat import (
    HarnessHeartbeatHealth,
    RuntimeHeartbeatCatalogPage,
    RuntimeHeartbeatPruneReceipt,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLease

_RETENTION_RUN_ID = "runtime-heartbeat-retention"
_PROTECTED_HEALTH = frozenset(
    {
        HarnessHeartbeatHealth.STARTING,
        HarnessHeartbeatHealth.HEALTHY,
        HarnessHeartbeatHealth.DRAINING,
        HarnessHeartbeatHealth.STALE,
        HarnessHeartbeatHealth.CLOCK_REGRESSION,
    }
)


class RuntimeHeartbeatRetentionPort(Protocol):
    async def acquire_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...
    async def renew_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...
    async def release_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...
    async def list_runtime_heartbeats(self, **kwargs: object) -> RuntimeHeartbeatCatalogPage: ...
    async def prune_runtime_heartbeats(self, **kwargs: object) -> RuntimeHeartbeatPruneReceipt: ...


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeatRetentionPolicy:
    interval_seconds: float = 21_600
    standby_retry_seconds: float = 60
    retention_seconds: int = 604_800
    lease_seconds: int = 60
    scan_limit: int = 100
    catalog_limit: int = 200

    def __post_init__(self) -> None:
        if isinstance(self.interval_seconds, bool) or not 0 < self.interval_seconds <= 604_800:
            raise ValueError("runtime heartbeat retention interval 必须在 0 到 7 天之间。")
        if (
            isinstance(self.standby_retry_seconds, bool)
            or not 0 < self.standby_retry_seconds <= 3600
        ):
            raise ValueError("runtime heartbeat retention standby retry 必须在 0 到 1 小时之间。")
        if (
            isinstance(self.retention_seconds, bool)
            or not 259_200 <= self.retention_seconds <= 31_536_000
        ):
            raise ValueError("runtime heartbeat 保留期必须在 3 天到 1 年之间。")
        if isinstance(self.lease_seconds, bool) or not 3 <= self.lease_seconds <= 86_400:
            raise ValueError("runtime heartbeat retention lease 必须在 3 到 86400 秒之间。")
        if isinstance(self.scan_limit, bool) or not 1 <= self.scan_limit <= 1000:
            raise ValueError("runtime heartbeat retention scan limit 必须在 1 到 1000 之间。")
        if isinstance(self.catalog_limit, bool) or not 1 <= self.catalog_limit <= 200:
            raise ValueError("runtime heartbeat retention catalog limit 必须在 1 到 200 之间。")


class RuntimeHeartbeatRetentionState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    STANDBY = "standby"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeatRetentionSnapshot:
    state: RuntimeHeartbeatRetentionState
    cycle_count: int
    deleted_count: int
    failure_count: int
    last_error_code: str
    last_cycle_at: str
    next_delay_seconds: float


class RuntimeHeartbeatRetentionService:
    """Run bounded cleanup cycles while holding one workspace runtime lease."""

    def __init__(
        self,
        *,
        port: RuntimeHeartbeatRetentionPort,
        workspace_root: str | Path,
        policy: RuntimeHeartbeatRetentionPolicy | None = None,
        owner_id: str | None = None,
        protected_subject_ids: Callable[[], tuple[str, ...]] = tuple,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._port = port
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.policy = policy or RuntimeHeartbeatRetentionPolicy()
        self.owner_id = owner_id or f"runtime-retention-{uuid.uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.owner_id):
            raise ValueError("runtime heartbeat retention owner_id 无效。")
        self._protected_subject_ids = protected_subject_ids
        self._now = now
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = RuntimeHeartbeatRetentionState.STOPPED
        self._cycle_count = 0
        self._deleted_count = 0
        self._failure_count = 0
        self._last_error_code = ""
        self._last_cycle_at = ""
        self._next_delay = self.policy.interval_seconds

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="naumi-runtime-heartbeat-retention")
        return True

    async def stop(self) -> bool:
        if self._task is None or self._task.done():
            return False
        self._stop.set()
        await self._task
        self._task = None
        return True

    async def run_cycle(self) -> RuntimeHeartbeatPruneReceipt | None:
        try:
            timestamp = self._timestamp()
            self._last_cycle_at = timestamp
            lease = await self._port.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=_RETENTION_RUN_ID,
                owner_id=self.owner_id,
                now=timestamp,
                lease_seconds=self.policy.lease_seconds,
            )
        except Exception:
            self._record_failure("lease_acquire_failed")
            return None
        if lease is None:
            self._state = RuntimeHeartbeatRetentionState.STANDBY
            self._next_delay = self.policy.standby_retry_seconds
            return None
        self._state = RuntimeHeartbeatRetentionState.RUNNING
        try:
            catalog = await self._port.list_runtime_heartbeats(
                workspace_root=self.workspace_root,
                assessed_at=timestamp,
                limit=self.policy.catalog_limit,
            )
            protected = set(self._protected_subject_ids())
            protected.update(
                item.heartbeat.subject_id
                for item in catalog.items
                if item.health in _PROTECTED_HEALTH
            )
            renewed = await self._port.renew_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=_RETENTION_RUN_ID,
                owner_id=self.owner_id,
                epoch=lease.epoch,
                now=self._timestamp(),
                lease_seconds=self.policy.lease_seconds,
            )
            if renewed is None:
                self._record_failure("lease_lost")
                return None
            cutoff = (
                datetime.fromisoformat(timestamp) - timedelta(seconds=self.policy.retention_seconds)
            ).isoformat()
            receipt = await self._port.prune_runtime_heartbeats(
                workspace_root=self.workspace_root,
                observed_before=cutoff,
                assessed_at=timestamp,
                limit=self.policy.scan_limit,
                protected_subject_ids=tuple(sorted(protected)),
            )
            self._cycle_count += 1
            self._deleted_count += receipt.deleted_count
            self._last_error_code = ""
            self._state = RuntimeHeartbeatRetentionState.WAITING
            self._next_delay = self.policy.interval_seconds
            return receipt
        except Exception:
            self._record_failure("cycle_failed")
            return None
        finally:
            try:
                await self._port.release_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.RUNTIME,
                    run_id=_RETENTION_RUN_ID,
                    owner_id=self.owner_id,
                    epoch=lease.epoch,
                    now=self._timestamp(),
                )
            except Exception:
                self._record_failure("lease_release_failed")

    def snapshot(self) -> RuntimeHeartbeatRetentionSnapshot:
        return RuntimeHeartbeatRetentionSnapshot(
            self._state,
            self._cycle_count,
            self._deleted_count,
            self._failure_count,
            self._last_error_code,
            self._last_cycle_at,
            self._next_delay,
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self.run_cycle()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._next_delay)
                except TimeoutError:
                    pass
        finally:
            self._state = RuntimeHeartbeatRetentionState.STOPPED

    def _record_failure(self, code: str) -> None:
        self._failure_count += 1
        self._last_error_code = code
        self._state = RuntimeHeartbeatRetentionState.FAILED
        self._next_delay = self.policy.standby_retry_seconds

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime heartbeat retention 时钟必须包含时区。")
        return value.astimezone(UTC).isoformat()


__all__ = [
    "RuntimeHeartbeatRetentionPolicy",
    "RuntimeHeartbeatRetentionService",
    "RuntimeHeartbeatRetentionSnapshot",
    "RuntimeHeartbeatRetentionState",
]
