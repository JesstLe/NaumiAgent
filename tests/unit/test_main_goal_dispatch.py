"""Tests for shared `/goal` command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_goal
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    async def execute(self, **kwargs: Any) -> str:
        return "直接工具结果"


class _EngineFake:
    def __init__(self) -> None:
        self.tool_registry = {
            name: _FakeTool()
            for name in (
                "goal_create",
                "goal_status",
                "goal_list",
                "goal_update",
                "goal_pursue",
            )
        }
        self.executed: list[tuple[ToolCall, str | None]] = []

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.executed.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content="目标命令已执行",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "tool_name", "expected_args"),
    [
        ("", "goal_status", {}),
        ("status goal_demo", "goal_status", {"goal_id": "goal_demo"}),
        ("list", "goal_list", {"include_finished": True}),
        ("list --active", "goal_list", {"include_finished": False}),
        ("完善 New UI", "goal_create", {"objective": "完善 New UI"}),
        ("create 完善 Goal", "goal_create", {"objective": "完善 Goal"}),
        ("pause 等待验证", "goal_update", {"status": "paused", "note": "等待验证"}),
        ("resume 继续执行", "goal_update", {"status": "active", "note": "继续执行"}),
        ("block 缺少凭据", "goal_update", {"status": "blocked", "note": "缺少凭据"}),
        ("complete 已验收", "goal_update", {"status": "completed", "note": "已验收"}),
        ("cancel 改变方向", "goal_update", {"status": "cancelled", "note": "改变方向"}),
        ("pursue", "goal_pursue", {}),
    ],
)
async def test_run_goal_routes_all_operations_through_engine_executor(
    argument: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    engine = _EngineFake()

    await _run_goal(engine, argument)

    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["create", "block", "pursue later", "unknown later"])
async def test_run_goal_rejects_invalid_or_ambiguous_operations(argument: str) -> None:
    engine = _EngineFake()

    await _run_goal(engine, argument)

    if argument == "unknown later":
        assert engine.executed
        assert engine.executed[0][0].name == "goal_create"
    else:
        assert engine.executed == []
