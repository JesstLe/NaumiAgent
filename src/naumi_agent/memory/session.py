"""会话管理 — SQLite 持久化."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiosqlite

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.lifecycle import (
    SessionRetentionCandidate,
    SessionRetentionScan,
)

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """会话对象."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    model: str = "claude-sonnet-4-6"
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)
    archived_at: datetime | None = None
    status: str = "active"
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    workspace_root: str = ""
    git_branch: str = ""
    summary: str = ""

    def add_message(self, role: str, content: str, **metadata: Any) -> None:
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        msg.update(metadata)
        self.messages = [*self.messages, msg]  # immutable
        self.updated_at = datetime.now()

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "messages": json.dumps(self.messages, ensure_ascii=False),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "workspace_root": self.workspace_root,
            "git_branch": self.git_branch,
            "summary": self.summary,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Session:
        return cls(
            id=row["id"],
            title=row["title"],
            model=row["model"],
            messages=json.loads(row["messages"])
            if isinstance(row["messages"], str)
            else row["messages"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_accessed_at=datetime.fromisoformat(
                row.get("last_accessed_at") or row["updated_at"]
            ),
            archived_at=(
                datetime.fromisoformat(row["archived_at"])
                if row.get("archived_at")
                else None
            ),
            status=row["status"],
            total_tokens=row.get("total_tokens", 0),
            total_cost_usd=row.get("total_cost_usd", 0.0),
            workspace_root=row.get("workspace_root", ""),
            git_branch=row.get("git_branch", ""),
            summary=row.get("summary", ""),
        )


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    messages TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    archived_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    workspace_root TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT ''
)
"""

_UPSERT = """
INSERT INTO sessions
    (
        id, title, model, messages, created_at, updated_at, status, total_tokens,
        total_cost_usd, workspace_root, git_branch, summary,
        last_accessed_at, archived_at
    )
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    title = excluded.title,
    model = excluded.model,
    messages = excluded.messages,
    updated_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.updated_at ELSE excluded.updated_at END,
    status = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.status ELSE excluded.status END,
    total_tokens = excluded.total_tokens,
    total_cost_usd = excluded.total_cost_usd,
    workspace_root = excluded.workspace_root,
    git_branch = excluded.git_branch,
    summary = excluded.summary,
    last_accessed_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.last_accessed_at ELSE excluded.last_accessed_at END,
    archived_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.archived_at ELSE excluded.archived_at END
"""

_GET = "SELECT * FROM sessions WHERE id = ?"
_DELETE = "DELETE FROM sessions WHERE id = ?"
_ARCHIVE = """
UPDATE sessions
SET status = 'archived', updated_at = ?, archived_at = ?
WHERE id = ?
"""
_EXTRA_COLUMNS = {
    "workspace_root": "TEXT NOT NULL DEFAULT ''",
    "git_branch": "TEXT NOT NULL DEFAULT ''",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "last_accessed_at": "TEXT",
    "archived_at": "TEXT",
}

_PAYLOAD_BYTES_SQL = """
length(CAST(id AS BLOB)) + length(CAST(title AS BLOB))
+ length(CAST(model AS BLOB)) + length(CAST(messages AS BLOB))
+ length(CAST(created_at AS BLOB)) + length(CAST(updated_at AS BLOB))
+ length(CAST(COALESCE(last_accessed_at, '') AS BLOB))
+ length(CAST(COALESCE(archived_at, '') AS BLOB))
+ length(CAST(status AS BLOB)) + length(CAST(total_tokens AS BLOB))
+ length(CAST(total_cost_usd AS BLOB))
+ length(CAST(COALESCE(workspace_root, '') AS BLOB))
+ length(CAST(COALESCE(git_branch, '') AS BLOB))
+ length(CAST(COALESCE(summary, '') AS BLOB))
"""

_EFFECTIVE_ACCESS_SQL = """
CASE
    WHEN archived_at IS NOT NULL
         AND archived_at > COALESCE(last_accessed_at, updated_at)
    THEN archived_at
    ELSE COALESCE(last_accessed_at, updated_at)
END
"""


class SessionStore:
    """SQLite 会话存储."""

    def __init__(self, config: MemoryConfig) -> None:
        self._db_path = config.session_db_path
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute(_CREATE_TABLE)
            await self._ensure_schema(self._db)
            await self._db.commit()
        return self._db

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(sessions)")
        rows = await cursor.fetchall()
        existing = {row["name"] for row in rows}
        for column, ddl in _EXTRA_COLUMNS.items():
            if column not in existing:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")
        await db.execute(
            "UPDATE sessions SET last_accessed_at = updated_at "
            "WHERE last_accessed_at IS NULL OR last_accessed_at = ''"
        )
        await db.execute(
            "UPDATE sessions SET archived_at = updated_at "
            "WHERE status = 'archived' AND archived_at IS NULL"
        )

    async def create_session(
        self,
        title: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> Session:
        session = Session(
            title=title or "新会话",
            model=model or "kimi-for-coding",
        )
        if system_prompt:
            session.add_message("system", system_prompt)
        await self.save(session)
        return session

    async def save(self, session: Session) -> None:
        db = await self._get_db()
        if session.status == "active":
            timestamp = datetime.now()
            session.updated_at = timestamp
            session.last_accessed_at = timestamp
            session.archived_at = None
        row = session.to_row()
        await db.execute(
            _UPSERT,
            (
                row["id"],
                row["title"],
                row["model"],
                row["messages"],
                row["created_at"],
                row["updated_at"],
                row["status"],
                row["total_tokens"],
                row["total_cost_usd"],
                row["workspace_root"],
                row["git_branch"],
                row["summary"],
                row["last_accessed_at"],
                row["archived_at"],
            ),
        )
        await db.commit()

    async def load(self, session_id: str) -> Session | None:
        db = await self._get_db()
        cursor = await db.execute(_GET, (session_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return Session.from_row(dict(row))

    async def resume(
        self,
        session_id: str,
        *,
        accessed_at: datetime | None = None,
    ) -> Session | None:
        """Atomically reactivate a Session and record explicit user access."""
        db = await self._get_db()
        timestamp = (accessed_at or datetime.now()).isoformat()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(_GET, (session_id,))
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            await db.execute(
                """
                UPDATE sessions
                SET status = 'active', last_accessed_at = ?, archived_at = NULL
                WHERE id = ?
                """,
                (timestamp, session_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        data = dict(row)
        data["status"] = "active"
        data["last_accessed_at"] = timestamp
        data["archived_at"] = None
        return Session.from_row(data)

    async def scan_retention_candidates(
        self,
        *,
        limit: int = 10_000,
    ) -> SessionRetentionScan:
        """Read archived metadata without loading or decoding message JSON."""
        if not 1 <= limit <= 10_000:
            raise ValueError("保留候选扫描上限必须在 1 到 10000 之间。")
        db = await self._get_db()
        cursor = await db.execute(
            f"""
            WITH archived AS (
                SELECT id, title, status,
                       COALESCE(last_accessed_at, updated_at) AS last_accessed_at,
                       archived_at,
                       {_PAYLOAD_BYTES_SQL} AS payload_bytes,
                       {_EFFECTIVE_ACCESS_SQL} AS effective_access
                FROM sessions
                WHERE status = 'archived'
            )
            SELECT id, title, status, last_accessed_at, archived_at,
                   payload_bytes, effective_access,
                   COUNT(*) OVER () AS total_count,
                   SUM(payload_bytes) OVER () AS total_bytes
            FROM archived
            ORDER BY effective_access ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        candidates = tuple(
            SessionRetentionCandidate(
                session_id=str(row["id"]),
                title=str(row["title"]),
                status=str(row["status"]),
                last_accessed_at=datetime.fromisoformat(row["last_accessed_at"]),
                archived_at=(
                    datetime.fromisoformat(row["archived_at"])
                    if row["archived_at"]
                    else None
                ),
                payload_bytes=int(row["payload_bytes"] or 0),
            )
            for row in rows
        )
        total_archived_count = int(rows[0]["total_count"] or 0) if rows else 0
        total_archived_bytes = int(rows[0]["total_bytes"] or 0) if rows else 0
        return SessionRetentionScan(
            candidates=candidates,
            total_archived_count=total_archived_count,
            total_archived_bytes=total_archived_bytes,
        )

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
    ) -> tuple[list[Session], int]:
        db = await self._get_db()
        offset = (page - 1) * page_size
        normalized_query = query.strip()

        where = "status = 'active'"
        params: list[Any] = []
        if normalized_query:
            where += (
                " AND (id LIKE ? OR title LIKE ? OR model LIKE ? OR "
                "workspace_root LIKE ? OR git_branch LIKE ? OR summary LIKE ?)"
            )
            like = f"%{normalized_query}%"
            params.extend([like, like, like, like, like, like])

        cursor = await db.execute(f"SELECT COUNT(*) FROM sessions WHERE {where}", params)
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            f"SELECT * FROM sessions WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        rows = await cursor.fetchall()
        sessions = [Session.from_row(dict(r)) for r in rows]

        return sessions, total

    async def delete(self, session_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(_DELETE, (session_id,))
        await db.commit()
        return cursor.rowcount > 0

    async def archive(self, session_id: str) -> bool:
        db = await self._get_db()
        timestamp = datetime.now().isoformat()
        cursor = await db.execute(_ARCHIVE, (timestamp, timestamp, session_id))
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
