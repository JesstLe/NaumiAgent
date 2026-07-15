"""Tests for main CLI background command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_background
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "后台任务已执行。"


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
            content="后台任务已通过 Engine 执行。",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "tool_name", "expected_args"),
    [
        ("run python worker.py --flag", "background_run", {"command": "python worker.py --flag"}),
        ("status bg_1", "background_status", {"task_id": "bg_1"}),
        ("list", "background_list", {}),
        ("cancel bg_1", "background_cancel", {"task_id": "bg_1"}),
        ("cleanup", "background_cleanup", {}),
        ("output bg_1", "background_read_output", {"task_id": "bg_1"}),
    ],
)
async def test_run_background_routes_subcommands_through_engine_tool_executor(
    command: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake(tool_name, tool)

    await _run_background(engine, command)

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args
