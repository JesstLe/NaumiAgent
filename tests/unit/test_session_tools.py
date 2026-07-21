"""Session history tool tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    ReconciliationCoordinatorResult,
)
from naumi_agent.harness.reconciliation import SessionReconciliationState
from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_periodic import (
    RetentionWorkerSnapshot,
    RetentionWorkerState,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
    SessionRetentionReason,
    SessionRetentionSelection,
)
from naumi_agent.memory.lifecycle import SessionDeletePreview
from naumi_agent.memory.session import Session
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.session import (
    SessionDeleteTool,
    SessionHistoryTool,
    SessionLoadTool,
    SessionRetentionTool,
    SessionRetentionWorkerTool,
)


def _session(session_id: str = "s1", title: str = "Demo") -> Session:
    return Session(
        id=session_id,
        title=title,
        model="kimi-for-coding",
        messages=[
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "可以。"},
        ],
        updated_at=datetime(2026, 6, 11, 10, 30),
        workspace_root="/tmp/naumi",
        git_branch="main",
        summary="测试摘要",
    )


def _delete_result(
    outcome: ReconciliationCoordinatorOutcome,
    session_id: str,
) -> ReconciliationCoordinatorResult:
    return ReconciliationCoordinatorResult(
        session_id=session_id,
        request_id=(
            f"request-{session_id}"
            if outcome is not ReconciliationCoordinatorOutcome.NOT_FOUND
            else ""
        ),
        outcome=outcome,
        reconciliation_state=(
            SessionReconciliationState.RECORDS_COMMITTED
            if outcome is ReconciliationCoordinatorOutcome.COMPLETED
            else None
        ),
        tombstone_status=None,
        message="Session、Harness 记录与 Artifact 协调完成。",
    )


@pytest.mark.asyncio
async def test_session_history_tool_lists_sessions() -> None:
    engine = MagicMock()
    engine._session = MagicMock(id="s1")
    engine.workspace_root = "/tmp/naumi"
    engine.list_sessions = AsyncMock(return_value=([_session()], 1))
    tool = SessionHistoryTool(engine)

    output = await tool.execute(query="Demo", page=1, page_size=20)

    assert tool.metadata.read_only is True
    assert "历史会话（共 1 个） · 搜索: Demo" in output
    assert "Demo *当前" in output
    assert "id: s1" in output
    engine.list_sessions.assert_awaited_once_with(page=1, page_size=20, query="Demo")


@pytest.mark.asyncio
async def test_session_history_tool_previews_session() -> None:
    engine = MagicMock()
    engine.session_store.load = AsyncMock(return_value=_session("s2", "Preview"))
    tool = SessionHistoryTool(engine)

    output = await tool.execute(action="preview", session_id="s2")

    assert "## Preview" in output
    assert "ID：`s2`" in output
    assert "测试摘要" in output
    engine.session_store.load.assert_awaited_once_with("s2")


@pytest.mark.asyncio
async def test_session_history_tool_previews_delete_impact_read_only() -> None:
    engine = MagicMock()
    engine.preview_session_delete = AsyncMock(
        return_value=SessionDeletePreview(
            session_id="s2",
            title="Preview",
            workspace_root="/tmp/naumi",
            message_count=2,
            is_active=False,
            harness_run_count=1,
            criterion_count=1,
            check_count=2,
            evidence_count=3,
            replay_baseline_count=0,
            check_artifact_reference_count=1,
            evidence_artifact_reference_count=2,
        )
    )
    tool = SessionHistoryTool(engine)

    output = await tool.execute(action="delete_preview", session_id="s2")

    assert tool.metadata.read_only is True
    assert "Session 删除影响预览" in output
    assert "Harness Run：1" in output
    engine.preview_session_delete.assert_awaited_once_with("s2")


@pytest.mark.asyncio
async def test_session_history_tool_previews_retention_plan_read_only() -> None:
    engine = MagicMock()
    engine.preview_session_retention = AsyncMock(
        return_value=SessionRetentionPreview(
            selected=(
                SessionRetentionSelection(
                    session_id="old",
                    title="旧会话",
                    effective_last_accessed_at=datetime(2026, 5, 1),
                    payload_bytes=2048,
                    reason=SessionRetentionReason.AGE_EXPIRED,
                ),
            ),
            total_archived_count=2,
            total_archived_bytes=4096,
            scanned_count=2,
            eligible_count=1,
            deferred_eligible_count=0,
            selected_bytes=2048,
            storage_excess_bytes=0,
            scan_truncated=False,
            budget_exhausted=False,
            policy=SessionRetentionPolicy(),
        )
    )
    tool = SessionHistoryTool(engine)

    output = await tool.execute(action="retention_preview")

    assert "Session 保留策略预览" in output
    assert "旧会话" in output
    assert "仅为只读预览" in output
    engine.preview_session_retention.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_session_history_tool_reads_retention_worker_status_without_control() -> None:
    engine = MagicMock()
    engine.config.memory.session_retention.periodic_enabled = False
    engine.session_retention_worker_snapshot.return_value = RetentionWorkerSnapshot(
        owner_id="worker-a",
        state=RetentionWorkerState.STOPPED,
        lease_held=False,
        pass_count=0,
        completed_session_count=0,
        retry_scheduled_count=0,
        failure_count=0,
        consecutive_empty_passes=0,
        next_delay_seconds=300,
        last_pass_status="",
        last_error_code="",
        started_at="",
        last_pass_at="",
    )
    tool = SessionHistoryTool(engine)

    output = await tool.execute(action="retention_worker_status")

    assert "Session Retention Worker" in output
    assert "配置启用：否" in output
    assert tool.metadata.read_only is True


def test_engine_registers_session_history_tool(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_history")

    assert tool is not None
    assert tool.metadata.read_only is True


@pytest.mark.asyncio
async def test_session_load_tool_loads_by_session_id() -> None:
    engine = MagicMock()
    engine.load_session = AsyncMock(return_value=True)
    engine._session = _session("s2", "Loaded")
    tool = SessionLoadTool(engine)

    output = await tool.execute(session_id="s2")

    assert "已加载会话：Loaded" in output
    assert "ID：`s2`" in output
    engine.load_session.assert_awaited_once_with("s2")


@pytest.mark.asyncio
async def test_session_load_tool_loads_by_recent_index() -> None:
    engine = MagicMock()
    engine.list_sessions = AsyncMock(return_value=([_session("s1"), _session("s2")], 2))
    engine.load_session = AsyncMock(return_value=True)
    engine._session = _session("s2", "Second")
    tool = SessionLoadTool(engine)

    output = await tool.execute(session_id="2")

    assert "已加载会话：Second" in output
    engine.list_sessions.assert_awaited_once_with(page=1, page_size=10)
    engine.load_session.assert_awaited_once_with("s2")


def test_engine_registers_session_load_tool(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_load")

    assert tool is not None
    assert tool.metadata.read_only is False


@pytest.mark.asyncio
async def test_session_retention_tool_runs_one_destructive_bounded_pass() -> None:
    engine = MagicMock()
    engine.run_session_retention_once = AsyncMock(
        return_value=SessionRetentionPassResult(
            status=RetentionPassStatus.COMPLETED,
            planned_count=1,
            attempted_count=1,
            completed_count=1,
            retry_scheduled_count=0,
            retry_exhausted_count=0,
            policy_blocked_count=0,
            not_found_count=0,
            error_count=0,
            remaining_count=0,
            planned_bytes=100,
            duration_seconds=0.1,
            results=(),
            message="完成",
        )
    )
    tool = SessionRetentionTool(engine)

    output = await tool.execute()

    assert tool.metadata.destructive is True
    assert tool.metadata.requires_confirmation is True
    assert "Session 保留清理回执" in output
    engine.run_session_retention_once.assert_awaited_once_with()


def test_engine_registers_session_retention_tool(tmp_path) -> None:
    engine = create_agent_engine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_retention_run")

    assert tool is not None
    assert tool.metadata.destructive is True


@pytest.mark.asyncio
async def test_session_retention_worker_tool_controls_explicit_lifecycle() -> None:
    engine = MagicMock()
    engine.start_session_retention_worker.return_value = True
    engine.wake_session_retention_worker.return_value = True
    engine.stop_session_retention_worker = AsyncMock(return_value=True)
    tool = SessionRetentionWorkerTool(engine)

    assert "已启动" in await tool.execute(action="start")
    assert "已唤醒" in await tool.execute(action="wake")
    assert "已停止" in await tool.execute(action="stop")
    assert tool.metadata.destructive is True
    assert tool.metadata.requires_confirmation is True


def test_engine_registers_session_retention_worker_tool(tmp_path) -> None:
    engine = create_agent_engine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_retention_worker")

    assert tool is not None
    assert tool.metadata.destructive is True


@pytest.mark.asyncio
async def test_session_delete_tool_deletes_by_session_id() -> None:
    engine = MagicMock()
    engine.delete_session_detailed = AsyncMock(
        return_value=_delete_result(ReconciliationCoordinatorOutcome.COMPLETED, "s2")
    )
    tool = SessionDeleteTool(engine)

    output = await tool.execute(session_id="s2")

    assert "Session、Harness 记录与 Artifact 协调完成" in output
    assert "Session：`s2`" in output
    engine.delete_session_detailed.assert_awaited_once_with("s2")


@pytest.mark.asyncio
async def test_session_delete_tool_reports_missing_session() -> None:
    engine = MagicMock()
    engine.delete_session_detailed = AsyncMock(
        return_value=_delete_result(ReconciliationCoordinatorOutcome.NOT_FOUND, "missing")
    )
    tool = SessionDeleteTool(engine)

    output = await tool.execute(session_id="missing")

    assert "会话 missing 不存在" in output
    engine.delete_session_detailed.assert_awaited_once_with("missing")


@pytest.mark.asyncio
async def test_session_delete_tool_reports_durable_retry_request() -> None:
    engine = MagicMock()
    engine.delete_session_detailed = AsyncMock(
        return_value=_delete_result(
            ReconciliationCoordinatorOutcome.RETRY_SCHEDULED,
            "s2",
        )
    )
    tool = SessionDeleteTool(engine)

    output = await tool.execute(session_id="s2")

    assert "已安排安全重试" in output
    assert "request-s2" in output


@pytest.mark.asyncio
async def test_session_delete_tool_rejects_numeric_index() -> None:
    engine = MagicMock()
    engine.delete_session_detailed = AsyncMock()
    tool = SessionDeleteTool(engine)

    output = await tool.execute(session_id="1")

    assert "只接受明确会话 ID" in output
    engine.delete_session_detailed.assert_not_awaited()


def test_engine_registers_session_delete_tool_as_destructive(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_delete")

    assert tool is not None
    assert tool.metadata.destructive is True
    assert tool.metadata.requires_confirmation is True
