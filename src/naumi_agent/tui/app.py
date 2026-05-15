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
from textual.screen import ModalScreen
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


class LoadSessionMessage(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class DeleteSessionMessage(Message):
    def __init__(self, session_id: str, title: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.title = title

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


class HistoryPanel(VerticalScroll):
    """历史会话面板 — 显示会话列表，点击加载."""

    DEFAULT_CSS = """
    HistoryPanel {
        width: 36;
        height: 1fr;
        padding: 0 1;
        border-left: solid green;
        background: $surface;
        display: none;
    }

    HistoryPanel .history-title {
        padding: 1 0;
        text-style: bold;
        color: $text;
    }

    HistoryPanel .session-entry {
        padding: 0 1;
        margin: 0 0 1 0;
        background: $boost;
        width: 1fr;
    }

    HistoryPanel .session-entry:hover {
        background: $primary-darken-1;
    }

    HistoryPanel .session-entry.current {
        border-left: thick green;
    }

    HistoryPanel .session-entry .session-id {
        color: $text-muted;
        text-style: dim;
    }

    HistoryPanel .session-entry .session-title {
        color: $text;
    }

    HistoryPanel .session-entry .session-meta {
        color: $text-muted;
        text-style: dim;
    }
    """

    show_panel: reactive[bool] = reactive(False)

    def watch_show_panel(self, show: bool) -> None:
        self.display = show

    @work
    async def refresh_sessions(self) -> None:
        """从数据库加载会话列表."""
        app = self.app
        if not isinstance(app, NaumiApp):
            return

        # 清除旧内容
        for child in list(self.children):
            child.remove()

        self.mount(Static("📋 历史会话", classes="history-title"))

        try:
            sessions, total = await app.engine.list_sessions(page=1, page_size=50)
        except Exception:
            self.mount(Static("[dim]加载失败[/dim]"))
            return

        if not sessions:
            self.mount(Static("[dim]暂无历史会话[/dim]"))
            return

        current_id = app.engine._session.id if app.engine._session else None

        for s in sessions:
            title = s.title or "新会话"
            if len(title) > 28:
                title = title[:26] + "…"
            time_str = s.updated_at.strftime("%m-%d %H:%M")
            msg_count = len(s.messages)
            is_current = s.id == current_id

            entry = SessionEntry(
                session_id=s.id,
                title=title,
                time_str=time_str,
                msg_count=msg_count,
                is_current=is_current,
            )
            self.mount(entry)

        self.mount(Static(f"[dim]共 {total} 个会话[/dim]"))

    def on_session_entry_clicked(self, event: SessionEntry.Clicked) -> None:
        self.app.post_message(LoadSessionMessage(event.entry.session_id))

    def on_delete_session_message(self, event: DeleteSessionMessage) -> None:
        app = self.app
        if not isinstance(app, NaumiApp):
            return

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                app._delete_session(event.session_id, event.title)

        app.push_screen(DeleteConfirmScreen(event.title), on_confirm)


class DeleteConfirmScreen(ModalScreen[bool]):
    """删除确认弹窗."""

    DEFAULT_CSS = """
    DeleteConfirmScreen {
        align: center middle;
    }
    DeleteConfirmScreen > Container {
        width: auto;
        height: auto;
        padding: 1 2;
        border: thick $background 80%;
        background: $surface;
    }
    DeleteConfirmScreen > Container > Label {
        width: auto;
        margin: 0 0 1 0;
    }
    DeleteConfirmScreen > Container > Horizontal {
        width: auto;
        height: auto;
    }
    DeleteConfirmScreen > Container > Horizontal > Button {
        margin: 0 1;
    }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.session_title = title

    def compose(self) -> ComposeResult:
        from textual.widgets import Label
        with Container():
            yield Label(f"确认删除会话 [bold]{self.session_title}[/bold]？")
            with Horizontal():
                yield Button("确认", variant="error", id="confirm")
                yield Button("取消", variant="primary", id="cancel")

    @on(Button.Pressed, "#confirm")
    def on_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def on_cancel(self) -> None:
        self.dismiss(False)


class SessionEntry(Static):
    """单个会话条目 — 可点击加载，右侧删除按钮."""

    class Clicked(Message):
        def __init__(self, entry: SessionEntry) -> None:
            super().__init__()
            self.entry = entry

    DEFAULT_CSS = """
    SessionEntry {
        padding: 0 1;
        margin: 0 0 1 0;
        background: $boost;
        width: 1fr;
        height: auto;
        layout: horizontal;
    }

    SessionEntry:hover {
        background: $primary-darken-1;
    }

    SessionEntry.current {
        border-left: thick green;
    }

    SessionEntry .session-info {
        width: 1fr;
    }

    SessionEntry .delete-btn {
        width: 3;
        height: 3;
        min-width: 3;
    }
    """

    def __init__(
        self,
        session_id: str,
        title: str,
        time_str: str,
        msg_count: int,
        is_current: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.session_id = session_id
        self.title_text = title
        self._entry_title = title
        self._entry_time = time_str
        self._entry_count = msg_count
        if is_current:
            self.add_class("current")

    def compose(self) -> ComposeResult:
        yield Static(
            f"[dim]{self.session_id}[/dim]\n"
            f"{self._entry_title}\n"
            f"[dim]{self._entry_time} · {self._entry_count}条消息[/dim]",
            classes="session-info",
        )
        yield Button("✕", variant="error", classes="delete-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(DeleteSessionMessage(self.session_id, self.title_text))

    def on_click(self) -> None:
        self.post_message(self.Clicked(self))


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
        Binding("ctrl+h", "toggle_history", "历史"),
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
            yield HistoryPanel()
            yield ActivityPanel()
        yield InputBar()
        yield Spinner()
        yield StatusBar()
        yield Footer()

    async def on_unmount(self) -> None:
        await self.engine.shutdown()

    def on_user_input_message(self, msg: UserInputMessage) -> None:
        text = msg.content.strip()

        # 斜杠命令拦截
        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        chat = self.query_one(ChatPanel)
        chat.add_user_message(msg.content)
        status = self.query_one(StatusBar)
        status.status_text = "思考中..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        self._run_agent(msg.content)

    def _handle_slash_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        match cmd:
            case "/clear":
                chat.clear()
                self.engine.reset()
                status.status_text = "会话已清除"
            case "/help":
                help_text = (
                    "## 可用命令\n"
                    "- `/help` — 显示帮助\n"
                    "- `/tools` — 列出可用工具\n"
                    "- `/model` — 显示模型配置\n"
                    "- `/usage` — 显示 token 用量\n"
                    "- `/history` — 查看历史会话列表\n"
                    "- `/load <id>` — 加载指定会话\n"
                    "- `/chaos [目标]` — 灾难演练 (SPOF)\n"
                    "- `/scale [QPS]` — 并发海啸测试\n"
                    "- `/state` — 云原生状态审查\n"
                    "- `/vibe <描述>` — 极速构建 Demo\n"
                    "- `/eval <路径>` — 评测驱动 (EDD)\n"
                    "- `/page` — 内存分页调度\n"
                    "- `/heal <错误>` — 自愈修复\n"
                    "- `/dspy [描述]` — DSPy 编译优化\n"
                    "- `/graph [路径]` — 图谱推演 (GraphRAG)\n"
                    "- `/mcts <问题>` — 蒙特卡洛树搜索\n"
                    "- `/route <任务>` — MoE 混合专家调度\n"
                    "- `/speculate <路径>` — 推测解码\n"
                    "- `/clear` — 清除当前会话\n"
                    "- `/quit` — 退出\n"
                )
                chat.mount(Markdown(help_text, classes="agent-msg"))
            case "/tools":
                tools = self.engine.tool_registry.all()
                lines = ["## 可用工具\n"]
                for t in tools:
                    lines.append(f"- **{t.name}** — {t.description}")
                chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
            case "/model":
                info = (
                    f"## 模型配置\n"
                    f"- 默认: `{self.engine.router.resolve_model('capable')}`\n"
                    f"- 快速: `{self.engine.router.resolve_model('fast')}`\n"
                    f"- 推理: `{self.engine.router.resolve_model('reasoning')}`\n"
                )
                chat.mount(Markdown(info, classes="agent-msg"))
            case "/usage":
                u = self.engine.usage
                total_tok = u.total_input_tokens + u.total_output_tokens
                info = (
                    f"## 用量统计\n"
                    f"- Token: {total_tok}\n"
                    f"- 费用: ${u.total_cost_usd:.4f}\n"
                    f"- 轮次: {u.turns}\n"
                )
                chat.mount(Markdown(info, classes="agent-msg"))
            case "/history":
                self.action_toggle_history()
            case "/load":
                if not arg:
                    status.status_text = "用法: /load <session_id>"
                    self.action_toggle_history()
                else:
                    self._load_and_show_session(arg)
            case "/chaos":
                self._run_analysis_mode("chaos", arg or "当前项目")
            case "/scale":
                self._run_analysis_mode("scale", arg or "当前项目")
            case "/state":
                self._run_analysis_mode("state", arg or "当前项目")
            case "/vibe":
                if not arg:
                    status.status_text = "用法: /vibe <功能描述>"
                else:
                    self._run_analysis_mode("vibe", arg)
            case "/eval":
                if not arg:
                    status.status_text = "用法: /eval <文件或目录路径>"
                else:
                    self._run_analysis_mode("eval", arg)
            case "/page":
                self._run_analysis_mode("page", "memory")
            case "/heal":
                if not arg:
                    status.status_text = "用法: /heal <错误日志或错误描述>"
                else:
                    self._run_analysis_mode("heal", arg)
            case "/dspy":
                self._run_analysis_mode("dspy", arg or "")
            case "/graph":
                self._run_analysis_mode("graph", arg or "")
            case "/mcts":
                if not arg:
                    status.status_text = "用法: /mcts <问题描述>"
                else:
                    self._run_analysis_mode("mcts", arg)
            case "/route":
                if not arg:
                    status.status_text = "用法: /route <任务描述>"
                else:
                    self._run_analysis_mode("route", arg)
            case "/speculate":
                if not arg:
                    status.status_text = "用法: /speculate <文件或目录路径>"
                else:
                    self._run_analysis_mode("speculate", arg)
            case "/quit" | "/exit":
                self.exit()
            case _:
                chat.mount(
                    Markdown(
                        f"**未知命令**: `{cmd}`\n输入 `/help` 查看可用命令",
                        classes="agent-msg",
                    )
                )

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

    @work(exclusive=True, exit_on_error=False)
    async def _run_analysis_mode(self, mode: str, target: str) -> None:
        """执行分析模式 (chaos/scale/state/vibe) — 走工具的 execute 路径."""
        tool_names = {
            "chaos": "analysis_chaos",
            "scale": "analysis_scale",
            "state": "analysis_state",
            "vibe": "analysis_vibe",
            "eval": "analysis_eval",
            "page": "analysis_page",
            "heal": "analysis_heal",
            "dspy": "analysis_dspy",
            "graph": "analysis_graph",
            "mcts": "analysis_mcts",
            "route": "analysis_route",
            "speculate": "analysis_speculate",
        }
        labels = {
            "chaos": "⚡ 灾难演练",
            "scale": "🌊 并发海啸 (10K QPS)",
            "state": "☁️ 状态审查",
            "vibe": "🚀 极速构建",
            "eval": "🧪 评测驱动 (EDD)",
            "page": "💾 内存分页",
            "heal": "🏥 自愈修复",
            "dspy": "🔧 DSPy 编译优化",
            "graph": "🕸️ 图谱推演 (GraphRAG)",
            "mcts": "🌳 蒙特卡洛树搜索",
            "route": "🧠 MoE 混合专家调度",
            "speculate": "⚡推测解码 (Draft+Review)",
        }

        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        label = labels[mode]

        tool = self.engine.tool_registry.get(tool_names[mode])
        if tool is None:
            chat.mount(Markdown(f"**工具未注册**: {tool_names[mode]}", classes="agent-msg"))
            return

        chat.mount(Markdown(f"**{label}** 扫描 + 分析中...", classes="agent-msg"))
        status.status_text = f"{label} 分析中..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True

        try:
            if mode == "vibe":
                result = await tool.execute(description=target)
            elif mode == "scale":
                result = await tool.execute(target=target, qps=10000)
            elif mode == "eval":
                result = await tool.execute(target=target)
            elif mode == "page":
                result = await tool.execute()
            elif mode == "heal":
                result = await tool.execute(error_log=target)
            elif mode == "dspy":
                result = await tool.execute(prompt_target=target)
            elif mode == "graph":
                result = await tool.execute(target=target)
            elif mode == "mcts":
                result = await tool.execute(problem=target)
            elif mode == "route":
                result = await tool.execute(task=target)
            elif mode == "speculate":
                result = await tool.execute(target=target)
            else:
                result = await tool.execute(target=target)
            chat.mount(Markdown(result, classes="agent-msg"))
            status.status_text = f"✅ {label} 完成"
        except Exception as e:
            chat.mount(Markdown(f"**分析失败**: {e}", classes="agent-msg"))
            status.status_text = f"❌ 分析失败: {e}"
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)

    def action_toggle_activity(self) -> None:
        activity = self.query_one(ActivityPanel)
        activity.show_panel = not activity.show_panel

    def action_toggle_history(self) -> None:
        history = self.query_one(HistoryPanel)
        history.show_panel = not history.show_panel
        if history.show_panel:
            history.refresh_sessions()

    def on_load_session_message(self, msg: LoadSessionMessage) -> None:
        self._load_and_show_session(msg.session_id)

    @work(exclusive=True, exit_on_error=False)
    async def _load_and_show_session(self, session_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        loaded = await self.engine.load_session(session_id)
        if not loaded:
            status.status_text = f"会话 {session_id} 不存在"
            return

        session = self.engine._session
        chat.clear()

        # 回放历史消息
        for m in session.messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                chat.add_user_message(content)
            elif role == "assistant":
                chat.mount(Markdown(content, classes="agent-msg"))

        title = session.title or session_id
        msg_count = len(session.messages)
        status.status_text = (
            f"已加载: {title} | {msg_count}条消息 | "
            f"Token: {session.total_tokens} | ${session.total_cost_usd:.4f}"
        )

        # 刷新历史面板高亮
        history = self.query_one(HistoryPanel)
        if history.show_panel:
            history.refresh_sessions()

    def action_clear_chat(self) -> None:
        chat = self.query_one(ChatPanel)
        chat.clear()
        self.engine.reset()

    @work(exclusive=True, exit_on_error=False)
    async def _delete_session(self, session_id: str, title: str) -> None:
        status = self.query_one(StatusBar)
        try:
            ok = await self.engine.delete_session(session_id)
            if ok:
                status.status_text = f"已删除: {title}"
                if self.engine._session and self.engine._session.id == session_id:
                    chat = self.query_one(ChatPanel)
                    chat.clear()
                    self.engine.reset()
                history = self.query_one(HistoryPanel)
                if history.show_panel:
                    history.refresh_sessions()
            else:
                status.status_text = f"会话不存在: {session_id}"
        except Exception as e:
            status.status_text = f"删除失败: {e}"

    def action_show_tools(self) -> None:
        chat = self.query_one(ChatPanel)
        tools = self.engine.tool_registry.all()
        lines = ["## 可用工具\n"]
        for t in tools:
            lines.append(f"- **{t.name}** — {t.description}")
        chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
