"""Durable workspace-scoped trust for exact Harness profile digests."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from naumi_agent.config.state_paths import resolve_naumi_state_home

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def resolve_harness_trust_db_path() -> Path:
    """Resolve user-owned state outside any Agent-writable workspace."""
    return resolve_naumi_state_home() / "harness-trust.db"


@dataclass(frozen=True)
class HarnessTrustRecord:
    workspace_root: str
    profile_digest: str
    trusted_at: str
    source: str


class HarnessTrustStoreError(RuntimeError):
    """Safe user-facing failure from the external trust database."""


class HarnessTrustStore:
    """Persist one currently trusted profile digest per canonical workspace."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._schema_ready = False

    async def is_trusted(self, workspace_root: str | Path, digest: str) -> bool:
        canonical = _canonical_workspace(workspace_root)
        normalized_digest = _validate_digest(digest)
        record = await self.get(canonical)
        return record is not None and record.profile_digest == normalized_digest

    async def get(self, workspace_root: str | Path) -> HarnessTrustRecord | None:
        canonical = _canonical_workspace(workspace_root)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT workspace_root, profile_digest, trusted_at, source
                    FROM harness_profile_trust
                    WHERE workspace_root = ?
                    """,
                    (canonical,),
                )
                row = await cursor.fetchone()
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessTrustStoreError(
                "无法读取用户级 Harness 信任状态。请检查状态目录权限或重建信任库。"
            ) from exc
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessTrustStoreError(
                "无法读取用户级 Harness 信任状态。请检查状态目录权限或重建信任库。"
            ) from exc
        if row is None:
            return None
        return HarnessTrustRecord(
            workspace_root=str(row[0]),
            profile_digest=str(row[1]),
            trusted_at=str(row[2]),
            source=str(row[3]),
        )

    async def trust(
        self,
        workspace_root: str | Path,
        digest: str,
        *,
        source: str,
    ) -> HarnessTrustRecord:
        canonical = _canonical_workspace(workspace_root)
        normalized_digest = _validate_digest(digest)
        normalized_source = source.strip() if isinstance(source, str) else ""
        if not normalized_source or len(normalized_source) > 64:
            raise ValueError("Harness trust source 必须是 1-64 个字符。")

        try:
            async with self._write_lock:
                await self._ensure_schema()
                existing = await self.get(canonical)
                if (
                    existing is not None
                    and existing.profile_digest == normalized_digest
                    and existing.source == normalized_source
                ):
                    return existing

                trusted_at = datetime.now(UTC).isoformat()
                async with self._connect() as db:
                    await db.execute(
                        """
                        INSERT INTO harness_profile_trust (
                            workspace_root, profile_digest, trusted_at, source
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(workspace_root) DO UPDATE SET
                            profile_digest = excluded.profile_digest,
                            trusted_at = excluded.trusted_at,
                            source = excluded.source
                        """,
                        (canonical, normalized_digest, trusted_at, normalized_source),
                    )
                    await db.commit()
                return HarnessTrustRecord(
                    workspace_root=canonical,
                    profile_digest=normalized_digest,
                    trusted_at=trusted_at,
                    source=normalized_source,
                )
        except HarnessTrustStoreError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessTrustStoreError(
                "无法保存 Harness 信任状态。请检查用户级状态目录权限。"
            ) from exc

    async def untrust(self, workspace_root: str | Path) -> bool:
        canonical = _canonical_workspace(workspace_root)
        if not self._db_path.is_file():
            return False
        try:
            async with self._write_lock, self._connect() as db:
                cursor = await db.execute(
                    "DELETE FROM harness_profile_trust WHERE workspace_root = ?",
                    (canonical,),
                )
                await db.commit()
                return cursor.rowcount > 0
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            raise HarnessTrustStoreError(
                "无法撤销 Harness 信任状态。请检查用户级状态目录权限。"
            ) from exc
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessTrustStoreError(
                "无法撤销 Harness 信任状态。请检查用户级状态目录权限。"
            ) from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _restrict_permissions(self._db_path.parent, 0o700)
            async with self._connect() as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_profile_trust (
                        workspace_root TEXT PRIMARY KEY,
                        profile_digest TEXT NOT NULL,
                        trusted_at TEXT NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )
                await db.commit()
            _restrict_permissions(self._db_path, 0o600)
            self._schema_ready = True

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self._db_path, timeout=5.0)


def _canonical_workspace(workspace_root: str | Path) -> str:
    if isinstance(workspace_root, str) and not workspace_root.strip():
        raise ValueError("workspace_root 不能为空。")
    return str(Path(workspace_root).expanduser().resolve())


def _validate_digest(digest: str) -> str:
    normalized = digest.strip() if isinstance(digest, str) else ""
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("profile digest 必须是 64 位小写 SHA-256。")
    return normalized


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass
