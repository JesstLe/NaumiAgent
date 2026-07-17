"""Real cross-Store HAR-06.2a reconciliation scenario."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.reconciliation import SessionReconciliationState
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.store import HarnessStore
from naumi_agent.memory.session import SessionStore

NOW = "2026-07-17T15:00:00+08:00"
LATER = "2026-07-17T15:01:00+08:00"


@pytest.mark.asyncio
async def test_real_session_and_harness_stores_reconcile_idempotently(
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
        session = await sessions.create_session(title="真实协调")
        session.workspace_root = str(workspace)
        await sessions.save(session)
        await harness.start_run(
            workspace_root=workspace,
            contract=HarnessCompletionContract(
                run_id="real-run",
                session_id=session.id,
                task_kind=HarnessTaskKind.CHANGE,
                objective="真实双 Store 协调",
            ),
            tree_fingerprint_before="a" * 64,
            started_at=NOW,
        )
        prepared = await harness.prepare_session_delete_reconciliation(
            request_id="real-delete-request",
            workspace_root=workspace,
            session_id=session.id,
            actor=LifecycleActor.USER,
            created_at=NOW,
        )
        assert prepared.state is SessionReconciliationState.PREPARED
        assert await sessions.delete(session.id) is True
        await harness.mark_session_delete_committed(
            prepared.request_id,
            updated_at=LATER,
        )

        restarted = HarnessStore(harness_path)
        completed = await restarted.reconcile_session_delete_records(
            prepared.request_id,
            updated_at=LATER,
        )
        replay = await restarted.reconcile_session_delete_records(
            prepared.request_id,
            updated_at=LATER,
        )

        assert completed == replay
        assert completed.state is SessionReconciliationState.RECORDS_COMMITTED
        assert completed.deleted_run_count == 1
        assert await sessions.load(session.id) is None
        assert await restarted.get_run("real-run") is None
    finally:
        await sessions.close()
