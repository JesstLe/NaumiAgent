"""Virtualized CLI transcript storage.

The full-screen CLI receives output in chunks, but prompt_toolkit ultimately
asks the output control for individual lines.  This module keeps an indexed
line view of the transcript so rendering can answer those line requests lazily
instead of rebuilding one giant ANSI fragment list on every refresh.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.layout.controls import UIContent, UIControl


@dataclass(frozen=True)
class HistoryStats:
    """Small observable snapshot for tests and debug traces."""

    output_chunks: int
    live_chunks: int
    line_count: int
    revision: int
    cached_lines: int


class VirtualizedCLIHistory:
    """Append-only output history with lazy ANSI line rendering."""

    def __init__(self, *, max_line_cache: int = 2_000) -> None:
        self._output: list[str] = []
        self._live: list[str] = []
        self._lines: list[str] = [""]
        self._max_line_cache = max(1, max_line_cache)
        self._revision = 0
        self._line_cache_revision = 0
        self._line_cache: tuple[str, ...] = ()
        self._formatted_cache: OrderedDict[tuple[int, int, bool, int], StyleAndTextTuples] = (
            OrderedDict()
        )

    @property
    def output_chunks(self) -> int:
        return len(self._output)

    @property
    def live_chunks(self) -> int:
        return len(self._live)

    @property
    def output_text_chunks(self) -> tuple[str, ...]:
        return tuple(self._output)

    @property
    def live_text_chunks(self) -> tuple[str, ...]:
        return tuple(self._live)

    @property
    def revision(self) -> int:
        return self._revision

    def append_output(self, ansi_text: str) -> None:
        """Append finalized transcript text."""
        if not ansi_text:
            return
        self._output.append(ansi_text)
        self._append_to_line_index(ansi_text)
        self._mark_changed()

    def append_live(self, ansi_text: str) -> None:
        """Append transient live text for the current turn."""
        if not ansi_text:
            return
        self._live.append(ansi_text)
        self._append_to_line_index(ansi_text)
        self._mark_changed()

    def finalize_live(self) -> int:
        """Move live chunks into the permanent transcript."""
        count = len(self._live)
        if not count:
            return 0
        self._output.extend(self._live)
        self._live.clear()
        return count

    def clear_live(self) -> int:
        """Drop transient live chunks without touching finalized output."""
        count = len(self._live)
        if not count:
            return 0
        self._live.clear()
        self._rebuild_line_index_from_output()
        self._mark_changed()
        return count

    def clear(self) -> None:
        """Drop all transcript state and cached render lines."""
        self._output.clear()
        self._live.clear()
        self._lines = [""]
        self._mark_changed()

    def transcript(self) -> str:
        """Return the full transcript text, including live output."""
        return "".join([*self._output, *self._live])

    def line_count(self) -> int:
        """Return the number of logical transcript lines."""
        return len(self._visible_lines())

    def get_line(
        self,
        lineno: int,
        *,
        width: int,
        pin_cursor: bool = False,
    ) -> StyleAndTextTuples:
        """Return one formatted transcript line.

        ``width`` is part of the cache key because prompt_toolkit may include
        width-sensitive line prefixes or style calculations in future renderers.
        Keeping it here avoids stale lines after terminal resize.
        """
        lines = self._visible_lines()
        if lineno < 0 or lineno >= len(lines):
            return []

        safe_width = max(1, width)
        key = (self._revision, lineno, pin_cursor, safe_width)
        cached = self._formatted_cache.get(key)
        if cached is not None:
            self._formatted_cache.move_to_end(key)
            return cached

        raw = lines[lineno]
        fragments = list(ANSI(raw).__pt_formatted_text__())
        if pin_cursor:
            fragments.append(("[SetCursorPosition]", ""))
        self._formatted_cache[key] = fragments
        if len(self._formatted_cache) > self._max_line_cache:
            self._formatted_cache.popitem(last=False)
        return fragments

    def visible_text(
        self,
        *,
        width: int,
        height: int,
        scroll_offset: int = 0,
    ) -> str:
        """Return a plain viewport slice for replay/debug tests.

        ``scroll_offset`` is counted from the bottom.  ``0`` means the newest
        lines are visible, matching CLI auto-scroll behavior.
        """
        del width  # Logical-line viewport; wrapping is handled by the window.
        if height <= 0:
            return ""
        lines = self._visible_lines()
        if not lines:
            return ""
        offset = max(0, scroll_offset)
        end = max(0, len(lines) - offset)
        start = max(0, end - height)
        return "\n".join(lines[start:end])

    def stats(self) -> HistoryStats:
        """Return cache and transcript counters."""
        return HistoryStats(
            output_chunks=len(self._output),
            live_chunks=len(self._live),
            line_count=self.line_count(),
            revision=self._revision,
            cached_lines=len(self._formatted_cache),
        )

    def _visible_lines(self) -> tuple[str, ...]:
        if self._line_cache_revision == self._revision:
            return self._line_cache
        if len(self._lines) > 1 and self._lines[-1] == "":
            lines = tuple(self._lines[:-1])
        else:
            lines = tuple(self._lines)
        if not lines:
            lines = ("",)
        self._line_cache = lines
        self._line_cache_revision = self._revision
        self._formatted_cache.clear()
        return lines

    def _mark_changed(self) -> None:
        self._revision += 1
        self._line_cache_revision = -1
        self._line_cache = ()
        self._formatted_cache.clear()

    def _append_to_line_index(self, text: str) -> None:
        parts = text.split("\n")
        self._lines[-1] += parts[0]
        for part in parts[1:]:
            self._lines.append(part)

    def _rebuild_line_index_from_output(self) -> None:
        self._lines = [""]
        for chunk in self._output:
            self._append_to_line_index(chunk)


class VirtualizedHistoryControl(UIControl):
    """prompt_toolkit UIControl backed by :class:`VirtualizedCLIHistory`."""

    def __init__(
        self,
        history: VirtualizedCLIHistory,
        *,
        should_pin_cursor: Any,
    ) -> None:
        self._history = history
        self._should_pin_cursor = should_pin_cursor

    def create_content(self, width: int, height: int) -> UIContent:
        del height
        line_count = self._history.line_count()
        should_pin = bool(self._should_pin_cursor())
        cursor_position = None
        if should_pin:
            last_line = self._history.get_line(line_count - 1, width=width)
            cursor_position = Point(
                x=fragment_list_width(last_line),
                y=line_count - 1,
            )

        def _get_line(lineno: int) -> StyleAndTextTuples:
            pin = should_pin and lineno == line_count - 1
            return self._history.get_line(lineno, width=width, pin_cursor=pin)

        return UIContent(
            get_line=_get_line,
            line_count=max(1, line_count),
            cursor_position=cursor_position,
            show_cursor=False,
        )
