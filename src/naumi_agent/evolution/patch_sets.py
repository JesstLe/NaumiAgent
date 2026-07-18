"""Durable, ordered write-set contract for multi-file evolution patches."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.evolution.static_guards import EvolutionStaticGuardReceipt

PATCH_SET_POLICY = "evolution-patch-set-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 256 * 1024
_MIN_FILES = 2
_MAX_FILES = 16


class PatchSetState(StrEnum):
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLING_BACK = "rolling_back"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_FAILED = "recovery_failed"


class PatchSetFilePhase(StrEnum):
    PREPARED = "prepared"
    REPLACED = "replaced"
    ROLLED_BACK = "rolled_back"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPatchSetFileFact(_StrictModel):
    index: int = Field(ge=0, lt=_MAX_FILES)
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    backup_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    backup_retained: bool
    file_mode: int = Field(ge=0, le=0o777)
    phase: PatchSetFilePhase
    fact_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(ord(char) < 32 for char in normalized)
        ):
            raise ValueError("Patch Set path 必须是安全相对路径。")
        return normalized

    @model_validator(mode="after")
    def _fact_is_consistent(self) -> Self:
        if self.operation == "modify":
            if self.before_sha256 is None:
                raise ValueError("modify Patch Set fact 缺少 before digest。")
        elif self.before_sha256 is not None:
            raise ValueError("create Patch Set fact 不得包含 before digest。")
        if self.backup_retained != (self.backup_sha256 is not None):
            raise ValueError("Patch Set backup presence 与摘要不一致。")
        if self.operation == "create" and self.backup_retained:
            raise ValueError("create Patch Set fact 不得保留 backup。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"fact_sha256"})
        )
        if not hmac.compare_digest(self.fact_sha256, expected):
            raise ValueError("fact_sha256 与 Patch Set file fact 不一致。")
        return self


class EvolutionPatchSetTransaction(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-patch-set-v1"] = PATCH_SET_POLICY
    transaction_id: str = Field(pattern=r"^evset_[0-9a-f]{24}$")
    transaction_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    guard_receipt_sha256: str = Field(pattern=_SHA256_RE)
    worktree_name: str = Field(pattern=r"^experiment-[0-9a-f]{16}$")
    worktree_path: str = Field(min_length=1, max_length=4_096)
    files: tuple[EvolutionPatchSetFileFact, ...] = Field(
        min_length=_MIN_FILES,
        max_length=_MAX_FILES,
    )
    applied_count: int = Field(ge=0, le=_MAX_FILES)
    rollback_cursor: int = Field(ge=-1, lt=_MAX_FILES)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    state: PatchSetState
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    failure_code: str = Field(default="", max_length=128)
    created_at: str
    updated_at: str
    write_authorized: Literal[False] = False
    execution_ready: Literal[False] = False

    @field_validator("worktree_path")
    @classmethod
    def _absolute_worktree(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("Patch Set worktree_path 必须是安全绝对路径。")
        return str(path.resolve())

    @model_validator(mode="after")
    def _state_and_digest_match(self) -> Self:
        count = len(self.files)
        if tuple(item.index for item in self.files) != tuple(range(count)):
            raise ValueError("Patch Set file index 必须连续且有序。")
        paths = tuple(item.path for item in self.files)
        if len(paths) != len(set(paths)) or paths != tuple(sorted(paths)):
            raise ValueError("Patch Set files 必须按 Guard 路径确定性排序且不得重复。")
        if self.attempt > self.max_attempts or self.applied_count > count:
            raise ValueError("Patch Set progress 超过计划边界。")
        phases = tuple(item.phase for item in self.files)
        retained = any(item.backup_retained for item in self.files)
        if self.state is PatchSetState.PREPARED:
            if self.applied_count or self.rollback_cursor != -1:
                raise ValueError("prepared Patch Set progress 不一致。")
            if any(phase is not PatchSetFilePhase.PREPARED for phase in phases):
                raise ValueError("prepared Patch Set file phase 不一致。")
        elif self.state is PatchSetState.APPLYING:
            if not 0 < self.applied_count < count or self.rollback_cursor != -1:
                raise ValueError("applying Patch Set progress 不一致。")
            expected = (PatchSetFilePhase.REPLACED,) * self.applied_count + (
                PatchSetFilePhase.PREPARED,
            ) * (count - self.applied_count)
            if phases != expected:
                raise ValueError("applying Patch Set file phase 不连续。")
        elif self.state in {PatchSetState.APPLIED, PatchSetState.COMMITTED}:
            if self.applied_count != count or self.rollback_cursor != -1:
                raise ValueError("applied Patch Set progress 不一致。")
            if any(phase is not PatchSetFilePhase.REPLACED for phase in phases):
                raise ValueError("applied Patch Set file phase 不一致。")
        elif self.state is PatchSetState.ROLLING_BACK:
            if not -1 <= self.rollback_cursor < count:
                raise ValueError("rolling_back Patch Set cursor 不一致。")
            for index, phase in enumerate(phases):
                if index > self.rollback_cursor:
                    if phase is not PatchSetFilePhase.ROLLED_BACK:
                        raise ValueError("Patch Set rollback 必须严格逆序推进。")
                elif phase is PatchSetFilePhase.ROLLED_BACK:
                    raise ValueError("Patch Set rollback cursor 前不得提前完成。")
        elif self.state is PatchSetState.ROLLED_BACK:
            if self.rollback_cursor != -1:
                raise ValueError("rolled_back Patch Set cursor 不一致。")
            if any(phase is not PatchSetFilePhase.ROLLED_BACK for phase in phases):
                raise ValueError("rolled_back Patch Set file phase 不完整。")
        if self.state in {
            PatchSetState.PREPARED,
            PatchSetState.APPLYING,
            PatchSetState.APPLIED,
            PatchSetState.ROLLING_BACK,
        }:
            if self.receipt_sha256 is not None or self.failure_code:
                raise ValueError("活动 Patch Set 不得提前写入终态字段。")
            if any(
                item.operation == "modify" and not item.backup_retained
                for item in self.files
            ):
                raise ValueError("活动 modify Patch Set 必须保留全部 backup。")
        if self.state is PatchSetState.COMMITTED:
            if self.receipt_sha256 is None or retained or self.failure_code:
                raise ValueError("committed Patch Set 终态字段不一致。")
        if self.state is PatchSetState.ROLLED_BACK:
            if self.receipt_sha256 is not None or retained:
                raise ValueError("rolled_back Patch Set 不得保留 receipt/backup。")
        if self.state is PatchSetState.RECOVERY_FAILED:
            if not self.failure_code or self.receipt_sha256 is not None:
                raise ValueError("recovery_failed Patch Set 必须保留 failure code。")
        _parse_time(self.created_at)
        _parse_time(self.updated_at)
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"transaction_sha256"})
        )
        if not hmac.compare_digest(self.transaction_sha256, expected):
            raise ValueError("transaction_sha256 与 Patch Set 不一致。")
        return self


class PatchSetScanFailure(_StrictModel):
    transaction_id: str = Field(pattern=r"^evset_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    failure_code: Literal["patch_set_corrupt"] = "patch_set_corrupt"


class EvolutionPatchSetStore:
    """SQLite CAS store for an entire ordered write-set; it never writes files."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = str(database_path)
        self._clock = clock or (lambda: datetime.now(UTC))

    def prepare(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        guard_receipt: EvolutionStaticGuardReceipt,
        before_contents: Mapping[str, bytes | None],
        file_modes: Mapping[str, int],
    ) -> EvolutionPatchSetTransaction:
        changes = _require_prepare_binding(
            contract,
            lease,
            source_snapshot,
            mutation_plan,
            guard_receipt,
            before_contents,
            file_modes,
        )
        core = {
            "contract_id": contract.contract_id,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "mutation_plan_id": mutation_plan.plan_id,
            "worktree_name": lease.worktree_name,
            "worktree_path": lease.worktree_path,
            "paths": [item.path for item in changes],
            "operations": [item.operation for item in changes],
            "before_sha256": [item.before_sha256 for item in changes],
        }
        transaction_id = f"evset_{_sha256_payload(core)[:24]}"
        now = _iso(self._clock())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM evolution_patch_sets WHERE lease_id = ?",
                (lease.lease_id,),
            ).fetchone()
            if row is not None:
                current = self._from_row(db, row)
                _require_same_core(current, transaction_id, core)
                if current.state is not PatchSetState.ROLLED_BACK:
                    _require_same_attempt(current, guard_receipt)
                    db.commit()
                    return current
                if current.attempt >= current.max_attempts:
                    db.commit()
                    return current
                transaction = _build_transaction(
                    **{key: value for key, value in core.items() if key not in {
                        "paths", "operations", "before_sha256"
                    }},
                    transaction_id=transaction_id,
                    guard_id=guard_receipt.guard_id,
                    guard_receipt_sha256=guard_receipt.receipt_sha256,
                    files=_build_file_facts(
                        changes, before_contents, file_modes, retain_backups=True
                    ),
                    applied_count=0,
                    rollback_cursor=-1,
                    attempt=current.attempt + 1,
                    max_attempts=mutation_plan.max_attempts,
                    state=PatchSetState.PREPARED,
                    receipt_sha256=None,
                    failure_code="",
                    created_at=current.created_at,
                    updated_at=now,
                    write_authorized=False,
                    execution_ready=False,
                )
                self._replace_row(db, transaction, before_contents, receipt_json=None)
                db.commit()
                return transaction
            transaction = _build_transaction(
                **{key: value for key, value in core.items() if key not in {
                    "paths", "operations", "before_sha256"
                }},
                transaction_id=transaction_id,
                guard_id=guard_receipt.guard_id,
                guard_receipt_sha256=guard_receipt.receipt_sha256,
                files=_build_file_facts(
                    changes, before_contents, file_modes, retain_backups=True
                ),
                applied_count=0,
                rollback_cursor=-1,
                attempt=1,
                max_attempts=mutation_plan.max_attempts,
                state=PatchSetState.PREPARED,
                receipt_sha256=None,
                failure_code="",
                created_at=now,
                updated_at=now,
                write_authorized=False,
                execution_ready=False,
            )
            db.execute(
                """INSERT INTO evolution_patch_sets
                   (transaction_id, transaction_json, lease_id, state, updated_at,
                    receipt_json) VALUES (?, ?, ?, ?, ?, NULL)""",
                (
                    transaction.transaction_id,
                    transaction.model_dump_json(),
                    transaction.lease_id,
                    transaction.state.value,
                    transaction.updated_at,
                ),
            )
            self._insert_backups(db, transaction, before_contents)
            db.commit()
        return transaction

    def get_by_lease(self, lease_id: str) -> EvolutionPatchSetTransaction | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_sets WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            return self._from_row(db, row) if row is not None else None

    def list_recoverable(
        self, *, limit: int = 100
    ) -> tuple[EvolutionPatchSetTransaction, ...]:
        transactions, failures = self.scan_recoverable(limit=limit)
        if failures:
            raise ValueError("检测到损坏的 Patch Set。")
        return transactions

    def scan_recoverable(
        self,
        *,
        limit: int = 100,
    ) -> tuple[
        tuple[EvolutionPatchSetTransaction, ...],
        tuple[PatchSetScanFailure, ...],
    ]:
        if limit < 1 or limit > 1_000:
            raise ValueError("Patch Set recovery limit 必须在 1..1000。")
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM evolution_patch_sets
                   WHERE state IN ('prepared', 'applying', 'applied', 'rolling_back')
                   ORDER BY updated_at, transaction_id LIMIT ?""",
                (limit,),
            ).fetchall()
            transactions: list[EvolutionPatchSetTransaction] = []
            failures: list[PatchSetScanFailure] = []
            for row in rows:
                try:
                    transactions.append(self._from_row(db, row))
                except (TypeError, ValueError):
                    failures.append(
                        PatchSetScanFailure(
                            transaction_id=_safe_row_id(
                                row["transaction_id"],
                                prefix="evset",
                                pattern=r"^evset_[0-9a-f]{24}$",
                            ),
                            lease_id=_safe_row_id(
                                row["lease_id"],
                                prefix="evl",
                                pattern=r"^evl_[0-9a-f]{24}$",
                            ),
                        )
                    )
        return tuple(transactions), tuple(failures)

    def load_backups(self, transaction_id: str) -> tuple[bytes | None, ...]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_sets WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(transaction_id)
            transaction = self._from_row(db, row)
            backups = self._backup_values(db, transaction.transaction_id)
            return _validate_backups(transaction, backups)

    def load_receipt_json(self, transaction_id: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_sets WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(transaction_id)
            transaction = self._from_row(db, row)
            receipt_json = row["receipt_json"]
        if transaction.state is not PatchSetState.COMMITTED:
            return None
        if not isinstance(receipt_json, str) or transaction.receipt_sha256 is None:
            raise ValueError("committed Patch Set 缺少 receipt。")
        actual = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, transaction.receipt_sha256):
            raise ValueError("Patch Set receipt 摘要不一致。")
        return receipt_json

    def mark_file_replaced(
        self, transaction_id: str, *, file_index: int
    ) -> EvolutionPatchSetTransaction:
        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state not in {PatchSetState.PREPARED, PatchSetState.APPLYING}:
                raise ValueError("Patch Set CAS state 不允许继续 apply。")
            if file_index != current.applied_count:
                raise ValueError("Patch Set 必须按 Guard 顺序 apply。")
            files = list(current.files)
            files[file_index] = _replace_file_phase(
                files[file_index], PatchSetFilePhase.REPLACED
            )
            count = current.applied_count + 1
            state = PatchSetState.APPLIED if count == len(files) else PatchSetState.APPLYING
            return _rebuild(current, files=tuple(files), applied_count=count, state=state)

        return self._mutate(transaction_id, mutate)

    def begin_rollback(self, transaction_id: str) -> EvolutionPatchSetTransaction:
        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state not in {
                PatchSetState.PREPARED,
                PatchSetState.APPLYING,
                PatchSetState.APPLIED,
            }:
                raise ValueError("Patch Set CAS state 不允许开始 rollback。")
            return _rebuild(
                current,
                state=PatchSetState.ROLLING_BACK,
                rollback_cursor=len(current.files) - 1,
            )

        return self._mutate(transaction_id, mutate)

    def mark_file_rolled_back(
        self, transaction_id: str, *, file_index: int
    ) -> EvolutionPatchSetTransaction:
        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state is not PatchSetState.ROLLING_BACK:
                raise ValueError("Patch Set CAS state 不允许标记 rollback。")
            if file_index != current.rollback_cursor:
                raise ValueError("Patch Set 必须按 Guard 逆序 rollback。")
            files = list(current.files)
            files[file_index] = _replace_file_phase(
                files[file_index], PatchSetFilePhase.ROLLED_BACK
            )
            return _rebuild(
                current,
                files=tuple(files),
                rollback_cursor=current.rollback_cursor - 1,
            )

        return self._mutate(transaction_id, mutate)

    def mark_rolled_back(
        self, transaction_id: str, *, failure_code: str
    ) -> EvolutionPatchSetTransaction:
        code = _safe_failure_code(failure_code)

        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state is not PatchSetState.ROLLING_BACK:
                raise ValueError("Patch Set CAS state 不允许完成 rollback。")
            if current.rollback_cursor != -1 or any(
                item.phase is not PatchSetFilePhase.ROLLED_BACK for item in current.files
            ):
                raise ValueError("Patch Set rollback 尚未逐文件完成。")
            return _rebuild(
                current,
                files=tuple(_drop_backup(item) for item in current.files),
                state=PatchSetState.ROLLED_BACK,
                failure_code=code,
            )

        return self._mutate(transaction_id, mutate, clear_backups=True)

    def mark_committed(
        self, transaction_id: str, *, receipt_json: str
    ) -> EvolutionPatchSetTransaction:
        encoded = receipt_json.encode("utf-8")
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("Patch Set receipt 超过 256 KiB 上限。")
        receipt_sha = hashlib.sha256(encoded).hexdigest()

        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state is not PatchSetState.APPLIED:
                raise ValueError("Patch Set CAS state 不允许 commit。")
            return _rebuild(
                current,
                files=tuple(_drop_backup(item) for item in current.files),
                state=PatchSetState.COMMITTED,
                receipt_sha256=receipt_sha,
            )

        return self._mutate(
            transaction_id,
            mutate,
            clear_backups=True,
            receipt_json=receipt_json,
        )

    def mark_recovery_failed(
        self, transaction_id: str, *, failure_code: str
    ) -> EvolutionPatchSetTransaction:
        code = _safe_failure_code(failure_code)

        def mutate(current: EvolutionPatchSetTransaction) -> EvolutionPatchSetTransaction:
            if current.state not in {
                PatchSetState.PREPARED,
                PatchSetState.APPLYING,
                PatchSetState.APPLIED,
                PatchSetState.ROLLING_BACK,
            }:
                raise ValueError("Patch Set CAS state 不允许标记 recovery failure。")
            return _rebuild(
                current,
                state=PatchSetState.RECOVERY_FAILED,
                failure_code=code,
            )

        return self._mutate(transaction_id, mutate)

    def _mutate(
        self,
        transaction_id: str,
        mutator: Callable[[EvolutionPatchSetTransaction], EvolutionPatchSetTransaction],
        *,
        clear_backups: bool = False,
        receipt_json: str | None = None,
    ) -> EvolutionPatchSetTransaction:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM evolution_patch_sets WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(transaction_id)
            current = self._from_row(db, row)
            candidate = mutator(current)
            updated = _rebuild(candidate, updated_at=_iso(self._clock()))
            cursor = db.execute(
                """UPDATE evolution_patch_sets
                   SET transaction_json = ?, state = ?, updated_at = ?, receipt_json = ?
                   WHERE transaction_id = ? AND transaction_json = ?""",
                (
                    updated.model_dump_json(),
                    updated.state.value,
                    updated.updated_at,
                    receipt_json if receipt_json is not None else row["receipt_json"],
                    transaction_id,
                    current.model_dump_json(),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Patch Set CAS 更新丢失。")
            if clear_backups:
                db.execute(
                    "DELETE FROM evolution_patch_set_backups WHERE transaction_id = ?",
                    (transaction_id,),
                )
            db.commit()
        return updated

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_patch_sets (
                   transaction_id TEXT PRIMARY KEY,
                   transaction_json TEXT NOT NULL,
                   lease_id TEXT NOT NULL UNIQUE,
                   state TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   receipt_json TEXT
               )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_patch_set_backups (
                   transaction_id TEXT NOT NULL,
                   file_index INTEGER NOT NULL,
                   backup BLOB NOT NULL,
                   PRIMARY KEY (transaction_id, file_index),
                   FOREIGN KEY (transaction_id) REFERENCES evolution_patch_sets(transaction_id)
                       ON DELETE CASCADE
               )"""
        )
        db.commit()
        return db

    def _from_row(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> EvolutionPatchSetTransaction:
        transaction = EvolutionPatchSetTransaction.model_validate_json(
            row["transaction_json"]
        )
        if row["transaction_id"] != transaction.transaction_id:
            raise ValueError("Patch Set row identity 不一致。")
        if (
            row["lease_id"] != transaction.lease_id
            or row["state"] != transaction.state.value
            or row["updated_at"] != transaction.updated_at
        ):
            raise ValueError("Patch Set row binding 不一致。")
        backups = self._backup_values(db, transaction.transaction_id)
        _validate_backups(transaction, backups)
        return transaction

    @staticmethod
    def _backup_values(db: sqlite3.Connection, transaction_id: str) -> dict[int, bytes]:
        rows = db.execute(
            """SELECT file_index, backup FROM evolution_patch_set_backups
               WHERE transaction_id = ? ORDER BY file_index""",
            (transaction_id,),
        ).fetchall()
        values: dict[int, bytes] = {}
        for row in rows:
            index = row["file_index"]
            backup = row["backup"]
            if not isinstance(index, int) or not isinstance(backup, bytes) or index in values:
                raise ValueError("Patch Set backup row 格式损坏。")
            values[index] = backup
        return values

    @staticmethod
    def _insert_backups(
        db: sqlite3.Connection,
        transaction: EvolutionPatchSetTransaction,
        before_contents: Mapping[str, bytes | None],
    ) -> None:
        for item in transaction.files:
            backup = before_contents[item.path]
            if backup is not None:
                db.execute(
                    """INSERT INTO evolution_patch_set_backups
                       (transaction_id, file_index, backup) VALUES (?, ?, ?)""",
                    (transaction.transaction_id, item.index, backup),
                )

    def _replace_row(
        self,
        db: sqlite3.Connection,
        transaction: EvolutionPatchSetTransaction,
        before_contents: Mapping[str, bytes | None],
        *,
        receipt_json: str | None,
    ) -> None:
        cursor = db.execute(
            """UPDATE evolution_patch_sets
               SET transaction_json = ?, state = ?, updated_at = ?, receipt_json = ?
               WHERE transaction_id = ?""",
            (
                transaction.model_dump_json(),
                transaction.state.value,
                transaction.updated_at,
                receipt_json,
                transaction.transaction_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Patch Set retry 更新丢失。")
        db.execute(
            "DELETE FROM evolution_patch_set_backups WHERE transaction_id = ?",
            (transaction.transaction_id,),
        )
        self._insert_backups(db, transaction, before_contents)


def _require_prepare_binding(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    guard: EvolutionStaticGuardReceipt,
    before_contents: Mapping[str, bytes | None],
    file_modes: Mapping[str, int],
) -> tuple[object, ...]:
    changes = guard.changes
    paths = tuple(item.path for item in changes)
    if lease.state is not ExperimentLeaseState.ACTIVE or not lease.worktree_ready:
        raise ValueError("Patch Set 需要 active Experiment Lease。")
    if (
        lease.contract_id != contract.contract_id
        or snapshot.contract_id != contract.contract_id
        or snapshot.lease_id != lease.lease_id
        or plan.contract_id != contract.contract_id
        or plan.lease_id != lease.lease_id
        or plan.source_snapshot_id != snapshot.snapshot_id
        or guard.contract_id != contract.contract_id
        or guard.lease_id != lease.lease_id
        or guard.source_snapshot_id != snapshot.snapshot_id
        or guard.mutation_plan_id != plan.plan_id
        or guard.mutation_plan_sha256 != plan.plan_sha256
        or not guard.preflight_passed
        or len(changes) < _MIN_FILES
        or paths != tuple(sorted(paths))
        or set(paths) != set(plan.authorized_files)
        or set(before_contents) != set(paths)
        or set(file_modes) != set(paths)
    ):
        raise ValueError("Patch Set Contract/Lease/Snapshot/Plan/Guard binding 不一致。")
    for change in changes:
        before = before_contents[change.path]
        mode = file_modes[change.path]
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
            raise ValueError("Patch Set file mode 无效。")
        if change.operation == "modify":
            if before is None or change.before_sha256 is None:
                raise ValueError("modify Patch Set 缺少 baseline backup。")
            actual = hashlib.sha256(before).hexdigest()
            if not hmac.compare_digest(actual, change.before_sha256):
                raise ValueError("Patch Set baseline backup 摘要不一致。")
        elif change.operation == "create":
            if before is not None:
                raise ValueError("create Patch Set 不得包含 baseline backup。")
        else:
            raise ValueError("Patch Set Guard operation 无效。")
    return changes


def _build_file_facts(
    changes: tuple[object, ...],
    before_contents: Mapping[str, bytes | None],
    file_modes: Mapping[str, int],
    *,
    retain_backups: bool,
) -> tuple[EvolutionPatchSetFileFact, ...]:
    facts = []
    for index, change in enumerate(changes):
        path = str(getattr(change, "path"))
        before = before_contents[path]
        payload = {
            "index": index,
            "path": path,
            "operation": getattr(change, "operation"),
            "before_sha256": getattr(change, "before_sha256"),
            "after_sha256": getattr(change, "after_sha256"),
            "backup_sha256": (
                hashlib.sha256(before).hexdigest()
                if retain_backups and before is not None
                else None
            ),
            "backup_retained": retain_backups and before is not None,
            "file_mode": file_modes[path],
            "phase": PatchSetFilePhase.PREPARED,
        }
        facts.append(_build_file_fact(payload))
    return tuple(facts)


def _build_file_fact(payload: Mapping[str, object]) -> EvolutionPatchSetFileFact:
    digest = _sha256_payload(dict(payload))
    return EvolutionPatchSetFileFact.model_validate(
        {**payload, "fact_sha256": digest}
    )


def _replace_file_phase(
    current: EvolutionPatchSetFileFact, phase: PatchSetFilePhase
) -> EvolutionPatchSetFileFact:
    payload = current.model_dump(mode="python", exclude={"fact_sha256", "phase"})
    return _build_file_fact({**payload, "phase": phase})


def _drop_backup(current: EvolutionPatchSetFileFact) -> EvolutionPatchSetFileFact:
    payload = current.model_dump(
        mode="python", exclude={"fact_sha256", "backup_sha256", "backup_retained"}
    )
    return _build_file_fact(
        {**payload, "backup_sha256": None, "backup_retained": False}
    )


def _build_transaction(**payload: object) -> EvolutionPatchSetTransaction:
    base = {"schema_version": 1, "policy_version": PATCH_SET_POLICY, **payload}
    digest = _sha256_payload(base)
    return EvolutionPatchSetTransaction.model_validate(
        {**base, "transaction_sha256": digest}
    )


def _rebuild(
    current: EvolutionPatchSetTransaction, **updates: object
) -> EvolutionPatchSetTransaction:
    payload = current.model_dump(
        mode="python",
        exclude={"schema_version", "policy_version", "transaction_sha256"},
    )
    payload.update(updates)
    payload.setdefault("updated_at", _iso(datetime.now(UTC)))
    return _build_transaction(**payload)


def _require_same_core(
    current: EvolutionPatchSetTransaction,
    transaction_id: str,
    core: Mapping[str, object],
) -> None:
    if current.transaction_id != transaction_id:
        raise ValueError("Lease 已绑定其他 Patch Set。")
    for key in (
        "contract_id",
        "lease_id",
        "source_snapshot_id",
        "mutation_plan_id",
        "worktree_name",
        "worktree_path",
    ):
        if getattr(current, key) != core[key]:
            raise ValueError("Patch Set retry 偏离原始计划边界。")
    if tuple(item.path for item in current.files) != tuple(core["paths"]):
        raise ValueError("Patch Set retry 改变文件范围或顺序。")
    if tuple(item.operation for item in current.files) != tuple(core["operations"]):
        raise ValueError("Patch Set retry 改变文件操作类型。")
    if tuple(item.before_sha256 for item in current.files) != tuple(core["before_sha256"]):
        raise ValueError("Patch Set retry 改变 baseline authority。")


def _require_same_attempt(
    current: EvolutionPatchSetTransaction,
    guard: EvolutionStaticGuardReceipt,
) -> None:
    if (
        current.guard_id != guard.guard_id
        or current.guard_receipt_sha256 != guard.receipt_sha256
        or tuple(item.after_sha256 for item in current.files)
        != tuple(item.after_sha256 for item in guard.changes)
    ):
        raise ValueError("活动 Patch Set 已绑定不同 Guard attempt。")


def _validate_backups(
    transaction: EvolutionPatchSetTransaction,
    backups: Mapping[int, bytes],
) -> tuple[bytes | None, ...]:
    expected_indices = {
        item.index for item in transaction.files if item.backup_retained
    }
    if set(backups) != expected_indices:
        raise ValueError("Patch Set backup 行与 transaction facts 不一致。")
    values: list[bytes | None] = []
    for item in transaction.files:
        if not item.backup_retained:
            values.append(None)
            continue
        backup = backups[item.index]
        actual = hashlib.sha256(backup).hexdigest()
        if item.backup_sha256 is None or not hmac.compare_digest(
            actual, item.backup_sha256
        ):
            raise ValueError("Patch Set backup 摘要不一致。")
        values.append(backup)
    return tuple(values)


def _safe_failure_code(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or not all(
        char.isalnum() or char in "_.-" for char in normalized
    ):
        raise ValueError("Patch Set failure code 格式无效。")
    return normalized


def _safe_row_id(value: object, *, prefix: str, pattern: str) -> str:
    if isinstance(value, str) and re.fullmatch(pattern, value):
        return value
    digest = hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Patch Set 时间必须含时区。")
    return parsed


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported canonical payload type: {type(value).__name__}")


__all__ = [
    "EvolutionPatchSetFileFact",
    "EvolutionPatchSetStore",
    "EvolutionPatchSetTransaction",
    "PatchSetFilePhase",
    "PatchSetScanFailure",
    "PatchSetState",
]
