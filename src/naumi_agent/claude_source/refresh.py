"""Governed refresh approval and immutable history for Claude source identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.governance import (
    SourceAuditResult,
    SourceIdentityManifest,
    capture_source_identity,
    load_source_identity,
    verify_source_identity,
    write_source_identity,
)
from naumi_agent.config.state_paths import resolve_naumi_state_home

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
_SECRET_MARKERS = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:api[_-]?key|token|password)\s*[:=])"
)
CLAUDE_SOURCE_STORE_SCHEMA_VERSION = 1
_STORE_COLUMNS = {
    "source_identity_history": (
        "entry_id",
        "source_name",
        "revision",
        "manifest_sha256",
        "payload_json",
        "reviewed_at",
    ),
    "source_refresh_proposals": (
        "proposal_id",
        "source_name",
        "base_entry_id",
        "status",
        "payload_json",
        "created_at",
    ),
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceIdentityHistoryEntry(_StrictModel):
    schema_version: Literal[1] = 1
    entry_id: str
    source_name: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    previous_entry_id: str = ""
    manifest_sha256: str
    identity_sha256: str
    manifest: SourceIdentityManifest
    audit_status: Literal["valid"] = "valid"
    audit_findings: tuple[str, ...] = ()
    decision_kind: Literal["baseline_import", "refresh_approved"]
    reviewed_by: str
    review_reason: str = Field(min_length=1, max_length=500)
    license_change_acknowledged: bool = False
    mapping_change_acknowledged: bool = False
    reviewed_at: str

    @field_validator(
        "entry_id",
        "manifest_sha256",
        "identity_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("previous_entry_id")
    @classmethod
    def _optional_digest(cls, value: str) -> str:
        return _require_sha256(value) if value else ""

    @field_validator("reviewed_by")
    @classmethod
    def _actor(cls, value: str) -> str:
        return _safe_actor(value)

    @field_validator("review_reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _safe_review_reason(value)

    @field_validator("reviewed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _aware_timestamp(value, "reviewed_at")

    @model_validator(mode="after")
    def _integrity(self) -> SourceIdentityHistoryEntry:
        if self.source_name != self.manifest.source_name:
            raise ValueError("history source_name 与 manifest 不一致。")
        if self.manifest_sha256 != manifest_sha256(self.manifest):
            raise ValueError("history manifest digest 不一致。")
        if self.identity_sha256 != source_identity_sha256(self.manifest):
            raise ValueError("history identity digest 不一致。")
        if self.entry_id != _history_entry_id(self):
            raise ValueError("history entry digest 不一致。")
        if self.audit_findings:
            raise ValueError("valid history entry 不得携带 audit findings。")
        return self


class SourceRefreshProposal(_StrictModel):
    schema_version: Literal[1] = 1
    proposal_id: str
    source_name: str = Field(min_length=1, max_length=128)
    base_entry_id: str
    base_manifest_sha256: str
    candidate_manifest_sha256: str
    candidate_identity_sha256: str
    candidate: SourceIdentityManifest
    changes: tuple[str, ...] = Field(min_length=1, max_length=20)
    requires_license_review: bool
    requires_mapping_review: bool
    status: Literal["pending", "approved", "stale", "rejected"] = "pending"
    created_at: str
    decided_at: str = ""
    reviewed_by: str = ""
    review_reason: str = ""

    @field_validator(
        "proposal_id",
        "base_entry_id",
        "base_manifest_sha256",
        "candidate_manifest_sha256",
        "candidate_identity_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("created_at")
    @classmethod
    def _created_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value, "created_at")

    @field_validator("decided_at")
    @classmethod
    def _decided_timestamp(cls, value: str) -> str:
        return _aware_timestamp(value, "decided_at") if value else ""

    @model_validator(mode="after")
    def _integrity(self) -> SourceRefreshProposal:
        if self.source_name != self.candidate.source_name:
            raise ValueError("proposal source_name 与 candidate 不一致。")
        if self.candidate_manifest_sha256 != manifest_sha256(self.candidate):
            raise ValueError("proposal candidate manifest digest 不一致。")
        if self.candidate_identity_sha256 != source_identity_sha256(self.candidate):
            raise ValueError("proposal candidate identity digest 不一致。")
        if self.changes != tuple(sorted(set(self.changes))):
            raise ValueError("proposal changes 必须排序且不得重复。")
        if self.proposal_id != _proposal_id(
            source_name=self.source_name,
            base_entry_id=self.base_entry_id,
            base_manifest_sha256=self.base_manifest_sha256,
            candidate_identity_sha256=self.candidate_identity_sha256,
            changes=self.changes,
        ):
            raise ValueError("proposal digest 不一致。")
        if self.requires_license_review != any(
            item.startswith("license.") for item in self.changes
        ):
            raise ValueError("proposal license review 标志与 changes 不一致。")
        if self.requires_mapping_review != ("mapping.sha256" in self.changes):
            raise ValueError("proposal mapping review 标志与 changes 不一致。")
        if self.status == "pending":
            if self.decided_at or self.reviewed_by or self.review_reason:
                raise ValueError("pending proposal 不得携带审批结果。")
        else:
            _safe_actor(self.reviewed_by)
            _safe_review_reason(self.review_reason)
            if not self.decided_at:
                raise ValueError("终态 proposal 必须包含 decided_at。")
        return self


class SourceRefreshAssessment(_StrictModel):
    status: Literal["unchanged", "change_detected", "invalid"]
    audit: SourceAuditResult
    proposal: SourceRefreshProposal | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SourceRefreshAssessment:
        if (self.status == "change_detected") != (self.proposal is not None):
            raise ValueError("change_detected 与 proposal 必须同时出现。")
        if self.status == "unchanged" and self.audit.status != "valid":
            raise ValueError("unchanged 必须来自 valid audit。")
        if self.status == "invalid" and self.audit.status != "invalid":
            raise ValueError("invalid assessment 必须来自 invalid audit。")
        return self


def resolve_claude_source_db_path() -> Path:
    """Return the platform-native source governance database path."""
    return resolve_naumi_state_home() / "claude-source.db"


class SourceRefreshStore:
    """SQLite authority for approved identities and pending refresh proposals."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or resolve_claude_source_db_path()).expanduser().resolve()

    def bootstrap(
        self,
        manifest: SourceIdentityManifest,
        *,
        source_root: str | Path,
        project_root: str | Path,
        reviewed_by: str,
        review_reason: str,
        reviewed_at: datetime | None = None,
    ) -> SourceIdentityHistoryEntry:
        """Record one already-reviewed valid baseline, idempotently."""
        audit = verify_source_identity(manifest, source_root, project_root)
        if audit.status != "valid" or audit.findings:
            raise ValueError("只有当前验证为 valid 的 manifest 才能建立历史基线。")
        actor = _safe_actor(reviewed_by)
        reason = _safe_review_reason(review_reason)
        timestamp = _timestamp(reviewed_at)
        with self._connect() as db:
            self._begin(db)
            latest = self._latest_entry(db, manifest.source_name)
            digest = manifest_sha256(manifest)
            if latest is not None:
                if latest.manifest_sha256 == digest:
                    db.commit()
                    return latest
                db.rollback()
                raise ValueError("source history 已有其他基线，必须走 refresh approval。")
            entry = _build_history_entry(
                manifest=manifest,
                revision=1,
                previous_entry_id="",
                decision_kind="baseline_import",
                reviewed_by=actor,
                review_reason=reason,
                license_change_acknowledged=False,
                mapping_change_acknowledged=False,
                reviewed_at=timestamp,
            )
            self._insert_entry(db, entry)
            db.commit()
            return entry

    def propose(
        self,
        current: SourceIdentityManifest,
        candidate: SourceIdentityManifest,
        *,
        source_root: str | Path,
        project_root: str | Path,
        changes: tuple[str, ...],
        created_at: datetime | None = None,
    ) -> SourceRefreshProposal:
        """Persist one stable proposal without approving or changing the manifest."""
        candidate_audit = verify_source_identity(candidate, source_root, project_root)
        if candidate_audit.status != "valid" or candidate_audit.findings:
            raise ValueError("candidate 当前不是 valid，不能建立 refresh proposal。")
        normalized_changes = tuple(sorted(set(changes)))
        if not normalized_changes:
            raise ValueError("相同 identity 不得制造 refresh proposal。")
        if current.source_name != candidate.source_name:
            raise ValueError("refresh candidate 必须属于同一 source。")
        expected_changes = _identity_changes(current, candidate)
        if normalized_changes != expected_changes:
            raise ValueError("refresh changes 与真实 identity 差异不一致。")
        with self._connect() as db:
            self._begin(db)
            latest = self._latest_entry(db, current.source_name)
            if latest is None:
                db.rollback()
                raise ValueError("source history 尚未 bootstrap。")
            current_digest = manifest_sha256(current)
            if latest.manifest_sha256 != current_digest:
                db.rollback()
                raise ValueError("current manifest 与已审批 history 不一致。")
            proposal = _build_proposal(
                current=current,
                candidate=candidate,
                base_entry_id=latest.entry_id,
                changes=normalized_changes,
                created_at=_timestamp(created_at),
            )
            existing = db.execute(
                """
                SELECT proposal_id, source_name, base_entry_id, status, payload_json, created_at
                FROM source_refresh_proposals WHERE proposal_id = ?
                """,
                (proposal.proposal_id,),
            ).fetchone()
            if existing is not None:
                restored = self._proposal_from_row(existing)
                db.commit()
                return restored
            db.execute(
                """
                INSERT INTO source_refresh_proposals (
                    proposal_id, source_name, base_entry_id, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.source_name,
                    proposal.base_entry_id,
                    proposal.status,
                    proposal.model_dump_json(),
                    proposal.created_at,
                ),
            )
            db.commit()
            return proposal

    def approve(
        self,
        proposal_id: str,
        *,
        source_root: str | Path,
        project_root: str | Path,
        reviewed_by: str,
        review_reason: str,
        allow_license_change: bool = False,
        allow_mapping_change: bool = False,
        decided_at: datetime | None = None,
    ) -> tuple[SourceRefreshProposal, SourceIdentityHistoryEntry]:
        """Approve a still-current proposal and append exactly one history entry."""
        proposal_key = _require_sha256(proposal_id)
        actor = _safe_actor(reviewed_by)
        reason = _safe_review_reason(review_reason)
        timestamp = _timestamp(decided_at)
        proposal_before_lock = self.get_proposal(proposal_key)
        if proposal_before_lock is None:
            raise ValueError("refresh proposal 不存在。")
        if proposal_before_lock.status == "pending":
            live_audit = verify_source_identity(
                proposal_before_lock.candidate,
                source_root,
                project_root,
            )
            if live_audit.status != "valid" or live_audit.findings:
                raise ValueError("candidate 已变化或无效，不能审批。")
        with self._connect() as db:
            self._begin(db)
            row = db.execute(
                """
                SELECT proposal_id, source_name, base_entry_id, status, payload_json, created_at
                FROM source_refresh_proposals WHERE proposal_id = ?
                """,
                (proposal_key,),
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("refresh proposal 不存在。")
            proposal = self._proposal_from_row(row)
            latest = self._latest_entry(db, proposal.source_name)
            if latest is None:
                db.rollback()
                raise ValueError("source history 不存在。")
            if proposal.status == "approved":
                if latest.manifest_sha256 != proposal.candidate_manifest_sha256:
                    db.rollback()
                    raise ValueError("已审批 proposal 与最新 history 不一致。")
                db.commit()
                return proposal, latest
            if proposal.status != "pending":
                db.rollback()
                raise ValueError(f"refresh proposal 已是 {proposal.status}。")
            if (
                latest.entry_id != proposal.base_entry_id
                or latest.manifest_sha256 != proposal.base_manifest_sha256
            ):
                stale = proposal.model_copy(update={
                    "status": "stale",
                    "decided_at": timestamp,
                    "reviewed_by": actor,
                    "review_reason": reason,
                })
                self._update_proposal(db, stale)
                db.commit()
                raise ValueError("refresh proposal 基线已变化，必须重新生成。")
            if proposal.requires_license_review and not allow_license_change:
                db.rollback()
                raise ValueError("许可证证据已变化，必须显式确认 license review。")
            if proposal.requires_mapping_review and not allow_mapping_change:
                db.rollback()
                raise ValueError("映射证据已变化，必须显式确认 mapping review。")
            entry = _build_history_entry(
                manifest=proposal.candidate,
                revision=latest.revision + 1,
                previous_entry_id=latest.entry_id,
                decision_kind="refresh_approved",
                reviewed_by=actor,
                review_reason=reason,
                license_change_acknowledged=(
                    proposal.requires_license_review and allow_license_change
                ),
                mapping_change_acknowledged=(
                    proposal.requires_mapping_review and allow_mapping_change
                ),
                reviewed_at=timestamp,
            )
            approved = proposal.model_copy(update={
                "status": "approved",
                "decided_at": timestamp,
                "reviewed_by": actor,
                "review_reason": reason,
            })
            self._insert_entry(db, entry)
            self._update_proposal(db, approved)
            db.commit()
            return approved, entry

    def get_proposal(self, proposal_id: str) -> SourceRefreshProposal | None:
        if not self.db_path.is_file():
            return None
        with self._connect_readonly() as db:
            row = db.execute(
                """
                SELECT proposal_id, source_name, base_entry_id, status, payload_json, created_at
                FROM source_refresh_proposals WHERE proposal_id = ?
                """,
                (_require_sha256(proposal_id),),
            ).fetchone()
        return self._proposal_from_row(row) if row else None

    def list_history(
        self,
        source_name: str,
        *,
        limit: int = 50,
    ) -> tuple[SourceIdentityHistoryEntry, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("history limit 必须在 1 到 200 之间。")
        if not self.db_path.is_file():
            return ()
        with self._connect_readonly() as db:
            entries = self._history_entries(db, source_name.strip())
        return tuple(reversed(entries))[:limit]

    def latest(self, source_name: str) -> SourceIdentityHistoryEntry | None:
        if not self.db_path.is_file():
            return None
        with self._connect_readonly() as db:
            return self._latest_entry(db, source_name.strip())

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = f"{self.db_path.as_uri()}?mode=ro"
        db = sqlite3.connect(uri, timeout=10, uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=10000")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version != CLAUDE_SOURCE_STORE_SCHEMA_VERSION:
                raise ValueError(
                    "claude-source.db schema 版本不受支持："
                    f"{version}；当前仅支持 v{CLAUDE_SOURCE_STORE_SCHEMA_VERSION}。"
                )
            self._validate_schema(db)
            return db
        except Exception:
            db.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            if os.name != "nt":
                self.db_path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                self._create_schema(db)
                self._validate_schema(db)
                db.execute(
                    f"PRAGMA user_version = {CLAUDE_SOURCE_STORE_SCHEMA_VERSION}"
                )
            elif version == CLAUDE_SOURCE_STORE_SCHEMA_VERSION:
                self._validate_schema(db)
            else:
                raise ValueError(
                    "claude-source.db schema 版本不受支持："
                    f"{version}；当前仅支持 v{CLAUDE_SOURCE_STORE_SCHEMA_VERSION}。"
                )
            db.commit()
            return db
        except Exception:
            db.rollback()
            db.close()
            raise

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS source_identity_history (
                entry_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                manifest_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                UNIQUE(source_name, revision),
                UNIQUE(source_name, manifest_sha256)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS source_refresh_proposals (
                proposal_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                base_entry_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        for table, expected in _STORE_COLUMNS.items():
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            observed = tuple(str(row["name"]) for row in rows)
            if observed != expected:
                raise ValueError(
                    f"claude-source.db 表结构无效：{table}；"
                    "请保留数据库并运行诊断，不会自动覆盖。"
                )

    @staticmethod
    def _begin(db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")

    @classmethod
    def _latest_entry(
        cls,
        db: sqlite3.Connection,
        source_name: str,
    ) -> SourceIdentityHistoryEntry | None:
        entries = cls._history_entries(db, source_name)
        return entries[-1] if entries else None

    @classmethod
    def _history_entries(
        cls,
        db: sqlite3.Connection,
        source_name: str,
    ) -> tuple[SourceIdentityHistoryEntry, ...]:
        rows = db.execute(
            """
            SELECT entry_id, source_name, revision, manifest_sha256, payload_json, reviewed_at
            FROM source_identity_history
            WHERE source_name = ? ORDER BY revision ASC
            """,
            (source_name,),
        ).fetchall()
        entries = tuple(cls._entry_from_row(row) for row in rows)
        previous_entry_id = ""
        for expected_revision, entry in enumerate(entries, start=1):
            if (
                entry.revision != expected_revision
                or entry.previous_entry_id != previous_entry_id
            ):
                raise ValueError("source history 链断裂或 revision 不连续。")
            previous_entry_id = entry.entry_id
        return entries

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> SourceIdentityHistoryEntry:
        entry = SourceIdentityHistoryEntry.model_validate_json(row["payload_json"])
        if (
            row["entry_id"] != entry.entry_id
            or row["source_name"] != entry.source_name
            or int(row["revision"]) != entry.revision
            or row["manifest_sha256"] != entry.manifest_sha256
            or row["reviewed_at"] != entry.reviewed_at
        ):
            raise ValueError("source history 投影列与不可变 payload 不一致。")
        return entry

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> SourceRefreshProposal:
        proposal = SourceRefreshProposal.model_validate_json(row["payload_json"])
        if (
            row["proposal_id"] != proposal.proposal_id
            or row["source_name"] != proposal.source_name
            or row["base_entry_id"] != proposal.base_entry_id
            or row["status"] != proposal.status
            or row["created_at"] != proposal.created_at
        ):
            raise ValueError("refresh proposal 投影列与不可变 payload 不一致。")
        return proposal

    @staticmethod
    def _insert_entry(db: sqlite3.Connection, entry: SourceIdentityHistoryEntry) -> None:
        db.execute(
            """
            INSERT INTO source_identity_history (
                entry_id, source_name, revision, manifest_sha256, payload_json, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.source_name,
                entry.revision,
                entry.manifest_sha256,
                entry.model_dump_json(),
                entry.reviewed_at,
            ),
        )

    @staticmethod
    def _update_proposal(db: sqlite3.Connection, proposal: SourceRefreshProposal) -> None:
        db.execute(
            """
            UPDATE source_refresh_proposals SET status = ?, payload_json = ?
            WHERE proposal_id = ?
            """,
            (proposal.status, proposal.model_dump_json(), proposal.proposal_id),
        )


def assess_source_refresh(
    store: SourceRefreshStore,
    current: SourceIdentityManifest,
    *,
    source_root: str | Path,
    project_root: str | Path,
    dirty_reason: str = "",
    observed_at: datetime | None = None,
) -> SourceRefreshAssessment:
    """Audit current identity and create a proposal only for a valid changed source."""
    project = Path(project_root).expanduser().resolve()
    audit = verify_source_identity(current, source_root, project)
    if audit.status == "valid":
        return SourceRefreshAssessment(status="unchanged", audit=audit)
    if audit.status == "invalid":
        return SourceRefreshAssessment(status="invalid", audit=audit)
    candidate = capture_source_identity(
        source_root,
        project / current.legacy_mapping.path,
        source_name=current.source_name,
        checkout_hint=current.checkout_hint,
        license_path=current.license.path,
        license_claim=current.license.claim,
        dirty_reason=dirty_reason,
        observed_at=observed_at,
    )
    candidate_audit = verify_source_identity(candidate, source_root, project)
    if candidate_audit.status != "valid":
        return SourceRefreshAssessment(status="invalid", audit=candidate_audit)
    changes = _identity_changes(current, candidate)
    if not changes:
        return SourceRefreshAssessment(status="unchanged", audit=candidate_audit)
    proposal = store.propose(
        current,
        candidate,
        source_root=source_root,
        project_root=project,
        changes=changes,
        created_at=observed_at,
    )
    return SourceRefreshAssessment(
        status="change_detected",
        audit=audit,
        proposal=proposal,
    )


def approve_source_refresh(
    store: SourceRefreshStore,
    proposal_id: str,
    *,
    manifest_path: str | Path,
    source_root: str | Path,
    project_root: str | Path,
    reviewed_by: str,
    review_reason: str,
    allow_license_change: bool = False,
    allow_mapping_change: bool = False,
    decided_at: datetime | None = None,
) -> SourceIdentityHistoryEntry:
    """Re-audit, approve, and materialize a candidate manifest."""
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise ValueError("refresh proposal 不存在。")
    target = Path(manifest_path).expanduser().resolve()
    current = load_source_identity(target)
    current_digest = manifest_sha256(current)
    if current_digest not in {
        proposal.base_manifest_sha256,
        proposal.candidate_manifest_sha256,
    }:
        raise ValueError("manifest 文件已被其他审批更新，拒绝覆盖。")
    _approved, entry = store.approve(
        proposal_id,
        source_root=source_root,
        project_root=project_root,
        reviewed_by=reviewed_by,
        review_reason=review_reason,
        allow_license_change=allow_license_change,
        allow_mapping_change=allow_mapping_change,
        decided_at=decided_at,
    )
    if current_digest != entry.manifest_sha256:
        write_source_identity(target, entry.manifest)
    return entry


def manifest_sha256(manifest: SourceIdentityManifest) -> str:
    return _sha256_json(manifest.model_dump(mode="json"))


def source_identity_sha256(manifest: SourceIdentityManifest) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("generated_at", None)
    return _sha256_json(payload)


def _identity_changes(
    current: SourceIdentityManifest,
    candidate: SourceIdentityManifest,
) -> tuple[str, ...]:
    changes: list[str] = []
    comparisons = (
        (current.git.commit, candidate.git.commit, "git.commit"),
        (current.git.remote, candidate.git.remote, "git.remote"),
        (current.git.branch, candidate.git.branch, "git.branch"),
        (current.git.upstream, candidate.git.upstream, "git.upstream"),
        (
            (current.git.ahead, current.git.behind),
            (candidate.git.ahead, candidate.git.behind),
            "git.ahead_behind",
        ),
        (
            (current.git.dirty, current.git.worktree_sha256),
            (candidate.git.dirty, candidate.git.worktree_sha256),
            "git.worktree",
        ),
        (current.license.sha256, candidate.license.sha256, "license.sha256"),
        (current.license.claim, candidate.license.claim, "license.claim"),
        (current.legacy_mapping.sha256, candidate.legacy_mapping.sha256, "mapping.sha256"),
    )
    changes.extend(label for before, after, label in comparisons if before != after)
    return tuple(sorted(changes))


def _build_proposal(
    *,
    current: SourceIdentityManifest,
    candidate: SourceIdentityManifest,
    base_entry_id: str,
    changes: tuple[str, ...],
    created_at: str,
) -> SourceRefreshProposal:
    base_digest = manifest_sha256(current)
    identity_digest = source_identity_sha256(candidate)
    return SourceRefreshProposal(
        proposal_id=_proposal_id(
            source_name=current.source_name,
            base_entry_id=base_entry_id,
            base_manifest_sha256=base_digest,
            candidate_identity_sha256=identity_digest,
            changes=changes,
        ),
        source_name=current.source_name,
        base_entry_id=base_entry_id,
        base_manifest_sha256=base_digest,
        candidate_manifest_sha256=manifest_sha256(candidate),
        candidate_identity_sha256=identity_digest,
        candidate=candidate,
        changes=changes,
        requires_license_review=any(item.startswith("license.") for item in changes),
        requires_mapping_review="mapping.sha256" in changes,
        created_at=created_at,
    )


def _build_history_entry(
    *,
    manifest: SourceIdentityManifest,
    revision: int,
    previous_entry_id: str,
    decision_kind: Literal["baseline_import", "refresh_approved"],
    reviewed_by: str,
    review_reason: str,
    license_change_acknowledged: bool,
    mapping_change_acknowledged: bool,
    reviewed_at: str,
) -> SourceIdentityHistoryEntry:
    values = {
        "schema_version": 1,
        "source_name": manifest.source_name,
        "revision": revision,
        "previous_entry_id": previous_entry_id,
        "manifest_sha256": manifest_sha256(manifest),
        "identity_sha256": source_identity_sha256(manifest),
        "manifest": manifest,
        "audit_status": "valid",
        "audit_findings": (),
        "decision_kind": decision_kind,
        "reviewed_by": reviewed_by,
        "review_reason": review_reason,
        "license_change_acknowledged": license_change_acknowledged,
        "mapping_change_acknowledged": mapping_change_acknowledged,
        "reviewed_at": reviewed_at,
    }
    return SourceIdentityHistoryEntry(
        entry_id=_sha256_json(_jsonable(values)),
        **values,
    )


def _history_entry_id(entry: SourceIdentityHistoryEntry) -> str:
    payload = entry.model_dump(mode="json")
    payload.pop("entry_id", None)
    return _sha256_json(payload)


def _proposal_id(
    *,
    source_name: str,
    base_entry_id: str,
    base_manifest_sha256: str,
    candidate_identity_sha256: str,
    changes: tuple[str, ...],
) -> str:
    return _sha256_json({
        "source_name": source_name,
        "base_entry_id": base_entry_id,
        "base_manifest_sha256": base_manifest_sha256,
        "candidate_identity_sha256": candidate_identity_sha256,
        "changes": list(changes),
    })


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return timestamp.isoformat()


def _aware_timestamp(value: str, field: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return value


def _safe_actor(value: str) -> str:
    actor = value.strip()
    if not _ACTOR_RE.fullmatch(actor):
        raise ValueError("reviewed_by 格式无效。")
    return actor


def _safe_review_reason(value: str) -> str:
    reason = value.strip()
    if not reason or len(reason) > 500:
        raise ValueError("review_reason 必须为 1 到 500 字符。")
    if any(character in reason for character in ("\x00", "\r")):
        raise ValueError("review_reason 含非法控制字符。")
    if _SECRET_MARKERS.search(reason):
        raise ValueError("review_reason 疑似包含 secret，拒绝持久化。")
    return reason


def _require_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("digest 必须是完整小写 SHA-256。")
    return value


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code source identity 刷新审批")
    parser.add_argument("action", choices=("bootstrap", "propose", "approve", "history"))
    parser.add_argument("--manifest", default="frontend/terminal-ui/cc-source-map.v2.json")
    parser.add_argument("--source", default="../claude-code")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--store", default=str(resolve_claude_source_db_path()))
    parser.add_argument("--reviewed-by", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--proposal-id", default="")
    parser.add_argument("--dirty-reason", default="")
    parser.add_argument("--allow-license-change", action="store_true")
    parser.add_argument("--allow-mapping-change", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    store = SourceRefreshStore(args.store)
    try:
        if args.action == "history":
            manifest = load_source_identity(args.manifest)
            payload = [item.model_dump(mode="json") for item in store.list_history(
                manifest.source_name,
                limit=args.limit,
            )]
        elif args.action == "bootstrap":
            manifest = load_source_identity(args.manifest)
            payload = store.bootstrap(
                manifest,
                source_root=args.source,
                project_root=args.project_root,
                reviewed_by=args.reviewed_by,
                review_reason=args.reason,
            ).model_dump(mode="json")
        elif args.action == "propose":
            manifest = load_source_identity(args.manifest)
            payload = assess_source_refresh(
                store,
                manifest,
                source_root=args.source,
                project_root=args.project_root,
                dirty_reason=args.dirty_reason,
            ).model_dump(mode="json")
        else:
            payload = approve_source_refresh(
                store,
                args.proposal_id,
                manifest_path=args.manifest,
                source_root=args.source,
                project_root=args.project_root,
                reviewed_by=args.reviewed_by,
                review_reason=args.reason,
                allow_license_change=args.allow_license_change,
                allow_mapping_change=args.allow_mapping_change,
            ).model_dump(mode="json")
    except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
