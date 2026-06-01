"""MCP 工具权限测试."""

from __future__ import annotations

from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionReasonCode,
)


class TestMCPToolPermissions:
    def test_mcp_tool_allowed_moderate(self):
        checker = PermissionChecker(mode=PermissionMode.MODERATE)
        decision = checker.check("mcp__search", {"query": "test"})
        assert decision.allowed

    def test_mcp_tool_allowed_permissive(self):
        checker = PermissionChecker(mode=PermissionMode.PERMISSIVE)
        decision = checker.check("mcp__tool_x", {"arg": "val"})
        assert decision.allowed

    def test_mcp_tool_allowed_bypass(self):
        checker = PermissionChecker(mode=PermissionMode.BYPASS)
        decision = checker.check("mcp__anything", {})
        assert decision.allowed

    def test_mcp_tool_blocked_strict(self):
        checker = PermissionChecker(mode=PermissionMode.STRICT)
        decision = checker.check("mcp__dangerous", {"cmd": "rm"})
        assert not decision.allowed
        assert decision.code == PermissionReasonCode.MODE_BLOCKED
        assert "不允许" in decision.reason

    def test_mcp_tool_blocked_lockdown(self):
        checker = PermissionChecker(mode=PermissionMode.LOCKDOWN)
        decision = checker.check("mcp__tool", {})
        assert not decision.allowed

    def test_mcp_tool_call_counting(self):
        checker = PermissionChecker(mode=PermissionMode.MODERATE)
        checker.check("mcp__search", {"q": "1"})
        checker.check("mcp__search", {"q": "2"})
        assert checker.get_call_counts()["mcp__search"] == 2

    def test_non_mcp_unknown_blocked(self):
        """Non-MCP unknown tools should still be blocked."""
        checker = PermissionChecker(mode=PermissionMode.MODERATE)
        decision = checker.check("totally_unknown_tool", {})
        assert not decision.allowed
        assert decision.code == PermissionReasonCode.UNKNOWN_TOOL
        assert "未知工具" in decision.reason
