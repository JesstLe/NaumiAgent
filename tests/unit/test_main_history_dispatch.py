"""Tests for shared history deletion-preview CLI dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
)
from naumi_agent.main import _show_history
from naumi_agent.memory.lifecycle import SessionDeletePreview


@pytest.mark.asyncio
async def test_show_history_routes_delete_preview_through_engine() -> None:
    engine = AsyncMock()
    engine.preview_session_delete.return_value = SessionDeletePreview(
        session_id="session-1",
        title="真实会话",
        workspace_root="/tmp/workspace",
        message_count=2,
        is_active=False,
        harness_run_count=1,
        criterion_count=1,
        check_count=0,
        evidence_count=0,
        replay_baseline_count=0,
        check_artifact_reference_count=0,
        evidence_artifact_reference_count=0,
    )

    await _show_history(engine, "delete-preview session-1")

    engine.preview_session_delete.assert_awaited_once_with("session-1")


@pytest.mark.asyncio
async def test_show_history_routes_retention_preview_through_engine() -> None:
    engine = AsyncMock()
    engine.preview_session_retention.return_value = SessionRetentionPreview(
        selected=(),
        total_archived_count=0,
        total_archived_bytes=0,
        scanned_count=0,
        eligible_count=0,
        deferred_eligible_count=0,
        selected_bytes=0,
        storage_excess_bytes=0,
        scan_truncated=False,
        budget_exhausted=False,
        policy=SessionRetentionPolicy(),
    )

    await _show_history(engine, "retention-preview")

    engine.preview_session_retention.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_show_history_routes_retention_run_through_engine() -> None:
    engine = AsyncMock()
    engine.run_session_retention_once.return_value = SessionRetentionPassResult(
        status=RetentionPassStatus.COMPLETED,
        planned_count=0,
        attempted_count=0,
        completed_count=0,
        retry_scheduled_count=0,
        retry_exhausted_count=0,
        policy_blocked_count=0,
        not_found_count=0,
        error_count=0,
        remaining_count=0,
        planned_bytes=0,
        duration_seconds=0,
        results=(),
        message="无需清理",
    )

    await _show_history(engine, "retention-run")

    engine.run_session_retention_once.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_show_history_routes_retention_worker_actions() -> None:
    engine = MagicMock()
    engine.start_session_retention_worker.return_value = True
    engine.wake_session_retention_worker.return_value = True
    engine.stop_session_retention_worker = AsyncMock(return_value=True)

    await _show_history(engine, "retention-worker start")
    await _show_history(engine, "retention-worker wake")
    await _show_history(engine, "retention-worker stop")

    engine.start_session_retention_worker.assert_called_once_with()
    engine.wake_session_retention_worker.assert_called_once_with()
    engine.stop_session_retention_worker.assert_awaited_once_with()
