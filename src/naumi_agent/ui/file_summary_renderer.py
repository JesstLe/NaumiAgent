"""File/diff summary renderers — produce compact, scannable output cards.

Prevents large file writes, long diffs, and big code blocks from flooding
the terminal.  Shared between CLI (ANSI) and TUI (Textual) paths.
"""

from __future__ import annotations

from naumi_agent.ui.tool_summary import ToolCardSummary

# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

_DIFF_PREVIEW_MAX_LINES = 40
_FILE_PREVIEW_MAX_LINES = 15
_OUTPUT_PREVIEW_MAX_CHARS = 500


def format_file_write_summary(summary: ToolCardSummary) -> str:
    """Format a file_write result as a compact, human-readable card.

    Returns ANSI-formatted text suitable for CLI/TUI display.
    """
    lines = []
    path = summary.file_path or summary.primary_arg

    # Header line
    if summary.status == "success":
        lines.append("\033[32m📝 写入成功\033[0m")
    else:
        lines.append("\033[31m📝 写入失败\033[0m")

    # Detail lines
    if path:
        lines.append(f"  \033[2m路径: {path}\033[0m")
    if summary.line_count:
        lines.append(f"  \033[2m行数: {summary.line_count}\033[0m")
    if summary.byte_count:
        lines.append(f"  \033[2m大小: {_format_bytes(summary.byte_count)}\033[0m")
    if summary.duration_ms:
        lines.append(f"  \033[2m耗时: {summary.duration_ms}ms\033[0m")

    if summary.error_summary:
        lines.append(f"  \033[31m错误: {summary.error_summary}\033[0m")

    return "\n".join(lines) + "\n"


def format_file_edit_summary(summary: ToolCardSummary) -> str:
    """Format a file_edit result with diff stats.

    Returns ANSI-formatted text.
    """
    lines = []
    path = summary.file_path or summary.primary_arg

    if summary.status == "success":
        lines.append("\033[32m✏️ 编辑成功\033[0m")
    else:
        lines.append("\033[31m✏️ 编辑失败\033[0m")

    if path:
        lines.append(f"  \033[2m路径: {path}\033[0m")

    if summary.hunk_count:
        parts = [f"{summary.hunk_count} hunk"]
        if summary.additions or summary.deletions:
            parts.append(f"\033[32m+{summary.additions}\033[0m")
            parts.append(f"\033[31m-{summary.deletions}\033[0m")
        lines.append(f"  \033[2m{' · '.join(parts)}\033[0m")

    if summary.duration_ms:
        lines.append(f"  \033[2m耗时: {summary.duration_ms}ms\033[0m")

    if summary.error_summary:
        lines.append(f"  \033[31m错误: {summary.error_summary}\033[0m")

    return "\n".join(lines) + "\n"


def format_file_read_summary(summary: ToolCardSummary) -> str:
    """Format a file_read result.

    Returns ANSI-formatted text.
    """
    lines = []
    path = summary.file_path or summary.primary_arg

    lines.append("\033[36m📖 读取完成\033[0m")
    if path:
        lines.append(f"  \033[2m路径: {path}\033[0m")
    if summary.line_count:
        lines.append(f"  \033[2m行数: {summary.line_count}\033[0m")
    if summary.duration_ms:
        lines.append(f"  \033[2m耗时: {summary.duration_ms}ms\033[0m")

    return "\n".join(lines) + "\n"


def format_tool_output_preview(
    tool_name: str,
    content: str,
    *,
    max_lines: int = _OUTPUT_PREVIEW_MAX_CHARS // 40,
) -> str:
    """Truncate generic tool output for display.

    Returns ANSI-formatted preview text with truncation notice.
    """
    if not content:
        return ""

    raw_lines = content.splitlines()

    # Detect diff content
    if tool_name in ("file_edit",) and _looks_like_diff(raw_lines[:12]):
        return _format_diff_preview(raw_lines)

    # Generic: show first N lines
    if len(raw_lines) <= max_lines:
        return content + "\n" if not content.endswith("\n") else content

    preview = "\n".join(raw_lines[:max_lines])
    hidden = len(raw_lines) - max_lines
    return f"{preview}\n\033[2m... 还有 {hidden} 行输出未展示\033[0m\n"


def _looks_like_diff(sample_lines: list[str]) -> bool:
    has_markers = any(
        line.startswith(("---", "+++", "@@")) for line in sample_lines
    )
    has_changes = any(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in sample_lines
    )
    return has_markers and has_changes


def _format_diff_preview(raw_lines: list[str]) -> str:
    """Format diff lines with color and truncation."""
    if len(raw_lines) <= _DIFF_PREVIEW_MAX_LINES:
        return _color_diff(raw_lines)

    preview = raw_lines[:_DIFF_PREVIEW_MAX_LINES]
    hidden = len(raw_lines) - _DIFF_PREVIEW_MAX_LINES
    colored = _color_diff(preview)
    return (
        f"{colored}"
        f"\033[2m... 还有 {hidden} 行 diff 未展示\033[0m\n"
    )


def _color_diff(lines: list[str]) -> str:
    """Apply ANSI colors to unified diff lines."""
    out: list[str] = []
    for line in lines:
        if line.startswith("+++"):
            out.append(f"\033[32m{line}\033[0m")
        elif line.startswith("---"):
            out.append(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            out.append(f"\033[36m{line}\033[0m")
        elif line.startswith("+"):
            out.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            out.append(f"\033[31m{line}\033[0m")
        else:
            out.append(f"\033[2m{line}\033[0m")
    return "\n".join(out) + "\n"


def _format_bytes(count: int) -> str:
    if count < 1024:
        return f"{count}B"
    return f"{count / 1024:.1f}KB"


def render_tool_summary_card(summary: ToolCardSummary) -> str:
    """Dispatch to the right summary renderer based on tool name.

    Returns ANSI-formatted text, or empty string if no summary available.
    """
    if summary.tool_name == "file_write":
        return format_file_write_summary(summary)
    if summary.tool_name == "file_edit":
        return format_file_edit_summary(summary)
    if summary.tool_name == "file_read":
        return format_file_read_summary(summary)
    # Generic: just show output/error summary
    if summary.output_summary:
        return f"\033[2m{summary.output_summary}\033[0m\n"
    if summary.error_summary:
        return f"\033[31m{summary.error_summary}\033[0m\n"
    return ""
