"""Tests for shared history deletion-preview CLI dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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
