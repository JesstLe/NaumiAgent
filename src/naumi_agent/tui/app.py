"""NaumiAgent TUI — Textual 界面，支持流式输出与思考过程展示."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
from uuid import uuid4

import rich.markup
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.events import Key
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
    Static,
)

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.cli_completer import COMMANDS
from naumi_agent.clipboard import copy_or_save_transcript, strip_ansi
from naumi_agent.harness.conversation_queue_runtime import (
    ConversationQueueClaim,
    ConversationQueueClaimError,
    DurableConversationQueueAuthority,
)
from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.interaction_runtime import (
    DurableInteractionAuthorityClient,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreConflictError
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.streaming.sinks import CallbackEventSink
from naumi_agent.tools.base import ToolCall, ToolResult
from naumi_agent.tui.agent_control import AgentControlScreen
from naumi_agent.tui.completion_receipt import (
    completion_outcome_label,
    format_completion_receipt_text,
)
from naumi_agent.tui.runtime_inspector import RuntimeInspectorScreen
from naumi_agent.tui.semantic_markdown import SemanticMarkdown as Markdown
from naumi_agent.tui.workbench_overview import WorkbenchOverviewScreen
from naumi_agent.tui.working_indicator import (
    WORKING_INDICATOR_FRAME_COUNT,
    render_working_indicator_frame,
)
from naumi_agent.ui.budget import format_budget_detail
from naumi_agent.ui.code_excerpt import excerpt_markdown_code_blocks
from naumi_agent.ui.doctor import render_doctor_report, run_doctor
from naumi_agent.ui.doctor_health import (
    render_doctor_health_item_markdown,
    runtime_heartbeat_retention_health_item,
)
from naumi_agent.ui.history_screen import (
    build_history_snapshot,
    render_history_preview,
    render_session_delete_preview,
    render_session_retention_preview,
    render_session_retention_result,
    render_session_retention_worker,
)
from naumi_agent.ui.keybindings import (
    KEYBINDING_DEFINITIONS,
    KeybindingSet,
    build_keybindings,
    render_keybinding_help,
    to_textual_key,
)
from naumi_agent.ui.theme import UIStyleConfig, build_ui_style_config
from naumi_agent.ui.tool_activity import format_tool_prepare_status
from naumi_agent.user_interaction import (
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

logger = logging.getLogger(__name__)

_TERMINAL_NOISE_LOGGERS = ("litellm", "LiteLLM", "naumi_agent")
_API_FORMAT_LABELS = {
    "openai_chat": "OpenAI Chat",
    "openai_responses": "OpenAI Responses",
    "anthropic_messages": "Anthropic Messages",
    "google_genai": "Google GenAI",
    "azure_openai": "Azure OpenAI",
    "ollama": "Ollama",
    "legacy": "兼容模式",
}


def _format_api_format_label(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "未解析"
    return _API_FORMAT_LABELS.get(normalized, normalized)


async def _find_latest_user_session_id(
    engine: Any,
    *,
    page_size: int = 20,
) -> str | None:
    """Find the newest persisted session that contains real user messages."""
    page = 1
    checked = 0
    while True:
        sessions, total = await engine.list_sessions(page=page, page_size=page_size)
        if not sessions:
            return None
        for session in sessions:
            if any(m.get("role") == "user" for m in session.messages):
                return session.id
        checked += len(sessions)
        if checked >= total:
            return None
        page += 1


@contextlib.contextmanager
def _capture_tui_terminal_noise() -> Any:
    """Capture stray terminal writes and mute noisy model client loggers."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    previous_levels = {
        name: logging.getLogger(name).level for name in _TERMINAL_NOISE_LOGGERS
    }
    try:
        for name in _TERMINAL_NOISE_LOGGERS:
            logging.getLogger(name).setLevel(logging.ERROR)
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            yield stdout_buf, stderr_buf
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def _captured_terminal_text(stdout_buf: io.StringIO, stderr_buf: io.StringIO) -> str:
    return stdout_buf.getvalue() + stderr_buf.getvalue()


def _mount_captured_terminal_noise(
    chat: ChatPanel,
    text: str,
    *,
    debug_trace: Any | None = None,
) -> None:
    """Show captured third-party terminal output inside chat, collapsed."""
    if not text.strip():
        return
    if debug_trace is not None:
        debug_trace.output("tui.captured_terminal_noise", text)
    preview = text.strip()
    if len(preview) > 4000:
        preview = preview[:4000] + "\n... 已截断外部日志预览"
    content = Static(RichMarkdown(f"```text\n{preview}\n```"))
    panel = Collapsible(
        content,
        title=f"已拦截外部终端日志 ({len(text)} 字符)",
        collapsed=True,
        classes="tool-done",
    )
    chat.mount(panel)
    chat.scroll_end(animate=False)


def _format_tool_output_markdown(content: str) -> str:
    """Wrap tool output for TUI display while preserving diff/code fences."""
    text = content.strip("\n")
    if not text:
        return ""
    lines = text.splitlines()
    if "```diff" in text or "```" in text:
        return text
    if _looks_like_diff(lines):
        return f"```diff\n{text}\n```"
    if _looks_like_markdown(lines):
        return text
    return f"```\n{text}\n```"


def _looks_like_diff(lines: list[str]) -> bool:
    sample = [line for line in lines[:12] if line.strip()]
    return any(line.startswith(("---", "+++", "@@")) for line in sample) and any(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in sample
    )


def _looks_like_markdown(lines: list[str]) -> bool:
    for line in lines[:20]:
        text = line.strip()
        if not text:
            continue
        if text.startswith(("### ", "## ", "# ")):
            return True
        if text.startswith(("- ", "* ", "> ", "|", "```")):
            return True
        if text[:2].isdigit() and text[2:3] in {".", "、"}:
            return True
    return False


class _TuiSlashCommandFrontend:
    """Adapter passed to shared CLI slash command backend."""

    def __init__(self, app: NaumiApp) -> None:
        self._app = app

    def keybinding_help(self) -> str:
        return render_keybinding_help(self._app._keybindings, interface="tui")

    def debug_info(self) -> str:
        if self._app.debug_trace is not None:
            return self._app.debug_trace.describe()
        return "当前 TUI 未启用结构化调试日志。"

    def copy_transcript(self, scope: str = "all") -> None:
        self._app.action_copy_transcript(scope)

    def set_mode_status(self, mode: str) -> None:
        status = self._app.query_one(StatusBar)
        status.mode_text = mode

    def set_status(self, text: str) -> None:
        status = self._app.query_one(StatusBar)
        status.status_text = text

    async def update_harness_eval_batch(self, progress: Any) -> None:
        """Project factual repeated-Eval progress into the persistent TUI status bar."""
        labels = {
            "preparing": "准备",
            "evaluating": "评测",
            "persisting": "保存",
        }
        label = labels.get(str(progress.stage), str(progress.stage))
        status = self._app.query_one(StatusBar)
        status.status_text = (
            f"Eval Batch {label}: {progress.completed}/{progress.requested}"
            f" · 已保存 {progress.persisted} · {progress.batch_id}"
        )

    async def request_user_interaction(
        self,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        """Delegate guided Slash interactions to the same Textual modal host."""
        return await self._app.request_user_interaction(payload)

    def clear_output(self) -> None:
        self._app.query_one(ChatPanel).clear()
        self._app._clear_runtime_task_panels()
        status = self._app.query_one(StatusBar)
        status.status_text = "就绪"
        status.session_text = "会话:-"

    def set_todo_status(self, text: str) -> None:
        todo = self._app.query_one(TodoBar)
        todo.todo_text = text

_TUI_LOCAL_COMMANDS = (
    "/agents",
    "/cancel-queued",
    "/send-now",
    "/workbench",
)
_SLASH_SUGGESTIONS = SuggestFromList(
    [*_TUI_LOCAL_COMMANDS, *(cmd for cmd, _, _ in COMMANDS)],
    case_sensitive=True,
)


def _fuzzy_match(query: str, text: str) -> bool:
    if not query:
        return True
    qi = 0
    for ch in text:
        if qi < len(query) and ch.lower() == query[qi].lower():
            qi += 1
    return qi == len(query)


def _matches_slash_command(query: str, command: str) -> bool:
    """Match slash command by substring and fuzzy fallback."""
    if not command.startswith("/"):
        return False
    target = command[1:]
    if not query:
        return True
    if query.lower() in target.lower():
        return True
    return _fuzzy_match(query, target)


def _build_textual_bindings(keybindings: KeybindingSet) -> list[Binding]:
    bindings: list[Binding] = []
    for definition in KEYBINDING_DEFINITIONS:
        if "tui" not in definition.interfaces or definition.textual_action is None:
            continue
        for key in keybindings.keys_for(definition.action, interface="tui"):
            bindings.append(
                Binding(
                    to_textual_key(key),
                    definition.textual_action,
                    definition.description,
                    priority=definition.textual_priority,
                )
            )
    return bindings


class LoadSessionMessage(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class DeleteSessionMessage(Message):
    def __init__(self, session_id: str, title: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.title = title


class ArchiveSessionMessage(Message):
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
        self.debug_trace: Any | None = None
        self._response_text = ""
        self._response_widget: Markdown | Static | None = None
        self._thinking_text = ""
        self._thinking_content_widget: Static | None = None
        self._thinking_collapsible: Collapsible | None = None
        self._show_reasoning = False
        self._current_tool_widget: Static | None = None
        self._model_widget: Static | None = None

    def add_user_message(self, content: str) -> None:
        self._trace_output("tui.user_message", content)
        self.mount(Markdown(f"**你** {content}", classes="user-msg"))
        self.scroll_end(animate=False)

    def mount(self, *widgets: Any, before: Any = None, after: Any = None) -> Any:
        for widget in widgets:
            text = self._widget_debug_text(widget)
            payload = {
                "widget": type(widget).__name__,
                "classes": sorted(getattr(widget, "_classes", set())),
            }
            if text:
                self._trace_output("tui.mount", text, **payload)
            else:
                self._trace_event("tui.mount", payload)
        return super().mount(*widgets, before=before, after=after)

    # --- 思考过程 ---

    def start_thinking(self) -> None:
        self._trace_event("tui.thinking_start", {})
        self._thinking_text = ""
        self._thinking_content_widget = Static(_THINKING_LABEL, classes="thinking-content")
        self._thinking_collapsible = Collapsible(
            self._thinking_content_widget,
            title="💭 思考过程",
            classes="thinking-block",
            collapsed=self._show_reasoning,
        )
        self.mount(self._thinking_collapsible)
        self.scroll_end(animate=False)

    def add_thinking_chunk(self, content: str) -> None:
        self._trace_output("tui.thinking", content)
        self._thinking_text += content
        if self._show_reasoning and self._thinking_content_widget:
            self._thinking_content_widget.update(RichMarkdown(self._thinking_text))
            self.scroll_end(animate=False)

    def end_thinking(self) -> None:
        self._trace_event("tui.thinking_end", {"chars": len(self._thinking_text)})
        if self._thinking_collapsible and not self._show_reasoning:
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
        if not self._show_reasoning:
            self._trace_event("tui.thinking_restored_hidden", {"chars": len(content)})
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
        self._trace_event("tui.response_start", {})
        self._response_text = ""
        self._response_widget = Markdown("", classes="agent-msg")
        self.mount(self._response_widget)
        self.scroll_end(animate=False)

    def add_response_token(self, token: str) -> None:
        self._trace_output("tui.response_token", token)
        self._response_text += token
        if self._response_widget:
            self._response_widget.update(excerpt_markdown_code_blocks(self._response_text))
            self.scroll_end(animate=False)

    # --- 工具调用 ---

    def update_tool_prepare(self, text: str) -> None:
        self._trace_event("tui.tool_prepare", {"text": text})
        rendered = Text(f"  {text}", style="dim")
        if self._current_tool_widget is None:
            self._current_tool_widget = Static(rendered, classes="tool-running")
            self.mount(self._current_tool_widget)
        else:
            self._current_tool_widget.update(rendered)
        self.scroll_end(animate=False)

    def end_tool_prepare(self) -> None:
        self._trace_event("tui.tool_prepare_end", {})

    def start_tool(self, name: str) -> None:
        self._trace_event("tui.tool_start", {"name": name})
        safe_name = rich.markup.escape(name)
        text = Text.from_markup(f"  ⏳ [dim]{safe_name}[/dim]")
        if self._current_tool_widget is None:
            self._current_tool_widget = Static(text, classes="tool-running")
            self.mount(self._current_tool_widget)
        else:
            self._current_tool_widget.update(text)
        self.scroll_end(animate=False)

    def end_tool(
        self,
        name: str,
        status: str,
        duration_ms: int,
        content: str = "",
    ) -> None:
        self._trace_event(
            "tui.tool_end",
            {
                "name": name,
                "status": status,
                "duration_ms": duration_ms,
                "content_chars": len(content),
            },
        )
        icon = "✅" if status == "success" else "❌"
        safe_name = rich.markup.escape(name)
        done_text = Text.from_markup(f"  {icon} [dim]{safe_name} ({duration_ms}ms)[/dim]")
        if self._current_tool_widget is None:
            self.mount(Static(done_text, classes="tool-done"))
        else:
            self._current_tool_widget.update(done_text)
            self._current_tool_widget.set_class(False, "tool-running")
            self._current_tool_widget.set_class(True, "tool-done")
            self._current_tool_widget = None
        if content:
            self.mount(
                Markdown(_format_tool_output_markdown(content), classes="tool-output")
            )
        self.scroll_end(animate=False)

    # --- 清空 ---

    def clear(self) -> None:
        self._trace_event("tui.chat_cleared", {"children": len(self.children)})
        self.query(Static).remove()
        self.query(Markdown).remove()
        self.query(Collapsible).remove()
        self._response_text = ""
        self._response_widget = None
        self._thinking_text = ""
        self._thinking_content_widget = None
        self._current_tool_widget = None
        self._thinking_collapsible = None
        self._model_widget = None
        self.scroll_to(0, animate=False)

    # --- 模型信息 ---

    def show_model(self, model: str) -> None:
        """Display the model name before response starts."""
        self._trace_event("tui.model", {"model": model})
        self._model_widget = Static(
            Text.from_markup(f"[dim]  ⚙ {model}[/dim]"),
            classes="tool-done",
        )
        self.mount(self._model_widget)
        self.scroll_end(animate=False)

    # --- 结束 ---

    def finalize(
        self,
        turns: int,
        cost: float,
        tokens: int = 0,
        model: str = "",
        token_speed: float = 0.0,
        first_feedback: float = 0.0,
        first_model_chunk: float = 0.0,
        ttft: float = 0.0,
        duration: float = 0.0,
        cache_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        engine: Any = None,
    ) -> None:
        self._trace_event(
            "tui.response_finalized",
            {
                "turns": turns,
                "cost": cost,
                "tokens": tokens,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        self._response_text = ""
        self._response_widget = None

        # Line 1: Model | Turns | Tokens (speed) | Cache | TTFT | Duration
        line1_parts: list[str] = []
        if model:
            line1_parts.append(model)
        line1_parts.append(f"轮次: {turns}")
        if input_tokens > 0 or output_tokens > 0:
            tok_str = f"↑{input_tokens} ↓{output_tokens}"
        else:
            tok_str = f"Token: {tokens}"
        if token_speed > 0:
            tok_str += f" ({token_speed:.1f} tok/s)"
        line1_parts.append(tok_str)
        if cache_tokens > 0:
            line1_parts.append(f"缓存: {cache_tokens}")
        if first_feedback > 0:
            line1_parts.append(f"首反馈: {first_feedback:.1f}s")
        if first_model_chunk > 0:
            line1_parts.append(f"首包: {first_model_chunk:.1f}s")
        if ttft > 0:
            line1_parts.append(f"首字: {ttft:.1f}s")
        if duration > 0:
            line1_parts.append(f"耗时: {duration:.1f}s")
        self.mount(
            Static(
                Text.from_markup(f"[dim]{' | '.join(line1_parts)}[/dim]"),
                classes="usage-info",
            )
        )

        # Line 2: Context | Budget | Cost
        line2_parts: list[str] = []
        if engine:
            ctx = engine.get_context_info()
            ctx_pct = ctx["percentage"]
            used_k = ctx["used"] / 1000
            window_k = ctx["window"] / 1000
            line2_parts.append(f"上下文: {used_k:.0f}K/{window_k:.0f}K ({ctx_pct}%)")
            budget = engine.get_budget_info()
            line2_parts.append(f"预算: {format_budget_detail(budget)}")
        line2_parts.append(f"费用: ${cost:.4f}")
        self.mount(
            Static(
                Text.from_markup(f"[dim]{' | '.join(line2_parts)}[/dim]"),
                classes="usage-info",
            )
        )
        self.scroll_end(animate=False)

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int) -> None:
        self._trace_event(
            "tui.tool_call",
            {"tool_name": tool_name, "status": status, "duration_ms": duration_ms},
        )
        color = "green" if status == "success" else "red"
        self.mount(
            Static(
                f"  [dim]⚙ {tool_name} ({duration_ms}ms) [{color}]{status}[/{color}][/dim]",
                classes="tool-msg",
            )
        )

    def _trace_event(self, name: str, data: dict[str, Any]) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event(name, data)

    def _trace_output(self, sink: str, text: str, **extra: Any) -> None:
        if self.debug_trace is not None:
            self.debug_trace.output(sink, text, **extra)

    def _widget_debug_text(self, widget: Any) -> str:
        for attr in ("_initial_markdown", "_Static__content"):
            value = getattr(widget, attr, None)
            if value is not None:
                return str(value)
        return ""


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

    def clear_logs(self) -> None:
        for child in list(self.children):
            child.remove()


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
    search_query: reactive[str] = reactive("")

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

        title = "📋 历史会话"
        if self.search_query:
            title += f" · 搜索: {self.search_query}"
        self.mount(Static(title, classes="history-title"))

        try:
            sessions, total = await app.engine.list_sessions(
                page=1,
                page_size=50,
                query=self.search_query,
            )
        except Exception:
            self.mount(Static("[dim]加载失败[/dim]"))
            return

        if not sessions:
            self.mount(Static("[dim]暂无历史会话[/dim]"))
            return

        snapshot = build_history_snapshot(
            sessions,
            total=total,
            query=self.search_query,
            current_session_id=app.engine._session.id if app.engine._session else None,
            fallback_workspace=str(getattr(app.engine, "workspace_root", "")),
        )
        for item in snapshot.items:
            entry = SessionEntry(
                session_id=item.id,
                title=item.title,
                time_str=item.updated_at.strftime("%m-%d %H:%M"),
                msg_count=item.message_count,
                meta=(
                    f"{item.model} · Token {item.total_tokens} · "
                    f"${item.total_cost_usd:.4f}"
                ),
                workspace=item.workspace_root,
                git_branch=item.git_branch,
                summary=item.summary,
                is_current=item.is_current,
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

    def on_archive_session_message(self, event: ArchiveSessionMessage) -> None:
        app = self.app
        if isinstance(app, NaumiApp):
            app._archive_session(event.session_id)


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


class PermissionConfirmScreen(ModalScreen[str]):
    """工具权限确认弹窗."""

    DEFAULT_CSS = """
    PermissionConfirmScreen {
        align: center middle;
    }
    PermissionConfirmScreen > Container {
        width: 78;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: thick $warning 80%;
        background: $surface;
    }
    PermissionConfirmScreen Label {
        width: 1fr;
        margin: 0 0 1 0;
    }
    PermissionConfirmScreen .permission-preview {
        width: 1fr;
        max-height: 8;
        overflow-y: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $surface-lighten-2;
        background: $surface-darken-1;
    }
    PermissionConfirmScreen > Container > Horizontal {
        width: auto;
        height: auto;
    }
    PermissionConfirmScreen > Container > Horizontal > Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def compose(self) -> ComposeResult:
        from textual.widgets import Label

        tool_name = str(self.payload.get("tool_name", "?"))
        reason = str(self.payload.get("reason", "") or "该工具需要用户确认。")
        arguments = self.payload.get("arguments", {})
        preview = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        if len(preview) > 1200:
            preview = preview[:1200] + "\n..."
        with Container():
            yield Label(f"工具需要确认：[bold]{tool_name}[/bold]")
            yield Label(reason)
            yield Static(preview, classes="permission-preview")
            with Horizontal():
                yield Button("允许一次", variant="success", id="allow")
                yield Button("拒绝", variant="error", id="deny")
                yield Button("Bypass 全权限执行", variant="warning", id="bypass")

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.id or "deny"))


class UserInteractionScreen(ModalScreen[dict[str, str]]):
    """Structured choice and custom-input modal for model questions."""

    DEFAULT_CSS = """
    UserInteractionScreen {
        align: center middle;
    }
    UserInteractionScreen > Container {
        width: 78;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: thick $accent 80%;
        background: $surface;
    }
    UserInteractionScreen Label {
        width: 1fr;
        margin: 0 0 1 0;
    }
    UserInteractionScreen Button {
        width: 1fr;
        margin: 0 0 1 0;
    }
    UserInteractionScreen Input {
        width: 1fr;
        margin: 0 0 1 0;
    }
    UserInteractionScreen .interaction-help {
        color: $text-muted;
        margin: 0;
    }
    UserInteractionScreen .interaction-error {
        color: $error;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def compose(self) -> ComposeResult:
        from textual.widgets import Label

        with Container():
            yield Label(f"[bold]{self.payload.get('header') or '需要你的选择'}[/bold]")
            yield Label(str(self.payload.get("question") or "请选择一个选项。"))
            for index, option in enumerate(self.payload.get("options") or []):
                description = str(option.get("description") or "")
                suffix = f"\n[dim]{description}[/dim]" if description else ""
                yield Button(
                    f"{index + 1}. {option.get('label') or option.get('value')}{suffix}",
                    id=f"interaction-choice-{index}",
                )
            if self.payload.get("allow_custom", True):
                yield Button(
                    str(self.payload.get("custom_label") or "其他"),
                    id="interaction-custom",
                )
            custom_input = Input(
                placeholder="输入自定义答案后按 Enter",
                id="interaction-custom-input",
                max_length=4_000,
            )
            custom_input.display = False
            yield custom_input
            error = Static("", id="interaction-error", classes="interaction-error")
            error.display = False
            yield error
            yield Static(
                "↑/↓ 选择 · Enter 确认 · Ctrl+C 取消运行",
                classes="interaction-help",
            )

    def on_mount(self) -> None:
        buttons = list(self.query(Button))
        if buttons:
            buttons[0].focus()

    def on_key(self, event: Key) -> None:
        custom_input = self.query_one("#interaction-custom-input", Input)
        if custom_input.display:
            if event.key == "escape":
                custom_input.display = False
                custom_input.value = ""
                buttons = list(self.query(Button))
                if buttons:
                    buttons[0].focus()
                event.stop()
            return
        if event.key not in {"up", "down"}:
            return
        buttons = list(self.query(Button))
        if not buttons:
            return
        focused = self.focused
        index = buttons.index(focused) if focused in buttons else 0
        offset = -1 if event.key == "up" else 1
        buttons[(index + offset) % len(buttons)].focus()
        event.stop()

    @on(Button.Pressed)
    def on_interaction_button(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id or "")
        if button_id == "interaction-custom":
            custom_input = self.query_one("#interaction-custom-input", Input)
            custom_input.display = True
            custom_input.focus()
            return
        if not button_id.startswith("interaction-choice-"):
            return
        index = int(button_id.rsplit("-", 1)[-1])
        options = self.payload.get("options") or []
        if 0 <= index < len(options):
            self.dismiss({"kind": "option", "value": str(options[index].get("value") or "")})

    @on(Input.Submitted, "#interaction-custom-input")
    def on_custom_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        error = self.query_one("#interaction-error", Static)
        if not text:
            error.update("自定义答案不能为空。")
            error.display = True
            return
        self.dismiss({"kind": "custom", "custom_text": text})


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

    SessionEntry .archive-btn {
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
        meta: str = "",
        workspace: str = "",
        git_branch: str = "",
        summary: str = "",
        is_current: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.session_id = session_id
        self.title_text = title
        self._entry_title = title
        self._entry_time = time_str
        self._entry_count = msg_count
        self._entry_meta = meta
        self._entry_workspace = workspace
        self._entry_git_branch = git_branch
        self._entry_summary = summary
        if is_current:
            self.add_class("current")

    def compose(self) -> ComposeResult:
        workspace = Path(self._entry_workspace).name if self._entry_workspace else "未知工作区"
        git = self._entry_git_branch or "未知分支"
        yield Static(
            f"[dim]{self.session_id}[/dim]\n"
            f"{self._entry_title}\n"
            f"[dim]{self._entry_time} · {self._entry_count}条消息 · {self._entry_meta}[/dim]\n"
            f"[dim]{workspace} · {git}[/dim]\n"
            f"[dim]{self._entry_summary}[/dim]",
            classes="session-info",
        )
        yield Button("A", variant="warning", classes="archive-btn")
        yield Button("✕", variant="error", classes="delete-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if "archive-btn" in event.button.classes:
            self.post_message(ArchiveSessionMessage(self.session_id, self.title_text))
        else:
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
    _slash_candidates: list[str] = []
    _slash_candidate_index: int = -1

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slash_candidates = []
        self._slash_candidate_index = -1

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

    def _build_slash_candidates(self, query: str) -> list[str]:
        candidates = [
            cmd
            for cmd in [*_TUI_LOCAL_COMMANDS, *(item[0] for item in COMMANDS)]
            if _matches_slash_command(query, cmd)
        ]
        return sorted(candidates)

    @on(Input.Changed)
    def on_input_changed(self, event: Input.Changed) -> None:
        value = event.value or ""
        if not value.startswith("/") or " " in value:
            self._slash_candidates = []
            self._slash_candidate_index = -1
            return

        query = value[1:]
        candidates = self._build_slash_candidates(query)
        self._slash_candidates = candidates
        if not candidates:
            self._slash_candidate_index = -1
            return

        normalized = value.lower()
        try:
            self._slash_candidate_index = [cmd.lower() for cmd in candidates].index(
                normalized
            )
        except ValueError:
            self._slash_candidate_index = 0

    def on_key(self, event: Key) -> None:
        if event.key not in {"up", "down"}:
            return

        input_widget = self.query_one("#msg-input", Input)
        if self.app.focused is not input_widget:
            return
        if not self._slash_candidates:
            return

        if event.key == "down":
            if self._slash_candidate_index < 0:
                self._slash_candidate_index = 0
            else:
                self._slash_candidate_index = (self._slash_candidate_index + 1) % len(
                    self._slash_candidates
                )
        else:
            if self._slash_candidate_index < 0:
                self._slash_candidate_index = len(self._slash_candidates) - 1
            else:
                self._slash_candidate_index = (self._slash_candidate_index - 1) % len(
                    self._slash_candidates
                )

        input_widget.value = self._slash_candidates[self._slash_candidate_index]
        event.prevent_default()
        event.stop()


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

        self.mount(Static("📋 任务面板", classes="browser-section-title"))
        self.mount(Static("[dim]正在读取任务状态...[/dim]", classes="browser-status"))

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
    mode_text: reactive[str] = reactive("default")
    session_text: reactive[str] = reactive("会话:-")
    reasoning_text: reactive[str] = reactive("off")
    effort_text: reactive[str] = reactive("auto")
    debug_trace: Any | None = None

    def watch_mode_text(self, text: str) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("tui.runtime_mode", {"mode": text})
        self._refresh()

    def watch_status_text(self, text: str) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("tui.status", {"text": text})
        self._refresh()

    def watch_reasoning_text(self, _text: str) -> None:
        self._refresh()

    def watch_effort_text(self, _text: str) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.update(
            f"mode: {self.mode_text} | "
            f"思考文本: {self.reasoning_text} | "
            f"强度: {self.effort_text} | "
            f"{self.status_text} | {self.session_text}"
        )


class TodoBar(Static):
    """底部常驻 todo 栏；无未完成任务时隐藏."""

    DEFAULT_CSS = """
    TodoBar {
        height: 1;
        background: $boost;
        color: $warning;
        padding: 0 1;
    }
    TodoBar.hidden {
        display: none;
    }
    """

    todo_text: reactive[str] = reactive("")
    debug_trace: Any | None = None

    def on_mount(self) -> None:
        self.set_class(True, "hidden")

    def watch_todo_text(self, text: str) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("tui.todo_status", {"text": text})
        self.set_class(not bool(text), "hidden")
        self.update(text)


class Spinner(Static):
    """动画旋转指示器."""

    _frame: reactive[int] = reactive(0)
    _active: reactive[bool] = reactive(False)

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick, pause=True)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % WORKING_INDICATOR_FRAME_COUNT

    def watch__frame(self, idx: int) -> None:
        if self._active:
            self.update(render_working_indicator_frame(idx))

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

    .tool-output {
        background: $surface-darken-1;
        border: round cyan;
        padding: 0 1;
        margin: 0 2 1 2;
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

    BINDINGS = _build_textual_bindings(build_keybindings())

    def __init__(
        self,
        engine: AgentEngine,
        debug_trace: Any | None = None,
        keybindings: KeybindingSet | None = None,
        style_config: UIStyleConfig | None = None,
        show_reasoning: bool = False,
        **kwargs: Any,
    ) -> None:
        self._keybindings = keybindings or build_keybindings()
        self._style_config = style_config or build_ui_style_config()
        self.BINDINGS = _build_textual_bindings(self._keybindings)
        self.CSS = self.CSS + "\n" + self._style_config.tui_css()
        super().__init__(**kwargs)
        self.engine = engine
        self.debug_trace = debug_trace
        self._show_reasoning = show_reasoning
        self._slash_frontend = _TuiSlashCommandFrontend(self)
        self._interaction_lock = asyncio.Lock()
        self._active_interaction_ids: set[str] = set()
        self._interaction_records: dict[str, HarnessInteractionRecord] = {}
        self._interaction_owner_tasks: dict[str, asyncio.Task[None]] = {}
        self._interaction_owner_id = f"tui-{uuid4().hex}"
        self._interaction_authority_client: (
            DurableInteractionAuthorityClient | None
        ) = None
        self._interaction_authority_store: object | None = None
        self._conversation_session_lock = asyncio.Lock()
        self._queue_owner_id = f"queue-tui-{uuid4().hex}"
        self._queue_authorities: dict[str, DurableConversationQueueAuthority] = {}
        self._active_queue_authority: DurableConversationQueueAuthority | None = None
        self._active_queue_claim: ConversationQueueClaim | None = None
        self._queue_claim_renew_task: asyncio.Task[None] | None = None
        self._queue_claim_lost = False
        self._agent_busy = False
        self._agent_worker: Any | None = None
        self.engine.set_permission_confirmer(self.confirm_permission)
        self.engine.set_user_interaction_handler(self.request_user_interaction)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-area"):
            yield ChatPanel()
            yield HistoryPanel()
            yield BrowserPanel()
            yield ActivityPanel()
        yield TodoBar()
        yield InputBar()
        yield Spinner()
        yield StatusBar()
        yield Footer()

    async def on_unmount(self) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("tui.unmount", {})
            self.debug_trace.close()
        owner_tasks = tuple(self._interaction_owner_tasks.values())
        for task in owner_tasks:
            task.cancel()
        if owner_tasks:
            await asyncio.gather(*owner_tasks, return_exceptions=True)
        self._interaction_owner_tasks.clear()
        self._interaction_records.clear()
        await self._stop_queue_claim_renewal()
        await self.engine.shutdown()

    def on_mount(self) -> None:
        """Initialize UI on startup."""
        chat = self.query_one(ChatPanel)
        chat.debug_trace = self.debug_trace
        chat._show_reasoning = self._show_reasoning
        status = self.query_one(StatusBar)
        status.debug_trace = self.debug_trace
        current_session = self.engine._session.id if self.engine._session else None
        status.session_text = f"会话:{current_session[:8]}" if current_session else "会话:-"
        self._sync_model_control_status(status)
        todo = self.query_one(TodoBar)
        todo.debug_trace = self.debug_trace
        if self.debug_trace is not None:
            self.debug_trace.event("tui.mount", {"title": self.TITLE})
        self._update_git_title()
        self._show_startup_status()
        self._recover_session_reconciliations()
        self._recover_durable_interactions()

    @work(exclusive=False, exit_on_error=False)
    async def _recover_session_reconciliations(self) -> None:
        status = self.query_one(StatusBar)
        try:
            results = await self.engine.start_long_running_services()
        except Exception:
            status.status_text = (
                "会话协调恢复失败，周期清理未启动；请运行 /doctor 查看诊断"
            )
            return
        patch_status_getter = getattr(
            self.engine,
            "evolution_patch_recovery_status",
            None,
        )
        patch_status = patch_status_getter() if callable(patch_status_getter) else {}
        patch_total = int(patch_status.get("total", 0)) if patch_status else 0
        patch_completed = int(patch_status.get("completed", 0)) if patch_status else 0
        patch_failed = int(patch_status.get("failed", 0)) if patch_status else 0
        patch_deferred = int(patch_status.get("deferred", 0)) if patch_status else 0
        patch_multi = int(patch_status.get("multi_file_total", 0)) if patch_status else 0
        if not results and patch_total == 0:
            return
        completed = sum(
            result.outcome is ReconciliationCoordinatorOutcome.COMPLETED
            for result in results
        )
        parts: list[str] = []
        if patch_total:
            multi_text = f" · 多文件 {patch_multi}" if patch_multi else ""
            parts.append(
                f"实验补丁恢复: {patch_completed}/{patch_total} 完成"
                f"{multi_text} · 失败 {patch_failed} · 延后 {patch_deferred}"
            )
        if results:
            parts.append(f"会话协调恢复: {completed}/{len(results)} 完成")
        status.status_text = " | ".join(parts)

    async def confirm_permission(self, payload: dict[str, Any]) -> str:
        """Show a modal confirmation dialog for sensitive tools."""
        if self.debug_trace is not None:
            self.debug_trace.event(
                "tui.permission_confirm_prompt",
                {
                    "tool_name": payload.get("tool_name"),
                    "risk_level": payload.get("risk_level"),
                    "permission_mode": payload.get("permission_mode"),
                },
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def on_choice(choice: str | None) -> None:
            if not future.done():
                future.set_result(str(choice or "deny"))

        self.push_screen(PermissionConfirmScreen(payload), on_choice)
        result = await future
        choice = str(result or "deny")
        if choice == "bypass":
            self.query_one(StatusBar).mode_text = "bypass"
        if self.debug_trace is not None:
            self.debug_trace.event(
                "tui.permission_confirm_choice",
                {"tool_name": payload.get("tool_name"), "choice": choice},
            )
        return choice

    async def request_user_interaction(self, payload: dict[str, Any]) -> dict[str, str]:
        """Persist, serialize, and fence one model question in the fallback TUI."""
        request = normalize_interaction_request(payload)
        interaction_id = str(payload.get("_interaction_id") or "").strip()
        if not re.fullmatch(r"ask-[A-Za-z0-9._:-]{1,128}", interaction_id):
            interaction_id = f"ask-{uuid4().hex}"
        authority = self._interaction_authority()
        record: HarnessInteractionRecord | None = None
        if authority is not None:
            session_id = str(
                getattr(getattr(self.engine, "_session", None), "id", "") or ""
            )
            record = await authority.create(
                request=request,
                interaction_id=interaction_id,
                subject_kind=str(
                    payload.get("_durable_subject_kind") or "runtime"
                ),
                subject_id=str(
                    payload.get("_durable_subject_id")
                    or session_id
                    or "runtime-sessionless"
                ),
                session_id=session_id,
                agent_name=str(payload.get("agent_name") or "main"),
            )
        self._active_interaction_ids.add(interaction_id)
        if record is not None:
            self._start_interaction_owner_renewal(record)
        try:
            pursuit_begin = payload.get("_pursuit_begin")
            if callable(pursuit_begin):
                await pursuit_begin(interaction_id, request.to_public_dict())
            async with self._interaction_lock:
                record = self._interaction_records.get(interaction_id, record)
                raw_response = await self._present_user_interaction(
                    {
                        **request.to_public_dict(),
                        "request_id": interaction_id,
                        "expires_at": record.expires_at if record else "",
                    },
                    record=record,
                )
            if authority is None or record is None:
                return raw_response
            record = await self._stop_interaction_owner_renewal(
                interaction_id,
                fallback=record,
            )
            try:
                record, response = await authority.answer(
                    record=record,
                    response=raw_response,
                )
            except Exception as exc:
                self._set_interaction_status(
                    "答案未能提交到持久交互 authority；请重新打开 TUI 后重试。"
                )
                raise UserInteractionUnavailableError(
                    "答案未能提交到持久交互 authority"
                ) from exc
            pursuit_resolve = payload.get("_pursuit_resolve")
            if callable(pursuit_resolve):
                try:
                    await pursuit_resolve(interaction_id, response)
                except Exception as exc:
                    self._set_interaction_status(
                        "答案已持久化，但目标 checkpoint 尚未确认；"
                        "请执行 /pursue resume。"
                    )
                    raise UserInteractionUnavailableError(
                        "答案已持久化，请执行 /pursue resume"
                    ) from exc
            return response
        except TimeoutError as exc:
            if authority is not None and record is not None:
                record = await self._stop_interaction_owner_renewal(
                    interaction_id,
                    fallback=record,
                )
                try:
                    await authority.expire(record=record, now=record.expires_at)
                except Exception:
                    logger.warning("TUI durable interaction timeout commit failed")
            raise UserInteractionUnavailableError("用户交互等待已超时") from exc
        finally:
            if interaction_id in self._interaction_owner_tasks:
                await self._stop_interaction_owner_renewal(
                    interaction_id,
                    fallback=record,
                )
            self._active_interaction_ids.discard(interaction_id)

    def _interaction_authority(
        self,
    ) -> DurableInteractionAuthorityClient | None:
        harness_service = getattr(self.engine, "harness_service", None)
        store = getattr(harness_service, "store", None)
        if store is None:
            self._interaction_authority_client = None
            self._interaction_authority_store = None
            return None
        if (
            self._interaction_authority_client is None
            or self._interaction_authority_store is not store
        ):
            self._interaction_authority_client = DurableInteractionAuthorityClient(
                store=store,
                workspace_root=self.engine.workspace_root,
                owner_id=self._interaction_owner_id,
            )
            self._interaction_authority_store = store
        return self._interaction_authority_client

    def _set_interaction_status(self, message: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one(StatusBar).status_text = message

    def _start_interaction_owner_renewal(
        self,
        record: HarnessInteractionRecord,
    ) -> None:
        interaction_id = record.interaction_id
        authority = self._interaction_authority()
        if authority is None:
            return
        self._interaction_records[interaction_id] = record
        current = self._interaction_owner_tasks.get(interaction_id)
        if current is not None and not current.done():
            return

        async def keep_owner_live() -> None:
            failures = 0
            try:
                while interaction_id in self._active_interaction_ids:
                    await asyncio.sleep(authority.owner_renew_interval_seconds)
                    latest = self._interaction_records.get(interaction_id)
                    if latest is None or latest.state != "pending":
                        return
                    try:
                        self._interaction_records[interaction_id] = (
                            await authority.renew(record=latest)
                        )
                        failures = 0
                    except Exception:
                        failures += 1
                        try:
                            current_record = await authority.store.get_interaction(
                                workspace_root=self.engine.workspace_root,
                                interaction_id=interaction_id,
                            )
                        except Exception:
                            current_record = None
                        if (
                            current_record is not None
                            and current_record.state == "pending"
                            and current_record.owner_id == authority.owner_id
                        ):
                            self._interaction_records[interaction_id] = current_record
                        elif current_record is not None or failures >= 3:
                            self._set_interaction_status(
                                "持久交互 owner 续租失败；答案提交前将再次核对。"
                            )
                            return
                        await asyncio.sleep(float(min(failures, 3)))
            except asyncio.CancelledError:
                raise
            finally:
                if self._interaction_owner_tasks.get(interaction_id) is asyncio.current_task():
                    self._interaction_owner_tasks.pop(interaction_id, None)

        self._interaction_owner_tasks[interaction_id] = asyncio.create_task(
            keep_owner_live(),
            name=f"naumi-tui-interaction-owner-{interaction_id}",
        )

    async def _stop_interaction_owner_renewal(
        self,
        interaction_id: str,
        *,
        fallback: HarnessInteractionRecord,
    ) -> HarnessInteractionRecord:
        owner_task = self._interaction_owner_tasks.pop(interaction_id, None)
        if owner_task is not None and owner_task is not asyncio.current_task():
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
        return self._interaction_records.pop(interaction_id, fallback)

    async def _present_user_interaction(
        self,
        payload: dict[str, Any],
        *,
        record: HarnessInteractionRecord | None,
    ) -> dict[str, str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()

        def on_answer(answer: dict[str, str] | None) -> None:
            if not future.done():
                future.set_result(dict(answer or {}))

        screen = UserInteractionScreen(payload)
        self.push_screen(screen, on_answer)
        timeout = (
            DurableInteractionAuthorityClient.remaining_timeout_seconds(record)
            if record is not None
            else None
        )
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            if screen.is_mounted:
                screen.dismiss(None)
            raise

    @work(exclusive=False, exit_on_error=False)
    async def _recover_durable_interactions(self) -> None:
        """Replay authority-owned pending questions without resuming a model run."""
        authority = self._interaction_authority()
        if authority is None:
            return
        recovery_failures = 0
        while self.is_running:
            try:
                recovery = await authority.recover_pending(limit=50)
            except Exception:
                logger.warning("TUI durable interaction recovery failed")
                self._set_interaction_status(
                    "持久交互恢复失败；请运行 /doctor 后重试。"
                )
                recovery_failures += 1
                if recovery_failures >= 3:
                    return
                await asyncio.sleep(float(2 ** (recovery_failures - 1)))
                continue
            recovery_failures = 0
            claimed = tuple(
                record for record in recovery.claimed
                if record.interaction_id not in self._active_interaction_ids
            )
            for record in claimed:
                self._active_interaction_ids.add(record.interaction_id)
                self._start_interaction_owner_renewal(record)
            for record in claimed:
                try:
                    async with self._interaction_lock:
                        record = self._interaction_records.get(
                            record.interaction_id,
                            record,
                        )
                        raw_response = await self._present_user_interaction(
                            {
                                "request_id": record.interaction_id,
                                **record.request().to_public_dict(),
                                "expires_at": record.expires_at,
                            },
                            record=record,
                        )
                    record = await self._stop_interaction_owner_renewal(
                        record.interaction_id,
                        fallback=record,
                    )
                    await authority.answer(record=record, response=raw_response)
                    self._set_interaction_status(
                        f"已恢复并保存交互 {record.interaction_id}；"
                        "若属于目标追踪，请执行 /pursue resume。"
                    )
                except TimeoutError:
                    record = await self._stop_interaction_owner_renewal(
                        record.interaction_id,
                        fallback=record,
                    )
                    try:
                        await authority.expire(
                            record=record,
                            now=record.expires_at,
                        )
                    except Exception:
                        logger.warning("TUI replay interaction timeout commit failed")
                except Exception:
                    logger.warning("TUI replay interaction answer failed")
                    self._set_interaction_status(
                        "恢复问题的答案未能持久化；重开 TUI 后可重试。"
                    )
                finally:
                    if record.interaction_id in self._interaction_owner_tasks:
                        await self._stop_interaction_owner_renewal(
                            record.interaction_id,
                            fallback=record,
                        )
                    self._active_interaction_ids.discard(record.interaction_id)
            if recovery.retry_after_seconds is None:
                return
            await asyncio.sleep(max(0.05, recovery.retry_after_seconds + 0.05))

    def _show_startup_status(self) -> None:
        """Show model, budget, and context info in status bar on startup."""
        try:
            status = self.query_one(StatusBar)
            runtime_mode = getattr(self.engine, "runtime_mode", None)
            status.mode_text = getattr(runtime_mode, "value", str(runtime_mode or "default"))
            model = self.engine.router.resolve_model("capable")
            model_display = model
            provider_display = ""
            try:
                identity = self.engine.router.get_runtime_identity(model)
                provider = identity.provider or "未解析"
                api_format = _format_api_format_label(identity.api_format)
                provider_display = f"提供方: {provider}/{api_format} | "
                if identity.upstream_model and identity.upstream_model != model:
                    model_display = f"{model} → {identity.upstream_model}"
            except Exception:
                pass
            budget = self.engine.get_budget_info()
            ctx = self.engine.get_context_info()
            window_k = ctx["window"] / 1000
            workspace_root = getattr(self.engine, "workspace_root", Path.cwd())
            status.status_text = (
                f"{provider_display}模型: {model_display} | "
                f"工作区: {workspace_root} | "
                f"上下文: 0K/{window_k:.0f}K | "
                f"预算: {format_budget_detail(budget)}"
            )
        except Exception:
            pass

    def _sync_model_control_status(self, status: StatusBar | None = None) -> None:
        """Refresh persistent reasoning visibility and effort from authoritative state."""
        status = status or self.query_one(StatusBar)
        status.reasoning_text = "on" if self._show_reasoning else "off"
        try:
            effort = self.engine.router.get_reasoning_effort_status()
            status.effort_text = effort.effective.value
        except Exception:
            status.effort_text = "auto"

    def _update_git_title(self) -> None:
        """Update sub-title with git branch info."""
        from naumi_agent.main import _get_git_info

        git = _get_git_info()
        if git["branch"]:
            tag = git["branch"] + ("*" if git["dirty"] else "")
            self.sub_title = f"📂 {tag} — 通用智能 Agent"

    @work(exclusive=True, exit_on_error=False)
    async def _auto_resume_latest(self) -> None:
        """Auto-load the most recent session with user conversation."""
        try:
            session_id = await _find_latest_user_session_id(self.engine)
        except Exception:
            return
        if session_id is None:
            return
        await self._load_and_show_session(session_id)

    def on_user_input_message(self, msg: UserInputMessage) -> None:
        text = msg.content.strip()
        if self.debug_trace is not None:
            self.debug_trace.input("tui.input", msg.content)

        # 斜杠命令拦截
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            if command.lower() == "/cancel-queued":
                self._cancel_queued_conversation(argument.strip())
                return
            if self._agent_busy and command.lower() == "/send-now":
                self._promote_queued_conversation(argument.strip())
                return
            if self._agent_busy and command.lower() not in {"/q", "/quit", "/exit"}:
                self.query_one(ChatPanel).mount(
                    Markdown(
                        "当前模型仍在运行；普通消息可继续排队，"
                        "使用 `/send-now [request-id]` 调整下一条消息。",
                        classes="agent-msg",
                    )
                )
                return
            self._handle_slash_command(text)
            return

        if self._agent_busy:
            self._enqueue_queued_conversation(msg.content)
            return

        chat = self.query_one(ChatPanel)
        chat.add_user_message(msg.content)
        status = self.query_one(StatusBar)
        status.status_text = "思考中..."
        self._agent_busy = True
        self.query_one(Spinner)._active = True
        self._agent_worker = self._run_agent(msg.content)

    async def _ensure_conversation_session(self, text: str) -> str:
        async with self._conversation_session_lock:
            session = getattr(self.engine, "_session", None)
            if session is None:
                title = next(
                    (line.strip() for line in text.splitlines() if line.strip()),
                    "新任务",
                )[:80]
                session = await self.engine.get_or_create_session(title=title)
            return str(getattr(session, "id", "") or "")

    def _conversation_queue_authority(
        self,
        session_id: str,
    ) -> DurableConversationQueueAuthority | None:
        normalized = session_id.strip()
        harness_service = getattr(self.engine, "harness_service", None)
        store = getattr(harness_service, "store", None)
        if not normalized or not isinstance(store, HarnessStore):
            return None
        existing = self._queue_authorities.get(normalized)
        if existing is not None and existing.store is store:
            return existing
        authority = DurableConversationQueueAuthority(
            store=store,
            workspace_root=self.engine.workspace_root,
            session_id=normalized,
            owner_id=self._queue_owner_id,
        )
        self._queue_authorities[normalized] = authority
        return authority

    def _current_conversation_queue_authority(
        self,
    ) -> DurableConversationQueueAuthority | None:
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        return self._conversation_queue_authority(session_id)

    @work(exclusive=False, exit_on_error=False)
    async def _enqueue_queued_conversation(self, text: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            session_id = await self._ensure_conversation_session(text)
            authority = self._conversation_queue_authority(session_id)
            if authority is None:
                raise RuntimeError("durable queue unavailable")
            item = await authority.enqueue(
                request_id=f"tui-{uuid4().hex}",
                text=text,
                client_id=self._queue_owner_id,
            )
        except HarnessStoreConflictError as exc:
            message = str(exc)
            status.status_text = message
            chat.mount(Markdown(f"**排队失败**：{message}", classes="agent-msg"))
            return
        except Exception:
            logger.warning("TUI durable conversation enqueue failed", exc_info=True)
            status.status_text = "排队消息未能安全保存"
            chat.mount(
                Markdown(
                    "**排队失败**：消息没有进入持久队列，请运行 `/doctor` 后重试。",
                    classes="agent-msg",
                )
            )
            return
        chat.add_user_message(text)
        chat.mount(
            Markdown(
                f"已排队 · 第 {item.position} 位 · `{item.request_id}`",
                classes="agent-msg",
            )
        )
        status.status_text = f"已排队 {item.position} 条；可用 /send-now 提升"

    @work(exclusive=False, exit_on_error=False)
    async def _promote_queued_conversation(self, request_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            authority = self._current_conversation_queue_authority()
            if authority is None:
                raise ConversationQueueClaimError("当前会话没有可提升的排队消息。")
            items = await authority.store.list_queued_conversations(
                workspace_root=authority.workspace_root,
                session_id=authority.session_id,
                limit=20,
            )
            target = request_id or (items[-1].request_id if items else "")
            if not target:
                raise ConversationQueueClaimError("当前没有可提升的排队消息。")
            promoted = await authority.promote(
                request_id=target,
                active_claim=self._active_queue_claim,
            )
        except (ConversationQueueClaimError, HarnessStoreConflictError) as exc:
            status.status_text = str(exc)
            chat.mount(Markdown(f"**立即发送失败**：{exc}", classes="agent-msg"))
            return
        except Exception:
            logger.warning("TUI queue promotion failed", exc_info=True)
            status.status_text = "队列提升失败"
            chat.mount(
                Markdown(
                    "**立即发送失败**：无法读取持久队列，请运行 `/doctor`。",
                    classes="agent-msg",
                )
            )
            return
        status.status_text = "已提升到下一安全执行位置"
        chat.mount(
            Markdown(
                f"**立即发送**：`{promoted.request_id}` 已提升到下一安全执行位置。",
                classes="agent-msg",
            )
        )

    @work(exclusive=False, exit_on_error=False)
    async def _cancel_queued_conversation(self, request_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            authority = self._current_conversation_queue_authority()
            if authority is None:
                raise ConversationQueueClaimError("当前会话没有可取消的排队消息。")
            items = await authority.store.list_queued_conversations(
                workspace_root=authority.workspace_root,
                session_id=authority.session_id,
                limit=20,
            )
            target = request_id or (items[-1].request_id if items else "")
            if not target:
                raise ConversationQueueClaimError("当前没有可取消的排队消息。")
            cancelled = await authority.cancel_unclaimed_request(request_id=target)
        except (ConversationQueueClaimError, HarnessStoreConflictError) as exc:
            status.status_text = str(exc)
            chat.mount(Markdown(f"**取消排队失败**：{exc}", classes="agent-msg"))
            return
        except Exception:
            logger.warning("TUI queue cancellation failed", exc_info=True)
            status.status_text = "取消排队失败"
            chat.mount(
                Markdown(
                    "**取消排队失败**：无法更新持久队列，请运行 `/doctor`。",
                    classes="agent-msg",
                )
            )
            return
        status.status_text = "排队消息已取消，未进入模型执行"
        chat.mount(
            Markdown(
                f"**已取消排队**：`{cancelled.request_id}` 尚未派发。",
                classes="agent-msg",
            )
        )

    def _handle_slash_command(self, text: str) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("tui.command_start", {"command": text})
        raw = text.strip()
        if not raw:
            return
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if command == "/agents":
            self._open_agent_control()
            return
        if command == "/workbench":
            self._open_workbench_overview()
            return
        if command == "/reasoning":
            self._handle_reasoning_command(arg)
            return
        self._run_cli_slash_command(raw)

    @work(exclusive=True, exit_on_error=False)
    async def _run_cli_slash_command(self, text: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return

        command = parts[0].lower()
        raw_command = parts[0]
        if command in {"/quit", "/q", "/exit"}:
            if self.debug_trace is not None:
                self.debug_trace.event("tui.exit_requested", {"command": text})
            self.exit()
            return

        if command == "/n":
            command = "/new"
            text = command + (f" {parts[1]}" if len(parts) > 1 else "")
        elif command != raw_command:
            text = command + (f" {parts[1]}" if len(parts) > 1 else "")

        status.status_text = "命令执行中..."
        try:
            raw_output = await execute_slash_command(
                self.engine,
                text,
                frontend=self._slash_frontend,
            )
        except Exception as exc:
            logger.exception("TUI slash command execution failed")
            if self.debug_trace is not None:
                self.debug_trace.exception("tui.command.failed", exc)
            chat.mount(
                Markdown(
                    f"**命令执行失败**: {type(exc).__name__} — {exc}",
                    classes="agent-msg",
                )
            )
            status.status_text = "命令执行失败"
            return

        output = strip_ansi(raw_output).strip()
        if output:
            chat.mount(
                Markdown(
                    _format_tool_output_markdown(output),
                    classes="agent-msg",
                )
            )
        else:
            chat.mount(
                Markdown(f"**命令已执行**: `{text}`", classes="agent-msg")
            )
        self._sync_model_control_status(status)
        status.status_text = "就绪"

    def _handle_reasoning_command(self, arg: str) -> None:
        raw = arg.strip().lower()
        if not raw or raw == "toggle":
            enabled = not self._show_reasoning
        elif raw in {"on", "true", "1", "show", "open"}:
            enabled = True
        elif raw in {"off", "false", "0", "hide", "close"}:
            enabled = False
        else:
            self.query_one(ChatPanel).mount(
                Markdown("用法: `/reasoning on|off|toggle`", classes="agent-msg")
            )
            return
        self._show_reasoning = enabled
        chat = self.query_one(ChatPanel)
        chat._show_reasoning = enabled
        config = getattr(self.engine, "_config", None)
        if config is not None and getattr(config, "ui", None) is not None:
            config.ui.show_reasoning = enabled
        chat.mount(
            Markdown(
                "思考文本显示已开启。" if enabled else "思考文本显示已关闭。",
                classes="agent-msg",
            )
        )
        status = self.query_one(StatusBar)
        status.reasoning_text = "on" if enabled else "off"
        status.status_text = "就绪"

    @work(exclusive=False, exit_on_error=False)
    async def _run_agent(
        self,
        task: str,
        queue_authority: DurableConversationQueueAuthority | None = None,
        queue_claim: ConversationQueueClaim | None = None,
    ) -> None:
        import time

        from naumi_agent.main import _tool_label

        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        terminal_state = "failed"
        terminal_reason = "run_failed"
        queue_commit_ok = True
        if self.debug_trace is not None:
            self.debug_trace.event("tui.agent_run_start", {"task": task})

        # Streaming state for metrics
        streaming_model = ""
        streaming_token_count = 0
        streaming_first_time = 0.0
        streaming_last_time = 0.0
        streaming_turn_start = 0.0
        streaming_first_feedback = 0.0
        streaming_first_model_chunk = 0.0
        streaming_first_token_latency = 0.0

        async def on_event(event_type: str, data: dict[str, Any]) -> None:
            nonlocal streaming_model, streaming_token_count
            nonlocal streaming_first_time, streaming_last_time
            nonlocal streaming_turn_start
            nonlocal streaming_first_feedback, streaming_first_model_chunk
            nonlocal streaming_first_token_latency
            if self.debug_trace is not None:
                self.debug_trace.event(
                    "engine.stream_event",
                    {"event": event_type, "data": data},
                )
            runtime_mode = getattr(self.engine, "runtime_mode", None)
            status.mode_text = getattr(runtime_mode, "value", str(runtime_mode or "default"))
            if (
                isinstance(self.screen, AgentControlScreen)
                and event_type in {
                    "subagent_event",
                    "team_event",
                    "tool_prepare_start",
                    "tool_prepare_snapshot",
                    "tool_prepare_end",
                    "tool_start",
                    "tool_end",
                    "permission_bubble",
                    "completion_receipt",
                }
            ):
                self.screen.refresh_snapshot()

            match event_type:
                case "run_started":
                    status.status_text = "⏳ 已接手，准备执行..."
                case "perf_phase":
                    label = data.get("label") or data.get("phase") or "阶段"
                    duration = int(data.get("duration_ms", 0) or 0)
                    status.status_text = f"⏱ {label}: {duration}ms"
                case "latency_metric":
                    metric = str(data.get("metric", ""))
                    duration = int(data.get("duration_ms", 0) or 0)
                    seconds = duration / 1000
                    if metric == "first_progress":
                        streaming_first_feedback = seconds
                    elif metric == "first_model_chunk":
                        streaming_first_model_chunk = seconds
                    elif metric == "first_token":
                        streaming_first_token_latency = seconds
                    label = data.get("label") or metric or "延迟"
                    status.status_text = f"⏱ {label}: {duration}ms"
                case "turn_start":
                    model_val = data.get("model", "")
                    if model_val:
                        streaming_model = model_val
                        chat.show_model(model_val)
                    turn = data["turn"]
                    if turn > 1:
                        status.status_text = f"🔄 第 {turn} 轮..."
                    streaming_token_count = 0
                    streaming_first_time = 0.0
                    streaming_last_time = 0.0
                    streaming_turn_start = time.monotonic()
                    streaming_first_feedback = 0.0
                    streaming_first_model_chunk = 0.0
                    streaming_first_token_latency = 0.0
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
                    content = data["content"]
                    if content:
                        now = time.monotonic()
                        if streaming_first_time == 0.0:
                            streaming_first_time = now
                        streaming_last_time = now
                        streaming_token_count += 1
                    chat.add_response_token(content)
                case "response_end":
                    pass
                case "completion_receipt":
                    receipt = CompletionReceipt.from_dict(data)
                    chat.mount(
                        Static(
                            format_completion_receipt_text(receipt),
                            classes="agent-msg",
                        )
                    )
                    status.status_text = (
                        f"完成回执：{completion_outcome_label(receipt)}"
                    )
                    if isinstance(self.screen, RuntimeInspectorScreen):
                        self.screen.refresh_snapshot()
                case "tool_prepare_start" | "tool_prepare_snapshot":
                    prepare_text = format_tool_prepare_status(data)
                    chat.update_tool_prepare(prepare_text)
                    status.status_text = prepare_text
                case "tool_prepare_end":
                    chat.end_tool_prepare()
                case "tool_start":
                    tool_name = data["name"]
                    label = _tool_label(tool_name, data.get("args", ""))
                    chat.start_tool(label)
                    status.status_text = f"{label}..."
                case "tool_end":
                    tool_name = data["name"]
                    label = _tool_label(tool_name)
                    chat.end_tool(
                        label,
                        data["status"],
                        data["duration_ms"],
                        str(data.get("content", "") or ""),
                    )
                case "hook_trace":
                    point = str(data.get("point", "?"))
                    callback = str(data.get("callback", "?"))
                    duration = int(data.get("duration_ms", 0) or 0)
                    error = str(data.get("error", "") or "")
                    aborted = bool(data.get("aborted", False))
                    status_label = "拦截" if aborted else "异常" if error else "触发"
                    style = "yellow" if aborted else "red" if error else "magenta"
                    suffix = f" · {error}" if error else ""
                    chat.mount(
                        Static(
                            Text.from_markup(
                                f"  [{style}]hook {status_label}: "
                                f"{point} → {callback} ({duration}ms){suffix}[/{style}]"
                            ),
                            classes="tool-done",
                        )
                    )
                    status.status_text = f"hook {status_label}: {point}"
                case "task_snapshot":
                    from naumi_agent.main import _format_todo_bar

                    source = str(data.get("source", "todo"))
                    todo_bar = self.query_one(TodoBar)
                    todo_bar.todo_text = _format_todo_bar(data)
                    status.status_text = f"todo 已更新：{source}"
                case "subagent_event":
                    event_status = str(data.get("status", "?"))
                    agent = strip_ansi(str(data.get("agent_name", "") or "未匹配"))
                    task_id = strip_ansi(str(data.get("task_id", "?")))
                    description = strip_ansi(str(data.get("description", "") or ""))
                    message = strip_ansi(str(data.get("message", "") or ""))
                    try:
                        tokens = max(0, int(data.get("tokens", 0) or 0))
                    except (TypeError, ValueError):
                        tokens = 0
                    try:
                        cost = max(0.0, float(data.get("cost", 0.0) or 0.0))
                    except (TypeError, ValueError):
                        cost = 0.0
                    status_label = {
                        "started": "进行中",
                        "running": "进行中",
                        "completed": "已完成",
                        "error": "失败",
                        "failed": "失败",
                        "cancelled": "已取消",
                    }.get(event_status, event_status)
                    style = (
                        "green"
                        if event_status == "completed"
                        else "red"
                        if event_status in {"error", "failed"}
                        else "yellow"
                        if event_status == "cancelled"
                        else "cyan"
                    )
                    output = Text()
                    output.append(f"  子智能体 {status_label}: ", style=style)
                    output.append(f"{agent} / {task_id}", style=style)
                    if description:
                        output.append(f"\n    任务 · {description}", style="dim")
                    if message:
                        output.append(f"\n    最新 · {message}")
                    resources = []
                    if tokens:
                        resources.append(f"{tokens:,} tokens")
                    if cost:
                        resources.append(f"${cost:.4f}")
                    if resources:
                        output.append(
                            f"\n    资源 · {' · '.join(resources)}",
                            style="dim",
                        )
                    chat.mount(
                        Static(
                            output,
                            classes="tool-done",
                        )
                    )
                    status.status_text = f"子智能体 {status_label}: {agent}"
                case "permission_bubble":
                    agent = str(data.get("agent_name", "?"))
                    tool = str(data.get("tool_name", "?"))
                    bubble_status = str(data.get("status", "?"))
                    reason = str(data.get("reason", "") or "")
                    style = (
                        "red"
                        if bubble_status in {
                            "blocked",
                            "blocked_by_hook",
                            "blocked_by_plan_mode",
                            "denied",
                            "confirmation_error",
                        }
                        else "green"
                        if bubble_status in {"confirmed", "bypass_enabled"}
                        else "yellow"
                    )
                    suffix = f" · {reason[:120]}" if reason else ""
                    chat.mount(
                        Static(
                            Text.from_markup(
                                f"  [{style}]permission bubble: "
                                f"{agent} → {tool} [{bubble_status}]{suffix}[/{style}]"
                            ),
                            classes="tool-done",
                        )
                    )
                    status.status_text = f"permission bubble: {agent} → {tool}"
                case "team_event":
                    event_type = str(data.get("event_type", "?"))
                    sender = str(data.get("sender", "?"))
                    recipient = str(data.get("recipient", "") or "广播")
                    priority = str(data.get("priority", "normal"))
                    message = str(data.get("message", "") or "")
                    style = (
                        "red"
                        if priority == "critical"
                        else "yellow"
                        if priority == "high"
                        else "cyan"
                    )
                    suffix = f" · {message[:120]}" if message else ""
                    chat.mount(
                        Static(
                            Text.from_markup(
                                f"  [{style}]team {event_type}: "
                                f"{sender} → {recipient} [{priority}]{suffix}[/{style}]"
                            ),
                            classes="tool-done",
                        )
                    )
                    status.status_text = f"team {event_type}: {sender} → {recipient}"
                case "runtime_notification":
                    title = str(data.get("title", "") or "运行时通知")
                    source = str(data.get("source", "runtime"))
                    count = int(data.get("count", 0) or 0)
                    preview = str(data.get("preview", "") or "").replace("\n", " ")
                    suffix = f" · {preview[:160]}" if preview else ""
                    line = Text(
                        f"  {title}: {source} ×{count}{suffix}",
                        style="cyan",
                    )
                    chat.mount(Static(line, classes="tool-done"))
                    status.status_text = f"{title}: {source} ×{count}"
                case "context_compacted":
                    logger.info(
                        "Context compacted: %d → %d messages",
                        data["before"],
                        data["after"],
                    )
                    preserved = data.get("preserved_sections", [])
                    warnings = data.get("warnings", [])
                    archived = int(data.get("archived_tool_results", 0) or 0)
                    archived_text = (
                        f"；归档：{archived} 个大型工具结果"
                        if archived
                        else ""
                    )
                    preserved_text = (
                        "；保留：" + "、".join(str(item) for item in preserved)
                        if isinstance(preserved, list) and preserved
                        else ""
                    )
                    warning_text = (
                        "；风险：" + "；".join(str(item) for item in warnings)
                        if isinstance(warnings, list) and warnings
                        else ""
                    )
                    chat.mount(
                        Static(
                            Text.from_markup(
                                "[magenta]  context compacted: "
                                f"{data['before']} → {data['after']} messages"
                                f"{archived_text}{preserved_text}{warning_text}[/magenta]"
                            ),
                            classes="tool-done",
                        )
                    )
                    status.status_text = "上下文已压缩，运行时状态已保留"
                case "recovery_event":
                    reason = str(data.get("reason", "?"))
                    action = str(data.get("action", "?"))
                    phase = str(data.get("phase", "?"))
                    before = data.get("before", "?")
                    after = data.get("after", "?")
                    unit = str(data.get("unit", "messages"))
                    style = (
                        "green"
                        if phase == "completed"
                        else "red"
                        if phase == "failed"
                        else "yellow"
                    )
                    suffix = (
                        f" {before} → {after} {unit}"
                        if after != "?"
                        else f" before={before}"
                    )
                    chat.mount(
                        Static(
                            Text.from_markup(
                                f"  [{style}]recovery {phase}: {action} "
                                f"({reason}){suffix}[/{style}]"
                            ),
                            classes="tool-done",
                        )
                    )
                    status.status_text = f"恢复流程：{phase}"
                case "error":
                    chat.start_response()
                    chat.add_response_token(f"**错误**: {data['message']}")
                    chat.finalize(0, 0.0, engine=self.engine)

        # Calculate token speed
        def _get_token_speed() -> float:
            if (
                streaming_token_count > 0
                and streaming_first_time > 0
                and streaming_last_time > streaming_first_time
            ):
                return streaming_token_count / (streaming_last_time - streaming_first_time)
            return 0.0

        def _get_ttft() -> float:
            if streaming_first_token_latency > 0:
                return streaming_first_token_latency
            if streaming_first_time > 0 and streaming_turn_start > 0:
                return streaming_first_time - streaming_turn_start
            return 0.0

        def _get_duration() -> float:
            if streaming_turn_start > 0:
                end = streaming_last_time if streaming_last_time > 0 else time.monotonic()
                return end - streaming_turn_start
            return 0.0

        captured_noise = ""
        try:
            await self._ensure_conversation_session(task)
            if queue_authority is not None and queue_claim is not None:
                self._active_queue_authority = queue_authority
                self._active_queue_claim = queue_claim
                self._queue_claim_lost = False
                self._start_queue_claim_renewal(queue_authority, queue_claim)
            with _capture_tui_terminal_noise() as (stdout_buf, stderr_buf):
                result = await self.engine.run_streaming(
                    task,
                    CallbackEventSink(on_event),
                )
                captured_noise = _captured_terminal_text(stdout_buf, stderr_buf)
            if self.debug_trace is not None:
                self.debug_trace.event(
                    "tui.agent_run_end",
                    {
                        "status": result.status,
                        "response": result.response,
                        "error": result.error,
                        "usage": result.usage,
                    },
                )

            if result.status == "error" and result.error:
                chat.start_response()
                chat.add_response_token(f"**错误**: {result.error}")

            if result.status in {"completed", "success"}:
                terminal_state = "completed"
                terminal_reason = "run_completed"
            else:
                terminal_state = "failed"
                terminal_reason = f"run_{result.status or 'failed'}"

            token_speed = _get_token_speed()
            ttft = _get_ttft()
            duration = _get_duration()
            total_tokens = result.usage.total_input_tokens + result.usage.total_output_tokens

            chat.finalize(
                result.usage.turns,
                result.usage.total_cost_usd,
                total_tokens,
                model=streaming_model,
                token_speed=token_speed,
                first_feedback=streaming_first_feedback,
                first_model_chunk=streaming_first_model_chunk,
                ttft=ttft,
                duration=duration,
                cache_tokens=result.usage.cache_tokens,
                engine=self.engine,
            )

            # Build status bar text
            status_parts: list[str] = []
            if streaming_model:
                status_parts.append(streaming_model)
            status_parts.append(f"轮次: {result.usage.turns}")
            tok_str = f"↑{result.usage.total_input_tokens} ↓{result.usage.total_output_tokens}"
            if token_speed > 0:
                tok_str += f" ({token_speed:.1f} tok/s)"
            status_parts.append(tok_str)
            if streaming_first_feedback > 0:
                status_parts.append(f"首反馈: {streaming_first_feedback:.1f}s")
            if streaming_first_model_chunk > 0:
                status_parts.append(f"首包: {streaming_first_model_chunk:.1f}s")
            if ttft > 0:
                status_parts.append(f"首字: {ttft:.1f}s")
            if duration > 0:
                status_parts.append(f"耗时: {duration:.1f}s")
            status_parts.append(f"费用: ${result.usage.total_cost_usd:.4f}")
            ctx = self.engine.get_context_info()
            ctx_pct = ctx["percentage"]
            status_parts.append(f"上下文: {ctx_pct}%")
            status.status_text = "✅ " + " | ".join(status_parts)
        except asyncio.CancelledError:
            terminal_state = "cancelled"
            terminal_reason = "run_cancelled"
            raise
        except Exception as e:
            if "stdout_buf" in locals() and "stderr_buf" in locals():
                captured_noise = _captured_terminal_text(stdout_buf, stderr_buf)
            logger.debug("Agent run failed", exc_info=True)
            if self.debug_trace is not None:
                self.debug_trace.exception("tui.agent_run", e, task=task)
            chat.start_response()
            chat.add_response_token(f"**错误**: {e}")
            chat.finalize(0, 0.0, engine=self.engine)
            status.status_text = f"❌ 错误: {e}"
        finally:
            await self._stop_queue_claim_renewal()
            if queue_authority is not None and queue_claim is not None:
                if self._queue_claim_lost:
                    queue_commit_ok = False
                else:
                    try:
                        await queue_authority.finish(
                            queue_claim,
                            state=terminal_state,
                            terminal_reason=terminal_reason,
                        )
                    except Exception:
                        queue_commit_ok = False
                        logger.warning("TUI queue terminal commit failed", exc_info=True)
                        status.status_text = (
                            "排队消息终态未通过 claim 校验；请重开会话核对。"
                        )
                self._active_queue_authority = None
                self._active_queue_claim = None
            _mount_captured_terminal_noise(
                chat,
                captured_noise,
                debug_trace=self.debug_trace,
            )
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            self._agent_busy = False
            self._agent_worker = None
            if queue_commit_ok:
                await self._start_next_queued_conversation()

    def _set_input_enabled(self, enabled: bool) -> None:
        input_bar = self.query_one(InputBar)
        msg_input = input_bar.query_one("#msg-input", Input)
        send_btn = input_bar.query_one("#send-btn", Button)
        msg_input.disabled = not enabled
        send_btn.disabled = not enabled
        if enabled:
            msg_input.focus()

    def _start_queue_claim_renewal(
        self,
        authority: DurableConversationQueueAuthority,
        claim: ConversationQueueClaim,
    ) -> None:
        current = self._queue_claim_renew_task
        if current is not None and not current.done():
            current.cancel()

        async def keepalive() -> None:
            active = claim
            try:
                while self._agent_busy:
                    await asyncio.sleep(max(1.0, authority.lease_seconds / 3))
                    active = await authority.renew(active)
                    self._active_queue_claim = active
            except asyncio.CancelledError:
                raise
            except Exception:
                self._queue_claim_lost = True
                logger.warning("TUI queue claim renewal failed", exc_info=True)
                self.query_one(StatusBar).status_text = (
                    "排队消息 claim 续租失败；当前运行将停止以避免重复提交。"
                )
                worker = self._agent_worker
                if worker is not None:
                    worker.cancel()

        self._queue_claim_renew_task = asyncio.create_task(
            keepalive(),
            name=f"naumi-tui-queue-claim-{claim.item.request_id}",
        )

    async def _stop_queue_claim_renewal(self) -> None:
        task = self._queue_claim_renew_task
        self._queue_claim_renew_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _start_next_queued_conversation(self) -> bool:
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        authority = self._conversation_queue_authority(session_id)
        if authority is None:
            return False
        try:
            recovery = await authority.recover(limit=20)
            if recovery.blocked:
                self.query_one(StatusBar).status_text = (
                    "排队消息存在待核对的历史 claim；请使用 /queue 处理。"
                )
                return False
            if not recovery.ready:
                return False
            item = recovery.ready[0]
            claim = await authority.claim(item)
        except ConversationQueueClaimError as exc:
            self.query_one(StatusBar).status_text = str(exc)
            return False
        except Exception:
            logger.warning("TUI queue dispatch failed", exc_info=True)
            self.query_one(StatusBar).status_text = (
                "持久队列派发失败；请运行 /doctor 后恢复会话。"
            )
            return False
        self._agent_busy = True
        self.query_one(Spinner)._active = True
        self.query_one(StatusBar).status_text = (
            f"开始执行排队消息 · {item.request_id}"
        )
        self._agent_worker = self._run_agent(item.text, authority, claim)
        return True

    def action_toggle_activity(self) -> None:
        activity = self.query_one(ActivityPanel)
        activity.show_panel = not activity.show_panel

    def action_toggle_inspector(self) -> None:
        current = self.screen
        if isinstance(current, RuntimeInspectorScreen):
            self.pop_screen()
            return
        if isinstance(current, ModalScreen):
            return
        self.push_screen(RuntimeInspectorScreen(self.engine))

    def action_toggle_agents(self) -> None:
        current = self.screen
        if isinstance(current, AgentControlScreen):
            self.pop_screen()
            return
        self._open_agent_control()

    def _open_agent_control(self) -> None:
        current = self.screen
        if isinstance(current, AgentControlScreen | ModalScreen):
            return
        self.push_screen(AgentControlScreen(self.engine))

    def _open_workbench_overview(self) -> None:
        current = self.screen
        if isinstance(current, WorkbenchOverviewScreen | ModalScreen):
            return
        self.push_screen(WorkbenchOverviewScreen(self.engine))

    def action_toggle_browser(self) -> None:
        browser = self.query_one(BrowserPanel)
        browser.show_panel = not browser.show_panel
        if browser.show_panel:
            browser.refresh_browser_state(self.engine)

    def action_cycle_runtime_mode(self) -> None:
        mode = self.engine.cycle_runtime_mode()
        status = self.query_one(StatusBar)
        status.mode_text = mode.value
        status.status_text = f"已切换模式: {mode.value}"
        if self.debug_trace is not None:
            self.debug_trace.event("tui.runtime_mode_changed", {"mode": mode.value})

    def action_toggle_history(self) -> None:
        history = self.query_one(HistoryPanel)
        history.show_panel = not history.show_panel
        if history.show_panel:
            history.refresh_sessions()

    def _run_history_command(self, arg: str) -> None:
        history = self.query_one(HistoryPanel)
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if subcommand == "preview":
            if not sub_arg:
                status.status_text = "用法: /history preview <session_id>"
                return
            self._show_history_preview(sub_arg)
            return
        if subcommand in {"delete-preview", "delete_preview"}:
            if not sub_arg:
                status.status_text = "用法: /history delete-preview <session_id>"
                return
            self._show_session_delete_preview(sub_arg)
            return
        if subcommand in {"retention-preview", "retention_preview"}:
            self._show_session_retention_preview()
            return
        if subcommand in {"retention-run", "retention_run"}:
            self._run_session_retention()
            return
        if subcommand in {"retention-worker", "retention_worker"}:
            self._control_session_retention_worker(sub_arg.lower() or "status")
            return
        if subcommand == "archive":
            if not sub_arg:
                status.status_text = "用法: /history archive <session_id>"
                return
            self._archive_session(sub_arg)
            return
        if subcommand == "delete":
            if not sub_arg:
                status.status_text = "用法: /history delete <session_id>"
                return
            self._delete_session(sub_arg, sub_arg)
            return
        history.search_query = arg.strip()
        history.show_panel = True
        history.refresh_sessions()
        if history.search_query:
            chat.mount(
                Markdown(f"历史会话搜索：`{history.search_query}`", classes="agent-msg")
            )

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
        status.session_text = f"会话:{session.id[:8]}"
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
                    chat.mount(
                        Markdown(
                            excerpt_markdown_code_blocks(content),
                            classes="agent-msg",
                        )
                    )
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

        # --- Show session stats immediately in chat panel ---
        stats_parts: list[str] = []
        model = getattr(session, "model", "")
        if model:
            stats_parts.append(model)
        user_msgs = sum(1 for m in session.messages if m.get("role") == "user")
        stats_parts.append(f"消息: {user_msgs}条")
        if session.total_tokens:
            stats_parts.append(f"Token: {session.total_tokens}")
        if session.total_cost_usd > 0:
            stats_parts.append(f"费用: ${session.total_cost_usd:.4f}")
        ctx = self.engine.get_context_info()
        ctx_pct = ctx["percentage"]
        used_k = ctx["used"] / 1000
        window_k = ctx["window"] / 1000
        stats_parts.append(f"上下文: {used_k:.0f}K/{window_k:.0f}K ({ctx_pct}%)")
        budget = self.engine.get_budget_info()
        stats_parts.append(f"预算: {format_budget_detail(budget)}")
        stats_line = " | ".join(stats_parts)
        chat.mount(
            Static(
                Text.from_markup(f"[dim]  {stats_line}[/dim]"),
                classes="usage-info",
            )
        )
        chat.scroll_end(animate=False)

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
            session_id = await _find_latest_user_session_id(self.engine)
        except Exception:
            status.status_text = "加载失败"
            return
        if session_id is None:
            status.status_text = "暂无历史会话"
            return
        await self._load_and_show_session(session_id)

    def action_clear_chat(self) -> None:
        chat = self.query_one(ChatPanel)
        chat.clear()
        self._clear_runtime_task_panels()
        self.engine.reset()
        self._show_startup_status()

    def _clear_runtime_task_panels(self) -> None:
        self.query_one(TodoBar).todo_text = ""
        self.query_one(ActivityPanel).clear_logs()

    def action_copy_transcript(self, scope: str = "all") -> None:
        """Copy or export diagnostic transcript content."""
        status = self.query_one(StatusBar)
        normalized_scope = scope.strip().lower() or "all"
        if normalized_scope in {"last", "error"} and self.debug_trace is not None:
            text = self.debug_trace.build_diagnostic_text(normalized_scope)
            prefix = f"tui-{normalized_scope}-diagnostic"
        else:
            text = self._build_session_transcript()
            prefix = "tui-transcript"
        result = copy_or_save_transcript(
            text,
            base_dir=Path.cwd() / "data",
            prefix=prefix,
        )
        status.status_text = result.message

    def _build_session_transcript(self) -> str:
        """Build a plain-text transcript from the engine conversation state."""
        lines = [
            "NaumiAgent TUI 会话记录",
            f"工作区根目录: {getattr(self.engine, 'workspace_root', Path.cwd())}",
            f"启动目录: {Path.cwd()}",
        ]
        session = getattr(self.engine, "_session", None)
        if session is not None:
            lines.append(f"会话ID: {session.id}")
            if session.title:
                lines.append(f"标题: {session.title}")
        lines.append("")

        messages = (
            getattr(self.engine, "_full_history", None)
            or getattr(self.engine, "_messages", [])
            or []
        )
        if not messages:
            lines.append("当前还没有可导出的会话消息。")
            return "\n".join(lines)

        for index, message in enumerate(messages, start=1):
            role = message.get("role", "")
            content = str(message.get("content") or "")
            if role == "user":
                lines.append(f"[{index}] 你\n{content}")
            elif role == "assistant":
                reasoning = str(message.get("reasoning_content") or "")
                lines.append(f"[{index}] NaumiAgent")
                if reasoning:
                    lines.append(f"思考过程:\n{reasoning}")
                if content:
                    lines.append(content)
                for tool_call in message.get("tool_calls", []) or []:
                    function = (
                        tool_call.get("function", {})
                        if isinstance(tool_call, dict)
                        else {}
                    )
                    name = function.get("name", "tool")
                    args = function.get("arguments", "")
                    lines.append(f"工具调用: {name}\n{args}")
            elif role == "tool":
                name = message.get("name") or message.get("tool_call_id") or "tool"
                lines.append(f"[{index}] 工具结果 {name}\n{content}")
            else:
                lines.append(f"[{index}] {role or 'unknown'}\n{content}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    @work(exclusive=True, exit_on_error=False)
    async def _show_history_preview(self, session_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        session = await self.engine.session_store.load(session_id)
        if session is None:
            status.status_text = f"会话不存在: {session_id}"
            return
        chat.mount(Markdown(render_history_preview(session), classes="agent-msg"))
        status.status_text = f"已预览: {session.title or session_id}"

    @work(exclusive=True, exit_on_error=False)
    async def _show_session_delete_preview(self, session_id: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            preview = await self.engine.preview_session_delete(session_id)
        except Exception as exc:
            status.status_text = f"删除影响预览失败: {exc}"
            return
        if preview is None:
            status.status_text = f"会话不存在: {session_id}"
            return
        chat.mount(
            Markdown(render_session_delete_preview(preview), classes="agent-msg")
        )
        status.status_text = f"已预览删除影响: {preview.title}"

    @work(exclusive=True, exit_on_error=False)
    async def _show_session_retention_preview(self) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            preview = await self.engine.preview_session_retention()
        except Exception as exc:
            status.status_text = f"保留策略预览失败: {exc}"
            return
        chat.mount(
            Markdown(render_session_retention_preview(preview), classes="agent-msg")
        )
        status.status_text = f"已预览保留策略: {len(preview.selected)} 个候选"

    @work(exclusive=True, exit_on_error=False)
    async def _run_session_retention(self) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "正在执行有界 Session 保留清理"
        try:
            result = await self.engine.run_session_retention_once()
        except asyncio.CancelledError:
            status.status_text = "Session 保留清理已取消"
            raise
        except Exception:
            status.status_text = "Session 保留清理失败关闭"
            return
        chat.mount(
            Markdown(render_session_retention_result(result), classes="agent-msg")
        )
        status.status_text = (
            f"Session 保留清理 {result.status.value}: "
            f"完整删除 {result.completed_count}"
        )

    @work(exclusive=True, exit_on_error=False)
    async def _control_session_retention_worker(self, action: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        if action == "start":
            started = self.engine.start_session_retention_worker()
            status.status_text = (
                "Session retention worker 已启动"
                if started
                else "Worker 未启动：配置未启用或已经运行"
            )
            return
        if action == "stop":
            stopped = await self.engine.stop_session_retention_worker()
            status.status_text = (
                "Session retention worker 已停止"
                if stopped
                else "Worker 已处于停止状态"
            )
            return
        if action == "wake":
            status.status_text = (
                "Session retention worker 已唤醒"
                if self.engine.wake_session_retention_worker()
                else "Worker 尚未运行"
            )
            return
        if action == "status":
            chat.mount(
                Markdown(
                    render_session_retention_worker(
                        self.engine.session_retention_worker_snapshot(),
                        configured_enabled=(
                            self.engine.config.memory.session_retention.periodic_enabled
                        ),
                    ),
                    classes="agent-msg",
                )
            )
            status.status_text = "已显示 Session retention worker 状态"
            return
        status.status_text = "用法: /history retention-worker [status|start|stop|wake]"

    @work(exclusive=True, exit_on_error=False)
    async def _run_doctor(self) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "环境诊断中"
        report = await run_doctor(
            self.engine._config,
            workspace_root=getattr(self.engine, "workspace_root", Path.cwd()),
            mcp_manager=getattr(self.engine, "_mcp_manager", None),
            model_router=self.engine.router,
        )
        retention_config = getattr(
            getattr(self.engine._config, "harness", None),
            "runtime_heartbeat_retention",
            None,
        )
        retention_item = runtime_heartbeat_retention_health_item(
            {
                "configured_enabled": bool(
                    getattr(retention_config, "enabled", False)
                ),
                "state": "unavailable",
            }
        )
        content = (
            render_doctor_report(report)
            + "\n\n"
            + render_doctor_health_item_markdown(retention_item)
        )
        chat.mount(Markdown(content, classes="agent-msg"))
        status.status_text = {
            "pass": "环境诊断通过",
            "warn": "环境诊断存在提醒",
            "error": "环境诊断发现错误",
        }[report.status]
        if report.status == "pass" and retention_item.severity == "unknown":
            status.status_text = "环境诊断存在未知项"

    @work(exclusive=True, exit_on_error=False)
    async def _archive_session(self, session_id: str) -> None:
        status = self.query_one(StatusBar)
        try:
            ok = await self.engine.archive_session(session_id)
            if ok:
                status.status_text = f"已归档: {session_id}"
                history = self.query_one(HistoryPanel)
                if history.show_panel:
                    history.refresh_sessions()
            else:
                status.status_text = f"会话不存在: {session_id}"
        except Exception as e:
            status.status_text = f"归档失败: {e}"

    @work(exclusive=True, exit_on_error=False)
    async def _delete_session(self, session_id: str, title: str) -> None:
        status = self.query_one(StatusBar)
        was_active = bool(
            self.engine._session and self.engine._session.id == session_id
        )
        try:
            result = await self.engine.delete_session_detailed(session_id)
            if was_active and self.engine._session is None:
                self.query_one(ChatPanel).clear()
            if result.outcome is ReconciliationCoordinatorOutcome.COMPLETED:
                status.status_text = f"已删除: {title} · {result.message}"
            elif result.outcome is ReconciliationCoordinatorOutcome.NOT_FOUND:
                status.status_text = f"会话不存在: {session_id}"
            elif result.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED:
                status.status_text = f"删除协调等待安全重试: {result.request_id}"
            elif result.outcome is ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED:
                status.status_text = f"删除协调重试已耗尽: {result.request_id}"
            else:
                status.status_text = f"生命周期策略阻止删除: {session_id}"
            if result.outcome not in {
                ReconciliationCoordinatorOutcome.NOT_FOUND,
                ReconciliationCoordinatorOutcome.POLICY_BLOCKED,
            }:
                history = self.query_one(HistoryPanel)
                if history.show_panel:
                    history.refresh_sessions()
        except Exception as e:
            status.status_text = f"删除失败: {e}"

    async def _execute_registered_tool(
        self,
        tool_name: str,
        **arguments: Any,
    ) -> ToolResult:
        """Execute one registered tool through the Engine's public facade."""
        return await self.engine.execute_tool(
            ToolCall(
                id=f"tui-{tool_name}-{uuid4()}",
                name=tool_name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            ),
            agent_name="tui",
        )

    @work(exclusive=True, exit_on_error=False)
    async def _run_pursue(self, goal: str) -> None:
        """执行目标追踪循环."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)

        parts = goal.strip().split(maxsplit=1)
        if parts and parts[0] in {"list", "status", "resume"}:
            await self._run_pursue_meta(parts[0], parts[1] if len(parts) > 1 else "")
            return

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
            tool_result = await self._execute_registered_tool(
                "pursue_goal",
                goal=goal,
            )
            chat.end_thinking()
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**目标追踪失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(
                Markdown(
                    f"## 🎯 目标追踪报告\n\n{tool_result.content}",
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

    async def _run_pursue_meta(self, subcommand: str, arg: str) -> None:
        """执行 pursuit 持久化状态命令."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        tool_map = {
            "list": "pursuit_list",
            "status": "pursuit_status",
            "resume": "pursuit_resume",
        }
        tool_name = tool_map[subcommand]
        tool = self.engine.tool_registry.get(tool_name)
        if tool is None:
            chat.mount(Markdown(f"**工具未注册**: `{tool_name}`", classes="agent-msg"))
            return
        if subcommand in {"status", "resume"} and not arg:
            status.status_text = f"用法: /pursue {subcommand} <运行ID>"
            return
        status.status_text = "目标追踪状态处理中..."
        try:
            arguments = (
                {"active_only": "--active" in arg.split()}
                if subcommand == "list"
                else {"run_id": arg.strip()}
            )
            tool_result = await self._execute_registered_tool(tool_name, **arguments)
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**目标追踪状态失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(
                Markdown(
                    f"## 目标追踪状态\n\n{tool_result.content}",
                    classes="agent-msg",
                )
            )
        finally:
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_worktree_command(self, arg: str) -> None:
        """执行 worktree 隔离区命令."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = arg.strip().split()
        subcommand = parts[0] if parts else "status"

        async def _execute(tool_name: str, **kwargs: Any) -> None:
            tool = self.engine.tool_registry.get(tool_name)
            if tool is None:
                chat.mount(Markdown(f"**工具未注册**: `{tool_name}`", classes="agent-msg"))
                return
            status.status_text = "Worktree 操作中..."
            tool_result = await self._execute_registered_tool(tool_name, **kwargs)
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**Worktree 命令失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(
                Markdown(
                    f"## Worktree 隔离区\n\n{tool_result.content}",
                    classes="agent-msg",
                )
            )

        try:
            match subcommand:
                case "status" | "list":
                    name = parts[1] if len(parts) > 1 else ""
                    await _execute("worktree_status", name=name)
                case "create":
                    if len(parts) < 2:
                        status.status_text = "用法: /worktree create <名称> [任务ID]"
                        return
                    task_id = parts[2] if len(parts) > 2 else ""
                    await _execute("worktree_create", name=parts[1], task_id=task_id)
                case "bind":
                    if len(parts) < 3:
                        status.status_text = "用法: /worktree bind <名称> <任务ID>"
                        return
                    await _execute("worktree_bind_task", name=parts[1], task_id=parts[2])
                case "keep":
                    if len(parts) < 2:
                        status.status_text = "用法: /worktree keep <名称> [原因]"
                        return
                    reason = " ".join(parts[2:]) if len(parts) > 2 else ""
                    await _execute("worktree_keep", name=parts[1], reason=reason)
                case "remove":
                    if len(parts) < 2:
                        status.status_text = "用法: /worktree remove <名称> [--discard]"
                        return
                    discard = "--discard" in parts[2:] or "--force" in parts[2:]
                    await _execute("worktree_remove", name=parts[1], discard_changes=discard)
                case _:
                    status.status_text = "未知 worktree 子命令"
                    chat.mount(
                        Markdown(
                            "可用子命令：`status`、`list`、`create`、`bind`、`keep`、`remove`",
                            classes="agent-msg",
                        )
                    )
        finally:
            status.status_text = "就绪"
            input_bar = self.query_one(InputBar)
            msg_input = input_bar.query_one("#msg-input", Input)
            msg_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def _run_background_command(self, arg: str) -> None:
        """执行后台任务命令."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = arg.strip().split(maxsplit=2)
        subcommand = parts[0] if parts else "list"

        async def _execute(tool_name: str, **kwargs: Any) -> None:
            tool = self.engine.tool_registry.get(tool_name)
            if tool is None:
                chat.mount(Markdown(f"**工具未注册**: `{tool_name}`", classes="agent-msg"))
                return
            status.status_text = "后台任务处理中..."
            tool_result = await self._execute_registered_tool(tool_name, **kwargs)
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**后台任务命令失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(
                Markdown(
                    f"## 后台任务\n\n{tool_result.content}",
                    classes="agent-msg",
                )
            )

        try:
            match subcommand:
                case "run":
                    if len(parts) < 2:
                        status.status_text = "用法: /background run <命令>"
                        return
                    command = arg.strip()[len("run"):].strip()
                    await _execute("background_run", command=command)
                case "status":
                    if len(parts) < 2:
                        status.status_text = "用法: /background status <任务ID>"
                        return
                    await _execute("background_status", task_id=parts[1])
                case "list":
                    await _execute("background_list")
                case "cancel":
                    if len(parts) < 2:
                        status.status_text = "用法: /background cancel <任务ID>"
                        return
                    await _execute("background_cancel", task_id=parts[1])
                case "cleanup":
                    await _execute("background_cleanup")
                case "output":
                    if len(parts) < 2:
                        status.status_text = "用法: /background output <任务ID>"
                        return
                    await _execute("background_read_output", task_id=parts[1])
                case _:
                    status.status_text = "未知后台任务子命令"
                    chat.mount(
                        Markdown(
                            "可用子命令：`run`、`status`、`list`、`cancel`、`cleanup`、`output`",
                            classes="agent-msg",
                        )
                    )
        finally:
            status.status_text = "就绪"
            input_bar = self.query_one(InputBar)
            msg_input = input_bar.query_one("#msg-input", Input)
            msg_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def _run_schedule_command(self, arg: str) -> None:
        """执行调度/提醒命令."""
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        try:
            parts = shlex.split(arg.strip())
        except ValueError as e:
            status.status_text = f"参数解析失败：{e}"
            return
        subcommand = parts[0] if parts else "list"

        async def _execute(tool_name: str, **kwargs: Any) -> None:
            tool = self.engine.tool_registry.get(tool_name)
            if tool is None:
                chat.mount(Markdown(f"**工具未注册**: `{tool_name}`", classes="agent-msg"))
                return
            status.status_text = "调度提醒处理中..."
            tool_result = await self._execute_registered_tool(tool_name, **kwargs)
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**调度命令失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(
                Markdown(
                    f"## 调度提醒\n\n{tool_result.content}",
                    classes="agent-msg",
                )
            )

        try:
            match subcommand:
                case "create":
                    if len(parts) < 4:
                        status.status_text = "用法: /schedule create once <ISO时间> <提醒内容>"
                        chat.mount(
                            Markdown(
                                '或：`/schedule create cron "*/15 * * * *" <提醒内容>`',
                                classes="agent-msg",
                            )
                        )
                        return
                    await _execute(
                        "schedule_create",
                        kind=parts[1],
                        expression=parts[2],
                        prompt=" ".join(parts[3:]),
                    )
                case "list":
                    await _execute("schedule_list", active_only="--active" in parts[1:])
                case "cancel":
                    if len(parts) < 2:
                        status.status_text = "用法: /schedule cancel <调度ID>"
                        return
                    await _execute("schedule_cancel", schedule_id=parts[1])
                case "pause":
                    if len(parts) < 2:
                        status.status_text = "用法: /schedule pause <调度ID>"
                        return
                    await _execute("schedule_pause", schedule_id=parts[1])
                case "resume":
                    if len(parts) < 2:
                        status.status_text = "用法: /schedule resume <调度ID>"
                        return
                    await _execute("schedule_resume", schedule_id=parts[1])
                case _:
                    status.status_text = "未知调度子命令"
                    chat.mount(
                        Markdown(
                            "可用子命令：`create`、`list`、`cancel`、`pause`、`resume`",
                            classes="agent-msg",
                        )
                    )
        finally:
            status.status_text = "就绪"
            input_bar = self.query_one(InputBar)
            msg_input = input_bar.query_one("#msg-input", Input)
            msg_input.focus()

    @work(exclusive=True, exit_on_error=False)
    async def _run_todo_command(self, arg: str) -> None:
        """执行 todo 命令."""
        from naumi_agent.main import _format_todo_bar
        from naumi_agent.tasks.commands import run_todo_command

        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "todo 处理中..."
        try:
            session = await self.engine.get_or_create_session()
            self.engine.task_store.set_session(session.id)
            result = await run_todo_command(self.engine.task_store, arg)
            tasks = await self.engine.task_store.list_tasks()
            open_tasks = [task for task in tasks if task.status.value != "completed"]
            self.query_one(TodoBar).todo_text = _format_todo_bar({
                "count": len(tasks),
                "open_count": len(open_tasks),
                "completed_count": len(tasks) - len(open_tasks),
                "items": [
                    {
                        "id": task.id,
                        "status": task.status.value,
                        "subject": task.active_form or task.subject,
                    }
                    for task in open_tasks
                ],
                "summary": result,
            })
            chat.mount(Markdown(f"## todo\n\n{result}", classes="agent-msg"))
        except Exception as e:
            chat.mount(Markdown(f"**todo 命令失败**: {e}", classes="agent-msg"))
        finally:
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_team_command(self, arg: str) -> None:
        """执行 team protocol 命令."""
        from naumi_agent.agents.team_commands import run_team_command

        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "team 协议处理中..."
        try:
            result = await run_team_command(self.engine.subagent_manager, arg)
            chat.mount(Markdown(f"## team\n\n{result}", classes="agent-msg"))
        except Exception as e:
            chat.mount(Markdown(f"**team 命令失败**: {e}", classes="agent-msg"))
        finally:
            status.status_text = "就绪"

    @work(exclusive=True, exit_on_error=False)
    async def _run_runtime_command(self, arg: str) -> None:
        """执行 runtime 状态命令."""
        from naumi_agent.tools.runtime import run_runtime_command

        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        status.status_text = "runtime 状态读取中..."
        try:
            result = await run_runtime_command(self.engine, arg)
            chat.mount(Markdown(result, classes="agent-msg"))
        except Exception as e:
            chat.mount(Markdown(f"**runtime 命令失败**: {e}", classes="agent-msg"))
        finally:
            status.status_text = "就绪"

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

    @work(exclusive=True, exit_on_error=False)
    async def _run_browser_daemon(self, arg: str) -> None:
        chat = self.query_one(ChatPanel)
        status = self.query_one(StatusBar)
        parts = shlex.split(arg) if arg.strip() else []
        subcommand = parts[0] if parts else "health"

        async def _execute(tool_name: str, **kwargs: Any) -> None:
            tool = self.engine.tool_registry.get(tool_name)
            if not tool:
                chat.mount(Markdown(f"**工具未注册**: `{tool_name}`", classes="agent-msg"))
                return
            tool_result = await self._execute_registered_tool(tool_name, **kwargs)
            if tool_result.status != "success":
                chat.mount(
                    Markdown(
                        f"**Browser daemon 命令失败**: {tool_result.content}",
                        classes="agent-msg",
                    )
                )
                return
            chat.mount(Markdown(tool_result.content, classes="agent-msg"))

        status.status_text = f"browser daemon: {subcommand}"
        self._set_input_enabled(False)
        self.query_one(Spinner)._active = True
        try:
            match subcommand:
                case "health":
                    await _execute("browser_daemon_health")
                case "start":
                    await _execute("browser_daemon_start")
                case "dashboard":
                    await _execute("browser_daemon_dashboard")
                case "run":
                    task = " ".join(parts[1:]).strip()
                    if not task:
                        status.status_text = "用法: /bdaemon run <任务描述>"
                        return
                    await _execute("browser_daemon_run", task_instruction=task)
                case "list" | "runs":
                    limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
                    await _execute("browser_daemon_list_runs", limit=limit)
                case "status":
                    if len(parts) < 2:
                        status.status_text = "用法: /bdaemon status <运行ID>"
                        return
                    await _execute("browser_daemon_run_status", run_id=parts[1])
                case "watch":
                    if len(parts) < 2:
                        status.status_text = "用法: /bdaemon watch <运行ID> [超时毫秒]"
                        return
                    timeout_ms = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30000
                    await _execute("browser_daemon_watch", run_id=parts[1], timeout_ms=timeout_ms)
                case "reply":
                    if len(parts) < 3:
                        status.status_text = "用法: /bdaemon reply <运行ID> <指令>"
                        return
                    await _execute(
                        "browser_daemon_reply",
                        run_id=parts[1],
                        instruction=" ".join(parts[2:]),
                    )
                case "resume":
                    if len(parts) < 2:
                        status.status_text = "用法: /bdaemon resume <运行ID> [指令]"
                        return
                    instruction = " ".join(parts[2:]) if len(parts) > 2 else ""
                    await _execute(
                        "browser_daemon_resume",
                        run_id=parts[1],
                        instruction=instruction,
                    )
                case "abort":
                    if len(parts) < 2:
                        status.status_text = "用法: /bdaemon abort <运行ID> [原因]"
                        return
                    reason = " ".join(parts[2:]) if len(parts) > 2 else ""
                    await _execute("browser_daemon_abort", run_id=parts[1], reason=reason)
                case "manual" | "manual-control":
                    if len(parts) < 2:
                        status.status_text = "用法: /bdaemon manual <运行ID> [原因]"
                        return
                    reason = " ".join(parts[2:]) if len(parts) > 2 else ""
                    await _execute("browser_daemon_manual_control", run_id=parts[1], reason=reason)
                case _:
                    status.status_text = "未知 bdaemon 子命令"
                    chat.mount(
                        Markdown(
                            "可用子命令：`health`、`start`、`dashboard`、`run`、"
                            "`list`、`status`、`watch`、`reply`、`resume`、`abort`、`manual`",
                            classes="agent-msg",
                        )
                    )
        finally:
            self.query_one(Spinner)._active = False
            self._set_input_enabled(True)
            if (
                not status.status_text.startswith("用法")
                and status.status_text != "未知 bdaemon 子命令"
            ):
                status.status_text = "就绪"

    def _show_tasks(self) -> None:
        browser = self.query_one(BrowserPanel)
        browser.show_panel = True
        browser.refresh_tasks(self.engine)
        self._refresh_task_panel()

    @work(exclusive=True, exit_on_error=False)
    async def _refresh_task_panel(self) -> None:
        from naumi_agent.ui.task_panel import render_task_panel

        browser = self.query_one(BrowserPanel)
        for child in list(browser.children):
            child.remove()
        browser.mount(Static("📋 任务面板", classes="browser-section-title"))
        browser.mount(
            Static(
                Text.from_ansi(await render_task_panel(self.engine, limit=15)),
                classes="browser-status",
            )
        )

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
