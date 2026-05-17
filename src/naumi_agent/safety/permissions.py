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

    def __init__(self, mode: PermissionMode, allowed_dirs: list[str] | None = None) -> None:
        self._mode = mode
        self._allowed_dirs = allowed_dirs or ["/workspace"]
        self._call_counts: dict[str, int] = {}

    def check(self, tool_name: str, args: dict[str, Any]) -> PermissionDecision:
        """检查工具调用是否被允许."""
        rule = TOOL_PERMISSIONS.get(tool_name)

        # 某些 API（如 Kimi）返回的工具名可能带 namespace 前缀
        if not rule:
            normalized = tool_name
            if "." in normalized:
                normalized = normalized.split(".")[-1]
            elif "__" in normalized:
                normalized = normalized.split("__")[-1]
            if normalized != tool_name:
                rule = TOOL_PERMISSIONS.get(normalized)
                if rule:
                    tool_name = normalized

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

        # 命令检查
        if tool_name == "bash_run" and "command" in args:
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

        abs_path = os.path.abspath(os.path.expanduser(path))
        if any(abs_path.startswith(allowed) for allowed in self._allowed_dirs):
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
