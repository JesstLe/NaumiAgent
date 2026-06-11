"""Tests for main CLI browser daemon command dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from naumi_agent.main import _run_browser_daemon
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "浏览器 daemon 命令已执行。"


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
            content="浏览器 daemon 命令已通过 Engine 执行。",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "tool_name", "expected_args"),
    [
        ("", "browser_daemon_health", {}),
        ("start", "browser_daemon_start", {}),
        ("dashboard", "browser_daemon_dashboard", {}),
        (
            "run 检查登录页按钮",
            "browser_daemon_run",
            {"task_instruction": "检查登录页按钮"},
        ),
        ("list 7", "browser_daemon_list_runs", {"limit": 7}),
        ("runs bad", "browser_daemon_list_runs", {"limit": 20}),
        ("status run_1", "browser_daemon_run_status", {"run_id": "run_1"}),
        (
            "watch run_1 12345",
            "browser_daemon_watch",
            {"run_id": "run_1", "timeout_ms": 12345},
        ),
        (
            "reply run_1 继续点击确认",
            "browser_daemon_reply",
            {"run_id": "run_1", "instruction": "继续点击确认"},
        ),
        (
            "resume run_1 继续执行",
            "browser_daemon_resume",
            {"run_id": "run_1", "instruction": "继续执行"},
        ),
        (
            "abort run_1 用户取消",
            "browser_daemon_abort",
            {"run_id": "run_1", "reason": "用户取消"},
        ),
        (
            "manual run_1 需要人工登录",
            "browser_daemon_manual_control",
            {"run_id": "run_1", "reason": "需要人工登录"},
        ),
    ],
)
async def test_run_browser_daemon_routes_subcommands_through_engine_tool_executor(
    command: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake(tool_name, tool)

    await _run_browser_daemon(engine, command)

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args
