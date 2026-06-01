"""Full-screen CLI layout with fixed input bar at the bottom."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import (
    BufferControl,
    UIContent,
    UIControl,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.utils import get_cwidth

from naumi_agent.cli.history import VirtualizedCLIHistory, VirtualizedHistoryControl
from naumi_agent.cli_completer import SlashCommandCompleter
from naumi_agent.clipboard import copy_or_save_transcript
from naumi_agent.ui.keybindings import (
    KeybindingAction,
    KeybindingSet,
    build_keybindings,
    render_keybinding_help,
)
from naumi_agent.ui.theme import UIStyleConfig, build_ui_style_config


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

    def __init__(
        self,
        debug_trace: Any = None,
        keybindings: KeybindingSet | None = None,
        style_config: UIStyleConfig | None = None,
    ) -> None:
        self._history = VirtualizedCLIHistory()
        self._processing = False
        self._debug_trace = debug_trace
        self._keybindings = keybindings or build_keybindings()
        self._style_config = style_config or build_ui_style_config()
        self._app: Application | None = None
        self._input_buf = Buffer(
            multiline=False,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
        )
        self._kb = KeyBindings()
        self._on_submit: Callable[[str], Awaitable[None]] | None = None
        self._on_mode_toggle: Callable[[], str] | None = None
        self._output_win: _OutputWindow | None = None
        self._git_branch: str = ""
        self._git_dirty: bool = False
        self._mode_text = "default"
        self._status_text = "就绪"
        self._todo_text = ""
        self._activity_text = ""
        self._pending_permission: asyncio.Future[str] | None = None

        self._last_esc_time = 0.0
        permission_pending = Condition(lambda: self._pending_permission is not None)
        no_permission_pending = Condition(lambda: self._pending_permission is None)

        def _bind(
            action: KeybindingAction,
            handler: Callable[[Any], None],
            *,
            filter: Condition | None = None,
        ) -> None:
            for key in self._keybindings.keys_for(action, interface="cli"):
                if filter is None:
                    self._kb.add(key)(handler)
                else:
                    self._kb.add(key, filter=filter)(handler)

        def _permission_allow(event: Any) -> None:
            self._resolve_pending_permission("allow")

        _bind(
            KeybindingAction.PERMISSION_ALLOW,
            _permission_allow,
            filter=permission_pending,
        )

        def _permission_deny(event: Any) -> None:
            self._resolve_pending_permission("deny")

        _bind(
            KeybindingAction.PERMISSION_DENY,
            _permission_deny,
            filter=permission_pending,
        )

        def _permission_bypass(event: Any) -> None:
            self._resolve_pending_permission("bypass")

        _bind(
            KeybindingAction.PERMISSION_BYPASS,
            _permission_bypass,
            filter=permission_pending,
        )

        def _toggle_runtime_mode(event: Any) -> None:
            if self._on_mode_toggle is None:
                return
            mode = self._on_mode_toggle()
            self.set_mode_status(mode)
            self.set_status(f"已切换模式: {mode}")

        _bind(
            KeybindingAction.MODE_CYCLE,
            _toggle_runtime_mode,
            filter=no_permission_pending,
        )

        def _submit(event: Any) -> None:
            if self._processing:
                return
            text = self._input_buf.text.strip()
            if text and self._on_submit:
                self._input_buf.text = ""
                asyncio.ensure_future(self._run_submit(text))

        _bind(KeybindingAction.SUBMIT, _submit)

        def _escape(event: Any) -> None:
            import time

            now = time.monotonic()
            if self._processing and now - self._last_esc_time < 0.5:
                self._processing = False
            self._last_esc_time = now

        _bind(KeybindingAction.INTERRUPT, _escape)

        def _exit(event: Any) -> None:
            if not self._processing:
                event.app.exit()

        _bind(KeybindingAction.EXIT, _exit)

        def _page_up(event: Any) -> None:
            if self._output_win:
                for _ in range(10):
                    self._output_win._scroll_up()
                self._invalidate()

        _bind(KeybindingAction.SCROLL_PAGE_UP, _page_up)

        def _page_down(event: Any) -> None:
            if self._output_win:
                for _ in range(10):
                    self._output_win._scroll_down()
                self._invalidate()

        _bind(KeybindingAction.SCROLL_PAGE_DOWN, _page_down)

        def _copy_all(event: Any) -> None:
            self.copy_transcript()

        _bind(KeybindingAction.COPY_TRANSCRIPT, _copy_all)

    def set_submit_handler(self, handler: Callable[[str], Awaitable[None]]) -> None:
        self._on_submit = handler

    def set_mode_toggle_handler(self, handler: Callable[[], str]) -> None:
        self._on_mode_toggle = handler

    async def _run_submit(self, text: str) -> None:
        self._processing = True
        self._history.clear_live()
        self._activity_text = ""
        self._debug_input("cli.input", text)
        if self._output_win:
            self._output_win.scroll_to_bottom()
        self._invalidate()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            if self._on_submit:
                with (
                    contextlib.redirect_stdout(stdout_buf),
                    contextlib.redirect_stderr(stderr_buf),
                ):
                    await self._on_submit(text)
        except Exception:
            self._append_captured_streams(stdout_buf, stderr_buf)
            self._debug_exception("cli.submit", traceback.format_exc())
            self.append_output(self._format_submit_exception())
        finally:
            self._append_captured_streams(stdout_buf, stderr_buf)
            # Any remaining live content not yet finalized
            self._history.finalize_live()
            self._activity_text = ""
            self._processing = False
            self._invalidate()

    def _append_captured_streams(
        self,
        stdout_buf: io.StringIO,
        stderr_buf: io.StringIO,
    ) -> None:
        text = stdout_buf.getvalue() + stderr_buf.getvalue()
        if not text:
            return
        self._history.append_output(text)
        self._debug_output("cli.captured_stream", text)
        stdout_buf.seek(0)
        stdout_buf.truncate(0)
        stderr_buf.seek(0)
        stderr_buf.truncate(0)
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def _format_submit_exception(self) -> str:
        trace = traceback.format_exc().rstrip()
        last_line = trace.rsplit("\n", 1)[-1] if trace else "未知错误"
        return (
            "\033[31m提交处理失败，已拦截异常，界面仍可继续使用。\033[0m\n"
            f"\033[31m{last_line}\033[0m\n"
            f"\033[2m{trace}\033[0m\n"
        )

    def _debug_input(self, source: str, text: str, **extra: Any) -> None:
        if self._debug_trace is not None:
            self._debug_trace.input(source, text, **extra)

    def _debug_output(self, sink: str, text: str, **extra: Any) -> None:
        if self._debug_trace is not None:
            self._debug_trace.output(sink, text, **extra)

    def _debug_exception(self, where: str, trace: str) -> None:
        if self._debug_trace is not None:
            self._debug_trace.event("exception", {"where": where, "trace": trace})

    def record_debug_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        if self._debug_trace is not None:
            self._debug_trace.event(name, data or {})

    @property
    def _live(self) -> tuple[str, ...]:
        """Compatibility view for tests and old private integrations."""
        return self._history.live_text_chunks

    @property
    def _output(self) -> tuple[str, ...]:
        """Compatibility view for tests and old private integrations."""
        return self._history.output_text_chunks

    async def confirm_permission(self, payload: dict[str, Any]) -> str:
        """Ask for one-shot permission from the fixed CLI input area."""
        if self._pending_permission is not None:
            self.record_debug_event(
                "cli.permission_confirm_busy",
                {"tool_name": payload.get("tool_name")},
            )
            return "deny"

        loop = asyncio.get_running_loop()
        self._pending_permission = loop.create_future()
        tool_name = str(payload.get("tool_name", "?"))
        reason = str(payload.get("reason", "") or "该工具需要用户确认。")
        arguments = payload.get("arguments", {})
        args_preview = _fit_text_to_width(
            json.dumps(arguments, ensure_ascii=False, default=str),
            500,
        ).rstrip()
        self.record_debug_event(
            "cli.permission_confirm_prompt",
            {
                "tool_name": tool_name,
                "risk_level": payload.get("risk_level"),
                "permission_mode": payload.get("permission_mode"),
            },
        )
        allow_keys = self._keybindings.display_keys_for(
            KeybindingAction.PERMISSION_ALLOW,
            interface="cli",
        )
        deny_keys = self._keybindings.display_keys_for(
            KeybindingAction.PERMISSION_DENY,
            interface="cli",
        )
        bypass_keys = self._keybindings.display_keys_for(
            KeybindingAction.PERMISSION_BYPASS,
            interface="cli",
        )
        self.set_status(
            f"权限确认: {allow_keys} 允许一次 | {deny_keys} 拒绝 | "
            f"{bypass_keys} 切换 bypass 并执行"
        )
        self.append_live(
            "\n"
            + self._style_config.ansi("permission", "权限确认")
            + "\n"
            f"  工具: {tool_name}\n"
            f"  原因: {reason}\n"
            f"  参数: {args_preview}\n"
            f"  按 {allow_keys} 允许一次，按 {deny_keys} 拒绝，"
            f"按 {bypass_keys} 切换 bypass 并执行。\n"
        )
        try:
            choice = await self._pending_permission
        finally:
            self._pending_permission = None
            self.set_status("执行中")
        self.record_debug_event(
            "cli.permission_confirm_choice",
            {"tool_name": tool_name, "choice": choice},
        )
        return choice

    def _resolve_pending_permission(self, choice: str) -> None:
        future = self._pending_permission
        if future is None or future.done():
            return
        labels = {
            "allow": "已允许本次工具执行",
            "deny": "已拒绝本次工具执行",
            "bypass": "已切换 bypass 并执行本次工具",
        }
        if choice == "bypass":
            self.set_mode_status("bypass")
        self.append_live(
            self._style_config.ansi("muted", labels.get(choice, choice)) + "\n"
        )
        future.set_result(choice)

    def debug_info(self) -> str:
        if self._debug_trace is None:
            return "当前 CLI 未启用结构化调试日志。"
        return self._debug_trace.describe()

    def keybinding_help(self) -> str:
        return render_keybinding_help(self._keybindings, interface="cli")

    def exit(self) -> None:
        if self._app:
            self._app.exit()

    def _invalidate(self) -> None:
        if self._app:
            self._app.invalidate()

    def append_output(self, ansi_text: str) -> None:
        self._history.append_output(ansi_text)
        self._debug_output("cli.output", ansi_text)
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def append_live(self, text: str) -> None:
        self._history.append_live(text)
        self._debug_output("cli.live", text, live=True)
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def finalize_live(self) -> None:
        chunks = self._history.finalize_live()
        if chunks:
            self.record_debug_event("cli.live_finalized", {"chunks": chunks})
        if self._output_win:
            self._output_win.ensure_at_bottom()
        self._invalidate()

    def clear_output(self) -> None:
        self.record_debug_event(
            "cli.output_cleared",
            {
                "output_chunks": self._history.output_chunks,
                "live_chunks": self._history.live_chunks,
            },
        )
        self._history.clear()
        if self._output_win:
            self._output_win.scroll_to_bottom()
        self._invalidate()

    def reset_output(self) -> None:
        """Replace the transcript view with a fresh scroll state."""
        self.clear_output()

    def set_status(self, text: str) -> None:
        """Update fixed bottom status text without adding it to chat history."""
        self._status_text = text
        self.record_debug_event("cli.status", {"text": text})
        self._invalidate()

    def set_mode_status(self, text: str) -> None:
        """Update runtime mode shown in the fixed bottom status line."""
        self._mode_text = text
        self.record_debug_event("cli.runtime_mode", {"mode": text})
        self._invalidate()

    def set_todo_status(self, text: str | None) -> None:
        """Update the sticky bottom todo bar, or clear it when text is empty."""
        self._todo_text = text or ""
        self.record_debug_event("cli.todo_status", {"text": self._todo_text})
        self._invalidate()

    def set_activity_status(self, text: str | None) -> None:
        """Update the sticky bottom activity bar for transient tool preparation."""
        self._activity_text = text or ""
        self.record_debug_event("cli.activity_status", {"text": self._activity_text})
        self._invalidate()

    def get_transcript(self) -> str:
        """Return the complete visible transcript, including live output."""
        return self._history.transcript()

    def copy_transcript(self, scope: str = "all") -> str:
        """Copy or save transcript diagnostics and show a short status line."""
        normalized_scope = scope.strip().lower() or "all"
        if normalized_scope in {"last", "error"} and self._debug_trace is not None:
            text = self._debug_trace.build_diagnostic_text(normalized_scope)
            prefix = f"cli-{normalized_scope}-diagnostic"
        else:
            text = self.get_transcript()
            prefix = "cli-transcript"
        result = copy_or_save_transcript(
            text,
            base_dir=Path.cwd() / "data",
            prefix=prefix,
        )
        self.append_output(f"\033[2m{result.message}\033[0m\n")
        return result.message

    def set_git_info(self, branch: str, dirty: bool) -> None:
        """Update git branch shown in the prompt prefix."""
        self._git_branch = branch
        self._git_dirty = dirty
        self._invalidate()

    def _render_output(self) -> list:
        # Kept for tests and any external prompt_toolkit caller that still uses
        # FormattedTextControl directly.  The real CLI output window is backed
        # by VirtualizedHistoryControl, so visible lines are formatted lazily.
        fragments: list = []
        line_count = self._history.line_count()
        for lineno in range(line_count):
            pin = bool(self._output_win and self._output_win.auto_scroll)
            fragments.extend(
                self._history.get_line(
                    lineno,
                    width=80,
                    pin_cursor=pin and lineno == line_count - 1,
                )
            )
            if lineno < line_count - 1:
                fragments.append(("", "\n"))
        return fragments

    def _render_status(self, cols: int) -> FormattedText:
        text = f" mode: {self._mode_text} | {self._status_text}"
        return FormattedText([("class:status", _fit_text_to_width(text, cols))])

    def _render_todo(self, cols: int) -> FormattedText:
        text = f" {self._todo_text}"
        return FormattedText([("class:processing", _fit_text_to_width(text, cols))])

    def _render_activity(self, cols: int) -> FormattedText:
        text = f" {self._activity_text}"
        return FormattedText([("class:processing", _fit_text_to_width(text, cols))])

    def _build_app(self) -> Application:
        self._output_win = _OutputWindow(
            content=VirtualizedHistoryControl(
                self._history,
                should_pin_cursor=lambda: bool(
                    self._output_win and self._output_win.auto_scroll
                ),
            ),
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
        todo_win = ConditionalContainer(
            Window(
                height=1,
                content=_DynamicLineControl(lambda width: self._render_todo(width)),
            ),
            filter=Condition(lambda: bool(self._todo_text)),
        )
        activity_win = ConditionalContainer(
            Window(
                height=1,
                content=_DynamicLineControl(lambda width: self._render_activity(width)),
            ),
            filter=Condition(lambda: bool(self._activity_text)),
        )

        body = HSplit([
            self._output_win,
            activity_win,
            todo_win,
            status_win,
            border_top,
            input_win,
            border_bot,
        ])
        root = FloatContainer(
            content=body,
            floats=[
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12)),
            ],
        )
        return Application(
            layout=Layout(root, focused_element=input_win),
            key_bindings=self._kb,
            style=self._style_config.prompt_toolkit_style(),
            full_screen=True,
            mouse_support=True,
        )

    async def run(self) -> None:
        self._app = self._build_app()
        await self._app.run_async()
