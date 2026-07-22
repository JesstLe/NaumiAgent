"""Authoritative, bounded command metadata shared by terminal surfaces."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
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

_CATEGORY_SEARCH_LABELS = {
    "basic": "基础",
    "session": "会话",
    "analysis": "分析",
    "orchestration": "编排",
    "navigation": "导航",
    "control": "控制",
}
_RISK_SEARCH_LABELS = {
    "read_only": "只读",
    "session_state": "会话状态",
    "permission_change": "权限变更",
    "workspace_write": "工作区写入",
    "tool_execution": "工具执行",
    "destructive": "破坏性",
}

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


def search_terminal_commands(
    entries: Sequence[TerminalCommandIndexEntry],
    query: str,
    *,
    limit: int = 50,
    recent_commands: Sequence[str] = (),
) -> tuple[TerminalCommandIndexEntry, ...]:
    """Rank bounded command metadata without executing or mutating a command."""
    if limit < 1 or limit > 200:
        raise ValueError("命令搜索 limit 必须在 1 到 200 之间。")
    term = _normalize_search_text(query)[:200].removeprefix("/")
    recent_rank = {
        command: index
        for index, command in enumerate(tuple(recent_commands)[:20])
    }
    ranked = [
        (score, recent_rank.get(entry.command, 20), entry.command, entry)
        for entry in entries
        if (score := _command_search_score(entry, term)) is not None
    ]
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in ranked[:limit])


def record_recent_terminal_command(
    entries: Sequence[TerminalCommandIndexEntry],
    recent_commands: Sequence[str],
    submitted_text: str,
    *,
    limit: int = 20,
) -> tuple[str, ...]:
    """Record one known command name without retaining arguments or user text."""
    if limit < 1 or limit > 20:
        raise ValueError("最近命令 limit 必须在 1 到 20 之间。")
    token = submitted_text.strip().split(maxsplit=1)[0].lower()
    if not token.startswith("/"):
        return tuple(recent_commands)[:limit]
    aliases = {
        alias.lower(): entry.command
        for entry in entries
        for alias in entry.aliases
    }
    canonical = next(
        (entry.command for entry in entries if entry.command.lower() == token),
        aliases.get(token, ""),
    )
    if not canonical:
        return tuple(recent_commands)[:limit]
    remaining = tuple(
        command for command in recent_commands if command != canonical
    )
    return (canonical, *remaining)[:limit]


def terminal_command_template(entry: TerminalCommandIndexEntry) -> str:
    """Return the editable composer template for a QuickOpen selection."""
    syntax = entry.arguments.syntax.strip()
    return f"{entry.command} {syntax}" if syntax else entry.command


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


def _command_search_score(
    entry: TerminalCommandIndexEntry,
    term: str,
) -> int | None:
    if not term:
        return 0
    command = _normalize_search_text(entry.command.removeprefix("/"))
    aliases = tuple(
        _normalize_search_text(alias.removeprefix("/")) for alias in entry.aliases
    )
    if term == command:
        return 0
    if term in aliases:
        return 1
    if command.startswith(term):
        return 10_000 + len(command) - len(term)
    alias_prefixes = [len(alias) - len(term) for alias in aliases if alias.startswith(term)]
    if alias_prefixes:
        return 12_000 + min(alias_prefixes)
    if term in command:
        return 20_000 + command.index(term)
    alias_positions = [alias.index(term) for alias in aliases if term in alias]
    if alias_positions:
        return 30_000 + min(alias_positions)
    metadata = _normalize_search_text(
        " ".join(
            (
                entry.description,
                entry.category,
                _CATEGORY_SEARCH_LABELS[entry.category],
                entry.permission_risk,
                _RISK_SEARCH_LABELS[entry.permission_risk],
                entry.source,
            )
        )
    )
    if term in metadata:
        return 40_000 + metadata.index(term)
    command_gap = _subsequence_gap(term, command)
    if command_gap is not None:
        return 60_000 + command_gap
    alias_gaps = [
        gap for alias in aliases if (gap := _subsequence_gap(term, alias)) is not None
    ]
    if alias_gaps:
        return 70_000 + min(alias_gaps)
    metadata_gap = _subsequence_gap(term, metadata)
    return None if metadata_gap is None else 100_000 + metadata_gap


def _subsequence_gap(term: str, target: str) -> int | None:
    position = -1
    score = 0
    for character in term:
        next_position = target.find(character, position + 1)
        if next_position < 0:
            return None
        score += next_position if position < 0 else next_position - position - 1
        position = next_position
    return score


def _normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


__all__ = [
    "COMMAND_ALIASES",
    "COMMAND_INDEX_SCHEMA_VERSION",
    "CommandArgumentSchema",
    "TerminalCommandIndexEntry",
    "build_terminal_command_index",
    "search_terminal_commands",
    "terminal_command_template",
]
