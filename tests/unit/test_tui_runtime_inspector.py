"""Textual Runtime Inspector parity tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from textual.widgets import Markdown, Static, Tabs

from naumi_agent.config.settings import AppConfig
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.runtime_inspector import (
    RuntimeInspectorScreen,
    format_runtime_inspector_markdown,
)


def test_runtime_inspector_formatter_covers_all_authoritative_tabs() -> None:
    snapshot = _snapshot()

    rendered = {
        tab: format_runtime_inspector_markdown(snapshot, tab)
        for tab in ("plan", "tools", "context", "changes", "tests")
    }

    assert "Runtime Inspector · Plan" in rendered["plan"]
    assert "实现 Textual Inspector" in rendered["plan"]
    assert "file_read" in rendered["tools"]
    assert "/tmp/project" in rendered["context"]
    assert "src/example.py" in rendered["changes"]
    assert "pytest tests/unit -q" in rendered["tests"]


def test_runtime_inspector_formatter_states_authoritative_empty_data() -> None:
    snapshot = RuntimeInspectorSnapshot.empty(session_id="session-empty")

    assert "尚未产生计划" in format_runtime_inspector_markdown(snapshot, "plan")
    assert "尚未调用工具" in format_runtime_inspector_markdown(snapshot, "tools")
    assert "尚未产生运行上下文" in format_runtime_inspector_markdown(snapshot, "context")
    assert "尚未记录文件改动" in format_runtime_inspector_markdown(snapshot, "changes")
    assert "尚未记录验证" in format_runtime_inspector_markdown(snapshot, "tests")


def test_runtime_inspector_formatter_renders_unlimited_budget() -> None:
    payload = _snapshot().to_dict()
    payload["context"].update(
        {
            "budget_enabled": False,
            "budget_used_usd": 0.0123,
            "budget_max_usd": None,
            "budget_percentage": None,
            "budget_max_input_tokens": None,
            "budget_max_output_tokens": None,
        }
    )

    rendered = format_runtime_inspector_markdown(payload, "context")

    assert "预算：不限 · 已用 $0.0123" in rendered
    assert "$0.0000" not in rendered


@pytest.mark.asyncio
async def test_textual_runtime_inspector_loads_switches_and_closes() -> None:
    engine = AgentEngine(AppConfig())
    snapshot = _snapshot()
    engine.runtime_inspector.snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("ctrl+i")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, RuntimeInspectorScreen)
        assert engine.runtime_inspector.snapshot.await_count == 1
        assert screen.query_one(Tabs).active == "plan"
        content = screen.query_one("#inspector-content", Markdown)._markdown
        assert "实现 Textual Inspector" in content

        await pilot.press("]")
        await pilot.pause(0.05)
        assert screen.query_one(Tabs).active == "tools"
        assert "file_read" in screen.query_one("#inspector-content", Markdown)._markdown

        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, RuntimeInspectorScreen)


@pytest.mark.asyncio
async def test_textual_runtime_inspector_retains_last_snapshot_on_refresh_error() -> None:
    engine = AgentEngine(AppConfig())
    engine.runtime_inspector.snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_snapshot(), RuntimeError("git unavailable")]
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("ctrl+i")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, RuntimeInspectorScreen)

        await pilot.press("r")
        await pilot.pause(0.1)

        content = screen.query_one("#inspector-content", Markdown)._markdown
        error = screen.query_one("#inspector-error", Static).render()
        assert "实现 Textual Inspector" in content
        assert "已保留上一次快照" in str(error)
        assert engine.runtime_inspector.snapshot.await_count == 2


def _snapshot() -> RuntimeInspectorSnapshot:
    return RuntimeInspectorSnapshot.from_dict(
        {
            "schema_version": 1,
            "session_id": "session-tui-inspector",
            "revision": 4,
            "generated_at": "2026-07-13T00:00:00+00:00",
            "active_run_id": "run-tui-inspector",
            "plan": {
                "state": "ready",
                "items": [
                    {
                        "id": "todo-1",
                        "subject": "实现 Textual Inspector",
                        "status": "in_progress",
                        "active_form": "正在同步 TUI",
                        "owner": "main",
                        "blocked_by": [],
                    }
                ],
                "next_actions": [],
                "warnings": [],
            },
            "tools": {
                "state": "ready",
                "items": [
                    {
                        "call_id": "read-1",
                        "name": "file_read",
                        "status": "success",
                        "summary": "读取真实文件",
                        "duration_ms": 8,
                        "run_id": "run-tui-inspector",
                    }
                ],
                "approvals": [],
                "warnings": [],
            },
            "context": {
                "state": "ready",
                "workspace_root": "/tmp/project",
                "branch": "main",
                "commit": "abc1234",
                "git_available": True,
                "git_dirty": True,
                "model": "openai/test",
                "runtime_mode": "default",
                "permission_mode": "moderate",
                "context_used": 1200,
                "context_window": 128000,
                "context_percentage": 0.9,
                "budget_used_usd": 0.01,
                "budget_max_usd": 5,
                "budget_percentage": 0.2,
                "input_tokens": 1000,
                "output_tokens": 200,
                "turns": 1,
                "warnings": [],
            },
            "changes": {
                "state": "ready",
                "source_run_id": "run-tui-inspector",
                "receipt_id": "receipt-1",
                "summary": "修改运行检查器。",
                "items": [
                    {
                        "path": "src/example.py",
                        "status": "modified",
                        "source_tool": "file_edit",
                        "additions": 4,
                        "deletions": 1,
                    }
                ],
                "git_state": {
                    "available": True,
                    "branch": "main",
                    "dirty": True,
                    "commit": "abc1234",
                    "ahead": 0,
                    "behind": 0,
                },
                "warnings": [],
            },
            "tests": {
                "state": "ready",
                "source_run_id": "run-tui-inspector",
                "receipt_id": "receipt-1",
                "validations": [
                    {
                        "command": "pytest tests/unit -q",
                        "scope": "tests/unit",
                        "status": "passed",
                        "exit_code": 0,
                        "passed": 12,
                        "failed": 0,
                        "skipped": 1,
                        "log_ref": "logs/unit.txt",
                    }
                ],
                "unverified": [],
                "next_actions": [],
                "warnings": [],
            },
        }
    )
