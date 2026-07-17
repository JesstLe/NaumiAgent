"""Tests for shared resume/history screen rendering."""

from __future__ import annotations

from datetime import datetime

from naumi_agent.memory.lifecycle import SessionDeletePreview
from naumi_agent.memory.session import Session
from naumi_agent.ui.history_screen import (
    build_history_item,
    build_history_snapshot,
    render_history_preview,
    render_history_screen,
    render_session_delete_preview,
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
