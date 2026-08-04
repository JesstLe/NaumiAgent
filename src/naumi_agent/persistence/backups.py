"""Verified pre-migration backups for catalogued SQLite stores."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from naumi_agent.persistence.store_catalog import (
    StorageKind,
    StoreDefinition,
    VersionStrategy,
)

BACKUP_MANIFEST_SCHEMA_VERSION = 1
BACKUP_SPACE_FLOOR_BYTES = 1024 * 1024
MAX_BACKUP_MANIFEST_BYTES = 64 * 1024
_STORE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_BACKUP_ID_PATTERN = re.compile(r"arc5b_[a-f0-9]{24}\Z")

Clock = Callable[[], datetime]
BackupIdFactory = Callable[[], str]
FreeSpaceProbe = Callable[[Path], int]


class BackupError(RuntimeError):
    """Base class for safe, user-visible backup failures."""


class BackupSpaceError(BackupError):
    """Raised when the destination cannot hold a verified SQLite snapshot."""


class BackupVerificationError(BackupError):
    """Raised when a backup or its manifest cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SQLiteBackupPlan:
    """Read-only space and source metadata for one backup attempt."""

    store_id: str
    source_path: Path
    backup_root: Path
    source_schema_version: int
    target_schema_version: int
    source_size_bytes: int
    logical_size_bytes: int
    required_free_bytes: int
    available_free_bytes: int
    sufficient_space: bool
    destination_permission_mode: int | None
    destination_private: bool


@dataclass(frozen=True, slots=True)
class SQLiteBackupManifest:
    """Canonical, non-secret identity for one immutable SQLite backup."""

    schema_version: int
    backup_id: str
    store_id: str
    created_at: str
    source_path_sha256: str
    source_schema_version: int
    target_schema_version: int
    backup_file: str
    backup_sha256: str
    backup_size_bytes: int
    page_count: int
    page_size: int

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                asdict(self),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SQLiteBackupReceipt:
    """Verified local paths and digests for a published backup directory."""

    manifest: SQLiteBackupManifest
    backup_dir: Path
    backup_path: Path
    manifest_path: Path
    manifest_sha256: str


class SQLiteBackupManager:
    """Plan, atomically create, and independently verify SQLite backups."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        backup_id_factory: BackupIdFactory | None = None,
        free_space_probe: FreeSpaceProbe | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backup_id_factory = backup_id_factory or (
            lambda: f"arc5b_{secrets.token_hex(12)}"
        )
        self._free_space_probe = free_space_probe or (
            lambda path: shutil.disk_usage(path).free
        )

    def plan(
        self,
        definition: StoreDefinition,
        *,
        backup_root: str | Path,
    ) -> SQLiteBackupPlan:
        """Inspect source and destination capacity without writing any bytes."""
        source_path, target_version = _validate_definition(definition)
        raw_root = Path(backup_root).expanduser()
        if raw_root.is_symlink():
            raise BackupVerificationError("备份根目录不能是符号链接。")
        root = raw_root.resolve()
        if root.exists() and not root.is_dir():
            raise BackupVerificationError("备份根路径不是目录。")
        capacity_path = _nearest_existing_directory(root)
        source_version, page_count, page_size = _inspect_sqlite(source_path)
        if source_version > target_version:
            raise BackupVerificationError(
                f"Store {definition.store_id} 的版本 {source_version} 高于当前支持版本 "
                f"{target_version}。"
            )
        source_size = source_path.stat().st_size
        logical_size = page_count * page_size
        payload_size = max(source_size, logical_size)
        required = payload_size + max(BACKUP_SPACE_FLOOR_BYTES, payload_size // 10)
        try:
            available = self._free_space_probe(capacity_path)
        except OSError as exc:
            raise BackupSpaceError(
                f"Store {definition.store_id} 无法读取备份目录剩余空间。"
            ) from exc
        if isinstance(available, bool) or not isinstance(available, int) or available < 0:
            raise BackupSpaceError(
                f"Store {definition.store_id} 的备份空间探测结果无效。"
            )
        destination_mode, destination_private = _private_directory_state(root)
        return SQLiteBackupPlan(
            store_id=definition.store_id,
            source_path=source_path,
            backup_root=root,
            source_schema_version=source_version,
            target_schema_version=target_version,
            source_size_bytes=source_size,
            logical_size_bytes=logical_size,
            required_free_bytes=required,
            available_free_bytes=available,
            sufficient_space=available >= required,
            destination_permission_mode=destination_mode,
            destination_private=destination_private,
        )

    def create(
        self,
        definition: StoreDefinition,
        *,
        backup_root: str | Path,
    ) -> SQLiteBackupReceipt:
        """Create and publish one verified snapshot without exposing partial output."""
        plan = self.plan(definition, backup_root=backup_root)
        if not plan.sufficient_space:
            raise BackupSpaceError(
                f"Store {plan.store_id} 备份空间不足：至少需要 "
                f"{plan.required_free_bytes} 字节，可用 {plan.available_free_bytes} 字节。"
            )
        created_at = _utc_timestamp(self._clock())
        backup_id = self._backup_id_factory()
        if not isinstance(backup_id, str) or not _BACKUP_ID_PATTERN.fullmatch(backup_id):
            raise BackupError("备份 ID 生成器返回了无效身份。")

        store_root = _prepare_owned_directories(plan.backup_root, plan.store_id)
        final_dir = store_root / backup_id
        staging_dir = store_root / f".{backup_id}.tmp"
        if _lexists(final_dir) or _lexists(staging_dir):
            raise BackupError(f"Store {plan.store_id} 的备份身份已存在，请重试。")

        backup_name = "store.sqlite3"
        manifest_name = "manifest.json"
        staging_owned = False
        try:
            staging_dir.mkdir(mode=0o700)
            staging_owned = True
            backup_path = staging_dir / backup_name
            _sqlite_online_backup(plan.source_path, backup_path, plan.store_id)
            backup_version, page_count, page_size = _inspect_sqlite(
                backup_path,
                quick_check=True,
            )
            if backup_version != plan.source_schema_version:
                raise BackupVerificationError(
                    f"Store {plan.store_id} 备份版本与预检不一致。"
                )
            backup_sha256 = _sha256_file(backup_path)
            manifest = SQLiteBackupManifest(
                schema_version=BACKUP_MANIFEST_SCHEMA_VERSION,
                backup_id=backup_id,
                store_id=plan.store_id,
                created_at=created_at,
                source_path_sha256=_sha256_text(str(plan.source_path)),
                source_schema_version=backup_version,
                target_schema_version=plan.target_schema_version,
                backup_file=backup_name,
                backup_sha256=backup_sha256,
                backup_size_bytes=backup_path.stat().st_size,
                page_count=page_count,
                page_size=page_size,
            )
            manifest_path = staging_dir / manifest_name
            _write_new_file(manifest_path, manifest.canonical_bytes())
            _fsync_directory(staging_dir)
            os.replace(staging_dir, final_dir)
            _fsync_directory(store_root)
        except BackupError:
            if staging_owned:
                _remove_staging_directory(staging_dir)
            raise
        except (OSError, sqlite3.Error) as exc:
            if staging_owned:
                _remove_staging_directory(staging_dir)
            raise BackupError(
                f"Store {plan.store_id} 备份失败，未发布不完整快照。"
            ) from exc

        try:
            return self.verify(final_dir)
        except BackupError:
            _remove_staging_directory(final_dir)
            _fsync_directory(store_root)
            raise

    def verify(self, backup_dir: str | Path) -> SQLiteBackupReceipt:
        """Verify a published manifest, digest, SQLite health, and permissions."""
        raw_directory = Path(backup_dir).expanduser()
        if raw_directory.is_symlink():
            raise BackupVerificationError("备份目录不能是符号链接。")
        directory = raw_directory.resolve()
        if not directory.is_dir():
            raise BackupVerificationError("备份目录不存在或类型无效。")
        _require_private_mode(directory, directory=True)
        manifest_path = directory / "manifest.json"
        manifest_bytes = _read_bounded_regular_file(manifest_path)
        manifest = _parse_manifest(manifest_bytes)
        if directory.name != manifest.backup_id:
            raise BackupVerificationError("备份目录与 manifest 身份不一致。")
        if directory.parent.name != manifest.store_id:
            raise BackupVerificationError("备份 Store 目录与 manifest 不一致。")

        backup_path = directory / manifest.backup_file
        if backup_path.parent != directory or backup_path.is_symlink():
            raise BackupVerificationError("manifest backup_file 路径越界或为符号链接。")
        _require_private_mode(manifest_path, directory=False)
        _require_private_mode(backup_path, directory=False)
        if backup_path.stat().st_size != manifest.backup_size_bytes:
            raise BackupVerificationError("备份文件大小与 manifest 不一致。")
        if _sha256_file(backup_path) != manifest.backup_sha256:
            raise BackupVerificationError("备份文件 digest 与 manifest 不一致。")
        version, page_count, page_size = _inspect_sqlite(backup_path, quick_check=True)
        if (
            version != manifest.source_schema_version
            or page_count != manifest.page_count
            or page_size != manifest.page_size
        ):
            raise BackupVerificationError("备份 SQLite 元数据与 manifest 不一致。")
        return SQLiteBackupReceipt(
            manifest=manifest,
            backup_dir=directory,
            backup_path=backup_path,
            manifest_path=manifest_path,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )


def _validate_definition(definition: StoreDefinition) -> tuple[Path, int]:
    if not _STORE_ID_PATTERN.fullmatch(definition.store_id):
        raise BackupVerificationError("Store ID 不能安全映射到备份目录。")
    if definition.kind is not StorageKind.SQLITE:
        raise BackupVerificationError("备份管理器当前仅支持 SQLite Store。")
    if definition.version_strategy is not VersionStrategy.SQLITE_USER_VERSION:
        raise BackupVerificationError(
            "SQLite Store 必须使用 sqlite_user_version 版本策略。"
        )
    target = definition.supported_schema_version
    if isinstance(target, bool) or not isinstance(target, int) or target < 0:
        raise BackupVerificationError("Store 必须声明非负整数目标版本。")
    raw_path = definition.path.expanduser()
    if raw_path.is_symlink():
        raise BackupVerificationError(f"Store {definition.store_id} 不能是符号链接。")
    path = raw_path.resolve()
    if not path.exists():
        raise BackupVerificationError(
            f"Store {definition.store_id} 不存在，备份不会自动创建源文件。"
        )
    if not path.is_file() or path.is_symlink():
        raise BackupVerificationError(f"Store {definition.store_id} 不是普通文件。")
    return path, target


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            raise BackupSpaceError("备份路径没有可用的父目录。")
        current = current.parent
    if not current.is_dir():
        raise BackupSpaceError("备份路径的已有父级不是目录。")
    return current


def _inspect_sqlite(
    path: Path,
    *,
    quick_check: bool = False,
) -> tuple[int, int, int]:
    try:
        uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.execute("PRAGMA query_only = ON")
            version = _pragma_integer(connection, "user_version")
            page_count = _pragma_integer(connection, "page_count")
            page_size = _pragma_integer(connection, "page_size")
            if quick_check:
                rows = connection.execute("PRAGMA quick_check").fetchall()
                if rows != [("ok",)]:
                    raise BackupVerificationError("备份 SQLite quick_check 未通过。")
    except BackupVerificationError:
        raise
    except sqlite3.Error as exc:
        raise BackupVerificationError("SQLite Store 无法读取或已损坏。") from exc
    return version, page_count, page_size


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if (
        row is None
        or len(row) != 1
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
        or row[0] < 0
    ):
        raise BackupVerificationError(f"SQLite {name} 元数据无效。")
    return row[0]


def _sqlite_online_backup(source_path: Path, backup_path: Path, store_id: str) -> None:
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source, closing(sqlite3.connect(backup_path)) as destination:
            source.execute("PRAGMA query_only = ON")
            source.backup(destination, pages=256, sleep=0.01)
            destination.commit()
        backup_path.chmod(0o600)
        _fsync_file(backup_path)
    except sqlite3.Error as exc:
        raise BackupError(
            f"Store {store_id} 无法生成一致性 SQLite 快照。"
        ) from exc


def _prepare_owned_directories(root: Path, store_id: str) -> Path:
    if not _lexists(root):
        try:
            root.mkdir(parents=True, mode=0o700, exist_ok=False)
            root.chmod(0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise BackupError("无法创建私有备份根目录。") from exc
    if not root.is_dir() or root.is_symlink():
        raise BackupError("备份根目录类型无效。")
    _require_private_mode(root, directory=True)
    store_root = root / store_id
    if not _lexists(store_root):
        try:
            store_root.mkdir(mode=0o700, exist_ok=False)
            store_root.chmod(0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise BackupError(f"Store {store_id} 无法创建私有备份目录。") from exc
    if not store_root.is_dir() or store_root.is_symlink():
        raise BackupError("Store 备份目录类型无效。")
    _require_private_mode(store_root, directory=True)
    return store_root


def _lexists(path: Path) -> bool:
    """Return whether a directory entry exists without following symlinks."""
    return os.path.lexists(path)


def _require_private_mode(path: Path, *, directory: bool) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except OSError as exc:
        raise BackupVerificationError("无法读取备份权限。") from exc
    required = 0o700 if directory else 0o600
    if mode & 0o077 or mode & required != required:
        label = "目录" if directory else "文件"
        raise BackupVerificationError(
            f"备份{label}权限必须至少为 {required:o} 且不能向 group/other 开放。"
        )


def _private_directory_state(path: Path) -> tuple[int | None, bool]:
    if not path.exists() or os.name != "posix":
        return None, True
    try:
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except OSError as exc:
        raise BackupVerificationError("无法读取备份根目录权限。") from exc
    return mode, not bool(mode & 0o077) and mode & 0o700 == 0o700


def _write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _read_bounded_regular_file(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise BackupVerificationError("备份 manifest 不存在或类型无效。")
    size = path.stat().st_size
    if size < 2 or size > MAX_BACKUP_MANIFEST_BYTES:
        raise BackupVerificationError("备份 manifest 大小无效。")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BackupVerificationError("备份 manifest 无法读取。") from exc


def _parse_manifest(content: bytes) -> SQLiteBackupManifest:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupVerificationError("备份 manifest 不是有效 JSON。") from exc
    expected = {field.name for field in fields(SQLiteBackupManifest)}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise BackupVerificationError("备份 manifest 字段不完整或包含未知字段。")
    try:
        manifest = SQLiteBackupManifest(**payload)
    except TypeError as exc:
        raise BackupVerificationError("备份 manifest 字段类型无效。") from exc
    _validate_manifest(manifest)
    if manifest.canonical_bytes() != content:
        raise BackupVerificationError("备份 manifest 不是 canonical JSON。")
    return manifest


def _validate_manifest(manifest: SQLiteBackupManifest) -> None:
    integer_fields = (
        manifest.schema_version,
        manifest.source_schema_version,
        manifest.target_schema_version,
        manifest.backup_size_bytes,
        manifest.page_count,
        manifest.page_size,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in integer_fields
    ):
        raise BackupVerificationError("备份 manifest 数值字段无效。")
    if manifest.schema_version != BACKUP_MANIFEST_SCHEMA_VERSION:
        raise BackupVerificationError("备份 manifest schema_version 不兼容。")
    if not isinstance(manifest.backup_id, str) or not _BACKUP_ID_PATTERN.fullmatch(
        manifest.backup_id
    ):
        raise BackupVerificationError("备份 manifest backup_id 无效。")
    if not isinstance(manifest.store_id, str) or not _STORE_ID_PATTERN.fullmatch(
        manifest.store_id
    ):
        raise BackupVerificationError("备份 manifest store_id 无效。")
    if manifest.backup_file != "store.sqlite3":
        raise BackupVerificationError("备份 manifest backup_file 无效。")
    if not _is_sha256(manifest.source_path_sha256) or not _is_sha256(
        manifest.backup_sha256
    ):
        raise BackupVerificationError("备份 manifest digest 无效。")
    if manifest.backup_size_bytes < 1 or manifest.page_count < 1 or manifest.page_size < 1:
        raise BackupVerificationError("备份 manifest SQLite 大小元数据无效。")
    if manifest.source_schema_version > manifest.target_schema_version:
        raise BackupVerificationError("备份 manifest schema 版本关系无效。")
    try:
        parsed_time = datetime.fromisoformat(manifest.created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise BackupVerificationError("备份 manifest created_at 无效。") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() != timedelta(0):
        raise BackupVerificationError("备份 manifest created_at 必须使用 UTC。")
    if _utc_timestamp(parsed_time) != manifest.created_at:
        raise BackupVerificationError("备份 manifest created_at 不是规范格式。")


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BackupError("备份 authority clock 必须包含 UTC offset。")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupVerificationError("备份文件无法读取。") from exc
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_staging_directory(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError:
        pass


__all__ = [
    "BACKUP_MANIFEST_SCHEMA_VERSION",
    "BACKUP_SPACE_FLOOR_BYTES",
    "BackupError",
    "BackupSpaceError",
    "BackupVerificationError",
    "SQLiteBackupManager",
    "SQLiteBackupManifest",
    "SQLiteBackupPlan",
    "SQLiteBackupReceipt",
]
