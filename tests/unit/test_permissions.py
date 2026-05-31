"""权限系统测试."""

from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
)


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

    def test_unknown_tool_blocked(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("unknown_tool", {})
        assert not result.allowed
        assert "Unknown tool" in result.reason

    def test_path_sandbox(self) -> None:
        checker = PermissionChecker(
            PermissionMode.MODERATE, allowed_dirs=["/workspace", "/tmp/naumi"]
        )
        assert checker.check("file_read", {"path": "/workspace/file.txt"}).allowed
        assert not checker.check("file_read", {"path": "/etc/passwd"}).allowed

    def test_blocked_commands(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for cmd in ["rm -rf /", "sudo rm -rf /home", "mkfs.ext4 /dev/sda"]:
            result = checker.check("bash_run", {"command": cmd})
            assert not result.allowed, f"Should block: {cmd}"

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
        assert "exceeded" in result.reason

    def test_confirmation_required(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("bash_run", {"command": "echo test"})
        assert result.allowed
        assert result.requires_confirmation

    def test_bypass_no_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.BYPASS)
        result = checker.check("bash_run", {"command": "echo test"})
        assert result.allowed
        assert not result.requires_confirmation

    def test_task_tracking_tools_allowed_without_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        for tool_name in ["task_create", "task_update", "task_list", "task_delete"]:
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
            "blackboard_write",
            "self_modify",
            "self_review",
            "forge_tool",
            "pursue_goal",
            "yaml_validate",
        ]:
            result = checker.check(tool_name, {})
            assert result.allowed, tool_name
            assert "Unknown tool" not in result.reason

    def test_namespaced_tool_family_allowed(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("default__browser_scroll", {})
        assert result.allowed
        assert "Unknown tool" not in result.reason

    def test_reset_counts(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        checker.check("file_read", {"path": "/workspace/test.txt"})
        assert len(checker.get_call_counts()) == 1
        checker.reset_counts()
        assert len(checker.get_call_counts()) == 0
