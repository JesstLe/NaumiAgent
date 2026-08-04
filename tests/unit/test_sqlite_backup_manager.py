from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.persistence import backups
from naumi_agent.persistence.backups import (
    BACKUP_MANIFEST_SCHEMA_VERSION,
    BackupError,
    BackupSpaceError,
    BackupVerificationError,
    SQLiteBackupManager,
)
from naumi_agent.persistence.migrations import (
    MigrationRegistry,
    MigrationRunner,
    MigrationStep,
)
from naumi_agent.persistence.store_catalog import (
    DataSensitivity,
    RetentionPolicy,
    StorageKind,
    StoreDefinition,
    VersionStrategy,
)

NOW = datetime(2026, 8, 5, 6, 30, tzinfo=UTC)
BACKUP_ID = "arc5b_" + "a" * 24


def _definition(path: Path, *, target: int = 2) -> StoreDefinition:
    return StoreDefinition(
        store_id="test.sqlite",
        path=path,
        kind=StorageKind.SQLITE,
        owners=("tests",),
        version_strategy=VersionStrategy.SQLITE_USER_VERSION,
        supported_schema_version=target,
        sensitivity=DataSensitivity.RESTRICTED,
        retention=RetentionPolicy.USER_MANAGED,
        lazy=False,
        description="ARC-05.3a 真实 SQLite 备份 fixture",
    )


def _database(path: Path, *, version: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO items(value) VALUES (?)",
            [("one",), ("two",), ("three",)],
        )
        connection.execute(f"PRAGMA user_version = {version}")


def _manager(**changes) -> SQLiteBackupManager:
    values = {
        "clock": lambda: NOW,
        "backup_id_factory": lambda: BACKUP_ID,
    }
    values.update(changes)
    return SQLiteBackupManager(**values)


def _fingerprint(path: Path) -> tuple[int, int, str]:
    return (
        path.stat().st_size,
        path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_plan_is_read_only_and_reports_space_and_permissions(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)
    before = _fingerprint(source)

    plan = _manager(free_space_probe=lambda _: 10_000_000).plan(
        _definition(source),
        backup_root=backup_root,
    )

    assert _fingerprint(source) == before
    assert not backup_root.exists()
    assert plan.store_id == "test.sqlite"
    assert plan.source_schema_version == 1
    assert plan.target_schema_version == 2
    assert plan.logical_size_bytes > 0
    assert plan.required_free_bytes > plan.logical_size_bytes
    assert plan.available_free_bytes == 10_000_000
    assert plan.sufficient_space is True
    assert plan.destination_permission_mode is None
    assert plan.destination_private is True


def test_create_captures_committed_wal_state_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO items(value) VALUES ('wal-committed')")
        writer.commit()
        durable_source_files = [
            source,
            source.with_name(f"{source.name}-wal"),
        ]
        shared_memory = source.with_name(f"{source.name}-shm")
        assert all(path.exists() for path in [*durable_source_files, shared_memory])
        before = {path.name: _fingerprint(path) for path in durable_source_files}

        receipt = _manager().create(_definition(source), backup_root=backup_root)

        after = {path.name: _fingerprint(path) for path in durable_source_files}
        assert after == before
        assert shared_memory.exists()
        with sqlite3.connect(receipt.backup_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        writer.close()

    manifest = receipt.manifest
    assert manifest.schema_version == BACKUP_MANIFEST_SCHEMA_VERSION
    assert manifest.backup_id == BACKUP_ID
    assert manifest.store_id == "test.sqlite"
    assert manifest.created_at == "2026-08-05T06:30:00+00:00"
    assert manifest.backup_sha256 == hashlib.sha256(
        receipt.backup_path.read_bytes()
    ).hexdigest()
    assert str(source.resolve()) not in receipt.manifest_path.read_text(encoding="utf-8")
    assert len(manifest.source_path_sha256) == 64
    assert not list((backup_root / "test.sqlite").glob(".*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(receipt.backup_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(receipt.backup_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(receipt.manifest_path.stat().st_mode) == 0o600


def test_create_fails_closed_on_space_and_broad_destination_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)
    manager = _manager(free_space_probe=lambda _: 0)

    plan = manager.plan(_definition(source), backup_root=backup_root)
    assert plan.sufficient_space is False
    with pytest.raises(BackupSpaceError, match="空间不足"):
        manager.create(_definition(source), backup_root=backup_root)
    assert not backup_root.exists()

    if os.name == "posix":
        backup_root.mkdir(mode=0o755)
        backup_root.chmod(0o755)
        permissions = _manager().plan(_definition(source), backup_root=backup_root)
        assert permissions.destination_permission_mode == 0o755
        assert permissions.destination_private is False
        with pytest.raises(BackupVerificationError, match="权限"):
            _manager().create(_definition(source), backup_root=backup_root)
        assert list(backup_root.iterdir()) == []


def test_publish_failure_removes_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("private publish failure")

    monkeypatch.setattr(backups.os, "replace", fail_replace)
    with pytest.raises(BackupError, match="未发布不完整快照") as captured:
        _manager().create(_definition(source), backup_root=backup_root)

    assert "private publish failure" not in str(captured.value)
    assert list((backup_root / "test.sqlite").iterdir()) == []


def test_final_verification_failure_withdraws_published_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)
    manager = _manager()

    def fail_verify(_path: Path) -> None:
        raise BackupVerificationError("forced final verification failure")

    monkeypatch.setattr(manager, "verify", fail_verify)
    with pytest.raises(BackupVerificationError, match="forced final"):
        manager.create(_definition(source), backup_root=backup_root)

    assert list((backup_root / "test.sqlite").iterdir()) == []


def test_verify_rejects_tamper_unknown_fields_and_unsafe_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    _database(source)
    receipt = _manager().create(_definition(source), backup_root=tmp_path / "backups")
    original_manifest = receipt.manifest_path.read_bytes()
    original_backup = receipt.backup_path.read_bytes()

    with receipt.backup_path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(BackupVerificationError, match="大小|digest"):
        _manager().verify(receipt.backup_dir)

    receipt.backup_path.write_bytes(original_backup)
    payload = json.loads(original_manifest)
    payload["private_sql"] = "SELECT secret"
    receipt.manifest_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        receipt.manifest_path.chmod(0o600)
    with pytest.raises(BackupVerificationError, match="未知字段"):
        _manager().verify(receipt.backup_dir)

    receipt.manifest_path.write_bytes(original_manifest)
    if os.name == "posix":
        receipt.manifest_path.chmod(0o644)
        with pytest.raises(BackupVerificationError, match="权限"):
            _manager().verify(receipt.backup_dir)


def test_symlink_and_invalid_source_versions_fail_without_output(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _database(source, version=3)
    backup_root = tmp_path / "backups"
    with pytest.raises(BackupVerificationError, match="高于当前支持版本"):
        _manager().plan(_definition(source, target=2), backup_root=backup_root)
    assert not backup_root.exists()

    link = tmp_path / "source-link.db"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    with pytest.raises(BackupVerificationError, match="符号链接"):
        _manager().plan(_definition(link, target=3), backup_root=backup_root)


def test_missing_corrupt_and_non_sqlite_sources_fail_closed(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    missing = tmp_path / "missing.db"
    with pytest.raises(BackupVerificationError, match="不存在"):
        _manager().plan(_definition(missing), backup_root=backup_root)

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(BackupVerificationError, match="无法读取|损坏"):
        _manager().plan(_definition(corrupt), backup_root=backup_root)

    directory_definition = replace(_definition(corrupt), kind=StorageKind.DIRECTORY)
    with pytest.raises(BackupVerificationError, match="仅支持 SQLite"):
        _manager().plan(directory_definition, backup_root=backup_root)
    assert not backup_root.exists()


def test_invalid_clock_backup_id_and_backup_root_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    _database(source)

    naive_clock_root = tmp_path / "naive-clock"
    with pytest.raises(BackupError, match="UTC offset"):
        _manager(clock=lambda: datetime(2026, 8, 5, 6, 30)).create(
            _definition(source),
            backup_root=naive_clock_root,
        )
    assert not naive_clock_root.exists()

    invalid_id_root = tmp_path / "invalid-id"
    with pytest.raises(BackupError, match="无效身份"):
        _manager(backup_id_factory=lambda: "../escape").create(
            _definition(source),
            backup_root=invalid_id_root,
        )
    assert not invalid_id_root.exists()

    target = tmp_path / "backup-target"
    target.mkdir()
    backup_link = tmp_path / "backup-link"
    try:
        backup_link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    with pytest.raises(BackupVerificationError, match="根目录不能是符号链接"):
        _manager().plan(_definition(source), backup_root=backup_link)
    assert list(target.iterdir()) == []


def test_dangling_store_and_backup_identity_symlinks_are_not_overwritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    _database(source)
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    if os.name == "posix":
        backup_root.chmod(0o700)

    store_root = backup_root / "test.sqlite"
    try:
        store_root.symlink_to(tmp_path / "missing-store-root", target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    with pytest.raises(BackupError, match="Store 备份目录类型无效"):
        _manager().create(_definition(source), backup_root=backup_root)
    assert store_root.is_symlink()

    store_root.unlink()
    store_root.mkdir(mode=0o700)
    if os.name == "posix":
        store_root.chmod(0o700)
    final_identity = store_root / BACKUP_ID
    final_identity.symlink_to(tmp_path / "missing-backup", target_is_directory=True)
    with pytest.raises(BackupError, match="备份身份已存在"):
        _manager().create(_definition(source), backup_root=backup_root)
    assert final_identity.is_symlink()


def test_verify_rejects_noncanonical_time_and_invalid_schema_relationship(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    _database(source)
    receipt = _manager().create(_definition(source), backup_root=tmp_path / "backups")
    original = receipt.manifest_path.read_bytes()

    payload = json.loads(original)
    payload["created_at"] = "2026-08-05T08:30:00+02:00"
    receipt.manifest_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        receipt.manifest_path.chmod(0o600)
    with pytest.raises(BackupVerificationError, match="必须使用 UTC"):
        _manager().verify(receipt.backup_dir)

    payload = json.loads(original)
    payload["source_schema_version"] = 3
    payload["target_schema_version"] = 2
    receipt.manifest_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        receipt.manifest_path.chmod(0o600)
    with pytest.raises(BackupVerificationError, match="版本关系无效"):
        _manager().verify(receipt.backup_dir)


def test_same_backup_identity_has_one_concurrent_winner(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source)
    manager = _manager()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(manager.create, _definition(source), backup_root=backup_root)
            for _ in range(2)
        ]
    results = []
    errors = []
    for future in futures:
        try:
            results.append(future.result())
        except BackupError as exc:
            errors.append(exc)

    assert len(results) == 1
    assert len(errors) == 1
    assert results[0].manifest.backup_id == BACKUP_ID
    assert _manager().verify(results[0].backup_dir).manifest.backup_id == BACKUP_ID
    assert not list((backup_root / "test.sqlite").glob(".*.tmp"))


def test_backup_remains_a_verified_pre_migration_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    backup_root = tmp_path / "backups"
    _database(source, version=1)
    definition = _definition(source, target=2)
    receipt = _manager().create(definition, backup_root=backup_root)

    def migrate(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE items ADD COLUMN note TEXT NOT NULL DEFAULT ''")

    result = MigrationRunner(
        MigrationRegistry(
            (
                MigrationStep(
                    store_id="test.sqlite",
                    from_version=1,
                    to_version=2,
                    description="items 增加 note",
                    apply=migrate,
                ),
            )
        )
    ).apply(definition)

    assert result.changed is True
    assert _manager().verify(receipt.backup_dir).manifest.source_schema_version == 1
    with sqlite3.connect(source) as current, sqlite3.connect(
        receipt.backup_path
    ) as previous:
        assert current.execute("PRAGMA user_version").fetchone()[0] == 2
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "note" in {
            row[1] for row in current.execute("PRAGMA table_info(items)")
        }
        assert "note" not in {
            row[1] for row in previous.execute("PRAGMA table_info(items)")
        }
