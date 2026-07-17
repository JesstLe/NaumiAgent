from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.persistence.migrations import (
    MigrationCancelledError,
    MigrationExecutionError,
    MigrationLockedError,
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
        description="迁移运行器真实 SQLite fixture",
    )


def _database(path: Path, *, version: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO items(value) VALUES (?)",
            [("one",), ("two",), ("three",)],
        )
        connection.execute(f"PRAGMA user_version = {version}")


def _steps(*, fail_second: bool = False) -> tuple[MigrationStep, ...]:
    def first(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE items ADD COLUMN note TEXT NOT NULL DEFAULT ''")

    def second(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE INDEX idx_items_value ON items(value)")
        if fail_second:
            raise RuntimeError("private migration failure")

    return (
        MigrationStep(
            store_id="test.sqlite",
            from_version=0,
            to_version=1,
            description="items 增加 note",
            apply=first,
            estimate_query="SELECT COUNT(*) FROM items",
        ),
        MigrationStep(
            store_id="test.sqlite",
            from_version=1,
            to_version=2,
            description="items value 索引",
            apply=second,
            irreversible=True,
        ),
    )


def _runner(path: Path, *, fail_second: bool = False) -> MigrationRunner:
    return MigrationRunner(
        MigrationRegistry(_steps(fail_second=fail_second)),
        busy_timeout_seconds=0.05,
    )


def test_dry_run_is_byte_identical_and_reports_plan(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)
    before = (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
        path.stat().st_mtime_ns,
    )

    plan = _runner(path).plan(_definition(path))

    after = (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
        path.stat().st_mtime_ns,
    )
    assert before == after
    assert plan.current_version == 0
    assert plan.target_version == 2
    assert [step.to_version for step in plan.steps] == [1, 2]
    assert plan.estimated_rows == 3
    assert plan.size_bytes == path.stat().st_size
    assert plan.irreversible_steps == ("1->2",)


def test_apply_is_transactional_progress_visible_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)
    progress = []
    runner = _runner(path)

    result = runner.apply(_definition(path), progress=progress.append)
    repeated = runner.apply(_definition(path))

    assert result.changed is True
    assert result.applied_versions == (1, 2)
    assert [event.phase for event in progress] == ["started", "step", "step", "completed"]
    assert repeated.changed is False
    assert repeated.applied_versions == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
        assert "note" in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_items_value'"
        ).fetchone()[0] == 1


def test_failure_rolls_back_without_leaking_private_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)

    with pytest.raises(MigrationExecutionError, match="1 -> 2") as captured:
        _runner(path, fail_second=True).apply(_definition(path))

    assert "private migration failure" not in str(captured.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
        assert "note" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_items_value'"
        ).fetchone()[0] == 0


def test_cancel_between_steps_rolls_back_entire_transaction(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)
    completed_steps = 0

    def on_progress(event: object) -> None:
        nonlocal completed_steps
        if getattr(event, "phase", "") == "step":
            completed_steps += 1

    with pytest.raises(MigrationCancelledError):
        _runner(path).apply(
            _definition(path),
            progress=on_progress,
            cancelled=lambda: completed_steps >= 1,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "note" not in {
            row[1] for row in connection.execute("PRAGMA table_info(items)")
        }


def test_step_cannot_escape_atomic_transaction_with_commit(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)

    def unsafe(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE items ADD COLUMN escaped TEXT")
        connection.commit()

    registry = MigrationRegistry(
        (
            MigrationStep(
                store_id="test.sqlite",
                from_version=0,
                to_version=1,
                description="不得自行提交",
                apply=unsafe,
            ),
        )
    )

    with pytest.raises(MigrationExecutionError, match="0 -> 1"):
        MigrationRunner(registry).apply(_definition(path, target=1))

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "escaped" not in {
            row[1] for row in connection.execute("PRAGMA table_info(items)")
        }


def test_busy_database_returns_typed_lock_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(MigrationLockedError):
            _runner(path).apply(_definition(path))
    finally:
        holder.rollback()
        holder.close()


def test_registry_rejects_gaps_and_runner_rejects_future_or_corrupt_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    _database(path, version=3)
    runner = _runner(path)
    with pytest.raises(MigrationExecutionError, match="高于当前支持版本"):
        runner.plan(_definition(path))

    path.write_bytes(b"not sqlite")
    with pytest.raises(MigrationExecutionError, match="无法读取"):
        runner.plan(_definition(path))

    gap = replace(_steps()[1], from_version=2, to_version=3)
    with pytest.raises(ValueError, match="不连续"):
        MigrationRegistry((_steps()[0], gap))


def test_plan_rejects_absent_store_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"

    with pytest.raises(MigrationExecutionError, match="不存在"):
        _runner(path).plan(_definition(path))

    assert not path.exists()
