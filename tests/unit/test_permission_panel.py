from __future__ import annotations

from types import SimpleNamespace

from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.ui.permission_panel import (
    build_permission_panel_snapshot,
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
    assert "风险:high" in rendered
    assert "来源:TOOL_PERMISSIONS:bash_run" in rendered
    assert "确认:需要确认" in rendered
    assert "bypass 允许；跳过逐次确认和路径沙箱，危险命令仍拦截" in rendered


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
    assert "未知工具会被拒绝" in rendered


def test_permission_panel_includes_active_session_grants() -> None:
    snapshot = build_permission_panel_snapshot(_Engine())

    rendered = render_permission_panel_snapshot(snapshot)

    assert snapshot.grants[0]["grant_id"] == "grant-shell"
    assert "有效授权" in rendered
    assert "grant-shell shell [session]" in rendered
