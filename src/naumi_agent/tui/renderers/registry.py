"""TUI renderer registry — table-driven dispatch from UIMessage → Textual widgets.

Usage::

    renderer = TUIRenderer()
    renderer.render(msg, chat_panel, status_bar, todo_bar)

Each renderer function receives the message plus the UI panels it needs
to update. The registry dispatches by message type — no if/elif chain.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import rich.markup
from rich.text import Text
from textual.widgets import Static

from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    CompletionReceiptMessage,
    ContextCompactMessage,
    ErrorMessage,
    HookTraceMessage,
    PermissionBubbleMessage,
    RecoveryMessage,
    RuntimeNotificationMessage,
    RuntimeStatusMessage,
    SubagentEventMessage,
    TeamEventMessage,
    ThinkingMessage,
    TodoStatusMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
)
from naumi_agent.ui.render_cache import RenderCacheStats, RenderLRUCache, message_render_cache_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type alias for the ChatPanel duck-typed interface
# ---------------------------------------------------------------------------

# The ChatPanel-like object must expose these methods.
# We use protocol duck-typing rather than importing the real class
# to avoid circular imports (tui.app imports renderers).
ChatPanelLike = Any
StatusBarLike = Any
TodoBarLike = Any


# ---------------------------------------------------------------------------
# Individual renderers — one per message type
# ---------------------------------------------------------------------------


def _render_thinking(
    msg: ThinkingMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    if msg.phase == "start":
        chat.start_thinking()
        status.status_text = "💭 思考中..."
    elif msg.phase == "delta":
        chat.add_thinking_chunk(msg.content)
    else:
        chat.end_thinking()


def _render_assistant_stream(
    msg: AssistantStreamMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    if msg.phase == "start":
        chat.start_response()
        status.status_text = "✍ 生成回复..."
    elif msg.phase == "token":
        chat.add_response_token(msg.content)
    # end — nothing extra needed


def _render_tool_prepare(
    msg: ToolPrepareMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    if msg.phase == "end":
        chat.end_tool_prepare()
        return
    parts = [f"准备 {msg.tool_name}"]
    if msg.path:
        parts.append(msg.path)
    if msg.content_lines and msg.content_chars:
        parts.append(f"内容 {msg.content_lines} 行")
    if msg.elapsed_ms >= 1000:
        parts.append(f"{msg.elapsed_ms / 1000:.1f}s")
    text = " · ".join(parts)
    chat.update_tool_prepare(text)
    status.status_text = text


def _render_tool_use(
    msg: ToolUseMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    from naumi_agent.main import _tool_label

    display_arg = msg.primary_arg or msg.file_path or msg.command or msg.query or msg.url
    label_args = (
        json.dumps({"path": display_arg}, ensure_ascii=False)
        if display_arg else msg.args_summary
    )
    label = _tool_label(msg.tool_name, label_args)
    chat.start_tool(label)
    status.status_text = f"{label}..."


def _render_tool_result(
    msg: ToolResultMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    from naumi_agent.main import _tool_label

    label = _tool_label(msg.tool_name)
    chat.end_tool(
        label,
        msg.status,
        msg.duration_ms,
        _highlightable_tool_preview(msg),
    )


def _highlightable_tool_preview(msg: ToolResultMessage) -> str:
    """Wrap raw previews with a fence when adapter supplied a highlight hint."""
    content = msg.content_preview
    if not content or "```" in content:
        return content
    if msg.preview_format == "diff":
        return f"```diff\n{content}\n```"
    if msg.preview_format == "code":
        language = msg.preview_language or "text"
        return f"```{language}\n{content}\n```"
    return content


def _render_hook_trace(
    msg: HookTraceMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    status_label = "拦截" if msg.aborted else "异常" if msg.error else "触发"
    style = "yellow" if msg.aborted else "red" if msg.error else "magenta"
    suffix = f" · {rich.markup.escape(msg.error)}" if msg.error else ""
    chat.mount(
        Static(
            Text.from_markup(
                f"  [{style}]hook {rich.markup.escape(status_label)}: "
                f"{rich.markup.escape(msg.point)} → {rich.markup.escape(msg.callback)} "
                f"({msg.duration_ms}ms){suffix}[/{style}]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = f"hook {status_label}: {msg.point}"


def _render_todo_status(
    msg: TodoStatusMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    from naumi_agent.main import _format_todo_bar

    if todo is not None:
        todo_text = _format_todo_bar({
            "count": msg.total_count,
            "open_count": msg.open_count,
            "completed_count": msg.completed_count,
            "items": list(msg.items),
            "summary": msg.summary_text,
        })
        todo.todo_text = todo_text or ""
    status.status_text = f"todo 已更新：{msg.source}"


def _render_subagent_event(
    msg: SubagentEventMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    style = (
        "green" if msg.status == "completed"
        else "red" if msg.status in {"error", "failed"}
        else "cyan"
    )
    suffix = f" · {rich.markup.escape(msg.message)}" if msg.message else ""
    chat.mount(
        Static(
            Text.from_markup(
                f"  [{style}]subagent {rich.markup.escape(msg.status)}: "
                f"{rich.markup.escape(msg.agent_name)} / "
                f"{rich.markup.escape(msg.task_id)}{suffix}[/{style}]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = f"subagent {msg.status}: {msg.agent_name}"


def _render_permission_bubble(
    msg: PermissionBubbleMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    blocked_statuses = {
        "blocked", "blocked_by_hook", "blocked_by_plan_mode",
        "denied", "confirmation_error",
    }
    style = (
        "red" if msg.status in blocked_statuses
        else "green" if msg.status in {"confirmed", "bypass_enabled"}
        else "yellow"
    )
    suffix = (
        f" · {rich.markup.escape(msg.reason[:120])}" if msg.reason else ""
    )
    status_text = rich.markup.escape(f"[{msg.status}]")
    chat.mount(
        Static(
            Text.from_markup(
                f"  [{style}]permission bubble: "
                f"{rich.markup.escape(msg.agent_name)} → "
                f"{rich.markup.escape(msg.tool_name)} "
                f"{status_text}{suffix}[/{style}]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = (
        f"permission bubble: {msg.agent_name} → {msg.tool_name}"
    )


def _render_team_event(
    msg: TeamEventMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    style = (
        "red" if msg.priority == "critical"
        else "yellow" if msg.priority == "high"
        else "cyan"
    )
    suffix = f" · {msg.message[:120]}" if msg.message else ""
    priority_text = rich.markup.escape(f"[{msg.priority}]")
    chat.mount(
        Static(
            Text.from_markup(
                f"  [{style}]team {msg.event_type}: "
                f"{msg.sender} → {msg.recipient} "
                f"{priority_text}{suffix}[/{style}]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = f"team {msg.event_type}: {msg.sender} → {msg.recipient}"


def _render_runtime_notification(
    msg: RuntimeNotificationMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    suffix = f" · {msg.preview[:160]}" if msg.preview else ""
    chat.mount(
        Static(
            Text(f"  {msg.title}: {msg.source} ×{msg.count}{suffix}", style="cyan"),
            classes="tool-done",
        )
    )
    status.status_text = f"{msg.title}: {msg.source} ×{msg.count}"


def _render_runtime_status(
    msg: RuntimeStatusMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    if msg.phase == "run_started":
        status.status_text = "⏳ 已接手，准备执行..."
    elif msg.phase == "turn_start":
        if msg.model:
            chat.show_model(msg.model)
        if msg.turn > 1:
            status.status_text = f"🔄 第 {msg.turn} 轮..."
    elif msg.phase == "perf_phase":
        label = msg.label or "阶段"
        status.status_text = f"⏱ {label}: {msg.duration_ms}ms"


def _render_context_compact(
    msg: ContextCompactMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    archived_text = (
        f"；归档：{msg.archived_tool_results} 个大型工具结果"
        if msg.archived_tool_results
        else ""
    )
    preserved_text = (
        "；保留：" + rich.markup.escape("、".join(msg.preserved_sections))
        if msg.preserved_sections
        else ""
    )
    warning_text = (
        "；风险：" + rich.markup.escape("；".join(msg.warnings))
        if msg.warnings
        else ""
    )
    chat.mount(
        Static(
            Text.from_markup(
                "[magenta]  context compacted: "
                f"{msg.before} → {msg.after} messages"
                f"{rich.markup.escape(archived_text)}"
                f"{preserved_text}{warning_text}[/magenta]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = "上下文已压缩，运行时状态已保留"


def _render_recovery(
    msg: RecoveryMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    style = (
        "green" if msg.phase == "completed"
        else "red" if msg.phase == "failed"
        else "yellow"
    )
    phase = rich.markup.escape(msg.phase)
    action = rich.markup.escape(msg.action)
    reason = rich.markup.escape(msg.reason)
    before = rich.markup.escape(str(msg.before))
    after = rich.markup.escape(str(msg.after))
    unit = rich.markup.escape(msg.unit)
    suffix = (
        f" {before} → {after} {unit}"
        if msg.after != "?"
        else f" before={before}"
    )
    chat.mount(
        Static(
            Text.from_markup(
                f"  [{style}]recovery {phase}: "
                f"{action} ({reason}){suffix}[/{style}]"
            ),
            classes="tool-done",
        )
    )
    status.status_text = f"恢复流程：{msg.phase}"


def _render_error(
    msg: ErrorMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    chat.start_response()
    chat.add_response_token(f"**错误**: {msg.message}")


def _render_completion_receipt(
    msg: CompletionReceiptMessage,
    chat: ChatPanelLike,
    status: StatusBarLike,
    todo: TodoBarLike,
) -> None:
    from naumi_agent.tui.completion_receipt import (
        completion_outcome_label,
        format_completion_receipt_text,
    )

    chat.mount(
        Static(
            format_completion_receipt_text(msg.receipt),
            classes="agent-msg",
        )
    )
    status.status_text = f"完成回执：{completion_outcome_label(msg.receipt)}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_RendererFunc = Callable[
    [UIMessage, ChatPanelLike, StatusBarLike, TodoBarLike],
    None,
]


class TUIRenderer:
    """Table-driven renderer: UIMessage → Textual widget updates.

    Usage::

        renderer = TUIRenderer()
        renderer.render(msg, chat_panel, status_bar, todo_bar)
    """

    def __init__(self, *, cache_size: int = 2_048) -> None:
        self._registry: dict[MessageType, _RendererFunc] = {}
        self._rendered_messages: RenderLRUCache[tuple[str, str], bool] = RenderLRUCache(
            cache_size
        )
        self._register_defaults()

    def register(self, msg_type: MessageType, fn: _RendererFunc) -> None:
        """Register or override a renderer for a message type."""
        self._registry[msg_type] = fn
        self._rendered_messages.clear()

    def clear_cache(self) -> None:
        self._rendered_messages.clear()

    def cache_stats(self) -> RenderCacheStats:
        return self._rendered_messages.stats()

    def render(
        self,
        msg: UIMessage,
        chat: ChatPanelLike,
        status: StatusBarLike,
        todo: TodoBarLike,
    ) -> None:
        """Dispatch a UIMessage to the appropriate renderer."""
        key = message_render_cache_key(msg)
        if self._rendered_messages.get(key) is not None:
            return
        fn = self._registry.get(msg.type)
        if fn is None:
            logger.debug("No TUI renderer for message type: %s", msg.type)
            return
        fn(msg, chat, status, todo)
        self._rendered_messages.set(key, True)

    def _register_defaults(self) -> None:
        """Register the default set of renderers."""
        self._registry.update({
            MessageType.THINKING: _render_thinking,
            MessageType.ASSISTANT_STREAM: _render_assistant_stream,
            MessageType.TOOL_PREPARE: _render_tool_prepare,
            MessageType.TOOL_USE: _render_tool_use,
            MessageType.TOOL_RESULT: _render_tool_result,
            MessageType.HOOK_TRACE: _render_hook_trace,
            MessageType.TODO_STATUS: _render_todo_status,
            MessageType.SUBAGENT_EVENT: _render_subagent_event,
            MessageType.PERMISSION_BUBBLE: _render_permission_bubble,
            MessageType.TEAM_EVENT: _render_team_event,
            MessageType.RUNTIME_NOTIFICATION: _render_runtime_notification,
            MessageType.RUNTIME_STATUS: _render_runtime_status,
            MessageType.CONTEXT_COMPACT: _render_context_compact,
            MessageType.RECOVERY: _render_recovery,
            MessageType.ERROR: _render_error,
            MessageType.COMPLETION_RECEIPT: _render_completion_receipt,
        })
