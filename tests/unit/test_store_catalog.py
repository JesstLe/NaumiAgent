"""Focused contracts for ARC-05.1 physical Store Catalog."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.execution_grants import EXECUTION_GRANT_SCHEMA_VERSION
from naumi_agent.daemons.worker_registry import WORKER_REGISTRY_SCHEMA_VERSION
from naumi_agent.evolution.store import EVOLUTION_STORE_SCHEMA_VERSION
from naumi_agent.harness.store import HARNESS_STORE_SCHEMA_VERSION
from naumi_agent.persistence.store_catalog import (
    CatalogStatus,
    DataSensitivity,
    RetentionPolicy,
    StorageKind,
    StoreCatalogError,
    StoreState,
    VersionStrategy,
    build_store_catalog,
    inspect_store_catalog,
)


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.memory.session_db_path = str(tmp_path / "runtime" / "sessions.db")
    config.memory.vector_db_path = str(tmp_path / "runtime" / "chroma")
    config.workspace_root = str(tmp_path / "workspace")
    return config


def _write_sqlite(path: Path, *, user_version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {user_version}")


def test_default_catalog_covers_physical_stores_without_duplicate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))

    definitions = build_store_catalog(_config(tmp_path))

    assert len(definitions) == 14
    assert len({item.store_id for item in definitions}) == len(definitions)
    assert len({item.path for item in definitions}) == len(definitions)
    assert all(item.path.is_absolute() for item in definitions)
    core = next(item for item in definitions if item.store_id == "runtime.core")
    assert core.kind is StorageKind.SQLITE
    assert set(core.owners) == {"memory.sessions", "tasks", "workbench"}
    assert core.sensitivity is DataSensitivity.RESTRICTED
    assert core.retention is RetentionPolicy.USER_MANAGED
    harness = next(item for item in definitions if item.store_id == "harness.evidence")
    assert harness.version_strategy is VersionStrategy.SQLITE_USER_VERSION
    assert harness.supported_schema_version == HARNESS_STORE_SCHEMA_VERSION
    workers = next(
        item for item in definitions if item.store_id == "runtime.worker_registry"
    )
    assert workers.path == (tmp_path / "runtime" / "worker-registry.db").resolve()
    assert workers.supported_schema_version == WORKER_REGISTRY_SCHEMA_VERSION == 1
    assert workers.retention is RetentionPolicy.AUDIT_LONG_TERM
    grants = next(
        item for item in definitions if item.store_id == "runtime.execution_grants"
    )
    assert grants.path == (tmp_path / "runtime" / "execution-grants.db").resolve()
    assert grants.supported_schema_version == EXECUTION_GRANT_SCHEMA_VERSION == 1
    assert grants.retention is RetentionPolicy.AUDIT_LONG_TERM
    evolution = next(
        item for item in definitions if item.store_id == "evolution.candidates"
    )
    assert evolution.version_strategy is VersionStrategy.SQLITE_USER_VERSION
    assert evolution.supported_schema_version == EVOLUTION_STORE_SCHEMA_VERSION == 1
    assert evolution.retention is RetentionPolicy.AUDIT_LONG_TERM
    assert evolution.sensitivity is DataSensitivity.RESTRICTED


def test_absent_lazy_stores_are_read_only_and_do_not_create_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    definitions = build_store_catalog(_config(tmp_path))
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))

    report = inspect_store_catalog(definitions)

    after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    assert before == after == ()
    assert report.status is CatalogStatus.PASS
    assert all(item.state is StoreState.ABSENT for item in report.stores)


def test_catalog_distinguishes_current_unversioned_and_future_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    definitions = build_store_catalog(_config(tmp_path))
    core = next(item for item in definitions if item.store_id == "runtime.core")
    harness = next(item for item in definitions if item.store_id == "harness.evidence")
    _write_sqlite(core.path, user_version=0)
    _write_sqlite(harness.path, user_version=HARNESS_STORE_SCHEMA_VERSION)

    report = inspect_store_catalog((core, harness))

    observations = {item.definition.store_id: item for item in report.stores}
    assert observations["runtime.core"].state is StoreState.LEGACY_UNVERSIONED
    assert observations["runtime.core"].status is CatalogStatus.WARN
    assert observations["harness.evidence"].state is StoreState.READY
    assert (
        observations["harness.evidence"].observed_schema_version
        == HARNESS_STORE_SCHEMA_VERSION
    )

    future_path = tmp_path / "future-harness.db"
    _write_sqlite(future_path, user_version=HARNESS_STORE_SCHEMA_VERSION + 1)
    future = replace(harness, path=future_path)
    future_observation = inspect_store_catalog((future,)).stores[0]
    assert future_observation.state is StoreState.UNSUPPORTED_NEWER
    assert future_observation.status is CatalogStatus.ERROR


def test_catalog_reports_corrupt_sqlite_and_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    definitions = build_store_catalog(_config(tmp_path))
    harness = next(item for item in definitions if item.store_id == "harness.evidence")
    background = next(item for item in definitions if item.store_id == "tasks.background")
    harness.path.parent.mkdir(parents=True, exist_ok=True)
    harness.path.write_bytes(b"not sqlite")
    background.path.parent.mkdir(parents=True, exist_ok=True)
    background.path.write_text("{broken", encoding="utf-8")

    report = inspect_store_catalog((harness, background))

    observations = {item.definition.store_id: item for item in report.stores}
    assert observations["harness.evidence"].state is StoreState.CORRUPT
    assert observations["tasks.background"].state is StoreState.CORRUPT
    assert report.status is CatalogStatus.ERROR

    with background.path.open("wb") as stream:
        stream.seek(8 * 1024 * 1024)
        stream.write(b"x")
    oversized = inspect_store_catalog((background,)).stores[0]
    assert oversized.state is StoreState.UNREADABLE
    assert "json_read_limit_exceeded" in oversized.issue_codes


def test_catalog_validates_embedded_json_versions_and_rejects_ambiguous_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    definitions = build_store_catalog(_config(tmp_path))
    background = next(item for item in definitions if item.store_id == "tasks.background")
    versioned = replace(
        background,
        version_strategy=VersionStrategy.JSON_SCHEMA_VERSION,
        supported_schema_version=2,
    )
    versioned.path.parent.mkdir(parents=True, exist_ok=True)
    versioned.path.write_text('{"schema_version": 1, "tasks": {}}', encoding="utf-8")

    observation = inspect_store_catalog((versioned,)).stores[0]

    assert observation.state is StoreState.UPGRADE_REQUIRED
    assert observation.observed_schema_version == 1
    with pytest.raises(StoreCatalogError, match="不能为空"):
        inspect_store_catalog(())
    duplicate = replace(versioned, store_id="tasks.background.copy")
    with pytest.raises(StoreCatalogError, match="路径重复"):
        inspect_store_catalog((versioned, duplicate))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_catalog_warns_without_mutating_overly_open_sensitive_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    harness = next(
        item
        for item in build_store_catalog(_config(tmp_path))
        if item.store_id == "harness.evidence"
    )
    _write_sqlite(harness.path, user_version=HARNESS_STORE_SCHEMA_VERSION)
    harness.path.chmod(0o644)

    observation = inspect_store_catalog((harness,)).stores[0]

    assert observation.state is StoreState.READY
    assert observation.status is CatalogStatus.WARN
    assert "permissions_too_open" in observation.issue_codes
    assert (harness.path.stat().st_mode & 0o777) == 0o644
