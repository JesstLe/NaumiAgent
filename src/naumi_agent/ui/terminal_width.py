"""Unicode terminal-cell measurement shared by Python terminal surfaces.

The contract is intentionally grapheme-aware: truncation and wrapping never split
combining sequences, emoji modifiers, regional flags, keycaps, or ZWJ emoji.
"""

from __future__ import annotations

from collections.abc import Iterator

import regex
from wcwidth import wcswidth

_ANSI_PATTERN = regex.compile(
    r"(?:\x1b\][\s\S]*?(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~])"
)
_GRAPHEME_PATTERN = regex.compile(r"\X")


def _text(value: object) -> str:
    return "" if value is None else str(value)


def strip_terminal_sequences(value: object) -> str:
    """Remove CSI/OSC terminal controls before measuring untrusted text."""
    return _ANSI_PATTERN.sub("", _text(value))


def iter_graphemes(value: object) -> Iterator[str]:
    """Yield Unicode extended grapheme clusters from plain text."""
    yield from _GRAPHEME_PATTERN.findall(_text(value))


def grapheme_width(grapheme: str) -> int:
    """Return the bounded terminal width of one extended grapheme cluster."""
    measured = wcswidth(grapheme)
    return max(0, measured)


def _graphemes_with_width(value: object) -> Iterator[tuple[str, int]]:
    skip_first_code_point = False
    for grapheme in iter_graphemes(value):
        measured = grapheme
        if skip_first_code_point:
            measured = grapheme[1:]
            skip_first_code_point = False
        yield grapheme, grapheme_width(measured)
        if grapheme.endswith("\u200d"):
            skip_first_code_point = True


def display_width(value: object) -> int:
    """Measure visible terminal cells after removing ANSI/OSC controls."""
    plain = strip_terminal_sequences(value)
    return sum(width for _grapheme, width in _graphemes_with_width(plain))


def _prefix_within_width(value: str, width: int) -> str:
    output: list[str] = []
    used = 0
    for grapheme, next_width in _graphemes_with_width(value):
        if used + next_width > width:
            break
        output.append(grapheme)
        used += next_width
    return "".join(output)


def truncate_to_width(value: object, width: int, *, marker: str = "…") -> str:
    """Truncate plain text to at most ``width`` cells without splitting graphemes."""
    safe_width = max(0, int(width))
    if safe_width == 0:
        return ""

    text = strip_terminal_sequences(value)
    if display_width(text) <= safe_width:
        return text

    safe_marker = _prefix_within_width(strip_terminal_sequences(marker), safe_width)
    marker_width = display_width(safe_marker)
    target = max(0, safe_width - marker_width)
    return f"{_prefix_within_width(text, target)}{safe_marker}"


def pad_or_truncate(value: object, width: int, *, marker: str = "…") -> str:
    """Return plain text occupying exactly ``width`` terminal cells."""
    safe_width = max(0, int(width))
    clipped = truncate_to_width(value, safe_width, marker=marker)
    return f"{clipped}{' ' * max(0, safe_width - display_width(clipped))}"


def wrap_to_width(
    value: object,
    width: int,
    *,
    overflow_replacement: str = "…",
) -> list[str]:
    """Wrap plain text at grapheme boundaries with bounded narrow-column fallback."""
    safe_width = max(1, int(width))
    text = strip_terminal_sequences(value)
    if not text:
        return [""]

    replacement = _prefix_within_width(overflow_replacement, safe_width)
    lines: list[str] = []
    current: list[str] = []
    current_width = 0

    for grapheme, next_width in _graphemes_with_width(text):
        if next_width > safe_width:
            if current:
                lines.append("".join(current))
                current = []
                current_width = 0
            lines.append(replacement)
            continue
        if current and current_width + next_width > safe_width:
            lines.append("".join(current))
            current = []
            current_width = 0
        current.append(grapheme)
        current_width += next_width

    if current or not lines:
        lines.append("".join(current))
    return lines


__all__ = [
    "display_width",
    "grapheme_width",
    "iter_graphemes",
    "pad_or_truncate",
    "strip_terminal_sequences",
    "truncate_to_width",
    "wrap_to_width",
]
