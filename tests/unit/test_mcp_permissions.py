"""MCP 工具权限测试."""

from __future__ import annotations

import pytest

from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
    PermissionRiskLevel,
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

    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
    )
    def test_opaque_mcp_tool_requires_high_risk_double_confirmation(self, mode):
        checker = PermissionChecker(mode=mode)

        decision = checker.check("mcp__anything", {"query": "test"})

        assert decision.allowed
        assert decision.outcome is PermissionOutcome.CONFIRM
        assert decision.risk_level is PermissionRiskLevel.HIGH
        assert decision.requires_confirmation
        assert decision.requires_double_confirm
        assert decision.allow_session_grant is False

    def test_mcp_tool_matching_a_builtin_name_remains_opaque(self):
        checker = PermissionChecker(mode=PermissionMode.MODERATE)

        decision = checker.check("mcp__terminal__bash_run", {"command": "echo safe"})

        assert decision.allowed
        assert decision.risk_level is PermissionRiskLevel.HIGH
        assert checker.get_call_counts() == {"mcp__terminal__bash_run": 1}

    @pytest.mark.parametrize(
        ("args", "expected_code"),
        [
            ({"cmd": "rm -rf /"}, PermissionReasonCode.DANGEROUS_COMMAND),
            ({"directory": "/etc"}, PermissionReasonCode.PATH_OUTSIDE_SANDBOX),
        ],
    )
    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
    )
    def test_mcp_tool_blocks_dangerous_common_argument_aliases(
        self, mode, args, expected_code
    ):
        checker = PermissionChecker(mode=mode)

        decision = checker.check("mcp__terminal__run", args)

        assert not decision.allowed
        assert decision.code is expected_code

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("printf safe\nrm -fr /absolute", id="newline-command-boundary"),
            pytest.param("printf safe\n\nrm -fr /absolute", id="double-newline-boundary"),
            pytest.param("printf safe;\nrm -fr /absolute", id="semicolon-newline-boundary"),
            pytest.param("printf safe &&\nrm -fr /absolute", id="and-newline-boundary"),
            pytest.param("bash -lc 'rm -fr /absolute'", id="shell-option-bundle"),
            pytest.param("exec rm -fr /absolute", id="exec-wrapper"),
        ],
    )
    @pytest.mark.parametrize("argument_name", ["command", "cmd"])
    def test_mcp_dynamic_command_aliases_block_mainstream_shell_syntax(
        self, command, argument_name
    ):
        checker = PermissionChecker(mode=PermissionMode.BYPASS)

        decision = checker.check("mcp__terminal__run", {argument_name: command})

        assert not decision.allowed
        assert decision.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_mcp_tool_blocked_strict(self):
        checker = PermissionChecker(mode=PermissionMode.STRICT)
        decision = checker.check("mcp__dangerous", {"cmd": "echo safe"})
        assert not decision.allowed
        assert decision.code == PermissionReasonCode.MODE_BLOCKED
        assert "不允许" in decision.reason

    def test_mcp_tool_outside_path_blocks_before_strict_mode_rejection(self):
        checker = PermissionChecker(mode=PermissionMode.STRICT)

        decision = checker.check("mcp__dangerous", {"directory": "/etc"})

        assert not decision.allowed
        assert decision.code is PermissionReasonCode.PATH_OUTSIDE_SANDBOX

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
