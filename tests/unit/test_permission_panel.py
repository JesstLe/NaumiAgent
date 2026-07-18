from __future__ import annotations

from types import SimpleNamespace

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionSource,
)
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.ui.permission_panel import (
    build_permission_panel_snapshot,
    permission_panel_payload,
    render_permission_panel_snapshot,
)


class _Engine:
    runtime_mode = SimpleNamespace(value="default")
    permission_mode = PermissionMode.MODERATE

    def get_recent_permission_bubbles(self, limit: int = 12) -> list[dict[str, str]]:
        return [
            {
                "request_id": "hist-1",
                "agent_name": "coder",
                "tool_name": "file_write",
                "status": "confirmed",
                "reason": "用户已允许。",
            },
            {
                "request_id": "hist-2",
                "agent_name": "reviewer",
                "tool_name": "analysis_chaos",
                "status": "allowed",
                "reason": "静态扫描。",
            },
        ][-limit:]

    def list_permission_grants(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                grant_id="grant-shell",
                session_id="session-1",
                tool_family="shell",
                created_at="2026-07-13T00:00:00+00:00",
                expires_at=None,
                source_request_id="perm-1",
            ),
            SimpleNamespace(
                grant_id="grant-browser",
                session_id="session-1",
                tool_family="browser",
                created_at="2026-07-13T00:00:00+00:00",
                expires_at="2026-07-13T01:00:00+00:00",
                source_request_id="perm-2",
            ),
        )


def test_permission_panel_renders_real_policy_metadata_for_pending_tool() -> None:
    snapshot = build_permission_panel_snapshot(
        _Engine(),
        pending={
            "perm-1": {
                "tool_name": "bash_run",
                "reason": "需要启动本地服务。",
            }
        },
    )

    rendered = render_permission_panel_snapshot(snapshot)

    assert "perm-1 main -> bash_run [needs_confirmation]" in rendered
    assert "风险:medium" in rendered
    assert "来源:TOOL_PERMISSIONS:bash_run" in rendered
    assert "确认:需要确认" in rendered
    assert "bypass 全权限放行；不执行确认、路径、命令与次数检查" in rendered


def test_permission_panel_resolves_prefix_and_unknown_tool_policy() -> None:
    snapshot = build_permission_panel_snapshot(
        _Engine(),
        pending={
            "perm-prefix": {"tool_name": "analysis_chaos", "reason": "审查项目。"},
            "perm-unknown": {"tool_name": "unknown_tool", "reason": "未知能力。"},
        },
    )

    rendered = render_permission_panel_snapshot(snapshot)

    assert "来源:PREFIX_PERMISSIONS:analysis_" in rendered
    assert "风险:low" in rendered
    assert "perm-unknown main -> unknown_tool [needs_confirmation]" in rendered
    assert "来源:unknown_tool" in rendered
    assert "其他模式未知工具会被拒绝" in rendered
    assert "bypass 全权限放行" in rendered


def test_permission_panel_includes_active_session_grants() -> None:
    snapshot = build_permission_panel_snapshot(_Engine())

    rendered = render_permission_panel_snapshot(snapshot)

    assert [grant["grant_id"] for grant in snapshot.grants] == ["grant-shell", "grant-browser"]
    assert "有效授权" in rendered
    assert "grant-shell shell [本会话]" in rendered
    assert "grant-browser browser [有效至 2026-07-13T01:00:00+00:00]" in rendered
    assert "[session]" not in rendered
    assert "[until" not in rendered


def test_permission_panel_prefers_durable_terminal_decision_history() -> None:
    engine = _Engine()
    engine.list_permission_decision_receipts = lambda limit=12: (
        SimpleNamespace(
            receipt_id="receipt-1",
            request_id="call-1",
            session_id="session-1",
            run_id="run-1",
            call_id="call-1",
            agent_name="main",
            tool_name="bash_run",
            tool_family="shell",
            outcome=PermissionDecisionOutcome.ALLOW_ONCE,
            actor=PermissionDecisionActor.USER,
            source=PermissionDecisionSource.USER_CONFIRMATION,
            risk_level="high",
            decided_at="2026-07-19T08:00:00+00:00",
        ),
    )

    snapshot = build_permission_panel_snapshot(engine)

    assert len(snapshot.history) == 1
    assert snapshot.history[0]["status"] == "allow_once"
    assert snapshot.history[0]["receipt_id"] == "receipt-1"
    assert "用户已允许本次工具执行" in snapshot.history[0]["reason"]
    payload = permission_panel_payload(snapshot)
    assert payload["history"][0]["actor"] == "user"
    assert payload["history"][0]["source"] == "user_confirmation"
    assert payload["history"][0]["decided_at"] == "2026-07-19T08:00:00+00:00"
    assert "操作者:user" in render_permission_panel_snapshot(snapshot)


def test_permission_panel_payload_is_typed_bounded_and_private_field_free() -> None:
    pending = {
        f"perm-{index}": {
            "call_id": f"call-{index}",
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_name": "main",
            "tool_name": "bash_run",
            "tool_family": "shell",
            "arguments_summary": "command=echo safe",
            "reason": "需要执行定向检查。" + "x" * 600,
            "risk_level": "medium",
            "choices": ["allow_once", "deny", "grant_session"],
            "private_payload": "must-not-leak",
        }
        for index in range(60)
    }
    snapshot = build_permission_panel_snapshot(_Engine(), pending=pending, limit=50)

    payload = permission_panel_payload(snapshot)

    assert payload["schema_version"] == 1
    assert payload["runtime_mode"] == "default"
    assert payload["permission_mode"] == "moderate"
    assert len(payload["pending"]) == 50
    assert len(payload["pending"][0]["reason"]) == 500
    assert payload["pending"][0]["policy"]["source"] == "TOOL_PERMISSIONS:bash_run"
    assert "private_payload" not in str(payload)
