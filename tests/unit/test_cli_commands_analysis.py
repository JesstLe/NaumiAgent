"""Tests for shared CLI analysis command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.cli.commands_analysis import ANALYSIS_TOOL_NAMES, run_analysis
from naumi_agent.tools.analysis import create_analysis_tools
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "# 分析完成\n\n真实工具已执行。"


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
async def test_run_analysis_uses_registered_analysis_tool_names() -> None:
    tool = _FakeTool()
    engine = _FakeEngine("analysis_chaos", tool)

    await run_analysis(engine, "chaos", "src/naumi_agent")

    assert tool.calls == [{"target": "src/naumi_agent"}]


@pytest.mark.asyncio
async def test_run_analysis_passes_mode_specific_tool_kwargs() -> None:
    tool = _FakeTool()
    engine = _FakeEngine("analysis_vibe", tool)

    await run_analysis(engine, "vibe", "构建一个 TODO demo")

    assert tool.calls == [{"description": "构建一个 TODO demo"}]


@pytest.mark.asyncio
async def test_run_analysis_routes_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("analysis_vibe", tool)

    await run_analysis(engine, "vibe", "构建一个 TODO demo")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "analysis_vibe"
    assert json.loads(tool_call.arguments) == {"description": "构建一个 TODO demo"}


@pytest.mark.asyncio
async def test_run_analysis_parses_scale_qps_like_main_cli() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("analysis_scale", tool)

    await run_analysis(engine, "scale", "50000")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, _agent_name = engine.executed[0]
    assert tool_call.name == "analysis_scale"
    assert json.loads(tool_call.arguments) == {"target": "当前项目", "qps": 50000}


def test_analysis_command_tool_names_match_registered_tools() -> None:
    registered_names = {tool.name for tool in create_analysis_tools()}

    missing = sorted(set(ANALYSIS_TOOL_NAMES.values()) - registered_names)

    assert missing == []
