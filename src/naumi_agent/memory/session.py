"""会话管理 — SQLite 持久化."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Any

import aiosqlite

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.lifecycle import (
    SessionRetentionCandidate,
    SessionRetentionScan,
)

logger = logging.getLogger(__name__)


def _serialized(operation):
    @wraps(operation)
    async def wrapped(self, *args, **kwargs):
        async with self._operation_lock:
            try:
                return await operation(self, *args, **kwargs)
            except BaseException:
                if self._db is not None and self._db.in_transaction:
                    await self._db.rollback()
                raise

    return wrapped


def _delivery_json(value: Any) -> str:
    def visit(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("团队消息资料嵌套过深。")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if any(ord(c) < 32 and c not in "\n\r\t" for c in item):
                raise ValueError("团队消息包含无效控制字符。")
            item.encode("utf-8")
            return
        if isinstance(item, int) or isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise ValueError("团队消息必须使用有效 JSON 资料。")

    try:
        visit(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 8 * 1024**2:
            raise ValueError("单次团队消息资料最多允许 8 MB。")
        return encoded
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("团队消息编码不正确。") from exc


def _delivery_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value):
        raise ValueError("团队消息或交付编号格式不正确。")
    return value


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
    pinned_at: datetime | None = None
    status: str = "active"
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    workspace_root: str = ""
    git_branch: str = ""
    summary: str = ""
    engine: str = "naumi"
    personal_agent_id: str = ""
    personal_execution_id: str = ""
    personal_team_id: str = ""

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
            "pinned_at": self.pinned_at.isoformat() if self.pinned_at else None,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "workspace_root": self.workspace_root,
            "git_branch": self.git_branch,
            "summary": self.summary,
            "engine": self.engine,
            "personal_agent_id": self.personal_agent_id,
            "personal_execution_id": self.personal_execution_id,
            "personal_team_id": self.personal_team_id,
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
                datetime.fromisoformat(row["archived_at"]) if row.get("archived_at") else None
            ),
            pinned_at=(datetime.fromisoformat(row["pinned_at"]) if row.get("pinned_at") else None),
            status=row["status"],
            total_tokens=row.get("total_tokens", 0),
            total_cost_usd=row.get("total_cost_usd", 0.0),
            workspace_root=row.get("workspace_root", ""),
            git_branch=row.get("git_branch", ""),
            summary=row.get("summary", ""),
            engine=row.get("engine") or "naumi",
            personal_agent_id=row.get("personal_agent_id") or "",
            personal_execution_id=row.get("personal_execution_id") or "",
            personal_team_id=row.get("personal_team_id") or "",
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
    pinned_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    workspace_root TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT 'naumi',
    personal_agent_id TEXT NOT NULL DEFAULT '',
    personal_execution_id TEXT NOT NULL DEFAULT '',
    personal_team_id TEXT NOT NULL DEFAULT ''
)
"""

_UPSERT = """
INSERT INTO sessions
    (
        id, title, model, messages, created_at, updated_at, status, total_tokens,
        total_cost_usd, workspace_root, git_branch, summary, engine,
        last_accessed_at, archived_at, pinned_at, personal_agent_id, personal_execution_id,
        personal_team_id
    )
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    title = excluded.title,
    model = excluded.model,
    messages = CASE WHEN sessions.personal_team_id <> ''
        THEN sessions.messages ELSE excluded.messages END,
    updated_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.updated_at ELSE excluded.updated_at END,
    status = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.status ELSE excluded.status END,
    total_tokens = CASE WHEN sessions.personal_team_id <> ''
        THEN sessions.total_tokens ELSE excluded.total_tokens END,
    total_cost_usd = CASE WHEN sessions.personal_team_id <> ''
        THEN sessions.total_cost_usd ELSE excluded.total_cost_usd END,
    workspace_root = CASE WHEN sessions.personal_team_id <> ''
        THEN sessions.workspace_root ELSE excluded.workspace_root END,
    git_branch = excluded.git_branch,
    summary = excluded.summary,
    engine = CASE WHEN sessions.personal_team_id <> ''
        THEN sessions.engine ELSE excluded.engine END,
    last_accessed_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.last_accessed_at ELSE excluded.last_accessed_at END,
    archived_at = CASE
        WHEN sessions.status = 'archived' AND excluded.status = 'active'
        THEN sessions.archived_at ELSE excluded.archived_at END,
    pinned_at = excluded.pinned_at,
    personal_agent_id = CASE
        WHEN sessions.personal_agent_id <> '' THEN sessions.personal_agent_id
        ELSE excluded.personal_agent_id END,
    personal_execution_id = CASE
        WHEN sessions.personal_team_id <> '' THEN ''
        WHEN sessions.personal_execution_id <> '' THEN sessions.personal_execution_id
        ELSE excluded.personal_execution_id END,
    personal_team_id = CASE
        WHEN sessions.personal_team_id <> '' THEN sessions.personal_team_id
        ELSE excluded.personal_team_id END
WHERE sessions.personal_team_id <> '' OR excluded.personal_team_id = ''
    OR (sessions.personal_execution_id = '' AND
        excluded.personal_team_id = CASE WHEN sessions.personal_agent_id <> ''
            THEN sessions.personal_agent_id ELSE excluded.personal_agent_id END)
RETURNING personal_agent_id, personal_execution_id, personal_team_id,
          messages, total_tokens, total_cost_usd, engine, workspace_root,
          status, archived_at, updated_at, last_accessed_at
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
    "engine": "TEXT NOT NULL DEFAULT 'naumi'",
    "last_accessed_at": "TEXT",
    "archived_at": "TEXT",
    "pinned_at": "TEXT",
    "personal_agent_id": "TEXT NOT NULL DEFAULT ''",
    "personal_execution_id": "TEXT NOT NULL DEFAULT ''",
    "personal_team_id": "TEXT NOT NULL DEFAULT ''",
}

_PAYLOAD_BYTES_SQL = """
length(CAST(id AS BLOB)) + length(CAST(title AS BLOB))
+ length(CAST(model AS BLOB)) + length(CAST(messages AS BLOB))
+ length(CAST(created_at AS BLOB)) + length(CAST(updated_at AS BLOB))
+ length(CAST(COALESCE(last_accessed_at, '') AS BLOB))
+ length(CAST(COALESCE(archived_at, '') AS BLOB))
+ length(CAST(COALESCE(pinned_at, '') AS BLOB))
+ length(CAST(status AS BLOB)) + length(CAST(total_tokens AS BLOB))
+ length(CAST(total_cost_usd AS BLOB))
+ length(CAST(COALESCE(workspace_root, '') AS BLOB))
+ length(CAST(COALESCE(git_branch, '') AS BLOB))
+ length(CAST(COALESCE(summary, '') AS BLOB))
+ length(CAST(COALESCE(personal_agent_id, '') AS BLOB))
+ length(CAST(COALESCE(personal_execution_id, '') AS BLOB))
+ length(CAST(COALESCE(personal_team_id, '') AS BLOB))
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
        self._initialize_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()

    async def _get_db(self) -> aiosqlite.Connection:
        async with self._initialize_lock:
            if self._db is None:
                os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
                db = await aiosqlite.connect(self._db_path)
                try:
                    db.row_factory = aiosqlite.Row
                    # Serialize additive migrations across independently opened stores.
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute(_CREATE_TABLE)
                    await self._ensure_schema(db)
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    await db.close()
                    raise
                self._db = db
        return self._db

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(sessions)")
        rows = await cursor.fetchall()
        existing = {row["name"] for row in rows}
        for column, ddl in _EXTRA_COLUMNS.items():
            if column not in existing:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")
        await db.execute("""CREATE TABLE IF NOT EXISTS personal_session_deliveries (
            session_id TEXT NOT NULL, delivery_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL, message_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (session_id, delivery_id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS deleted_personal_team_sessions (
            session_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, deleted_at TEXT NOT NULL)""")
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
        workspace_root: str | None = None,
        git_branch: str | None = None,
        engine: str | None = None,
        personal_agent_id: str = "",
        personal_execution_id: str = "",
        personal_team_id: str = "",
    ) -> Session:
        session = Session(
            title=title or "新会话",
            model=model or "kimi-for-coding",
            workspace_root=str(workspace_root or "").strip(),
            git_branch=str(git_branch or "").strip(),
            engine=engine or "naumi",
            personal_agent_id=personal_agent_id,
            personal_execution_id=personal_execution_id,
            personal_team_id=personal_team_id,
        )
        if system_prompt:
            session.add_message("system", system_prompt)
        await self.save(session)
        return session

    @_serialized
    async def save(self, session: Session) -> None:
        marker = session.personal_agent_id
        if (
            not isinstance(marker, str)
            or len(marker) > 128
            or any(ord(character) < 33 or ord(character) == 127 for character in marker)
        ):
            raise ValueError("Agent 身份标记必须是无空白的字符串，最多 128 字符。")
        marker = session.personal_execution_id
        if (
            not isinstance(marker, str)
            or len(marker) > 128
            or any(ord(character) < 33 or ord(character) == 127 for character in marker)
        ):
            raise ValueError("会话执行身份标记必须是无空白的字符串，最多 128 字符。")
        marker = session.personal_team_id
        if (
            not isinstance(marker, str)
            or len(marker) > 128
            or any(ord(c) < 33 or ord(c) == 127 for c in marker)
            or (
                marker
                and (
                    marker != session.personal_agent_id
                    or session.personal_execution_id
                    or session.engine != "naumi"
                )
            )
        ):
            raise ValueError("团队会话身份必须匹配 Agent 身份，且不能是执行会话或其他引擎。")
        db = await self._get_db()
        if session.status == "active":
            timestamp = datetime.now()
            session.updated_at = timestamp
            session.last_accessed_at = timestamp
            session.archived_at = None
        row = session.to_row()
        await db.execute("BEGIN IMMEDIATE")
        try:
            deleted = await (
                await db.execute(
                    "SELECT 1 FROM deleted_personal_team_sessions WHERE session_id=?", (session.id,)
                )
            ).fetchone()
            if deleted:
                raise ValueError("团队对话已删除，不重新创建旧会话。")
            cursor = await db.execute(
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
                    row["engine"],
                    row["last_accessed_at"],
                    row["archived_at"],
                    row["pinned_at"],
                    row["personal_agent_id"],
                    row["personal_execution_id"],
                    row["personal_team_id"],
                ),
            )
            # Ownership is sticky even when a stale caller saves an unmarked object.
            saved = await cursor.fetchone()
            if saved is None:
                raise ValueError("团队对话归属与原会话不一致。")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        if saved is not None:
            session.personal_agent_id = saved["personal_agent_id"]
            session.personal_execution_id = saved["personal_execution_id"]
            session.personal_team_id = saved["personal_team_id"]
            if session.personal_team_id:
                session.messages = json.loads(saved["messages"])
                session.total_tokens = saved["total_tokens"]
                session.total_cost_usd = saved["total_cost_usd"]
                session.engine = saved["engine"]
                session.workspace_root = saved["workspace_root"]
                session.status = saved["status"]
                session.archived_at = (
                    datetime.fromisoformat(saved["archived_at"]) if saved["archived_at"] else None
                )
                session.updated_at = datetime.fromisoformat(saved["updated_at"])
                session.last_accessed_at = datetime.fromisoformat(
                    saved["last_accessed_at"] or saved["updated_at"]
                )

    @_serialized
    async def bind_personal_execution(
        self, session_id: str, expected_agent_id: str, execution_id: str
    ) -> None:
        """Mark legacy execution history without racing or rewriting its messages."""
        if (
            not isinstance(execution_id, str)
            or not execution_id
            or len(execution_id) > 128
            or any(ord(character) < 33 or ord(character) == 127 for character in execution_id)
        ):
            raise ValueError("会话执行编号不正确。")
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE sessions SET personal_execution_id=? WHERE id=? AND personal_agent_id=? "
            "AND personal_team_id='' AND personal_execution_id IN ('',?) "
            "RETURNING personal_execution_id",
            (execution_id, session_id, expected_agent_id, execution_id),
        )
        result = await cursor.fetchone()
        await db.commit()
        if result is None:
            raise ValueError("会话归属或执行编号不一致，未修改历史内容。")

    @_serialized
    async def bind_personal_team(self, session_id: str, expected_agent_id: str) -> None:
        """Upgrade a legacy team conversation without saving a stale message list."""
        _delivery_id(session_id)
        if (
            not isinstance(expected_agent_id, str)
            or re.fullmatch(r"pa_[a-f0-9]{32}", expected_agent_id) is None
        ):
            raise ValueError("团队 Agent 编号格式不正确。")
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE sessions SET personal_team_id=? WHERE id=? AND personal_agent_id=? "
            "AND personal_execution_id='' AND personal_team_id IN ('',?) "
            "AND engine='naumi' AND status='active' AND archived_at IS NULL "
            "RETURNING personal_team_id",
            (expected_agent_id, session_id, expected_agent_id, expected_agent_id),
        )
        row = await cursor.fetchone()
        await db.commit()
        if row is None:
            raise ValueError("团队会话归属或状态不一致，未修改历史内容。")

    @_serialized
    async def append_personal_messages_once(
        self,
        session_id: str,
        *,
        expected_agent_id: str,
        delivery_id: str,
        messages: list[dict],
        usage_tokens: int = 0,
        usage_cost_usd: float = 0.0,
    ) -> list[str]:
        """Append a durable team delivery without overwriting a newer conversation."""
        _delivery_id(session_id)
        _delivery_id(delivery_id)
        if (
            not isinstance(expected_agent_id, str)
            or re.fullmatch(r"pa_[a-f0-9]{32}", expected_agent_id) is None
        ):
            raise ValueError("团队 Agent 编号格式不正确。")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 128:
            raise ValueError("每次交付需要 1 至 128 条团队消息。")
        identities = []
        for message in messages:
            if (
                not isinstance(message, dict)
                or set(message) != {"id", "role", "content", "metadata"}
                or not isinstance(message["role"], str)
                or message["role"] not in {"user", "assistant"}
                or not isinstance(message["content"], str)
                or not message["content"].strip()
                or not isinstance(message["metadata"], dict)
            ):
                raise ValueError("团队消息需要编号、用户或 Agent 角色、正文与资料字段。")
            identities.append(_delivery_id(message["id"]))
        if len(set(identities)) != len(identities):
            raise ValueError("团队消息编号重复。")
        if (
            isinstance(usage_tokens, bool)
            or not isinstance(usage_tokens, int)
            or not 0 <= usage_tokens <= 2**63 - 1
        ):
            raise ValueError("团队消息用量必须是非负整数。")
        if (
            isinstance(usage_cost_usd, bool)
            or not isinstance(usage_cost_usd, (int, float))
            or not math.isfinite(usage_cost_usd)
            or not 0 <= usage_cost_usd <= 1e12
        ):
            raise ValueError("团队消息费用必须是有效非负数值。")
        payload = _delivery_json(
            {
                "session_id": session_id,
                "agent_id": expected_agent_id,
                "delivery_id": delivery_id,
                "messages": messages,
                "usage_tokens": usage_tokens,
                "usage_cost_usd": float(usage_cost_usd),
            }
        )
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        # Detach caller-owned mutable dictionaries before crossing an await.
        normalized = json.loads(payload)["messages"]
        db = await self._get_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await (await db.execute(_GET, (session_id,))).fetchone()
            if (
                row is None
                or row["personal_team_id"] != expected_agent_id
                or row["personal_agent_id"] != expected_agent_id
                or row["personal_execution_id"]
                or row["engine"] != "naumi"
            ):
                raise ValueError("团队对话不存在或归属不一致，未写入消息。")
            previous = await (
                await db.execute(
                    "SELECT * FROM personal_session_deliveries "
                    "WHERE session_id=? AND delivery_id=?",
                    (session_id, delivery_id),
                )
            ).fetchone()
            history = json.loads(row["messages"])
            if not isinstance(history, list):
                raise ValueError("团队对话记录不完整，请恢复资料库。")
            if previous is not None:
                if (
                    previous["agent_id"] != expected_agent_id
                    or previous["payload_sha256"] != payload_hash
                ):
                    raise ValueError("该交付编号已有不同内容，请保留原始交付。")
                if json.loads(previous["message_ids_json"]) != identities:
                    raise ValueError("团队交付回执不完整。")
                known = {
                    message.get("id"): message for message in history if isinstance(message, dict)
                }
                for message in normalized:
                    actual = known.get(message["id"])
                    if actual is None or any(
                        actual.get(key) != value for key, value in message.items()
                    ):
                        raise ValueError("团队交付记录与消息正文不一致。")
                await db.commit()
                return list(identities)
            if row["status"] != "active" or row["archived_at"] is not None:
                raise ValueError("团队对话已归档，未追加新消息。")
            if set(identities) & {m.get("id") for m in history if isinstance(m, dict)}:
                raise ValueError("消息编号已用于其他交付，未重复追加。")
            tokens = row["total_tokens"] + usage_tokens
            cost = row["total_cost_usd"] + float(usage_cost_usd)
            if tokens > 2**63 - 1 or not math.isfinite(cost):
                raise ValueError("团队累计用量超出可保存范围。")
            timestamp = datetime.now().isoformat()
            for message in normalized:
                message["timestamp"] = timestamp
            history.extend(normalized)
            await db.execute(
                "UPDATE sessions SET messages=?, total_tokens=?, total_cost_usd=?, updated_at=? "
                "WHERE id=?",
                (json.dumps(history, ensure_ascii=False), tokens, cost, timestamp, session_id),
            )
            await db.execute(
                "INSERT INTO personal_session_deliveries VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    delivery_id,
                    expected_agent_id,
                    payload_hash,
                    json.dumps(identities),
                    timestamp,
                ),
            )
            await db.commit()
            return list(identities)
        except BaseException:
            await db.rollback()
            raise

    @_serialized
    async def load(self, session_id: str) -> Session | None:
        db = await self._get_db()
        cursor = await db.execute(_GET, (session_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return Session.from_row(dict(row))

    @_serialized
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

    @_serialized
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
                    datetime.fromisoformat(row["archived_at"]) if row["archived_at"] else None
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

    @_serialized
    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
        workspace_root: str | None = None,
    ) -> tuple[list[Session], int]:
        db = await self._get_db()
        offset = (page - 1) * page_size
        normalized_query = query.strip()

        where = "status = 'active'"
        params: list[Any] = []
        normalized_workspace = str(workspace_root or "").strip()
        if normalized_workspace:
            where += " AND workspace_root = ?"
            params.append(normalized_workspace)
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
            f"SELECT * FROM sessions WHERE {where} "
            "ORDER BY (pinned_at IS NOT NULL) DESC, pinned_at DESC, updated_at DESC "
            "LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        rows = await cursor.fetchall()
        sessions = [Session.from_row(dict(r)) for r in rows]

        return sessions, total

    @_serialized
    async def delete(self, session_id: str) -> bool:
        db = await self._get_db()
        return await self._delete(db, session_id, archived_only=False)

    @_serialized
    async def delete_if_archived(self, session_id: str) -> bool:
        """Atomically delete only while retention authority is still valid."""
        db = await self._get_db()
        return await self._delete(db, session_id, archived_only=True)

    async def _delete(self, db, session_id: str, *, archived_only: bool) -> bool:
        await db.execute("BEGIN IMMEDIATE")
        try:
            condition = " AND status='archived'" if archived_only else ""
            await db.execute(
                "INSERT OR IGNORE INTO deleted_personal_team_sessions "
                "SELECT id, personal_team_id, ? FROM sessions WHERE id=? "
                "AND personal_team_id<>''" + condition,
                (datetime.now().isoformat(), session_id),
            )
            cursor = await db.execute("DELETE FROM sessions WHERE id=?" + condition, (session_id,))
            await db.commit()
            return cursor.rowcount > 0
        except BaseException:
            await db.rollback()
            raise

    @_serialized
    async def archive(self, session_id: str) -> bool:
        db = await self._get_db()
        timestamp = datetime.now().isoformat()
        cursor = await db.execute(_ARCHIVE, (timestamp, timestamp, session_id))
        await db.commit()
        return cursor.rowcount > 0

    @_serialized
    async def set_pinned(self, session_id: str, pinned: bool) -> bool:
        db = await self._get_db()
        pinned_at = datetime.now().isoformat() if pinned else None
        cursor = await db.execute(
            "UPDATE sessions SET pinned_at = ? WHERE id = ?",
            (pinned_at, session_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def duplicate(self, session_id: str) -> Session | None:
        source = await self.load(session_id)
        if source is None:
            return None
        now = datetime.now()
        duplicate = Session(
            title=f"{source.title or '新会话'} 副本",
            model=source.model,
            messages=deepcopy(source.messages),
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            status="active",
            total_tokens=source.total_tokens,
            total_cost_usd=source.total_cost_usd,
            workspace_root=source.workspace_root,
            git_branch=source.git_branch,
            summary=source.summary,
            personal_agent_id=source.personal_agent_id,
            personal_execution_id=source.personal_execution_id,
            personal_team_id=source.personal_team_id,
        )
        await self.save(duplicate)
        return duplicate

    @_serialized
    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
