"""Full-screen CLI layout with fixed input bar at the bottom."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style

from naumi_agent.cli_completer import SlashCommandCompleter


class _OutputWindow(Window):
    """Auto-scrolling output window — scrolls to bottom on new content, allows manual scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.auto_scroll = True

    def _scroll_up(self) -> None:
        if self.auto_scroll:
            # Capture current bottom position before switching to manual
            self.vertical_scroll = max(0, self.max_scroll_y)
            self.auto_scroll = False
        else:
            self.vertical_scroll = max(0, self.vertical_scroll - 1)

    def _scroll_down(self) -> None:
        if self.auto_scroll:
            return
        self.vertical_scroll += 1
        if self.vertical_scroll >= self.max_scroll_y:
            self.auto_scroll = True

    def scroll_to_bottom(self) -> None:
        self.auto_scroll = True
        self.vertical_scroll = 99999

    def ensure_at_bottom(self) -> None:
        """滚动到底部（仅在 auto_scroll 开启时）."""
        if self.auto_scroll:
            self.vertical_scroll = 99999

_STYLE = Style.from_dict(
    {
        "border": "#444444",
        "border-active": "#00aa00",
        "prompt": "#00aa00 bold",
        "processing": "#888888",
    }
)


def _border_line(cols: int, left: str, mid: str, right: str, cls: str = "border") -> list:
    return [
        ("class:" + cls, f" {left}"),
        ("class:" + cls, mid * (cols - 2)),
        ("class:" + cls, right),
    ]


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
                self._output_win.auto_scroll = False
                for _ in range(10):
                    self._output_win._scroll_up()
                self._invalidate()

        @self._kb.add("pagedown")
        def _page_down(event: Any) -> None:
            if self._output_win:
                for _ in range(10):
                    self._output_win._scroll_down()
                self._invalidate()

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
        self._invalidate()

    def clear_output(self) -> None:
        self._output.clear()
        self._live.clear()
        self._invalidate()

    def _render_output(self) -> list:
        result: list = []
        for text in self._output:
            result.extend(ANSI(text).__pt_formatted_text__())
        for text in self._live:
            result.extend(ANSI(text).__pt_formatted_text__())
        return result

    def _build_app(self) -> Application:
        cols = shutil.get_terminal_size().columns

        self._output_win = _OutputWindow(
            content=FormattedTextControl(self._render_output),
            wrap_lines=True,
            always_hide_cursor=True,
            height=Dimension(min=1, weight=1),
        )
        self._output_win.scroll_to_bottom()

        input_win = Window(
            height=1,
            content=BufferControl(
                buffer=self._input_buf,
                focus_on_click=True,
            ),
            get_line_prefix=lambda *_: (
                FormattedText([("class:processing", " ⏳ ")])
                if self._processing
                else FormattedText([("class:prompt", " ❯ ")])
            ),
        )

        border_cls = "border" if not self._processing else "border-active"
        border_top = Window(
            height=1,
            content=FormattedTextControl(
                lambda: _border_line(cols, "╭", "─", "╮", border_cls),
            ),
        )

        border_bot = Window(
            height=1,
            content=FormattedTextControl(
                lambda: _border_line(cols, "╰", "─", "╯", border_cls),
            ),
        )

        body = HSplit([self._output_win, border_top, input_win, border_bot])
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
        )

    async def run(self) -> None:
        self._app = self._build_app()
        await self._app.run_async()
