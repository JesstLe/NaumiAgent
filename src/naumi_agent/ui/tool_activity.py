"""Compact rendering helpers for streamed tool preparation events."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _format_bytes(chars: int) -> str:
    if chars < 1024:
        return f"{chars}B"
    return f"{chars / 1024:.1f}KB"


def _short_path(path: str, *, max_chars: int = 58) -> str:
    if not path:
        return ""
    if len(path) <= max_chars:
        return path
    try:
        name = Path(path).name
    except Exception:
        name = path.rsplit("/", 1)[-1]
    if len(name) + 3 <= max_chars:
        return f".../{name}"
    return "..." + path[-max(0, max_chars - 3):]


def format_tool_prepare_status(data: dict[str, Any]) -> str:
    """Format a one-line status for a tool call whose arguments are streaming."""
    name = str(data.get("name") or "tool")
    parts = [f"准备 {name}"]
    path = _short_path(str(data.get("path") or ""))
    if path:
        parts.append(path)
    content_lines = int(data.get("content_lines") or 0)
    content_chars = int(data.get("content_chars") or 0)
    if content_lines and content_chars:
        parts.append(f"内容 {content_lines} 行 / {_format_bytes(content_chars)}")
    else:
        argument_chars = int(data.get("argument_chars") or 0)
        if argument_chars:
            parts.append(f"参数 {_format_bytes(argument_chars)}")
    elapsed_ms = int(data.get("elapsed_ms") or 0)
    if elapsed_ms >= 1000:
        parts.append(f"{elapsed_ms / 1000:.1f}s")
    return " · ".join(parts)
