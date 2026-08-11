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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "tool_name", "expected_args"),
    [
        ("list --active", "pursuit_list", {"active_only": True}),
        ("status run-1", "pursuit_status", {"run_id": "run-1"}),
        ("resume run-1", "pursuit_resume", {"run_id": "run-1"}),
        ("outbox run-now", "pursuit_terminal_outbox_run_now", {}),
        (
            "outbox requeue ptfail_" + "a" * 24,
            "pursuit_terminal_dead_letter_requeue",
            {"dead_letter_id": "ptfail_" + "a" * 24},
        ),
        (
            "outbox abandon ptfail_" + "a" * 24 + " superseded",
            "pursuit_terminal_dead_letter_abandon",
            {
                "dead_letter_id": "ptfail_" + "a" * 24,
                "reason": "superseded",
            },
        ),
        (
            "outbox retention-preview --retention-days 45 --limit 5 "
            "--scan-limit 10 --assessed-at 2026-08-11T08:00:00+08:00",
            "pursuit_terminal_outbox_retention_preview",
            {
                "retention_days": 45,
                "limit": 5,
                "scan_limit": 10,
                "assessed_at": "2026-08-11T08:00:00+08:00",
            },
        ),
        (
            "outbox retention-admit ptorpv_" + "a" * 24 + " " + "a" * 64
            + " --retention-days 45 --limit 5 --scan-limit 10 "
            "--assessed-at 2026-08-11T08:00:00+08:00",
            "pursuit_terminal_outbox_retention_admission",
            {
                "preview_id": "ptorpv_" + "a" * 24,
                "preview_sha256": "a" * 64,
                "retention_days": 45,
                "limit": 5,
                "scan_limit": 10,
                "assessed_at": "2026-08-11T08:00:00+08:00",
            },
        ),
        (
            "outbox retention-prune ptora_" + "a" * 24 + " " + "a" * 64
            + " --execute",
            "pursuit_terminal_outbox_retention_prune",
            {
                "admission_id": "ptora_" + "a" * 24,
                "admission_sha256": "a" * 64,
                "execute": True,
            },
        ),
        (
            "reconcile recovery-" + "a" * 64,
            "pursuit_reconcile",
            {"attempt_id": "recovery-" + "a" * 64},
        ),
    ],
)
async def test_run_pursue_meta_routes_through_engine_tool_executor(
    command: str,
    tool_name: str,
    expected_args: dict[str, object],
) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake(tool_name, tool)

    await _run_pursue(engine, command)

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == tool_name
    assert json.loads(tool_call.arguments) == expected_args
