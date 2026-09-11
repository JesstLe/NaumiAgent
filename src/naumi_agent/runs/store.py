"""Durable execution records for streamed Agent turns."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from naumi_agent.harness.runtime_release_binding import HarnessRuntimeReleaseBinding
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.release_provenance import (
    RunReleaseProvenance,
    build_run_release_provenance,
)
from naumi_agent.runs.usage import RunUsage

logger = logging.getLogger(__name__)


class ChatRunStoreConflictError(RuntimeError):
    """Raised when a terminal run is asked to accept conflicting evidence."""


@dataclass(slots=True)
class ChatRunStepRecord:
    sequence: int
    stage: str
    status: str
    summary: str
    detail: str = ""
    event_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatArtifactRecord:
    id: str
    kind: str
    title: str
    summary: dict[str, Any]
    status: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatRunRecord:
    id: str
    session_id: str
    user_message_id: str
    status: str
    started_at: str
    updated_at: str
    completed_at: str = ""
    assistant_message_id: str = ""
    steps: list[ChatRunStepRecord] = field(default_factory=list)
    artifacts: list[ChatArtifactRecord] = field(default_factory=list)
    receipt: CompletionReceipt | None = None
    release_provenance: RunReleaseProvenance | None = None
    usage: RunUsage | None = None


@dataclass(slots=True)
class SourceReferenceRecord:
    id: str
    session_id: str
    kind: str
    title: str
    path: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatRunStore:
    """SQLite store with session isolation and idempotent step sequencing."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def start_run(
        self,
        *,
        session_id: str,
        user_message_id: str,
        run_id: str | None = None,
        release_binding: HarnessRuntimeReleaseBinding | None = None,
    ) -> ChatRunRecord:
        now = _now_iso()
        normalized_run_id = run_id or uuid.uuid4().hex[:12]
        provenance = (
            None
            if release_binding is None
            else build_run_release_provenance(
                workspace_root=release_binding.workspace_root,
                run_id=normalized_run_id,
                binding=release_binding,
                run_started_at=now,
            )
        )
        record = ChatRunRecord(
            id=normalized_run_id,
            session_id=session_id,
            user_message_id=user_message_id,
            status="running",
            started_at=now,
            updated_at=now,
            release_provenance=provenance,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO chat_runs (
                    id, session_id, user_message_id, status, started_at, updated_at,
                    completed_at, assistant_message_id, release_provenance_id,
                    release_provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.user_message_id,
                    record.status,
                    record.started_at,
                    record.updated_at,
                    "" if provenance is None else provenance.provenance_id,
                    "" if provenance is None else provenance.model_dump_json(),
                ),
            )
            await db.commit()
        return record

    async def add_source(
        self,
        *,
        session_id: str,
        kind: str,
        title: str,
        path: str,
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SourceReferenceRecord:
        source = SourceReferenceRecord(
            id=source_id or uuid.uuid4().hex[:12],
            session_id=session_id,
            kind=kind,
            title=title,
            path=path,
            created_at=_now_iso(),
            metadata=metadata or {},
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO chat_sources (
                    id, session_id, kind, title, path, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.session_id,
                    source.kind,
                    source.title,
                    source.path,
                    source.created_at,
                    json.dumps(source.metadata, ensure_ascii=False),
                ),
            )
            await db.commit()
        return source

    async def list_sources(self, session_id: str) -> list[SourceReferenceRecord]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM chat_sources
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [
                SourceReferenceRecord(
                    id=row["id"],
                    session_id=row["session_id"],
                    kind=row["kind"],
                    title=row["title"],
                    path=row["path"],
                    created_at=row["created_at"],
                    metadata=json.loads(row["metadata_json"]),
                )
                for row in rows
            ]

    async def append_step(
        self,
        run_id: str,
        *,
        sequence: int,
        stage: str,
        status: str,
        summary: str,
        detail: str = "",
        event_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now_iso()
        completed_at = now if status in {"completed", "failed", "cancelled"} else ""
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO chat_run_steps (
                    run_id, sequence, stage, status, summary, detail, event_id,
                    started_at, completed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sequence) DO UPDATE SET
                    stage=excluded.stage,
                    status=excluded.status,
                    summary=excluded.summary,
                    detail=excluded.detail,
                    event_id=excluded.event_id,
                    completed_at=excluded.completed_at,
                    metadata_json=json_patch(chat_run_steps.metadata_json, excluded.metadata_json)
                """,
                (
                    run_id,
                    sequence,
                    stage,
                    status,
                    summary,
                    detail,
                    event_id,
                    now,
                    completed_at,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            await db.execute(
                "UPDATE chat_runs SET updated_at = ? WHERE id = ?",
                (now, run_id),
            )
            await db.commit()

    async def append_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        title: str,
        summary: dict[str, Any],
        status: str,
        artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatArtifactRecord:
        artifact = ChatArtifactRecord(
            id=artifact_id or uuid.uuid4().hex[:12],
            kind=kind,
            title=title,
            summary=summary,
            status=status,
            created_at=_now_iso(),
            metadata=metadata or {},
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO chat_run_artifacts (
                    id, run_id, kind, title, summary_json, status, created_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    summary_json=excluded.summary_json,
                    status=excluded.status,
                    metadata_json=excluded.metadata_json
                """,
                (
                    artifact.id,
                    run_id,
                    artifact.kind,
                    artifact.title,
                    json.dumps(artifact.summary, ensure_ascii=False),
                    artifact.status,
                    artifact.created_at,
                    json.dumps(artifact.metadata, ensure_ascii=False),
                ),
            )
            await db.commit()
        return artifact

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        assistant_message_id: str = "",
        receipt: CompletionReceipt | None = None,
        usage: RunUsage | None = None,
    ) -> None:
        normalized_receipt = (
            CompletionReceipt.from_dict(receipt.to_dict())
            if receipt is not None
            else None
        )
        if normalized_receipt is not None and normalized_receipt.run_id != run_id:
            raise ValueError("completion receipt run_id does not match the finished run")
        now = _now_iso()
        receipt_json = (
            json.dumps(
                normalized_receipt.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if normalized_receipt is not None
            else ""
        )
        receipt_id = (
            normalized_receipt.receipt_id if normalized_receipt is not None else ""
        )
        normalized_usage = _validated_usage(usage, run_id=run_id)
        usage_id = "" if normalized_usage is None else normalized_usage.usage_id
        usage_json = (
            "" if normalized_usage is None else normalized_usage.model_dump_json()
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT status, assistant_message_id, receipt_json, usage_json "
                    "FROM chat_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if existing is None:
                await db.rollback()
                return
            if existing["status"] != "running":
                if existing["status"] != status:
                    await db.rollback()
                    raise ChatRunStoreConflictError(
                        "Chat run 终态不能改写为其他状态。"
                    )
                if (
                    receipt_json
                    and existing["receipt_json"]
                    and existing["receipt_json"] != receipt_json
                ):
                    await db.rollback()
                    raise ChatRunStoreConflictError(
                        "Chat run 已绑定不同 Completion Receipt。"
                    )
                if (
                    usage_json
                    and existing["usage_json"]
                    and existing["usage_json"] != usage_json
                ):
                    await db.rollback()
                    raise ChatRunStoreConflictError(
                        "Chat run 已绑定不同 Run Usage。"
                    )
                await db.execute(
                    """
                    UPDATE chat_runs
                    SET assistant_message_id = CASE
                            WHEN assistant_message_id = '' THEN ?
                            ELSE assistant_message_id
                        END,
                        receipt_id = CASE
                            WHEN receipt_json = '' THEN ? ELSE receipt_id
                        END,
                        receipt_json = CASE
                            WHEN receipt_json = '' THEN ? ELSE receipt_json
                        END,
                        usage_id = CASE
                            WHEN usage_json = '' THEN ? ELSE usage_id
                        END,
                        usage_json = CASE
                            WHEN usage_json = '' THEN ? ELSE usage_json
                        END
                    WHERE id = ?
                    """,
                    (
                        assistant_message_id,
                        receipt_id,
                        receipt_json,
                        usage_id,
                        usage_json,
                        run_id,
                    ),
                )
                await db.commit()
                return
            await db.execute(
                """
                UPDATE chat_runs
                SET status = ?, updated_at = ?, completed_at = ?, assistant_message_id = ?,
                    receipt_id = ?, receipt_json = ?, usage_id = ?, usage_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    now,
                    assistant_message_id,
                    receipt_id,
                    receipt_json,
                    usage_id,
                    usage_json,
                    run_id,
                ),
            )
            await db.execute(
                """
                UPDATE chat_run_steps
                SET status = CASE
                    WHEN stage IN ('analysis', 'response')
                        AND ? IN ('success', 'completed') THEN 'completed'
                    WHEN ? = 'cancelled' THEN 'cancelled'
                    ELSE 'interrupted' END,
                    completed_at = ?
                WHERE run_id = ? AND status IN ('running', 'awaiting_approval')
                """,
                (status, status, now, run_id),
            )
            await db.commit()

    async def get_receipt(
        self,
        session_id: str,
        receipt_id: str,
    ) -> CompletionReceipt | None:
        """Load one receipt without allowing cross-session access."""
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT receipt_json FROM chat_runs
                WHERE session_id = ? AND receipt_id = ?
                LIMIT 1
                """,
                (session_id, receipt_id),
            )
            row = await cursor.fetchone()
            return _receipt_from_json(row["receipt_json"]) if row is not None else None

    async def get_run(self, session_id: str, run_id: str) -> ChatRunRecord | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM chat_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return await self._record_from_row(db, row)

    async def list_runs(self, session_id: str, *, limit: int = 50) -> list[ChatRunRecord]:
        safe_limit = max(1, min(limit, 200))
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM chat_runs
                WHERE session_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, safe_limit),
            )
            rows = await cursor.fetchall()
            return [await self._record_from_row(db, row) for row in rows]

    async def _record_from_row(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> ChatRunRecord:
        step_cursor = await db.execute(
            "SELECT * FROM chat_run_steps WHERE run_id = ? ORDER BY sequence ASC",
            (row["id"],),
        )
        artifact_cursor = await db.execute(
            "SELECT * FROM chat_run_artifacts WHERE run_id = ? ORDER BY created_at, id",
            (row["id"],),
        )
        steps = await step_cursor.fetchall()
        artifacts = await artifact_cursor.fetchall()
        return ChatRunRecord(
            id=row["id"],
            session_id=row["session_id"],
            user_message_id=row["user_message_id"],
            status=row["status"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            assistant_message_id=row["assistant_message_id"],
            receipt=_receipt_from_json(row["receipt_json"]),
            release_provenance=_release_provenance_from_json(
                row["release_provenance_json"],
                expected_id=row["release_provenance_id"],
            ),
            usage=_usage_from_json(row["usage_json"], expected_id=row["usage_id"]),
            steps=[
                ChatRunStepRecord(
                    sequence=step["sequence"],
                    stage=step["stage"],
                    status=step["status"],
                    summary=step["summary"],
                    detail=step["detail"],
                    event_id=step["event_id"],
                    started_at=step["started_at"],
                    completed_at=step["completed_at"],
                    metadata=json.loads(step["metadata_json"]),
                )
                for step in steps
            ],
            artifacts=[
                ChatArtifactRecord(
                    id=artifact["id"],
                    kind=artifact["kind"],
                    title=artifact["title"],
                    summary=json.loads(artifact["summary_json"]),
                    status=artifact["status"],
                    created_at=artifact["created_at"],
                    metadata=json.loads(artifact["metadata_json"]),
                )
                for artifact in artifacts
            ],
        )

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS chat_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    assistant_message_id TEXT NOT NULL DEFAULT '',
                    receipt_id TEXT NOT NULL DEFAULT '',
                    receipt_json TEXT NOT NULL DEFAULT '',
                    release_provenance_id TEXT NOT NULL DEFAULT '',
                    release_provenance_json TEXT NOT NULL DEFAULT '',
                    usage_id TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_chat_runs_session_started
                    ON chat_runs(session_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS chat_run_steps (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES chat_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chat_run_artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(run_id) REFERENCES chat_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chat_sources (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sources_session_created
                    ON chat_sources(session_id, created_at DESC);
                """
            )
            cursor = await db.execute("PRAGMA table_info(chat_runs)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "receipt_id" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN receipt_id TEXT NOT NULL DEFAULT ''"
                )
            if "receipt_json" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN receipt_json TEXT NOT NULL DEFAULT ''"
                )
            if "release_provenance_id" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN "
                    "release_provenance_id TEXT NOT NULL DEFAULT ''"
                )
            if "release_provenance_json" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN "
                    "release_provenance_json TEXT NOT NULL DEFAULT ''"
                )
            if "usage_id" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN usage_id TEXT NOT NULL DEFAULT ''"
                )
            if "usage_json" not in columns:
                await db.execute(
                    "ALTER TABLE chat_runs ADD COLUMN usage_json TEXT NOT NULL DEFAULT ''"
                )
            await self._backfill_receipt_ids(db)
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_runs_session_receipt
                    ON chat_runs(session_id, receipt_id)
                """
            )
            await db.commit()
            self._initialized = True

    async def _backfill_receipt_ids(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute(
            """
            SELECT id, receipt_json FROM chat_runs
            WHERE receipt_id = '' AND receipt_json <> ''
            """
        )
        for row in await cursor.fetchall():
            receipt = _receipt_from_json(row[1])
            if receipt is None:
                continue
            await db.execute(
                "UPDATE chat_runs SET receipt_id = ? WHERE id = ?",
                (receipt.receipt_id, row[0]),
            )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _receipt_from_json(value: str) -> CompletionReceipt | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
        return CompletionReceipt.from_dict(decoded)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid completion receipt JSON: %s", exc)
        return None


def _release_provenance_from_json(
    value: str,
    *,
    expected_id: str,
) -> RunReleaseProvenance | None:
    if not value:
        return None
    try:
        provenance = RunReleaseProvenance.model_validate_json(value)
        if not expected_id or provenance.provenance_id != expected_id:
            raise ValueError("Run Release Provenance ID 与 payload 不一致。")
        return provenance
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid run release provenance JSON: %s", exc)
        return None


def _validated_usage(value: RunUsage | None, *, run_id: str) -> RunUsage | None:
    if value is None:
        return None
    try:
        usage = RunUsage.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Run Usage artifact 无效。") from exc
    if usage.run_id != run_id:
        raise ValueError("Run Usage 与 chat run 不一致。")
    return usage


def _usage_from_json(value: str, *, expected_id: str) -> RunUsage | None:
    if not value:
        return None
    try:
        usage = RunUsage.model_validate_json(value)
        if not expected_id or usage.usage_id != expected_id:
            raise ValueError("Run Usage ID 与 payload 不一致。")
        return usage
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid run usage JSON: %s", exc)
        return None


__all__ = [
    "ChatArtifactRecord",
    "ChatRunRecord",
    "ChatRunStepRecord",
    "ChatRunStore",
    "ChatRunStoreConflictError",
    "SourceReferenceRecord",
]
