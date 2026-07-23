"""Bounded Agent QuickOpen search and safe Agent Control deep links."""

from __future__ import annotations

import re
import shlex
import unicodedata
from collections.abc import Sequence

from naumi_agent.agent_control import AgentDescriptor

AGENT_QUICK_OPEN_QUERY_LIMIT = 200
AGENT_QUICK_OPEN_RESULT_LIMIT = 50
AGENT_QUICK_OPEN_SOURCE_LIMIT = 100

_SAFE_AGENT_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_STATE_ORDER = {
    "running": 0,
    "ready": 1,
    "spawned": 2,
    "idle": 3,
    "uninitialized": 4,
    "destroyed": 5,
}
_KIND_ORDER = {"dynamic": 0, "preset": 1}
_STATE_LABELS = {
    "uninitialized": "未初始化",
    "spawned": "已启动",
    "ready": "就绪",
    "running": "运行中",
    "idle": "空闲",
    "destroyed": "已销毁",
}
_KIND_LABELS = {"preset": "预置", "dynamic": "动态"}


def search_terminal_agents(
    items: Sequence[AgentDescriptor],
    query: str,
    *,
    limit: int = AGENT_QUICK_OPEN_RESULT_LIMIT,
) -> tuple[AgentDescriptor, ...]:
    """Rank one bounded authoritative Agent projection."""
    if isinstance(limit, bool) or not 1 <= limit <= AGENT_QUICK_OPEN_RESULT_LIMIT:
        raise ValueError("Agent QuickOpen limit 必须在 1 到 50 之间。")
    term = _normalize(query)[:AGENT_QUICK_OPEN_QUERY_LIMIT]
    ranked = [
        (
            score,
            _STATE_ORDER.get(item.state, 9),
            _KIND_ORDER.get(item.kind, 9),
            _normalize(item.name),
            item,
        )
        for item in tuple(items)[:AGENT_QUICK_OPEN_SOURCE_LIMIT]
        if is_safe_agent_quick_open_item(item)
        if (score := _agent_search_score(item, term)) is not None
    ]
    ranked.sort(key=lambda value: value[:4])
    return tuple(value[4] for value in ranked[:limit])


def terminal_agent_template(item: AgentDescriptor | str) -> str:
    """Return an editable Agent Control deep link without opening it."""
    name = item.name if isinstance(item, AgentDescriptor) else str(item)
    if not _SAFE_AGENT_NAME.fullmatch(name):
        raise ValueError("Agent 名称无法安全填入 QuickOpen。")
    return f"/agents agent {shlex.quote(name)}"


def parse_terminal_agent_deep_link(text: str) -> str | None:
    """Parse only the read-only Agent detail form used by QuickOpen."""
    try:
        parts = shlex.split(str(text or ""), posix=True)
    except ValueError:
        return None
    if len(parts) != 3 or parts[0].lower() != "/agents" or parts[1].lower() != "agent":
        return None
    name = parts[2]
    return name if _SAFE_AGENT_NAME.fullmatch(name) else None


def is_safe_agent_quick_open_item(item: AgentDescriptor) -> bool:
    return bool(
        isinstance(item, AgentDescriptor)
        and _SAFE_AGENT_NAME.fullmatch(item.name)
        and item.state in _STATE_ORDER
        and item.kind in _KIND_ORDER
    )


def _agent_search_score(item: AgentDescriptor, term: str) -> int | None:
    if not term:
        return 0
    name = _normalize(item.name)
    if term == name:
        return 0
    if name.startswith(term):
        return 10_000 + len(name) - len(term)
    if term in name:
        return 20_000 + name.index(term)
    metadata = _normalize(" ".join((
        item.description,
        item.kind,
        _KIND_LABELS.get(item.kind, item.kind),
        item.state,
        _STATE_LABELS.get(item.state, item.state),
        item.model_tier,
        item.permission_level,
        *item.capabilities,
        *item.tools,
    )))
    if term in metadata:
        return 30_000 + metadata.index(term)
    gap = _subsequence_gap(term, f"{name} {metadata}")
    return None if gap is None else 100_000 + gap


def _normalize(value: str) -> str:
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
    "AGENT_QUICK_OPEN_QUERY_LIMIT",
    "AGENT_QUICK_OPEN_RESULT_LIMIT",
    "search_terminal_agents",
    "terminal_agent_template",
    "parse_terminal_agent_deep_link",
]
