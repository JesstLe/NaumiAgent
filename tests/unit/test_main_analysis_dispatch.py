"""Tests for main CLI analysis dispatch semantics."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_analysis
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "# 分析完成\n\n测试输出。"


class _FakeEngine:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self.tool_registry = {tool_name: tool}

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        del agent_name
        tool = self.tool_registry[tool_call.name]
        content = await tool.execute(**json.loads(tool_call.arguments))
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content=content,
        )


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
            content="# 分析完成\n\nEngine 执行器已执行。",
        )


@pytest.mark.asyncio
async def test_run_analysis_passes_page_session_context() -> None:
    tool = _FakeTool()
    engine = _FakeEngine("analysis_page", tool)

    await _run_analysis(engine, "page", "用户消息很多，需要分页")

    assert tool.calls == [{"session_context": "用户消息很多，需要分页"}]


@pytest.mark.asyncio
async def test_run_analysis_routes_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("analysis_page", tool)

    await _run_analysis(engine, "page", "用户消息很多，需要分页")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "analysis_page"
    assert json.loads(tool_call.arguments) == {
        "session_context": "用户消息很多，需要分页"
    }
