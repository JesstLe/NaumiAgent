"""Terminal-safe Rich frames for Textual's working indicator."""

from __future__ import annotations

from rich.text import Text

WORKING_INDICATOR_FRAME_COUNT = 4

_FRAMES = (
    ("◐", "bold magenta"),
    ("◓", "bold blue"),
    ("◑", "bold cyan"),
    ("◒", "bold green"),
)


def render_working_indicator_frame(index: int) -> Text:
    """Return one stable-width animated Naumi core frame."""
    normalized = int(index) % WORKING_INDICATOR_FRAME_COUNT
    core, core_style = _FRAMES[normalized]
    rendered = Text()
    rendered.append("  ╭", style="cyan")
    rendered.append(core, style=core_style)
    rendered.append("╮ ", style="cyan")
    rendered.append("Naumi 工作中", style="bold green")
    return rendered


__all__ = ["WORKING_INDICATOR_FRAME_COUNT", "render_working_indicator_frame"]
