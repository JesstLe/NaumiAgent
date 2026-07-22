"""Shared search and safe composer template for session QuickOpen."""

from __future__ import annotations

import re
from collections.abc import Sequence

from naumi_agent.ui.session_list import SessionListItem


def search_terminal_sessions(
    entries: Sequence[SessionListItem], query: str, *, limit: int = 100
) -> tuple[SessionListItem, ...]:
    term = query.strip().casefold()[:200]
    matching = [
        item
        for item in entries
        if item.resumable
        and (
            not term
            or term
            in " ".join((item.session_id, item.title, item.model, item.git_branch)).casefold()
        )
    ]
    matching.sort(key=lambda item: item.updated_at, reverse=True)
    matching.sort(key=lambda item: not item.is_current)
    return tuple(matching[: max(1, min(100, limit))])


def terminal_session_template(item: SessionListItem) -> str:
    if not item.resumable or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", item.session_id
    ):
        raise ValueError("会话不可安全恢复。")
    return f"/load {item.session_id}"


__all__ = ["search_terminal_sessions", "terminal_session_template"]
