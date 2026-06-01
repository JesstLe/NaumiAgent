"""Structured task status rendering — CLI and TUI compatible.

Provides consistent rendering for:
- Todo list progress
- Subagent/teammate activity status
- Background task status
- Multi-task summary bar

Inspired by Claude Code's taskStatusUtils.tsx pattern:
each entity gets an icon, color, status text, and detail.
All output is ANSI-formatted text (CLI) or plain text (TUI can wrap).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskPhase(StrEnum):
    """Lifecycle phases for any tracked task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    IDLE = "idle"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"


# ANSI color codes
_CLR = {
    "success": "32",
    "error": "31",
    "warning": "33",
    "background": "36",
    "dim": "2",
    "bold": "1",
}

# Status icons
_ICON = {
    TaskPhase.PENDING: "○",
    TaskPhase.RUNNING: "▶",
    TaskPhase.COMPLETED: "✓",
    TaskPhase.FAILED: "✗",
    TaskPhase.KILLED: "⊘",
    TaskPhase.IDLE: "…",
    TaskPhase.AWAITING_APPROVAL: "?",
    TaskPhase.BLOCKED: "⚑",
}


def _color(phase: TaskPhase) -> str:
    if phase in (TaskPhase.COMPLETED,):
        return _CLR["success"]
    if phase in (TaskPhase.FAILED, TaskPhase.KILLED):
        return _CLR["error"]
    if phase in (TaskPhase.AWAITING_APPROVAL, TaskPhase.BLOCKED):
        return _CLR["warning"]
    if phase == TaskPhase.IDLE:
        return _CLR["dim"]
    return _CLR["background"]


def _icon(phase: TaskPhase) -> str:
    return _ICON.get(phase, "•")


@dataclass(frozen=True)
class TodoItem:
    """Single todo entry."""
    text: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    id: str = ""


@dataclass(frozen=True)
class AgentStatus:
    """Subagent or teammate status snapshot."""
    name: str = ""
    task_id: str = ""
    phase: TaskPhase = TaskPhase.IDLE
    description: str = ""  # current activity description
    error: str = ""


@dataclass(frozen=True)
class BackgroundTaskStatus:
    """Background shell task status."""
    task_id: str = ""
    command: str = ""
    phase: TaskPhase = TaskPhase.RUNNING
    exit_code: int | None = None
    runtime_s: float = 0.0
    last_output_line: str = ""


def render_todo_bar(items: tuple[TodoItem, ...], *, width: int = 0) -> str:
    """Render a compact todo progress bar for the bottom status area.

    Returns ANSI text suitable for the todo bar line.
    """
    if not items:
        return ""

    total = len(items)
    done = sum(1 for i in items if i.status == "completed")
    blocked = sum(1 for i in items if i.status == "blocked")

    # Progress bar
    bar_len = min(20, max(5, total * 2))
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    parts = [f"\033[{_CLR['dim']}m📋 {done}/{total} [{bar}]\033[0m"]

    # Show current active task
    active = next((i for i in items if i.status == "in_progress"), None)
    if active:
        text = active.text[:40] + ("…" if len(active.text) > 40 else "")
        parts.append(f"\033[{_CLR['background']}m{text}\033[0m")

    if blocked:
        parts.append(f"\033[{_CLR['warning']}m⚑{blocked} blocked\033[0m")

    return " ".join(parts)


def render_agent_status(agents: tuple[AgentStatus, ...]) -> str:
    """Render multi-agent status lines.

    Returns ANSI text with one line per active agent.
    """
    if not agents:
        return ""

    lines: list[str] = []
    for agent in agents:
        clr = _color(agent.phase)
        icon = _icon(agent.phase)
        name = agent.name or agent.task_id or "?"
        desc = agent.description[:50] if agent.description else str(agent.phase.value)
        line = f"\033[{clr}m{icon} {name}: {desc}\033[0m"
        if agent.error:
            line += f" \033[{_CLR['error']}m{agent.error[:60]}\033[0m"
        lines.append(line)

    return "\n".join(lines) + "\n"


def render_background_status(tasks: tuple[BackgroundTaskStatus, ...]) -> str:
    """Render background task status lines.

    Returns ANSI text with one line per background task.
    """
    if not tasks:
        return ""

    lines: list[str] = []
    for task in tasks:
        clr = _color(task.phase)
        icon = _icon(task.phase)
        cmd = task.command[:40] + ("…" if len(task.command) > 40 else "")
        parts = [f"\033[{clr}m{icon}"]

        if task.phase == TaskPhase.RUNNING:
            parts.append(f"⏱{task.runtime_s:.0f}s")
        elif task.exit_code is not None:
            if task.exit_code == 0:
                parts.append("✓ exit 0")
            else:
                parts.append(f"✗ exit {task.exit_code}")

        status_text = " ".join(parts)
        detail = task.last_output_line[:50] if task.last_output_line else cmd
        lines.append(f"{status_text} {detail}\033[0m")

    return "\n".join(lines) + "\n"


def render_task_summary_bar(
    *,
    todo_items: tuple[TodoItem, ...] = (),
    agents: tuple[AgentStatus, ...] = (),
    background_tasks: tuple[BackgroundTaskStatus, ...] = (),
) -> str:
    """Render a compact single-line summary for the activity bar.

    Combines todo progress, agent count, and background task count into
    one line suitable for the activity/status bar.
    """
    parts: list[str] = []

    if todo_items:
        total = len(todo_items)
        done = sum(1 for i in todo_items if i.status == "completed")
        parts.append(f"📋 {done}/{total}")

    active_agents = [a for a in agents if a.phase == TaskPhase.RUNNING]
    if active_agents:
        names = ", ".join(a.name or a.task_id for a in active_agents[:3])
        if len(active_agents) > 3:
            names += f" +{len(active_agents) - 3}"
        parts.append(f"🚀 {names}")

    running_bg = [t for t in background_tasks if t.phase == TaskPhase.RUNNING]
    if running_bg:
        parts.append(f"⏱ {len(running_bg)} bg")

    if not parts:
        return ""

    return "\033[2m" + " | ".join(parts) + "\033[0m"


def render_todo_detail_panel(items: tuple[TodoItem, ...]) -> str:
    """Render a full todo detail panel for the output area.

    Returns multi-line ANSI text with all todo items and their status.
    """
    if not items:
        return "\033[2m  暂无任务\033[0m\n"

    lines: list[str] = ["\033[1m📋 任务清单\033[0m\n"]

    status_style: dict[str, tuple[str, str]] = {
        "completed": (_CLR["success"], "✓"),
        "in_progress": (_CLR["background"], "▶"),
        "blocked": (_CLR["warning"], "⚑"),
        "pending": (_CLR["dim"], "○"),
    }

    for item in items:
        clr, icon = status_style.get(item.status, (_CLR["dim"], "○"))
        text = item.text[:70] + ("…" if len(item.text) > 70 else "")
        lines.append(f"  \033[{clr}m{icon} {text}\033[0m")

    total = len(items)
    done = sum(1 for i in items if i.status == "completed")
    pct = int(100 * done / total) if total > 0 else 0
    lines.append(f"\n\033[2m  {done}/{total} 完成 ({pct}%)\033[0m\n")

    return "\n".join(lines)
