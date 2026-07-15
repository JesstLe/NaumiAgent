"""Tests for main CLI worktree command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_worktree
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "worktree 命令已执行。"


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
            content="worktree 命令已通过 Engine 执行。",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "tool_name", "expected_args"),
    [
        ("status demo", "worktree_status", {"name": "demo"}),
        ("create demo task-1", "worktree_create", {"name": "demo", "task_id": "task-1"}),
        ("bind demo task-1", "worktree_bind_task", {"name": "demo", "task_id": "task-1"}),
        ("keep demo 需要人工复查", "worktree_keep", {"name": "demo", "reason": "需要人工复查"}),
        ("remove demo --discard", "worktree_remove", {"name": "demo", "discard_changes": True}),
    ],
)
async def test_run_worktree_routes_subcommands_through_engine_tool_executor(
    command: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake(tool_name, tool)

    await _run_worktree(engine, command)

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args
