"""Real Session/Harness coordinator with injected Harness-stage failure."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    SessionReconciliationCoordinator,
)
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.reconciliation import SessionReconciliationState
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.memory.session import SessionStore

NOW = "2026-07-17T18:00:00+08:00"
DUE = "2026-07-17T19:00:00+08:00"


@pytest.mark.asyncio
async def test_real_success_path_deletes_session_and_scoped_harness_rows(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionStore(
        MemoryConfig(session_db_path=str(tmp_path / "runtime" / "sessions.db"))
    )
    harness = HarnessStore(tmp_path / "state" / "harness.db")
    try:
        session = await sessions.create_session(title="真实成功协调")
        session.workspace_root = str(workspace)
        await sessions.save(session)
        await harness.start_run(
            workspace_root=workspace,
            contract=HarnessCompletionContract(
                run_id="success-run",
                session_id=session.id,
                task_kind=HarnessTaskKind.CHANGE,
                objective="验证正常协调路径",
            ),
            tree_fingerprint_before="a" * 64,
            started_at=NOW,
        )
        coordinator = SessionReconciliationCoordinator(
            session_port=sessions,
            harness_store=harness,
            fallback_workspace=workspace,
        )

        result = await coordinator.delete_session(session.id, now=NOW)

        assert result.outcome is ReconciliationCoordinatorOutcome.COMPLETED
        assert result.reconciliation_state is SessionReconciliationState.RECORDS_COMMITTED
        assert await sessions.load(session.id) is None
        assert await harness.get_run("success-run") is None
        assert await harness.get_reconciliation_tombstone(result.request_id) is None
    finally:
        await sessions.close()


@pytest.mark.asyncio
async def test_harness_stage_failure_recovers_without_redeleting_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionStore(
        MemoryConfig(session_db_path=str(tmp_path / "runtime" / "sessions.db"))
    )
    harness_path = tmp_path / "state" / "harness.db"
    harness = HarnessStore(harness_path)
    try:
        session = await sessions.create_session(title="真实故障恢复")
        session.workspace_root = str(workspace)
        await sessions.save(session)
        await harness.start_run(
            workspace_root=workspace,
            contract=HarnessCompletionContract(
                run_id="coordinator-run",
                session_id=session.id,
                task_kind=HarnessTaskKind.CHANGE,
                objective="验证 Harness 阶段恢复",
            ),
            tree_fingerprint_before="a" * 64,
            started_at=NOW,
        )
        original_reconcile = harness.reconcile_session_delete_records
        harness.reconcile_session_delete_records = AsyncMock(
            side_effect=HarnessStoreError("injected raw Harness failure")
        )
        coordinator = SessionReconciliationCoordinator(
            session_port=sessions,
            harness_store=harness,
            fallback_workspace=workspace,
            max_attempts=3,
        )

        failed = await coordinator.delete_session(session.id, now=NOW)

        assert failed.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
        record = await HarnessStore(harness_path).get_session_delete_reconciliation(
            failed.request_id
        )
        assert record is not None
        assert record.state is SessionReconciliationState.SESSION_COMMITTED
        assert await sessions.load(session.id) is None
        assert await HarnessStore(harness_path).get_run("coordinator-run") is not None

        harness.reconcile_session_delete_records = original_reconcile
        recovered = await SessionReconciliationCoordinator(
            session_port=sessions,
            harness_store=HarnessStore(harness_path),
            fallback_workspace=workspace,
            max_attempts=3,
        ).recover_due(worker_id="recovery-worker", now=DUE, lease_seconds=60)

        assert recovered[0].outcome is ReconciliationCoordinatorOutcome.COMPLETED
        assert await HarnessStore(harness_path).get_run("coordinator-run") is None
    finally:
        await sessions.close()
