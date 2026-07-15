"""Tests for main CLI self-review command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_self_review
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "自我审查已执行。"


class _EngineToolCallFake:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self.tool_registry = {tool_name: tool}
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
            content="自我审查已通过 Engine 执行。",
        )


@pytest.mark.asyncio
async def test_run_self_review_routes_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_review", tool)

    await _run_self_review(engine, "architecture src/naumi_agent")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "self_review"
    assert json.loads(tool_call.arguments) == {
        "focus": "architecture",
        "module": "src/naumi_agent",
    }
