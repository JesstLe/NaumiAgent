"""Textual Workbench overview fallback parity tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input, Markdown, Static

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.workbench_overview import (
    ProposalDecisionScreen,
    WorkbenchOverviewScreen,
    format_workbench_overview_markdown,
    format_workbench_reviews_markdown,
    format_workbench_worktrees_markdown,
)
from naumi_agent.workbench.proposal_governance import ProposalAction


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


def test_worktree_formatter_renders_authoritative_detail_and_error_states() -> None:
    rendered = format_workbench_worktrees_markdown(_snapshot())

    assert "共 1 个" in rendered
    assert "ui-10-real" in rendered
    assert "有未提交改动" in rendered
    assert "codex/ui-10-worktrees" in rendered
    assert "实现 TUI fallback" in rendered
    assert "Workbench-Agent" in rendered
    assert "可安全删除：否" in rendered

    unavailable = format_workbench_worktrees_markdown(
        {
            **_snapshot(),
            "worktrees_status": "unavailable",
            "worktrees_code": "worktree_snapshot_failed",
            "worktrees": [],
        }
    )
    assert "暂时不可用" in unavailable
    assert "worktree_snapshot_failed" in unavailable


def test_worktree_formatter_bounds_one_hundred_items_around_selection() -> None:
    worktrees = [
        {
            "name": f"worktree-{index}",
            "path": f"/tmp/worktree-{index}",
            "branch": f"naumi/worktree-{index}",
            "status": "clean",
            "dirty_files": 0,
            "commits_ahead": 0,
            "removable": True,
        }
        for index in range(100)
    ]
    rendered = format_workbench_worktrees_markdown(
        {
            **_snapshot(),
            "worktrees": worktrees,
            "worktrees_total": 100,
        },
        selected_index=99,
    )

    assert "当前 100/100" in rendered
    assert "worktree-99" in rendered
    assert "worktree-50" not in rendered
    assert len(rendered.splitlines()) < 30


def test_reviews_formatter_renders_real_checks_files_and_diff() -> None:
    detail = {
        "evidence": {
            "approval": {
                "id": "approval-1",
                "title": "等待用户确认",
                "detail": "审查真实变更",
                "requester": "Workbench-Agent",
            },
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [{"status": "failed", "exit_code": 1}],
            "changed_files": [{"path": "src/ui.py", "status": "modified"}],
            "diff_hunks": [{"path": "src/ui.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
        }
    }

    rendered = format_workbench_reviews_markdown(_snapshot(), detail=detail)

    assert "阻塞：1 项验证失败" in rendered
    assert "src/ui.py" in rendered
    assert "-old" in rendered
    assert "+new" in rendered


def test_reviews_formatter_renders_open_proposal_actions_and_policy_boundary() -> None:
    snapshot = _proposal_snapshot()

    rendered = format_workbench_reviews_markdown(snapshot)

    assert "Proposal · 收紧 Harness 裁判" in rendered
    assert "高风险" in rendered
    assert "harness/judge.py" in rendered
    assert "批准只进入下一 policy gate" in rendered
    assert "`a` 批准 · `x` 拒绝" in rendered


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
    engine = create_agent_engine(AppConfig())
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

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert screen.selected_tab == "worktrees"
        assert "ui-10-real" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown

        await pilot.press("1")
        await pilot.pause(0.05)
        assert screen.selected_tab == "overview"

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
async def test_textual_workbench_reviews_loads_selected_service_evidence() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    engine.workbench_service.get_review_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "approval": {
                "id": "approval-1",
                "title": "等待用户确认",
                "detail": "检查实际变更",
                "requester": "Workbench-Agent",
            },
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [{"status": "passed", "exit_code": 0}],
            "changed_files": [{"path": "src/ui.py", "status": "modified"}],
            "diff_hunks": [{"path": "src/ui.py", "patch": "-old\n+new"}],
        }
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        rendered = screen.query_one("#workbench-content", Markdown)._markdown
        assert "Reviews" in rendered
        assert "证据就绪" in rendered
        assert "src/ui.py" in rendered
        engine.workbench_service.get_review_evidence.assert_awaited_once_with(
            "session-workbench-tui", "approval-1"
        )


@pytest.mark.asyncio
async def test_textual_workbench_rejects_proposal_with_required_reason() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "proposal-1", "state": "rejected"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "x")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ProposalDecisionScreen)
        note = app.screen.query_one("#proposal-decision-note", Input)
        await pilot.press("enter")
        assert "不能为空" in str(
            app.screen.query_one("#proposal-decision-error", Static).render()
        )

        note.value = "缺少跨平台回归证据"
        await pilot.press("enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.workbench_service.govern_proposal.assert_awaited_once_with(
            "session-workbench-tui",
            "proposal-1",
            action=ProposalAction.REJECT,
            reviewer="Human",
            decision_note="缺少跨平台回归证据",
        )
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Proposal 已拒绝" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_bypass_approves_proposal_without_modal() -> None:
    engine = create_agent_engine(AppConfig())
    engine.set_runtime_mode("bypass")
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "proposal-1", "state": "approved"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "a")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.workbench_service.govern_proposal.assert_awaited_once_with(
            "session-workbench-tui",
            "proposal-1",
            action=ProposalAction.APPROVE,
            reviewer="Human",
            decision_note="",
        )


@pytest.mark.asyncio
async def test_textual_workbench_rejects_cross_session_snapshot() -> None:
    engine = create_agent_engine(AppConfig())
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
    engine = create_agent_engine(AppConfig())
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
    engine = create_agent_engine(
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
        for _ in range(200):
            if screen.snapshot is not None:
                break
            await pilot.pause(0.05)
        else:
            pytest.fail("真实 Workbench 权威快照未在 10 秒内加载完成")
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
        "proposals": [],
        "events": [],
        "worktrees_status": "ready",
        "worktrees_code": "",
        "worktrees_total": 1,
        "worktrees_truncated": False,
        "worktrees": [{
            "name": "ui-10-real",
            "path": "/tmp/ui-10-real",
            "branch": "codex/ui-10-worktrees",
            "status": "dirty",
            "dirty_files": 2,
            "commits_ahead": 1,
            "removable": False,
            "kept_reason": "",
            "task_id": "task-1",
            "task": {
                "id": "task-1",
                "subject": "实现 TUI fallback",
            },
            "lease": {"id": "lease-1", "agent_id": "Workbench-Agent"},
            "agent_id": "Workbench-Agent",
        }],
    }


def _proposal_snapshot() -> dict[str, object]:
    snapshot = _snapshot()
    snapshot["approvals"] = []
    snapshot["active_selection"] = {
        **snapshot["active_selection"],  # type: ignore[arg-type]
        "review_id": "proposal-1",
        "review_kind": "proposal",
    }
    snapshot["proposals"] = [{
        "id": "proposal-1",
        "session_id": "session-workbench-tui",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "agent_id": "Harness-Agent",
        "title": "收紧 Harness 裁判",
        "impact_scope": "避免无证据通过",
        "intended_files": ["src/naumi_agent/harness/judge.py"],
        "validation_plan": ["运行裁判模块测试"],
        "risk_level": "high",
        "questions": [],
        "state": "open",
        "source_kind": "evolution_candidate",
        "source_id": "candidate-1",
        "source_revision": 2,
        "proposal_kind": "harness_policy",
    }]
    return snapshot
