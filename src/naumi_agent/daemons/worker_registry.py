"""Durable Runtime authority for worker incarnation registration and fencing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionDecision,
    WorkerAdmissionReason,
    WorkerAdmissionRequirements,
    WorkerAdmissionResult,
    WorkerCapability,
    WorkerContract,
    WorkerHealthReport,
    WorkerIsolationContract,
    WorkerKind,
    WorkerPlatform,
    WorkerResourceEnvelope,
    assess_worker_admission,
    normalize_worker_timestamp,
    verify_worker_contract,
)

WORKER_REGISTRY_SCHEMA_VERSION = 3
_MAX_CONTRACT_JSON_BYTES = 64 * 1024
_MAX_CAPACITY_WAITERS = 10_000


class WorkerRegistryStoreError(RuntimeError):
    """Raised when the worker authority cannot provide trustworthy state."""


class WorkerRegistryConflictError(WorkerRegistryStoreError):
    """Raised when a registration violates incarnation fencing."""


class WorkerCapacityExhaustedError(WorkerRegistryConflictError):
    """Raised when an atomic reservation would exceed worker capacity."""


class WorkerRegistrationState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class WorkerCapacityReservationState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    FENCED = "fenced"


class WorkerCapacityWaiterState(StrEnum):
    WAITING = "waiting"
    CLAIMED = "claimed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    contract: WorkerContract
    state: WorkerRegistrationState
    registered_at: str
    terminal_at: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class WorkerCapacityReservation:
    reservation_id: str
    worker_id: str
    instance_id: str
    epoch: int
    job_id: str
    state: WorkerCapacityReservationState
    reserved_at: str
    expires_at: str
    terminal_at: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class WorkerCapacitySnapshot:
    worker_id: str
    instance_id: str
    epoch: int
    maximum: int
    reserved: int
    available: int
    assessed_at: str


@dataclass(frozen=True, slots=True)
class WorkerCapacityWaiter:
    queue_id: str
    worker_id: str
    instance_id: str
    epoch: int
    job_id: str
    workspace_sha256: str
    state: WorkerCapacityWaiterState
    enqueued_at: str
    deadline_at: str
    reservation_id: str | None
    terminal_at: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class WorkerCapacityClaim:
    waiter: WorkerCapacityWaiter
    reservation: WorkerCapacityReservation


class WorkerRegistryStore:
    """SQLite-backed source of truth for the active incarnation of each worker."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Worker registry 路径必须是绝对路径。")
        path = unresolved.resolve(strict=False)
        self._db_path = path
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def register(
        self,
        contract: WorkerContract,
        *,
        registered_at: str,
    ) -> WorkerRegistration:
        """Register a higher incarnation or idempotently replay the active one."""
        if not isinstance(contract, WorkerContract):
            raise TypeError("contract 必须是 WorkerContract。")
        if not verify_worker_contract(contract):
            raise ValueError("Worker contract 摘要校验失败，拒绝注册。")
        timestamp = normalize_worker_timestamp(registered_at, field="registered_at")
        if datetime.fromisoformat(contract.issued_at) > datetime.fromisoformat(timestamp):
            raise ValueError("Worker contract issued_at 不能晚于 registered_at。")
        contract_json = _serialize_contract(contract)

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                latest_row = await _select_latest(db, contract.worker_id)
                if latest_row is not None:
                    latest = _registration_from_row(latest_row)
                    if latest.state is WorkerRegistrationState.SUPERSEDED:
                        raise WorkerRegistryStoreError(
                            "Worker registry 历史断裂：最高 epoch 不应为 superseded。"
                        )
                    if latest.contract.contract_sha256 == contract.contract_sha256:
                        if latest.state is not WorkerRegistrationState.ACTIVE:
                            raise WorkerRegistryConflictError(
                                "已终结的 Worker incarnation 不能重新激活。"
                            )
                        await db.commit()
                        return latest
                    _validate_takeover(latest.contract, contract)
                    boundary = latest.terminal_at or latest.registered_at
                    if datetime.fromisoformat(timestamp) < datetime.fromisoformat(boundary):
                        raise WorkerRegistryConflictError("新 Worker registered_at 发生回退。")
                    if latest.state is WorkerRegistrationState.ACTIVE:
                        await db.execute(
                            """
                            UPDATE worker_registrations
                            SET state = ?, terminal_at = ?, reason_code = ?
                            WHERE worker_id = ? AND epoch = ? AND state = ?
                            """,
                            (
                                WorkerRegistrationState.SUPERSEDED.value,
                                timestamp,
                                "higher_epoch_registered",
                                latest.contract.worker_id,
                                latest.contract.epoch,
                                WorkerRegistrationState.ACTIVE.value,
                            ),
                        )
                        await _fence_capacity_reservations(
                            db,
                            worker_id=latest.contract.worker_id,
                            epoch=latest.contract.epoch,
                            fenced_at=timestamp,
                            reason_code="higher_epoch_registered",
                        )
                        await _fence_capacity_waiters(
                            db,
                            worker_id=latest.contract.worker_id,
                            epoch=latest.contract.epoch,
                            fenced_at=timestamp,
                            reason_code="higher_epoch_registered",
                        )
                await db.execute(
                    """
                    INSERT INTO worker_registrations (
                        worker_id, epoch, instance_id, contract_sha256,
                        contract_json, state, registered_at, terminal_at,
                        reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        contract.worker_id,
                        contract.epoch,
                        contract.instance_id,
                        contract.contract_sha256,
                        contract_json,
                        WorkerRegistrationState.ACTIVE.value,
                        timestamp,
                    ),
                )
                row = await _select_registration(db, contract.worker_id, contract.epoch)
                await db.commit()
                assert row is not None
                return _registration_from_row(row)
        except WorkerRegistryConflictError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise WorkerRegistryConflictError("Worker 注册与现有 incarnation 冲突。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法持久化 Worker 注册。") from exc

    async def revoke(
        self,
        *,
        worker_id: str,
        instance_id: str,
        epoch: int,
        reason_code: str,
        revoked_at: str,
    ) -> WorkerRegistration:
        """Revoke exactly one active generation; stale callers cannot revoke newer work."""
        _validate_identifier(worker_id, field="worker_id")
        _validate_identifier(instance_id, field="instance_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        _validate_reason(reason_code)
        timestamp = normalize_worker_timestamp(revoked_at, field="revoked_at")

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_registration(db, worker_id, epoch)
                if row is None:
                    raise WorkerRegistryConflictError("Worker incarnation 不存在。")
                registration = _registration_from_row(row)
                if registration.contract.instance_id != instance_id:
                    raise WorkerRegistryConflictError("Worker instance 不匹配。")
                if registration.state is WorkerRegistrationState.REVOKED:
                    if registration.reason_code != reason_code:
                        raise WorkerRegistryConflictError("Worker 已由不同原因撤销。")
                    await db.commit()
                    return registration
                if registration.state is not WorkerRegistrationState.ACTIVE:
                    raise WorkerRegistryConflictError("旧 Worker incarnation 已被 fencing。")
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    registration.registered_at
                ):
                    raise WorkerRegistryConflictError("revoked_at 早于 registered_at。")
                await db.execute(
                    """
                    UPDATE worker_registrations
                    SET state = ?, terminal_at = ?, reason_code = ?
                    WHERE worker_id = ? AND epoch = ? AND state = ?
                    """,
                    (
                        WorkerRegistrationState.REVOKED.value,
                        timestamp,
                        reason_code,
                        worker_id,
                        epoch,
                        WorkerRegistrationState.ACTIVE.value,
                    ),
                )
                await _fence_capacity_reservations(
                    db,
                    worker_id=worker_id,
                    epoch=epoch,
                    fenced_at=timestamp,
                    reason_code=reason_code,
                )
                await _fence_capacity_waiters(
                    db,
                    worker_id=worker_id,
                    epoch=epoch,
                    fenced_at=timestamp,
                    reason_code=reason_code,
                )
                updated = await _select_registration(db, worker_id, epoch)
                await db.commit()
                assert updated is not None
                return _registration_from_row(updated)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法撤销 Worker 注册。") from exc

    async def get_active(self, worker_id: str) -> WorkerRegistration | None:
        """Read the current authority snapshot without accepting caller contract state."""
        _validate_identifier(worker_id, field="worker_id")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_active(db, worker_id)
                return _registration_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取当前 Worker 注册。") from exc

    async def list_history(self, worker_id: str) -> tuple[WorkerRegistration, ...]:
        _validate_identifier(worker_id, field="worker_id")
        if not _registry_file_exists(self._db_path):
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM worker_registrations
                    WHERE worker_id = ? ORDER BY epoch ASC
                    """,
                    (worker_id,),
                )
                return tuple(_registration_from_row(row) for row in await cursor.fetchall())
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 Worker 注册历史。") from exc

    async def reserve_capacity(
        self,
        *,
        reservation_id: str,
        worker_id: str,
        instance_id: str,
        epoch: int,
        job_id: str,
        reserved_at: str,
        ttl_seconds: int,
    ) -> WorkerCapacityReservation:
        """Atomically reserve one slot on the exact active worker incarnation."""
        for field, value in (
            ("reservation_id", reservation_id),
            ("worker_id", worker_id),
            ("instance_id", instance_id),
            ("job_id", job_id),
        ):
            _validate_identifier(value, field=field)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("ttl_seconds 必须在 1 到 604800 之间。")
        timestamp = normalize_worker_timestamp(reserved_at, field="reserved_at")
        expires_at = (
            datetime.fromisoformat(timestamp) + timedelta(seconds=ttl_seconds)
        ).isoformat()

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                registration_row = await _select_active(db, worker_id)
                if registration_row is None:
                    raise WorkerRegistryConflictError("Worker 当前没有 active incarnation。")
                registration = _registration_from_row(registration_row)
                if (
                    registration.contract.instance_id != instance_id
                    or registration.contract.epoch != epoch
                ):
                    raise WorkerRegistryConflictError(
                        "Worker capacity reservation incarnation 已被 fencing。"
                    )
                if ttl_seconds > registration.contract.resources.max_wall_seconds:
                    raise WorkerRegistryConflictError(
                        "Capacity reservation TTL 超过 Worker max_wall_seconds。"
                    )
                await _expire_capacity_reservations(db, worker_id=worker_id, now=timestamp)
                existing = await _select_capacity_reservation(db, reservation_id)
                if existing is not None:
                    reservation = _capacity_reservation_from_row(existing)
                    if (
                        reservation.worker_id != worker_id
                        or reservation.instance_id != instance_id
                        or reservation.epoch != epoch
                        or reservation.job_id != job_id
                    ):
                        raise WorkerRegistryConflictError(
                            "Capacity reservation identity 被不同事实复用。"
                        )
                    if reservation.state is not WorkerCapacityReservationState.ACTIVE:
                        raise WorkerRegistryConflictError(
                            "已终结的 capacity reservation 不能重新激活。"
                        )
                    await db.commit()
                    return reservation
                duplicate = await _select_capacity_job(db, worker_id, epoch, job_id)
                if duplicate is not None:
                    raise WorkerRegistryConflictError(
                        "同一 Worker job 已使用其他 reservation identity。"
                    )
                active_count = await _count_active_capacity(db, worker_id, instance_id, epoch)
                if active_count >= registration.contract.resources.max_concurrent_jobs:
                    raise WorkerCapacityExhaustedError("Worker capacity 已耗尽。")
                await db.execute(
                    """
                    INSERT INTO worker_capacity_reservations (
                        reservation_id, worker_id, instance_id, epoch, job_id, state,
                        reserved_at, expires_at, terminal_at, reason_code
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
                    """,
                    (reservation_id, worker_id, instance_id, epoch, job_id, timestamp, expires_at),
                )
                row = await _select_capacity_reservation(db, reservation_id)
                await db.commit()
                assert row is not None
                return _capacity_reservation_from_row(row)
        except (WorkerRegistryConflictError, WorkerCapacityExhaustedError):
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法预留 Worker capacity。") from exc

    async def release_capacity(
        self,
        *,
        reservation_id: str,
        worker_id: str,
        instance_id: str,
        epoch: int,
        reason_code: str,
        released_at: str,
        accept_terminal: bool = False,
    ) -> WorkerCapacityReservation:
        """Release only the exact reservation owner; stale workers are fenced."""
        for field, value in (
            ("reservation_id", reservation_id),
            ("worker_id", worker_id),
            ("instance_id", instance_id),
        ):
            _validate_identifier(value, field=field)
        _validate_reason(reason_code)
        if not isinstance(accept_terminal, bool):
            raise TypeError("accept_terminal 必须是布尔值。")
        timestamp = normalize_worker_timestamp(released_at, field="released_at")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await _expire_capacity_reservations(db, worker_id=worker_id, now=timestamp)
                row = await _select_capacity_reservation(db, reservation_id)
                if row is None:
                    raise WorkerRegistryConflictError("Capacity reservation 不存在。")
                reservation = _capacity_reservation_from_row(row)
                if (
                    reservation.worker_id != worker_id
                    or reservation.instance_id != instance_id
                    or reservation.epoch != epoch
                ):
                    raise WorkerRegistryConflictError("Capacity reservation owner 不匹配。")
                if reservation.state is WorkerCapacityReservationState.RELEASED:
                    if reservation.reason_code != reason_code:
                        raise WorkerRegistryConflictError("Capacity reservation 已由不同原因释放。")
                    await db.commit()
                    return reservation
                if accept_terminal and reservation.state in {
                    WorkerCapacityReservationState.EXPIRED,
                    WorkerCapacityReservationState.FENCED,
                }:
                    await db.commit()
                    return reservation
                if reservation.state is not WorkerCapacityReservationState.ACTIVE:
                    raise WorkerRegistryConflictError("Capacity reservation 已被终结或 fencing。")
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    reservation.reserved_at
                ):
                    raise WorkerRegistryConflictError("released_at 早于 reserved_at。")
                await db.execute(
                    """
                    UPDATE worker_capacity_reservations
                    SET state = 'released', terminal_at = ?, reason_code = ?
                    WHERE reservation_id = ? AND state = 'active'
                    """,
                    (timestamp, reason_code, reservation_id),
                )
                updated = await _select_capacity_reservation(db, reservation_id)
                await db.commit()
                assert updated is not None
                return _capacity_reservation_from_row(updated)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法释放 Worker capacity。") from exc

    async def renew_capacity(
        self,
        *,
        reservation_id: str,
        worker_id: str,
        instance_id: str,
        epoch: int,
        job_id: str,
        renewed_at: str,
        ttl_seconds: int,
    ) -> WorkerCapacityReservation:
        """Renew one live slot on the exact active Worker incarnation."""
        for field, value in (
            ("reservation_id", reservation_id),
            ("worker_id", worker_id),
            ("instance_id", instance_id),
            ("job_id", job_id),
        ):
            _validate_identifier(value, field=field)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("ttl_seconds 必须在 1 到 604800 之间。")
        timestamp = normalize_worker_timestamp(renewed_at, field="renewed_at")
        expires_at = (
            datetime.fromisoformat(timestamp) + timedelta(seconds=ttl_seconds)
        ).isoformat()

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                registration_row = await _select_active(db, worker_id)
                if registration_row is None:
                    raise WorkerRegistryConflictError(
                        "Worker 当前没有 active incarnation。"
                    )
                registration = _registration_from_row(registration_row)
                if (
                    registration.contract.instance_id != instance_id
                    or registration.contract.epoch != epoch
                ):
                    raise WorkerRegistryConflictError(
                        "Worker capacity renewal incarnation 已被 fencing。"
                    )
                if ttl_seconds > registration.contract.resources.max_wall_seconds:
                    raise WorkerRegistryConflictError(
                        "Capacity renewal TTL 超过 Worker max_wall_seconds。"
                    )
                await _expire_capacity_reservations(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                row = await _select_capacity_reservation(db, reservation_id)
                if row is None:
                    raise WorkerRegistryConflictError(
                        "Capacity reservation 不存在。"
                    )
                reservation = _capacity_reservation_from_row(row)
                if (
                    reservation.worker_id != worker_id
                    or reservation.instance_id != instance_id
                    or reservation.epoch != epoch
                    or reservation.job_id != job_id
                ):
                    raise WorkerRegistryConflictError(
                        "Capacity reservation renewal owner 不匹配。"
                    )
                if reservation.state is not WorkerCapacityReservationState.ACTIVE:
                    raise WorkerRegistryConflictError(
                        "Capacity reservation 已过期、终结或 fencing。"
                    )
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    reservation.reserved_at
                ):
                    raise WorkerRegistryConflictError(
                        "renewed_at 早于 reserved_at。"
                    )
                if datetime.fromisoformat(expires_at) < datetime.fromisoformat(
                    reservation.expires_at
                ):
                    raise WorkerRegistryConflictError(
                        "Capacity renewal 不能缩短现有 lease。"
                    )
                await db.execute(
                    """
                    UPDATE worker_capacity_reservations
                    SET expires_at = ?
                    WHERE reservation_id = ? AND state = 'active'
                    """,
                    (expires_at, reservation_id),
                )
                updated = await _select_capacity_reservation(db, reservation_id)
                await db.commit()
                assert updated is not None
                return _capacity_reservation_from_row(updated)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法续期 Worker capacity。") from exc

    async def get_capacity_reservation(
        self,
        reservation_id: str,
        *,
        assessed_at: str,
    ) -> WorkerCapacityReservation | None:
        """Read one reservation after mechanically applying its TTL."""
        _validate_identifier(reservation_id, field="reservation_id")
        timestamp = normalize_worker_timestamp(assessed_at, field="assessed_at")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_capacity_reservation(db, reservation_id)
                if row is None:
                    await db.commit()
                    return None
                worker_id = str(row["worker_id"])
                await _expire_capacity_reservations(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                updated = await _select_capacity_reservation(db, reservation_id)
                await db.commit()
                assert updated is not None
                return _capacity_reservation_from_row(updated)
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 Worker capacity reservation。") from exc

    async def capacity_snapshot(
        self,
        *,
        worker_id: str,
        assessed_at: str,
    ) -> WorkerCapacitySnapshot | None:
        _validate_identifier(worker_id, field="worker_id")
        timestamp = normalize_worker_timestamp(assessed_at, field="assessed_at")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_active(db, worker_id)
                if row is None:
                    await db.commit()
                    return None
                registration = _registration_from_row(row)
                await _expire_capacity_reservations(db, worker_id=worker_id, now=timestamp)
                contract = registration.contract
                reserved = await _count_active_capacity(
                    db, worker_id, contract.instance_id, contract.epoch
                )
                await db.commit()
                maximum = contract.resources.max_concurrent_jobs
                return WorkerCapacitySnapshot(
                    worker_id=worker_id,
                    instance_id=contract.instance_id,
                    epoch=contract.epoch,
                    maximum=maximum,
                    reserved=reserved,
                    available=max(0, maximum - reserved),
                    assessed_at=timestamp,
                )
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 Worker capacity。") from exc

    async def enqueue_capacity_waiter(
        self,
        *,
        queue_id: str,
        worker_id: str,
        instance_id: str,
        epoch: int,
        job_id: str,
        workspace_sha256: str,
        enqueued_at: str,
        deadline_at: str,
        max_waiters: int,
    ) -> WorkerCapacityWaiter:
        """Persist one exact-incarnation FIFO waiter under a hard queue bound."""
        for field, value in (
            ("queue_id", queue_id),
            ("worker_id", worker_id),
            ("instance_id", instance_id),
            ("job_id", job_id),
        ):
            _validate_identifier(value, field=field)
        _validate_sha256(workspace_sha256, field="workspace_sha256")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        if isinstance(max_waiters, bool) or not isinstance(max_waiters, int):
            raise TypeError("max_waiters 必须是整数。")
        if not 0 <= max_waiters <= _MAX_CAPACITY_WAITERS:
            raise ValueError(f"max_waiters 必须在 0 到 {_MAX_CAPACITY_WAITERS} 之间。")
        enqueued = normalize_worker_timestamp(enqueued_at, field="enqueued_at")
        deadline = normalize_worker_timestamp(deadline_at, field="deadline_at")
        if datetime.fromisoformat(deadline) <= datetime.fromisoformat(enqueued):
            raise ValueError("Capacity waiter deadline_at 必须晚于 enqueued_at。")

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                registration_row = await _select_active(db, worker_id)
                if registration_row is None:
                    raise WorkerRegistryConflictError("Worker 当前没有 active incarnation。")
                registration = _registration_from_row(registration_row)
                if (
                    registration.contract.instance_id != instance_id
                    or registration.contract.epoch != epoch
                ):
                    raise WorkerRegistryConflictError(
                        "Worker capacity waiter incarnation 已被 fencing。"
                    )
                await _ensure_capacity_queue_policy(
                    db,
                    worker_id=worker_id,
                    instance_id=instance_id,
                    epoch=epoch,
                    max_waiters=max_waiters,
                    configured_at=enqueued,
                )
                await _expire_capacity_waiters(
                    db,
                    worker_id=worker_id,
                    now=enqueued,
                )
                existing_row = await _select_capacity_waiter(db, queue_id)
                if existing_row is not None:
                    existing = _capacity_waiter_from_row(existing_row)
                    await _validate_capacity_waiter_links(db, (existing,))
                    if (
                        existing.worker_id != worker_id
                        or existing.instance_id != instance_id
                        or existing.epoch != epoch
                        or existing.job_id != job_id
                        or existing.workspace_sha256 != workspace_sha256
                        or existing.enqueued_at != enqueued
                        or existing.deadline_at != deadline
                    ):
                        raise WorkerRegistryConflictError(
                            "Capacity waiter identity 被不同事实复用。"
                        )
                    await db.commit()
                    return existing
                duplicate = await _select_capacity_waiter_job(
                    db,
                    worker_id=worker_id,
                    epoch=epoch,
                    job_id=job_id,
                )
                if duplicate is not None:
                    raise WorkerRegistryConflictError(
                        "同一 Worker job 已使用其他 capacity waiter identity。"
                    )
                waiting_count = await _count_capacity_waiters(
                    db,
                    worker_id=worker_id,
                    instance_id=instance_id,
                    epoch=epoch,
                )
                if waiting_count >= max_waiters:
                    raise WorkerCapacityExhaustedError(
                        f"Worker capacity 等待队列已满（上限 {max_waiters}）。"
                    )
                await db.execute(
                    """
                    INSERT INTO worker_capacity_waiters (
                        queue_id, worker_id, instance_id, epoch, job_id,
                        workspace_sha256, state, enqueued_at, deadline_at,
                        reservation_id, terminal_at, reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        queue_id,
                        worker_id,
                        instance_id,
                        epoch,
                        job_id,
                        workspace_sha256,
                        enqueued,
                        deadline,
                    ),
                )
                row = await _select_capacity_waiter(db, queue_id)
                await db.commit()
                assert row is not None
                return _capacity_waiter_from_row(row)
        except (WorkerRegistryConflictError, WorkerCapacityExhaustedError):
            raise
        except aiosqlite.IntegrityError as exc:
            raise WorkerRegistryConflictError("Capacity waiter 与现有记录冲突。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法持久化 capacity waiter。") from exc

    async def cancel_capacity_waiter(
        self,
        *,
        queue_id: str,
        worker_id: str,
        instance_id: str,
        epoch: int,
        job_id: str,
        reason_code: str,
        cancelled_at: str,
    ) -> WorkerCapacityWaiter:
        """Cancel only the exact waiting request; claimed work cannot be withdrawn."""
        for field, value in (
            ("queue_id", queue_id),
            ("worker_id", worker_id),
            ("instance_id", instance_id),
            ("job_id", job_id),
        ):
            _validate_identifier(value, field=field)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        _validate_reason(reason_code)
        timestamp = normalize_worker_timestamp(cancelled_at, field="cancelled_at")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await _expire_capacity_waiters(db, worker_id=worker_id, now=timestamp)
                row = await _select_capacity_waiter(db, queue_id)
                if row is None:
                    raise WorkerRegistryConflictError("Capacity waiter 不存在。")
                waiter = _capacity_waiter_from_row(row)
                if (
                    waiter.worker_id != worker_id
                    or waiter.instance_id != instance_id
                    or waiter.epoch != epoch
                    or waiter.job_id != job_id
                ):
                    raise WorkerRegistryConflictError("Capacity waiter owner 不匹配。")
                if waiter.state is WorkerCapacityWaiterState.CANCELLED:
                    if waiter.reason_code != reason_code:
                        raise WorkerRegistryConflictError("Capacity waiter 已由不同原因取消。")
                    await db.commit()
                    return waiter
                if waiter.state is not WorkerCapacityWaiterState.WAITING:
                    raise WorkerRegistryConflictError("Capacity waiter 已终结，不能取消。")
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(waiter.enqueued_at):
                    raise WorkerRegistryConflictError(
                        "cancelled_at 早于 Capacity waiter enqueued_at。"
                    )
                await db.execute(
                    """
                    UPDATE worker_capacity_waiters
                    SET state = 'cancelled', terminal_at = ?, reason_code = ?
                    WHERE queue_id = ? AND state = 'waiting'
                    """,
                    (timestamp, reason_code, queue_id),
                )
                updated = await _select_capacity_waiter(db, queue_id)
                await db.commit()
                assert updated is not None
                return _capacity_waiter_from_row(updated)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法取消 capacity waiter。") from exc

    async def claim_next_capacity_waiter(
        self,
        *,
        worker_id: str,
        instance_id: str,
        epoch: int,
        claimed_at: str,
    ) -> WorkerCapacityClaim | None:
        """Atomically claim the FIFO head and reserve one worker slot."""
        _validate_identifier(worker_id, field="worker_id")
        _validate_identifier(instance_id, field="instance_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        timestamp = normalize_worker_timestamp(claimed_at, field="claimed_at")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                registration_row = await _select_active(db, worker_id)
                if registration_row is None:
                    raise WorkerRegistryConflictError("Worker 当前没有 active incarnation。")
                registration = _registration_from_row(registration_row)
                contract = registration.contract
                if contract.instance_id != instance_id or contract.epoch != epoch:
                    raise WorkerRegistryConflictError(
                        "Capacity waiter claim incarnation 已被 fencing。"
                    )
                await _expire_capacity_reservations(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                await _expire_capacity_waiters(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                active_count = await _count_active_capacity(
                    db,
                    worker_id,
                    instance_id,
                    epoch,
                )
                if active_count >= contract.resources.max_concurrent_jobs:
                    await db.commit()
                    return None
                waiter_row = await _select_next_capacity_waiter(
                    db,
                    worker_id=worker_id,
                    instance_id=instance_id,
                    epoch=epoch,
                )
                if waiter_row is None:
                    await db.commit()
                    return None
                waiter = _capacity_waiter_from_row(waiter_row)
                remaining = math.ceil(
                    (
                        datetime.fromisoformat(waiter.deadline_at)
                        - datetime.fromisoformat(timestamp)
                    ).total_seconds()
                )
                if remaining < 1:
                    raise WorkerRegistryStoreError("Capacity waiter expiry 与 FIFO claim 不一致。")
                ttl_seconds = min(
                    contract.resources.max_wall_seconds,
                    remaining,
                    7 * 24 * 60 * 60,
                )
                reservation_id = capacity_waiter_reservation_id(waiter.queue_id)
                duplicate = await _select_capacity_reservation(db, reservation_id)
                if duplicate is not None:
                    raise WorkerRegistryStoreError(
                        "Capacity waiter reservation identity 已被占用。"
                    )
                duplicate_job = await _select_capacity_job(
                    db,
                    worker_id,
                    epoch,
                    waiter.job_id,
                )
                if duplicate_job is not None:
                    raise WorkerRegistryStoreError("Capacity waiter job 已存在 reservation。")
                expires_at = (
                    datetime.fromisoformat(timestamp) + timedelta(seconds=ttl_seconds)
                ).isoformat()
                await db.execute(
                    """
                    INSERT INTO worker_capacity_reservations (
                        reservation_id, worker_id, instance_id, epoch, job_id, state,
                        reserved_at, expires_at, terminal_at, reason_code
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
                    """,
                    (
                        reservation_id,
                        worker_id,
                        instance_id,
                        epoch,
                        waiter.job_id,
                        timestamp,
                        expires_at,
                    ),
                )
                result = await db.execute(
                    """
                    UPDATE worker_capacity_waiters
                    SET state = 'claimed', reservation_id = ?,
                        terminal_at = ?, reason_code = 'capacity_claimed'
                    WHERE queue_id = ? AND state = 'waiting'
                    """,
                    (reservation_id, timestamp, waiter.queue_id),
                )
                if result.rowcount != 1:
                    raise WorkerRegistryStoreError("Capacity waiter FIFO claim 并发提交失败。")
                claimed_row = await _select_capacity_waiter(db, waiter.queue_id)
                reservation_row = await _select_capacity_reservation(
                    db,
                    reservation_id,
                )
                await db.commit()
                assert claimed_row is not None and reservation_row is not None
                return WorkerCapacityClaim(
                    waiter=_capacity_waiter_from_row(claimed_row),
                    reservation=_capacity_reservation_from_row(reservation_row),
                )
        except WorkerRegistryConflictError:
            raise
        except WorkerRegistryStoreError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise WorkerRegistryStoreError("Capacity waiter claim 与现有事实冲突。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法 claim capacity waiter。") from exc

    async def get_capacity_waiter(
        self,
        queue_id: str,
    ) -> WorkerCapacityWaiter | None:
        """Read one exact waiter and verify any claimed reservation linkage."""
        _validate_identifier(queue_id, field="queue_id")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                row = await _select_capacity_waiter(db, queue_id)
                if row is None:
                    await db.commit()
                    return None
                waiter = _capacity_waiter_from_row(row)
                await _validate_capacity_waiter_links(db, (waiter,))
                await db.commit()
                return waiter
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 capacity waiter。") from exc

    async def get_capacity_waiter_for_job(
        self,
        *,
        worker_id: str,
        epoch: int,
        job_id: str,
    ) -> WorkerCapacityWaiter | None:
        """Read the unique durable queue fact for one worker-incarnation job."""
        _validate_identifier(worker_id, field="worker_id")
        _validate_identifier(job_id, field="job_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                row = await _select_capacity_waiter_job(
                    db,
                    worker_id=worker_id,
                    epoch=epoch,
                    job_id=job_id,
                )
                if row is None:
                    await db.commit()
                    return None
                waiter = _capacity_waiter_from_row(row)
                await _validate_capacity_waiter_links(db, (waiter,))
                await db.commit()
                return waiter
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 job capacity waiter。") from exc

    async def get_capacity_claim_for_job(
        self,
        *,
        worker_id: str,
        epoch: int,
        job_id: str,
        assessed_at: str,
    ) -> WorkerCapacityClaim | None:
        """Read one claimed waiter and its current reservation as one snapshot."""
        _validate_identifier(worker_id, field="worker_id")
        _validate_identifier(job_id, field="job_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        timestamp = normalize_worker_timestamp(assessed_at, field="assessed_at")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await _expire_capacity_reservations(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                await _expire_capacity_waiters(
                    db,
                    worker_id=worker_id,
                    now=timestamp,
                )
                row = await _select_capacity_waiter_job(
                    db,
                    worker_id=worker_id,
                    epoch=epoch,
                    job_id=job_id,
                )
                if row is None:
                    await db.commit()
                    return None
                waiter = _capacity_waiter_from_row(row)
                if waiter.state is not WorkerCapacityWaiterState.CLAIMED:
                    raise WorkerRegistryConflictError(
                        "Capacity waiter 尚未被 claim。"
                    )
                await _validate_capacity_waiter_links(db, (waiter,))
                assert waiter.reservation_id is not None
                reservation_row = await _select_capacity_reservation(
                    db,
                    waiter.reservation_id,
                )
                assert reservation_row is not None
                reservation = _capacity_reservation_from_row(reservation_row)
                await db.commit()
                return WorkerCapacityClaim(waiter=waiter, reservation=reservation)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 job capacity claim。") from exc

    async def list_capacity_waiters(
        self,
        *,
        worker_id: str,
        assessed_at: str,
        limit: int = 100,
    ) -> tuple[WorkerCapacityWaiter, ...]:
        """Read a bounded oldest-first catalog after applying deterministic expiry."""
        _validate_identifier(worker_id, field="worker_id")
        timestamp = normalize_worker_timestamp(assessed_at, field="assessed_at")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是整数。")
        if not 1 <= limit <= 200:
            raise ValueError("limit 必须在 1 到 200 之间。")
        if not _registry_file_exists(self._db_path):
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await _expire_capacity_waiters(db, worker_id=worker_id, now=timestamp)
                cursor = await db.execute(
                    """
                    SELECT * FROM worker_capacity_waiters
                    WHERE worker_id = ?
                    ORDER BY enqueued_at ASC, queue_id ASC
                    LIMIT ?
                    """,
                    (worker_id, limit),
                )
                rows = await cursor.fetchall()
                waiters = tuple(_capacity_waiter_from_row(row) for row in rows)
                await _validate_capacity_waiter_links(db, waiters)
                await db.commit()
                return waiters
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 capacity waiter。") from exc

    async def assess_admission(
        self,
        *,
        worker_id: str,
        report: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        now: str,
    ) -> WorkerAdmissionResult:
        """Assess only the contract selected by this authority's active pointer."""
        checked_at = normalize_worker_timestamp(now, field="now")
        registration = await self.get_active(worker_id)
        if registration is None:
            return WorkerAdmissionResult(
                decision=WorkerAdmissionDecision.BLOCKED,
                reasons=(WorkerAdmissionReason.REGISTRATION_MISSING,),
                checked_at=checked_at,
                heartbeat_health=None,
            )
        return assess_worker_admission(
            registration.contract,
            report,
            requirements,
            now=checked_at,
        )

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            created_parent = not self._db_path.parent.exists()
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created_parent and os.name != "nt":
                self._db_path.parent.chmod(0o700)
            existed = self._db_path.exists()
            try:
                async with self._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute("PRAGMA user_version")
                    row = await cursor.fetchone()
                    version = int(row[0]) if row is not None else 0
                    if version == 0:
                        tables = await _user_tables(db)
                        if tables:
                            raise WorkerRegistryStoreError(
                                "Worker registry 是未知的未版本化数据库。"
                            )
                        for statement in _SCHEMA_V1_STATEMENTS:
                            await db.execute(statement)
                        for statement in _SCHEMA_V2_STATEMENTS:
                            await db.execute(statement)
                        for statement in _SCHEMA_V3_STATEMENTS:
                            await db.execute(statement)
                        await db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION}")
                    elif version == 1:
                        for statement in _SCHEMA_V2_STATEMENTS:
                            await db.execute(statement)
                        for statement in _SCHEMA_V3_STATEMENTS:
                            await db.execute(statement)
                        await db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION}")
                    elif version == 2:
                        for statement in _SCHEMA_V3_STATEMENTS:
                            await db.execute(statement)
                        await db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION}")
                    elif version != WORKER_REGISTRY_SCHEMA_VERSION:
                        raise WorkerRegistryStoreError(
                            f"Worker registry schema v{version} 不受支持；"
                            f"当前仅支持 v{WORKER_REGISTRY_SCHEMA_VERSION}。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except WorkerRegistryStoreError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise WorkerRegistryStoreError("无法初始化 Worker registry。") from exc

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path, timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db
        finally:
            await db.close()


def _validate_takeover(current: WorkerContract, incoming: WorkerContract) -> None:
    if incoming.epoch <= current.epoch:
        raise WorkerRegistryConflictError(
            f"Worker epoch {incoming.epoch} 未高于当前 epoch {current.epoch}。"
        )
    if incoming.instance_id == current.instance_id:
        raise WorkerRegistryConflictError("新 epoch 必须使用新的 instance_id。")
    if datetime.fromisoformat(incoming.issued_at) < datetime.fromisoformat(current.issued_at):
        raise WorkerRegistryConflictError("新 Worker contract issued_at 发生回退。")


def _serialize_contract(contract: WorkerContract) -> str:
    payload = _json_value(asdict(contract))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_JSON_BYTES:
        raise ValueError("Worker contract 超过持久化大小上限。")
    return encoded


def _deserialize_contract(raw: str) -> WorkerContract:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CONTRACT_JSON_BYTES:
        raise ValueError("持久化 Worker contract 大小无效。")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "worker_id",
        "instance_id",
        "epoch",
        "kind",
        "protocol_min",
        "protocol_max",
        "software_version",
        "platform",
        "capabilities",
        "resources",
        "isolation",
        "issued_at",
        "contract_sha256",
    }:
        raise ValueError("持久化 Worker contract 字段集合无效。")
    platform = payload["platform"]
    resources = payload["resources"]
    isolation = payload["isolation"]
    capabilities = payload["capabilities"]
    if not isinstance(platform, dict) or not isinstance(resources, dict):
        raise ValueError("持久化 Worker contract 结构无效。")
    if not isinstance(isolation, dict) or not isinstance(capabilities, list):
        raise ValueError("持久化 Worker contract 能力结构无效。")
    if set(platform) != {
        "system",
        "machine",
        "python_implementation",
        "python_version",
    } or set(resources) != {
        "max_concurrent_jobs",
        "max_memory_bytes",
        "max_cpu_seconds",
        "max_wall_seconds",
        "max_output_bytes",
    }:
        raise ValueError("持久化 Worker contract 平台或资源字段无效。")
    if set(isolation) != {
        "ephemeral_workspace",
        "network_default_deny",
        "environment_allowlist",
        "resource_limits_enforced",
        "process_tree_cancel",
        "artifact_digest",
    }:
        raise ValueError("持久化 Worker contract 隔离字段无效。")
    try:
        contract = WorkerContract(
            schema_version=payload["schema_version"],
            worker_id=payload["worker_id"],
            instance_id=payload["instance_id"],
            epoch=payload["epoch"],
            kind=WorkerKind(payload["kind"]),
            protocol_min=payload["protocol_min"],
            protocol_max=payload["protocol_max"],
            software_version=payload["software_version"],
            platform=WorkerPlatform(**platform),
            capabilities=tuple(WorkerCapability(item) for item in capabilities),
            resources=WorkerResourceEnvelope(**resources),
            isolation=WorkerIsolationContract(**isolation),
            issued_at=payload["issued_at"],
            contract_sha256=payload["contract_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("持久化 Worker contract 内容无效。") from exc
    if not verify_worker_contract(contract):
        raise ValueError("持久化 Worker contract 摘要校验失败。")
    return contract


def _registration_from_row(row: aiosqlite.Row) -> WorkerRegistration:
    return deserialize_worker_registration(dict(row))


def _capacity_reservation_from_row(row: aiosqlite.Row) -> WorkerCapacityReservation:
    return deserialize_worker_capacity_reservation(dict(row))


def _capacity_waiter_from_row(row: aiosqlite.Row) -> WorkerCapacityWaiter:
    return deserialize_worker_capacity_waiter(dict(row))


def deserialize_worker_capacity_reservation(
    record: Mapping[str, object],
) -> WorkerCapacityReservation:
    """Validate one durable capacity record for strict read-only consumers."""
    required = {
        "reservation_id",
        "worker_id",
        "instance_id",
        "epoch",
        "job_id",
        "state",
        "reserved_at",
        "expires_at",
        "terminal_at",
        "reason_code",
    }
    if not isinstance(record, Mapping) or not required.issubset(record):
        raise ValueError("Worker capacity reservation 记录字段不完整。")
    for field in ("reservation_id", "worker_id", "instance_id", "job_id"):
        _validate_identifier(str(record[field]), field=field)
    epoch = int(record["epoch"])
    if epoch < 1:
        raise ValueError("Capacity reservation epoch 必须是正整数。")
    reserved_at = normalize_worker_timestamp(str(record["reserved_at"]), field="reserved_at")
    expires_at = normalize_worker_timestamp(str(record["expires_at"]), field="expires_at")
    terminal_at = (
        normalize_worker_timestamp(str(record["terminal_at"]), field="terminal_at")
        if record["terminal_at"] is not None
        else None
    )
    if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(reserved_at):
        raise ValueError("Capacity reservation expires_at 必须晚于 reserved_at。")
    reason = str(record["reason_code"]) if record["reason_code"] is not None else None
    if reason is not None:
        _validate_reason(reason)
    state = WorkerCapacityReservationState(str(record["state"]))
    if (state is WorkerCapacityReservationState.ACTIVE) != (terminal_at is None and reason is None):
        raise ValueError("Capacity reservation 终态字段不一致。")
    return WorkerCapacityReservation(
        reservation_id=str(record["reservation_id"]),
        worker_id=str(record["worker_id"]),
        instance_id=str(record["instance_id"]),
        epoch=epoch,
        job_id=str(record["job_id"]),
        state=state,
        reserved_at=reserved_at,
        expires_at=expires_at,
        terminal_at=terminal_at,
        reason_code=reason,
    )


def deserialize_worker_capacity_waiter(
    record: Mapping[str, object],
) -> WorkerCapacityWaiter:
    """Validate one durable queue record without trusting its index columns."""
    required = {
        "queue_id",
        "worker_id",
        "instance_id",
        "epoch",
        "job_id",
        "workspace_sha256",
        "state",
        "enqueued_at",
        "deadline_at",
        "reservation_id",
        "terminal_at",
        "reason_code",
    }
    if not isinstance(record, Mapping) or not required.issubset(record):
        raise ValueError("Worker capacity waiter 记录字段不完整。")
    for field in ("queue_id", "worker_id", "instance_id", "job_id"):
        _validate_identifier(str(record[field]), field=field)
    _validate_sha256(str(record["workspace_sha256"]), field="workspace_sha256")
    epoch = int(record["epoch"])
    if epoch < 1:
        raise ValueError("Capacity waiter epoch 必须是正整数。")
    enqueued_at = normalize_worker_timestamp(
        str(record["enqueued_at"]),
        field="enqueued_at",
    )
    deadline_at = normalize_worker_timestamp(
        str(record["deadline_at"]),
        field="deadline_at",
    )
    if datetime.fromisoformat(deadline_at) <= datetime.fromisoformat(enqueued_at):
        raise ValueError("Capacity waiter deadline_at 必须晚于 enqueued_at。")
    reservation_id = str(record["reservation_id"]) if record["reservation_id"] is not None else None
    if reservation_id is not None:
        _validate_identifier(reservation_id, field="reservation_id")
    terminal_at = (
        normalize_worker_timestamp(str(record["terminal_at"]), field="terminal_at")
        if record["terminal_at"] is not None
        else None
    )
    reason_code = str(record["reason_code"]) if record["reason_code"] is not None else None
    if reason_code is not None:
        _validate_reason(reason_code)
    state = WorkerCapacityWaiterState(str(record["state"]))
    if state is WorkerCapacityWaiterState.WAITING:
        if reservation_id is not None or terminal_at is not None or reason_code is not None:
            raise ValueError("Waiting capacity waiter 不能包含终态字段。")
    elif terminal_at is None or reason_code is None:
        raise ValueError("终态 capacity waiter 缺少 terminal 字段。")
    elif datetime.fromisoformat(terminal_at) < datetime.fromisoformat(enqueued_at):
        raise ValueError("Capacity waiter terminal_at 早于 enqueued_at。")
    if state is WorkerCapacityWaiterState.CLAIMED:
        if reservation_id != capacity_waiter_reservation_id(str(record["queue_id"])):
            raise ValueError("Claimed capacity waiter reservation identity 不一致。")
        if reason_code != "capacity_claimed":
            raise ValueError("Claimed capacity waiter reason_code 不一致。")
        assert terminal_at is not None
        if datetime.fromisoformat(terminal_at) >= datetime.fromisoformat(deadline_at):
            raise ValueError("Claimed capacity waiter 已超过 deadline。")
    elif reservation_id is not None:
        raise ValueError("非 claimed capacity waiter 不能绑定 reservation。")
    return WorkerCapacityWaiter(
        queue_id=str(record["queue_id"]),
        worker_id=str(record["worker_id"]),
        instance_id=str(record["instance_id"]),
        epoch=epoch,
        job_id=str(record["job_id"]),
        workspace_sha256=str(record["workspace_sha256"]),
        state=state,
        enqueued_at=enqueued_at,
        deadline_at=deadline_at,
        reservation_id=reservation_id,
        terminal_at=terminal_at,
        reason_code=reason_code,
    )


def deserialize_worker_registration(record: Mapping[str, object]) -> WorkerRegistration:
    """Validate one durable registry record without trusting its index columns."""
    required = {
        "worker_id",
        "epoch",
        "instance_id",
        "contract_sha256",
        "contract_json",
        "state",
        "registered_at",
        "terminal_at",
        "reason_code",
    }
    if not isinstance(record, Mapping) or not required.issubset(record):
        raise ValueError("Worker registry 记录字段不完整。")
    contract = _deserialize_contract(str(record["contract_json"]))
    if (
        contract.worker_id != str(record["worker_id"])
        or contract.instance_id != str(record["instance_id"])
        or contract.epoch != int(record["epoch"])
        or contract.contract_sha256 != str(record["contract_sha256"])
    ):
        raise ValueError("Worker registry 索引列与合同不一致。")
    registered_at = normalize_worker_timestamp(
        str(record["registered_at"]), field="registered_at"
    )
    terminal_at = (
        normalize_worker_timestamp(str(record["terminal_at"]), field="terminal_at")
        if record["terminal_at"] is not None
        else None
    )
    reason_code = (
        str(record["reason_code"]) if record["reason_code"] is not None else None
    )
    if datetime.fromisoformat(contract.issued_at) > datetime.fromisoformat(registered_at):
        raise ValueError("Worker registry registered_at 早于合同 issued_at。")
    if terminal_at is not None and datetime.fromisoformat(terminal_at) < datetime.fromisoformat(
        registered_at
    ):
        raise ValueError("Worker registry terminal_at 早于 registered_at。")
    if reason_code is not None:
        _validate_reason(reason_code)
    return WorkerRegistration(
        contract=contract,
        state=WorkerRegistrationState(str(record["state"])),
        registered_at=registered_at,
        terminal_at=terminal_at,
        reason_code=reason_code,
    )


async def _select_active(
    db: aiosqlite.Connection,
    worker_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ? AND state = ?",
        (worker_id, WorkerRegistrationState.ACTIVE.value),
    )
    return await cursor.fetchone()


async def _select_registration(
    db: aiosqlite.Connection,
    worker_id: str,
    epoch: int,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ? AND epoch = ?",
        (worker_id, epoch),
    )
    return await cursor.fetchone()


async def _select_latest(
    db: aiosqlite.Connection,
    worker_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_registrations
        WHERE worker_id = ? ORDER BY epoch DESC LIMIT 1
        """,
        (worker_id,),
    )
    return await cursor.fetchone()


async def _select_capacity_reservation(
    db: aiosqlite.Connection, reservation_id: str
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_capacity_reservations WHERE reservation_id = ?",
        (reservation_id,),
    )
    return await cursor.fetchone()


async def _select_capacity_job(
    db: aiosqlite.Connection, worker_id: str, epoch: int, job_id: str
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_capacity_reservations
        WHERE worker_id = ? AND epoch = ? AND job_id = ?
        """,
        (worker_id, epoch, job_id),
    )
    return await cursor.fetchone()


async def _select_capacity_waiter(
    db: aiosqlite.Connection,
    queue_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_capacity_waiters WHERE queue_id = ?",
        (queue_id,),
    )
    return await cursor.fetchone()


async def _select_capacity_waiter_job(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    epoch: int,
    job_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_capacity_waiters
        WHERE worker_id = ? AND epoch = ? AND job_id = ?
        """,
        (worker_id, epoch, job_id),
    )
    return await cursor.fetchone()


async def _select_next_capacity_waiter(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_capacity_waiters
        WHERE worker_id = ? AND instance_id = ? AND epoch = ?
          AND state = 'waiting'
        ORDER BY enqueued_at ASC, queue_id ASC
        LIMIT 1
        """,
        (worker_id, instance_id, epoch),
    )
    return await cursor.fetchone()


async def _ensure_capacity_queue_policy(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    max_waiters: int,
    configured_at: str,
) -> None:
    cursor = await db.execute(
        """
        SELECT instance_id, max_waiters
        FROM worker_capacity_queue_policies
        WHERE worker_id = ? AND epoch = ?
        """,
        (worker_id, epoch),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.execute(
            """
            INSERT INTO worker_capacity_queue_policies (
                worker_id, instance_id, epoch, max_waiters, configured_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (worker_id, instance_id, epoch, max_waiters, configured_at),
        )
        return
    if str(row["instance_id"]) != instance_id:
        raise WorkerRegistryStoreError("Capacity queue policy 与 Worker instance 不一致。")
    if int(row["max_waiters"]) != max_waiters:
        raise WorkerRegistryConflictError("Capacity queue max_waiters 与 durable policy 不一致。")


async def _validate_capacity_waiter_links(
    db: aiosqlite.Connection,
    waiters: tuple[WorkerCapacityWaiter, ...],
) -> None:
    for waiter in waiters:
        if waiter.state is not WorkerCapacityWaiterState.CLAIMED:
            continue
        assert waiter.reservation_id is not None
        row = await _select_capacity_reservation(db, waiter.reservation_id)
        if row is None:
            raise ValueError("Claimed capacity waiter 缺少 reservation。")
        reservation = _capacity_reservation_from_row(row)
        if (
            reservation.worker_id != waiter.worker_id
            or reservation.instance_id != waiter.instance_id
            or reservation.epoch != waiter.epoch
            or reservation.job_id != waiter.job_id
        ):
            raise ValueError("Claimed capacity waiter 与 reservation 不一致。")


async def _count_capacity_waiters(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
) -> int:
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM worker_capacity_waiters
        WHERE worker_id = ? AND instance_id = ? AND epoch = ?
          AND state = 'waiting'
        """,
        (worker_id, instance_id, epoch),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def _count_active_capacity(
    db: aiosqlite.Connection, worker_id: str, instance_id: str, epoch: int
) -> int:
    cursor = await db.execute(
        """
        SELECT * FROM worker_capacity_reservations
        WHERE worker_id = ? AND epoch = ? AND state = 'active'
        """,
        (worker_id, epoch),
    )
    reservations = tuple(_capacity_reservation_from_row(row) for row in await cursor.fetchall())
    if any(item.instance_id != instance_id for item in reservations):
        raise ValueError("Capacity reservation 与 active Worker instance 不一致。")
    return len(reservations)


async def _expire_capacity_reservations(
    db: aiosqlite.Connection, *, worker_id: str, now: str
) -> None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_capacity_reservations
        WHERE worker_id = ? AND state = 'active'
        """,
        (worker_id,),
    )
    assessed = datetime.fromisoformat(now)
    reservations = tuple(_capacity_reservation_from_row(row) for row in await cursor.fetchall())
    expired_ids = [
        item.reservation_id
        for item in reservations
        if datetime.fromisoformat(item.expires_at) <= assessed
    ]
    if not expired_ids:
        return
    placeholders = ",".join("?" for _ in expired_ids)
    await db.execute(
        f"""
        UPDATE worker_capacity_reservations
        SET state = 'expired', terminal_at = ?, reason_code = 'ttl_expired'
        WHERE reservation_id IN ({placeholders}) AND state = 'active'
        """,  # noqa: S608 - placeholders are generated, values remain parameterized.
        (now, *expired_ids),
    )


async def _fence_capacity_reservations(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    epoch: int,
    fenced_at: str,
    reason_code: str,
) -> None:
    await db.execute(
        """
        UPDATE worker_capacity_reservations
        SET state = 'fenced', terminal_at = ?, reason_code = ?
        WHERE worker_id = ? AND epoch = ? AND state = 'active'
        """,
        (fenced_at, reason_code, worker_id, epoch),
    )


async def _expire_capacity_waiters(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    now: str,
) -> None:
    await db.execute(
        """
        UPDATE worker_capacity_waiters
        SET state = 'expired', terminal_at = ?, reason_code = 'deadline_expired'
        WHERE worker_id = ? AND state = 'waiting' AND deadline_at <= ?
        """,
        (now, worker_id, now),
    )


async def _fence_capacity_waiters(
    db: aiosqlite.Connection,
    *,
    worker_id: str,
    epoch: int,
    fenced_at: str,
    reason_code: str,
) -> None:
    await db.execute(
        """
        UPDATE worker_capacity_waiters
        SET state = 'fenced', terminal_at = ?, reason_code = ?
        WHERE worker_id = ? AND epoch = ? AND state = 'waiting'
        """,
        (fenced_at, reason_code, worker_id, epoch),
    )


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def capacity_waiter_reservation_id(queue_id: str) -> str:
    """Return the stable reservation identity owned by one queue waiter."""
    _validate_identifier(queue_id, field="queue_id")
    digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()
    return f"scheduler:{digest}"


def _validate_identifier(value: str, *, field: str) -> None:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(character not in allowed for character in value)
    ):
        raise ValueError(f"{field} 格式无效。")


def _validate_sha256(value: str, *, field: str) -> None:
    allowed = "0123456789abcdef"
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in allowed for character in value)
    ):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _validate_reason(value: str) -> None:
    _validate_identifier(value, field="reason_code")


def _registry_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkerRegistryStoreError("无法检查 Worker registry 路径。") from exc
    if not stat.S_ISREG(mode):
        raise WorkerRegistryStoreError("Worker registry 路径不是文件。")
    return True


_SCHEMA_V1_STATEMENTS = (
    """
CREATE TABLE worker_registrations (
    worker_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    instance_id TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    contract_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'revoked')),
    registered_at TEXT NOT NULL,
    terminal_at TEXT,
    reason_code TEXT,
    PRIMARY KEY (worker_id, epoch),
    UNIQUE (contract_sha256),
    CHECK (
        (state = 'active' AND terminal_at IS NULL AND reason_code IS NULL)
        OR (state != 'active' AND terminal_at IS NOT NULL AND reason_code IS NOT NULL)
    )
)
""",
    """
CREATE UNIQUE INDEX one_active_worker_incarnation
ON worker_registrations (worker_id) WHERE state = 'active'
""",
    """
CREATE INDEX worker_registration_history
ON worker_registrations (worker_id, epoch DESC)
""",
)


_SCHEMA_V2_STATEMENTS = (
    """
CREATE TABLE worker_capacity_reservations (
    reservation_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    job_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'released', 'expired', 'fenced')),
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    terminal_at TEXT,
    reason_code TEXT,
    UNIQUE (worker_id, epoch, job_id),
    FOREIGN KEY (worker_id, epoch) REFERENCES worker_registrations(worker_id, epoch),
    CHECK (expires_at > reserved_at),
    CHECK (
        (state = 'active' AND terminal_at IS NULL AND reason_code IS NULL)
        OR (state != 'active' AND terminal_at IS NOT NULL AND reason_code IS NOT NULL)
    )
)
""",
    """
CREATE INDEX active_worker_capacity
ON worker_capacity_reservations (worker_id, instance_id, epoch, expires_at)
WHERE state = 'active'
""",
)


_SCHEMA_V3_STATEMENTS = (
    """
CREATE TABLE worker_capacity_queue_policies (
    worker_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    max_waiters INTEGER NOT NULL CHECK (max_waiters >= 0 AND max_waiters <= 10000),
    configured_at TEXT NOT NULL,
    PRIMARY KEY (worker_id, epoch),
    FOREIGN KEY (worker_id, epoch) REFERENCES worker_registrations(worker_id, epoch)
)
""",
    """
CREATE TABLE worker_capacity_waiters (
    queue_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    job_id TEXT NOT NULL,
    workspace_sha256 TEXT NOT NULL CHECK (length(workspace_sha256) = 64),
    state TEXT NOT NULL CHECK (
        state IN ('waiting', 'claimed', 'cancelled', 'expired', 'fenced')
    ),
    enqueued_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    reservation_id TEXT UNIQUE,
    terminal_at TEXT,
    reason_code TEXT,
    UNIQUE (worker_id, epoch, job_id),
    FOREIGN KEY (worker_id, epoch)
        REFERENCES worker_capacity_queue_policies(worker_id, epoch),
    FOREIGN KEY (reservation_id)
        REFERENCES worker_capacity_reservations(reservation_id),
    CHECK (deadline_at > enqueued_at),
    CHECK (
        (state = 'waiting' AND reservation_id IS NULL
            AND terminal_at IS NULL AND reason_code IS NULL)
        OR (state = 'claimed' AND reservation_id IS NOT NULL
            AND terminal_at IS NOT NULL AND reason_code = 'capacity_claimed')
        OR (state IN ('cancelled', 'expired', 'fenced')
            AND reservation_id IS NULL
            AND terminal_at IS NOT NULL AND reason_code IS NOT NULL)
    )
)
""",
    """
CREATE INDEX waiting_worker_capacity_fifo
ON worker_capacity_waiters (
    worker_id, instance_id, epoch, enqueued_at, queue_id
)
WHERE state = 'waiting'
""",
)


__all__ = [
    "WORKER_REGISTRY_SCHEMA_VERSION",
    "WorkerCapacityClaim",
    "WorkerCapacityExhaustedError",
    "WorkerCapacityReservation",
    "WorkerCapacityReservationState",
    "WorkerCapacitySnapshot",
    "WorkerCapacityWaiter",
    "WorkerCapacityWaiterState",
    "WorkerRegistration",
    "WorkerRegistrationState",
    "WorkerRegistryConflictError",
    "WorkerRegistryStore",
    "WorkerRegistryStoreError",
    "capacity_waiter_reservation_id",
    "deserialize_worker_capacity_waiter",
    "deserialize_worker_capacity_reservation",
    "deserialize_worker_registration",
]
