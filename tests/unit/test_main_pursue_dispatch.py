"""Tests for main CLI pursuit command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_pursue
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "✅ 目标追踪已启动。"


class _EngineToolCallFake:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self.tool_registry = {tool_name: tool}
        self.executed: list[tuple[ToolCall, str | None]] = []

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.executed.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content="✅ 目标追踪已通过 Engine 启动。",
        )


@pytest.mark.asyncio
async def test_run_pursue_routes_goal_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("pursue_goal", tool)

    await _run_pursue(engine, "修复一个真实缺陷")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursue_goal"
    assert json.loads(tool_call.arguments) == {"goal": "修复一个真实缺陷"}
