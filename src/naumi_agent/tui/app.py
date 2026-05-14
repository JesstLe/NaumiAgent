"""NaumiAgent TUI — Textual 界面."""

from __future__ import annotations

import logging
from typing import Any

from rich.markdown import Markdown
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Static,
)

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)


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
    """聊天面板 — 显示对话消息."""

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
        self._agent_text = ""
        self._current_agent_widget: Static | None = None

    def add_user_message(self, content: str) -> None:
        md = Static(Markdown(f"**你** {content}"), classes="user-msg")
        self.mount(md)
        self.scroll_end(animate=False)

    def add_agent_chunk(self, token: str) -> None:
        if self._current_agent_widget is None:
            self._agent_text = token
            self._current_agent_widget = Static(Markdown(token), classes="agent-msg")
            self.mount(self._current_agent_widget)
        else:
            self._agent_text += token
            self._current_agent_widget.update(Markdown(self._agent_text))
        self.scroll_end(animate=False)

    def finalize_agent_message(self, turns: int, cost: float) -> None:
        self._agent_text = ""
        self._current_agent_widget = None
        self.mount(Static(
            f"[dim]轮次: {turns} | 费用: ${cost:.4f}[/dim]",
            classes="usage-info",
        ))
        self.scroll_end(animate=False)

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int) -> None:
        color = "green" if status == "success" else "red"
        self.mount(Static(
            f"  [dim]⚙ {tool_name} ({duration_ms}ms) [{color}]{status}[/{color}][/dim]",
            classes="tool-msg",
        ))


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
        self.mount(Static(
            f"[{color}]{icon}[/{color}] {tool_name} ({duration_ms}ms)\n"
            f"  [dim]{args}[/dim]",
            classes="tool-log-entry",
        ))
        self.scroll_end(animate=False)


class InputBar(Horizontal):
    """输入栏."""

    DEFAULT_CSS = """
    InputBar {
        height: 3;
        padding: 0 1;
        border-top: solid green;
    }
    InputBar Input {
        width: 1fr;
    }
    InputBar Button {
        width: auto;
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="输入任务，Shift+Enter 换行...", id="msg-input")
        yield Button("发送", variant="primary", id="send-btn")

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.app.post_message(UserInputMessage(event.value))
            event.input.value = ""

    @on(Button.Pressed)
    def on_send_pressed(self) -> None:
        input = self.query_one(Input)
        if input.value.strip():
            self.app.post_message(UserInputMessage(input.value))
            input.value = ""


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


class NaumiApp(App):
    """NaumiAgent TUI 应用."""

    TITLE = "NaumiAgent"
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
        border-left: solid blue;
    }

    .agent-msg {
        background: $surface;
        padding: 1 2;
        margin: 1 0;
        border-left: solid green;
    }

    .tool-msg {
        padding: 0 2;
    }

    .usage-info {
        padding: 0 2;
        margin-bottom: 1;
    }

    .tool-log-entry {
        padding: 0 1;
        margin-bottom: 1;
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
        yield StatusBar()
        yield Footer()

    def on_user_input_message(self, msg: UserInputMessage) -> None:
        chat = self.query_one(ChatPanel)
        chat.add_user_message(msg.content)
        status = self.query_one(StatusBar)
        status.status_text = "思考中..."
        self._set_input_enabled(False)
        self._run_agent(msg.content)

    @work(exclusive=True, exit_on_error=False)
    async def _run_agent(self, task: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        try:
            result = await self.engine.run(task)

            if result.status == "error" and result.error:
                chat.add_agent_chunk(f"**错误**: {result.error}")
            else:
                chat.add_agent_chunk(result.response or "(无响应)")

            chat.finalize_agent_message(result.usage.turns, result.usage.total_cost_usd)

            status.status_text = (
                f"完成 | 轮次: {result.usage.turns} | "
                f"Token: {result.usage.total_input_tokens + result.usage.total_output_tokens} | "
                f"费用: ${result.usage.total_cost_usd:.4f}"
            )
        except Exception as e:
            logger.exception("Agent run failed")
            chat.add_agent_chunk(f"**错误**: {e}")
            chat.finalize_agent_message(0, 0.0)
            status.status_text = f"错误: {e}"
        finally:
            self._set_input_enabled(True)

    def _set_input_enabled(self, enabled: bool) -> None:
        input_bar = self.query_one(InputBar)
        msg_input = input_bar.query_one(Input)
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
        chat.query(Static).remove()
        chat._agent_text = ""
        chat._current_agent_widget = None
        self.engine.reset()

    def action_show_tools(self) -> None:
        chat = self.query_one(ChatPanel)
        tools = self.engine.tool_registry.all()
        lines = ["## 可用工具\n"]
        for t in tools:
            lines.append(f"- **{t.name}** — {t.description}")
        chat.mount(Static(Markdown("\n".join(lines)), classes="agent-msg"))
