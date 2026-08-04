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
from naumi_agent.orchestrator.subagent_manager import (
    AgentRecoveryActionResult,
    StopExecutionResult,
)
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
    results = format_agent_control_markdown(snapshot, "results", "delivery-1")
    recovery = format_agent_control_markdown(
        snapshot,
        "recovery",
        "recovery:job:agent-job-recovery",
    )
    team = format_agent_control_markdown(snapshot, "team", "blackboard:team/review")

    assert "Agent Control Center · Agent" in agents
    assert "capable" in agents
    assert "file_read" in agents
    assert "task-1" in executions
    assert "running_tool" in executions
    assert "可停止" in executions
    assert "Worker 合同" in executions
    assert "aaaaaaaaaaaa" in executions
    assert "file_read" in executions
    assert "持久任务" in executions
    assert "agent-job-1" in executions
    assert "epoch 3" in executions
    assert "共享 Agent capacity" in executions
    assert "1/4" in executions
    assert "持久结果" in results
    assert "结果正文" in results
    assert "已经脱敏或截断" in results
    assert "running Job 需要恢复裁决" in recovery
    assert "agent-job-recovery" in recovery
    assert "按 `u`" in recovery
    assert "不会自动重放模型" in recovery
    assert "team/review" in team
    assert "ready" in team


def test_agent_control_formatter_states_empty_data_and_warnings() -> None:
    snapshot = AgentControlSnapshot.from_dict({
        **_snapshot().to_dict(),
        "agents": [],
        "executions": [],
        "results": [],
        "recovery_catalog": {"assessed_at": "", "items": [], "truncated": False},
        "team_messages": [],
        "blackboard": [],
        "warnings": ["消息总线暂时不可用。"],
    })

    assert "暂无 Agent" in format_agent_control_markdown(snapshot, "agents", "")
    assert "暂无执行记录" in format_agent_control_markdown(snapshot, "executions", "")
    assert "暂无持久结果" in format_agent_control_markdown(snapshot, "results", "")
    assert "暂无 Agent 恢复条目" in format_agent_control_markdown(
        snapshot, "recovery", ""
    )
    team = format_agent_control_markdown(snapshot, "team", "")
    assert "暂无团队消息或黑板记录" in team
    assert "消息总线暂时不可用" in team


def test_agent_control_formatter_highlights_quarantined_publication() -> None:
    payload = _snapshot().to_dict()
    payload["summary"]["durable_publications_quarantined"] = 1
    payload["recovery_catalog"] = {
        "assessed_at": "2026-08-05T12:00:01+00:00",
        "items": [{
            "kind": "publication",
            "item_id": "publication-quarantined",
            "job_id": "agent-job-quarantined",
            "publication_id": "publication-quarantined",
            "agent_name": "coder",
            "job_state": "completed",
            "recovery_state": "publication_quarantined",
            "session_scope": "current",
            "claim_epoch": 2,
            "claim_expires_at": "",
            "attempt_count": 5,
            "occurred_at": "2026-08-05T12:00:00+00:00",
            "request_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
            "reason_code": "agent_publication_recovery_delivery_failed",
        }],
        "truncated": False,
    }
    rendered = format_agent_control_markdown(
        AgentControlSnapshot.from_dict(payload),
        "recovery",
        "recovery:publication:publication-quarantined",
    )

    assert "已隔离 1" in rendered
    assert "发布失败已隔离" in rendered
    assert "投递尝试：5" in rendered
    assert "agent_publication_recovery_delivery_failed" in rendered


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
        await pilot.press("]")
        await pilot.pause(0.05)
        assert screen.query_one(TabbedContent).active == "results"
        assert "结果正文" in screen.query_one(
            "#agent-content-results", Markdown
        )._markdown


@pytest.mark.asyncio
async def test_textual_agent_control_resolves_exact_recovery_without_second_confirm() -> None:
    engine = AgentEngine(AppConfig())
    session = await engine.get_or_create_session(title="Agent Recovery TUI")
    running = _snapshot()
    recovery_item = running.to_dict()["recovery_catalog"]["items"][0]
    resolved = AgentControlSnapshot.from_dict({
        **running.to_dict(),
        "revision": 2,
        "recovery_catalog": {
            "assessed_at": "2026-07-13T00:00:03+00:00",
            "items": [{
                **recovery_item,
                "job_state": "unknown",
                "recovery_state": "outcome_unknown",
                "claim_expires_at": "2026-07-13T00:00:01+00:00",
                "receipt_sha256": "f" * 64,
                "reason_code": "agent_job_recovery_unknown",
            }],
            "truncated": False,
        },
    })
    engine.agent_control.snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[running, resolved]
    )
    engine.subagent_manager.resolve_recovery_unknown = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentRecoveryActionResult(
            action="resolve_unknown",
            job_id="agent-job-recovery",
            accepted=True,
            applied=True,
            code="recovery_resolved_unknown",
            message="已将过期 running Agent Job 收口为 unknown。",
            job_state="unknown",
            claim_epoch=4,
            receipt_sha256="f" * 64,
        )
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("ctrl+g")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, AgentControlScreen)
        await pilot.press("]", "]", "]")
        await pilot.pause(0.05)
        assert screen.selected_tab == "recovery"
        await pilot.press("u")
        await pilot.pause(0.15)

        engine.subagent_manager.resolve_recovery_unknown.assert_awaited_once_with(
            session_id=session.id,
            job_id="agent-job-recovery",
            expected_request_sha256="d" * 64,
            expected_claim_epoch=4,
            expected_latest_receipt_sha256="e" * 64,
        )
        assert "执行结果未知" in screen.query_one(
            "#agent-content-recovery", Markdown
        )._markdown
        assert "unknown" in str(screen.query_one("#agent-error", Static).render())


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
        "schema_version": 5,
        "session_id": "session-tui-agents",
        "revision": 1,
        "generated_at": "2026-07-13T00:00:00+00:00",
        "summary": {
            "total_agents": 1,
            "active_agents": 1,
            "attention_agents": 0,
            "stoppable_executions": 1,
            "pending_messages": 1,
            "durable_capacity_configured": True,
            "durable_active_jobs": 1,
            "durable_max_active_jobs": 4,
            "durable_waiting_jobs": 1,
            "durable_max_waiters": 64,
            "durable_reclaimable_jobs": 0,
            "durable_recovery_required_jobs": 0,
            "durable_results_visible": 1,
            "durable_publications_pending": 0,
            "durable_publications_claimed": 0,
            "durable_publications_expired": 0,
            "durable_publications_quarantined": 0,
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
        "results": [{
            "delivery_id": "delivery-1",
            "publication_id": "publication-1",
            "job_id": "agent-job-1",
            "task_id": "result-task",
            "agent_name": "coder",
            "status": "completed",
            "delivered_at": "2026-07-13T00:00:01+00:00",
            "result_sha256": "b" * 64,
            "delivery_sha256": "c" * 64,
            "task_excerpt": "验证结果收件箱",
            "response_excerpt": "结果正文",
            "error_excerpt": "",
            "content_truncated": True,
            "response_bytes": 12,
            "total_tokens": 8,
            "total_cost_usd": 0.001,
            "turns": 1,
            "reason_code": "agent_completed",
        }],
        "recovery_catalog": {
            "assessed_at": "2026-07-13T00:00:02+00:00",
            "items": [{
                "kind": "job",
                "item_id": "agent-job-recovery",
                "job_id": "agent-job-recovery",
                "publication_id": "",
                "agent_name": "coder",
                "job_state": "running",
                "recovery_state": "recovery_required",
                "session_scope": "current",
                "claim_epoch": 4,
                "claim_expires_at": "2026-07-13T00:00:01+00:00",
                "attempt_count": 0,
                "occurred_at": "2026-07-13T00:00:00+00:00",
                "request_sha256": "d" * 64,
                "receipt_sha256": "e" * 64,
                "reason_code": "agent_job_running",
            }],
            "truncated": False,
        },
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
            "heartbeat_subject_id": "agent-execution-test",
            "heartbeat_phase": "running",
            "heartbeat_failure_code": "",
            "worker_request_sha256": "a" * 64,
            "worker_result_sha256": "",
            "worker_tool_scope": ["file_read"],
            "worker_contract_failure_code": "",
            "worker_job_id": "agent-job-1234567890",
            "worker_job_state": "running",
            "worker_claim_epoch": 3,
            "worker_job_failure_code": "",
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
