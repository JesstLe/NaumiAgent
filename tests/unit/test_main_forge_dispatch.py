"""Tests for main CLI forge command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_forge
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse("class GeneratedTool: pass")


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "工具锻造已执行。"


class _EngineToolCallFake:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self._router = _FakeRouter()
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
            content="工具锻造已通过 Engine 执行。",
        )


@pytest.mark.asyncio
async def test_run_forge_routes_save_step_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("forge_tool", tool)

    await _run_forge(engine, "统计代码注释率的工具")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "forge_tool"
    assert json.loads(tool_call.arguments) == {
        "description": "统计代码注释率的工具",
        "llm_output": "class GeneratedTool: pass",
    }
