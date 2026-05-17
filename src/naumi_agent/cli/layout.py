"""Full-screen CLI layout with fixed input bar at the bottom."""

from __future__ import annotations

import shutil
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style

from naumi_agent.cli_completer import SlashCommandCompleter

_STYLE = Style.from_dict(
    {
        "border": "#444444",
        "prompt": "#00aa00 bold",
    }
)


def _border_line(cols: int, left: str, mid: str, right: str) -> list:
    return [
        ("class:border", f" {left}"),
        ("class:border", mid * (cols - 2)),
        ("class:border", right),
    ]


class CLIApp:
    """Full-screen CLI: scrollable output + fixed input bar."""

    def __init__(self) -> None:
        self._output: list[str] = []
        self._kb = KeyBindings()
        self._input_buf = Buffer(
            multiline=False,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
        )

        @self._kb.add("enter")
        def _submit(event: Any) -> None:
            text = self._input_buf.text
            if text.strip():
                self._input_buf.text = ""
                event.app.exit(result=text.strip())

        @self._kb.add("c-c")
        def _cancel(event: Any) -> None:
            event.app.exit(result=None)

        @self._kb.add("c-d")
        def _eof(event: Any) -> None:
            event.app.exit(result=None)

    def _build_app(self) -> Application:
        cols = shutil.get_terminal_size().columns

        output_win = Window(
            content=FormattedTextControl(self._render_output),
            wrap_lines=True,
            always_hide_cursor=True,
            height=Dimension(min=1, weight=1),
        )

        input_win = Window(
            height=1,
            content=BufferControl(
                buffer=self._input_buf,
                focus_on_click=True,
            ),
            get_line_prefix=lambda *_: FormattedText(
                [("class:prompt", " ❯ ")],
            ),
        )

        border_top = Window(
            height=1,
            content=FormattedTextControl(
                lambda: _border_line(cols, "╭", "─", "╮"),
            ),
        )

        border_bot = Window(
            height=1,
            content=FormattedTextControl(
                lambda: _border_line(cols, "╰", "─", "╯"),
            ),
        )

        root = HSplit([output_win, border_top, input_win, border_bot])
        return Application(
            layout=Layout(root, focused_element=input_win),
            key_bindings=self._kb,
            style=_STYLE,
            full_screen=True,
        )

    def _render_output(self) -> list:
        result: list = []
        for text in self._output:
            result.extend(ANSI(text).formatted_text)
        return result

    def add_output(self, ansi_text: str) -> None:
        self._output.append(ansi_text)

    async def get_input(self) -> str | None:
        app = self._build_app()
        result = await app.run_async()
        return result
