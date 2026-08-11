"""Authoritative, surface-aware terminal navigation page index."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from naumi_agent.ui.command_index import build_terminal_command_index

PAGE_INDEX_SCHEMA_VERSION = 1

type PageSurface = Literal["new_ui", "tui"]


class TerminalPageIndexEntry(BaseModel):
    """One safe, exact navigation destination exposed to terminal frontends."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1] = PAGE_INDEX_SCHEMA_VERSION
    page_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    command: str = Field(pattern=r"^/[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    keywords: tuple[str, ...] = Field(max_length=12)
    order: StrictInt = Field(ge=0, le=1_000)
    surface: PageSurface

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.keywords != tuple(sorted(set(self.keywords))):
            raise ValueError("页面 keywords 必须排序且不得重复。")
        for name, value in (
            ("label", self.label),
            ("description", self.description),
            *((f"keywords[{index}]", keyword) for index, keyword in enumerate(self.keywords)),
        ):
            if value != unicodedata.normalize("NFKC", value).strip():
                raise ValueError(f"页面 {name} 必须是规范化且无首尾空白的文本。")
            if re.search(r"[\u0000-\u001f\u007f]", value):
                raise ValueError(f"页面 {name} 不得包含控制字符。")
        return self

    def to_public_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _PageDefinition:
    page_id: str
    command: str
    label: str
    description: str
    keywords: tuple[str, ...]
    order: int
    surfaces: frozenset[PageSurface]


_BOTH_SURFACES: frozenset[PageSurface] = frozenset({"new_ui", "tui"})
_PAGE_DEFINITIONS = (
    _PageDefinition(
        page_id="conversation",
        command="/chat",
        label="对话",
        description="返回主对话与输入区。",
        keywords=("chat", "主页", "聊天", "输入"),
        order=0,
        surfaces=frozenset({"new_ui"}),
    ),
    _PageDefinition(
        page_id="tasks",
        command="/tasks",
        label="任务",
        description="查看统一任务面板、状态、详情与取消入口。",
        keywords=("tasks", "任务", "待办", "进度"),
        order=10,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="goals",
        command="/goal",
        label="Goal",
        description="查看持久 Goal、Pursuit 状态与阻塞信息。",
        keywords=("goal", "pursuit", "目标", "追踪"),
        order=20,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="agents",
        command="/agents",
        label="Agent 控制中心",
        description="查看 Agent、执行、团队消息与协作状态。",
        keywords=("agent", "team", "子智能体", "智能体", "集群"),
        order=30,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="workbench",
        command="/workbench",
        label="Workbench",
        description="查看工作树、审查与权威运行概览。",
        keywords=("git", "workbench", "工作台", "工作树", "审查"),
        order=40,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="permissions",
        command="/permissions",
        label="权限",
        description="查看待确认请求、授权范围与撤销入口。",
        keywords=("approval", "permission", "批准", "授权", "权限"),
        order=50,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="doctor",
        command="/doctor",
        label="Doctor",
        description="运行并查看环境、配置和运行时健康诊断。",
        keywords=("doctor", "health", "健康", "环境", "诊断"),
        order=60,
        surfaces=_BOTH_SURFACES,
    ),
    _PageDefinition(
        page_id="evolution",
        command="/evolution",
        label="Evolution",
        description=(
            "查看自进化 Candidate、Stable Population 候选、受控回滚回执与 Proposal Outcome。"
        ),
        keywords=(
            "candidate",
            "evolution",
            "rollback",
            "outcome",
            "proposal",
            "population",
            "候选",
            "审查",
            "进化",
            "稳定发布",
            "回滚",
            "结果",
        ),
        order=70,
        surfaces=_BOTH_SURFACES,
    ),
)


def build_terminal_page_index(surface: PageSurface) -> tuple[TerminalPageIndexEntry, ...]:
    """Build a deterministic page projection whose commands exist on the surface."""
    if surface not in {"new_ui", "tui"}:
        raise ValueError("未知 terminal page surface。")
    available_commands = {
        entry.command for entry in build_terminal_command_index(surface)
    }
    entries: list[TerminalPageIndexEntry] = []
    for definition in _PAGE_DEFINITIONS:
        if surface not in definition.surfaces:
            continue
        if definition.command not in available_commands:
            raise ValueError(
                f"页面 {definition.page_id} 引用了当前 surface 不存在的命令。"
            )
        entries.append(
            TerminalPageIndexEntry(
                page_id=definition.page_id,
                command=definition.command,
                label=definition.label,
                description=definition.description,
                keywords=tuple(sorted(definition.keywords)),
                order=definition.order,
                surface=surface,
            )
        )
    if len({entry.page_id for entry in entries}) != len(entries):
        raise ValueError("页面索引包含重复 page_id。")
    if len({entry.command for entry in entries}) != len(entries):
        raise ValueError("页面索引包含重复 command。")
    return tuple(sorted(entries, key=lambda entry: (entry.order, entry.page_id)))


def search_terminal_pages(
    entries: Sequence[TerminalPageIndexEntry],
    query: str,
    *,
    limit: int = 32,
) -> tuple[TerminalPageIndexEntry, ...]:
    """Search bounded page metadata without navigating or mutating runtime state."""
    if limit < 1 or limit > 32:
        raise ValueError("页面 QuickOpen limit 必须在 1 到 32 之间。")
    term = _normalize_search_text(query)[:200].removeprefix("/")
    ranked = [
        (score, entry.order, entry.page_id, entry)
        for entry in entries
        if (score := _page_search_score(entry, term)) is not None
    ]
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in ranked[:limit])


def terminal_page_template(entry: TerminalPageIndexEntry) -> str:
    """Return the exact navigation command; selection never executes it."""
    if re.fullmatch(r"/[a-z][a-z0-9_-]{0,63}", entry.command) is None:
        raise ValueError("页面命令无法安全填入 QuickOpen。")
    return entry.command


def _page_search_score(
    entry: TerminalPageIndexEntry,
    term: str,
) -> int | None:
    if not term:
        return 0
    page_id = _normalize_search_text(entry.page_id)
    command = _normalize_search_text(entry.command.removeprefix("/"))
    label = _normalize_search_text(entry.label)
    if term in {page_id, command, label}:
        return 0
    if page_id.startswith(term) or command.startswith(term):
        return 10_000
    if label.startswith(term):
        return 20_000
    if term in page_id or term in command:
        return 30_000
    if term in label:
        return 40_000
    metadata = _normalize_search_text(
        " ".join((*entry.keywords, entry.description, entry.surface))
    )
    if term in metadata:
        return 50_000 + metadata.index(term)
    gap = _subsequence_gap(term, f"{page_id} {command} {label} {metadata}")
    return None if gap is None else 100_000 + gap


def _normalize_search_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


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


__all__ = [
    "PAGE_INDEX_SCHEMA_VERSION",
    "TerminalPageIndexEntry",
    "build_terminal_page_index",
    "search_terminal_pages",
    "terminal_page_template",
]
