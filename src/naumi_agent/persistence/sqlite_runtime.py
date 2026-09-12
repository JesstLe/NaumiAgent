"""Shared SQLite connection setup for concurrently initialized stores."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

SchemaInitializer = Callable[[aiosqlite.Connection], Awaitable[None]]


class AsyncSQLiteSchemaGuard:
    """Initialize one store schema once per physical SQLite database file."""

    def __init__(self, *, connection_timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(connection_timeout_seconds, bool)
            or not isinstance(connection_timeout_seconds, int | float)
        ):
            raise TypeError("connection_timeout_seconds must be numeric")
        if connection_timeout_seconds <= 0:
            raise ValueError("connection_timeout_seconds must be positive")
        self._connection_timeout_seconds = float(connection_timeout_seconds)
        self._lock = asyncio.Lock()
        self._initialized = False
        self._database_identity: tuple[int, int] | None = None

    async def ensure(self, db_path: str | Path, initializer: SchemaInitializer) -> None:
        path = Path(db_path).expanduser().resolve()
        if self._initialized and _database_identity(path) == self._database_identity:
            return
        async with self._lock:
            if self._initialized and _database_identity(path) == self._database_identity:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(
                path,
                timeout=self._connection_timeout_seconds,
            ) as db:
                await initializer(db)
                await db.commit()
            identity = _database_identity(path)
            if identity is None:
                raise OSError("SQLite schema initialization did not create a database")
            self._database_identity = identity
            self._initialized = True


async def configure_sqlite_connection(
    db: aiosqlite.Connection,
    *,
    busy_timeout_ms: int = 10_000,
) -> None:
    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
        raise TypeError("busy_timeout_ms must be an integer")
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")
    await db.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")


async def ensure_sqlite_wal(
    db: aiosqlite.Connection,
    *,
    retry_seconds: float = 10.0,
) -> None:
    if isinstance(retry_seconds, bool) or not isinstance(retry_seconds, int | float):
        raise TypeError("retry_seconds must be numeric")
    if retry_seconds <= 0:
        raise ValueError("retry_seconds must be positive")
    deadline = asyncio.get_running_loop().time() + float(retry_seconds)
    while True:
        try:
            current = await (await db.execute("PRAGMA journal_mode")).fetchone()
            if current is not None and str(current[0]).lower() == "wal":
                return
            updated = await (await db.execute("PRAGMA journal_mode = WAL")).fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.02)
            continue
        if updated is not None and str(updated[0]).lower() == "wal":
            return
        raise sqlite3.OperationalError("SQLite database could not enable WAL mode")


def _database_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return stat_result.st_dev, stat_result.st_ino


__all__ = [
    "AsyncSQLiteSchemaGuard",
    "configure_sqlite_connection",
    "ensure_sqlite_wal",
]
