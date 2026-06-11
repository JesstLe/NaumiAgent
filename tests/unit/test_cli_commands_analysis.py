"""Tests for shared CLI analysis command dispatch."""

from __future__ import annotations

from typing import Any

import pytest

from naumi_agent.cli.commands_analysis import ANALYSIS_TOOL_NAMES, run_analysis
from naumi_agent.tools.analysis import create_analysis_tools


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "# 分析完成\n\n真实工具已执行。"


class _FakeEngine:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self.tool_registry = {tool_name: tool}


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


def test_analysis_command_tool_names_match_registered_tools() -> None:
    registered_names = {tool.name for tool in create_analysis_tools()}

    missing = sorted(set(ANALYSIS_TOOL_NAMES.values()) - registered_names)

    assert missing == []
