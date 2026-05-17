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
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Markdown,
    Static,
)

from naumi_agent.cli_completer import COMMANDS
from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

_SLASH_SUGGESTIONS = SuggestFromList(
    [cmd for cmd, _, _ in COMMANDS],
    case_sensitive=True,
)


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
        overflow-y: auto;
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

    def add_completed_thinking(self, content: str) -> None:
        """从历史消息中恢复已完成的思考过程."""
        if not content:
            return
        thinking_widget = Static(_THINKING_LABEL, classes="thinking-content")
        collapsible = Collapsible(
            thinking_widget,
            title="💭 思考过程",
            classes="thinking-block",
        )
        self.mount(collapsible)
        thinking_widget.update(RichMarkdown(content))
        collapsible.collapsed = True
        self.scroll_end(animate=False)

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
        self.scroll_to(0, animate=False)

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
    """输入栏 — 输入框 + 发送按钮，斜杠命令自动补全."""

    DEFAULT_CSS = """
    InputBar {
        height: auto;
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
        yield Input(
            placeholder="输入消息或 / 命令…",
            id="msg-input",
            suggester=_SLASH_SUGGESTIONS,
        )
        yield Button("发送", variant="primary", id="send-btn")

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.app.post_message(UserInputMessage(text))
            input_widget = self.query_one("#msg-input", Input)
            input_widget.value = ""

    @on(Button.Pressed)
    def on_send_pressed(self) -> None:
        input_widget = self.query_one("#msg-input", Input)
        text = input_widget.value.strip()
        if text:
            self.app.post_message(UserInputMessage(text))
            input_widget.value = ""


class BrowserPanel(VerticalScroll):
    """浏览器面板 — 显示浏览器状态、任务列表、扫描结果."""

    DEFAULT_CSS = """
    BrowserPanel {
        width: 40;
        height: 1fr;
        padding: 0 1;
        border-left: solid green;
        background: $surface;
        display: none;
    }

    BrowserPanel .browser-section-title {
        padding: 1 0;
        text-style: bold;
        color: $text;
    }

    BrowserPanel .browser-status {
        padding: 0 1;
        margin: 0 0 1 0;
    }

    BrowserPanel .task-entry {
        padding: 0 1;
        margin: 0 0 1 0;
        background: $boost;
        width: 1fr;
    }

    BrowserPanel .finding-entry {
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    show_panel: reactive[bool] = reactive(False)

    def watch_show_panel(self, show: bool) -> None:
        self.display = show

    def refresh_browser_state(self, engine: Any) -> None:
        for child in list(self.children):
            child.remove()

        self.mount(Static("🌐 浏览器", classes="browser-section-title"))
        runtime = engine._browser_session
        active = runtime.page is not None
        status_text = "✅ 活跃" if active else "⬜ 未启动"
        self.mount(Static(status_text, classes="browser-status"))
        if active:
            try:
                url = runtime.page.url
                title = runtime.page.title
                self.mount(
                    Static(
                        f"[dim]{url[:50]}[/dim]\n[bold]{title[:30]}[/bold]",
                        classes="browser-status",
                    )
                )
            except Exception:
                pass

    def refresh_tasks(self, engine: Any) -> None:
        for child in list(self.children):
            child.remove()

        self.mount(Static("📋 浏览器任务", classes="browser-section-title"))
        runner = engine.task_runner
        runs = runner.list_runs(limit=15)
        if not runs:
            self.mount(Static("[dim]暂无任务[/dim]", classes="browser-status"))
            return
        for r in runs:
            status = r.get("status", "?")
            instruction = (r.get("instruction") or "")[:25]
            style = "green" if status == "completed" else "red" if status == "failed" else "yellow"
            self.mount(
                Static(
                    f"[{style}]●[/{style}] {instruction}\n"
                    f"[dim]{status} · {(r.get('createdAt') or '')[:16]}[/dim]",
                    classes="task-entry",
                )
            )

    def show_scan_results(self, auditor: Any) -> None:
        for child in list(self.children):
            child.remove()

        self.mount(Static("🔒 安全扫描结果", classes="browser-section-title"))
        summary = auditor.get_summary()
        total = summary.get("totalFindings", 0)
        if total == 0:
            self.mount(Static("[green]✅ 未发现问题[/green]", classes="browser-status"))
            return

        lines = [f"总发现: {total}"]
        for sev in ("criticalCount", "highCount", "mediumCount", "lowCount"):
            label = sev.replace("Count", "")
            count = summary.get(sev, 0)
            if count:
                color = {"critical": "red", "high": "yellow", "medium": "cyan"}.get(label, "dim")
                lines.append(f"[{color}]{label}: {count}[/{color}]")
        self.mount(Static("\n".join(lines), classes="browser-status"))

        for f in auditor.get_results(min_severity="high")[:10]:
            severity = f.get("severity", "?")
            title = f.get("title", "?")
            cat = f.get("category", "?")
            color = "red" if severity == "critical" else "yellow"
            self.mount(
                Static(
                    f"[{color}][{severity}][/{color}] [{cat}] {title}",
                    classes="finding-entry",
                )
            )


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
        Binding("ctrl+b", "toggle_browser", "浏览器"),
    ]

    def __init__(self, engine: AgentEngine, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.engine = engine

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-area"):
            yield ChatPanel()
            yield HistoryPanel()
            yield BrowserPanel()
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
            case "/clear" | "/c":
                chat.clear()
                self.engine.reset()
                status.status_text = "会话已清除"
            case "/new" | "/n":
                if self.engine._messages and any(
                    m.get("role") == "user" for m in self.engine._messages
                ):
                    try:
                        import asyncio

                        asyncio.get_event_loop().run_until_complete(self.engine._save_session())
                    except Exception:
                        pass
                self.engine.reset()
                chat.clear()
                status.status_text = "新会话已开始"
            case "/help" | "/h":
                help_text = (
                    "## 可用命令\n"
                    "- `/help` — 显示帮助\n"
                    "- `/tools` — 列出可用工具\n"
                    "- `/model` — 显示模型配置\n"
                    "- `/usage` — 显示 token 用量\n"
                    "- `/history` — 查看历史会话列表\n"
                    "- `/load <id>` — 加载指定会话\n"
                    "- `/resume` — 继续最近的对话 (/r)\n"
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
                    "- `/jit <任务>` — JIT 即时工具生成\n"
                    "- `/pointer <路径>` — 语义指针(SPA)\n"
                    "- `/cooe <任务>` — 认知乱序执行(COOE)\n"
                    "- `/sleep` — 昼夜节律突触修剪\n"
                    "- `/entropy <文本>` — 耗散结构熵减\n"
                    "- `/ooda <路径>` — OODA 战场指挥\n"
                    "- `/probe <需求>` — 黑盒探测\n"
                    "- `/hook <目标>` — 逆向插桩\n"
                    "- `/vision <目标>` — AI 视觉数据提取\n"
                    "- `/spar <目标>` — 对抗自博弈 (GAN for Code)\n"
                    "- `/world <目标>` — 世界模型审计\n"
                    "- `/fusion <目标>` — 决定论-概率论融合审计\n"
                    "- `/consensus <目标>` — 拜占庭容错共识\n"
                    "- `/pid <目标>` — PID 闭环纠偏\n"
                    "- `/zkp <目标>` — 零知识证明与轨迹校验\n"
                    "- `/genesis <目标>` — 系统自重构与热演化\n"
                    "- `/macro <目标>` — 多智能体自由市场博弈\n"
                    "- `/cosmos <目标>` — 创世引擎审计\n"
                    "- `/watchdog <目标>` — 看门狗与灾难隔离\n"
                    "- `/supervisor <目标>` — Erlang 守护者树\n"
                    "- `/autopsy <目标>` — 执行迹切片与 Bug 解剖\n"
                    "- `/pursue <目标>` — 目标追踪（自主循环直至达成）\n"
                    "- `/browse <url>` — 打开 URL 并显示 SoM 元素\n"
                    "- `/autobrowse <任务>` — 自主浏览器任务\n"
                    "- `/browser-stop` — 停止浏览器\n"
                    "- `/browser-state` — 显示浏览器状态\n"
                    "- `/browser-screenshot` — 截取页面截图\n"
                    "- `/tasks` — 列出浏览器任务\n"
                    "- `/task <id>` — 查看任务详情\n"
                    "- `/task-reply <id> <指令>` — 回复等待中的任务\n"
                    "- `/task-abort <id>` — 中止任务\n"
                    "- `/task-resume <id>` — 恢复手动控制任务\n"
                    "- `/scan <url>` — 快速安全扫描\n"
                    "- `/scan-full <url>` — 完整 25 模块安全扫描\n"
                    "- `/scan-report [format]` — 导出扫描报告\n"
                    "- `/scan-baseline <url>` — 保存扫描为基线\n"
                    "- `/btemplate-list` — 列出浏览器任务模板\n"
                    "- `/btemplate-run <id>` — 从模板创建运行\n"
                    "- `/btemplate-compare <id>` — 比较模板运行结果\n"
                    "- `/clear` — 清除当前会话\n"
                    "- `/quit` — 退出\n"
                )
                chat.mount(Markdown(help_text, classes="agent-msg"))
            case "/tools" | "/t":
                tools = self.engine.tool_registry.all()
                lines = ["## 可用工具\n"]
                for t in tools:
                    lines.append(f"- **{t.name}** — {t.description}")
                chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
            case "/model" | "/m":
                info = (
                    f"## 模型配置\n"
                    f"- 默认: `{self.engine.router.resolve_model('capable')}`\n"
                    f"- 快速: `{self.engine.router.resolve_model('fast')}`\n"
                    f"- 推理: `{self.engine.router.resolve_model('reasoning')}`\n"
                )
                chat.mount(Markdown(info, classes="agent-msg"))
            case "/usage" | "/u":
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
                    self.action_toggle_history()
                else:
                    self._load_and_show_session(arg)
            case "/resume" | "/r":
                self._resume_latest()
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
            case "/jit":
                if not arg:
                    status.status_text = "用法: /jit <计算任务描述>"
                else:
                    self._run_analysis_mode("jit", arg)
            case "/pointer":
                if not arg:
                    status.status_text = "用法: /pointer <文件或目录路径>"
                else:
                    self._run_analysis_mode("pointer", arg)
            case "/cooe":
                if not arg:
                    status.status_text = "用法: /cooe <多步骤任务描述>"
                else:
                    self._run_analysis_mode("cooe", arg)
            case "/sleep":
                self._run_analysis_mode("sleep", arg or "")
            case "/entropy":
                if not arg:
                    status.status_text = "用法: /entropy <长文本或上下文>"
                else:
                    self._run_analysis_mode("entropy", arg)
            case "/ooda":
                if not arg:
                    status.status_text = "用法: /ooda <文件或目录路径>"
                else:
                    self._run_analysis_mode("ooda", arg)
            case "/probe":
                if not arg:
                    status.status_text = "用法: /probe <功能需求描述>"
                else:
                    self._run_analysis_mode("probe", arg)
            case "/vision":
                if not arg:
                    status.status_text = "用法: /vision <数据提取目标描述>"
                else:
                    self._run_analysis_mode("vision", arg)
            case "/spar":
                if not arg:
                    status.status_text = "用法: /spar <目标代码路径或功能描述>"
                else:
                    self._run_analysis_mode("spar", arg)
            case "/world":
                if not arg:
                    status.status_text = "用法: /world <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("world", arg)
            case "/fusion":
                if not arg:
                    status.status_text = "用法: /fusion <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("fusion", arg)
            case "/consensus":
                if not arg:
                    status.status_text = "用法: /consensus <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("consensus", arg)
            case "/pid":
                if not arg:
                    status.status_text = "用法: /pid <代码路径或流程描述>"
                else:
                    self._run_analysis_mode("pid", arg)
            case "/zkp":
                if not arg:
                    status.status_text = "用法: /zkp <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("zkp", arg)
            case "/genesis":
                if not arg:
                    status.status_text = "用法: /genesis <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("genesis", arg)
            case "/macro":
                if not arg:
                    status.status_text = "用法: /macro <任务或系统描述>"
                else:
                    self._run_analysis_mode("macro", arg)
            case "/cosmos":
                if not arg:
                    status.status_text = "用法: /cosmos <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("cosmos", arg)
            case "/watchdog":
                if not arg:
                    status.status_text = "用法: /watchdog <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("watchdog", arg)
            case "/supervisor":
                if not arg:
                    status.status_text = "用法: /supervisor <代码路径或系统描述>"
                else:
                    self._run_analysis_mode("supervisor", arg)
            case "/autopsy":
                if not arg:
                    status.status_text = "用法: /autopsy <代码路径或 Bug 描述>"
                else:
                    self._run_analysis_mode("autopsy", arg)
            case "/hook":
                if not arg:
                    status.status_text = "用法: /hook <逆向目标描述>"
                else:
                    self._run_analysis_mode("hook", arg)
            case "/pursue":
                if not arg:
                    status.status_text = "用法: /pursue <目标描述>"
                else:
                    self._run_pursue(arg)
            case "/browse":
                if not arg:
                    status.status_text = "用法: /browse <url>"
                else:
                    self._run_browse(arg)
            case "/autobrowse":
                if not arg:
                    status.status_text = "用法: /autobrowse <任务描述>"
                else:
                    self._run_autobrowse(arg)
            case "/browser-stop":
                self._run_browser_stop()
            case "/browser-state":
                self._run_browser_state()
            case "/browser-screenshot":
                self._run_browser_screenshot()
            case "/tasks":
                self._show_tasks()
            case "/task":
                if not arg:
                    status.status_text = "用法: /task <id>"
                else:
                    self._show_task_detail(arg)
            case "/task-reply":
                if not arg:
                    status.status_text = "用法: /task-reply <id> <指令>"
                else:
                    self._run_task_reply(arg)
            case "/task-abort":
                if not arg:
                    status.status_text = "用法: /task-abort <id>"
                else:
                    self._run_task_abort(arg)
            case "/task-resume":
                if not arg:
                    status.status_text = "用法: /task-resume <id>"
                else:
                    self._run_task_resume(arg)
            case "/scan":
                if not arg:
                    status.status_text = "用法: /scan <url>"
                else:
                    self._run_security_scan(arg, profile="quick")
            case "/scan-full":
                if not arg:
                    status.status_text = "用法: /scan-full <url>"
                else:
                    self._run_security_scan(arg, profile="full")
            case "/scan-report":
                self._run_scan_report(arg)
            case "/scan-baseline":
                if not arg:
                    status.status_text = "用法: /scan-baseline <url>"
                else:
                    self._run_scan_baseline(arg)
            case "/btemplate-list":
                self._show_btemplate_list()
            case "/btemplate-run":
                if not arg:
                    status.status_text = "用法: /btemplate-run <id>"
                else:
                    self._run_btemplate_run(arg)
            case "/btemplate-compare":
                if not arg:
                    status.status_text = "用法: /btemplate-compare <id>"
                else:
                    self._show_btemplate_compare(arg)
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
        msg_input = input_bar.query_one("#msg-input", Input)
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
            "jit": "analysis_jit",
            "pointer": "analysis_pointer",
            "cooe": "analysis_cooe",
            "sleep": "analysis_sleep",
            "entropy": "analysis_entropy",
            "ooda": "analysis_ooda",
            "probe": "analysis_probe",
            "hook": "analysis_hook",
            "vision": "analysis_vision",
            "spar": "analysis_spar",
            "world": "analysis_world",
            "fusion": "analysis_fusion",
            "consensus": "analysis_consensus",
            "pid": "analysis_pid",
            "zkp": "analysis_zkp",
            "genesis": "analysis_genesis",
            "macro": "analysis_macro",
            "cosmos": "analysis_cosmos",
            "watchdog": "analysis_watchdog",
            "supervisor": "analysis_supervisor",
            "autopsy": "analysis_autopsy",
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
            "jit": "🛠️ JIT 即时工具生成",
            "pointer": "🔗 语义指针架构 (SPA)",
            "cooe": "🔀 认知乱序执行 (COOE)",
            "sleep": "🧬 昼夜节律突触修剪",
            "entropy": "🌡️ 耗散结构熵减",
            "ooda": "⚔️ OODA 战场指挥",
            "probe": "🔦 黑盒探测 (Probe)",
            "hook": "💉 逆向插桩 (Hook)",
            "vision": "👁️ AI 视觉数据提取 (Vision)",
            "spar": "⚔️ 对抗性自博弈 (GAN for Code)",
            "world": "🌍 世界模型审计 (World Model)",
            "fusion": "⚖️ 决定论-概率论融合 (Fusion)",
            "consensus": "🏛️ 拜占庭容错共识 (Consensus)",
            "pid": "🎛️ PID 闭环纠偏 (Control Theory)",
            "zkp": "🔐 零知识证明与轨迹校验 (ZKP)",
            "genesis": "🧬 系统自重构与热演化 (Genesis)",
            "macro": "🏦 多智能体自由市场博弈 (Agentic Economy)",
            "cosmos": "🌌 创世引擎审计 (Cosmos)",
            "watchdog": "🛡️ 看门狗与灾难隔离 (Watchdog)",
            "supervisor": "⚙️ Erlang 守护者树 (Supervisor)",
            "autopsy": "🔬 执行迹切片与爆炸半径隔离 (DTS-CHE)",
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
            elif mode == "jit":
                result = await tool.execute(task=target)
            elif mode == "pointer":
                result = await tool.execute(target=target)
            elif mode == "cooe":
                result = await tool.execute(task=target)
            elif mode == "sleep":
                result = await tool.execute(session_context=target)
            elif mode == "entropy":
                result = await tool.execute(context=target)
            elif mode == "ooda":
                result = await tool.execute(target=target)
            elif mode == "probe":
                result = await tool.execute(task=target)
            elif mode == "hook":
                result = await tool.execute(task=target)
            elif mode == "vision":
                result = await tool.execute(task=target)
            elif mode == "spar":
                result = await tool.execute(task=target)
            elif mode == "world":
                result = await tool.execute(target=target)
            elif mode == "fusion":
                result = await tool.execute(target=target)
            elif mode == "consensus":
                result = await tool.execute(target=target)
            elif mode == "pid":
                result = await tool.execute(target=target)
            elif mode == "zkp":
                result = await tool.execute(target=target)
            elif mode == "genesis":
                result = await tool.execute(target=target)
            elif mode == "macro":
                result = await tool.execute(task=target)
            elif mode == "cosmos":
                result = await tool.execute(target=target)
            elif mode == "watchdog":
                result = await tool.execute(target=target)
            elif mode == "supervisor":
                result = await tool.execute(target=target)
            elif mode == "autopsy":
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

    def action_toggle_browser(self) -> None:
        browser = self.query_one(BrowserPanel)
        browser.show_panel = not browser.show_panel
        if browser.show_panel:
            browser.refresh_browser_state(self.engine)

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

        # 回放历史消息 — 用 _full_history（原始未截断数据）展示
        display_messages = self.engine._full_history or session.messages
        for m in display_messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                chat.add_user_message(content)
            elif role == "assistant":
                reasoning = m.get("reasoning_content", "")
                if reasoning:
                    chat.add_completed_thinking(reasoning)
                if content:
                    chat.mount(Markdown(content, classes="agent-msg"))
                # Show tool calls from this assistant message
                for tc in m.get("tool_calls", []):
                    tc_name = (
                        tc.get("function", {}).get("name", "tool")
                        if isinstance(tc, dict)
                        else "tool"
                    )
                    chat.mount(
                        Static(f"  ⚙ [dim]{tc_name}[/dim]", classes="tool-done")
                    )
            elif role == "tool":
                is_placeholder = "工具调用结果缺失" in (content or "")
                has_error = "error" in (content or "").lower()[:200]
                if is_placeholder:
                    icon = "⚠️"
                elif has_error:
                    icon = "❌"
                else:
                    icon = "✅"
                preview = (
                    (content[:120] + "…")
                    if content and len(content) > 120
                    else (content or "")
                )
                chat.mount(
                    Static(
                        f"  {icon} [dim]{preview}[/dim]",
                        classes="tool-done",
                    )
                )

        # 等待 Textual 完成布局计算后再滚动到底部
        self.call_after_refresh(chat._refresh_scroll)
        self.call_after_refresh(chat.scroll_end, animate=False)

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

    @work(exclusive=True, exit_on_error=False)
    async def _resume_latest(self) -> None:
        """加载最近一个历史会话."""
        status = self.query_one(StatusBar)
        try:
            sessions, _ = await self.engine.list_sessions(page=1, page_size=1)
        except Exception:
            status.status_text = "加载失败"
            return
        if not sessions:
            status.status_text = "暂无历史会话"
            return
        await self._load_and_show_session(sessions[0].id)

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

    @work(exclusive=True, exit_on_error=False)
    async def _run_pursue(self, goal: str) -> None:
        """执行目标追踪循环."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        chat.mount(
            Markdown(
                f"**🎯 目标追踪启动**\n\n{goal}",
                classes="agent-msg",
            )
        )

        tool = self.engine.tool_registry.get("pursue_goal")
        if not tool:
            chat.mount(
                Markdown(
                    "⚠️ 目标追踪工具未注册",
                    classes="agent-msg",
                )
            )
            return

        status.status_text = "🎯 目标追踪中..."
        chat.start_thinking()

        try:
            result = await tool.execute(goal=goal)
            chat.end_thinking()
            chat.mount(
                Markdown(
                    f"## 🎯 目标追踪报告\n\n{result}",
                    classes="agent-msg",
                )
            )
        except Exception as e:
            chat.end_thinking()
            chat.mount(
                Markdown(
                    f"⚠️ 目标追踪异常: {type(e).__name__}: {e}",
                    classes="agent-msg",
                )
            )
        finally:
            status.status_text = "就绪"
            input_bar = self.query_one(InputBar)
            msg_input = input_bar.query_one("#msg-input", Input)
            msg_input.focus()

    def action_show_tools(self) -> None:
        chat = self.query_one(ChatPanel)
        tools = self.engine.tool_registry.all()
        lines = ["## 可用工具\n"]
        for t in tools:
            lines.append(f"- **{t.name}** — {t.description}")
        chat.mount(Markdown("\n".join(lines), classes="agent-msg"))

    # --- Browser commands ---

    @work(exclusive=True, exit_on_error=False)
    async def _run_browse(self, url: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = f"🌐 导航到 {url}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            result = await self.engine._browser_session.goto(url.strip())
            elements = result.get("elements", [])
            lines = [f"## 🌐 页面已加载\n发现 {len(elements)} 个交互元素\n"]
            for el in elements[:20]:
                tag = el.get("tag", "?")
                label = el.get("label", el.get("text", ""))[:30]
                eid = el.get("id", "?")
                lines.append(f"- **[{eid}]** `{tag}` {label}")
            if len(elements) > 20:
                lines.append(f"\n... 还有 {len(elements) - 20} 个元素")
            chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
            browser = self.query_one(BrowserPanel)
            if browser.show_panel:
                browser.refresh_browser_state(self.engine)
        except Exception as e:
            chat.mount(Markdown(f"**导航失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_autobrowse(self, task: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = f"🤖 自主浏览: {task[:30]}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            runner = self.engine.task_runner
            run = runner.create_run(instruction=task.strip())
            run_id = run["id"]
            await runner.process_queue()
            updated = runner.get_run(run_id)
            if updated:
                s = updated.get("status", "unknown")
                summary = updated.get("summary", "")
                icon = "✅" if s == "completed" else "⚠️" if s == "failed" else "⏸"
                chat.mount(
                    Markdown(
                        f"## {icon} 任务 {s}\n\n{summary or '无摘要'}",
                        classes="agent-msg",
                    )
                )
            browser = self.query_one(BrowserPanel)
            if browser.show_panel:
                browser.refresh_tasks(self.engine)
        except Exception as e:
            chat.mount(Markdown(f"**任务失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_browser_stop(self) -> None:
        status = self.query_one(StatusBar)
        status.status_text = "🛑 停止浏览器..."
        try:
            await self.engine._browser_session.stop()
            status.status_text = "✅ 浏览器已停止"
        except Exception as e:
            status.status_text = f"❌ 停止失败: {e}"

    def _run_browser_state(self) -> None:
        browser = self.query_one(BrowserPanel)
        browser.show_panel = True
        browser.refresh_browser_state(self.engine)

    @work(exclusive=True, exit_on_error=False)
    async def _run_browser_screenshot(self) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "📸 截图中..."
        try:
            b64 = await self.engine._browser_session.screenshot_base64()
            import base64
            from pathlib import Path

            out = Path("screenshot.png")
            out.write_bytes(base64.b64decode(b64))
            chat.mount(
                Markdown(f"📸 截图已保存到 `{out}`", classes="agent-msg")
            )
        except Exception as e:
            chat.mount(Markdown(f"**截图失败**: {e}", classes="agent-msg"))
        finally:
            status.status_text = "就绪"

    def _show_tasks(self) -> None:
        browser = self.query_one(BrowserPanel)
        browser.show_panel = True
        browser.refresh_tasks(self.engine)

    @work(exclusive=True, exit_on_error=False)
    async def _show_task_detail(self, task_id: str) -> None:
        import json

        chat = self.query_one(ChatPanel)
        runner = self.engine.task_runner
        run = runner.get_run(task_id.strip())
        if not run:
            chat.mount(Markdown(f"**任务不存在**: {task_id}", classes="agent-msg"))
            return
        detail = json.dumps(run, indent=2, default=str, ensure_ascii=False)
        chat.mount(Markdown(f"```\n{detail[:1500]}\n```", classes="agent-msg"))

    @work(exclusive=True, exit_on_error=False)
    async def _run_task_reply(self, arg: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = arg.strip().split(maxsplit=1)
        if len(parts) < 2:
            status.status_text = "用法: /task-reply <id> <指令>"
            return
        run_id, instruction = parts
        status.status_text = f"回复任务 {run_id}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            runner = self.engine.task_runner
            await runner.reply_to_run(run_id, instruction)
            await runner.process_queue()
            updated = runner.get_run(run_id)
            s = updated.get("status", "?") if updated else "?"
            chat.mount(
                Markdown(f"**任务 {run_id}**: {s}", classes="agent-msg")
            )
        except Exception as e:
            chat.mount(Markdown(f"**回复失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    def _run_task_abort(self, task_id: str) -> None:
        chat = self.query_one(ChatPanel)
        runner = self.engine.task_runner
        run = runner.get_run(task_id.strip())
        if not run:
            chat.mount(Markdown(f"**任务不存在**: {task_id}", classes="agent-msg"))
            return
        runner.abort_run(task_id.strip(), reason="User requested")
        chat.mount(
            Markdown(f"**已中止任务**: {task_id}", classes="agent-msg")
        )

    @work(exclusive=True, exit_on_error=False)
    async def _run_task_resume(self, task_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = f"恢复任务 {task_id}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            runner = self.engine.task_runner
            await runner.resume_run(task_id.strip())
            await runner.process_queue()
            chat.mount(
                Markdown(f"**任务已恢复**: {task_id}", classes="agent-msg")
            )
        except Exception as e:
            chat.mount(Markdown(f"**恢复失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    # --- Security scan commands ---

    @work(exclusive=True, exit_on_error=False)
    async def _run_security_scan(self, url: str, profile: str = "quick") -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        label = "完整" if profile == "full" else "快速"
        status.status_text = f"🔒 {label}安全扫描: {url[:30]}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            if not self.engine._browser_session.page:
                await self.engine._browser_session.start(
                    {"source": "auto"}
                )
            await self.engine._browser_session.goto(url.strip())
            auditor = self.engine.security_auditor
            auditor.clear()
            await auditor.full_audit(profile=profile)
            summary = auditor.get_summary()
            total = summary.get("totalFindings", 0)
            critical = summary.get("criticalCount", 0)
            high = summary.get("highCount", 0)
            lines = [
                f"## 🔒 {label}安全扫描完成\n",
                f"- 总发现: **{total}**",
                f"- [red]严重: {critical}[/red]",
                f"- [yellow]高危: {high}[/yellow]",
                f"- 中危: {summary.get('mediumCount', 0)}",
                f"- 低危: {summary.get('lowCount', 0)}",
                "\n### 高危发现\n",
            ]
            for f in auditor.get_results(min_severity="high")[:15]:
                sev = f.get("severity", "?")
                title = f.get("title", "?")
                cat = f.get("category", "?")
                color = "red" if sev == "critical" else "yellow"
                lines.append(
                    f"- [{color}][{sev}][/{color}] [{cat}] {title}"
                )
            chat.mount(Markdown("\n".join(lines), classes="agent-msg"))
            browser = self.query_one(BrowserPanel)
            if browser.show_panel:
                browser.show_scan_results(auditor)
        except Exception as e:
            chat.mount(Markdown(f"**扫描失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_scan_report(self, arg: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        fmt = arg.strip() or "json"
        if fmt not in ("json", "sarif", "html"):
            status.status_text = "格式: json, sarif, html"
            return
        auditor = self.engine.security_auditor
        if not auditor.results:
            chat.mount(
                Markdown("**暂无扫描结果**，先执行 `/scan <url>`", classes="agent-msg")
            )
            return
        status.status_text = f"导出 {fmt} 报告..."
        try:
            import json
            from pathlib import Path

            result = await auditor.export_report(fmt=fmt)
            if fmt == "json":
                out = Path("security_report.json")
                out.write_text(
                    json.dumps(
                        result.get("data"), indent=2, ensure_ascii=False
                    ),
                    encoding="utf-8",
                )
            elif fmt == "sarif":
                out = Path("security_report.sarif")
                out.write_text(
                    json.dumps(result.get("sarif"), indent=2),
                    encoding="utf-8",
                )
            else:
                out = Path("security_report.html")
                out.write_text(
                    result.get("html", ""), encoding="utf-8"
                )
            chat.mount(
                Markdown(f"✅ 报告已保存到 `{out}`", classes="agent-msg")
            )
        except Exception as e:
            chat.mount(Markdown(f"**导出失败**: {e}", classes="agent-msg"))

    @work(exclusive=True, exit_on_error=False)
    async def _run_scan_baseline(self, url: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = f"📊 基线扫描: {url[:30]}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            if not self.engine._browser_session.page:
                await self.engine._browser_session.start(
                    {"source": "auto"}
                )
            await self.engine._browser_session.goto(url.strip())
            auditor = self.engine.security_auditor
            await auditor.full_audit(profile="standard")
            from pathlib import Path

            baseline_path = Path("security_baseline.json")
            auditor.save_baseline(str(baseline_path))
            chat.mount(
                Markdown(
                    f"✅ 基线已保存到 `{baseline_path}` "
                    f"({len(auditor.results)} 个发现)",
                    classes="agent-msg",
                )
            )
        except Exception as e:
            chat.mount(Markdown(f"**基线扫描失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    # --- Template commands ---

    def _show_btemplate_list(self) -> None:
        chat = self.query_one(ChatPanel)
        runner = self.engine.task_runner
        templates = runner.list_templates()
        if not templates:
            chat.mount(Markdown("**暂无浏览器任务模板**", classes="agent-msg"))
            return
        lines = ["## 浏览器任务模板\n"]
        for t in templates:
            tid = (t.get("id") or "")[:8]
            name = t.get("name", "")
            tp = t.get("timeoutPolicy", {})
            max_steps = tp.get("maxSteps", "?")
            rules = len(t.get("successRules", []))
            lines.append(
                f"- **{tid}** {name} (步骤:{max_steps} 规则:{rules})"
            )
        chat.mount(Markdown("\n".join(lines), classes="agent-msg"))

    @work(exclusive=True, exit_on_error=False)
    async def _run_btemplate_run(self, template_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        runner = self.engine.task_runner
        template = runner.get_template(template_id.strip())
        if not template:
            chat.mount(
                Markdown(f"**模板不存在**: {template_id}", classes="agent-msg")
            )
            return
        status.status_text = f"执行模板 {template_id}..."
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            run = runner.create_run_from_template(template_id.strip())
            run_id = run["id"]
            await runner.process_queue()
            updated = runner.get_run(run_id)
            s = updated.get("status", "?") if updated else "?"
            summary = updated.get("summary", "") if updated else ""
            chat.mount(
                Markdown(
                    f"**模板运行**: {s}\n\n{summary[:500]}",
                    classes="agent-msg",
                )
            )
        except Exception as e:
            chat.mount(Markdown(f"**模板运行失败**: {e}", classes="agent-msg"))
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            status.status_text = "就绪"

    def _show_btemplate_compare(self, template_id: str) -> None:
        import json

        chat = self.query_one(ChatPanel)
        runner = self.engine.task_runner
        comparison = runner.compare_template_runs(template_id.strip())
        if not comparison:
            chat.mount(Markdown("**无比较数据**", classes="agent-msg"))
            return
        detail = json.dumps(
            comparison, indent=2, default=str, ensure_ascii=False
        )
        chat.mount(
            Markdown(f"```\n{detail[:1500]}\n```", classes="agent-msg")
        )
