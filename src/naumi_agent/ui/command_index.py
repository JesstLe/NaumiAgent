"""Authoritative, bounded command metadata shared by terminal surfaces."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from naumi_agent.cli.completer import COMMANDS_META, CommandMeta

COMMAND_INDEX_SCHEMA_VERSION = 1
COMMAND_ALIASES: dict[str, str] = {
    "/h": "/help",
    "/histroy": "/history",
    "/r": "/resume",
    "/l": "/load",
    "/t": "/tools",
    "/c": "/clear",
    "/m": "/model",
    "/u": "/usage",
    "/v": "/version",
    "/n": "/new",
}

type CommandSurface = Literal["new_ui", "tui"]
type CommandSource = Literal["shared_runtime", "new_ui", "tui"]
type PermissionRisk = Literal[
    "read_only",
    "session_state",
    "permission_change",
    "workspace_write",
    "tool_execution",
    "destructive",
]

_CATEGORY_MAP = {
    "基础": "basic",
    "会话": "session",
    "分析": "analysis",
    "元命令": "orchestration",
}
_SESSION_STATE = frozenset(
    {
        "/q",
        "/quit",
        "/exit",
        "/clear",
        "/new",
        "/load",
        "/resume",
        "/reasoning",
        "/effort",
        "/style",
        "/chat",
        "/fold",
        "/folds",
        "/expand",
        "/collapse",
        "/cancel-queued",
    }
)
_PERMISSION_CHANGE = frozenset({"/mode", "/permissions"})
_DESTRUCTIVE = frozenset(
    {
        "/delete",
        "/forge-remove",
        "/task-abort",
        "/browser-stop",
    }
)
_WORKSPACE_WRITE = frozenset(
    {
        "/write",
        "/file_write",
        "/edit",
        "/file_edit",
        "/vibe",
        "/heal",
        "/jit",
        "/genesis",
        "/evolve",
        "/forge",
        "/reload",
    }
)


class CommandArgumentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    takes_arguments: StrictBool
    syntax: str = Field(max_length=300)
    required: StrictBool

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.takes_arguments != bool(self.syntax):
            raise ValueError("命令参数声明与 syntax 不一致。")
        if self.required and not self.takes_arguments:
            raise ValueError("无参数命令不得声明 required。")
        return self


class TerminalCommandIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1] = COMMAND_INDEX_SCHEMA_VERSION
    command: str = Field(pattern=r"^/[a-z][a-z0-9_-]{0,63}$")
    aliases: tuple[str, ...] = Field(max_length=12)
    description: str = Field(min_length=1, max_length=300)
    category: Literal["basic", "session", "analysis", "orchestration", "navigation", "control"]
    source: CommandSource
    readonly: StrictBool
    permission_risk: PermissionRisk
    arguments: CommandArgumentSchema

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.aliases != tuple(sorted(set(self.aliases))):
            raise ValueError("命令 aliases 必须排序且不得重复。")
        if any(
            re.fullmatch(r"/[a-z][a-z0-9_-]{0,63}", alias) is None or alias == self.command
            for alias in self.aliases
        ):
            raise ValueError("命令 alias 格式无效。")
        if self.readonly != (self.permission_risk == "read_only"):
            raise ValueError("命令 readonly 与 permission risk 不一致。")
        return self

    def to_public_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


_LOCAL_COMMANDS: dict[CommandSurface, tuple[CommandMeta, ...]] = {
    "new_ui": (
        CommandMeta("/chat", "切换为普通对话输入", readonly=False, category="navigation"),
        CommandMeta("/agents", "打开 Agent 控制中心", category="navigation"),
        CommandMeta("/workbench", "刷新 Workbench 权威快照", category="navigation"),
        CommandMeta(
            "/mode",
            "切换 runtime 模式 default / plan / bypass",
            takes_arg=True,
            arg_hint="<default|plan|bypass>",
            readonly=False,
            category="control",
        ),
        CommandMeta(
            "/retry", "重试最近一条发送失败或待确认消息", readonly=False, category="control"
        ),
        CommandMeta(
            "/send-now",
            "提升排队消息到下一安全执行位置",
            takes_arg=True,
            arg_hint="[request-id]",
            readonly=False,
            category="control",
        ),
        CommandMeta(
            "/cancel-queued",
            "取消尚未派发的排队消息",
            takes_arg=True,
            arg_hint="[request-id]",
            readonly=False,
            category="control",
        ),
        CommandMeta("/folds", "显示可折叠内容列表", category="navigation"),
        CommandMeta(
            "/fold",
            "切换指定折叠项",
            takes_arg=True,
            arg_hint="<编号|类型>",
            readonly=False,
            category="navigation",
        ),
        CommandMeta(
            "/expand",
            "展开指定折叠项",
            takes_arg=True,
            arg_hint="<编号|all>",
            readonly=False,
            category="navigation",
        ),
        CommandMeta(
            "/collapse",
            "折叠指定折叠项",
            takes_arg=True,
            arg_hint="<编号|all>",
            readonly=False,
            category="navigation",
        ),
    ),
    "tui": (
        CommandMeta("/agents", "打开 Agent 控制中心", category="navigation"),
        CommandMeta("/workbench", "刷新 Workbench 权威快照", category="navigation"),
        CommandMeta(
            "/send-now",
            "提升排队消息到下一安全执行位置",
            takes_arg=True,
            arg_hint="[request-id]",
            readonly=False,
            category="control",
        ),
        CommandMeta(
            "/cancel-queued",
            "取消尚未派发的排队消息",
            takes_arg=True,
            arg_hint="[request-id]",
            readonly=False,
            category="control",
        ),
    ),
}


def build_terminal_command_index(surface: CommandSurface) -> tuple[TerminalCommandIndexEntry, ...]:
    """Build one deterministic surface index from shared runtime metadata."""
    if surface not in _LOCAL_COMMANDS:
        raise ValueError("未知 terminal command surface。")
    entries: dict[str, TerminalCommandIndexEntry] = {}
    for meta in (*COMMANDS_META, *_LOCAL_COMMANDS[surface]):
        source: CommandSource = "shared_runtime" if meta in COMMANDS_META else surface
        entry = _entry(meta, source=source)
        if entry.command in entries:
            raise ValueError(f"命令索引包含重复 command: {entry.command}")
        entries[entry.command] = entry
    return tuple(sorted(entries.values(), key=lambda item: (item.category, item.command)))


def _entry(meta: CommandMeta, *, source: CommandSource) -> TerminalCommandIndexEntry:
    aliases = tuple(
        sorted(alias for alias, canonical in COMMAND_ALIASES.items() if canonical == meta.name)
    )
    syntax = meta.arg_hint.strip() if meta.takes_arg else ""
    return TerminalCommandIndexEntry(
        command=meta.name,
        aliases=aliases,
        description=meta.description.strip(),
        category=_category(meta.category),
        source=source,
        readonly=meta.readonly,
        permission_risk=_permission_risk(meta),
        arguments=CommandArgumentSchema(
            takes_arguments=meta.takes_arg,
            syntax=syntax,
            required=bool(syntax.startswith("<")),
        ),
    )


def _category(value: str) -> str:
    if value in {"navigation", "control"}:
        return value
    normalized = value.split("(", 1)[0].strip()
    return _CATEGORY_MAP.get(normalized, "basic")


def _permission_risk(meta: CommandMeta) -> PermissionRisk:
    if meta.readonly:
        return "read_only"
    if meta.name in _DESTRUCTIVE:
        return "destructive"
    if meta.name in _PERMISSION_CHANGE:
        return "permission_change"
    if meta.name in _WORKSPACE_WRITE:
        return "workspace_write"
    if meta.name in _SESSION_STATE:
        return "session_state"
    return "tool_execution"


__all__ = [
    "COMMAND_ALIASES",
    "COMMAND_INDEX_SCHEMA_VERSION",
    "CommandArgumentSchema",
    "TerminalCommandIndexEntry",
    "build_terminal_command_index",
]
