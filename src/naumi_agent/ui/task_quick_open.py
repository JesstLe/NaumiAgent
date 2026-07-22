"""Bounded task QuickOpen projection shared by Python terminal surfaces."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from naumi_agent.ui.task_panel import TaskViewItem

TASK_QUICK_OPEN_QUERY_LIMIT = 200
TASK_QUICK_OPEN_RESULT_LIMIT = 50
TASK_QUICK_OPEN_SOURCE_LIMIT = 200

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_ACTIVE_STATUS_ORDER = {
    "running": 0,
    "blocked": 1,
    "pending": 2,
    "failed": 3,
    "cancelled": 4,
    "completed": 5,
}
_SOURCE_ORDER = {"todo": 0, "subagent": 1, "background": 2, "browser": 3}


def search_terminal_tasks(
    items: Sequence[TaskViewItem],
    query: str,
    *,
    limit: int = TASK_QUICK_OPEN_RESULT_LIMIT,
) -> tuple[TaskViewItem, ...]:
    """Rank public task rows without exposing invalid IDs or mutating tasks."""
    if limit < 1 or limit > TASK_QUICK_OPEN_RESULT_LIMIT:
        raise ValueError("任务 QuickOpen limit 必须在 1 到 50 之间。")
    term = _normalize(query)[:TASK_QUICK_OPEN_QUERY_LIMIT]
    ranked = [
        (
            score,
            _ACTIVE_STATUS_ORDER.get(item.status, 9),
            _SOURCE_ORDER.get(item.source, 9),
            item.view_id,
            item,
        )
        for item in tuple(items)[:TASK_QUICK_OPEN_SOURCE_LIMIT]
        if is_safe_task_quick_open_item(item)
        if (score := _task_search_score(item, term)) is not None
    ]
    ranked.sort(key=lambda value: value[:4])
    return tuple(value[4] for value in ranked[:limit])


def terminal_task_template(item: TaskViewItem) -> str:
    """Return a read-only detail command that still requires explicit submit."""
    if not is_safe_task_quick_open_item(item):
        raise ValueError("任务 ID 无法安全填入 QuickOpen。")
    return f"/tasks detail {item.task_id}"


def is_safe_task_quick_open_item(item: TaskViewItem) -> bool:
    """Reject rows that cannot round-trip through the slash command parser."""
    return bool(
        _SAFE_TASK_ID.fullmatch(item.task_id)
        and item.source in _SOURCE_ORDER
        and item.view_id
    )


def _task_search_score(item: TaskViewItem, term: str) -> int | None:
    if not term:
        return 0
    task_id = _normalize(item.task_id)
    view_id = _normalize(item.view_id)
    title = _normalize(item.title)
    owner = _normalize(item.owner)
    if term in {task_id, view_id}:
        return 0
    if task_id.startswith(term) or view_id.startswith(term):
        return 10_000
    if title.startswith(term):
        return 20_000
    if term in task_id or term in view_id:
        return 30_000
    if term in title:
        return 40_000
    metadata = _normalize(
        " ".join(
            (
                item.source,
                item.status,
                item.owner,
                item.detail,
                _source_label(item.source),
                _status_label(item.status),
            )
        )
    )
    if term in metadata:
        return 50_000
    gap = _subsequence_gap(term, " ".join((task_id, title, owner, metadata)))
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


def _source_label(source: str) -> str:
    return {
        "todo": "待办",
        "subagent": "子智能体",
        "background": "后台任务",
        "browser": "浏览器",
    }.get(source, source)


def _status_label(status: str) -> str:
    return {
        "pending": "等待",
        "running": "运行中",
        "blocked": "阻塞",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, status)


__all__ = [
    "TASK_QUICK_OPEN_QUERY_LIMIT",
    "TASK_QUICK_OPEN_RESULT_LIMIT",
    "TASK_QUICK_OPEN_SOURCE_LIMIT",
    "is_safe_task_quick_open_item",
    "search_terminal_tasks",
    "terminal_task_template",
]
