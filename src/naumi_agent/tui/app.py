"""NaumiAgent TUI — Textual 界面，支持流式输出与思考过程展示."""

from __future__ import annotations

import logging
from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Markdown,
    Static,
    TextArea,
)

from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

_THINKING_LABEL = "\U0001f4ad 思考中"  # 💭 思考中


class AgentTokenMessage(Message):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token


class AgentEndMessage(Message):
    def __init__(self, status: str, turns: int, cost: float) -> None:
        super().__init__()
        self.status = status
        self.turns = turns
        self.cost = cost


class ToolCallMessage(Message):
    def __init__(self, tool_name: str, status: str, duration_ms: int = 0) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.status = status
        self.duration_ms = duration_ms


class ChatPanel(VerticalScroll):
    """聊天面板 — 显示对话消息，支持流式输出."""

    DEFAULT_CSS = """
    ChatPanel {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._response_text = ""
        self._response_widget: Markdown | Static | None = None
        self._thinking_text = ""
        self._thinking_content_widget: Static | None = None
        self._thinking_collapsible: Collapsible | None = None
        self._current_tool_widget: Static | None = None

    def add_user_message(self, content: str) -> None:
        self.mount(Markdown(f"**你** {content}", classes="user-msg"))
        self.scroll_end(animate=False)

    # --- 思考过程 ---

    def start_thinking(self) -> None:
        self._thinking_text = ""
        self._thinking_content_widget = Static(_THINKING_LABEL, classes="thinking-content")
        self._thinking_collapsible = Collapsible(
            self._thinking_content_widget,
            title="💭 思考过程",
            classes="thinking-block",
        )
        self.mount(self._thinking_collapsible)
        self.scroll_end(animate=False)

    def add_thinking_chunk(self, content: str) -> None:
        self._thinking_text += content
        if self._thinking_content_widget:
            self._thinking_content_widget.update(RichMarkdown(self._thinking_text))
            self.scroll_end(animate=False)

    def end_thinking(self) -> None:
        if not self._thinking_text:
            if self._thinking_collapsible:
                self._thinking_collapsible.remove()
        elif self._thinking_collapsible:
            self._thinking_collapsible.collapsed = True
        self._thinking_text = ""
        self._thinking_content_widget = None
        self._thinking_collapsible = None

    # --- 流式响应 ---

    def start_response(self) -> None:
        self._response_text = ""
        self._response_widget = Markdown("", classes="agent-msg")
        self.mount(self._response_widget)
        self.scroll_end(animate=False)

    def add_response_token(self, token: str) -> None:
        self._response_text += token
        if self._response_widget:
            self._response_widget.update(self._response_text)
            self.scroll_end(animate=False)

    # --- 工具调用 ---

    def start_tool(self, name: str) -> None:
        self._current_tool_widget = Static(
            Text.from_markup(f"  ⏳ [dim]{name}[/dim]"),
            classes="tool-running",
        )
        self.mount(self._current_tool_widget)
        self.scroll_end(animate=False)

    def end_tool(self, name: str, status: str, duration_ms: int) -> None:
        if self._current_tool_widget is None:
            return
        icon = "✅" if status == "success" else "❌"
        self._current_tool_widget.update(
            Text.from_markup(f"  {icon} [dim]{name} ({duration_ms}ms)[/dim]")
        )
        self._current_tool_widget.set_class(False, "tool-running")
        self._current_tool_widget.set_class(True, "tool-done")
        self._current_tool_widget = None
        self.scroll_end(animate=False)

    # --- 清空 ---

    def clear(self) -> None:
        self.query(Static).remove()
        self.query(Markdown).remove()
        self.query(Collapsible).remove()
        self._response_text = ""
        self._response_widget = None
        self._thinking_text = ""
        self._thinking_content_widget = None
        self._thinking_collapsible = None
        self._current_tool_widget = None

    # --- 结束 ---

    def finalize(self, turns: int, cost: float, tokens: int = 0) -> None:
        self._response_text = ""
        self._response_widget = None
        self.mount(
            Static(
                Text.from_markup(f"[dim]轮次: {turns} | Token: {tokens} | 费用: ${cost:.4f}[/dim]"),
                classes="usage-info",
            )
        )
        self.scroll_end(animate=False)

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int) -> None:
        color = "green" if status == "success" else "red"
        self.mount(
            Static(
                f"  [dim]⚙ {tool_name} ({duration_ms}ms) [{color}]{status}[/{color}][/dim]",
                classes="tool-msg",
            )
        )


class ActivityPanel(VerticalScroll):
    """活动面板 — 工具调用日志."""

    DEFAULT_CSS = """
    ActivityPanel {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        border-left: solid green;
        display: none;
    }
    """

    show_panel: reactive[bool] = reactive(False)

    def watch_show_panel(self, show: bool) -> None:
        self.display = show

    def add_tool_log(self, tool_name: str, args: dict, status: str, duration_ms: int) -> None:
        icon = "✓" if status == "success" else "✗"
        color = "green" if status == "success" else "red"
        self.mount(
            Static(
                f"[{color}]{icon}[/{color}] {tool_name} ({duration_ms}ms)\n  [dim]{args}[/dim]",
                classes="tool-log-entry",
            )
        )
        self.scroll_end(animate=False)


class InputBar(Horizontal):
    """输入栏 — 多行输入 + 发送按钮."""

    DEFAULT_CSS = """
    InputBar {
        height: 5;
        padding: 0 1;
        border-top: solid green;
    }
    InputBar TextArea {
        width: 1fr;
        height: 1fr;
    }
    InputBar Button {
        width: auto;
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+enter", "send", "发送"),
    ]

    def compose(self) -> ComposeResult:
        yield TextArea(id="msg-input")
        yield Button("发送", variant="primary", id="send-btn")

    def action_send(self) -> None:
        textarea = self.query_one("#msg-input", TextArea)
        text = textarea.text.strip()
        if text:
            self.app.post_message(UserInputMessage(text))
            textarea.clear()

    @on(Button.Pressed)
    def on_send_pressed(self) -> None:
        self.action_send()


class UserInputMessage(Message):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content


class StatusBar(Static):
    """底部状态栏."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    status_text: reactive[str] = reactive("就绪")

    def watch_status_text(self, text: str) -> None:
        self.update(text)


class Spinner(Static):
    """动画旋转指示器."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _frame: reactive[int] = reactive(0)
    _active: reactive[bool] = reactive(False)

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick, pause=True)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._FRAMES)

    def watch__frame(self, idx: int) -> None:
        if self._active:
            self.update(Text(f"  {self._FRAMES[idx]}", style="bold green"))

    def watch__active(self, active: bool) -> None:
        if active:
            self._timer.resume()
            self._frame = 0
        else:
            self._timer.pause()
            self.update("")


class NaumiApp(App):
    """NaumiAgent TUI 应用."""

    TITLE = "⬡ NaumiAgent"
    SUB_TITLE = "通用智能 Agent"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-area {
        height: 1fr;
        layout: horizontal;
    }

    .user-msg {
        background: $boost;
        padding: 1 2;
        margin: 1 0;
        border-left: thick blue;
    }

    .agent-msg {
        background: $surface;
        padding: 1 2;
        margin: 1 0;
        border-left: thick green;
    }

    Markdown.user-msg {
        background: $boost;
        padding: 1 2;
        margin: 1 0;
        border-left: thick blue;
    }

    Markdown.agent-msg {
        background: $surface;
        padding: 1 2;
        margin: 1 0;
        border-left: thick green;
    }

    .thinking-block {
        margin: 1 0;
        padding: 0;
        border-left: thick yellow;
        background: $surface-darken-1;
    }

    .thinking-block CollapsibleTitle {
        text-style: bold italic;
        color: yellow;
        padding: 0 1;
    }

    .thinking-content {
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }

    .tool-running {
        padding: 0 2;
        margin: 0 0;
    }

    .tool-done {
        padding: 0 2;
        margin: 0 0;
    }

    .usage-info {
        padding: 0 2;
        margin-bottom: 1;
    }

    .tool-log-entry {
        padding: 0 1;
        margin-bottom: 1;
    }

    Spinner {
        height: 1;
        padding: 0 2;
        color: green;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("tab", "toggle_activity", "活动面板"),
        Binding("ctrl+l", "clear_chat", "清空"),
        Binding("ctrl+t", "show_tools", "工具列表"),
    ]

    def __init__(self, engine: AgentEngine, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.engine = engine

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-area"):
            yield ChatPanel()
            yield ActivityPanel()
        yield InputBar()
        yield Spinner()
        yield StatusBar()
        yield Footer()

    async def on_unmount(self) -> None:
        await self.engine.shutdown()

    def on_user_input_message(self, msg: UserInputMessage) -> None:
        chat = self.query_one(ChatPanel)
        chat.add_user_message(msg.content)
        status = self.query_one(StatusBar)
        status.status_text = "思考中..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        self._run_agent(msg.content)

    @work(exclusive=True, exit_on_error=False)
    async def _run_agent(self, task: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        async def on_event(event_type: str, data: dict[str, Any]) -> None:
            match event_type:
                case "thinking_start":
                    chat.start_thinking()
                    status.status_text = "💭 思考中..."
                case "thinking_delta":
                    chat.add_thinking_chunk(data["content"])
                case "thinking_end":
                    chat.end_thinking()
                case "response_start":
                    chat.start_response()
                    status.status_text = "✍ 生成回复..."
                case "token":
                    chat.add_response_token(data["content"])
                case "response_end":
                    pass
                case "turn_start":
                    turn = data["turn"]
                    if turn > 1:
                        status.status_text = f"🔄 第 {turn} 轮..."
                case "tool_start":
                    chat.start_tool(data["name"])
                    status.status_text = f"⚙ {data['name']}..."
                case "tool_end":
                    chat.end_tool(data["name"], data["status"], data["duration_ms"])
                case "context_compacted":
                    logger.info(
                        "Context compacted: %d → %d messages",
                        data["before"],
                        data["after"],
                    )
                case "error":
                    chat.start_response()
                    chat.add_response_token(f"**错误**: {data['message']}")
                    chat.finalize(0, 0.0)

        try:
            result = await self.engine.run_streaming(task, on_event)

            if result.status == "error" and result.error:
                chat.start_response()
                chat.add_response_token(f"**错误**: {result.error}")

            chat.finalize(
                result.usage.turns,
                result.usage.total_cost_usd,
                result.usage.total_input_tokens + result.usage.total_output_tokens,
            )
            status.status_text = (
                f"✅ 完成 | 轮次: {result.usage.turns} | "
                f"Token: {result.usage.total_input_tokens + result.usage.total_output_tokens} | "
                f"费用: ${result.usage.total_cost_usd:.4f}"
            )
        except Exception as e:
            logger.exception("Agent run failed")
            chat.start_response()
            chat.add_response_token(f"**错误**: {e}")
            chat.finalize(0, 0.0)
            status.status_text = f"❌ 错误: {e}"
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)

    def _set_input_enabled(self, enabled: bool) -> None:
        input_bar = self.query_one(InputBar)
        msg_input = input_bar.query_one("#msg-input", TextArea)
        send_btn = input_bar.query_one("#send-btn", Button)
        msg_input.disabled = not enabled
        send_btn.disabled = not enabled
        if enabled:
            msg_input.focus()

    def action_toggle_activity(self) -> None:
        activity = self.query_one(ActivityPanel)
        activity.show_panel = not activity.show_panel

    def action_clear_chat(self) -> None:
        chat = self.query_one(ChatPanel)
        chat.clear()
        self.engine.reset()

    def action_show_tools(self) -> None:
        chat = self.query_one(ChatPanel)
        tools = self.engine.tool_registry.all()
        lines = ["## 可用工具\n"]
        for t in tools:
            lines.append(f"- **{t.name}** — {t.description}")
        chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
