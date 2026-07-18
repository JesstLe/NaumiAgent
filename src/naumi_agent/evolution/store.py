"""Versioned durable Store for workspace-isolated Evolution Candidate drafts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from naumi_agent.config.state_paths import resolve_naumi_state_home
from naumi_agent.evolution.candidate import EvolutionCandidateDraft, build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence

EVOLUTION_STORE_SCHEMA_VERSION = 1


def resolve_evolution_db_path() -> Path:
    """Return the user-owned Evolution DB path outside Agent-writable workspaces."""
    return resolve_naumi_state_home() / "evolution.db"


class EvolutionStoreError(RuntimeError):
    """Safe user-facing Evolution persistence failure."""


class EvolutionStoreConflictError(EvolutionStoreError):
    """An immutable identity or evidence digest conflicts with stored facts."""


class EvolutionStoreCorruptionError(EvolutionStoreError):
    """Stored JSON, projection columns, or digests no longer agree."""


@dataclass(frozen=True, slots=True)
class EvolutionStoredCandidate:
    workspace_root: str
    draft: EvolutionCandidateDraft
    revision: int
    draft_sha256: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EvolutionCandidateEvent:
    event_id: int
    workspace_root: str
    candidate_id: str
    revision: int
    event_type: str
    previous_sha256: str
    current_sha256: str
    added_evidence_ids: tuple[str, ...]
    occurred_at: str


class EvolutionCandidateStore:
    """Persist immutable observations and an auditable Candidate materialization."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def upsert_candidate(
        self,
        workspace_root: str | Path,
        draft: EvolutionCandidateDraft,
    ) -> EvolutionStoredCandidate:
        workspace = _canonical_workspace(workspace_root)
        canonical = build_candidate_draft(draft.evidence)
        if canonical != draft:
            raise EvolutionStoreConflictError(
                "Candidate Draft 不是当前 Evidence 的确定性 materialization。"
            )
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await self._candidate_row(db, workspace, draft.candidate_id)
                if existing is None:
                    await self._ensure_fingerprint_available(db, workspace, draft)
                    stored = await self._insert_candidate(db, workspace, draft)
                else:
                    current = await self._load_candidate(db, existing)
                    stored = await self._merge_candidate(db, current, draft)
                await db.commit()
                return stored
        except EvolutionStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError(
                "无法保存 Evolution Candidate。请检查用户状态目录与数据库完整性。"
            ) from exc

    async def get_candidate(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> EvolutionStoredCandidate | None:
        workspace = _canonical_workspace(workspace_root)
        normalized_id = _candidate_id(candidate_id)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                row = await self._candidate_row(db, workspace, normalized_id)
                return None if row is None else await self._load_candidate(db, row)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise EvolutionStoreError("无法读取 Evolution Candidate。") from exc
        except EvolutionStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError("无法读取 Evolution Candidate。") from exc

    async def list_candidates(
        self,
        workspace_root: str | Path,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionStoredCandidate, ...]:
        workspace = _canonical_workspace(workspace_root)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Candidate list limit 必须在 1..500。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM evolution_candidates
                    WHERE workspace_root = ?
                    ORDER BY last_observed_at DESC, candidate_id ASC
                    LIMIT ?
                    """,
                    (workspace, limit),
                )
                rows = await cursor.fetchall()
                return tuple([await self._load_candidate(db, row) for row in rows])
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise EvolutionStoreError("无法列出 Evolution Candidate。") from exc
        except EvolutionStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError("无法列出 Evolution Candidate。") from exc

    async def list_events(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> tuple[EvolutionCandidateEvent, ...]:
        workspace = _canonical_workspace(workspace_root)
        normalized_id = _candidate_id(candidate_id)
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                candidate_row = await self._candidate_row(db, workspace, normalized_id)
                if candidate_row is None:
                    return ()
                stored = await self._load_candidate(db, candidate_row)
                return await self._verified_events(
                    db,
                    workspace,
                    normalized_id,
                    expected_revision=stored.revision,
                    expected_sha256=stored.draft_sha256,
                    expected_evidence_ids={
                        item.evidence_id for item in stored.draft.evidence
                    },
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise EvolutionStoreError("无法读取 Candidate 审计事件。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError("无法读取 Candidate 审计事件。") from exc

    async def _insert_candidate(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        draft: EvolutionCandidateDraft,
    ) -> EvolutionStoredCandidate:
        now = _timestamp(self._clock())
        draft_json, draft_sha256 = _model_payload(draft)
        await db.execute(
            """
            INSERT INTO evolution_candidates (
                workspace_root, candidate_id, fingerprint, finding_code, kind,
                scope, draft_json, draft_sha256, occurrence_count,
                first_observed_at, last_observed_at, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                workspace,
                draft.candidate_id,
                draft.fingerprint,
                draft.finding_code,
                draft.kind,
                draft.scope,
                draft_json,
                draft_sha256,
                draft.occurrence_count,
                draft.first_observed_at,
                draft.last_observed_at,
                now,
                now,
            ),
        )
        await self._insert_evidence(db, workspace, draft.candidate_id, draft.evidence)
        await self._insert_event(
            db,
            workspace,
            draft.candidate_id,
            revision=1,
            event_type="created",
            previous_sha256="",
            current_sha256=draft_sha256,
            added_evidence_ids=tuple(item.evidence_id for item in draft.evidence),
            occurred_at=now,
        )
        return EvolutionStoredCandidate(
            workspace_root=workspace,
            draft=draft,
            revision=1,
            draft_sha256=draft_sha256,
            created_at=now,
            updated_at=now,
        )

    async def _merge_candidate(
        self,
        db: aiosqlite.Connection,
        current: EvolutionStoredCandidate,
        incoming: EvolutionCandidateDraft,
    ) -> EvolutionStoredCandidate:
        if current.draft.fingerprint != incoming.fingerprint:
            raise EvolutionStoreConflictError("Candidate id 对应的 fingerprint 发生冲突。")
        added = _new_evidence(current.draft.evidence, incoming.evidence)
        if not added:
            return current
        merged = build_candidate_draft((*current.draft.evidence, *added))
        merged_json, merged_sha256 = _model_payload(merged)
        revision = current.revision + 1
        now = _timestamp(self._clock())
        await self._insert_evidence(db, current.workspace_root, merged.candidate_id, added)
        await self._update_candidate_projection(
            db,
            current,
            merged,
            merged_json=merged_json,
            merged_sha256=merged_sha256,
            revision=revision,
            updated_at=now,
        )
        await self._insert_event(
            db,
            current.workspace_root,
            merged.candidate_id,
            revision=revision,
            event_type="evidence_merged",
            previous_sha256=current.draft_sha256,
            current_sha256=merged_sha256,
            added_evidence_ids=tuple(item.evidence_id for item in added),
            occurred_at=now,
        )
        return EvolutionStoredCandidate(
            workspace_root=current.workspace_root,
            draft=merged,
            revision=revision,
            draft_sha256=merged_sha256,
            created_at=current.created_at,
            updated_at=now,
        )

    async def _update_candidate_projection(
        self,
        db: aiosqlite.Connection,
        current: EvolutionStoredCandidate,
        merged: EvolutionCandidateDraft,
        *,
        merged_json: str,
        merged_sha256: str,
        revision: int,
        updated_at: str,
    ) -> None:
        cursor = await db.execute(
            """
            UPDATE evolution_candidates SET
                draft_json = ?, draft_sha256 = ?, occurrence_count = ?,
                first_observed_at = ?, last_observed_at = ?, revision = ?, updated_at = ?
            WHERE workspace_root = ? AND candidate_id = ? AND revision = ?
            """,
            (
                merged_json,
                merged_sha256,
                merged.occurrence_count,
                merged.first_observed_at,
                merged.last_observed_at,
                revision,
                updated_at,
                current.workspace_root,
                merged.candidate_id,
                current.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise EvolutionStoreConflictError(
                "Candidate revision 已变化，请重新合并本次 Evidence。"
            )

    async def _insert_evidence(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        candidate_id: str,
        evidence: tuple[EvolutionEvidence, ...],
    ) -> None:
        for item in evidence:
            payload, digest = _model_payload(item)
            try:
                await db.execute(
                    """
                    INSERT INTO evolution_candidate_evidence (
                        workspace_root, candidate_id, evidence_id, source_kind,
                        observed_at, evidence_json, evidence_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace,
                        candidate_id,
                        item.evidence_id,
                        item.source_kind,
                        item.observed_at,
                        payload,
                        digest,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                cursor = await db.execute(
                    """
                    SELECT candidate_id, evidence_json, evidence_sha256
                    FROM evolution_candidate_evidence
                    WHERE workspace_root = ? AND evidence_id = ?
                    """,
                    (workspace, item.evidence_id),
                )
                existing = await cursor.fetchone()
                if existing is None or (
                    str(existing["candidate_id"]) != candidate_id
                    or str(existing["evidence_json"]) != payload
                    or str(existing["evidence_sha256"]) != digest
                ):
                    raise EvolutionStoreConflictError(
                        f"Evidence {item.evidence_id} 已绑定到冲突内容或 Candidate。"
                    ) from exc

    async def _load_candidate(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> EvolutionStoredCandidate:
        draft_json = str(row["draft_json"])
        draft_sha256 = str(row["draft_sha256"])
        if _sha256(draft_json) != draft_sha256:
            raise EvolutionStoreCorruptionError("Candidate Draft 摘要不匹配。")
        try:
            draft = EvolutionCandidateDraft.model_validate_json(draft_json)
        except ValueError as exc:
            raise EvolutionStoreCorruptionError("Candidate Draft JSON 无效。") from exc
        projections = (
            str(row["candidate_id"]),
            str(row["fingerprint"]),
            str(row["finding_code"]),
            str(row["kind"]),
            str(row["scope"]),
            int(row["occurrence_count"]),
            str(row["first_observed_at"]),
            str(row["last_observed_at"]),
        )
        expected = (
            draft.candidate_id,
            draft.fingerprint,
            draft.finding_code,
            draft.kind,
            draft.scope,
            draft.occurrence_count,
            draft.first_observed_at,
            draft.last_observed_at,
        )
        if projections != expected:
            raise EvolutionStoreCorruptionError("Candidate projection 与 Draft 不一致。")
        await self._verify_evidence_rows(db, str(row["workspace_root"]), draft)
        await self._verified_events(
            db,
            str(row["workspace_root"]),
            draft.candidate_id,
            expected_revision=int(row["revision"]),
            expected_sha256=draft_sha256,
            expected_evidence_ids={item.evidence_id for item in draft.evidence},
        )
        return EvolutionStoredCandidate(
            workspace_root=str(row["workspace_root"]),
            draft=draft,
            revision=int(row["revision"]),
            draft_sha256=draft_sha256,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def _verify_evidence_rows(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        draft: EvolutionCandidateDraft,
    ) -> None:
        cursor = await db.execute(
            """
            SELECT evidence_id, candidate_id, evidence_json, evidence_sha256
            FROM evolution_candidate_evidence
            WHERE workspace_root = ? AND candidate_id = ?
            ORDER BY observed_at ASC, evidence_id ASC
            """,
            (workspace, draft.candidate_id),
        )
        rows = await cursor.fetchall()
        if len(rows) != len(draft.evidence):
            raise EvolutionStoreCorruptionError("Candidate Evidence 行数不一致。")
        for row, expected in zip(rows, draft.evidence, strict=True):
            payload = str(row["evidence_json"])
            digest = str(row["evidence_sha256"])
            try:
                restored = EvolutionEvidence.model_validate_json(payload)
            except ValueError as exc:
                raise EvolutionStoreCorruptionError("Evidence JSON 无效。") from exc
            if (
                _sha256(payload) != digest
                or str(row["candidate_id"]) != draft.candidate_id
                or str(row["evidence_id"]) != expected.evidence_id
                or restored != expected
            ):
                raise EvolutionStoreCorruptionError("Candidate Evidence 摘要或内容不一致。")

    async def _verified_events(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
        expected_evidence_ids: set[str],
    ) -> tuple[EvolutionCandidateEvent, ...]:
        cursor = await db.execute(
            """
            SELECT * FROM evolution_candidate_events
            WHERE workspace_root = ? AND candidate_id = ?
            ORDER BY revision ASC
            """,
            (workspace, candidate_id),
        )
        events = tuple(_event_from_row(row) for row in await cursor.fetchall())
        if len(events) != expected_revision:
            raise EvolutionStoreCorruptionError("Candidate event 修订数量不一致。")
        observed_ids: set[str] = set()
        previous_sha256 = ""
        for revision, event in enumerate(events, start=1):
            expected_type = "created" if revision == 1 else "evidence_merged"
            if (
                event.workspace_root != workspace
                or event.candidate_id != candidate_id
                or event.revision != revision
                or event.event_type != expected_type
                or event.previous_sha256 != previous_sha256
                or not event.added_evidence_ids
                or observed_ids.intersection(event.added_evidence_ids)
            ):
                raise EvolutionStoreCorruptionError("Candidate event 审计链不一致。")
            observed_ids.update(event.added_evidence_ids)
            previous_sha256 = event.current_sha256
        if previous_sha256 != expected_sha256 or observed_ids != expected_evidence_ids:
            raise EvolutionStoreCorruptionError("Candidate event 未覆盖当前 materialization。")
        return events

    async def _ensure_fingerprint_available(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        draft: EvolutionCandidateDraft,
    ) -> None:
        cursor = await db.execute(
            """
            SELECT candidate_id FROM evolution_candidates
            WHERE workspace_root = ? AND fingerprint = ?
            """,
            (workspace, draft.fingerprint),
        )
        row = await cursor.fetchone()
        if row is not None and str(row["candidate_id"]) != draft.candidate_id:
            raise EvolutionStoreConflictError("Candidate fingerprint 已绑定到其他 id。")

    async def _candidate_row(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        candidate_id: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            """
            SELECT * FROM evolution_candidates
            WHERE workspace_root = ? AND candidate_id = ?
            """,
            (workspace, candidate_id),
        )
        return await cursor.fetchone()

    async def _insert_event(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        candidate_id: str,
        *,
        revision: int,
        event_type: str,
        previous_sha256: str,
        current_sha256: str,
        added_evidence_ids: tuple[str, ...],
        occurred_at: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO evolution_candidate_events (
                workspace_root, candidate_id, revision, event_type,
                previous_sha256, current_sha256, added_evidence_ids_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace,
                candidate_id,
                revision,
                event_type,
                previous_sha256,
                current_sha256,
                json.dumps(added_evidence_ids, separators=(",", ":")),
                occurred_at,
            ),
        )

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _restrict_permissions(self._db_path.parent, 0o700)
                async with self._connection() as db:
                    cursor = await db.execute("PRAGMA user_version")
                    version = int((await cursor.fetchone())[0])
                    if version > EVOLUTION_STORE_SCHEMA_VERSION:
                        raise EvolutionStoreError(
                            "Evolution 数据库版本高于当前程序支持范围，请升级 NaumiAgent。"
                        )
                    await db.executescript(_SCHEMA_V1)
                    await db.execute(
                        f"PRAGMA user_version = {EVOLUTION_STORE_SCHEMA_VERSION}"
                    )
                    await db.commit()
                _restrict_permissions(self._db_path, 0o600)
            except EvolutionStoreError:
                raise
            except (aiosqlite.Error, OSError) as exc:
                raise EvolutionStoreError("无法初始化 Evolution Candidate Store。") from exc
            self._schema_ready = True

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


def _event_from_row(row: aiosqlite.Row) -> EvolutionCandidateEvent:
    try:
        decoded = json.loads(str(row["added_evidence_ids_json"]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise EvolutionStoreCorruptionError("Candidate event Evidence JSON 无效。") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise EvolutionStoreCorruptionError("Candidate event Evidence id 无效。")
    evidence_ids = tuple(decoded)
    previous_sha256 = str(row["previous_sha256"])
    current_sha256 = str(row["current_sha256"])
    event_type = str(row["event_type"])
    if event_type not in {"created", "evidence_merged"}:
        raise EvolutionStoreCorruptionError("Candidate event 类型无效。")
    if not re.fullmatch(r"[0-9a-f]{64}", current_sha256):
        raise EvolutionStoreCorruptionError("Candidate event 当前摘要无效。")
    if previous_sha256 and not re.fullmatch(r"[0-9a-f]{64}", previous_sha256):
        raise EvolutionStoreCorruptionError("Candidate event 前序摘要无效。")
    return EvolutionCandidateEvent(
        event_id=int(row["event_id"]),
        workspace_root=str(row["workspace_root"]),
        candidate_id=str(row["candidate_id"]),
        revision=int(row["revision"]),
        event_type=event_type,
        previous_sha256=previous_sha256,
        current_sha256=current_sha256,
        added_evidence_ids=evidence_ids,
        occurred_at=str(row["occurred_at"]),
    )


def _new_evidence(
    current: tuple[EvolutionEvidence, ...],
    incoming: tuple[EvolutionEvidence, ...],
) -> tuple[EvolutionEvidence, ...]:
    current_by_id = {item.evidence_id: item for item in current}
    for item in incoming:
        stored = current_by_id.get(item.evidence_id)
        if stored is not None and stored != item:
            raise EvolutionStoreConflictError(
                f"Evidence {item.evidence_id} 与已存不可变内容冲突。"
            )
    return tuple(item for item in incoming if item.evidence_id not in current_by_id)


def _canonical_workspace(workspace_root: str | Path) -> str:
    path = Path(workspace_root).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace_root 必须是已存在目录。")
    return str(path)


def _candidate_id(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"evc_[0-9a-f]{24}", normalized):
        raise ValueError("candidate_id 格式无效。")
    return normalized


def _model_payload(model: EvolutionCandidateDraft | EvolutionEvidence) -> tuple[str, str]:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, _sha256(payload)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evolution Store clock 必须返回带时区时间。")
    return value.isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name != "nt" and path.exists():
        path.chmod(mode)


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS evolution_candidates (
    workspace_root TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    finding_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    draft_sha256 TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 1),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, candidate_id),
    UNIQUE (workspace_root, fingerprint)
);

CREATE TABLE IF NOT EXISTS evolution_candidate_evidence (
    workspace_root TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    PRIMARY KEY (workspace_root, evidence_id),
    FOREIGN KEY (workspace_root, candidate_id)
        REFERENCES evolution_candidates (workspace_root, candidate_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evolution_evidence_candidate
ON evolution_candidate_evidence (workspace_root, candidate_id, observed_at, evidence_id);

CREATE TABLE IF NOT EXISTS evolution_candidate_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_root TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'evidence_merged')),
    previous_sha256 TEXT NOT NULL,
    current_sha256 TEXT NOT NULL,
    added_evidence_ids_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (workspace_root, candidate_id, revision),
    FOREIGN KEY (workspace_root, candidate_id)
        REFERENCES evolution_candidates (workspace_root, candidate_id)
        ON DELETE CASCADE
);
"""


__all__ = [
    "EVOLUTION_STORE_SCHEMA_VERSION",
    "EvolutionCandidateEvent",
    "EvolutionCandidateStore",
    "EvolutionStoreConflictError",
    "EvolutionStoreCorruptionError",
    "EvolutionStoreError",
    "EvolutionStoredCandidate",
    "resolve_evolution_db_path",
]
