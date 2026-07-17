"""Tests for shared resume/history screen rendering."""

from __future__ import annotations

from datetime import datetime

from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
    SessionRetentionReason,
    SessionRetentionSelection,
)
from naumi_agent.memory.lifecycle import SessionDeletePreview
from naumi_agent.memory.session import Session
from naumi_agent.ui.history_screen import (
    build_history_item,
    build_history_snapshot,
    render_history_preview,
    render_history_screen,
    render_session_delete_preview,
    render_session_retention_preview,
    render_session_retention_result,
    summarize_session_messages,
)


def _session() -> Session:
    session = Session(
        id="abc123",
        title="调试 CLI",
        model="claude-test",
        updated_at=datetime(2026, 6, 2, 10, 30),
        total_tokens=1234,
        total_cost_usd=0.25,
        workspace_root="/Users/lv/Workspace/NaumiAgent",
        git_branch="feature/history",
        summary="继续修复历史界面",
    )
    session.messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "帮我继续"},
        {"role": "assistant", "content": "好的"},
    ]
    return session


def test_build_history_item_contains_resume_metadata() -> None:
    item = build_history_item(_session(), current_session_id="abc123")

    assert item.is_current is True
    assert item.title == "调试 CLI"
    assert item.model == "claude-test"
    assert item.user_message_count == 1
    assert item.workspace_root.endswith("NaumiAgent")
    assert item.git_branch == "feature/history"


def test_render_history_screen_lists_search_and_actions() -> None:
    snapshot = build_history_snapshot([_session()], total=1, query="CLI")

    rendered = render_history_screen(snapshot)

    assert "历史会话" in rendered
    assert "搜索: CLI" in rendered
    assert "调试 CLI" in rendered
    assert "workspace: NaumiAgent" in rendered
    assert "/history preview <ID>" in rendered
    assert "/history archive <ID>" in rendered


def test_render_history_preview_shows_recent_messages() -> None:
    rendered = render_history_preview(_session())

    assert "## 调试 CLI" in rendered
    assert "Workspace" in rendered
    assert "feature/history" in rendered
    assert "**user**：帮我继续" in rendered


def test_summarize_session_messages_is_deterministic_and_truncated() -> None:
    messages = [
        {"role": "user", "content": "a" * 120},
        {"role": "assistant", "content": "b" * 120},
    ]

    summary = summarize_session_messages(messages, max_chars=80)

    assert len(summary) == 80
    assert summary.endswith("…")


def test_render_session_delete_preview_distinguishes_records_from_artifact_refs() -> None:
    preview = SessionDeletePreview(
        session_id="abc123",
        title="调试 CLI",
        workspace_root="/Users/lv/Workspace/NaumiAgent",
        message_count=3,
        is_active=True,
        harness_run_count=2,
        criterion_count=4,
        check_count=3,
        evidence_count=5,
        replay_baseline_count=1,
        check_artifact_reference_count=2,
        evidence_artifact_reference_count=3,
    )

    rendered = render_session_delete_preview(preview)

    assert "Session 删除影响预览" in rendered
    assert "Harness Run：2" in rendered
    assert "Evidence：5" in rendered
    assert "Artifact 引用：5" in rendered
    assert "不是可安全删除文件数" in rendered
    assert "重新校验共享引用与路径" in rendered
    assert "其他文件会保留" in rendered
    assert "当前会话" in rendered
    assert "/delete abc123" in rendered


def test_render_session_retention_preview_explains_units_reasons_and_safety() -> None:
    preview = SessionRetentionPreview(
        selected=(
            SessionRetentionSelection(
                session_id="old",
                title="旧会话",
                effective_last_accessed_at=datetime(2026, 5, 1, 8, 0),
                payload_bytes=2048,
                reason=SessionRetentionReason.AGE_AND_STORAGE,
            ),
        ),
        total_archived_count=3,
        total_archived_bytes=8192,
        scanned_count=3,
        eligible_count=1,
        deferred_eligible_count=0,
        selected_bytes=2048,
        storage_excess_bytes=4096,
        scan_truncated=False,
        budget_exhausted=False,
        policy=SessionRetentionPolicy(max_archived_session_bytes=4096),
    )

    rendered = render_session_retention_preview(preview)

    assert "Session 保留策略预览" in rendered
    assert "旧会话" in rendered
    assert "过期 + 空间压力" in rendered
    assert "会话持久化载荷" in rendered
    assert "不包含 Harness 数据库和 Artifact 文件" in rendered
    assert "不会删除任何内容" in rendered


def test_render_session_retention_result_distinguishes_completed_and_retry() -> None:
    result = SessionRetentionPassResult(
        status=RetentionPassStatus.PARTIAL,
        planned_count=3,
        attempted_count=3,
        completed_count=1,
        retry_scheduled_count=1,
        retry_exhausted_count=0,
        policy_blocked_count=1,
        not_found_count=0,
        error_count=0,
        remaining_count=0,
        planned_bytes=4096,
        duration_seconds=1.25,
        results=(),
        message="部分完成",
    )

    rendered = render_session_retention_result(result)

    assert "Session 保留清理回执" in rendered
    assert "完整删除：1" in rendered
    assert "安全重试：1" in rendered
    assert "策略阻止：1" in rendered
    assert "4.0 KiB" in rendered
    assert "部分完成" in rendered
