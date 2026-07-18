"""Persistent worktree leases bound to non-executable Experiment Contracts."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.worktree.manager import WorktreeManager
from naumi_agent.worktree.models import WorktreeRecord, WorktreeStatus

EXPERIMENT_LEASE_POLICY = "evolution-experiment-lease-v1"
_SAFE_ID_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_LEASE_ID_RE = re.compile(r"^evl_[0-9a-f]{24}$")
_WORKTREE_NAME_RE = re.compile(r"^experiment-[0-9a-f]{16}$")
_ACTIVE_STATES = ("provisioning", "active", "released", "expired")


class ExperimentLeaseState(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    TOMBSTONED = "tombstoned"
    CLEANED = "cleaned"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ExperimentWorktreeLease(_StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    policy_version: str = EXPERIMENT_LEASE_POLICY
    lease_id: str
    contract_id: str
    manifest_sha256: str
    session_id: str
    mission_id: str
    task_id: str
    owner: str
    state: ExperimentLeaseState
    worktree_name: str
    worktree_path: str
    branch: str
    baseline_commit: str
    expires_at: str
    created_at: str
    updated_at: str
    terminal_reason: str = ""
    cleanup_attempts: int = Field(default=0, ge=0, le=100)
    worktree_ready: bool = False
    execution_ready: bool = False

    @field_validator(
        "lease_id",
        "contract_id",
        "session_id",
        "mission_id",
        "task_id",
        "owner",
    )
    @classmethod
    def _safe_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _SAFE_ID_RE.fullmatch(normalized):
            raise ValueError("Experiment Lease ID/binding 格式无效。")
        return normalized

    @field_validator("lease_id")
    @classmethod
    def _valid_lease_id(cls, value: str) -> str:
        if not _LEASE_ID_RE.fullmatch(value):
            raise ValueError("lease_id 格式无效。")
        return value

    @field_validator("manifest_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("Lease manifest_sha256 格式无效。")
        return normalized

    @field_validator("worktree_name")
    @classmethod
    def _valid_worktree_name(cls, value: str) -> str:
        if not _WORKTREE_NAME_RE.fullmatch(value):
            raise ValueError("Experiment worktree_name 格式无效。")
        return value

    @field_validator("worktree_path")
    @classmethod
    def _absolute_worktree_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Experiment worktree_path 必须是安全绝对路径。")
        return str(path.resolve())

    @field_validator("expires_at", "created_at", "updated_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        _parse_time(value)
        return value

    @model_validator(mode="after")
    def _state_matches_readiness(self) -> ExperimentWorktreeLease:
        if self.policy_version != EXPERIMENT_LEASE_POLICY:
            raise ValueError("Experiment Lease policy version 不兼容。")
        if self.lease_id != _lease_id(self.contract_id, self.manifest_sha256):
            raise ValueError("lease_id 与 Contract binding 不一致。")
        if self.worktree_name != _worktree_name(self.contract_id):
            raise ValueError("worktree_name 与 Contract binding 不一致。")
        expected_ready = self.state is ExperimentLeaseState.ACTIVE
        if self.worktree_ready is not expected_ready:
            raise ValueError("worktree_ready 与 Lease state 不一致。")
        if self.execution_ready:
            raise ValueError("EVO-02.2 Lease 不能授予执行权限。")
        if _parse_time(self.expires_at) <= _parse_time(self.created_at):
            raise ValueError("Lease expires_at 必须晚于 created_at。")
        return self


class ExperimentLeaseConflictError(RuntimeError):
    """Raised when an existing lease is bound to incompatible authority."""


class EvolutionExperimentLeaseStore:
    """SQLite CAS store for one durable lease per Experiment Contract."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)

    async def reserve(
        self,
        contract: EvolutionExperimentContract,
        *,
        owner: str,
        worktree_path: str,
        branch: str,
        expires_at: str,
        now: str,
    ) -> tuple[ExperimentWorktreeLease, bool]:
        clean_owner = _binding(owner, "owner")
        lease = ExperimentWorktreeLease(
            lease_id=_lease_id(contract.contract_id, contract.manifest_sha256),
            contract_id=contract.contract_id,
            manifest_sha256=contract.manifest_sha256,
            session_id=contract.source.session_id,
            mission_id=contract.source.mission_id,
            task_id=contract.source.task_id,
            owner=clean_owner,
            state=ExperimentLeaseState.PROVISIONING,
            worktree_name=_worktree_name(contract.contract_id),
            worktree_path=worktree_path,
            branch=branch,
            baseline_commit=contract.baseline.commit,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._database_path) as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM evolution_experiment_leases WHERE contract_id = ?",
                (contract.contract_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                await db.commit()
                existing = _lease_from_row(dict(row))
                _require_same_binding(existing, lease)
                return existing, False
            try:
                await db.execute(
                    """INSERT INTO evolution_experiment_leases
                       (lease_id, contract_id, manifest_sha256, session_id, mission_id,
                        task_id, owner, state, worktree_name, worktree_path, branch,
                        baseline_commit, expires_at, created_at, updated_at,
                        terminal_reason, cleanup_attempts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    _lease_values(lease),
                )
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ExperimentLeaseConflictError(
                    "Experiment worktree 已被其他 Contract 占用。"
                ) from exc
            await db.commit()
        return lease, True

    async def get(self, contract_id: str) -> ExperimentWorktreeLease | None:
        async with aiosqlite.connect(self._database_path) as db:
            await self._ensure_table(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM evolution_experiment_leases WHERE contract_id = ?",
                (_binding(contract_id, "contract"),),
            )
            row = await cursor.fetchone()
        return _lease_from_row(dict(row)) if row else None

    async def transition(
        self,
        contract_id: str,
        *,
        expected: Iterable[ExperimentLeaseState],
        state: ExperimentLeaseState,
        now: str,
        terminal_reason: str = "",
        increment_cleanup_attempts: bool = False,
    ) -> tuple[ExperimentWorktreeLease | None, bool]:
        expected_values = tuple(item.value for item in expected)
        if not expected_values:
            raise ValueError("Lease transition expected states 不能为空。")
        reason = _bounded_reason(terminal_reason)
        placeholders = ",".join("?" for _ in expected_values)
        increment = "cleanup_attempts + 1" if increment_cleanup_attempts else "cleanup_attempts"
        async with aiosqlite.connect(self._database_path) as db:
            await self._ensure_table(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"""UPDATE evolution_experiment_leases
                    SET state = ?, updated_at = ?, terminal_reason = ?,
                        cleanup_attempts = {increment}
                    WHERE contract_id = ? AND state IN ({placeholders})""",
                (state.value, now, reason, _binding(contract_id, "contract"), *expected_values),
            )
            applied = cursor.rowcount == 1
            db.row_factory = aiosqlite.Row
            current = await db.execute(
                "SELECT * FROM evolution_experiment_leases WHERE contract_id = ?",
                (contract_id,),
            )
            row = await current.fetchone()
            await db.commit()
        return (_lease_from_row(dict(row)) if row else None), applied

    async def list_reconcilable(self, *, limit: int = 200) -> list[ExperimentWorktreeLease]:
        bounded = min(500, max(1, int(limit)))
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        async with aiosqlite.connect(self._database_path) as db:
            await self._ensure_table(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM evolution_experiment_leases
                    WHERE state IN ({placeholders})
                    ORDER BY updated_at, lease_id LIMIT ?""",
                (*_ACTIVE_STATES, bounded),
            )
            rows = await cursor.fetchall()
        return [_lease_from_row(dict(row)) for row in rows]

    async def _ensure_table(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_experiment_leases (
                lease_id TEXT PRIMARY KEY,
                contract_id TEXT NOT NULL UNIQUE,
                manifest_sha256 TEXT NOT NULL,
                session_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                state TEXT NOT NULL,
                worktree_name TEXT NOT NULL UNIQUE,
                worktree_path TEXT NOT NULL UNIQUE,
                branch TEXT NOT NULL UNIQUE,
                baseline_commit TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                terminal_reason TEXT NOT NULL DEFAULT '',
                cleanup_attempts INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.commit()


class EvolutionExperimentLeaseManager:
    """Provision, recover, expire, and safely clean Contract-bound worktrees."""

    def __init__(
        self,
        *,
        store: EvolutionExperimentLeaseStore,
        worktree_manager: WorktreeManager,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._worktree_manager = worktree_manager
        self._clock = clock or (lambda: datetime.now(UTC))
        self._contract_locks: dict[str, asyncio.Lock] = {}

    async def acquire(
        self,
        contract: EvolutionExperimentContract,
        *,
        owner: str,
        duration_seconds: int | None = None,
    ) -> ExperimentWorktreeLease:
        if not isinstance(contract, EvolutionExperimentContract):
            raise TypeError("Lease 只能绑定 EvolutionExperimentContract。")
        lock = self._contract_locks.setdefault(contract.contract_id, asyncio.Lock())
        async with lock:
            return await self._acquire_locked(
                contract,
                owner=owner,
                duration_seconds=duration_seconds,
            )

    async def _acquire_locked(
        self,
        contract: EvolutionExperimentContract,
        *,
        owner: str,
        duration_seconds: int | None,
    ) -> ExperimentWorktreeLease:
        if contract.execution_ready or not (
            contract.requires_worktree_lease
            and contract.requires_source_snapshot
            and contract.requires_static_guard
        ):
            raise ValueError("Experiment Contract 安全前置不完整。")
        duration = (
            contract.budget.max_duration_seconds + 300
            if duration_seconds is None
            else int(duration_seconds)
        )
        if duration < 60 or duration > contract.budget.max_duration_seconds + 300:
            raise ValueError("Lease duration 超出 Contract 执行预算与清理宽限。")
        now = _aware_utc(self._clock())
        expires_at = _iso(now + timedelta(seconds=duration))
        name = _worktree_name(contract.contract_id)
        path = (self._worktree_manager.storage_dir / name).resolve()
        if not path.is_relative_to(self._worktree_manager.storage_dir.resolve()):
            raise ValueError("Experiment worktree 路径逃逸存储目录。")
        branch = f"naumi/worktree-{name}"
        lease, created = await self._store.reserve(
            contract,
            owner=owner,
            worktree_path=str(path),
            branch=branch,
            expires_at=expires_at,
            now=_iso(now),
        )
        if lease.state is ExperimentLeaseState.ACTIVE:
            verified = await self._verify_active(lease, now=now)
            if verified.state is not ExperimentLeaseState.ACTIVE:
                raise ExperimentLeaseConflictError(
                    f"Experiment Lease 已转为 {verified.state.value}，需要人工复核。"
                )
            return verified
        if lease.state is not ExperimentLeaseState.PROVISIONING:
            raise ExperimentLeaseConflictError(
                f"Experiment Lease 已处于 {lease.state.value}，不能重新获取。"
            )
        if _parse_time(lease.expires_at) <= now:
            await self._expire_and_cleanup(lease, now=now)
            raise ExperimentLeaseConflictError("Experiment Lease 在完成 provision 前已过期。")
        if not created:
            recovered = await self._recover_existing(lease, now=now)
            if recovered is not None:
                if recovered.state is ExperimentLeaseState.ACTIVE:
                    return recovered
                raise ExperimentLeaseConflictError(
                    f"Experiment Lease 已转为 {recovered.state.value}，需要人工复核。"
                )
            raise ExperimentLeaseConflictError(
                "Experiment worktree 正由另一个进程创建，请稍后重试。"
            )
        provisioned = await self._recover_or_provision(lease, contract=contract, now=now)
        if provisioned.state is not ExperimentLeaseState.ACTIVE:
            raise ExperimentLeaseConflictError(
                f"Experiment worktree provision 进入 {provisioned.state.value}。"
            )
        return provisioned

    async def release(
        self,
        contract_id: str,
        *,
        owner: str,
    ) -> ExperimentWorktreeLease:
        clean_contract_id = _binding(contract_id, "contract")
        lock = self._contract_locks.setdefault(clean_contract_id, asyncio.Lock())
        async with lock:
            return await self._release_locked(clean_contract_id, owner=owner)

    async def _release_locked(
        self,
        contract_id: str,
        *,
        owner: str,
    ) -> ExperimentWorktreeLease:
        lease = await self._store.get(contract_id)
        if lease is None:
            raise ValueError("Experiment Lease 不存在。")
        if lease.owner != _binding(owner, "owner"):
            raise ExperimentLeaseConflictError("只有 Lease owner 可以释放 Experiment worktree。")
        now = _aware_utc(self._clock())
        lease, _ = await self._store.transition(
            contract_id,
            expected=(ExperimentLeaseState.ACTIVE,),
            state=ExperimentLeaseState.RELEASED,
            now=_iso(now),
            terminal_reason="owner_released",
        )
        if lease is None:
            raise ValueError("Experiment Lease 不存在。")
        if lease.state not in {
            ExperimentLeaseState.RELEASED,
            ExperimentLeaseState.CLEANED,
            ExperimentLeaseState.TOMBSTONED,
        }:
            raise ExperimentLeaseConflictError(
                f"Experiment Lease 已处于 {lease.state.value}，不能释放。"
            )
        if lease.state is ExperimentLeaseState.RELEASED:
            return await self._cleanup(lease, now=now)
        return lease

    async def reconcile(self, *, limit: int = 200) -> list[ExperimentWorktreeLease]:
        now = _aware_utc(self._clock())
        outcomes: list[ExperimentWorktreeLease] = []
        for lease in await self._store.list_reconcilable(limit=limit):
            lock = self._contract_locks.setdefault(lease.contract_id, asyncio.Lock())
            async with lock:
                outcome = await self._reconcile_one(lease, now=now)
                if outcome is not None:
                    outcomes.append(outcome)
        return outcomes

    async def _reconcile_one(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
    ) -> ExperimentWorktreeLease | None:
        if lease.state is ExperimentLeaseState.PROVISIONING:
            if _parse_time(lease.expires_at) <= now:
                return await self._expire_and_cleanup(lease, now=now)
            return await self._recover_existing(lease, now=now)
        if lease.state is ExperimentLeaseState.ACTIVE:
            if _parse_time(lease.expires_at) <= now:
                return await self._expire_and_cleanup(lease, now=now)
            return await self._verify_active(lease, now=now)
        return await self._cleanup(lease, now=now)

    async def _recover_or_provision(
        self,
        lease: ExperimentWorktreeLease,
        *,
        contract: EvolutionExperimentContract,
        now: datetime,
    ) -> ExperimentWorktreeLease:
        recovered = await self._recover_existing(lease, now=now)
        if recovered is not None:
            return recovered
        path = Path(lease.worktree_path)
        if path.exists():
            return await self._tombstone(
                lease,
                now=now,
                reason="unmanaged_path_exists",
            )
        await self._worktree_manager.create_from_ref(
            lease.worktree_name,
            base_ref=contract.baseline.commit,
            task_id=contract.source.task_id,
            metadata={
                "experiment_contract_id": contract.contract_id,
                "experiment_manifest_sha256": contract.manifest_sha256,
                "experiment_lease_id": lease.lease_id,
            },
        )
        recovered = await self._recover_existing(lease, now=now)
        if recovered is not None:
            return recovered
        return await self._tombstone(lease, now=now, reason="provision_failed")

    async def _recover_existing(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
    ) -> ExperimentWorktreeLease | None:
        try:
            record = await self._worktree_manager.status(lease.worktree_name)
        except (KeyError, ValueError):
            return None
        if isinstance(record, list) or not _record_matches_lease(record, lease):
            return await self._tombstone(lease, now=now, reason="binding_mismatch")
        active, _ = await self._store.transition(
            lease.contract_id,
            expected=(ExperimentLeaseState.PROVISIONING,),
            state=ExperimentLeaseState.ACTIVE,
            now=_iso(now),
        )
        current = active or await self._store.get(lease.contract_id)
        if current is None:
            raise RuntimeError("Experiment Lease 激活后无法读取。")
        if current.state is ExperimentLeaseState.ACTIVE:
            return await self._verify_active(current, now=now)
        if current.state in {
            ExperimentLeaseState.RELEASED,
            ExperimentLeaseState.EXPIRED,
        }:
            return await self._cleanup(current, now=now)
        if current.state is ExperimentLeaseState.CLEANED:
            if record.removable:
                await self._worktree_manager.remove(lease.worktree_name)
                try:
                    await self._worktree_manager.status(lease.worktree_name)
                except KeyError:
                    return current
            return await self._tombstone(
                current,
                now=now,
                reason="late_worktree_after_cleanup",
            )
        return current

    async def _verify_active(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
    ) -> ExperimentWorktreeLease:
        try:
            record = await self._worktree_manager.status(lease.worktree_name)
        except (KeyError, ValueError):
            return await self._tombstone(
                lease,
                now=now,
                reason="active_worktree_missing",
            )
        if isinstance(record, list):
            return await self._tombstone(
                lease,
                now=now,
                reason="binding_mismatch",
            )
        if record.status is WorktreeStatus.MISSING:
            return await self._tombstone(
                lease,
                now=now,
                reason="active_worktree_missing",
            )
        if not _record_matches_lease(record, lease):
            return await self._tombstone(
                lease,
                now=now,
                reason="binding_mismatch",
            )
        return lease

    async def _expire_and_cleanup(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
    ) -> ExperimentWorktreeLease:
        expired, _ = await self._store.transition(
            lease.contract_id,
            expected=(ExperimentLeaseState.ACTIVE, ExperimentLeaseState.PROVISIONING),
            state=ExperimentLeaseState.EXPIRED,
            now=_iso(now),
            terminal_reason="lease_expired",
        )
        current = expired or await self._store.get(lease.contract_id)
        if current is None:
            raise RuntimeError("Expired Experiment Lease 无法读取。")
        if current.state in {
            ExperimentLeaseState.EXPIRED,
            ExperimentLeaseState.RELEASED,
        }:
            return await self._cleanup(current, now=now)
        return current

    async def _cleanup(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
    ) -> ExperimentWorktreeLease:
        try:
            record = await self._worktree_manager.status(lease.worktree_name)
        except KeyError:
            if Path(lease.worktree_path).exists():
                return await self._tombstone(
                    lease,
                    now=now,
                    reason="unmanaged_path_exists",
                )
            return await self._mark_cleaned(lease, now=now, reason="already_absent")
        except ValueError:
            return await self._tombstone(lease, now=now, reason="invalid_worktree_record")
        if isinstance(record, list) or not _record_matches_lease(record, lease):
            return await self._tombstone(lease, now=now, reason="binding_mismatch")
        if record.status is WorktreeStatus.MISSING:
            await self._worktree_manager.remove(lease.worktree_name)
            return await self._mark_cleaned(lease, now=now, reason="missing_record_cleaned")
        if not record.removable:
            await self._worktree_manager.keep(
                lease.worktree_name,
                reason="Experiment Lease 到期但存在未审查变更，禁止自动删除。",
            )
            return await self._tombstone(lease, now=now, reason="dirty_or_ahead")
        await self._worktree_manager.remove(lease.worktree_name)
        try:
            await self._worktree_manager.status(lease.worktree_name)
        except KeyError:
            if not Path(lease.worktree_path).exists():
                return await self._mark_cleaned(lease, now=now, reason="clean_removed")
        return await self._tombstone(lease, now=now, reason="cleanup_failed")

    async def _mark_cleaned(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
        reason: str,
    ) -> ExperimentWorktreeLease:
        updated, _ = await self._store.transition(
            lease.contract_id,
            expected=(ExperimentLeaseState.RELEASED, ExperimentLeaseState.EXPIRED),
            state=ExperimentLeaseState.CLEANED,
            now=_iso(now),
            terminal_reason=reason,
            increment_cleanup_attempts=True,
        )
        return updated or (await self._require_lease(lease.contract_id))

    async def _tombstone(
        self,
        lease: ExperimentWorktreeLease,
        *,
        now: datetime,
        reason: str,
    ) -> ExperimentWorktreeLease:
        updated, _ = await self._store.transition(
            lease.contract_id,
            expected=(
                ExperimentLeaseState.PROVISIONING,
                ExperimentLeaseState.ACTIVE,
                ExperimentLeaseState.RELEASED,
                ExperimentLeaseState.EXPIRED,
                ExperimentLeaseState.CLEANED,
            ),
            state=ExperimentLeaseState.TOMBSTONED,
            now=_iso(now),
            terminal_reason=reason,
            increment_cleanup_attempts=True,
        )
        return updated or (await self._require_lease(lease.contract_id))

    async def _require_lease(self, contract_id: str) -> ExperimentWorktreeLease:
        current = await self._store.get(contract_id)
        if current is None:
            raise RuntimeError("Experiment Lease transition 后无法读取。")
        return current


def _record_matches_lease(
    record: WorktreeRecord,
    lease: ExperimentWorktreeLease,
) -> bool:
    return (
        record.name == lease.worktree_name
        and Path(record.path).resolve() == Path(lease.worktree_path).resolve()
        and record.branch == lease.branch
        and record.base_ref.lower() == lease.baseline_commit.lower()
        and record.task_id == lease.task_id
        and record.metadata.get("experiment_contract_id") == lease.contract_id
        and record.metadata.get("experiment_manifest_sha256") == lease.manifest_sha256
        and record.metadata.get("experiment_lease_id") == lease.lease_id
    )


def _require_same_binding(
    existing: ExperimentWorktreeLease,
    requested: ExperimentWorktreeLease,
) -> None:
    fields = (
        "lease_id",
        "manifest_sha256",
        "session_id",
        "mission_id",
        "task_id",
        "owner",
        "worktree_name",
        "worktree_path",
        "branch",
        "baseline_commit",
    )
    if any(getattr(existing, field) != getattr(requested, field) for field in fields):
        raise ExperimentLeaseConflictError(
            "现有 Experiment Lease 与请求的 Contract/owner binding 不一致。"
        )


def _lease_from_row(row: dict[str, object]) -> ExperimentWorktreeLease:
    state = ExperimentLeaseState(str(row["state"]))
    return ExperimentWorktreeLease(
        schema_version=1,
        lease_id=str(row["lease_id"]),
        contract_id=str(row["contract_id"]),
        manifest_sha256=str(row["manifest_sha256"]),
        session_id=str(row["session_id"]),
        mission_id=str(row["mission_id"]),
        task_id=str(row["task_id"]),
        owner=str(row["owner"]),
        state=state,
        worktree_name=str(row["worktree_name"]),
        worktree_path=str(row["worktree_path"]),
        branch=str(row["branch"]),
        baseline_commit=str(row["baseline_commit"]),
        expires_at=str(row["expires_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        terminal_reason=str(row["terminal_reason"] or ""),
        cleanup_attempts=int(row["cleanup_attempts"]),
        worktree_ready=state is ExperimentLeaseState.ACTIVE,
        execution_ready=False,
    )


def _lease_values(lease: ExperimentWorktreeLease) -> tuple[object, ...]:
    return (
        lease.lease_id,
        lease.contract_id,
        lease.manifest_sha256,
        lease.session_id,
        lease.mission_id,
        lease.task_id,
        lease.owner,
        lease.state.value,
        lease.worktree_name,
        lease.worktree_path,
        lease.branch,
        lease.baseline_commit,
        lease.expires_at,
        lease.created_at,
        lease.updated_at,
        lease.terminal_reason,
        lease.cleanup_attempts,
    )


def _lease_id(contract_id: str, manifest_sha256: str) -> str:
    digest = hashlib.sha256(f"{contract_id}:{manifest_sha256}".encode()).hexdigest()
    return f"evl_{digest[:24]}"


def _worktree_name(contract_id: str) -> str:
    suffix = contract_id.removeprefix("evx_")
    if not re.fullmatch(r"[0-9a-f]{24}", suffix):
        raise ValueError("Experiment contract_id 格式无效。")
    return f"experiment-{suffix[:16]}"


def _binding(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"Experiment Lease {label} 格式无效。")
    return normalized


def _bounded_reason(value: str) -> str:
    normalized = str(value).strip()
    if len(normalized) > 128 or any(char in normalized for char in ("\x00", "\r", "\n")):
        raise ValueError("Experiment Lease terminal reason 格式无效。")
    return normalized


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Experiment Lease 时间必须是 ISO-8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Experiment Lease 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Experiment Lease clock 必须包含时区。")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="seconds")


__all__ = [
    "EvolutionExperimentLeaseManager",
    "EvolutionExperimentLeaseStore",
    "ExperimentLeaseConflictError",
    "ExperimentLeaseState",
    "ExperimentWorktreeLease",
]
