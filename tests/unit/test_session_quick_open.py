import pytest

from naumi_agent.ui.session_list import SessionListItem
from naumi_agent.ui.session_quick_open import (
    search_terminal_sessions,
    terminal_session_template,
)


def _item(session_id: str, title: str, updated: str, *, current: bool = False) -> SessionListItem:
    return SessionListItem(
        session_id=session_id,
        title=title,
        model="provider/model",
        updated_at=updated,
        message_count=2,
        user_message_count=1,
        git_branch="main",
        is_current=current,
        resumable=True,
    )


def test_session_quick_open_search_ranking_and_safe_template() -> None:
    items = (
        _item("newer", "新会话", "2026-07-22"),
        _item("current", "当前会话", "2026-07-20", current=True),
    )
    assert [item.session_id for item in search_terminal_sessions(items, "")] == ["current", "newer"]
    assert [item.session_id for item in search_terminal_sessions(items, "新")] == ["newer"]
    assert terminal_session_template(items[1]) == "/load current"
    unsafe = _item("bad;id", "异常", "2026-07-19")
    with pytest.raises(ValueError, match="安全恢复"):
        terminal_session_template(unsafe)
