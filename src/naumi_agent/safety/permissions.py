"""权限系统 — 工具调用检查与沙箱."""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from naumi_agent.tools.base import ToolMetadata

logger = logging.getLogger(__name__)


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    PERMISSIVE = "permissive"
    MODERATE = "moderate"
    STRICT = "strict"
    LOCKDOWN = "lockdown"


class PermissionReasonCode(StrEnum):
    ALLOWED = "allowed"
    UNKNOWN_TOOL = "unknown_tool"
    MODE_BLOCKED = "mode_blocked"
    MAX_CALLS_EXCEEDED = "max_calls_exceeded"
    INVALID_PATH_ARGUMENT = "invalid_path_argument"
    INVALID_COMMAND_ARGUMENT = "invalid_command_argument"
    PATH_OUTSIDE_SANDBOX = "path_outside_sandbox"
    DANGEROUS_COMMAND = "dangerous_command"


class PermissionRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False
    code: PermissionReasonCode = PermissionReasonCode.ALLOWED
    risk_level: PermissionRiskLevel = PermissionRiskLevel.LOW
    outcome: PermissionOutcome = PermissionOutcome.ALLOW
    tool_family: str = ""
    allow_session_grant: bool = False
    requires_double_confirm: bool = False


@dataclass(frozen=True)
class PermissionRule:
    tool_name: str
    allowed_modes: list[PermissionMode]
    requires_confirmation: bool
    max_calls_per_session: int | None = None
    blocked_commands: list[str] | None = None
    risk_level: PermissionRiskLevel = PermissionRiskLevel.LOW
    tool_family: str = ""
    allow_session_grant: bool = False


class PermissionAwareTool(Protocol):
    @property
    def metadata(self) -> ToolMetadata: ...


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
    "glob": PermissionRule(
        tool_name="glob",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "grep": PermissionRule(
        tool_name="grep",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "read": PermissionRule(
        tool_name="read",
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
        risk_level=PermissionRiskLevel.MEDIUM,
        tool_family="shell",
        allow_session_grant=True,
    ),
    "code_execute": PermissionRule(
        tool_name="code_execute",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=True,
        max_calls_per_session=20,
        risk_level=PermissionRiskLevel.MEDIUM,
        tool_family="code_execution",
        allow_session_grant=True,
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
    "session_history": PermissionRule(
        tool_name="session_history",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "workbench_snapshot": PermissionRule(
        tool_name="workbench_snapshot",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "workbench_propose_issue": PermissionRule(
        tool_name="workbench_propose_issue",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "session_load": PermissionRule(
        tool_name="session_load",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "session_delete": PermissionRule(
        tool_name="session_delete",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
        ],
        requires_confirmation=True,
        risk_level=PermissionRiskLevel.HIGH,
        tool_family="session_delete",
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
    "team_signal": PermissionRule(
        tool_name="team_signal",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
    ),
    "team_status": PermissionRule(
        tool_name="team_status",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
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
    "runtime_status": PermissionRule(
        tool_name="runtime_status",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
        ],
        requires_confirmation=False,
    ),
    "runtime_mcp_connect": PermissionRule(
        tool_name="runtime_mcp_connect",
        allowed_modes=[PermissionMode.BYPASS, PermissionMode.PERMISSIVE, PermissionMode.MODERATE],
        requires_confirmation=True,
        max_calls_per_session=10,
        risk_level=PermissionRiskLevel.MEDIUM,
        tool_family="external_runtime",
        allow_session_grant=True,
    ),
    "tool_search": PermissionRule(
        tool_name="tool_search",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
            PermissionMode.LOCKDOWN,
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
    "todo_write": PermissionRule(
        tool_name="todo_write",
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
    "doctor_diagnostics": PermissionRule(
        tool_name="doctor_diagnostics",
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
        risk_level=PermissionRiskLevel.MEDIUM,
        tool_family="background_process",
        allow_session_grant=True,
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
        tool_family="background_process",
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
        tool_family="background_process",
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
        tool_family="background_process",
    ),
    "background_cleanup": PermissionRule(
        tool_name="background_cleanup",
        allowed_modes=[
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ],
        requires_confirmation=False,
        tool_family="background_process",
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
        tool_family="background_process",
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

COMMON_PATH_ARGUMENT_NAMES = (
    "path",
    "cwd",
    "directory",
    "dir",
    "file_path",
    "working_directory",
)

COMMON_COMMAND_ARGUMENT_NAMES = ("command", "cmd", "shell", "script")
SHELL_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
SUDO_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-D",
        "-g",
        "-h",
        "-p",
        "-r",
        "-t",
        "-u",
        "-U",
        "--chroot",
        "--close-from",
        "--command-timeout",
        "--group",
        "--host",
        "--login-class",
        "--other-user",
        "--role",
        "--type",
        "--user",
    }
)


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

    @property
    def mode(self) -> PermissionMode:
        """Return the active permission mode."""
        return self._mode

    def set_mode(self, mode: PermissionMode) -> None:
        """Switch permission mode for the current session."""
        self._mode = mode

    @staticmethod
    def _deny(
        reason: str,
        *,
        code: PermissionReasonCode,
        risk_level: PermissionRiskLevel = PermissionRiskLevel.MEDIUM,
        tool_family: str = "",
        requires_confirmation: bool = False,
    ) -> PermissionDecision:
        return PermissionDecision(
            allowed=False,
            reason=reason,
            requires_confirmation=requires_confirmation,
            code=code,
            risk_level=risk_level,
            outcome=PermissionOutcome.BLOCK,
            tool_family=tool_family,
        )

    def _resolve_path_for_sandbox(self, path: str) -> str:
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded):
            resolved = os.path.abspath(expanded)
        else:
            resolved = os.path.abspath(os.path.join(self._workspace_root, expanded))
        return os.path.realpath(resolved)

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

    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        tool: PermissionAwareTool | None = None,
    ) -> PermissionDecision:
        """检查工具调用是否被允许."""
        requested_tool_name = tool_name
        is_dynamic_mcp = requested_tool_name.startswith("mcp__")
        tool_name, rule = self._resolve_rule(tool_name)
        metadata = tool.metadata if tool is not None else None

        if is_dynamic_mcp:
            tool_name = requested_tool_name
            rule = PermissionRule(
                tool_name=tool_name,
                allowed_modes=[
                    PermissionMode.BYPASS,
                    PermissionMode.PERMISSIVE,
                    PermissionMode.MODERATE,
                ],
                requires_confirmation=True,
                risk_level=PermissionRiskLevel.HIGH,
                tool_family="mcp",
            )
        elif not rule:
            if metadata and metadata.read_only:
                rule = PermissionRule(
                    tool_name=tool_name,
                    allowed_modes=[
                        PermissionMode.BYPASS,
                        PermissionMode.PERMISSIVE,
                        PermissionMode.MODERATE,
                        PermissionMode.STRICT,
                        PermissionMode.LOCKDOWN,
                    ],
                    requires_confirmation=False,
                )
            else:
                return self._deny(
                    f"未知工具：{tool_name}",
                    code=PermissionReasonCode.UNKNOWN_TOOL,
                    risk_level=PermissionRiskLevel.HIGH,
                )

        tool_family = rule.tool_family or tool_name
        language = args.get("language")
        if (
            tool_name == "code_execute"
            and isinstance(language, str)
            and language.strip().lower() == "bash"
        ):
            code = args.get("code")
            if isinstance(code, str) and code:
                cmd_check = self._check_command(
                    code,
                    arg_name="code",
                    tool_family=tool_family,
                )
                if not cmd_check.allowed:
                    return cmd_check

        path_argument_names = tuple(
            dict.fromkeys(
                (
                    *COMMON_PATH_ARGUMENT_NAMES,
                    *(metadata.path_argument_names if metadata else ()),
                )
            )
        )
        for arg_name in path_argument_names:
            if arg_name not in args:
                continue
            argument = args[arg_name]
            if isinstance(argument, str) and not argument:
                continue
            path_check = self._check_path_sandbox(
                argument,
                arg_name=arg_name,
                tool_family=tool_family,
            )
            if not path_check.allowed:
                return path_check

        command_argument_names = tuple(
            dict.fromkeys(
                (
                    *COMMON_COMMAND_ARGUMENT_NAMES,
                    *(metadata.command_argument_names if metadata else ()),
                )
            )
        )
        if is_dynamic_mcp or tool_name in {
            "bash_run",
            "background_run",
            "runtime_mcp_connect",
        }:
            for arg_name in command_argument_names:
                if arg_name not in args:
                    continue
                argument = args[arg_name]
                if isinstance(argument, str) and not argument:
                    continue
                cmd_check = self._check_command(
                    argument,
                    arg_name=arg_name,
                    tool_family=tool_family,
                )
                if not cmd_check.allowed:
                    return cmd_check

        if self._mode not in rule.allowed_modes:
            return self._deny(
                f"工具 `{tool_name}` 不允许在 {self._mode.value} 模式下执行。",
                code=PermissionReasonCode.MODE_BLOCKED,
                risk_level=PermissionRiskLevel.HIGH,
                tool_family=tool_family,
            )

        # 调用次数检查
        count = self._call_counts.get(tool_name, 0)
        if rule.max_calls_per_session and count >= rule.max_calls_per_session:
            return self._deny(
                f"工具 `{tool_name}` 已达到本会话最大调用次数（{rule.max_calls_per_session}）。",
                code=PermissionReasonCode.MAX_CALLS_EXCEEDED,
                risk_level=PermissionRiskLevel.MEDIUM,
                tool_family=tool_family,
            )

        # 记录调用
        self._call_counts[tool_name] = count + 1
        risk_level = rule.risk_level
        if metadata and metadata.destructive:
            risk_level = PermissionRiskLevel.HIGH
        metadata_requires_confirmation = bool(
            metadata and metadata.requires_confirmation is True
        )
        requires_confirmation = (
            rule.requires_confirmation
            or metadata_requires_confirmation
            or risk_level == PermissionRiskLevel.HIGH
        )
        if requires_confirmation and risk_level == PermissionRiskLevel.LOW:
            risk_level = PermissionRiskLevel.MEDIUM

        requires_double_confirm = risk_level == PermissionRiskLevel.HIGH and requires_confirmation
        allow_session_grant = (
            risk_level == PermissionRiskLevel.MEDIUM and rule.allow_session_grant
        )
        confirmation_required = requires_confirmation and (
            self._mode != PermissionMode.BYPASS or risk_level == PermissionRiskLevel.HIGH
        )

        return PermissionDecision(
            allowed=True,
            requires_confirmation=confirmation_required,
            risk_level=risk_level,
            outcome=(
                PermissionOutcome.CONFIRM if confirmation_required else PermissionOutcome.ALLOW
            ),
            tool_family=tool_family,
            allow_session_grant=allow_session_grant,
            requires_double_confirm=requires_double_confirm,
        )

    def _check_path_sandbox(
        self,
        path: Any,
        *,
        arg_name: str = "path",
        tool_family: str = "",
    ) -> PermissionDecision:
        """检查文件路径是否在允许的目录内."""
        if not isinstance(path, str):
            return self._deny(
                f"路径参数 `{arg_name}` 必须是字符串。",
                code=PermissionReasonCode.INVALID_PATH_ARGUMENT,
                risk_level=PermissionRiskLevel.MEDIUM,
                tool_family=tool_family,
            )

        abs_path = self._resolve_path_for_sandbox(path)
        for allowed in self._allowed_dirs:
            try:
                if os.path.commonpath([abs_path, allowed]) == allowed:
                    return PermissionDecision(allowed=True)
            except ValueError:
                # Windows paths on different drives cannot share a common path.
                continue

        return self._deny(
            f"路径 `{path}` 不在允许目录内。允许目录：{self._allowed_dirs}",
            code=PermissionReasonCode.PATH_OUTSIDE_SANDBOX,
            risk_level=PermissionRiskLevel.HIGH,
            tool_family=tool_family,
        )

    def _check_command(
        self,
        command: Any,
        *,
        arg_name: str = "command",
        tool_family: str = "",
    ) -> PermissionDecision:
        """检查 shell 命令是否安全."""
        if not isinstance(command, str):
            return self._deny(
                f"命令参数 `{arg_name}` 必须是字符串。",
                code=PermissionReasonCode.INVALID_COMMAND_ARGUMENT,
                risk_level=PermissionRiskLevel.MEDIUM,
                tool_family=tool_family,
            )

        destructive_rm = self._contains_destructive_rm_command(command)
        if destructive_rm:
            return self._deny(
                "命令包含高风险模式：递归强制删除绝对路径，已阻止执行。",
                code=PermissionReasonCode.DANGEROUS_COMMAND,
                risk_level=PermissionRiskLevel.HIGH,
                tool_family=tool_family,
            )

        cmd_lower = command.lower().strip()
        for blocked in BLOCKED_COMMANDS:
            if blocked == "rm -rf /" and destructive_rm is not None:
                continue
            if blocked.lower() in cmd_lower:
                return self._deny(
                    f"命令包含高风险模式 `{blocked}`，已阻止执行。",
                    code=PermissionReasonCode.DANGEROUS_COMMAND,
                    risk_level=PermissionRiskLevel.HIGH,
                    tool_family=tool_family,
                )
        return PermissionDecision(allowed=True)

    @staticmethod
    def _contains_destructive_rm_command(command: str) -> bool | None:
        """Return None when shell tokenization fails so callers can use literal fallback."""
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.commenters = ""
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None

        simple_command: list[str] = []
        for token in [*tokens, ";"]:
            if token in SHELL_COMMAND_SEPARATORS:
                if PermissionChecker._is_destructive_rm_simple_command(simple_command):
                    return True
                simple_command = []
            else:
                simple_command.append(token)
        return False

    @staticmethod
    def _is_destructive_rm_simple_command(tokens: list[str]) -> bool:
        """Detect a recursive, forced rm targeting an absolute path."""
        rm_arguments = PermissionChecker._rm_arguments_at_command_position(tokens)
        if rm_arguments is None:
            return False

        recursive = False
        force = False
        option_parsing = True
        targets: list[str] = []
        for argument in rm_arguments:
            if option_parsing and argument == "--":
                option_parsing = False
            elif option_parsing and argument == "--recursive":
                recursive = True
            elif option_parsing and argument == "--force":
                force = True
            elif option_parsing and argument.startswith("-") and argument != "-":
                short_options = argument[1:]
                recursive = recursive or "r" in short_options or "R" in short_options
                force = force or "f" in short_options
            else:
                targets.append(argument)

        if not (recursive and force):
            return False
        return any(PermissionChecker._is_dangerous_rm_target(target) for target in targets)

    @staticmethod
    def _rm_arguments_at_command_position(tokens: list[str]) -> list[str] | None:
        """Return rm arguments when rm starts a simple command or follows sudo options."""
        if not tokens:
            return None

        command_index = 0
        if tokens[command_index] == "sudo":
            command_index += 1
            while command_index < len(tokens):
                option = tokens[command_index]
                if option == "--":
                    command_index += 1
                    break
                if option == "-" or not option.startswith("-"):
                    break
                if option in SUDO_OPTIONS_WITH_VALUE:
                    command_index += 1
                    if command_index >= len(tokens):
                        return None
                command_index += 1

        if command_index >= len(tokens) or tokens[command_index] != "rm":
            return None
        return tokens[command_index + 1 :]

    @staticmethod
    def _is_dangerous_rm_target(target: str) -> bool:
        """Preserve the existing absolute-path hard block, including root aliases."""
        expanded_target = os.path.expanduser(target)
        if not os.path.isabs(expanded_target):
            return False
        return (
            PermissionChecker._is_root_equivalent_target(expanded_target)
            or os.path.isabs(expanded_target)
        )

    @staticmethod
    def _is_root_equivalent_target(target: str) -> bool:
        """Recognize lexical spellings of the filesystem root without executing a command."""
        return not target.rstrip(os.path.sep) or os.path.normpath(target) == os.path.sep

    def get_call_counts(self) -> dict[str, int]:
        return dict(self._call_counts)

    def reset_counts(self) -> None:
        self._call_counts.clear()
