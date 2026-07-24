"""Focused tests for CLI Doctor export dispatch."""

from __future__ import annotations

import json

import pytest
from rich.console import Console

from naumi_agent.main import _run_doctor_command
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeTool:
    pass


class _EngineToolCallFake:
    def __init__(self) -> None:
        self.tool_registry = {
            "doctor_export_diagnostics": _FakeTool(),
            "doctor_live_probe": _FakeTool(),
        }
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
            content="诊断导出已通过 Engine 执行。",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arg", "expected_args"),
    [
        ("export", {"action": "preview"}),
        (
            "export " + "A" * 64,
            {
                "action": "write",
                "expected_snapshot_sha256": "a" * 64,
            },
        ),
    ],
)
async def test_run_doctor_export_routes_through_engine_policy(
    arg: str,
    expected_args: dict[str, str],
) -> None:
    engine = _EngineToolCallFake()

    await _run_doctor_command(engine, arg)

    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "doctor_export_diagnostics"
    assert json.loads(tool_call.arguments) == expected_args


@pytest.mark.asyncio
async def test_run_doctor_export_rejects_unpreviewed_or_ambiguous_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _EngineToolCallFake()
    recorded = Console(record=True, width=120)
    monkeypatch.setattr("naumi_agent.main.console", recorded)

    await _run_doctor_command(engine, "export not-a-digest")
    await _run_doctor_command(engine, "export abc extra")
    await _run_doctor_command(engine, '"export')

    assert engine.executed == []
    output = recorded.export_text()
    assert output.count("用法: /doctor") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arg", "timeout_ms"),
    [("probe", 15_000), ("probe 12000", 12_000)],
)
async def test_run_doctor_probe_routes_through_engine_policy(
    arg: str,
    timeout_ms: int,
) -> None:
    engine = _EngineToolCallFake()

    await _run_doctor_command(engine, arg)

    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "doctor_live_probe"
    assert json.loads(tool_call.arguments) == {"timeout_ms": timeout_ms}
