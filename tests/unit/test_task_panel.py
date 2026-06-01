"""Tests for the unified task/subagent/background panel."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class FakeBackgroundRunner:
    def list_tasks(self) -> list[BackgroundTask]:
        return [
            BackgroundTask(
                id="bg_0001",
                command="pytest tests/unit/test_task_panel.py",
                cwd="/tmp/project",
                status=BackgroundStatus.RUNNING,
                output_path="/tmp/bg.log",
                started_at="2026-01-01T00:00:00",
                output_preview="collecting\nrunning tests",
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
                "createdAt": "2026-06-01T12:00:00",
            }
        ][:limit]


class FakeEngine:
    task_store = FakeTaskStore()
    subagent_manager = FakeSubagentManager()
    background_runner = FakeBackgroundRunner()
    task_runner = FakeBrowserTaskRunner()

    def get_recent_permission_bubbles(self, limit: int = 12) -> list[dict[str, str]]:
        return [
            {
                "agent_name": "coder",
                "tool_name": "bash_run",
                "status": "needs_confirmation",
                "reason": "该工具需要确认",
            }
        ][:limit]


@pytest.mark.asyncio
async def test_build_task_panel_snapshot_normalizes_all_sources() -> None:
    snapshot = await build_task_panel_snapshot(FakeEngine(), limit=10)

    assert len(snapshot.todo_items) == 3
    assert snapshot.todo_items[1].text == "正在接入 TUI"
    assert snapshot.agents[0].name == "coder"
    assert snapshot.agents[0].phase == TaskPhase.RUNNING
    assert snapshot.subagent_events[0]["task_id"] == "sub_1"
    assert snapshot.permission_bubbles[0]["tool_name"] == "bash_run"
    assert snapshot.background_tasks[0].task_id == "bg_0001"
    assert snapshot.browser_tasks[0].run_id == "run_1"
    assert snapshot.warnings == ()


@pytest.mark.asyncio
async def test_render_task_panel_contains_expected_sections() -> None:
    text = await render_task_panel(FakeEngine(), limit=10)

    assert "任务面板" in text
    assert "Todo" in text
    assert "正在接入 TUI" in text
    assert "Subagent" in text
    assert "coder" in text
    assert "权限冒泡" in text
    assert "Background" in text
    assert "running tests" in text
    assert "Browser Runs" in text
    assert "打开页面并检查按钮" in text


def test_render_task_panel_snapshot_handles_empty_state() -> None:
    text = render_task_panel_snapshot(build_empty_snapshot())

    assert "暂无任务" in text
    assert "暂无后台任务" in text
    assert "暂无浏览器任务运行" in text


def build_empty_snapshot():
    from naumi_agent.ui.task_panel import TaskPanelSnapshot

    return TaskPanelSnapshot()
