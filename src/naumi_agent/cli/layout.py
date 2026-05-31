"""Full-screen CLI layout with fixed input bar at the bottom."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import (
    BufferControl,
    FormattedTextControl,
    UIContent,
    UIControl,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from naumi_agent.cli_completer import SlashCommandCompleter
from naumi_agent.clipboard import copy_or_save_transcript


class _OutputWindow(Window):
    """Auto-scrolling output window — scrolls to bottom on new content, allows manual scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.auto_scroll = True

    def _scroll_up(self) -> None:
        if self.auto_scroll:
            # Capture current bottom position before switching to manual
            if self.render_info is not None:
                self.vertical_scroll = self.render_info.vertical_scroll
            elif self.vertical_scroll > 10_000:
                self.vertical_scroll = 0
            self.auto_scroll = False

        if self.vertical_scroll_2 > 0:
            self.vertical_scroll_2 -= 1
        elif self.vertical_scroll > 0:
            self.vertical_scroll -= 1
            if self.render_info is not None:
                self.vertical_scroll_2 = max(
                    0,
                    self.render_info.get_height_for_line(self.vertical_scroll) - 1,
                )

    def _scroll_down(self) -> None:
        if self.auto_scroll:
            return
        if self.render_info is not None and self.render_info.bottom_visible:
            self.auto_scroll = True
            return
        if self.render_info is None:
            self.vertical_scroll += 1
            return

        line_height = self.render_info.get_height_for_line(self.vertical_scroll)
        if self.vertical_scroll_2 < line_height - 1:
            self.vertical_scroll_2 += 1
        else:
            self.vertical_scroll += 1
            self.vertical_scroll_2 = 0

        max_line, max_line_offset = self._bottom_scroll_position(
            self.render_info.ui_content,
            self.render_info.window_width,
            self.render_info.window_height,
        )
        if (self.vertical_scroll, self.vertical_scroll_2) >= (max_line, max_line_offset):
            self.scroll_to_bottom()

    def scroll_to_bottom(self) -> None:
        self.auto_scroll = True
        self.vertical_scroll = 99999

    def ensure_at_bottom(self) -> None:
        """滚动到底部（仅在 auto_scroll 开启时）."""
        if self.auto_scroll:
            self.vertical_scroll = 99999

    def _scroll(self, ui_content: UIContent, width: int, height: int) -> None:
        """Scroll without cursor-snapping when the user is browsing history."""
        if self.auto_scroll:
            super()._scroll(ui_content, width, height)
            return

        self.horizontal_scroll = 0
        if ui_content.line_count <= 0 or width <= 0 or height <= 0:
            self.vertical_scroll = 0
            self.vertical_scroll_2 = 0
            return
        self._clamp_manual_scroll(ui_content, width, height)

    def _clamp_manual_scroll(self, ui_content: UIContent, width: int, height: int) -> None:
        """Keep manual scroll coordinates inside the rendered content."""
        max_line, max_line_offset = self._bottom_scroll_position(ui_content, width, height)
        self.vertical_scroll = max(0, min(self.vertical_scroll, max_line))

        line_height = self._line_height(ui_content, self.vertical_scroll, width)
        max_offset = max(0, line_height - 1)
        if self.vertical_scroll == max_line:
            max_offset = min(max_offset, max_line_offset)
        self.vertical_scroll_2 = max(0, min(self.vertical_scroll_2, max_offset))

    def _bottom_scroll_position(
        self,
        ui_content: UIContent,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        """Return the lowest top-of-window position that still shows content."""
        used_height = 0
        safe_width = max(1, width)
        for lineno in range(ui_content.line_count - 1, -1, -1):
            line_height = self._line_height(ui_content, lineno, safe_width)
            if used_height + line_height > height:
                return lineno, used_height + line_height - height
            used_height += line_height
        return 0, 0

    def _line_height(self, ui_content: UIContent, lineno: int, width: int) -> int:
        if self.wrap_lines():
            return ui_content.get_height_for_line(lineno, width, self.get_line_prefix)
        return 1

_STYLE = Style.from_dict(
    {
        "border": "#444444",
        "border-active": "#00aa00",
        "prompt": "#00aa00 bold",
        "processing": "#888888",
        "status": "#888888",
    }
)


def _border_line(cols: int, left: str, mid: str, right: str, cls: str = "border") -> list:
    safe_cols = max(0, cols)
    if safe_cols == 0:
        return []
    if safe_cols == 1:
        return [("class:" + cls, left)]
    return [("class:" + cls, left + (mid * max(0, safe_cols - 2)) + right)]


def _fit_text_to_width(text: str, cols: int) -> str:
    """Return text padded or truncated to exactly *cols* terminal cells."""
    if cols <= 0:
        return ""
    if get_cwidth(text) <= cols:
        return text + (" " * (cols - get_cwidth(text)))

    marker = "…"
    marker_width = get_cwidth(marker)
    if cols <= marker_width:
        return marker[:cols]

    target = cols - marker_width
    out: list[str] = []
    width = 0
    for char in text:
        char_width = get_cwidth(char)
        if width + char_width > target:
            break
        out.append(char)
        width += char_width
    return "".join(out) + (" " * max(0, target - width)) + marker


class _DynamicLineControl(UIControl):
    """Single-line control that redraws from the current render width."""

    def __init__(self, get_line: Callable[[int], list[tuple[str, str]]]) -> None:
        self._get_line = get_line

    def create_content(self, width: int, height: int) -> UIContent:
        return UIContent(
            get_line=lambda _lineno: self._get_line(width),
            line_count=1,
            show_cursor=False,
        )


class CLIApp:
    """Full-screen CLI: scrollable output + fixed input bar, no screen switching."""

    def __init__(self) -> None:
        self._output: list[str] = []
        self._live: list[str] = []
        self._processing = False
        self._app: Application | None = None
        self._input_buf = Buffer(
            multiline=False,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
        )
        self._kb = KeyBindings()
        self._on_submit: Callable[[str], Awaitable[None]] | None = None
        self._output_win: _OutputWindow | None = None
        self._git_branch: str = ""
        self._git_dirty: bool = False
        self._status_text = "就绪"

        self._last_esc_time = 0.0

        @self._kb.add("enter")
        def _submit(event: Any) -> None:
            if self._processing:
                return
            text = self._input_buf.text.strip()
            if text and self._on_submit:
                self._input_buf.text = ""
                asyncio.ensure_future(self._run_submit(text))

        @self._kb.add("escape")
        def _escape(event: Any) -> None:
            import time

            now = time.monotonic()
            if self._processing and now - self._last_esc_time < 0.5:
                self._processing = False
            self._last_esc_time = now

        @self._kb.add("c-c")
        def _cancel(event: Any) -> None:
            if not self._processing:
                event.app.exit()

        @self._kb.add("c-d")
        def _eof(event: Any) -> None:
            if not self._processing:
                event.app.exit()

        @self._kb.add("pageup")
        def _page_up(event: Any) -> None:
            if self._output_win:
                for _ in range(10):
                    self._output_win._scroll_up()
                self._invalidate()

        @self._kb.add("pagedown")
        def _page_down(event: Any) -> None:
            if self._output_win:
                for _ in range(10):
                    self._output_win._scroll_down()
                self._invalidate()

        @self._kb.add("c-y")
        def _copy_all(event: Any) -> None:
            self.copy_transcript()

    def set_submit_handler(self, handler: Callable[[str], Awaitable[None]]) -> None:
        self._on_submit = handler

    async def _run_submit(self, text: str) -> None:
        self._processing = True
        self._live = []
        if self._output_win:
            self._output_win.scroll_to_bottom()
        self._invalidate()
        try:
            if self._on_submit:
                await self._on_submit(text)
        finally:
            # Any remaining live content not yet finalized
            if self._live:
                self._output.extend(self._live)
                self._live = []
            self._processing = False
            self._invalidate()

    def exit(self) -> None:
        if self._app:
            self._app.exit()

    def _invalidate(self) -> None:
        if self._app:
            self._app.invalidate()

    def append_output(self, ansi_text: str) -> None:
        self._output.append(ansi_text)
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def append_live(self, text: str) -> None:
        self._live.append(text)
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def finalize_live(self) -> None:
        self._output.extend(self._live)
        self._live = []
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def clear_output(self) -> None:
        self._output.clear()
        self._live.clear()
        if self._output_win:
            self._output_win.scroll_to_bottom()
        self._invalidate()

    def reset_output(self) -> None:
        """Replace the transcript view with a fresh scroll state."""
        self.clear_output()

    def set_status(self, text: str) -> None:
        """Update fixed bottom status text without adding it to chat history."""
        self._status_text = text
        self._invalidate()

    def get_transcript(self) -> str:
        """Return the complete visible transcript, including live output."""
        return "".join([*self._output, *self._live])

    def copy_transcript(self) -> str:
        """Copy or save the complete transcript and show a short status line."""
        result = copy_or_save_transcript(
            self.get_transcript(),
            base_dir=Path.cwd() / "data",
            prefix="cli-transcript",
        )
        self.append_output(f"\033[2m{result.message}\033[0m\n")
        return result.message

    def set_git_info(self, branch: str, dirty: bool) -> None:
        """Update git branch shown in the prompt prefix."""
        self._git_branch = branch
        self._git_dirty = dirty
        self._invalidate()

    def _render_output(self) -> list:
        result: list = []
        for text in self._output:
            result.extend(ANSI(text).__pt_formatted_text__())
        for text in self._live:
            result.extend(ANSI(text).__pt_formatted_text__())
        # Pin the cursor to the last line so the Window's scroll algorithm
        # tracks the newest content. Only add when auto_scroll is on —
        # without this guard, manual scroll-up is impossible because the
        # cursor anchor forces the view back to the bottom every render.
        if self._output_win and self._output_win.auto_scroll:
            result.append(("[SetCursorPosition]", ""))
        return result

    def _render_status(self, cols: int) -> FormattedText:
        text = f" {self._status_text}"
        return FormattedText([("class:status", _fit_text_to_width(text, cols))])

    def _build_app(self) -> Application:
        self._output_win = _OutputWindow(
            content=FormattedTextControl(self._render_output),
            wrap_lines=True,
            always_hide_cursor=True,
            height=Dimension(min=1, weight=1),
            right_margins=[ScrollbarMargin(display_arrows=False)],
        )
        self._output_win.scroll_to_bottom()

        def _build_prefix() -> FormattedText:
            parts: list[tuple[str, str]] = []
            if self._git_branch:
                tag = self._git_branch + ("*" if self._git_dirty else "")
                parts.append(("class:border", f" {tag} "))
            if self._processing:
                parts.append(("class:processing", "⏳ "))
            else:
                parts.append(("class:prompt", "❯ "))
            return FormattedText(parts)

        input_win = Window(
            height=1,
            content=BufferControl(
                buffer=self._input_buf,
                focus_on_click=True,
            ),
            get_line_prefix=lambda *_: _build_prefix(),
        )

        border_cls = "border" if not self._processing else "border-active"
        border_top = Window(
            height=1,
            content=_DynamicLineControl(
                lambda width: _border_line(width, "╭", "─", "╮", border_cls),
            ),
        )

        border_bot = Window(
            height=1,
            content=_DynamicLineControl(
                lambda width: _border_line(width, "╰", "─", "╯", border_cls),
            ),
        )

        status_win = Window(
            height=1,
            content=_DynamicLineControl(lambda width: self._render_status(width)),
        )

        body = HSplit([self._output_win, status_win, border_top, input_win, border_bot])
        root = FloatContainer(
            content=body,
            floats=[
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12)),
            ],
        )
        return Application(
            layout=Layout(root, focused_element=input_win),
            key_bindings=self._kb,
            style=_STYLE,
            full_screen=True,
            mouse_support=True,
        )

    async def run(self) -> None:
        self._app = self._build_app()
        await self._app.run_async()
