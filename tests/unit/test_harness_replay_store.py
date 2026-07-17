from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.replay import capture_replay_baseline
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
)

NOW = "2026-07-15T10:00:00+00:00"
LATER = "2026-07-15T10:01:00+00:00"


async def _stored_run(store: HarnessStore, workspace: Path, *, run_id: str = "replay-store"):
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id="replay-session",
        task_kind=HarnessTaskKind.ANALYSIS,
        objective="持久化安全回放基线",
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before="a" * 64,
        started_at=NOW,
    )
    return contract


def _receipt(contract: HarnessCompletionContract) -> HarnessCompletionReceipt:
    return HarnessCompletionReceipt(
        run_id=contract.run_id,
        status="completed_verified",
        task_kind=contract.task_kind,
        changed_files=(),
        checks=(),
        criteria=(),
        warnings=(),
        tree_fingerprint="b" * 64,
    )


@pytest.mark.asyncio
async def test_finish_run_captures_baseline_readable_by_new_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    contract = await _stored_run(store, workspace)

    await store.finish_run(
        run_id=contract.run_id,
        receipt=_receipt(contract),
        completed_at=LATER,
    )

    baseline = await HarnessStore(db_path).get_replay_baseline(contract.run_id)
    assert baseline is not None
    assert baseline.run_id == contract.run_id
    assert baseline.manifest_sha256
    assert baseline.explanation_sha256
    assert baseline.created_at == LATER


@pytest.mark.asyncio
async def test_replay_baseline_is_idempotent_and_immutable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    contract = await _stored_run(store, workspace)
    run = await store.get_run(contract.run_id)
    assert run is not None
    payload = capture_replay_baseline(run, workspace_root=workspace)

    first = await store.record_replay_baseline(payload, created_at=NOW)
    second = await store.record_replay_baseline(payload, created_at=NOW)

    assert first == second
    conflict = replace(payload, rule_version="conflicting-rule")
    with pytest.raises(HarnessStoreConflictError, match="不可变"):
        await store.record_replay_baseline(conflict, created_at=NOW)


@pytest.mark.asyncio
async def test_v1_database_migrates_additively_and_cascades_delete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    first = HarnessStore(db_path)
    contract = await _stored_run(first, workspace)
    run = await first.get_run(contract.run_id)
    assert run is not None
    payload = capture_replay_baseline(run, workspace_root=workspace)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE IF EXISTS harness_replay_baselines")
        db.execute("PRAGMA user_version = 1")

    migrated = HarnessStore(db_path)
    await migrated.record_replay_baseline(payload, created_at=NOW)
    with sqlite3.connect(db_path) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("harness_replay_baselines",),
        ).fetchone()
    assert version == HARNESS_STORE_SCHEMA_VERSION == 2
    assert table is not None

    assert await migrated.delete_session_records(workspace, contract.session_id) == 1
    assert await migrated.get_replay_baseline(contract.run_id) is None
