"""Engine integration for durable Session/Harness deletion reconciliation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.store import HarnessStoreError
from naumi_agent.orchestrator.engine import AgentEngine


@pytest.mark.asyncio
async def test_engine_delete_reconciles_harness_and_runtime_authority(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "runtime" / "sessions.db")),
            workspace_root=str(workspace),
        )
    )
    try:
        session = await engine.get_or_create_session()
        await engine._save_session()
        grant = engine._permission_grant_store.create(
            session.id,
            "shell",
            "engine-delete-grant",
        )
        await engine._harness_store.start_run(
            workspace_root=workspace,
            contract=HarnessCompletionContract(
                run_id="engine-delete-run",
                session_id=session.id,
                task_kind=HarnessTaskKind.CHANGE,
                objective="验证 Engine 删除协调",
            ),
            tree_fingerprint_before="a" * 64,
            started_at="2026-07-17T20:00:00+08:00",
        )

        result = await engine.delete_session_detailed(session.id)

        assert result.outcome is ReconciliationCoordinatorOutcome.COMPLETED
        assert await engine.session_store.load(session.id) is None
        assert await engine._harness_store.get_run("engine-delete-run") is None
        assert engine._session is None
        assert engine._permission_grant_store.list_session(session.id) == ()
        revocations = [
            item
            for item in engine.get_recent_permission_bubbles(limit=20)
            if item["status"] == "grant_revoked"
        ]
        assert revocations[-1]["grant_id"] == grant.grant_id
        assert revocations[-1]["source"] == "session_deletion"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_harness_failure_clears_deleted_session_then_startup_recovers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "runtime" / "sessions.db")),
            workspace_root=str(workspace),
        )
    )
    try:
        session = await engine.get_or_create_session()
        await engine._save_session()
        engine._permission_grant_store.create(session.id, "shell", "pending-grant")
        original_reconcile = engine._harness_store.reconcile_session_delete_records
        engine._harness_store.reconcile_session_delete_records = AsyncMock(
            side_effect=HarnessStoreError("injected raw failure")
        )

        result = await engine.delete_session_detailed(session.id)

        assert result.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
        assert await engine.session_store.load(session.id) is None
        assert engine._session is None
        assert engine._permission_grant_store.list_session(session.id) == ()

        engine._harness_store.reconcile_session_delete_records = original_reconcile
        recovered = await engine.recover_session_reconciliations(
            now="2099-01-01T00:00:00+00:00",
            lease_seconds=60,
        )

        assert recovered[0].outcome is ReconciliationCoordinatorOutcome.COMPLETED
        tombstone = await engine._harness_store.get_reconciliation_tombstone(
            result.request_id
        )
        assert tombstone is not None
        assert tombstone.status.value == "resolved"
    finally:
        await engine.shutdown()
