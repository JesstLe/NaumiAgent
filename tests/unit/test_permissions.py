"""权限系统测试."""

import os
from types import SimpleNamespace

import pytest

from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from naumi_agent.tools.base import ToolMetadata
from naumi_agent.tools.builtin import YamlMicroVerifyTool, YamlValidateTool
from naumi_agent.tools.forge import ForgeTool
from naumi_agent.tools.hotreload import HotReloadTool
from naumi_agent.tools.sandbox import CodeExecuteTool
from naumi_agent.tools.self_evolve import SelfEvolveTool
from naumi_agent.tools.self_modify import SelfModifyTool
from naumi_agent.tools.subagent import DestroyAgentTool


class TestPermissionChecker:
    def test_bypass_allows_all(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        result = checker.check("bash_run", {"command": "echo hi"})
        assert result.allowed

    def test_moderate_allows_file_ops(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        assert checker.check("file_read", {"path": "/workspace/test.txt"}).allowed
        assert checker.check("file_write", {"path": "/workspace/test.txt"}).allowed

    def test_lockdown_blocks_write(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN, allowed_dirs=["/workspace"])
        result = checker.check("file_write", {"path": "/workspace/test.txt"})
        assert not result.allowed

    def test_lockdown_allows_read(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN, allowed_dirs=["/workspace"])
        result = checker.check("file_read", {"path": "/workspace/test.txt"})
        assert result.allowed

    def test_lockdown_allows_claude_style_read_only_tools_without_metadata(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN, allowed_dirs=["/workspace"])

        cases = [
            ("glob", {"pattern": "**/*.py", "directory": "/workspace"}),
            ("grep", {"pattern": "PermissionRule", "directory": "/workspace"}),
            ("read", {"path": "/workspace/test.txt"}),
        ]

        for tool_name, args in cases:
            result = checker.check(tool_name, args)

            assert result.allowed, f"{tool_name}: {result.reason}"
            assert not result.requires_confirmation

    def test_unknown_tool_blocked(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("unknown_tool", {})
        assert not result.allowed
        assert result.code == PermissionReasonCode.UNKNOWN_TOOL
        assert result.risk_level == PermissionRiskLevel.HIGH
        assert "未知工具" in result.reason

    def test_path_sandbox(self) -> None:
        checker = PermissionChecker(
            PermissionMode.MODERATE, allowed_dirs=["/workspace", "/tmp/naumi"]
        )
        assert checker.check("file_read", {"path": "/workspace/file.txt"}).allowed
        assert not checker.check("file_read", {"path": "/etc/passwd"}).allowed

    def test_path_sandbox_rejects_different_windows_drive_without_crashing(self) -> None:
        if os.name != "nt":
            pytest.skip("Windows drive semantics")
        checker = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[r"Z:\allowed"],
        )

        result = checker.check("file_read", {"path": r"C:\outside\file.txt"})

        assert not result.allowed
        assert result.code == PermissionReasonCode.PATH_OUTSIDE_SANDBOX

    def test_tool_metadata_path_args_are_sandboxed(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE, allowed_dirs=["/workspace"])
        tool = YamlValidateTool()

        allowed = checker.check(
            "yaml_validate",
            {"file_path": "/workspace/config.yaml"},
            tool=tool,
        )
        blocked = checker.check(
            "yaml_validate",
            {"file_path": "/etc/passwd"},
            tool=tool,
        )

        assert allowed.allowed
        assert not blocked.allowed
        assert blocked.code == PermissionReasonCode.PATH_OUTSIDE_SANDBOX
        assert blocked.risk_level == PermissionRiskLevel.HIGH
        assert "不在允许目录内" in blocked.reason

    def test_tool_metadata_path_args_reject_non_string_paths(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE, allowed_dirs=["/workspace"])
        tool = YamlMicroVerifyTool()

        result = checker.check(
            "yaml_micro_verify",
            {"file_path": ["not", "a", "path"]},
            tool=tool,
        )

        assert not result.allowed
        assert result.code == PermissionReasonCode.INVALID_PATH_ARGUMENT
        assert "必须是字符串" in result.reason

    def test_relative_path_uses_workspace_root(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        assert checker.check("file_write", {"path": "workspace/showcase/index.html"}).allowed

    def test_blocked_commands(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for cmd in ["rm -rf /", "sudo rm -rf /home", "mkfs.ext4 /dev/sda"]:
            result = checker.check("bash_run", {"command": cmd})
            assert not result.allowed, f"Should block: {cmd}"
            assert result.code == PermissionReasonCode.DANGEROUS_COMMAND
            assert result.risk_level == PermissionRiskLevel.HIGH
            assert "高风险模式" in result.reason

    def test_bypass_cannot_skip_dangerous_command_filter(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        result = checker.check(
            "bash_run",
            {"command": "rm -rf /Users/lv/Workspace/showcase-page && echo done"},
        )
        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rm -rf -- /", id="end-of-options-before-root"),
            pytest.param("rm -fr /", id="combined-short-options-reversed"),
            pytest.param("rm -r -f /", id="separate-short-options"),
            pytest.param("rm --recursive --force /", id="long-options"),
            pytest.param("sudo -n rm -fr /", id="sudo-wrapper"),
            pytest.param("printf safe; rm -fr /", id="second-command-after-semicolon"),
            pytest.param(
                "printf safe && rm --recursive --force /",
                id="second-command-after-and",
            ),
            pytest.param("rm -rf /.", id="root-equivalent-target"),
        ],
    )
    def test_bypass_blocks_structurally_destructive_rm_commands(self, command: str) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check("bash_run", {"command": command})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND
        assert "高风险" in result.reason

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rm -f ./file", id="force-only-local-file"),
            pytest.param("rm -r ./build", id="recursive-only-local-directory"),
            pytest.param("echo 'rm -rf /'", id="quoted-text-not-command"),
        ],
    )
    def test_bypass_allows_safe_rm_neighbors(self, command: str) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check("bash_run", {"command": command})

        assert result.allowed
        assert result.code is PermissionReasonCode.ALLOWED

    def test_malformed_shell_uses_normalized_literal_dangerous_fallback(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check("bash_run", {"command": "rm -rf / 'unterminated"})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_malformed_safe_shell_keeps_normal_confirmation_policy(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check("bash_run", {"command": "echo 'unterminated"})

        assert result.allowed
        assert result.outcome is PermissionOutcome.CONFIRM
        assert result.risk_level is PermissionRiskLevel.MEDIUM

    def test_strict_bash_dangerous_command_blocks_before_mode_rejection(self) -> None:
        checker = PermissionChecker(PermissionMode.STRICT)

        decision = checker.check("bash_run", {"command": "rm -rf /"})

        assert not decision.allowed
        assert decision.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_lockdown_background_dangerous_command_blocks_before_mode_rejection(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN)

        decision = checker.check("background_run", {"command": "rm -rf /"})

        assert not decision.allowed
        assert decision.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_non_string_command_argument_is_denied_without_raising(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        decision = checker.check("bash_run", {"command": ["echo", "safe"]})

        assert not decision.allowed
        assert decision.code.value == "invalid_command_argument"
        assert "命令参数 `command` 必须是字符串" in decision.reason

    def test_metadata_named_non_string_command_argument_is_denied_without_raising(
        self,
    ) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        tool = SimpleNamespace(
            metadata=ToolMetadata(command_argument_names=("shell_input",))
        )

        decision = checker.check(
            "bash_run",
            {"shell_input": ["echo", "safe"]},
            tool=tool,
        )

        assert not decision.allowed
        assert decision.code.value == "invalid_command_argument"
        assert "命令参数 `shell_input` 必须是字符串" in decision.reason

    @pytest.mark.parametrize("language", [" Bash ", "BASH", "bash\n"])
    def test_normalized_bash_code_execute_blocks_dangerous_commands_in_bypass(
        self, language: str
    ) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check(
            "code_execute",
            {"language": language, "code": "rm -rf / && echo done"},
            tool=CodeExecuteTool(),
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize("mode", list(PermissionMode))
    def test_bash_code_execute_blocks_dangerous_commands_in_every_mode(
        self, mode: PermissionMode
    ) -> None:
        checker = PermissionChecker(mode)

        result = checker.check(
            "code_execute",
            {"language": "bash", "code": "rm -rf / && echo done"},
            tool=CodeExecuteTool(),
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_non_shell_code_execute_does_not_scan_code_as_a_shell_command(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check(
            "code_execute",
            {"language": "python", "code": "command = 'rm -rf /'"},
            tool=CodeExecuteTool(),
        )

        assert result.allowed
        assert result.code is PermissionReasonCode.ALLOWED

    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
    )
    def test_dynamic_mcp_tool_blocks_dangerous_commands_in_every_allowed_mode(
        self, mode: PermissionMode
    ) -> None:
        checker = PermissionChecker(mode)

        result = checker.check("mcp__terminal__run", {"command": "rm -rf /"})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        "mode",
        [PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
    )
    def test_dynamic_mcp_tool_blocks_outside_sandbox_cwd_in_every_allowed_mode(
        self, mode: PermissionMode, tmp_path
    ) -> None:
        checker = PermissionChecker(
            mode,
            allowed_dirs=[str(tmp_path / "allowed")],
            workspace_root=str(tmp_path / "allowed"),
        )

        result = checker.check(
            "mcp__terminal__run",
            {"cwd": str(tmp_path / "outside")},
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.PATH_OUTSIDE_SANDBOX

    def test_dynamic_mcp_tool_uses_common_mode_count_and_session_grant_policy(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        allowed = checker.check("mcp__terminal__run", {"command": "echo hi"})
        checker.set_mode(PermissionMode.STRICT)
        blocked = checker.check("mcp__terminal__run", {"command": "echo hi"})

        assert allowed.allowed
        assert allowed.allow_session_grant is False
        assert checker.get_call_counts() == {"mcp__terminal__run": 1}
        assert not blocked.allowed
        assert blocked.code is PermissionReasonCode.MODE_BLOCKED

    def test_path_sandbox_blocks_symlink_escape(self, tmp_path) -> None:
        allowed_dir = tmp_path / "allowed"
        outside_dir = tmp_path / "outside"
        allowed_dir.mkdir()
        outside_dir.mkdir()
        (allowed_dir / "escape").symlink_to(outside_dir, target_is_directory=True)
        checker = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[str(allowed_dir)],
            workspace_root=str(allowed_dir),
        )

        result = checker.check("file_read", {"path": str(allowed_dir / "escape" / "secret.txt")})

        assert not result.allowed
        assert result.code is PermissionReasonCode.PATH_OUTSIDE_SANDBOX

    def test_safe_commands(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        assert checker.check("bash_run", {"command": "ls -la"}).allowed
        assert checker.check("bash_run", {"command": "pip install pytest"}).allowed

    def test_max_calls(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for _ in range(50):
            checker.check("bash_run", {"command": "echo test"})
        result = checker.check("bash_run", {"command": "echo one more"})
        assert not result.allowed
        assert result.code == PermissionReasonCode.MAX_CALLS_EXCEEDED
        assert "最大调用次数" in result.reason

    def test_confirmation_required(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("bash_run", {"command": "echo test"})
        assert result.allowed
        assert result.requires_confirmation

    def test_tool_metadata_can_require_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        cases = [
            (
                "self_modify",
                SelfModifyTool(),
                {
                    "target_file": "tools/example.py",
                    "new_content": "x = 1\n",
                    "description": "example",
                },
            ),
            (
                "forge_tool",
                ForgeTool(),
                {
                    "description": "count comments",
                    "tool_name": "comment_counter",
                },
            ),
            (
                "self_evolve",
                SelfEvolveTool(),
                {
                    "target_file": "tools/example.py",
                    "original_content": "x = 1\n",
                    "new_content": "x = 2\n",
                    "description": "example",
                },
            ),
            (
                "hot_reload",
                HotReloadTool(),
                {"target": "tools"},
            ),
            (
                "code_execute",
                CodeExecuteTool(),
                {"code": "print('ok')", "language": "python"},
            ),
            (
                "destroy_agent",
                DestroyAgentTool(SimpleNamespace()),
                {"name": "reviewer"},
            ),
        ]

        for tool_name, tool, args in cases:
            result = checker.check(tool_name, args, tool=tool)
            assert result.allowed, tool_name
            assert result.requires_confirmation, tool_name

    def test_bypass_no_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        result = checker.check("bash_run", {"command": "echo test"})
        assert result.allowed
        assert not result.requires_confirmation

    def test_metadata_cannot_weaken_high_rule_confirmation_in_bypass(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        tool = SimpleNamespace(metadata=ToolMetadata(requires_confirmation=False))

        decision = checker.check("session_delete", {}, tool=tool)

        assert decision.allowed
        assert decision.outcome is PermissionOutcome.CONFIRM
        assert decision.risk_level is PermissionRiskLevel.HIGH
        assert decision.requires_confirmation
        assert decision.requires_double_confirm
        assert decision.allow_session_grant is False

    def test_shell_confirmation_is_medium_and_session_grantable(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        decision = checker.check(
            "bash_run",
            {"command": "git status", "cwd": str(tmp_path)},
        )

        assert decision.outcome is PermissionOutcome.CONFIRM
        assert decision.risk_level is PermissionRiskLevel.MEDIUM
        assert decision.tool_family == "shell"
        assert decision.allow_session_grant is True
        assert decision.requires_double_confirm is False

    def test_dangerous_command_remains_blocked_in_bypass(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.BYPASS,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        decision = checker.check(
            "bash_run",
            {"command": "sudo rm -rf /", "cwd": str(tmp_path)},
        )

        assert decision.outcome is PermissionOutcome.BLOCK
        assert decision.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_path_violation_remains_blocked_in_bypass(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.BYPASS,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        decision = checker.check("file_read", {"path": str(tmp_path.parent / "outside.txt")})

        assert decision.outcome is PermissionOutcome.BLOCK
        assert decision.code is PermissionReasonCode.PATH_OUTSIDE_SANDBOX

    def test_destructive_metadata_requires_double_confirmation_in_bypass(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        decision = checker.check(
            "self_modify",
            {
                "target_file": "tools/example.py",
                "new_content": "x = 1\n",
                "description": "example",
            },
            tool=SelfModifyTool(),
        )

        assert decision.outcome is PermissionOutcome.CONFIRM
        assert decision.risk_level is PermissionRiskLevel.HIGH
        assert decision.requires_double_confirm is True
        assert decision.allow_session_grant is False

    @pytest.mark.parametrize(
        ("tool_name", "args", "requires_confirmation"),
        [
            ("background_run", {"command": "echo ok"}, True),
            ("background_status", {}, False),
            ("background_list", {}, False),
            ("background_cancel", {}, False),
            ("background_cleanup", {}, False),
            ("background_read_output", {}, False),
        ],
    )
    def test_background_tools_share_canonical_process_family(
        self,
        tool_name: str,
        args: dict[str, str],
        requires_confirmation: bool,
    ) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        decision = checker.check(tool_name, args)

        assert decision.allowed, tool_name
        assert decision.tool_family == "background_process"
        assert decision.requires_confirmation is requires_confirmation

    def test_task_tracking_tools_allowed_without_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for tool_name in [
            "todo_write",
            "task_create",
            "task_update",
            "task_list",
            "task_delete",
        ]:
            result = checker.check(tool_name, {})
            assert result.allowed, tool_name
            assert not result.requires_confirmation, tool_name

    def test_tool_families_allowed_without_unknown_failures(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for tool_name in [
            "analysis_chaos",
            "browser_scroll",
            "skill_code-review",
            "spawn_agent",
            "team_signal",
            "team_status",
            "blackboard_write",
            "runtime_status",
            "runtime_mcp_connect",
            "self_modify",
            "self_review",
            "forge_tool",
            "pursue_goal",
            "pursuit_list",
            "pursuit_status",
            "pursuit_resume",
            "yaml_validate",
        ]:
            result = checker.check(tool_name, {})
            assert result.allowed, tool_name
            assert "未知工具" not in result.reason

    def test_namespaced_tool_family_allowed(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("default__browser_scroll", {})
        assert result.allowed
        assert "未知工具" not in result.reason

    def test_reset_counts(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        checker.check("file_read", {"path": "/workspace/test.txt"})
        assert len(checker.get_call_counts()) == 1
        checker.reset_counts()
        assert len(checker.get_call_counts()) == 0
