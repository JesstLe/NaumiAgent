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

    def test_harness_check_has_explicit_permission_rule(self) -> None:
        moderate = PermissionChecker(PermissionMode.MODERATE)
        lockdown = PermissionChecker(PermissionMode.LOCKDOWN)

        allowed = moderate.check(
            "harness_run_check",
            {"check_id": "unit", "run_id": "run-1"},
        )
        blocked = lockdown.check(
            "harness_run_check",
            {"check_id": "unit", "run_id": "run-1"},
        )

        assert allowed.allowed
        assert not allowed.requires_confirmation
        assert not blocked.allowed

    def test_workbench_proposal_governance_confirms_except_in_bypass(self) -> None:
        moderate = PermissionChecker(PermissionMode.MODERATE)
        bypass = PermissionChecker(PermissionMode.BYPASS)
        arguments = {"proposal_id": "proposal-1", "action": "approve"}

        guarded = moderate.check("workbench_govern_proposal", arguments)
        unrestricted = bypass.check("workbench_govern_proposal", arguments)

        assert guarded.allowed
        assert guarded.requires_confirmation
        assert guarded.risk_level == PermissionRiskLevel.HIGH
        assert unrestricted.allowed
        assert not unrestricted.requires_confirmation

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

    def test_moderate_cannot_skip_dangerous_command_filter(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check(
            "bash_run",
            {"command": "rm -rf /Users/lv/Workspace/showcase-page && echo done"},
        )
        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        ("mode", "command"),
        [
            pytest.param(
                PermissionMode.MODERATE,
                "sudo rm /tmp/direct-file",
                id="moderate-direct-sudo-rm",
            ),
            pytest.param(
                PermissionMode.MODERATE,
                "sudo rm -f /tmp/direct-file",
                id="moderate-direct-sudo-rm-force",
            ),
            pytest.param(
                PermissionMode.MODERATE,
                "sudo -n rm /tmp/direct-file",
                id="moderate-sudo-non-interactive",
            ),
            pytest.param(
                PermissionMode.MODERATE,
                "sudo -u root rm /tmp/direct-file",
                id="moderate-sudo-user",
            ),
        ],
    )
    def test_direct_sudo_rm_remains_a_hard_block(
        self,
        mode: PermissionMode,
        command: str,
    ) -> None:
        checker = PermissionChecker(mode)

        result = checker.check("bash_run", {"command": command})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    def test_quoted_direct_sudo_rm_text_remains_allowed(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        result = checker.check(
            "bash_run",
            {"command": "echo 'sudo rm /tmp/direct-file'"},
        )

        assert result.allowed
        assert result.code is PermissionReasonCode.ALLOWED

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rm -rf -- /", id="end-of-options-before-root"),
            pytest.param("rm -fr /", id="combined-short-options-reversed"),
            pytest.param("rm -r -f /", id="separate-short-options"),
            pytest.param("rm --recursive --force /", id="long-options"),
            pytest.param("sudo -n rm -fr /", id="sudo-wrapper"),
            pytest.param("bash -c 'rm -fr /'", id="bash-c-wrapper"),
            pytest.param("sh -c 'rm -r -f /'", id="sh-c-wrapper"),
            pytest.param(
                "sudo bash -c 'rm --recursive --force /'",
                id="sudo-bash-c-wrapper",
            ),
            pytest.param("env SAFE=1 rm -fr /", id="env-assignment-wrapper"),
            pytest.param("env -u SAFE rm -rf /absolute", id="env-unset-wrapper"),
            pytest.param("env --chdir /tmp rm -rf /absolute", id="env-chdir-wrapper"),
            pytest.param("exec -a cleanup rm -rf /absolute", id="exec-argv0-wrapper"),
            pytest.param("command rm -fr /", id="command-wrapper"),
            pytest.param("/bin/rm -fr /", id="absolute-rm-wrapper"),
            pytest.param("printf safe; rm -fr /", id="second-command-after-semicolon"),
            pytest.param(
                "printf safe && rm --recursive --force /",
                id="second-command-after-and",
            ),
            pytest.param("rm -rf /.", id="root-equivalent-target"),
        ],
    )
    def test_moderate_blocks_structurally_destructive_rm_commands(self, command: str) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check("bash_run", {"command": command})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND
        assert "高风险" in result.reason

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("env -u", id="env-short-unset"),
            pytest.param("env --unset", id="env-long-unset"),
            pytest.param("env -C", id="env-short-chdir"),
            pytest.param("env --chdir", id="env-long-chdir"),
            pytest.param("env -S", id="env-short-split-string"),
            pytest.param("env --split-string", id="env-long-split-string"),
            pytest.param("env --unset=", id="env-long-unset-empty-value"),
            pytest.param("env --chdir=", id="env-long-chdir-empty-value"),
            pytest.param("env --split-string=", id="env-long-split-string-empty-value"),
            pytest.param("exec -a", id="exec-argv0"),
            pytest.param("command --", id="command-end-of-options"),
        ],
    )
    def test_incomplete_shell_wrapper_options_fail_closed(self, command: str) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check("bash_run", {"command": command})

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

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
    @pytest.mark.parametrize(
        ("tool_name", "argument_name", "tool"),
        [
            pytest.param("bash_run", "command", None, id="bash-run"),
            pytest.param("background_run", "command", None, id="background-run"),
            pytest.param("code_execute", "code", CodeExecuteTool(), id="bash-code-execute"),
        ],
    )
    def test_mainstream_shell_syntax_blocks_across_builtin_command_surfaces(
        self,
        command: str,
        tool_name: str,
        argument_name: str,
        tool: CodeExecuteTool | None,
    ) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        args = {argument_name: command}
        if tool_name == "code_execute":
            args["language"] = "Bash"

        result = checker.check(tool_name, args, tool=tool)

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rm -f ./file", id="force-only-local-file"),
            pytest.param("rm -r ./build", id="recursive-only-local-directory"),
            pytest.param(
                "rm -f --preserve-root /absolute/file",
                id="force-with-non-recursive-long-option",
            ),
            pytest.param(
                "rm -r --one-file-system /absolute",
                id="recursive-with-non-force-long-option",
            ),
            pytest.param("echo 'rm -rf /'", id="quoted-text-not-command"),
            pytest.param(
                "echo 'sudo rm -rf /tmp/example'",
                id="quoted-sudo-rm-text-not-command",
            ),
            pytest.param(
                "echo 'safe\n\nrm -rf /absolute'",
                id="quoted-double-newline-not-command",
            ),
            pytest.param(
                "echo '&&' rm -rf /absolute",
                id="quoted-and-not-command-boundary",
            ),
            pytest.param(
                "echo ';' rm -rf /absolute",
                id="quoted-semicolon-not-command-boundary",
            ),
            pytest.param(
                r"echo $'\n' rm -rf /absolute",
                id="ansi-c-quoted-newline-not-command-boundary",
            ),
            pytest.param("bash -c \"echo 'rm -rf /'\"", id="quoted-rm-in-shell-payload"),
        ],
    )
    def test_moderate_allows_safe_rm_neighbors(self, command: str) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check("bash_run", {"command": command})

        assert result.allowed
        assert result.code is PermissionReasonCode.ALLOWED

    def test_malformed_shell_uses_normalized_literal_dangerous_fallback(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

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
    def test_normalized_bash_code_execute_blocks_dangerous_commands_in_moderate(
        self, language: str
    ) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check(
            "code_execute",
            {"language": language, "code": "rm -rf / && echo done"},
            tool=CodeExecuteTool(),
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        "mode",
        [mode for mode in PermissionMode if mode is not PermissionMode.BYPASS],
    )
    def test_bash_code_execute_blocks_dangerous_commands_outside_bypass(
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

    def test_bash_code_execute_rejects_list_code_before_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check(
            "code_execute",
            {"language": "Bash", "code": ["echo", "safe"]},
            tool=CodeExecuteTool(),
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.INVALID_COMMAND_ARGUMENT
        assert not result.requires_confirmation

    @pytest.mark.parametrize("mode", [PermissionMode.MODERATE, PermissionMode.STRICT])
    def test_runtime_mcp_connect_blocks_destructive_executable_and_args_before_mode(
        self, mode: PermissionMode
    ) -> None:
        checker = PermissionChecker(mode)

        result = checker.check(
            "runtime_mcp_connect",
            {"command": "/bin/rm", "args": ["-rf", "/absolute"]},
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.DANGEROUS_COMMAND

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param("-rf /absolute", id="argv-not-list"),
            pytest.param(["-rf", 1], id="argv-item-not-string"),
        ],
    )
    def test_runtime_mcp_connect_rejects_invalid_argv_before_confirmation(self, argv) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        result = checker.check(
            "runtime_mcp_connect",
            {"command": "echo", "args": argv},
        )

        assert not result.allowed
        assert result.code is PermissionReasonCode.INVALID_COMMAND_ARGUMENT
        assert result.reason == "命令参数 `args` 必须是字符串数组。"
        assert not result.requires_confirmation

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
        [PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
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
        [PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
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

    def test_bypass_skips_high_risk_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        tool = SimpleNamespace(metadata=ToolMetadata(requires_confirmation=False))

        decision = checker.check("session_delete", {}, tool=tool)

        assert decision.allowed
        assert decision.outcome is PermissionOutcome.ALLOW
        assert decision.risk_level is PermissionRiskLevel.LOW
        assert not decision.requires_confirmation
        assert not decision.requires_double_confirm
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

    def test_bypass_allows_dangerous_commands(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.BYPASS,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        decision = checker.check(
            "bash_run",
            {"command": "sudo rm -rf /", "cwd": str(tmp_path)},
        )

        assert decision.outcome is PermissionOutcome.ALLOW
        assert decision.code is PermissionReasonCode.ALLOWED

    def test_bypass_allows_paths_outside_sandbox(self, tmp_path) -> None:
        checker = PermissionChecker(
            PermissionMode.BYPASS,
            allowed_dirs=[str(tmp_path)],
            workspace_root=str(tmp_path),
        )

        for tool_name in ("file_read", "file_write", "bash_run"):
            args = (
                {"command": "pwd", "cwd": str(tmp_path.parent)}
                if tool_name == "bash_run"
                else {"path": str(tmp_path.parent / "outside.txt")}
            )
            decision = checker.check(tool_name, args)

            assert decision.outcome is PermissionOutcome.ALLOW
            assert decision.code is PermissionReasonCode.ALLOWED

    def test_bypass_allows_destructive_tool_metadata(self) -> None:
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

        assert decision.outcome is PermissionOutcome.ALLOW
        assert decision.risk_level is PermissionRiskLevel.LOW
        assert decision.requires_double_confirm is False
        assert decision.allow_session_grant is False

    def test_bypass_allows_unknown_tools(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)

        decision = checker.check("custom_dynamic_tool", {"path": "/outside"})

        assert decision.outcome is PermissionOutcome.ALLOW
        assert decision.code is PermissionReasonCode.ALLOWED

    def test_high_risk_in_moderate_mode_requires_only_one_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)

        decision = checker.check("session_delete", {})

        assert decision.outcome is PermissionOutcome.CONFIRM
        assert decision.risk_level is PermissionRiskLevel.HIGH
        assert decision.requires_confirmation is True
        assert decision.requires_double_confirm is False

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
