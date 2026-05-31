"""权限系统 — 工具调用检查与沙箱."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    PERMISSIVE = "permissive"
    MODERATE = "moderate"
    STRICT = "strict"
    LOCKDOWN = "lockdown"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False


@dataclass(frozen=True)
class PermissionRule:
    tool_name: str
    allowed_modes: list[PermissionMode]
    requires_confirmation: bool
    max_calls_per_session: int | None = None
    blocked_commands: list[str] | None = None


# 工具权限表
TOOL_PERMISSIONS: dict[str, PermissionRule] = {
    "file_read": PermissionRule(
        tool_name="file_read",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "file_write": PermissionRule(
        tool_name="file_write",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "file_edit": PermissionRule(
        tool_name="file_edit",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "bash_run": PermissionRule(
        tool_name="bash_run",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=True,
        max_calls_per_session=50,
    ),
    "code_execute": PermissionRule(
        tool_name="code_execute",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=True,
        max_calls_per_session=20,
    ),
    "browser_goto": PermissionRule(
        tool_name="browser_goto",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "browser_click": PermissionRule(
        tool_name="browser_click",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=False,
    ),
    "browser_type": PermissionRule(
        tool_name="browser_type",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=False,
    ),
    "browser_observe": PermissionRule(
        tool_name="browser_observe",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "browser_screenshot": PermissionRule(
        tool_name="browser_screenshot",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "browser_evaluate": PermissionRule(
        tool_name="browser_evaluate",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "web_search": PermissionRule(
        tool_name="web_search",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "web_fetch": PermissionRule(
        tool_name="web_fetch",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "memory_store": PermissionRule(
        tool_name="memory_store",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "memory_recall": PermissionRule(
        tool_name="memory_recall",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "delegate_task": PermissionRule(
        tool_name="delegate_task",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "list_agents": PermissionRule(
        tool_name="list_agents",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "spawn_agent": PermissionRule(
        tool_name="spawn_agent",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "destroy_agent": PermissionRule(
        tool_name="destroy_agent",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "blackboard_read": PermissionRule(
        tool_name="blackboard_read",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "blackboard_write": PermissionRule(
        tool_name="blackboard_write",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "task_create": PermissionRule(
        tool_name="task_create",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "task_update": PermissionRule(
        tool_name="task_update",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "task_list": PermissionRule(
        tool_name="task_list",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "task_delete": PermissionRule(
        tool_name="task_delete",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "yaml_micro_verify": PermissionRule(
        tool_name="yaml_micro_verify",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "yaml_validate": PermissionRule(
        tool_name="yaml_validate",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "pursue_goal": PermissionRule(
        tool_name="pursue_goal",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "pursuit_list": PermissionRule(
        tool_name="pursuit_list",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "pursuit_status": PermissionRule(
        tool_name="pursuit_status",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "pursuit_resume": PermissionRule(
        tool_name="pursuit_resume",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "hot_reload": PermissionRule(
        tool_name="hot_reload",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
        ],
        requires_confirmation=False,
    ),
    "self_modify": PermissionRule(
        tool_name="self_modify",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
        ],
        requires_confirmation=False,
    ),
    "self_evolve": PermissionRule(
        tool_name="self_evolve",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
        ],
        requires_confirmation=False,
    ),
    "self_review": PermissionRule(
        tool_name="self_review",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "forge_tool": PermissionRule(
        tool_name="forge_tool",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
        ],
        requires_confirmation=False,
    ),
    "background_run": PermissionRule(
        tool_name="background_run",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=True,
        max_calls_per_session=50,
    ),
    "background_status": PermissionRule(
        tool_name="background_status",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "background_list": PermissionRule(
        tool_name="background_list",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "background_cancel": PermissionRule(
        tool_name="background_cancel",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "background_read_output": PermissionRule(
        tool_name="background_read_output",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "schedule_create": PermissionRule(
        tool_name="schedule_create",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "schedule_list": PermissionRule(
        tool_name="schedule_list",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "schedule_cancel": PermissionRule(
        tool_name="schedule_cancel",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "schedule_pause": PermissionRule(
        tool_name="schedule_pause",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "schedule_resume": PermissionRule(
        tool_name="schedule_resume",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "worktree_create": PermissionRule(
        tool_name="worktree_create",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "worktree_status": PermissionRule(
        tool_name="worktree_status",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "worktree_bind_task": PermissionRule(
        tool_name="worktree_bind_task",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "worktree_keep": PermissionRule(
        tool_name="worktree_keep",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "worktree_remove": PermissionRule(
        tool_name="worktree_remove",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
}

PREFIX_PERMISSIONS: dict[str, PermissionRule] = {
    "analysis_": PermissionRule(
        tool_name="analysis_*",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "browser_": PermissionRule(
        tool_name="browser_*",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "skill_": PermissionRule(
        tool_name="skill_*",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
}

# 阻止的危险 shell 命令模式
BLOCKED_COMMANDS = [
    "rm -rf /",
    "sudo rm",
    "mkfs",
    "dd if=",
    "> /dev/sd",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
]


class PermissionChecker:
    """工具调用权限检查器."""

    def __init__(
        self,
        mode: PermissionMode,
        allowed_dirs: list[str] | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self._mode = mode
        self._workspace_root = os.path.abspath(
            os.path.expanduser(workspace_root or os.getcwd())
        )
        self._allowed_dirs = [
            self._resolve_path_for_sandbox(path) for path in (allowed_dirs or ["/workspace"])
        ]
        self._call_counts: dict[str, int] = {}

    def _resolve_path_for_sandbox(self, path: str) -> str:
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        return os.path.abspath(os.path.join(self._workspace_root, expanded))

    def _resolve_rule(self, tool_name: str) -> tuple[str, PermissionRule | None]:
        """Resolve exact, namespaced, or prefix-based permission rules."""
        candidates = [tool_name]
        if "." in tool_name:
            candidates.append(tool_name.split(".")[-1])
        if "__" in tool_name:
            candidates.append(tool_name.split("__")[-1])

        for candidate in candidates:
            rule = TOOL_PERMISSIONS.get(candidate)
            if rule:
                return candidate, rule

            for prefix, prefix_rule in PREFIX_PERMISSIONS.items():
                if candidate.startswith(prefix):
                    return candidate, prefix_rule

        return tool_name, None

    def check(self, tool_name: str, args: dict[str, Any]) -> PermissionDecision:
        """检查工具调用是否被允许."""
        tool_name, rule = self._resolve_rule(tool_name)

        if not rule:
            # MCP tools are dynamic — allow based on mode
            if tool_name.startswith("mcp__"):
                mcp_allowed = [
                    PermissionMode.BYPASS,
                    PermissionMode.PERMISSIVE,
                    PermissionMode.MODERATE,
                ]
                if self._mode in mcp_allowed:
                    self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
                    return PermissionDecision(allowed=True)
                return PermissionDecision(
                    allowed=False,
                    reason=f"MCP tool '{tool_name}' not allowed in {self._mode.value} mode",
                )
            return PermissionDecision(allowed=False, reason=f"Unknown tool: {tool_name}")

        if self._mode not in rule.allowed_modes:
            return PermissionDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' not allowed in {self._mode.value} mode",
            )

        # 调用次数检查
        count = self._call_counts.get(tool_name, 0)
        if rule.max_calls_per_session and count >= rule.max_calls_per_session:
            return PermissionDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' exceeded max calls ({rule.max_calls_per_session})",
            )

        # 文件路径沙箱检查
        if "path" in args:
            path_check = self._check_path_sandbox(args["path"])
            if not path_check.allowed:
                return path_check
        if "cwd" in args and args["cwd"]:
            cwd_check = self._check_path_sandbox(args["cwd"])
            if not cwd_check.allowed:
                return cwd_check

        # 命令检查
        if tool_name in {"bash_run", "background_run"} and "command" in args:
            cmd_check = self._check_command(args["command"])
            if not cmd_check.allowed:
                return cmd_check

        # 记录调用
        self._call_counts[tool_name] = count + 1

        return PermissionDecision(
            allowed=True,
            requires_confirmation=(
                rule.requires_confirmation and self._mode != PermissionMode.BYPASS
            ),
        )

    def _check_path_sandbox(self, path: str) -> PermissionDecision:
        """检查文件路径是否在允许的目录内."""
        if self._mode == PermissionMode.BYPASS:
            return PermissionDecision(allowed=True)

        abs_path = self._resolve_path_for_sandbox(path)
        if any(
            os.path.commonpath([abs_path, allowed]) == allowed
            for allowed in self._allowed_dirs
        ):
            return PermissionDecision(allowed=True)

        return PermissionDecision(
            allowed=False,
            reason=f"Path '{path}' is outside allowed directories: {self._allowed_dirs}",
        )

    def _check_command(self, command: str) -> PermissionDecision:
        """检查 shell 命令是否安全."""
        cmd_lower = command.lower().strip()
        for blocked in BLOCKED_COMMANDS:
            if blocked.lower() in cmd_lower:
                return PermissionDecision(
                    allowed=False,
                    reason=f"Blocked dangerous command pattern: {blocked}",
                )
        return PermissionDecision(allowed=True)

    def get_call_counts(self) -> dict[str, int]:
        return dict(self._call_counts)

    def reset_counts(self) -> None:
        self._call_counts.clear()
