"""Tests for goal tools shared by Agent and slash commands."""

from __future__ import annotations

from typing import Any

import pytest

from naumi_agent.orchestrator.goal_store import GoalStatus, GoalStore
from naumi_agent.tools.goal import create_goal_tools


class _FakePursueTool:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.result


def _tool_map(store: GoalStore, pursuit: _FakePursueTool | None = None):
    tools = create_goal_tools(
        store,
        session_id_getter=lambda: "session-7",
        pursuit_tool_getter=lambda: pursuit,
    )
    return {tool.name: tool for tool in tools}


@pytest.mark.asyncio
async def test_goal_tools_create_read_list_and_update_shared_store(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    tools = _tool_map(store)

    created = await tools["goal_create"].execute(objective="完善 New UI")
    current = await tools["goal_status"].execute()
    listed = await tools["goal_list"].execute(include_finished=True)
    paused = await tools["goal_update"].execute(
        status="paused",
        note="等待用户验证",
    )

    assert "目标已创建" in created
    assert "完善 New UI" in current
    assert "session-7" in current
    assert "完善 New UI" in listed
    assert "已暂停" in paused
    assert store.current().status is GoalStatus.PAUSED
    assert store.current().note == "等待用户验证"


@pytest.mark.asyncio
async def test_goal_tools_report_validation_and_missing_current_goal(tmp_path) -> None:
    tools = _tool_map(GoalStore(tmp_path / "goals"))

    assert "当前没有未完成目标" in await tools["goal_status"].execute()
    assert "当前没有未完成目标" in await tools["goal_update"].execute(status="paused")
    assert "输入无效" in await tools["goal_create"].execute(objective="")
    assert "不支持的目标状态" in await tools["goal_update"].execute(status="unknown")


@pytest.mark.asyncio
async def test_goal_pursue_reuses_existing_pursuit_tool_and_links_run_id(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    goal = store.create("完成可视化验证")
    pursuit = _FakePursueTool(
        "✅ 目标追踪已在后台启动。\n\n- run_id: `pursuit_abc-123`"
    )
    tools = _tool_map(store, pursuit)

    result = await tools["goal_pursue"].execute()

    assert pursuit.calls == [{"goal": "完成可视化验证"}]
    assert "pursuit_abc-123" in result
    assert store.get(goal.id).pursuit_run_id == "pursuit_abc-123"


@pytest.mark.asyncio
async def test_goal_pursue_keeps_goal_unlinked_when_runtime_does_not_start(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    goal = store.create("不会被伪关联")
    pursuit = _FakePursueTool("⚠️ 目标追踪工具尚未初始化。")
    tools = _tool_map(store, pursuit)

    result = await tools["goal_pursue"].execute()

    assert "未返回有效 run_id" in result
    assert store.get(goal.id).pursuit_run_id == ""


@pytest.mark.asyncio
async def test_goal_pursue_reports_missing_goal_or_tool(tmp_path) -> None:
    empty_store = GoalStore(tmp_path / "empty")
    assert "当前没有未完成目标" in await _tool_map(empty_store)["goal_pursue"].execute()

    store = GoalStore(tmp_path / "goals")
    store.create("缺少追踪后端")
    assert "目标追踪工具未注册" in await _tool_map(store)["goal_pursue"].execute()
