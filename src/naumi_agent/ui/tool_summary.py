"""Tool-specific summary extractors — produce card-ready info per tool type.

Each function takes raw tool arguments and/or result data and returns a
compact, human-readable summary for display in tool cards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ToolCardSummary:
    """Unified summary for a tool card display."""

    tool_name: str = ""
    icon: str = "⚙️"
    primary_arg: str = ""  # key param shown in card title
    status: str = ""  # lifecycle state
    duration_ms: int = 0
    output_summary: str = ""
    error_summary: str = ""
    extra_lines: tuple[str, ...] = ()  # additional detail lines

    # Tool-specific fields
    file_path: str = ""
    line_count: int = 0
    byte_count: int = 0
    command: str = ""
    exit_code: int = 0
    language: str = ""
    query: str = ""
    url: str = ""
    result_count: int = 0
    agent_name: str = ""
    todo_done: int = 0
    todo_total: int = 0
    additions: int = 0
    deletions: int = 0
    hunk_count: int = 0


# Icon mapping for known tools
_TOOL_ICONS: dict[str, str] = {
    "file_read": "📖",
    "file_write": "📝",
    "file_edit": "✏️",
    "bash_run": "🖥️",
    "code_execute": "⌨️",
    "web_search": "🔍",
    "web_fetch": "🌐",
    "memory_store": "💾",
    "memory_recall": "🧠",
    "delegate_task": "👥",
    "spawn_agent": "🚀",
    "destroy_agent": "🗑️",
    "list_agents": "📋",
    "task_create": "📌",
    "task_update": "🔄",
    "task_list": "📋",
    "task_delete": "🗑️",
    "todo_write": "📋",
    "background_run": "⏱️",
    "background_status": "⏱️",
    "background_list": "📋",
    "background_cancel": "⏹️",
    "background_read_output": "📄",
    "schedule_create": "⏰",
    "schedule_list": "📋",
    "schedule_cancel": "⏹️",
    "worktree_create": "🌿",
    "worktree_status": "🌿",
    "analysis_chaos": "⚡",
    "analysis_scale": "🌊",
    "analysis_state": "☁️",
    "analysis_vibe": "🚀",
    "analysis_eval": "🧪",
    "analysis_page": "💾",
    "analysis_heal": "🏥",
}


def _get_icon(tool_name: str) -> str:
    return _TOOL_ICONS.get(tool_name, "⚙️")


def _parse_args(args_raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort parse of tool arguments."""
    if not args_raw:
        return {}
    if isinstance(args_raw, dict):
        return args_raw
    try:
        return json.loads(args_raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _short_path(path: str, *, max_len: int = 50) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def _format_bytes(count: int) -> str:
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f}KB"
    return f"{count / (1024 * 1024):.1f}MB"


def _extract_content_stats(content: str | None) -> tuple[int, int]:
    """Return (line_count, byte_count) for a string."""
    if not content:
        return 0, 0
    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    return lines, len(content)


def _count_diff_stats(diff_text: str) -> tuple[int, int, int]:
    """Count additions, deletions, and hunk count from unified diff."""
    additions = 0
    deletions = 0
    hunks = 0
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions, hunks


# ---------------------------------------------------------------------------
# Per-tool summary builders
# ---------------------------------------------------------------------------


def summarize_tool_start(
    tool_name: str,
    args_raw: str | dict[str, Any] | None,
) -> ToolCardSummary:
    """Build a summary when a tool execution starts."""
    args = _parse_args(args_raw)
    icon = _get_icon(tool_name)

    # Extract key argument for card title
    primary = ""
    for key in (
        "path", "file_path", "target_path", "filename",
        "command", "query", "url", "task", "description", "goal",
    ):
        val = args.get(key)
        if val:
            primary = str(val)
            if len(primary) > 50:
                primary = primary[:47] + "…"
            break

    return ToolCardSummary(
        tool_name=tool_name,
        icon=icon,
        primary_arg=primary,
        status="running",
    )


def summarize_tool_result(
    tool_name: str,
    status: str,
    duration_ms: int,
    content: str | None,
    args_raw: str | dict[str, Any] | None = None,
    error: str | None = None,
) -> ToolCardSummary:
    """Build a summary when a tool execution finishes."""
    args = _parse_args(args_raw)
    icon = _get_icon(tool_name)

    primary = ""
    for key in (
        "path", "file_path", "target_path", "filename",
        "command", "query", "url",
    ):
        val = args.get(key)
        if val:
            primary = str(val)
            if len(primary) > 50:
                primary = primary[:47] + "…"
            break

    base = ToolCardSummary(
        tool_name=tool_name,
        icon=icon,
        primary_arg=primary,
        status=status,
        duration_ms=duration_ms,
        error_summary=(error[:200] if error else ""),
    )

    # Tool-specific enrichment
    if tool_name in ("file_write",) and content is not None:
        lines, size = _extract_content_stats(
            args.get("content", "") or content
        )
        return replace(
            base,
            file_path=args.get("file_path", args.get("path", primary)),
            line_count=lines,
            byte_count=size,
            output_summary=(
                f"写入 {lines} 行 / {_format_bytes(size)}"
                if lines
                else "写入完成"
            ),
        )

    if tool_name in ("file_edit", "file_read"):
        path = args.get("file_path", args.get("path", primary))
        if content and ("+++" in content or "---" in content):
            additions, deletions, hunks = _count_diff_stats(content)
            return replace(
                base,
                file_path=path,
                additions=additions,
                deletions=deletions,
                hunk_count=hunks,
                output_summary=(
                    f"{hunks} hunk, +{additions}/-{deletions}"
                    if hunks
                    else "编辑完成"
                ),
            )
        if content:
            lines, _ = _extract_content_stats(content)
            return replace(
                base,
                file_path=path,
                line_count=lines,
                output_summary=f"读取 {lines} 行" if lines else "读取完成",
            )

    if tool_name == "bash_run" and content is not None:
        cmd = args.get("command", "")
        lines = content.splitlines()
        exit_code = 0 if status == "success" else 1
        for line in lines[-3:]:
            if "exit code" in line.lower():
                try:
                    exit_code = int(line.strip().split()[-1])
                except (ValueError, IndexError):
                    pass
        preview = lines[-5:] if len(lines) > 5 else lines
        return replace(
            base,
            command=cmd,
            exit_code=exit_code,
            output_summary="\n".join(preview)[:300],
        )

    if tool_name == "todo_write":
        done = 0
        total = 0
        todos = args.get("todos", [])
        if isinstance(todos, list):
            total = len(todos)
            done = sum(
                1 for t in todos
                if isinstance(t, dict) and t.get("status") == "completed"
            )
        return replace(
            base,
            todo_done=done,
            todo_total=total,
            output_summary=f"{done}/{total} 完成" if total else "更新 todo",
        )

    if tool_name in ("web_search",):
        query = args.get("query", "")
        result_count = 0
        if content:
            for line in content.splitlines()[:20]:
                stripped = line.strip()
                if stripped and (stripped[0].isdigit() or stripped.startswith("-")):
                    result_count += 1
        return replace(
            base,
            query=query,
            result_count=result_count,
            output_summary=(
                f"找到 {result_count} 条结果"
                if result_count
                else "搜索完成"
            ),
        )

    if tool_name in ("web_fetch",):
        url = args.get("url", "")
        lines, size = _extract_content_stats(content)
        return replace(
            base,
            url=url,
            line_count=lines,
            byte_count=size,
            output_summary=(
                f"获取 {lines} 行 / {_format_bytes(size)}"
                if lines
                else "获取完成"
            ),
        )

    # Generic fallback
    if content:
        lines, _ = _extract_content_stats(content)
        output_summary = (
            f"{lines} 行输出" if lines > 5 else content[:200]
        )
        return replace(base, output_summary=output_summary)

    return base



def format_card_title(summary: ToolCardSummary) -> str:
    """Format the one-line card title for display."""
    parts = [f"{summary.icon} {summary.tool_name}"]
    if summary.primary_arg:
        parts.append(summary.primary_arg)
    if summary.status:
        status_icons = {
            "running": "⏳",
            "success": "✓",
            "error": "✗",
            "failed": "✗",
            "skipped": "↷",
            "aborted": "!",
            "blocked": "🚫",
            "preparing": "⏳",
            "awaiting_permission": "🔒",
        }
        icon = status_icons.get(summary.status, "•")
        parts.append(icon)
    return " ".join(parts)


def format_card_detail(summary: ToolCardSummary) -> str:
    """Format the detail line for display (second line of card)."""
    if summary.output_summary:
        return summary.output_summary
    if summary.error_summary:
        return summary.error_summary
    return ""
