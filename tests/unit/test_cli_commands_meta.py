"""Tests for shared CLI meta-command tool execution."""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from naumi_agent.cli import commands_meta
from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.runtime.ports.events import EventSink, RuntimeEvent, RuntimeEventType
from naumi_agent.tools.base import ToolCall, ToolResult


class _DirectToolTrap:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        raise AssertionError("shared CLI 不得直接调用 Tool.execute")


class _EngineFacadeFake:
    def __init__(self, *, status: str = "success", content: str = "隔离区状态正常。") -> None:
        self.tool = _DirectToolTrap()
        self.tool_registry = {"worktree_status": self.tool}
        self.status = status
        self.content = content
        self.calls: list[tuple[ToolCall, str | None]] = []

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.calls.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status=self.status,
            content=self.content,
        )


@pytest.fixture
def rendered_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        commands_meta,
        "console",
        Console(file=output, force_terminal=False, width=100),
    )
    return output


@pytest.mark.asyncio
async def test_shared_meta_command_uses_public_engine_facade(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake()

    await commands_meta.run_worktree(engine, "status demo")

    assert engine.tool.calls == []
    assert len(engine.calls) == 1
    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "worktree_status"
    assert json.loads(tool_call.arguments) == {"name": "demo"}
    assert "隔离区状态正常" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_meta_command_stops_after_engine_tool_failure(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(status="error", content="权限策略拒绝了本次操作。")

    await commands_meta.run_worktree(engine, "status demo")

    rendered = rendered_console.getvalue()
    assert "权限策略拒绝了本次操作" in rendered
    assert "Worktree 隔离区" not in rendered
    assert engine.tool.calls == []


@pytest.mark.asyncio
async def test_delete_session_command_reports_durable_retry_request(
    rendered_console: StringIO,
) -> None:
    class Engine:
        async def delete_session_detailed(self, session_id: str):
            assert session_id == "session-1"
            return SimpleNamespace(
                outcome=ReconciliationCoordinatorOutcome.RETRY_SCHEDULED,
                request_id="request-1",
            )

    await commands_meta.delete_session(Engine(), "session-1")

    rendered = rendered_console.getvalue()
    assert "删除协调等待安全重试" in rendered
    assert "request-1" in rendered


@pytest.mark.asyncio
async def test_skill_run_passes_explicit_event_sink_to_engine(
    rendered_console: StringIO,
) -> None:
    del rendered_console
    received_sink: EventSink | None = None

    class Skill:
        arguments: list[object] = []

        @staticmethod
        def render(*, arguments: str) -> str:
            return f"执行 {arguments}"

    class SkillLoader:
        @staticmethod
        def get(name: str) -> Skill | None:
            return Skill() if name == "demo" else None

    class Engine:
        skill_loader = SkillLoader()

        async def run_streaming(
            self,
            task: str,
            event_sink: EventSink,
        ) -> object:
            nonlocal received_sink
            assert task == "执行 参数"
            received_sink = event_sink
            await event_sink.emit(RuntimeEvent.create(
                event_type=RuntimeEventType.TOKEN,
                data={"content": "完成"},
                session_id="session-cli",
                run_id="run-cli",
                sequence=1,
            ))
            return type("Result", (), {
                "status": "completed",
                "error": None,
                "response": "",
            })()

    await commands_meta.run_skill(Engine(), "demo", "参数")

    assert isinstance(received_sink, EventSink)
