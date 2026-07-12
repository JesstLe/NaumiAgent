"""Durable execution records for streamed chat turns."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


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
    ) -> ChatRunRecord:
        now = _now_iso()
        record = ChatRunRecord(
            id=run_id or uuid.uuid4().hex[:12],
            session_id=session_id,
            user_message_id=user_message_id,
            status="running",
            started_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO chat_runs (
                    id, session_id, user_message_id, status, started_at, updated_at,
                    completed_at, assistant_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, '', '')
                """,
                (
                    record.id,
                    record.session_id,
                    record.user_message_id,
                    record.status,
                    record.started_at,
                    record.updated_at,
                ),
            )
            await db.commit()
        return record

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
                    metadata_json=excluded.metadata_json
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
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                UPDATE chat_runs
                SET status = ?, updated_at = ?, completed_at = ?, assistant_message_id = ?
                WHERE id = ?
                """,
                (status, now, now, assistant_message_id, run_id),
            )
            await db.commit()

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
                    assistant_message_id TEXT NOT NULL DEFAULT ''
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
                """
            )
            await db.commit()
            self._initialized = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
