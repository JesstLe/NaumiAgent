"""Textual Workbench overview fallback parity tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Markdown, Static

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.workbench_overview import (
    WorkbenchOverviewScreen,
    format_workbench_overview_markdown,
)


def test_workbench_formatter_renders_authoritative_overview_fields() -> None:
    rendered = format_workbench_overview_markdown(_snapshot())

    assert "Workbench Overview" in rendered
    assert "revision 3" in rendered
    assert "Harness 评测裁判" in rendered
    assert "实现 TUI fallback" in rendered
    assert "codex/ui-10-7" in rendered
    assert "/tmp/ui-10-7" in rendered
    assert "验证通过" in rendered
    assert "高风险" in rendered
    assert "等待用户确认" in rendered


def test_workbench_formatter_has_bounded_empty_state() -> None:
    snapshot = {
        **_snapshot(),
        "missions": [],
        "tasks": [],
        "issues": [],
        "leases": [],
        "validation_runs": [],
        "failures": [],
        "approvals": [],
        "counts": {
            "missions": 0,
            "tasks": 0,
            "worktrees": 0,
            "reviews": 0,
            "failures": 0,
        },
        "active_selection": {
            "mission_id": "",
            "task_id": "",
            "worktree_id": "",
            "review_id": "",
        },
    }

    rendered = format_workbench_overview_markdown(snapshot)

    assert "暂无 Workbench 任务" in rendered
    assert "使用 `/task` 创建任务" in rendered
    assert len(rendered.splitlines()) < 20


def test_workbench_formatter_escapes_store_markdown_and_control_characters() -> None:
    snapshot = _snapshot()
    snapshot["missions"][0]["title"] = "# injected\n[link](file:///secret)"  # type: ignore[index]
    snapshot["tasks"][0]["description"] = "**fake success**\x00"  # type: ignore[index]

    rendered = format_workbench_overview_markdown(snapshot)

    assert "\\# injected" in rendered
    assert "\\[link\\](file:///secret)" in rendered
    assert "\\*\\*fake success\\*\\*" in rendered
    assert "\x00" not in rendered


@pytest.mark.asyncio
async def test_textual_workbench_slash_route_refreshes_and_retains_last_snapshot() -> None:
    engine = AgentEngine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_snapshot(), RuntimeError("store unavailable")]
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(90, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert "实现 TUI fallback" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        engine.workbench_service.dashboard_snapshot.assert_awaited_once_with(
            "session-workbench-tui"
        )

        await pilot.press("r")
        await pilot.pause(0.1)

        assert "实现 TUI fallback" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        assert "已保留上一次快照" in str(
            screen.query_one("#workbench-error", Static).render()
        )

        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, WorkbenchOverviewScreen)


@pytest.mark.asyncio
async def test_textual_workbench_rejects_cross_session_snapshot() -> None:
    engine = AgentEngine(AppConfig())
    engine._session = SimpleNamespace(id="session-current")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(80, 24)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert "权威快照暂时不可用" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        assert "加载失败" in str(
            screen.query_one("#workbench-error", Static).render()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 120, 200])
async def test_textual_workbench_keeps_core_status_visible_at_supported_widths(
    width: int,
) -> None:
    engine = AgentEngine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(width, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Workbench Overview" in rendered
        assert "任务 1" in rendered
        assert "实现 TUI fallback" in rendered


@pytest.mark.asyncio
async def test_real_store_service_and_textual_workbench_chain(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
            )
        )
    )
    session = await engine.get_or_create_session("TUI Workbench real chain")
    mission = await engine.workbench_service.create_mission(
        session_id=session.id,
        title="真实 Workbench 目标",
        goal="验证 SQLite 到 Textual 的只读链路",
    )
    await engine.workbench_service.create_issue(
        session_id=session.id,
        mission_id=mission.id,
        title="真实 TUI 任务",
        description="从真实 Store 读取",
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(80, 28)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.15)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        rendered = screen.query_one("#workbench-content", Markdown)._markdown
        assert "真实 Workbench 目标" in rendered
        assert "真实 TUI 任务" in rendered
        assert "SQLite 到 Textual" in rendered
        assert screen.snapshot is not None
        assert screen.snapshot["session_id"] == session.id


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "stream_id": "stream-workbench-tui",
        "revision": 3,
        "generated_at": "2026-07-18T04:40:00+08:00",
        "full": True,
        "session_id": "session-workbench-tui",
        "counts": {
            "missions": 1,
            "tasks": 1,
            "worktrees": 1,
            "reviews": 1,
            "failures": 1,
        },
        "active_selection": {
            "mission_id": "mission-1",
            "task_id": "task-1",
            "worktree_id": "task-1",
            "review_id": "approval-1",
        },
        "missions": [{
            "id": "mission-1",
            "title": "Harness 评测裁判",
            "goal": "建立可信回归门",
            "status": "active",
        }],
        "tasks": [{
            "id": "task-1",
            "subject": "实现 TUI fallback",
            "description": "复用权威 Workbench 快照",
            "status": "in_progress",
            "owner": "naumi",
            "blocked_by": [],
        }],
        "issues": [{
            "id": "issue-1",
            "task_id": "task-1",
            "risk_level": "high",
            "related_branch": "codex/ui-10-7",
            "related_worktree": "/tmp/ui-10-7",
            "related_pr": "#107",
            "expected_artifacts": ["tui overview"],
        }],
        "leases": [{"task_id": "task-1", "agent_id": "naumi"}],
        "validation_runs": [{
            "task_id": "task-1",
            "status": "passed",
            "exit_code": 0,
            "command": ["pytest", "-q", "test_tui_workbench_overview.py"],
        }],
        "failures": [{
            "task_id": "task-1",
            "kind": "test_failed",
            "title": "旧平台测试失败",
        }],
        "approvals": [{
            "id": "approval-1",
            "task_id": "task-1",
            "title": "等待用户确认",
        }],
        "events": [],
    }
