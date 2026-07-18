"""Durable intent journal for isolated evolution patch writes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuardReceipt,
    StaticGuardChangeFact,
)

PATCH_JOURNAL_POLICY = "evolution-patch-journal-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 256 * 1024


class PatchJournalState(StrEnum):
    PREPARED = "prepared"
    REPLACED = "replaced"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_FAILED = "recovery_failed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPatchJournal(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-patch-journal-v1"] = PATCH_JOURNAL_POLICY
    journal_id: str = Field(pattern=r"^evj_[0-9a-f]{24}$")
    journal_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    guard_receipt_sha256: str = Field(pattern=_SHA256_RE)
    worktree_name: str = Field(pattern=r"^experiment-[0-9a-f]{16}$")
    worktree_path: str = Field(min_length=1, max_length=4_096)
    target_path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    backup_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    backup_present: bool
    file_mode: int = Field(ge=0, le=0o777)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    state: PatchJournalState
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    failure_code: str = Field(default="", max_length=128)
    created_at: str
    updated_at: str

    @field_validator("worktree_path")
    @classmethod
    def _absolute_worktree(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("Patch Journal worktree_path 必须是安全绝对路径。")
        return str(path.resolve())

    @field_validator("target_path")
    @classmethod
    def _safe_target(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(ord(char) < 32 for char in normalized)
        ):
            raise ValueError("Patch Journal target_path 必须是安全相对路径。")
        return normalized

    @model_validator(mode="after")
    def _state_and_digest_match(self) -> Self:
        if self.attempt > self.max_attempts:
            raise ValueError("Patch Journal attempt 超过计划上限。")
        if self.operation == "modify":
            if self.before_sha256 is None:
                raise ValueError("modify Journal 缺少 before digest。")
        elif self.before_sha256 is not None:
            raise ValueError("create Journal 不得包含 before digest。")
        if self.backup_present != (self.backup_sha256 is not None):
            raise ValueError("Patch Journal backup presence 与摘要不一致。")
        if self.state in {PatchJournalState.PREPARED, PatchJournalState.REPLACED}:
            if self.operation == "modify" and not self.backup_present:
                raise ValueError("活动 modify Journal 必须保留 backup。")
            if self.receipt_sha256 is not None or self.failure_code:
                raise ValueError("活动 Patch Journal 不得提前写入终态字段。")
        if self.state is PatchJournalState.COMMITTED:
            if self.receipt_sha256 is None or self.backup_present or self.failure_code:
                raise ValueError("committed Journal 终态字段不一致。")
        if self.state is PatchJournalState.ROLLED_BACK:
            if self.receipt_sha256 is not None or self.backup_present:
                raise ValueError("rolled_back Journal 不得保留 receipt/backup。")
        if self.state is PatchJournalState.RECOVERY_FAILED:
            if not self.failure_code or self.receipt_sha256 is not None:
                raise ValueError("recovery_failed Journal 必须保留 failure code。")
        _parse_time(self.created_at)
        _parse_time(self.updated_at)
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"journal_sha256"})
        )
        if not hmac.compare_digest(self.journal_sha256, expected):
            raise ValueError("journal_sha256 与 Patch Journal 不一致。")
        return self


class PatchJournalScanFailure(_StrictModel):
    journal_id: str = Field(pattern=r"^evj_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    failure_code: Literal["journal_corrupt"] = "journal_corrupt"


class EvolutionPatchJournalStore:
    """SQLite CAS store holding recoverable pre-replacement bytes."""

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
        change: StaticGuardChangeFact,
        before: bytes | None,
        file_mode: int,
    ) -> EvolutionPatchJournal:
        _require_prepare_binding(
            contract,
            lease,
            source_snapshot,
            mutation_plan,
            guard_receipt,
            change,
            before,
        )
        now = _iso(self._clock())
        identity = {
            "contract_id": contract.contract_id,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "mutation_plan_id": mutation_plan.plan_id,
            "guard_id": guard_receipt.guard_id,
            "guard_receipt_sha256": guard_receipt.receipt_sha256,
            "worktree_name": lease.worktree_name,
            "worktree_path": lease.worktree_path,
            "target_path": change.path,
            "operation": change.operation,
            "before_sha256": change.before_sha256,
            "after_sha256": change.after_sha256,
        }
        journal_id = f"evj_{_sha256_payload(identity)[:24]}"
        backup_sha = hashlib.sha256(before).hexdigest() if before is not None else None
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM evolution_patch_journals WHERE lease_id = ?",
                (lease.lease_id,),
            ).fetchone()
            if row is not None:
                current = self._from_row(row)
                if current.state is not PatchJournalState.ROLLED_BACK:
                    _require_same_identity(current, journal_id, identity)
                    db.commit()
                    return current
                _require_retry_binding(current, identity)
                if current.attempt >= current.max_attempts:
                    db.commit()
                    return current
                journal = _build_journal(
                    **identity,
                    journal_id=journal_id,
                    backup_sha256=backup_sha,
                    backup_present=before is not None,
                    file_mode=file_mode,
                    attempt=current.attempt + 1,
                    max_attempts=mutation_plan.max_attempts,
                    state=PatchJournalState.PREPARED,
                    receipt_sha256=None,
                    failure_code="",
                    created_at=current.created_at,
                    updated_at=now,
                )
                self._update_row(
                    db,
                    journal,
                    backup=before,
                    receipt_json=None,
                    previous_journal_id=current.journal_id,
                )
                db.commit()
                return journal
            journal = _build_journal(
                **identity,
                journal_id=journal_id,
                backup_sha256=backup_sha,
                backup_present=before is not None,
                file_mode=file_mode,
                attempt=1,
                max_attempts=mutation_plan.max_attempts,
                state=PatchJournalState.PREPARED,
                receipt_sha256=None,
                failure_code="",
                created_at=now,
                updated_at=now,
            )
            db.execute(
                """INSERT INTO evolution_patch_journals
                   (journal_id, journal_json, lease_id, state, updated_at, backup,
                    receipt_json)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    journal.journal_id,
                    journal.model_dump_json(),
                    journal.lease_id,
                    journal.state.value,
                    journal.updated_at,
                    before,
                ),
            )
            db.commit()
        return journal

    def get_by_lease(self, lease_id: str) -> EvolutionPatchJournal | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_journals WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_recoverable(self, *, limit: int = 100) -> tuple[EvolutionPatchJournal, ...]:
        journals, failures = self.scan_recoverable(limit=limit)
        if failures:
            raise ValueError("检测到损坏的 Patch Journal。")
        return journals

    def scan_recoverable(
        self,
        *,
        limit: int = 100,
    ) -> tuple[tuple[EvolutionPatchJournal, ...], tuple[PatchJournalScanFailure, ...]]:
        if limit < 1 or limit > 1_000:
            raise ValueError("Patch Journal recovery limit 必须在 1..1000。")
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM evolution_patch_journals
                   WHERE state IN ('prepared', 'replaced')
                   ORDER BY updated_at, journal_id LIMIT ?""",
                (limit,),
            ).fetchall()
        journals: list[EvolutionPatchJournal] = []
        failures: list[PatchJournalScanFailure] = []
        for row in rows:
            try:
                journals.append(self._from_row(row))
            except (TypeError, ValueError):
                failures.append(
                    PatchJournalScanFailure(
                        journal_id=_safe_row_id(
                            row["journal_id"],
                            prefix="evj",
                            pattern=r"^evj_[0-9a-f]{24}$",
                        ),
                        lease_id=_safe_row_id(
                            row["lease_id"],
                            prefix="evl",
                            pattern=r"^evl_[0-9a-f]{24}$",
                        ),
                    )
                )
        return tuple(journals), tuple(failures)

    def load_backup(self, journal_id: str) -> bytes | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_journals WHERE journal_id = ?",
                (journal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(journal_id)
        journal = self._from_row(row)
        backup = row["backup"]
        if journal.backup_present:
            if not isinstance(backup, bytes) or journal.backup_sha256 is None:
                raise ValueError("Patch Journal backup 缺失。")
            if not hmac.compare_digest(hashlib.sha256(backup).hexdigest(), journal.backup_sha256):
                raise ValueError("Patch Journal backup 摘要不一致。")
            return backup
        if backup is not None:
            raise ValueError("Patch Journal 存在未声明 backup。")
        return None

    def load_receipt_json(self, journal_id: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_patch_journals WHERE journal_id = ?",
                (journal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(journal_id)
        journal = self._from_row(row)
        receipt_json = row["receipt_json"]
        if journal.state is not PatchJournalState.COMMITTED:
            return None
        if not isinstance(receipt_json, str) or journal.receipt_sha256 is None:
            raise ValueError("committed Patch Journal 缺少 receipt。")
        if not hmac.compare_digest(
            hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
            journal.receipt_sha256,
        ):
            raise ValueError("Patch Journal receipt 摘要不一致。")
        return receipt_json

    def mark_replaced(self, journal_id: str) -> EvolutionPatchJournal:
        return self._transition(
            journal_id,
            expected=PatchJournalState.PREPARED,
            state=PatchJournalState.REPLACED,
        )

    def mark_committed(self, journal_id: str, *, receipt_json: str) -> EvolutionPatchJournal:
        encoded = receipt_json.encode("utf-8")
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("Patch Journal receipt 超过 256 KiB 上限。")
        receipt_sha = hashlib.sha256(encoded).hexdigest()
        return self._transition(
            journal_id,
            expected=PatchJournalState.REPLACED,
            state=PatchJournalState.COMMITTED,
            receipt_sha256=receipt_sha,
            receipt_json=receipt_json,
            clear_backup=True,
        )

    def mark_rolled_back(
        self,
        journal_id: str,
        *,
        failure_code: str,
    ) -> EvolutionPatchJournal:
        return self._transition(
            journal_id,
            expected=(PatchJournalState.PREPARED, PatchJournalState.REPLACED),
            state=PatchJournalState.ROLLED_BACK,
            failure_code=_safe_failure_code(failure_code),
            clear_backup=True,
        )

    def mark_recovery_failed(
        self,
        journal_id: str,
        *,
        failure_code: str,
    ) -> EvolutionPatchJournal:
        return self._transition(
            journal_id,
            expected=(PatchJournalState.PREPARED, PatchJournalState.REPLACED),
            state=PatchJournalState.RECOVERY_FAILED,
            failure_code=_safe_failure_code(failure_code),
        )

    def _transition(
        self,
        journal_id: str,
        *,
        expected: PatchJournalState | tuple[PatchJournalState, ...],
        state: PatchJournalState,
        receipt_sha256: str | None = None,
        receipt_json: str | None = None,
        failure_code: str = "",
        clear_backup: bool = False,
    ) -> EvolutionPatchJournal:
        expected_states = (expected,) if isinstance(expected, PatchJournalState) else expected
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM evolution_patch_journals WHERE journal_id = ?",
                (journal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(journal_id)
            current = self._from_row(row)
            if current.state not in expected_states:
                raise ValueError("Patch Journal CAS state 不一致。")
            updated = _build_journal(
                **current.model_dump(
                    mode="python",
                    exclude={
                        "schema_version",
                        "policy_version",
                        "journal_sha256",
                        "state",
                        "receipt_sha256",
                        "failure_code",
                        "backup_sha256",
                        "backup_present",
                        "updated_at",
                    },
                ),
                state=state,
                receipt_sha256=receipt_sha256,
                failure_code=failure_code,
                backup_sha256=None if clear_backup else current.backup_sha256,
                backup_present=False if clear_backup else current.backup_present,
                updated_at=_iso(self._clock()),
            )
            backup = None if clear_backup else row["backup"]
            self._update_row(db, updated, backup=backup, receipt_json=receipt_json)
            db.commit()
        return updated

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_patch_journals (
                   journal_id TEXT PRIMARY KEY,
                   journal_json TEXT NOT NULL,
                   lease_id TEXT NOT NULL UNIQUE,
                   state TEXT NOT NULL,
                   updated_at TEXT NOT NULL DEFAULT '',
                   backup BLOB,
                   receipt_json TEXT
               )"""
        )
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(evolution_patch_journals)")
        }
        if "updated_at" not in columns:
            db.execute(
                "ALTER TABLE evolution_patch_journals "
                "ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
        db.commit()
        return db

    def _from_row(self, row: sqlite3.Row) -> EvolutionPatchJournal:
        journal = EvolutionPatchJournal.model_validate_json(row["journal_json"])
        if row["journal_id"] != journal.journal_id:
            raise ValueError("Patch Journal row identity 不一致。")
        if row["lease_id"] != journal.lease_id or row["state"] != journal.state.value:
            raise ValueError("Patch Journal row binding 不一致。")
        backup = row["backup"]
        if journal.backup_present:
            if not isinstance(backup, bytes) or journal.backup_sha256 is None:
                raise ValueError("Patch Journal backup 缺失。")
            if not hmac.compare_digest(hashlib.sha256(backup).hexdigest(), journal.backup_sha256):
                raise ValueError("Patch Journal backup 摘要不一致。")
        elif backup is not None:
            raise ValueError("Patch Journal 存在未声明 backup。")
        return journal

    @staticmethod
    def _update_row(
        db: sqlite3.Connection,
        journal: EvolutionPatchJournal,
        *,
        backup: bytes | None,
        receipt_json: str | None,
        previous_journal_id: str | None = None,
    ) -> None:
        cursor = db.execute(
            """UPDATE evolution_patch_journals
               SET journal_id = ?, journal_json = ?, lease_id = ?, state = ?,
                   updated_at = ?, backup = ?, receipt_json = ?
               WHERE journal_id = ?""",
            (
                journal.journal_id,
                journal.model_dump_json(),
                journal.lease_id,
                journal.state.value,
                journal.updated_at,
                backup,
                receipt_json,
                previous_journal_id or journal.journal_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Patch Journal 更新丢失。")


def _require_prepare_binding(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    guard: EvolutionStaticGuardReceipt,
    change: StaticGuardChangeFact,
    before: bytes | None,
) -> None:
    if (
        lease.contract_id != contract.contract_id
        or snapshot.contract_id != contract.contract_id
        or snapshot.lease_id != lease.lease_id
        or plan.contract_id != contract.contract_id
        or plan.lease_id != lease.lease_id
        or guard.contract_id != contract.contract_id
        or guard.lease_id != lease.lease_id
        or guard.source_snapshot_id != snapshot.snapshot_id
        or guard.mutation_plan_id != plan.plan_id
        or not guard.preflight_passed
        or len(guard.changes) != 1
        or guard.changes[0] != change
    ):
        raise ValueError("Patch Journal Contract/Lease/Snapshot/Plan/Guard binding 不一致。")
    if change.operation == "modify":
        if before is None or change.before_sha256 is None:
            raise ValueError("modify Patch Journal 缺少 baseline backup。")
        if not hmac.compare_digest(hashlib.sha256(before).hexdigest(), change.before_sha256):
            raise ValueError("Patch Journal baseline backup 摘要不一致。")
    elif change.operation == "create" and before is not None:
        raise ValueError("create Patch Journal 不得包含 baseline backup。")


def _require_same_identity(
    current: EvolutionPatchJournal,
    journal_id: str,
    identity: dict[str, object],
) -> None:
    if current.journal_id != journal_id:
        raise ValueError("Lease 已绑定其他 Patch Journal。")
    for key, value in identity.items():
        if getattr(current, key) != value:
            raise ValueError("Patch Journal identity 与现有 Lease journal 不一致。")


def _require_retry_binding(
    current: EvolutionPatchJournal,
    identity: dict[str, object],
) -> None:
    for key in (
        "contract_id",
        "lease_id",
        "source_snapshot_id",
        "mutation_plan_id",
        "worktree_name",
        "worktree_path",
        "target_path",
        "operation",
        "before_sha256",
    ):
        if getattr(current, key) != identity[key]:
            raise ValueError("Patch Journal retry 扩大或偏离原始计划边界。")


def _build_journal(**payload: object) -> EvolutionPatchJournal:
    base = {
        "schema_version": 1,
        "policy_version": PATCH_JOURNAL_POLICY,
        **payload,
    }
    digest = _sha256_payload(base)
    return EvolutionPatchJournal.model_validate({**base, "journal_sha256": digest})


def _safe_failure_code(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or not all(
        char.isalnum() or char in "_.-" for char in normalized
    ):
        raise ValueError("Patch Journal failure code 格式无效。")
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
        raise ValueError("Patch Journal 时间必须含时区。")
    return parsed


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionPatchJournal",
    "EvolutionPatchJournalStore",
    "PatchJournalScanFailure",
    "PatchJournalState",
]
