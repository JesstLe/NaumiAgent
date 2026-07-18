"""Harness-backed lease lifecycle for one Pursuit executor."""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import (
    HarnessRunFenceReceipt,
    HarnessRunKind,
    HarnessRunLease,
)

NowProvider = Callable[[], str]
SleepProvider = Callable[[float], Awaitable[None]]


class PursuitLeasePort(Protocol):
    """Minimum durable Harness authority consumed by Pursuit."""

    async def acquire_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> HarnessRunLease | None: ...

    async def renew_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        epoch: int,
        now: str,
        lease_seconds: int,
    ) -> HarnessRunLease | None: ...

    async def release_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        epoch: int,
        now: str,
    ) -> HarnessRunLease | None: ...

    async def get_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
    ) -> HarnessRunLease | None: ...

    async def record_run_fence_decision(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        operation_id: str,
        owner_id: str,
        epoch: int,
        checked_at: str,
    ) -> HarnessRunFenceReceipt: ...

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

    async def get_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
    ) -> HarnessHeartbeat | None: ...


class PursuitLeaseError(RuntimeError):
    """Base failure for safe Pursuit execution ownership."""


class PursuitLeaseUnavailableError(PursuitLeaseError):
    """Raised when another live executor owns the run."""

    def __init__(self, lease: HarnessRunLease | None) -> None:
        self.lease = lease
        if lease is None:
            message = "目标追踪租约暂不可用，请稍后重试。"
        else:
            message = (
                f"目标追踪正由 {lease.owner_id} 执行，租约到期时间为 "
                f"{lease.expires_at}。"
            )
        super().__init__(message)


class PursuitLeaseLostError(PursuitLeaseError):
    """Raised after renewal or fencing proves ownership was lost."""


class PursuitLeaseSession:
    """Own, renew, fence, and release one Pursuit run lease."""

    def __init__(
        self,
        *,
        port: PursuitLeasePort,
        workspace_root: str | Path,
        run_id: str,
        owner_id: str | None = None,
        lease_seconds: int = 300,
        renew_interval_seconds: float | None = None,
        now_provider: NowProvider | None = None,
        sleep_provider: SleepProvider = asyncio.sleep,
        auto_renew: bool = True,
    ) -> None:
        if not 3 <= lease_seconds <= 86_400:
            raise ValueError("Pursuit lease_seconds 必须在 3 到 86400 之间。")
        interval = (
            float(renew_interval_seconds)
            if renew_interval_seconds is not None
            else max(1.0, lease_seconds / 3)
        )
        if not 0 < interval < lease_seconds:
            raise ValueError("Pursuit renew interval 必须小于 lease_seconds。")
        self._port = port
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.run_id = run_id
        self.owner_id = owner_id or f"pursuit-worker-{uuid.uuid4().hex[:24]}"
        self.lease_seconds = lease_seconds
        self.renew_interval_seconds = interval
        self._now = now_provider or _utc_now
        self._sleep = sleep_provider
        self._auto_renew = auto_renew
        self._lease: HarnessRunLease | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._lost_reason = ""
        self._fence_sequence = 0
        self._heartbeat_sequence = 0
        self._closed = False

    @property
    def lease(self) -> HarnessRunLease | None:
        return self._lease

    @property
    def epoch(self) -> int:
        return self._lease.epoch if self._lease is not None else 0

    @property
    def is_owned(self) -> bool:
        return self._lease is not None and not self._lost_reason and not self._closed

    @property
    def lost_reason(self) -> str:
        return self._lost_reason

    async def acquire(self) -> HarnessRunLease:
        """Acquire once and start the keepalive only after durable ownership."""
        if self._lease is not None or self._closed:
            raise PursuitLeaseError("Pursuit lease session 不能重复获取。")
        timestamp = self._now()
        lease = await self._port.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.PURSUIT,
            run_id=self.run_id,
            owner_id=self.owner_id,
            now=timestamp,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            current = await self._port.get_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.PURSUIT,
                run_id=self.run_id,
            )
            raise PursuitLeaseUnavailableError(current)
        self._lease = lease
        try:
            existing_heartbeat = await self._port.get_heartbeat(
                workspace_root=self.workspace_root,
                subject_kind=HarnessRunKind.PURSUIT,
                subject_id=self.run_id,
            )
            if (
                existing_heartbeat is not None
                and existing_heartbeat.epoch == lease.epoch
                and existing_heartbeat.instance_id == self.owner_id
            ):
                self._heartbeat_sequence = existing_heartbeat.sequence
            await self._record_heartbeat(
                HarnessHeartbeatPhase.RUNNING,
                observed_at=timestamp,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._port.release_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.PURSUIT,
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    epoch=lease.epoch,
                    now=timestamp,
                )
            self._lease = None
            self._mark_lost(f"Worker 心跳初始化失败：{type(exc).__name__}")
            raise PursuitLeaseError(self._lost_reason) from exc
        if self._auto_renew:
            self._renew_task = asyncio.create_task(
                self._renew_loop(),
                name=f"naumi-pursuit-lease-{self.run_id}",
            )
        return lease

    async def renew_now(self) -> HarnessRunLease:
        """Renew the exact current epoch or fail closed."""
        lease = self._require_local_ownership()
        timestamp = self._now()
        try:
            renewed = await self._port.renew_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.PURSUIT,
                run_id=self.run_id,
                owner_id=self.owner_id,
                epoch=lease.epoch,
                now=timestamp,
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            self._mark_lost(f"租约续租失败：{type(exc).__name__}")
            raise PursuitLeaseLostError(self._lost_reason) from exc
        if renewed is None:
            self._mark_lost("租约已过期、被接管或 epoch 已变化。")
            raise PursuitLeaseLostError(self._lost_reason)
        self._lease = renewed
        try:
            await self._record_heartbeat(
                HarnessHeartbeatPhase.RUNNING,
                observed_at=timestamp,
            )
        except Exception as exc:
            self._mark_lost(f"Worker 心跳写入失败：{type(exc).__name__}")
            raise PursuitLeaseLostError(self._lost_reason) from exc
        return renewed

    async def require_current(self, boundary: str) -> HarnessRunFenceReceipt:
        """Mechanically fence one result/state commit boundary."""
        lease = self._require_local_ownership()
        self._fence_sequence += 1
        operation_id = (
            f"pursuit-fence-{lease.epoch}-{self._fence_sequence}-"
            f"{_safe_boundary(boundary)}"
        )[:128]
        try:
            receipt = await self._port.record_run_fence_decision(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.PURSUIT,
                run_id=self.run_id,
                operation_id=operation_id,
                owner_id=self.owner_id,
                epoch=lease.epoch,
                checked_at=self._now(),
            )
        except Exception as exc:
            self._mark_lost(f"租约 fencing 检查失败：{type(exc).__name__}")
            raise PursuitLeaseLostError(self._lost_reason) from exc
        if not receipt.accepted:
            self._mark_lost(f"租约 fencing 已拒绝：{receipt.reason.value}")
            raise PursuitLeaseLostError(self._lost_reason)
        return receipt

    async def close(self) -> bool:
        """Stop keepalive and release only the still-owned exact epoch."""
        if self._closed:
            return False
        self._closed = True
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        lease = self._lease
        if lease is None or self._lost_reason:
            return False
        timestamp = self._now()
        try:
            released = await self._port.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.PURSUIT,
                run_id=self.run_id,
                owner_id=self.owner_id,
                epoch=lease.epoch,
                now=timestamp,
            )
        except Exception:
            return False
        if released is None:
            return False
        with contextlib.suppress(Exception):
            await self._record_heartbeat(
                HarnessHeartbeatPhase.STOPPED,
                observed_at=timestamp,
            )
        return True

    async def _renew_loop(self) -> None:
        while not self._closed and not self._lost_reason:
            await self._sleep(self.renew_interval_seconds)
            if self._closed:
                return
            try:
                await self.renew_now()
            except PursuitLeaseLostError:
                return

    def _require_local_ownership(self) -> HarnessRunLease:
        if self._lost_reason:
            raise PursuitLeaseLostError(self._lost_reason)
        if self._lease is None or self._closed:
            raise PursuitLeaseLostError("目标追踪没有有效运行租约。")
        return self._lease

    async def _record_heartbeat(
        self,
        phase: HarnessHeartbeatPhase,
        *,
        observed_at: str,
    ) -> HarnessHeartbeat:
        lease = self._lease
        if lease is None:
            raise PursuitLeaseLostError("目标追踪没有可记录心跳的租约。")
        self._heartbeat_sequence += 1
        return await self._port.record_heartbeat(
            workspace_root=self.workspace_root,
            subject_kind=HarnessRunKind.PURSUIT,
            subject_id=self.run_id,
            instance_id=self.owner_id,
            epoch=lease.epoch,
            sequence=self._heartbeat_sequence,
            phase=phase,
            observed_at=observed_at,
            timeout_seconds=max(
                3,
                min(
                    self.lease_seconds,
                    math.ceil(self.renew_interval_seconds * 2.5),
                ),
            ),
            detail_code="lease_active" if phase is not HarnessHeartbeatPhase.STOPPED
            else "lease_released",
        )

    def _mark_lost(self, reason: str) -> None:
        if not self._lost_reason:
            self._lost_reason = reason


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_boundary(value: str) -> str:
    normalized = "".join(
        char if char.isascii() and (char.isalnum() or char in "._:-") else "-"
        for char in str(value or "boundary")
    ).strip("-")
    return normalized[:64] or "boundary"


__all__ = [
    "PursuitLeaseError",
    "PursuitLeaseLostError",
    "PursuitLeasePort",
    "PursuitLeaseSession",
    "PursuitLeaseUnavailableError",
]
