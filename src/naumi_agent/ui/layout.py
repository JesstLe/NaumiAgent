"""Shared bottom-bar layout helpers for CLI and TUI.

Provides consistent behavior for:
- Status bar: mode | model | workspace | tokens | budget | git
- Todo bar: active task progress
- Activity bar: transient tool preparation status
- Resize-safe width truncation
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BottomBarState:
    """Immutable snapshot of all bottom bar content."""

    mode: str = "default"
    status: str = "就绪"
    todo_text: str = ""
    activity_text: str = ""
    model: str = ""
    workspace: str = ""
    tokens: str = ""
    budget: str = ""
    git_branch: str = ""
    git_dirty: bool = False
    processing: bool = False


def format_status_bar(state: BottomBarState) -> str:
    """Format the main status bar line.

    Layout: mode: xxx | status_text
    All content is width-safe: callers should truncate to terminal width.
    """
    parts = [f"mode: {state.mode}"]
    if state.status:
        parts.append(state.status)
    return " | ".join(parts)


def format_todo_bar(state: BottomBarState) -> str:
    """Format the todo bar line.

    Returns empty string if no active todo.
    """
    if not state.todo_text:
        return ""
    return state.todo_text


def format_activity_bar(state: BottomBarState) -> str:
    """Format the activity bar line.

    Returns empty string if no activity.
    """
    if not state.activity_text:
        return ""
    return state.activity_text


def build_full_status_text(
    *,
    mode: str = "default",
    model: str = "",
    workspace: str = "",
    tokens: str = "",
    budget: str = "",
    git_branch: str = "",
    git_dirty: bool = False,
) -> str:
    """Build a detailed status string for display in the status bar.

    Used by both CLI and TUI for the initial/enhanced status display.
    """
    parts: list[str] = []
    if model:
        parts.append(model)
    if workspace:
        parts.append(f"工作区: {workspace}")
    if tokens:
        parts.append(tokens)
    if budget:
        parts.append(budget)
    if git_branch:
        tag = git_branch + ("*" if git_dirty else "")
        parts.append(f"📂 {tag}")
    return " | ".join(parts)


def clip_to_width(text: str, width: int) -> str:
    """Clip text to exactly *width* terminal cells.

    Uses prompt_toolkit's get_cwidth for CJK/emoji-safe measurement.
    Falls back to len() if prompt_toolkit is unavailable.
    """
    if width <= 0:
        return ""
    try:
        from prompt_toolkit.utils import get_cwidth

        text_width = sum(get_cwidth(ch) for ch in text)
    except ImportError:
        text_width = len(text)

    if text_width <= width:
        return text + " " * (width - text_width)

    # Truncate with ellipsis
    marker = "…"
    try:
        from prompt_toolkit.utils import get_cwidth

        marker_w = get_cwidth(marker)
    except ImportError:
        marker_w = 1

    target = width - marker_w
    if target <= 0:
        return marker[:width]

    out: list[str] = []
    current_width = 0
    for ch in text:
        try:
            from prompt_toolkit.utils import get_cwidth

            ch_w = get_cwidth(ch)
        except ImportError:
            ch_w = 1
        if current_width + ch_w > target:
            break
        out.append(ch)
        current_width += ch_w
    return "".join(out) + marker


def compute_output_guard_height(
    *,
    has_todo: bool = False,
    has_activity: bool = False,
) -> int:
    """Return the number of lines reserved for the bottom bars.

    The output window must never write into this region.
    Layout:
    - activity bar: 0 or 1 line
    - todo bar: 0 or 1 line
    - status bar: 1 line
    - top border: 1 line
    - input: 1 line
    - bottom border: 1 line

    Total: 4 + has_activity + has_todo
    """
    base = 4  # status + border_top + input + border_bot
    if has_activity:
        base += 1
    if has_todo:
        base += 1
    return base
