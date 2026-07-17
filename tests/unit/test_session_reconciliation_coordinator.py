"""HAR-06.2b/3b coordinator unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    SessionReconciliationCoordinator,
    build_session_delete_request_id,
)
from naumi_agent.harness.reconciliation import SessionReconciliationState
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tombstone import (
    ReconciliationFailureCode,
    ReconciliationTombstoneStatus,
)
from naumi_agent.memory.session import Session

NOW = "2026-07-17T18:00:00+08:00"
DUE = "2026-07-17T19:00:00+08:00"


class _SessionPort:
    def __init__(self, sessions: list[Session]) -> None:
        self.sessions = {session.id: session for session in sessions}
        self.delete_calls = 0
        self.fail_delete_count = 0
        self.cancel_delete = False

    async def load(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        self.delete_calls += 1
        if self.cancel_delete:
            raise asyncio.CancelledError
        if self.fail_delete_count:
            self.fail_delete_count -= 1
            raise OSError("RAW SECRET-LIKE SESSION ERROR")
        return self.sessions.pop(session_id, None) is not None


def _session(workspace: Path, *, session_id: str = "session-1") -> Session:
    return Session(
        id=session_id,
        title="协调测试",
        status="active",
        workspace_root=str(workspace),
        created_at=datetime(2026, 7, 17, 8, 0, tzinfo=UTC),
    )


def test_request_id_is_stable_and_session_instance_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session = _session(workspace)

    first = build_session_delete_request_id(session, fallback_workspace=workspace)
    second = build_session_delete_request_id(session, fallback_workspace=workspace)
    recreated = build_session_delete_request_id(
        replace(session, created_at=datetime(2026, 7, 17, 9, 0, tzinfo=UTC)),
        fallback_workspace=workspace,
    )

    assert first == second
    assert first.startswith("session-delete-")
    assert recreated != first


def test_retention_request_id_is_scoped_to_one_archive_epoch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first_archive = replace(
        _session(workspace),
        status="archived",
        archived_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    second_archive = replace(
        first_archive,
        archived_at=datetime(2026, 7, 2, tzinfo=UTC),
    )

    first = build_session_delete_request_id(
        first_archive,
        fallback_workspace=workspace,
        actor=LifecycleActor.RETENTION_WORKER,
    )
    second = build_session_delete_request_id(
        second_archive,
        fallback_workspace=workspace,
        actor=LifecycleActor.RETENTION_WORKER,
    )

    assert first != second


@pytest.mark.asyncio
async def test_unknown_session_policy_fails_closed_without_preparing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = replace(_session(workspace), status="mystery")
    port = _SessionPort([session])
    store = HarnessStore(tmp_path / "harness.db")
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=store,
        fallback_workspace=workspace,
    )

    result = await coordinator.delete_session(session.id, now=NOW)

    assert result.outcome is ReconciliationCoordinatorOutcome.POLICY_BLOCKED
    assert port.delete_calls == 0
    assert await store.list_pending_session_reconciliations() == ()


@pytest.mark.asyncio
async def test_retention_fails_closed_without_atomic_archived_delete_capability(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = replace(
        _session(workspace),
        status="archived",
        archived_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    port = _SessionPort([session])
    store = HarnessStore(tmp_path / "harness.db")
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=store,
        fallback_workspace=workspace,
    )

    result = await coordinator.delete_session(
        session.id,
        now=NOW,
        actor=LifecycleActor.RETENTION_WORKER,
    )

    assert result.outcome is ReconciliationCoordinatorOutcome.POLICY_BLOCKED
    assert port.delete_calls == 0
    assert port.sessions[session.id] is session


@pytest.mark.asyncio
async def test_session_failure_schedules_sanitized_retry_then_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace)
    port = _SessionPort([session])
    port.fail_delete_count = 1
    db_path = tmp_path / "harness.db"
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=HarnessStore(db_path),
        fallback_workspace=workspace,
        max_attempts=3,
    )

    failed = await coordinator.delete_session(session.id, now=NOW)

    assert failed.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
    assert "RAW" not in failed.message
    tombstone = await HarnessStore(db_path).get_reconciliation_tombstone(
        failed.request_id
    )
    assert tombstone is not None
    assert tombstone.error_code is ReconciliationFailureCode.SESSION_STORE_ERROR

    direct_retry = await coordinator.delete_session(session.id, now=NOW)
    assert direct_retry.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
    assert port.delete_calls == 1

    recovered = await SessionReconciliationCoordinator(
        session_port=port,
        harness_store=HarnessStore(db_path),
        fallback_workspace=workspace,
        max_attempts=3,
    ).recover_due(worker_id="worker-1", now=DUE, lease_seconds=60)

    assert recovered[0].outcome is ReconciliationCoordinatorOutcome.COMPLETED
    assert port.delete_calls == 2
    restored = await HarnessStore(db_path).get_reconciliation_tombstone(
        failed.request_id
    )
    assert restored is not None
    assert restored.status is ReconciliationTombstoneStatus.RESOLVED


@pytest.mark.asyncio
async def test_artifact_gc_failure_uses_durable_artifact_stage_and_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace)
    port = _SessionPort([session])
    store = HarnessStore(tmp_path / "harness.db")
    original = store.reconcile_session_artifacts
    store.reconcile_session_artifacts = AsyncMock(
        side_effect=OSError("injected artifact failure")
    )
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=store,
        fallback_workspace=workspace,
    )

    failed = await coordinator.delete_session(session.id, now=NOW)

    assert failed.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
    tombstone = await store.get_reconciliation_tombstone(failed.request_id)
    assert tombstone is not None
    assert tombstone.stage.value == "artifact_gc"

    store.reconcile_session_artifacts = original
    recovered = await coordinator.recover_due(
        worker_id="worker-gc",
        now="2099-01-01T00:00:00+00:00",
        lease_seconds=60,
    )

    assert recovered[0].outcome is ReconciliationCoordinatorOutcome.COMPLETED
    assert (
        recovered[0].reconciliation_state
        is SessionReconciliationState.RECORDS_COMMITTED
    )


@pytest.mark.asyncio
async def test_discovery_seeds_tombstone_for_prepared_crash_gap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace)
    port = _SessionPort([session])
    store = HarnessStore(tmp_path / "harness.db")
    request_id = build_session_delete_request_id(
        session,
        fallback_workspace=workspace,
    )
    await store.prepare_session_delete_reconciliation(
        request_id=request_id,
        workspace_root=workspace,
        session_id=session.id,
        actor="user",
        created_at=NOW,
    )
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=store,
        fallback_workspace=workspace,
    )

    seeded = await coordinator.seed_incomplete_reconciliations(now=NOW, limit=10)

    assert seeded == 1
    tombstone = await store.get_reconciliation_tombstone(request_id)
    assert tombstone is not None
    assert tombstone.status is ReconciliationTombstoneStatus.PENDING
    assert port.delete_calls == 0

    recovered = await coordinator.recover_due(
        worker_id="worker-1",
        now=DUE,
        lease_seconds=60,
    )
    assert recovered[0].outcome is ReconciliationCoordinatorOutcome.COMPLETED
    assert port.delete_calls == 1


@pytest.mark.asyncio
async def test_cancellation_is_recorded_then_propagated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace)
    port = _SessionPort([session])
    port.cancel_delete = True
    store = HarnessStore(tmp_path / "harness.db")
    coordinator = SessionReconciliationCoordinator(
        session_port=port,
        harness_store=store,
        fallback_workspace=workspace,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.delete_session(session.id, now=NOW)

    pending = await store.list_pending_session_reconciliations()
    assert len(pending) == 1
    tombstone = await store.get_reconciliation_tombstone(pending[0].request_id)
    assert tombstone is not None
    assert tombstone.error_code is ReconciliationFailureCode.CANCELLED
