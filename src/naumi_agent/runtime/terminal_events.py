"""Durable, integrity-checked protocol events for terminal reconnect recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

TERMINAL_EVENT_SCHEMA_VERSION = 1
DEFAULT_MAX_EVENTS_PER_STREAM = 4096
REPLAY_SAFE_TERMINAL_EVENTS = frozenset(
    {
        "completion/receipt",
        "harness/receipt",
    }
)

_IDENTITY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SESSION_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")


class TerminalEventJournalError(RuntimeError):
    """Raised when a terminal event cannot be durably verified."""


class TerminalEventJournalConflictError(TerminalEventJournalError):
    """Raised when one semantic identity is reused for different event data."""


@dataclass(frozen=True, slots=True)
class TerminalEventRecord:
    """One immutable protocol event identity within a session stream."""

    schema_version: int
    event_id: str
    stream_id: str
    cursor: int
    event_type: str
    criticality: str
    idempotency_key: str
    payload: dict[str, Any]
    payload_sha256: str
    envelope_sha256: str
    occurred_at: str

    def envelope_fields(self) -> dict[str, str | int]:
        """Return the stable fields added to a live protocol envelope."""
        return {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "cursor": self.cursor,
        }


class TerminalEventJournalStore:
    """SQLite authority for replay-safe terminal events and monotonic cursors."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        workspace_root: str | Path,
        max_events_per_stream: int = DEFAULT_MAX_EVENTS_PER_STREAM,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._workspace_root = str(Path(workspace_root).expanduser().resolve())
        if not isinstance(max_events_per_stream, int) or isinstance(
            max_events_per_stream,
            bool,
        ):
            raise TypeError("max_events_per_stream 必须是整数。")
        if not 1 <= max_events_per_stream <= 100_000:
            raise ValueError("max_events_per_stream 必须介于 1 和 100000。")
        self._max_events_per_stream = max_events_per_stream
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    async def append(
        self,
        *,
        session_id: str,
        event_type: str,
        criticality: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> TerminalEventRecord:
        """Persist before publish, reusing a matching semantic identity."""
        normalized_session = _validate_session_id(session_id)
        normalized_type = _validate_event_type(event_type)
        normalized_criticality = str(criticality).strip()
        if normalized_criticality != "terminal":
            raise ValueError("可重放回执的 criticality 必须是 terminal。")
        normalized_key = _validate_idempotency_key(idempotency_key)
        payload_json = _canonical_json_object(payload)
        payload_sha256 = _sha256(payload_json)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._prepare(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                existing = await self._find_by_key(
                    db,
                    session_id=normalized_session,
                    idempotency_key=normalized_key,
                )
                if existing is not None:
                    record = _record_from_row(existing)
                    if (
                        record.event_type != normalized_type
                        or record.criticality != normalized_criticality
                        or record.payload_sha256 != payload_sha256
                    ):
                        raise TerminalEventJournalConflictError(
                            "终端事件幂等键已用于不同内容，已拒绝覆盖。"
                        )
                    await db.commit()
                    return record

                stream_id, cursor = await self._allocate_cursor(
                    db,
                    session_id=normalized_session,
                )
                event_id = f"tev_{uuid.uuid4().hex[:24]}"
                occurred_at = datetime.now(UTC).isoformat()
                envelope_sha256 = _envelope_digest(
                    workspace_root=self._workspace_root,
                    session_id=normalized_session,
                    event_id=event_id,
                    stream_id=stream_id,
                    cursor=cursor,
                    event_type=normalized_type,
                    criticality=normalized_criticality,
                    idempotency_key=normalized_key,
                    payload_sha256=payload_sha256,
                    occurred_at=occurred_at,
                )
                await db.execute(
                    """
                    INSERT INTO terminal_events (
                        workspace_root, session_id, stream_id, cursor,
                        schema_version, event_id, event_type, criticality,
                        idempotency_key, payload_json, payload_sha256,
                        envelope_sha256, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._workspace_root,
                        normalized_session,
                        stream_id,
                        cursor,
                        TERMINAL_EVENT_SCHEMA_VERSION,
                        event_id,
                        normalized_type,
                        normalized_criticality,
                        normalized_key,
                        payload_json,
                        payload_sha256,
                        envelope_sha256,
                        occurred_at,
                    ),
                )
                await self._prune_stream(
                    db,
                    session_id=normalized_session,
                    cursor=cursor,
                )
                await db.commit()
            except TerminalEventJournalError:
                await _rollback_quietly(db)
                raise
            except sqlite3.Error as exc:
                await _rollback_quietly(db)
                raise TerminalEventJournalError(
                    "终端事件日志写入失败。"
                ) from exc
            except Exception:
                await _rollback_quietly(db)
                raise

        return TerminalEventRecord(
            schema_version=TERMINAL_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            stream_id=stream_id,
            cursor=cursor,
            event_type=normalized_type,
            criticality=normalized_criticality,
            idempotency_key=normalized_key,
            payload=json.loads(payload_json),
            payload_sha256=payload_sha256,
            envelope_sha256=envelope_sha256,
            occurred_at=occurred_at,
        )

    async def list_after(
        self,
        *,
        session_id: str,
        cursor: int,
        limit: int = 200,
    ) -> list[TerminalEventRecord]:
        """Read a verified cursor window for a later replay transport."""
        normalized_session = _validate_session_id(session_id)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("cursor 必须是非负整数。")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须介于 1 和 1000。")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._prepare(db)
            try:
                result = await db.execute(
                    """
                    SELECT * FROM terminal_events
                    WHERE workspace_root = ? AND session_id = ? AND cursor > ?
                    ORDER BY cursor ASC
                    LIMIT ?
                    """,
                    (self._workspace_root, normalized_session, cursor, limit),
                )
                rows = await result.fetchall()
            except sqlite3.Error as exc:
                raise TerminalEventJournalError(
                    "终端事件日志读取失败。"
                ) from exc
        return [_record_from_row(row) for row in rows]

    async def get_by_idempotency_key(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> TerminalEventRecord | None:
        """Return one verified event identity, if retained."""
        normalized_session = _validate_session_id(session_id)
        normalized_key = _validate_idempotency_key(idempotency_key)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._prepare(db)
            try:
                row = await self._find_by_key(
                    db,
                    session_id=normalized_session,
                    idempotency_key=normalized_key,
                )
            except sqlite3.Error as exc:
                raise TerminalEventJournalError(
                    "终端事件日志读取失败。"
                ) from exc
        return None if row is None else _record_from_row(row)

    async def _prepare(self, db: aiosqlite.Connection) -> None:
        for attempt in range(7):
            try:
                await self._prepare_once(db)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 6:
                    raise TerminalEventJournalError(
                        "终端事件日志初始化失败。"
                    ) from exc
                await _rollback_quietly(db)
                await asyncio.sleep(0.01 * (2**attempt))

    async def _prepare_once(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS terminal_event_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS terminal_event_streams (
                workspace_root TEXT NOT NULL,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                last_cursor INTEGER NOT NULL CHECK(last_cursor >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace_root, session_id),
                UNIQUE (stream_id)
            );
            CREATE TABLE IF NOT EXISTS terminal_events (
                workspace_root TEXT NOT NULL,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                cursor INTEGER NOT NULL CHECK(cursor > 0),
                schema_version INTEGER NOT NULL,
                event_id TEXT NOT NULL PRIMARY KEY,
                event_type TEXT NOT NULL,
                criticality TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                envelope_sha256 TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                UNIQUE (workspace_root, session_id, cursor),
                UNIQUE (workspace_root, session_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS terminal_events_stream_cursor_idx
                ON terminal_events (workspace_root, session_id, cursor);
            """
        )
        await db.execute(
            """
            INSERT INTO terminal_event_metadata (key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (str(TERMINAL_EVENT_SCHEMA_VERSION),),
        )
        result = await db.execute(
            "SELECT value FROM terminal_event_metadata WHERE key = 'schema_version'"
        )
        row = await result.fetchone()
        if row is None or row["value"] != str(TERMINAL_EVENT_SCHEMA_VERSION):
            raise TerminalEventJournalError(
                "终端事件日志 schema_version 不兼容。"
            )
        await db.commit()

    async def _find_by_key(
        self,
        db: aiosqlite.Connection,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> aiosqlite.Row | None:
        result = await db.execute(
            """
            SELECT * FROM terminal_events
            WHERE workspace_root = ? AND session_id = ? AND idempotency_key = ?
            """,
            (self._workspace_root, session_id, idempotency_key),
        )
        return await result.fetchone()

    async def _allocate_cursor(
        self,
        db: aiosqlite.Connection,
        *,
        session_id: str,
    ) -> tuple[str, int]:
        result = await db.execute(
            """
            SELECT stream_id, last_cursor FROM terminal_event_streams
            WHERE workspace_root = ? AND session_id = ?
            """,
            (self._workspace_root, session_id),
        )
        row = await result.fetchone()
        now = datetime.now(UTC).isoformat()
        if row is None:
            stream_id = f"tes_{uuid.uuid4().hex[:24]}"
            cursor = 1
            await db.execute(
                """
                INSERT INTO terminal_event_streams (
                    workspace_root, session_id, stream_id, last_cursor, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self._workspace_root, session_id, stream_id, cursor, now),
            )
            return stream_id, cursor
        stream_id = str(row["stream_id"])
        cursor = int(row["last_cursor"]) + 1
        await db.execute(
            """
            UPDATE terminal_event_streams
            SET last_cursor = ?, updated_at = ?
            WHERE workspace_root = ? AND session_id = ?
            """,
            (cursor, now, self._workspace_root, session_id),
        )
        return stream_id, cursor

    async def _prune_stream(
        self,
        db: aiosqlite.Connection,
        *,
        session_id: str,
        cursor: int,
    ) -> None:
        cutoff = cursor - self._max_events_per_stream
        if cutoff <= 0:
            return
        await db.execute(
            """
            DELETE FROM terminal_events
            WHERE workspace_root = ? AND session_id = ? AND cursor <= ?
            """,
            (self._workspace_root, session_id, cutoff),
        )


def _record_from_row(row: aiosqlite.Row) -> TerminalEventRecord:
    try:
        schema_version = int(row["schema_version"])
        payload_json = str(row["payload_json"])
        payload = json.loads(payload_json)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TerminalEventJournalError("终端事件日志记录无法解析。") from exc
    if schema_version != TERMINAL_EVENT_SCHEMA_VERSION or not isinstance(payload, dict):
        raise TerminalEventJournalError("终端事件日志记录版本或 payload 无效。")
    canonical_payload = _canonical_json_object(payload)
    payload_sha256 = str(row["payload_sha256"])
    if canonical_payload != payload_json or _sha256(payload_json) != payload_sha256:
        raise TerminalEventJournalError("终端事件日志 payload 完整性校验失败。")
    expected_envelope = _envelope_digest(
        workspace_root=str(row["workspace_root"]),
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        stream_id=str(row["stream_id"]),
        cursor=int(row["cursor"]),
        event_type=str(row["event_type"]),
        criticality=str(row["criticality"]),
        idempotency_key=str(row["idempotency_key"]),
        payload_sha256=payload_sha256,
        occurred_at=str(row["occurred_at"]),
    )
    if expected_envelope != str(row["envelope_sha256"]):
        raise TerminalEventJournalError("终端事件日志 envelope 完整性校验失败。")
    return TerminalEventRecord(
        schema_version=schema_version,
        event_id=str(row["event_id"]),
        stream_id=str(row["stream_id"]),
        cursor=int(row["cursor"]),
        event_type=str(row["event_type"]),
        criticality=str(row["criticality"]),
        idempotency_key=str(row["idempotency_key"]),
        payload=payload,
        payload_sha256=payload_sha256,
        envelope_sha256=str(row["envelope_sha256"]),
        occurred_at=str(row["occurred_at"]),
    )


async def _rollback_quietly(db: aiosqlite.Connection) -> None:
    try:
        await db.rollback()
    except sqlite3.Error:
        pass


def _validate_session_id(value: str) -> str:
    normalized = str(value).strip()
    if not _SESSION_ID_RE.fullmatch(normalized):
        raise ValueError("session_id 必须是 1 到 255 个可见字符。")
    return normalized


def _validate_event_type(value: str) -> str:
    normalized = str(value).strip()
    if normalized not in REPLAY_SAFE_TERMINAL_EVENTS:
        raise ValueError(f"事件不可进入终端重放日志: {normalized or '<empty>'}")
    return normalized


def _validate_idempotency_key(value: str) -> str:
    normalized = str(value).strip()
    if not _IDENTITY_KEY_RE.fullmatch(normalized):
        raise ValueError("idempotency_key 格式无效。")
    return normalized


def _canonical_json_object(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise TypeError("payload 必须是对象。")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload 必须是可确定序列化的 JSON 对象。") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _envelope_digest(
    *,
    workspace_root: str,
    session_id: str,
    event_id: str,
    stream_id: str,
    cursor: int,
    event_type: str,
    criticality: str,
    idempotency_key: str,
    payload_sha256: str,
    occurred_at: str,
) -> str:
    return _sha256(
        json.dumps(
            {
                "criticality": criticality,
                "cursor": cursor,
                "event_id": event_id,
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "occurred_at": occurred_at,
                "payload_sha256": payload_sha256,
                "schema_version": TERMINAL_EVENT_SCHEMA_VERSION,
                "session_id": session_id,
                "stream_id": stream_id,
                "workspace_root": workspace_root,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


__all__ = [
    "DEFAULT_MAX_EVENTS_PER_STREAM",
    "REPLAY_SAFE_TERMINAL_EVENTS",
    "TERMINAL_EVENT_SCHEMA_VERSION",
    "TerminalEventJournalConflictError",
    "TerminalEventJournalError",
    "TerminalEventJournalStore",
    "TerminalEventRecord",
]
