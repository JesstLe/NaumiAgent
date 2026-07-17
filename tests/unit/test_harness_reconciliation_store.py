"""HAR-06.2a durable reconciliation state machine tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.completion import HarnessEvidenceRef
from naumi_agent.harness.models import (
    HarnessAcceptanceCriterion,
    HarnessCompletionContract,
    HarnessTaskKind,
)
from naumi_agent.harness.reconciliation import (
    ReconciliationArtifactGcStatus,
    ReconciliationArtifactKind,
    SessionReconciliationState,
    SessionReconciliationTransitionError,
)
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)

NOW = "2026-07-17T14:00:00+08:00"
LATER = "2026-07-17T14:01:00+08:00"


def _contract(run_id: str, session_id: str) -> HarnessCompletionContract:
    return HarnessCompletionContract(
        run_id=run_id,
        session_id=session_id,
        task_kind=HarnessTaskKind.CHANGE,
        objective="验证协调状态机",
        acceptance_criteria=(
            HarnessAcceptanceCriterion(
                id="reconciled",
                description="派生数据可被安全协调",
            ),
        ),
    )


async def _seed_run(
    store: HarnessStore,
    workspace: Path,
    *,
    run_id: str,
    session_id: str,
) -> None:
    await store.start_run(
        workspace_root=workspace,
        contract=_contract(run_id, session_id),
        tree_fingerprint_before="a" * 64,
        started_at=NOW,
    )
    await store.record_check(
        result=HarnessCheckResult(
            run_id=run_id,
            check_id="unit",
            status=HarnessCheckStatus.PASSED,
            tree_fingerprint="b" * 64,
            profile_digest="c" * 64,
            message="检查通过",
            output="ok",
            exit_code=0,
            duration_ms=1,
        ),
        argv=("python", "-V"),
        cwd=workspace,
        started_at=NOW,
        completed_at=LATER,
        artifact_path=f"artifacts/{run_id}/unit.txt",
    )
    await store.record_evidence(
        run_id=run_id,
        evidence=HarnessEvidenceRef(
            id=f"evidence-{run_id}",
            kind="check_output",
            summary="测试证据",
            criterion_ids=("reconciled",),
        ),
        uri=f"artifact://{run_id}/unit.txt",
        sha256="d" * 64,
        summary={"exit_code": 0},
        producer="harness_check",
        created_at=LATER,
    )


@pytest.mark.asyncio
async def test_prepare_is_idempotent_and_snapshots_typed_artifact_refs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    await _seed_run(store, workspace, run_id="run-1", session_id="session-1")

    first = await store.prepare_session_delete_reconciliation(
        request_id="delete-request-1",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    replay = await store.prepare_session_delete_reconciliation(
        request_id="delete-request-1",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )

    assert first == replay
    assert first.state is SessionReconciliationState.PREPARED
    assert first.run_count == 1
    assert {(ref.kind, ref.value) for ref in first.artifact_references} == {
        (ReconciliationArtifactKind.CHECK_PATH, "artifacts/run-1/unit.txt"),
        (ReconciliationArtifactKind.EVIDENCE_URI, "artifact://run-1/unit.txt"),
    }
    restored = await HarnessStore(db_path).get_session_delete_reconciliation(
        "delete-request-1"
    )
    assert restored == first


@pytest.mark.asyncio
async def test_prepare_rejects_idempotency_key_reuse_for_another_scope(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await store.prepare_session_delete_reconciliation(
        request_id="same-request",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )

    with pytest.raises(HarnessStoreConflictError, match="幂等键"):
        await store.prepare_session_delete_reconciliation(
            request_id="same-request",
            workspace_root=other,
            session_id="session-1",
            actor=LifecycleActor.USER,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_reconcile_requires_session_commit_and_is_workspace_scoped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _seed_run(store, workspace, run_id="run-target", session_id="shared")
    await _seed_run(store, other, run_id="run-other", session_id="shared")
    await store.prepare_session_delete_reconciliation(
        request_id="delete-shared",
        workspace_root=workspace,
        session_id="shared",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )

    with pytest.raises(SessionReconciliationTransitionError, match="Session"):
        await store.reconcile_session_delete_records("delete-shared", updated_at=LATER)

    committed = await store.mark_session_delete_committed(
        "delete-shared",
        updated_at=LATER,
    )
    assert committed.state is SessionReconciliationState.SESSION_COMMITTED
    reconciled = await store.reconcile_session_delete_records(
        "delete-shared",
        updated_at=LATER,
    )
    replay = await store.reconcile_session_delete_records(
        "delete-shared",
        updated_at=LATER,
    )

    assert reconciled == replay
    assert reconciled.state is SessionReconciliationState.RECORDS_COMMITTED
    assert reconciled.artifact_gc_status is ReconciliationArtifactGcStatus.PENDING
    assert reconciled.deleted_run_count == 1
    assert await store.get_run("run-target") is None
    assert await store.get_run("run-other") is not None
    assert len(reconciled.artifact_references) == 2

    with pytest.raises(SessionReconciliationTransitionError, match="回退"):
        await store.mark_session_delete_committed(
            "delete-shared",
            updated_at=LATER,
        )


@pytest.mark.asyncio
async def test_artifact_gc_is_durable_idempotent_and_preserves_shared_refs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "artifacts" / "shared.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("shared", encoding="utf-8")
    store = HarnessStore(tmp_path / "harness.db")
    await _seed_run(store, workspace, run_id="target", session_id="session-target")
    await _seed_run(store, workspace, run_id="survivor", session_id="session-survivor")
    await store.prepare_session_delete_reconciliation(
        request_id="gc-target",
        workspace_root=workspace,
        session_id="session-target",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE harness_checks SET artifact_path = ? WHERE run_id IN (?, ?)",
            ("artifacts/shared.txt", "target", "survivor"),
        )
        db.execute(
            "UPDATE harness_session_reconciliations SET artifact_references_json = ? "
            "WHERE request_id = ?",
            (
                '[{"kind":"check_path","value":"artifacts/shared.txt"}]',
                "gc-target",
            ),
        )
        db.commit()
    await store.mark_session_delete_committed("gc-target", updated_at=LATER)
    await store.reconcile_session_delete_records("gc-target", updated_at=LATER)

    first = await store.reconcile_session_artifacts(
        "gc-target",
        updated_at=LATER,
    )
    replay = await store.reconcile_session_artifacts(
        "gc-target",
        updated_at=LATER,
    )

    assert first == replay
    assert first.artifact_gc_status is ReconciliationArtifactGcStatus.COMPLETED
    assert first.artifact_shared_count == 1
    assert first.artifact_deleted_count == 0
    assert artifact.read_text(encoding="utf-8") == "shared"


@pytest.mark.asyncio
async def test_records_committed_remains_pending_until_artifact_gc(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await store.prepare_session_delete_reconciliation(
        request_id="pending-gc",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    await store.mark_session_delete_committed("pending-gc", updated_at=LATER)
    await store.reconcile_session_delete_records("pending-gc", updated_at=LATER)

    pending = await HarnessStore(store.db_path).list_pending_session_reconciliations()

    assert [record.request_id for record in pending] == ["pending-gc"]
    await store.reconcile_session_artifacts("pending-gc", updated_at=LATER)
    assert await store.list_pending_session_reconciliations() == ()


@pytest.mark.asyncio
async def test_concurrent_prepare_across_store_instances_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    first = HarnessStore(db_path)
    second = HarnessStore(db_path)

    records = await asyncio.gather(
        first.prepare_session_delete_reconciliation(
            request_id="concurrent-request",
            workspace_root=workspace,
            session_id="session-1",
            actor=LifecycleActor.USER,
            created_at=NOW,
        ),
        second.prepare_session_delete_reconciliation(
            request_id="concurrent-request",
            workspace_root=workspace,
            session_id="session-1",
            actor=LifecycleActor.USER,
            created_at=NOW,
        ),
    )

    assert records[0] == records[1]


@pytest.mark.asyncio
async def test_corrupt_reconciliation_references_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    await store.prepare_session_delete_reconciliation(
        request_id="corrupt-request",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE harness_session_reconciliations "
            "SET artifact_references_json = ? WHERE request_id = ?",
            ("{", "corrupt-request"),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(db_path).get_session_delete_reconciliation(
            "corrupt-request"
        )


@pytest.mark.asyncio
async def test_v2_database_migrates_additively_without_losing_runs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    original = HarnessStore(db_path)
    await _seed_run(original, workspace, run_id="legacy-run", session_id="legacy")
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE harness_session_reconciliations")
        db.execute("PRAGMA user_version = 2")
        db.commit()

    migrated = HarnessStore(db_path)
    prepared = await migrated.prepare_session_delete_reconciliation(
        request_id="migrated-request",
        workspace_root=workspace,
        session_id="legacy",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )

    assert prepared.run_count == 1
    assert await migrated.get_run("legacy-run") is not None
    with sqlite3.connect(db_path) as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 5


@pytest.mark.asyncio
async def test_v4_database_backfills_pending_gc_and_migrates_failure_stage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    current = HarnessStore(db_path)
    await current.prepare_session_delete_reconciliation(
        request_id="legacy-v4",
        workspace_root=workspace,
        session_id="session-v4",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    await current.mark_session_delete_committed("legacy-v4", updated_at=LATER)
    await current.reconcile_session_delete_records("legacy-v4", updated_at=LATER)
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            DROP TABLE harness_session_artifact_gc;
            DROP TABLE harness_session_reconciliation_failure_events;
            DROP TABLE harness_session_reconciliation_tombstones;
            CREATE TABLE harness_session_reconciliation_tombstones (
                request_id TEXT PRIMARY KEY,
                policy TEXT NOT NULL CHECK (policy = 'delete'),
                stage TEXT NOT NULL CHECK (
                    stage IN ('session_delete', 'harness_records')
                ),
                error_code TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                next_retry_at TEXT NOT NULL,
                lease_owner TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                last_failure_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE harness_session_reconciliation_failure_events (
                failure_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                stage TEXT NOT NULL CHECK (
                    stage IN ('session_delete', 'harness_records')
                ),
                error_code TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            INSERT INTO harness_session_reconciliation_tombstones VALUES (
                'legacy-v4', 'delete', 'harness_records',
                'infrastructure_error', 'pending', 1, 8,
                '2026-07-17T06:00:05+00:00', '', '', 'legacy-v4-old-failure',
                '2026-07-17T06:00:00+00:00', '2026-07-17T06:00:00+00:00'
            );
            INSERT INTO harness_session_reconciliation_failure_events VALUES (
                'legacy-v4-old-failure', 'legacy-v4', 'harness_records',
                'infrastructure_error', '2026-07-17T06:00:00+00:00'
            );
            PRAGMA user_version = 4;
            """
        )
        db.commit()

    migrated = HarnessStore(db_path)
    pending = await migrated.list_pending_session_reconciliations()
    tombstone = await migrated.record_reconciliation_failure(
        request_id="legacy-v4",
        failure_id="legacy-v4-artifact-failure",
        stage="artifact_gc",
        error_code="infrastructure_error",
        occurred_at=LATER,
    )

    assert [record.request_id for record in pending] == ["legacy-v4"]
    assert pending[0].artifact_gc_status is ReconciliationArtifactGcStatus.PENDING
    assert tombstone.stage.value == "artifact_gc"
    assert tombstone.attempt_count == 2
    with sqlite3.connect(db_path) as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 5
        assert db.execute(
            "SELECT COUNT(*) FROM harness_session_reconciliation_failure_events"
        ).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_pending_reconciliations_are_discoverable_and_bounded(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    for request_id in ("request-a", "request-b"):
        await store.prepare_session_delete_reconciliation(
            request_id=request_id,
            workspace_root=workspace,
            session_id=f"session-{request_id}",
            actor=LifecycleActor.USER,
            created_at=NOW,
        )
    await store.mark_session_delete_committed("request-b", updated_at=LATER)
    await store.reconcile_session_delete_records("request-b", updated_at=LATER)

    pending = await HarnessStore(store.db_path).list_pending_session_reconciliations(
        limit=1
    )

    assert [record.request_id for record in pending] == ["request-a"]
    assert await HarnessStore(
        tmp_path / "missing.db"
    ).list_pending_session_reconciliations() == ()
    with pytest.raises(ValueError, match="limit"):
        await store.list_pending_session_reconciliations(limit=0)


@pytest.mark.asyncio
async def test_forward_transition_rejects_updated_at_regression(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await store.prepare_session_delete_reconciliation(
        request_id="time-request",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=LATER,
    )

    with pytest.raises(SessionReconciliationTransitionError, match="早于"):
        await store.mark_session_delete_committed("time-request", updated_at=NOW)
