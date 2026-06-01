"""Helpers for showing readable code excerpts in terminal UIs."""

from __future__ import annotations

DEFAULT_CODE_BLOCK_MAX_LINES = 80


def excerpt_markdown_code_blocks(
    markdown: str,
    *,
    max_lines: int = DEFAULT_CODE_BLOCK_MAX_LINES,
) -> str:
    """Return Markdown with long fenced code blocks replaced by short excerpts."""
    if max_lines <= 0 or "```" not in markdown:
        return markdown

    lines = markdown.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _is_fence(line):
            out.append(line)
            index += 1
            continue

        header = line
        body: list[str] = []
        index += 1
        closed = False
        closing = ""
        while index < len(lines):
            candidate = lines[index]
            if _is_fence(candidate):
                closing = candidate
                closed = True
                index += 1
                break
            body.append(candidate)
            index += 1

        if len(body) <= max_lines:
            out.append(header)
            out.extend(body)
            if closed:
                out.append(closing)
            continue

        hidden = len(body) - max_lines
        out.append(header)
        out.extend(body[:max_lines])
        if body[:max_lines] and not body[:max_lines][-1].endswith("\n"):
            out.append("\n")
        out.append(closing if closed else "```\n")
        out.append(
            f"\n_已隐藏 {hidden} 行代码；仅展示前 {max_lines} 行摘录。_\n"
        )

    return "".join(out)


def _is_fence(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```")
