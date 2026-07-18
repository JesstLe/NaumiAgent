"""Tests for the unified task/subagent/background panel."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.ui.task_panel import (
    build_task_panel_snapshot,
    render_task_panel,
    render_task_panel_snapshot,
)
from naumi_agent.ui.task_status_renderer import TaskPhase


class FakeTaskStore:
    async def list_tasks(self) -> list[Task]:
        return [
            Task(
                id="1",
                session_id="s1",
                subject="设计任务面板",
                description="",
                status=TaskStatus.COMPLETED,
            ),
            Task(
                id="2",
                session_id="s1",
                subject="接入 TUI",
                description="",
                status=TaskStatus.IN_PROGRESS,
                active_form="正在接入 TUI",
                owner="coder",
            ),
            Task(
                id="3",
                session_id="s1",
                subject="等待用户确认",
                description="",
                status=TaskStatus.BLOCKED,
                blocked_by=["2"],
            ),
        ]


class FakeSubagentManager:
    def list_agents(self) -> list[dict[str, str]]:
        return [
            {"name": "coder", "state": "running", "description": "实现面板", "tasks": "2"},
            {"name": "researcher", "state": "idle", "description": "等待任务"},
        ]

    def get_recent_events(self, limit: int = 8) -> list[dict[str, Any]]:
        return [
            {
                "status": "started",
                "agent_name": "coder",
                "task_id": "sub_1",
                "message": "子 Agent 已开始执行。",
            }
        ][:limit]

    def list_executions(self, limit: int = 100) -> list[Any]:
        return [
            SimpleNamespace(
                task_id="sub_1",
                description="探索项目结构",
                agent_name="coder",
                status="running",
                phase="running_tool",
                started_at=1_784_332_800.0,
                finished_at=None,
                elapsed_ms=900,
                current_tool="file_read",
            )
        ][:limit]


@dataclass
class FakeBackgroundRunner:
    calls: int = 0

    def list_tasks(self) -> list[BackgroundTask]:
        self.calls += 1
        return [
            BackgroundTask(
                id="bg_0001",
                command="pytest tests/unit/test_task_panel.py",
                cwd="/tmp/project",
                status=BackgroundStatus.RUNNING,
                output_path="/tmp/bg.log",
                pid=4242,
                port_hints=[8765],
                started_at="2026-01-01T00:00:00",
                output_preview="collecting\nrunning tests",
            )
        ]


class FakeCancelledBackgroundRunner:
    def list_tasks(self) -> list[BackgroundTask]:
        return [
            BackgroundTask(
                id="bg_cancel",
                command="sleep 60",
                cwd="/tmp/project",
                status=BackgroundStatus.CANCELLED,
                output_path="/tmp/bg-cancel.log",
                exit_code=-15,
                output_preview="terminated",
            )
        ]


class FakeHistoryBackgroundRunner:
    def list_active_tasks(self) -> list[BackgroundTask]:
        return [
            BackgroundTask(
                id="bg_active",
                command="sleep 5",
                cwd="/tmp/project",
                status=BackgroundStatus.RUNNING,
                output_path="/tmp/bg-active.log",
            )
        ]

    def list_history(self) -> list[BackgroundTask]:
        return [
            BackgroundTask(
                id="bg_history",
                command="pytest -q",
                cwd="/tmp/project",
                status=BackgroundStatus.COMPLETED,
                output_path="/tmp/bg-history.log",
                notified=True,
            )
        ]


class FakeBrowserTaskRunner:
    def list_runs(self, limit: int = 12) -> list[dict[str, Any]]:
        return [
            {
                "id": "run_1",
                "instruction": "打开页面并检查按钮",
                "status": "needs_input",
                "stepCount": 3,
                "currentStep": "等待用户选择页面元素",
                "createdAt": "2026-06-01T12:00:00",
                "artifacts": {
                    "trace": {"path": "/tmp/browser-trace.zip"},
                    "screenshots": [{"path": "/tmp/screen.png"}],
                },
            }
        ][:limit]


class FakeEngine:
    task_store = FakeTaskStore()
    subagent_manager = FakeSubagentManager()
    task_runner = FakeBrowserTaskRunner()

    def __init__(self) -> None:
        self.background_runner = FakeBackgroundRunner()

    def get_recent_permission_bubbles(self, limit: int = 12) -> list[dict[str, str]]:
        return [
            {
                "agent_name": "coder",
                "tool_name": "bash_run",
                "status": "needs_confirmation",
                "reason": "该工具需要确认",
            }
        ][:limit]


class FakeCancelledBackgroundEngine(FakeEngine):
    task_store = None
    subagent_manager = None
    task_runner = None

    def __init__(self) -> None:
        self.background_runner = FakeCancelledBackgroundRunner()

    def get_recent_permission_bubbles(self, limit: int = 12) -> list[dict[str, str]]:
        return []


class FakeHistoryBackgroundEngine(FakeCancelledBackgroundEngine):
    def __init__(self) -> None:
        self.background_runner = FakeHistoryBackgroundRunner()


@pytest.mark.asyncio
async def test_build_task_panel_snapshot_normalizes_all_sources() -> None:
    engine = FakeEngine()
    snapshot = await build_task_panel_snapshot(engine, limit=10)

    assert len(snapshot.todo_items) == 3
    assert snapshot.todo_items[1].text == "正在接入 TUI"
    assert snapshot.todo_details[1].owner == "coder"
    assert snapshot.todo_details[2].blocked_by == ("2",)
    assert snapshot.agents[0].name == "coder"
    assert snapshot.agents[0].phase == TaskPhase.RUNNING
    assert "tasks=2" in snapshot.agents[0].description
    assert snapshot.subagent_events[0]["task_id"] == "sub_1"
    assert snapshot.permission_bubbles[0]["tool_name"] == "bash_run"
    assert snapshot.background_tasks[0].task_id == "bg_0001"
    assert snapshot.background_details[0].cwd == "/tmp/project"
    assert snapshot.background_details[0].pid == 4242
    assert snapshot.background_details[0].port_hints == (8765,)
    assert snapshot.background_details[0].started_at == "2026-01-01T00:00:00"
    assert snapshot.browser_tasks[0].run_id == "run_1"
    assert snapshot.browser_tasks[0].current_step == "等待用户选择页面元素"
    assert snapshot.browser_tasks[0].record_paths == (
        "/tmp/browser-trace.zip",
        "/tmp/screen.png",
    )
    assert {event.source for event in snapshot.timeline_events} >= {
        "todo",
        "subagent",
        "permissions",
        "background",
        "browser",
    }
    assert any(event.task_id == "bg_0001" for event in snapshot.timeline_events)
    background_event = next(
        event for event in snapshot.timeline_events
        if event.task_id == "bg_0001"
    )
    assert background_event.timestamp == "2026-01-01T00:00:00"
    assert snapshot.warnings == ()
    assert engine.background_runner.calls == 1
    assert {item.source for item in snapshot.view_items} == {
        "todo",
        "subagent",
        "background",
        "browser",
    }
    todo = next(item for item in snapshot.view_items if item.view_id == "todo:3")
    assert todo.status == "blocked"
    assert todo.dependency_ids == ("2",)
    subagent = next(item for item in snapshot.view_items if item.source == "subagent")
    assert subagent.task_id == "sub_1"
    assert subagent.owner == "coder"
    protocol = snapshot.to_protocol_dict()
    assert protocol["schema_version"] == 1
    assert protocol["full"] is True
    assert protocol["items"][0]["view_id"]
    assert protocol["timeline"]


@pytest.mark.asyncio
async def test_preparing_background_task_is_visible_as_pending() -> None:
    engine = FakeEngine()
    engine.background_runner = SimpleNamespace(list_tasks=lambda: [BackgroundTask(
        id="bg_preparing",
        command="echo preparing",
        cwd="/tmp/project",
        status=BackgroundStatus.PREPARING,
        output_path="/tmp/bg-preparing.log",
        idempotency_key="pact_preparing-1",
    )])

    snapshot = await build_task_panel_snapshot(engine, limit=10)

    assert snapshot.background_tasks[0].task_id == "bg_preparing"
    assert snapshot.background_tasks[0].phase is TaskPhase.PENDING


@pytest.mark.asyncio
async def test_render_task_panel_contains_expected_sections() -> None:
    text = await render_task_panel(FakeEngine(), limit=10)

    assert "任务面板" in text
    assert "Timeline" in text
    assert "source=background" in text
    assert "Todo" in text
    assert "正在接入 TUI" in text
    assert "owner=coder" in text
    assert "blocked_by=2" in text
    assert "Subagent" in text
    assert "coder" in text
    assert "权限冒泡" in text
    assert "Background" in text
    assert "running tests" in text
    assert "cwd=/tmp/project" in text
    assert "ports=8765" in text
    assert "Browser Runs" in text
    assert "打开页面并检查按钮" in text
    assert "current=等待用户选择页面元素" in text
    assert "records=/tmp/browser-trace.zip" in text


@pytest.mark.asyncio
async def test_render_task_panel_detail_matches_task_sources() -> None:
    background_text = await render_task_panel(
        FakeEngine(),
        limit=10,
        detail_id="bg_0001",
    )

    assert "filter: source=all status=all detail=bg_0001" in background_text
    assert "Detail" in background_text
    assert "类型: Background" in background_text
    assert "ID: bg_0001" in background_text
    assert "命令: pytest tests/unit/test_task_panel.py" in background_text
    assert "CWD: /tmp/project" in background_text
    assert "Started: 2026-01-01T00:00:00" in background_text
    assert "最近输出: collecting" in background_text

    browser_text = await render_task_panel(
        FakeEngine(),
        limit=10,
        detail_id="run_1",
    )

    assert "类型: Browser Run" in browser_text
    assert "ID: run_1" in browser_text
    assert "Current: 等待用户选择页面元素" in browser_text
    assert "Records: /tmp/browser-trace.zip, /tmp/screen.png" in browser_text

    missing_text = await render_task_panel(
        FakeEngine(),
        limit=10,
        detail_id="missing",
    )

    assert "未找到 ID: missing" in missing_text


@pytest.mark.asyncio
async def test_render_task_panel_filters_by_source_and_status() -> None:
    todo_text = await render_task_panel(
        FakeEngine(),
        limit=10,
        source="todo",
        status="open",
    )

    assert "filter: source=todo status=open" in todo_text
    assert "正在接入 TUI" in todo_text
    assert "等待用户确认" in todo_text
    assert "设计任务面板" not in todo_text
    assert "Background" in todo_text
    assert "暂无后台任务" in todo_text
    assert "打开页面并检查按钮" not in todo_text

    browser_text = await render_task_panel(
        FakeEngine(),
        limit=10,
        source="browser",
        status="needs_input",
    )

    assert "filter: source=browser status=needs_input" in browser_text
    assert "打开页面并检查按钮" in browser_text
    assert "source=browser" in browser_text
    assert "正在接入 TUI" not in browser_text
    assert "running tests" not in browser_text


@pytest.mark.asyncio
async def test_render_task_panel_filters_background_by_raw_lifecycle_status() -> None:
    text = await render_task_panel(
        FakeCancelledBackgroundEngine(),
        limit=10,
        source="background",
        status="cancelled",
    )

    assert "filter: source=background status=cancelled" in text
    assert "bg_cancel" in text
    assert "sleep 60" in text
    assert "exit=-15" in text


@pytest.mark.asyncio
async def test_task_panel_separates_active_background_tasks_from_history() -> None:
    active = await build_task_panel_snapshot(
        FakeHistoryBackgroundEngine(),
        source="background",
    )
    history = await build_task_panel_snapshot(
        FakeHistoryBackgroundEngine(),
        source="background",
        history=True,
    )

    assert [item.task_id for item in active.background_tasks] == ["bg_active"]
    assert [item.task_id for item in history.background_tasks] == ["bg_history"]
    assert history.filters.history is True


def test_render_task_panel_snapshot_handles_empty_state() -> None:
    text = render_task_panel_snapshot(build_empty_snapshot())

    assert "暂无任务" in text
    assert "暂无后台任务" in text
    assert "暂无浏览器任务运行" in text


def build_empty_snapshot():
    from naumi_agent.ui.task_panel import TaskPanelSnapshot

    return TaskPanelSnapshot()
