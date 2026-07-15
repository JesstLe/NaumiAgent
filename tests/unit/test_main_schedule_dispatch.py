"""Tests for main CLI schedule command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_schedule
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "调度命令已执行。"


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
            content="调度命令已通过 Engine 执行。",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "tool_name", "expected_args"),
    [
        (
            "create once 2026-06-12T10:00:00 复查后台任务",
            "schedule_create",
            {
                "kind": "once",
                "expression": "2026-06-12T10:00:00",
                "prompt": "复查后台任务",
            },
        ),
        ("list --active", "schedule_list", {"active_only": True}),
        ("cancel sch_1", "schedule_cancel", {"schedule_id": "sch_1"}),
        ("pause sch_1", "schedule_pause", {"schedule_id": "sch_1"}),
        ("resume sch_1", "schedule_resume", {"schedule_id": "sch_1"}),
    ],
)
async def test_run_schedule_routes_subcommands_through_engine_tool_executor(
    command: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake(tool_name, tool)

    await _run_schedule(engine, command)

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args
