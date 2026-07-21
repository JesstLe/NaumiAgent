"""Textual Agent Control Center parity tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Markdown, Static, TabbedContent

from naumi_agent.agent_control import AgentControlSnapshot
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.shell_worker import (
    ShellSandboxUnavailableError,
    detect_shell_sandbox_backend,
)
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.orchestrator.subagent_manager import StopExecutionResult
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.tools.base import ToolCall
from naumi_agent.tui.agent_control import (
    AgentControlScreen,
    format_agent_control_markdown,
)
from naumi_agent.tui.app import NaumiApp, PermissionConfirmScreen

_ASYNC_TOOL_TIMEOUT_SECONDS = 10.0


def _require_real_shell_backend() -> None:
    try:
        detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


def test_agent_control_formatter_covers_all_authoritative_tabs() -> None:
    snapshot = _snapshot()

    agents = format_agent_control_markdown(snapshot, "agents", "coder")
    executions = format_agent_control_markdown(snapshot, "executions", "task-1")
    team = format_agent_control_markdown(snapshot, "team", "blackboard:team/review")

    assert "Agent Control Center · Agent" in agents
    assert "capable" in agents
    assert "file_read" in agents
    assert "task-1" in executions
    assert "running_tool" in executions
    assert "可停止" in executions
    assert "team/review" in team
    assert "ready" in team


def test_agent_control_formatter_states_empty_data_and_warnings() -> None:
    snapshot = AgentControlSnapshot.from_dict({
        **_snapshot().to_dict(),
        "agents": [],
        "executions": [],
        "team_messages": [],
        "blackboard": [],
        "warnings": ["消息总线暂时不可用。"],
    })

    assert "暂无 Agent" in format_agent_control_markdown(snapshot, "agents", "")
    assert "暂无执行记录" in format_agent_control_markdown(snapshot, "executions", "")
    team = format_agent_control_markdown(snapshot, "team", "")
    assert "暂无团队消息或黑板记录" in team
    assert "消息总线暂时不可用" in team


@pytest.mark.asyncio
async def test_textual_agent_control_loads_switches_and_confirms_stop() -> None:
    engine = AgentEngine(AppConfig())
    running = _snapshot()
    terminal = AgentControlSnapshot.from_dict({
        **running.to_dict(),
        "revision": 2,
        "executions": [{
            **running.to_dict()["executions"][0],
            "status": "cancelled",
            "phase": "finished",
            "stop_supported": False,
            "stop_requested": True,
        }],
    })
    engine.agent_control.snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[running, terminal]
    )
    engine.subagent_manager.stop_execution = AsyncMock(  # type: ignore[method-assign]
        return_value=StopExecutionResult(
            task_id="task-1",
            accepted=True,
            code="accepted",
            message="已请求停止。",
        )
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, AgentControlScreen)
        assert screen.query_one(TabbedContent).active == "agents"
        assert "coder" in screen.query_one("#agent-content-agents", Markdown)._markdown

        await pilot.press("]")
        await pilot.pause(0.05)
        assert screen.query_one(TabbedContent).active == "executions"
        await pilot.press("x")
        assert "确认停止" in str(screen.query_one("#agent-error", Static).render())
        await pilot.press("n")
        engine.subagent_manager.stop_execution.assert_not_awaited()
        await pilot.press("x")
        assert "确认停止" in str(screen.query_one("#agent-error", Static).render())
        await pilot.press("y")
        await pilot.pause(0.15)

        engine.subagent_manager.stop_execution.assert_awaited_once_with(
            "task-1",
            "用户在 Textual Agent 控制中心确认停止。",
        )
        assert "cancelled" in screen.query_one(
            "#agent-content-executions", Markdown
        )._markdown


@pytest.mark.asyncio
async def test_textual_agent_control_retains_snapshot_on_refresh_error() -> None:
    engine = AgentEngine(AppConfig())
    engine.agent_control.snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_snapshot(), RuntimeError("message bus unavailable")]
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, AgentControlScreen)
        await pilot.press("r")
        await pilot.pause(0.1)

        assert "coder" in screen.query_one(
            "#agent-content-agents", Markdown
        )._markdown
        assert "已保留上一次快照" in str(
            screen.query_one("#agent-error", Static).render()
        )


@pytest.mark.asyncio
async def test_textual_agents_slash_route_and_permission_modal_priority() -> None:
    engine = AgentEngine(AppConfig())
    engine.agent_control.snapshot = AsyncMock(return_value=_snapshot())  # type: ignore[method-assign]
    engine.subagent_manager.stop_execution = AsyncMock()  # type: ignore[method-assign]
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 34)) as pilot:
        app._handle_slash_command("/agents")
        await pilot.pause(0.1)
        assert isinstance(app.screen, AgentControlScreen)
        await pilot.press("]")
        await pilot.pause(0.05)
        assert app.screen.selected_tab == "executions"

        app.push_screen(PermissionConfirmScreen({
            "tool_name": "code_execute",
            "reason": "需要确认。",
            "arguments": {},
        }))
        await pilot.pause(0.05)
        await pilot.press("x")
        await pilot.pause(0.05)
        assert isinstance(app.screen, PermissionConfirmScreen)
        engine.subagent_manager.stop_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_textual_bypass_confirmation_enables_full_permission_mode() -> None:
    _require_real_shell_backend()
    engine = AgentEngine(AppConfig())
    session = await engine.get_or_create_session()
    app = NaumiApp(engine)

    try:
        async with app.run_test(size=(110, 34)) as pilot:
            execution = asyncio.create_task(
                engine.execute_tool(
                    ToolCall(
                        id="tui-bypass",
                        name="bash_run",
                        arguments='{"command": "printf tui-bypass"}',
                    ),
                    agent_name="tui",
                )
            )
            for _ in range(200):
                if isinstance(app.screen, PermissionConfirmScreen):
                    break
                await pilot.pause(0.05)
            else:
                pytest.fail("Bypass 权限确认弹窗未在 10 秒内就绪。")
            assert "全权限" in str(app.screen.query_one("#bypass", Button).label)
            bypass = app.screen.query_one("#bypass", Button)
            bypass.focus()
            await pilot.press("enter")
            result = await asyncio.wait_for(
                execution,
                timeout=_ASYNC_TOOL_TIMEOUT_SECONDS,
            )

            assert result.status == "success"
            assert "tui-bypass" in result.content
            assert engine.runtime_mode is AgentRuntimeMode.BYPASS
            assert engine.permission_mode is PermissionMode.BYPASS
            receipt = engine.list_permission_decision_receipts()[-1]
            assert receipt.session_id == session.id
            assert receipt.agent_name == "tui"
    finally:
        await engine.shutdown()


def _snapshot() -> AgentControlSnapshot:
    return AgentControlSnapshot.from_dict({
        "schema_version": 1,
        "session_id": "session-tui-agents",
        "revision": 1,
        "generated_at": "2026-07-13T00:00:00+00:00",
        "summary": {
            "total_agents": 1,
            "active_agents": 1,
            "attention_agents": 0,
            "stoppable_executions": 1,
            "pending_messages": 1,
        },
        "agents": [{
            "name": "coder",
            "description": "编程 Agent",
            "kind": "preset",
            "state": "running",
            "task_count": 1,
            "model_tier": "capable",
            "capabilities": ["代码"],
            "tools": ["file_read"],
            "permission_level": "moderate",
            "age_ms": 500,
            "heartbeat_age_ms": 100,
        }],
        "executions": [{
            "task_id": "task-1",
            "session_id": "session-tui-agents",
            "agent_name": "coder",
            "description": "实现 Textual 控制中心",
            "status": "running",
            "phase": "running_tool",
            "started_at": 1,
            "finished_at": None,
            "elapsed_ms": 1000,
            "heartbeat_age_ms": 100,
            "current_tool": "file_read",
            "recent_tools": ["file_read"],
            "total_tokens": 42,
            "total_cost_usd": 0.01,
            "turns": 2,
            "error": "",
            "stop_supported": True,
            "stop_requested": False,
        }],
        "team_messages": [{
            "sender": "coder",
            "recipient": "reviewer",
            "topic": "review",
            "priority": "high",
            "timestamp": 1,
            "content": "请检查实现",
        }],
        "blackboard": [{
            "key": "team/review",
            "author": "coder",
            "version": 1,
            "timestamp": 1,
            "value_summary": "ready",
        }],
        "warnings": [],
    })
