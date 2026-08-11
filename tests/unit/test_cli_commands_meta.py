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
async def test_shared_pursue_reconcile_routes_attempt_identity(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="恢复请求已完成机械对账。")
    engine.tool_registry = {"pursuit_reconcile": engine.tool}
    attempt_id = "recovery-" + "a" * 64

    await commands_meta.run_pursue(
        engine,
        f"reconcile {attempt_id}",
    )

    assert engine.tool.calls == []
    assert len(engine.calls) == 1
    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_reconcile"
    assert json.loads(tool_call.arguments) == {"attempt_id": attempt_id}
    assert "恢复请求已完成机械对账" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_empty_bounded_action(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="暂无到期记录。")
    engine.tool_registry = {"pursuit_terminal_outbox_run_now": engine.tool}

    await commands_meta.run_pursue(engine, "outbox run-now")

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_outbox_run_now"
    assert json.loads(tool_call.arguments) == {}
    assert "暂无到期记录" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_exact_dead_letter_requeue(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="死信已重新加入恢复队列。")
    engine.tool_registry = {"pursuit_terminal_dead_letter_requeue": engine.tool}
    dead_letter_id = "ptfail_" + "a" * 24

    await commands_meta.run_pursue(
        engine,
        f"outbox requeue {dead_letter_id}",
    )

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_dead_letter_requeue"
    assert json.loads(tool_call.arguments) == {"dead_letter_id": dead_letter_id}
    assert "重新加入恢复队列" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_exact_dead_letter_abandon(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="死信已永久停止后续投递。")
    engine.tool_registry = {"pursuit_terminal_dead_letter_abandon": engine.tool}
    dead_letter_id = "ptfail_" + "a" * 24

    await commands_meta.run_pursue(
        engine,
        f"outbox abandon {dead_letter_id} superseded",
    )

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_dead_letter_abandon"
    assert json.loads(tool_call.arguments) == {
        "dead_letter_id": dead_letter_id,
        "reason": "superseded",
    }


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_retention_preview(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="只读候选预演，不是删除授权。")
    engine.tool_registry = {
        "pursuit_terminal_outbox_retention_preview": engine.tool,
    }

    await commands_meta.run_pursue(
        engine,
        "outbox retention-preview --retention-days 45 --limit 5 "
        "--scan-limit 10 --assessed-at 2026-08-11T08:00:00+08:00",
    )

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_outbox_retention_preview"
    assert json.loads(tool_call.arguments) == {
        "retention_days": 45,
        "limit": 5,
        "scan_limit": 10,
        "assessed_at": "2026-08-11T08:00:00+08:00",
    }
    assert "只读候选预演" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_retention_admission(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="准入计划已持久化，没有删除记录。")
    engine.tool_registry = {
        "pursuit_terminal_outbox_retention_admission": engine.tool,
    }
    digest = "a" * 64

    await commands_meta.run_pursue(
        engine,
        f"outbox retention-admit ptorpv_{digest[:24]} {digest} "
        "--retention-days 45 --limit 5 --scan-limit 10 "
        "--assessed-at 2026-08-11T08:00:00+08:00",
    )

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_outbox_retention_admission"
    assert json.loads(tool_call.arguments) == {
        "preview_id": f"ptorpv_{digest[:24]}",
        "preview_sha256": digest,
        "retention_days": 45,
        "limit": 5,
        "scan_limit": 10,
        "assessed_at": "2026-08-11T08:00:00+08:00",
    }
    assert "准入计划已持久化" in rendered_console.getvalue()


@pytest.mark.asyncio
async def test_shared_pursue_outbox_routes_retention_prune(
    rendered_console: StringIO,
) -> None:
    engine = _EngineFacadeFake(content="默认 dry-run，未删除记录。")
    engine.tool_registry = {
        "pursuit_terminal_outbox_retention_prune": engine.tool,
    }
    digest = "a" * 64

    await commands_meta.run_pursue(
        engine,
        f"outbox retention-prune ptora_{digest[:24]} {digest}",
    )

    tool_call, agent_name = engine.calls[0]
    assert agent_name == "cli"
    assert tool_call.name == "pursuit_terminal_outbox_retention_prune"
    assert json.loads(tool_call.arguments) == {
        "admission_id": f"ptora_{digest[:24]}",
        "admission_sha256": digest,
        "execute": False,
    }
    assert "默认 dry-run" in rendered_console.getvalue()


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
async def test_delete_session_command_reports_artifact_gc_counts(
    rendered_console: StringIO,
) -> None:
    class Engine:
        async def delete_session_detailed(self, session_id: str):
            return SimpleNamespace(
                outcome=ReconciliationCoordinatorOutcome.COMPLETED,
                message=(
                    "Session、Harness 记录与 Artifact 协调完成；"
                    "Artifact 删除 1、已缺失 0、保留共享 2、跳过风险 1。"
                ),
            )

    await commands_meta.delete_session(Engine(), "session-1")

    rendered = rendered_console.getvalue()
    assert "Artifact 删除 1" in rendered
    assert "保留共享 2" in rendered


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
