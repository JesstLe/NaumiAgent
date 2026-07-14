"""CLI renderer registry — table-driven dispatch from UIMessage → ANSI text.

Usage::

    renderer = CLIRenderer()
    ansi_text = renderer.render(msg)
    if ansi_text:
        cli.append_live(ansi_text)

Renderers return a string (ANSI-formatted text) or ``None`` if the message
has no direct output representation (e.g. perf_phase only updates status).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable

from naumi_agent.clipboard import strip_ansi
from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ContextCompactMessage,
    ErrorMessage,
    HookTraceMessage,
    PermissionBubbleMessage,
    RecoveryMessage,
    RuntimeNotificationMessage,
    RuntimeStatusMessage,
    SubagentEventMessage,
    SystemNoticeMessage,
    TeamEventMessage,
    ThinkingMessage,
    TodoStatusMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserMessage,
)
from naumi_agent.ui.render_cache import RenderCacheStats, RenderLRUCache, message_render_cache_key

# Separator helpers (matching existing main.py style)
_SEP_THIN = "─"
_SEP_THICK = "━"


def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _sep(thin: bool = True) -> str:
    char = _SEP_THIN if thin else _SEP_THICK
    return f"\033[2m{char * _term_width()}\033[0m"


# ---------------------------------------------------------------------------
# Individual renderers — one per message type
# ---------------------------------------------------------------------------


def _render_user(msg: UserMessage) -> str | None:
    prompt = "\033[32m❯\033[0m"
    if msg.content:
        preview = msg.content
        if msg.is_command:
            prompt = "\033[33m❯\033[0m"
    else:
        preview = ""
    return f"{prompt} {preview}\n"


def _render_system_notice(msg: SystemNoticeMessage) -> str | None:
    color = {
        "success": "32",
        "warning": "33",
        "error": "31",
        "debug": "2",
    }.get(msg.level, "36")
    return f"\033[{color}m{msg.content}\033[0m\n"


def _render_thinking(msg: ThinkingMessage, *, show_reasoning: bool = False) -> str | None:
    if msg.phase == "start":
        return f"{_sep()}\n\033[2m💭 思考中...\033[0m\n"
    if msg.phase == "delta":
        return f"\033[2m{msg.content}\033[0m" if show_reasoning else None
    return ""


def _render_assistant_stream(msg: AssistantStreamMessage) -> str | None:
    if msg.phase == "start":
        return f"{_sep(thin=False)}\n"
    if msg.phase == "token":
        return msg.content
    # end — nothing extra
    return ""


def _render_tool_prepare(msg: ToolPrepareMessage) -> str | None:

    parts = [f"准备 {msg.tool_name}"]
    if msg.path:
        parts.append(msg.path)
    if msg.content_lines and msg.content_chars:
        parts.append(f"内容 {msg.content_lines} 行")
    if msg.argument_chars:
        parts.append(f"参数 {msg.argument_chars} 字符")
    if msg.elapsed_ms >= 1000:
        parts.append(f"{msg.elapsed_ms / 1000:.1f}s")
    if msg.phase == "end":
        return None  # activity bar cleared by caller
    text = " · ".join(parts)
    return f"\033[2m  {text}\033[0m\n"


def _render_tool_use(msg: ToolUseMessage) -> str | None:
    from naumi_agent.main import _tool_label

    # Use structured primary_arg (path/command/query) over truncated args_summary
    display_arg = msg.primary_arg or msg.file_path or msg.command or msg.query or msg.url
    label_args = (
        json.dumps({"path": display_arg}, ensure_ascii=False)
        if display_arg else msg.args_summary
    )
    label = _tool_label(msg.tool_name, label_args)
    return _tool_card_ansi(label, status="running")


def _render_tool_result(msg: ToolResultMessage) -> str | None:
    from naumi_agent.main import _capture, _print_tool_output, _tool_label

    label = _tool_label(msg.tool_name)
    card_status = "success" if msg.status == "success" else msg.status or "error"
    parts = [_tool_card_ansi(label, status=card_status, duration_ms=msg.duration_ms)]
    if msg.content_preview:
        # Reuse the existing diff/code highlighting (uses module-level console
        # that _capture can intercept).
        _name = msg.tool_name
        _content = _highlightable_tool_preview(msg)
        parts.append(_capture(lambda: _print_tool_output(_name, _content)))
    return "".join(parts)


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


def _render_hook_trace(msg: HookTraceMessage) -> str | None:
    status = "拦截" if msg.aborted else "异常" if msg.error else "触发"
    color = "33" if msg.aborted else "31" if msg.error else "35"
    suffix = f" · {msg.error}" if msg.error else ""
    return (
        f"\033[{color}m  hook {status}: "
        f"{msg.point} → {msg.callback} ({msg.duration_ms}ms){suffix}\033[0m\n"
    )


def _render_todo_status(msg: TodoStatusMessage) -> str | None:
    # Todo is rendered in the bottom bar, not in output area
    return None


def _render_subagent_event(msg: SubagentEventMessage) -> str | None:
    color = (
        "32" if msg.status == "completed"
        else "31" if msg.status in {"error", "failed"}
        else "36"
    )
    status = {
        "started": "进行中",
        "running": "进行中",
        "completed": "已完成",
        "error": "失败",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(msg.status, msg.status or "状态更新")
    details = []
    if msg.description:
        details.append(f"任务: {strip_ansi(msg.description)}")
    if msg.message:
        details.append(f"最新: {strip_ansi(msg.message)}")
    if msg.tokens or msg.cost:
        details.append(f"资源: {msg.tokens:,} tokens · ${msg.cost:.4f}")
    suffix = "" if not details else "\n    " + "\n    ".join(details)
    return (
        f"\033[{color}m  子智能体 {status}: "
        f"{strip_ansi(msg.agent_name)} / {strip_ansi(msg.task_id)}{suffix}\033[0m\n"
    )


def _render_permission_bubble(msg: PermissionBubbleMessage) -> str | None:
    blocked_statuses = {
        "blocked", "blocked_by_hook", "blocked_by_plan_mode",
        "denied", "confirmation_error",
    }
    color = (
        "31" if msg.status in blocked_statuses
        else "32" if msg.status in {"confirmed", "bypass_enabled"}
        else "33"
    )
    suffix = f" · {msg.reason[:120]}" if msg.reason else ""
    return (
        f"\033[{color}m  permission bubble: "
        f"{msg.agent_name} → {msg.tool_name} [{msg.status}]{suffix}\033[0m\n"
    )


def _render_team_event(msg: TeamEventMessage) -> str | None:
    color = (
        "31" if msg.priority == "critical"
        else "33" if msg.priority == "high"
        else "36"
    )
    suffix = f" · {msg.message[:120]}" if msg.message else ""
    return (
        f"\033[{color}m  team {msg.event_type}: "
        f"{msg.sender} → {msg.recipient} [{msg.priority}]{suffix}\033[0m\n"
    )


def _render_runtime_notification(msg: RuntimeNotificationMessage) -> str | None:
    suffix = f" · {msg.preview[:160]}" if msg.preview else ""
    return f"\033[36m  {msg.title}: {msg.source} ×{msg.count}{suffix}\033[0m\n"


def _render_runtime_status(msg: RuntimeStatusMessage) -> str | None:
    if msg.phase == "run_started":
        return f"{_sep()}\n\033[2m⏳ 已接手，准备执行...\033[0m\n"
    if msg.phase == "turn_start" and msg.model:
        return f"\033[2m  ⚙ {msg.model}\033[0m\n"
    if msg.phase == "perf_phase":
        label = msg.label or "阶段"
        return f"\033[2m  ⏱ {label}: {msg.duration_ms}ms\033[0m\n"
    return None


def _render_context_compact(msg: ContextCompactMessage) -> str | None:
    parts = [f"\033[35m  context compacted: {msg.before} → {msg.after} messages\033[0m"]
    if msg.archived_tool_results:
        parts.append(f"  归档：{msg.archived_tool_results} 个大型工具结果")
    if msg.preserved_sections:
        parts.append("  保留：" + "、".join(msg.preserved_sections))
    if msg.warnings:
        parts.append("  风险：" + "；".join(msg.warnings))
    return "\n".join(parts) + "\n"


def _render_recovery(msg: RecoveryMessage) -> str | None:
    color = (
        "32" if msg.phase == "completed"
        else "31" if msg.phase == "failed"
        else "33"
    )
    suffix = (
        f" {msg.before} → {msg.after} {msg.unit}"
        if msg.after != "?"
        else f" before={msg.before}"
    )
    return (
        f"\033[{color}m  recovery {msg.phase}: {msg.action} "
        f"({msg.reason}){suffix}\033[0m\n"
    )


def _render_error(msg: ErrorMessage) -> str | None:
    return f"\033[31m错误: {msg.message}\033[0m\n"


# ---------------------------------------------------------------------------
# Tool card ANSI helper
# ---------------------------------------------------------------------------


_TOOL_CARD_STYLES: dict[str, tuple[str, str, str]] = {
    "running": ("cyan", "⏳", "running"),
    "success": ("green", "✓", "success"),
    "error": ("red", "✗", "error"),
    "failed": ("red", "✗", "error"),
    "skipped": ("yellow", "↷", "skipped"),
    "aborted": ("yellow", "!", "aborted"),
}


def _tool_card_ansi(
    label: str,
    *,
    status: str,
    duration_ms: int | float | None = None,
) -> str:
    """Render a compact tool-use card as ANSI text (mirrors main.py style)."""
    style, icon, title_status = _TOOL_CARD_STYLES.get(
        status, ("cyan", "•", status or "tool")
    )
    # Map style names to ANSI codes
    ansi_colors = {
        "cyan": "36",
        "green": "32",
        "red": "31",
        "yellow": "33",
    }
    color = ansi_colors.get(style, "36")
    duration_str = f" ({int(duration_ms)}ms)" if duration_ms is not None else ""
    width = _term_width()
    # Simple bordered card
    title_len = len(f"tool · {title_status}") + 12
    top = (
        f"\033[{color}m╭─ tool · {title_status}"
        f" ─{'─' * max(0, width - title_len)}╮\033[0m"
    )
    body = f"\033[{color}m│\033[0m {icon} {label}{duration_str}"
    bot = f"\033[{color}m╰{'─' * max(0, width - 2)}╯\033[0m"
    return f"{top}\n{body}\n{bot}\n"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_RendererFunc = Callable[[UIMessage], str | None]


class CLIRenderer:
    """Table-driven renderer: UIMessage → ANSI text for the CLI.

    Usage::

        renderer = CLIRenderer()
        text = renderer.render(msg)
        if text is not None:
            cli.append_live(text)
    """

    _NONE_SENTINEL = "<__naumi_none__>"

    def __init__(self, *, cache_size: int = 2_048, show_reasoning: bool = False) -> None:
        self._registry: dict[MessageType, _RendererFunc] = {}
        self._cache: RenderLRUCache[tuple[str, str], str] = RenderLRUCache(cache_size)
        self._show_reasoning = show_reasoning
        self._register_defaults()

    def register(self, msg_type: MessageType, fn: _RendererFunc) -> None:
        """Register or override a renderer for a message type."""
        self._registry[msg_type] = fn
        self._cache.clear()

    def clear_cache(self) -> None:
        self._cache.clear()

    def cache_stats(self) -> RenderCacheStats:
        return self._cache.stats()

    def render(self, msg: UIMessage) -> str | None:
        """Render a UIMessage to ANSI text.

        Returns None if the message has no direct output representation.
        """
        fn = self._registry.get(msg.type)
        if fn is None:
            return None
        key = message_render_cache_key(msg)
        cached = self._cache.get(key)
        if cached is not None:
            return None if cached == self._NONE_SENTINEL else cached
        rendered = fn(msg)
        self._cache.set(key, self._NONE_SENTINEL if rendered is None else rendered)
        return rendered

    def _register_defaults(self) -> None:
        """Register the default set of renderers."""
        self._registry.update({
            MessageType.USER: _render_user,
            MessageType.SYSTEM_NOTICE: _render_system_notice,
            MessageType.THINKING: lambda msg: _render_thinking(
                msg, show_reasoning=self._show_reasoning
            ),
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
        })
