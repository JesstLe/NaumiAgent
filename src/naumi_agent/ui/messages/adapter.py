"""Adapter that converts raw engine (event_name, data) pairs into typed UIMessages.

The adapter is stateless and pure: given an engine event, it produces zero or
one UIMessage.  It does NOT store large tool argument bodies — only summaries
and references, keeping memory usage bounded even during long sessions.

Usage::

    adapter = EngineEventAdapter()
    msg = adapter.adapt("tool_end", {"name": "file_read", "status": "success", ...})
    if msg is not None:
        renderer.render(msg)  # CLI or TUI dispatches by msg.type
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

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

logger = logging.getLogger(__name__)

# Maximum length for tool argument summaries (avoids storing huge payloads).
_ARGS_SUMMARY_MAX = 200
_CONTENT_PREVIEW_MAX = 500
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+#.-]*)[^\n]*\n", re.MULTILINE)


def _safe_str(value: Any, default: str = "") -> str:
    """Extract a string from event data, defaulting gracefully."""
    if value is None:
        return default
    return str(value)


def _safe_int(value: Any, default: int = 0) -> int:
    """Extract an int from event data, defaulting gracefully."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Extract a finite float from event data, defaulting gracefully."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Extract a bool from event data, defaulting gracefully."""
    if value is None:
        return default
    return bool(value)


def _summarize_args(args_raw: Any) -> str:
    """Build a compact argument summary string from raw tool arguments."""
    if not args_raw:
        return ""
    text = args_raw if isinstance(args_raw, str) else json.dumps(args_raw, ensure_ascii=False)
    if len(text) <= _ARGS_SUMMARY_MAX:
        return text
    return text[: _ARGS_SUMMARY_MAX - 1] + "…"


def _content_preview(content: Any) -> tuple[str, int]:
    """Return (truncated_preview, full_length) for tool output."""
    if not content:
        return "", 0
    text = str(content)
    length = len(text)
    if length <= _CONTENT_PREVIEW_MAX:
        return text, length
    return text[: _CONTENT_PREVIEW_MAX] + "…", length


def _detect_preview_format(tool_name: str, content: str) -> tuple[str, str]:
    """Infer how a frontend should highlight a tool output preview."""
    text = content.strip()
    if not text:
        return "text", ""

    fence = _FENCE_RE.search(text)
    if fence:
        language = fence.group(1).strip().lower()
        if language == "diff":
            return "diff", "diff"
        return "code", language or _guess_language_from_tool(tool_name)

    lines = [line for line in text.splitlines()[:12] if line.strip()]
    if _looks_like_diff_lines(lines):
        return "diff", "diff"
    if _looks_like_markdown_lines(lines):
        return "markdown", "markdown"
    return "text", ""


def _looks_like_diff_lines(lines: list[str]) -> bool:
    return any(line.startswith(("---", "+++", "@@")) for line in lines) and any(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in lines
    )


def _looks_like_markdown_lines(lines: list[str]) -> bool:
    return any(
        line.startswith(("# ", "## ", "### ", "- ", "* ", "> ", "|"))
        for line in lines
    )


def _guess_language_from_tool(tool_name: str) -> str:
    if tool_name == "code_execute":
        return "python"
    return ""


def _to_tuple(value: Any) -> tuple[Any, ...]:
    """Ensure a list is stored as an immutable tuple."""
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return ()


def _parse_args_dict(args_raw: Any) -> dict[str, str]:
    """Parse tool arguments into a flat string dict for field extraction."""
    if not args_raw:
        return {}
    if isinstance(args_raw, dict):
        return {k: str(v) for k, v in args_raw.items() if v is not None}
    try:
        parsed = json.loads(args_raw)
        if isinstance(parsed, dict):
            return {k: str(v) for k, v in parsed.items() if v is not None}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _extract_primary_arg(args: dict[str, str]) -> str:
    """Extract the most informative argument for card display."""
    for key in (
        "file_path", "path", "target_path", "filename",
        "command", "query", "url", "task", "description", "goal",
    ):
        val = args.get(key)
        if val:
            if len(val) > 50:
                return val[:47] + "…"
            return val
    return ""


class EngineEventAdapter:
    """Stateless converter: raw engine event → typed UIMessage.

    Returns ``None`` for events that have no user-visible representation
    (e.g. internal perf events that only update a counter).
    """

    def adapt(self, event: str, data: dict[str, Any]) -> UIMessage | None:
        """Convert a single engine event into a typed UIMessage.

        Args:
            event: Engine event name (e.g. ``"tool_start"``).
            data: Engine event payload dict.

        Returns:
            A typed UIMessage, or ``None`` if the event is not user-visible.
        """
        handler = self._DISPATCH.get(event)
        if handler is not None:
            return handler(self, event, data)

        # Unknown events are logged but not surfaced to the UI.
        logger.debug("Unhandled engine event: %s", event)
        return None

    # ------------------------------------------------------------------
    # Individual converters — one per engine event kind
    # ------------------------------------------------------------------

    def _adapt_run_started(
        self, event: str, data: dict[str, Any]
    ) -> RuntimeStatusMessage:
        return RuntimeStatusMessage(
            type=MessageType.RUNTIME_STATUS,
            phase="run_started",
            label="已接手，准备执行...",
            raw_event=event,
            raw_data=None,
        )

    def _adapt_completion_receipt(
        self,
        event: str,
        data: dict[str, Any],
    ) -> CompletionReceiptMessage:
        from naumi_agent.runs.models import CompletionReceipt

        receipt_data = data or {
            "schema_version": 1,
            "receipt_id": "uninitialized",
            "run_id": "uninitialized",
            "outcome": "failed",
            "git_state": {"available": False, "dirty": False},
        }
        return CompletionReceiptMessage(
            type=MessageType.COMPLETION_RECEIPT,
            receipt=CompletionReceipt.from_dict(receipt_data),
            message_id=str(data.get("receipt_id") or ""),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_turn_start(
        self, event: str, data: dict[str, Any]
    ) -> RuntimeStatusMessage:
        return RuntimeStatusMessage(
            type=MessageType.RUNTIME_STATUS,
            phase="turn_start",
            model=_safe_str(data.get("model")),
            turn=_safe_int(data.get("turn")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_perf_phase(
        self, event: str, data: dict[str, Any]
    ) -> RuntimeStatusMessage:
        return RuntimeStatusMessage(
            type=MessageType.RUNTIME_STATUS,
            phase="perf_phase",
            label=_safe_str(data.get("label") or data.get("phase")),
            duration_ms=_safe_int(data.get("duration_ms")),
            turn=_safe_int(data.get("turn")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_latency_metric(
        self, event: str, data: dict[str, Any]
    ) -> RuntimeStatusMessage:
        return RuntimeStatusMessage(
            type=MessageType.RUNTIME_STATUS,
            phase="latency_metric",
            label=_safe_str(data.get("label") or data.get("metric")),
            duration_ms=_safe_int(data.get("duration_ms")),
            turn=_safe_int(data.get("turn")),
            raw_event=event,
            raw_data=None,
        )

    # -- thinking --

    def _adapt_thinking_start(
        self, event: str, data: dict[str, Any]
    ) -> ThinkingMessage:
        return ThinkingMessage(
            type=MessageType.THINKING,
            phase="start",
            raw_event=event,
            raw_data=None,
        )

    def _adapt_thinking_delta(
        self, event: str, data: dict[str, Any]
    ) -> ThinkingMessage:
        return ThinkingMessage(
            type=MessageType.THINKING,
            phase="delta",
            content=_safe_str(data.get("content")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_thinking_end(
        self, event: str, data: dict[str, Any]
    ) -> ThinkingMessage:
        return ThinkingMessage(
            type=MessageType.THINKING,
            phase="end",
            content=_safe_str(data.get("content")),
            raw_event=event,
            raw_data=None,
        )

    # -- assistant stream --

    def _adapt_response_start(
        self, event: str, data: dict[str, Any]
    ) -> AssistantStreamMessage:
        return AssistantStreamMessage(
            type=MessageType.ASSISTANT_STREAM,
            phase="start",
            raw_event=event,
            raw_data=None,
        )

    def _adapt_token(
        self, event: str, data: dict[str, Any]
    ) -> AssistantStreamMessage:
        return AssistantStreamMessage(
            type=MessageType.ASSISTANT_STREAM,
            phase="token",
            content=_safe_str(data.get("content")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_response_end(
        self, event: str, data: dict[str, Any]
    ) -> AssistantStreamMessage:
        return AssistantStreamMessage(
            type=MessageType.ASSISTANT_STREAM,
            phase="end",
            raw_event=event,
            raw_data=None,
        )

    # -- tool prepare --

    def _adapt_tool_prepare_start(
        self, event: str, data: dict[str, Any]
    ) -> ToolPrepareMessage:
        return ToolPrepareMessage(
            type=MessageType.TOOL_PREPARE,
            tool_name=_safe_str(data.get("name")),
            tool_call_id=_safe_str(data.get("call_id") or data.get("tool_call_id")),
            phase="start",
            path=_safe_str(data.get("path")),
            argument_chars=_safe_int(data.get("argument_chars")),
            content_chars=_safe_int(data.get("content_chars")),
            content_lines=_safe_int(data.get("content_lines")),
            elapsed_ms=_safe_int(data.get("elapsed_ms")),
            todo_total=_safe_int(data.get("todo_total")),
            todo_completed=_safe_int(data.get("todo_completed")),
            todo_open=_safe_int(data.get("todo_open")),
            todo_items=_to_tuple(data.get("todo_items")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_tool_prepare_snapshot(
        self, event: str, data: dict[str, Any]
    ) -> ToolPrepareMessage:
        return ToolPrepareMessage(
            type=MessageType.TOOL_PREPARE,
            tool_name=_safe_str(data.get("name")),
            tool_call_id=_safe_str(data.get("call_id") or data.get("tool_call_id")),
            phase="snapshot",
            path=_safe_str(data.get("path")),
            argument_chars=_safe_int(data.get("argument_chars")),
            content_chars=_safe_int(data.get("content_chars")),
            content_lines=_safe_int(data.get("content_lines")),
            elapsed_ms=_safe_int(data.get("elapsed_ms")),
            todo_total=_safe_int(data.get("todo_total")),
            todo_completed=_safe_int(data.get("todo_completed")),
            todo_open=_safe_int(data.get("todo_open")),
            todo_items=_to_tuple(data.get("todo_items")),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_tool_prepare_end(
        self, event: str, data: dict[str, Any]
    ) -> ToolPrepareMessage:
        return ToolPrepareMessage(
            type=MessageType.TOOL_PREPARE,
            tool_name=_safe_str(data.get("name")),
            tool_call_id=_safe_str(data.get("call_id") or data.get("tool_call_id")),
            phase="end",
            path=_safe_str(data.get("path")),
            argument_chars=_safe_int(data.get("argument_chars")),
            content_chars=_safe_int(data.get("content_chars")),
            content_lines=_safe_int(data.get("content_lines")),
            elapsed_ms=_safe_int(data.get("elapsed_ms")),
            todo_total=_safe_int(data.get("todo_total")),
            todo_completed=_safe_int(data.get("todo_completed")),
            todo_open=_safe_int(data.get("todo_open")),
            todo_items=_to_tuple(data.get("todo_items")),
            raw_event=event,
            raw_data=None,
        )

    # -- tool execution --

    def _adapt_tool_start(
        self, event: str, data: dict[str, Any]
    ) -> ToolUseMessage:
        args_raw = data.get("args")
        args_parsed = _parse_args_dict(args_raw)
        # Extract structured fields BEFORE truncation
        primary_arg = _extract_primary_arg(args_parsed)
        return ToolUseMessage(
            type=MessageType.TOOL_USE,
            tool_name=_safe_str(data.get("name")),
            tool_call_id=_safe_str(data.get("call_id") or data.get("tool_call_id")),
            args_summary=_summarize_args(args_raw),
            args_raw="",
            primary_arg=primary_arg,
            file_path=args_parsed.get("file_path", args_parsed.get("path", "")),
            command=args_parsed.get("command", ""),
            query=args_parsed.get("query", ""),
            url=args_parsed.get("url", ""),
            raw_event=event,
            raw_data=None,
        )

    def _adapt_tool_end(
        self, event: str, data: dict[str, Any]
    ) -> ToolResultMessage:
        raw_content = data.get("content", "")
        preview, length = _content_preview(raw_content)
        preview_format, preview_language = _detect_preview_format(
            _safe_str(data.get("name")),
            preview,
        )
        return ToolResultMessage(
            type=MessageType.TOOL_RESULT,
            tool_name=_safe_str(data.get("name")),
            tool_call_id=_safe_str(data.get("call_id") or data.get("tool_call_id")),
            status=_safe_str(data.get("status")),
            duration_ms=_safe_int(data.get("duration_ms")),
            content_preview=preview,
            content_length=length,
            preview_format=preview_format,
            preview_language=preview_language,
            content_truncated=length > len(preview),
            raw_event=event,
            raw_data=None,
        )

    # -- hook trace --

    def _adapt_hook_trace(
        self, event: str, data: dict[str, Any]
    ) -> HookTraceMessage:
        return HookTraceMessage(
            type=MessageType.HOOK_TRACE,
            point=_safe_str(data.get("point")),
            callback=_safe_str(data.get("callback")),
            duration_ms=_safe_int(data.get("duration_ms")),
            error=_safe_str(data.get("error")),
            aborted=_safe_bool(data.get("aborted")),
            raw_event=event,
            raw_data=None,
        )

    # -- task snapshot --

    def _adapt_task_snapshot(
        self, event: str, data: dict[str, Any]
    ) -> TodoStatusMessage:
        raw_items = data.get("items", [])
        items_tuple = tuple(raw_items) if isinstance(raw_items, list) else ()
        return TodoStatusMessage(
            type=MessageType.TODO_STATUS,
            source=_safe_str(data.get("source")),
            total_count=_safe_int(data.get("count")),
            open_count=_safe_int(data.get("open_count")),
            completed_count=_safe_int(data.get("completed_count")),
            items=items_tuple,
            summary_text=_safe_str(data.get("summary")),
            raw_event=event,
            raw_data=None,
        )

    # -- subagent --

    def _adapt_subagent_event(
        self, event: str, data: dict[str, Any]
    ) -> SubagentEventMessage:
        return SubagentEventMessage(
            type=MessageType.SUBAGENT_EVENT,
            agent_name=_safe_str(data.get("agent_name")),
            task_id=_safe_str(data.get("task_id")),
            status=_safe_str(data.get("status")),
            description=_safe_str(data.get("description")),
            message=_safe_str(data.get("message")),
            tokens=max(0, _safe_int(data.get("tokens"))),
            cost=max(0.0, _safe_float(data.get("cost"))),
            timestamp=max(0.0, _safe_float(data.get("timestamp"))),
            raw_event=event,
            raw_data=None,
        )

    # -- permission --

    def _adapt_permission_bubble(
        self, event: str, data: dict[str, Any]
    ) -> PermissionBubbleMessage:
        return PermissionBubbleMessage(
            type=MessageType.PERMISSION_BUBBLE,
            agent_name=_safe_str(data.get("agent_name")),
            tool_name=_safe_str(data.get("tool_name")),
            status=_safe_str(data.get("status")),
            reason=_safe_str(data.get("reason")),
            requires_confirmation=_safe_bool(data.get("requires_confirmation")),
            raw_event=event,
            raw_data=None,
        )

    # -- team --

    def _adapt_team_event(
        self, event: str, data: dict[str, Any]
    ) -> TeamEventMessage:
        return TeamEventMessage(
            type=MessageType.TEAM_EVENT,
            event_type=_safe_str(data.get("event_type")),
            sender=_safe_str(data.get("sender")),
            recipient=_safe_str(data.get("recipient", "广播")),
            priority=_safe_str(data.get("priority", "normal")),
            message=_safe_str(data.get("message")),
            raw_event=event,
            raw_data=None,
        )

    # -- runtime notification --

    def _adapt_runtime_notification(
        self, event: str, data: dict[str, Any]
    ) -> RuntimeNotificationMessage:
        return RuntimeNotificationMessage(
            type=MessageType.RUNTIME_NOTIFICATION,
            source=_safe_str(data.get("source", "runtime")),
            title=_safe_str(data.get("title", "运行时通知")),
            count=_safe_int(data.get("count")),
            preview=_safe_str(data.get("preview")),
            raw_event=event,
            raw_data=None,
        )

    # -- context compact --

    def _adapt_context_compacted(
        self, event: str, data: dict[str, Any]
    ) -> ContextCompactMessage:
        preserved = data.get("preserved_sections", [])
        warnings = data.get("warnings", [])
        return ContextCompactMessage(
            type=MessageType.CONTEXT_COMPACT,
            before=_safe_int(data.get("before")),
            after=_safe_int(data.get("after")),
            archived_tool_results=_safe_int(data.get("archived_tool_results")),
            preserved_sections=_to_tuple(preserved),
            warnings=_to_tuple(warnings),
            raw_event=event,
            raw_data=None,
        )

    # -- recovery --

    def _adapt_recovery_event(
        self, event: str, data: dict[str, Any]
    ) -> RecoveryMessage:
        return RecoveryMessage(
            type=MessageType.RECOVERY,
            reason=_safe_str(data.get("reason")),
            action=_safe_str(data.get("action")),
            phase=_safe_str(data.get("phase")),
            before=data.get("before", "?"),
            after=data.get("after", "?"),
            unit=_safe_str(data.get("unit")),
            raw_event=event,
            raw_data=None,
        )

    # -- error --

    def _adapt_error(
        self, event: str, data: dict[str, Any]
    ) -> ErrorMessage:
        return ErrorMessage(
            type=MessageType.ERROR,
            message=_safe_str(data.get("message")),
            raw_event=event,
            raw_data=None,
        )

    # ------------------------------------------------------------------
    # Dispatch table — maps engine event name to converter method
    # ------------------------------------------------------------------

    _DISPATCH: dict[str, Any] = {
        "completion_receipt": _adapt_completion_receipt,
        "run_started": _adapt_run_started,
        "turn_start": _adapt_turn_start,
        "perf_phase": _adapt_perf_phase,
        "latency_metric": _adapt_latency_metric,
        "thinking_start": _adapt_thinking_start,
        "thinking_delta": _adapt_thinking_delta,
        "thinking_end": _adapt_thinking_end,
        "response_start": _adapt_response_start,
        "token": _adapt_token,
        "response_end": _adapt_response_end,
        "tool_prepare_start": _adapt_tool_prepare_start,
        "tool_prepare_snapshot": _adapt_tool_prepare_snapshot,
        "tool_prepare_end": _adapt_tool_prepare_end,
        "tool_start": _adapt_tool_start,
        "tool_end": _adapt_tool_end,
        "hook_trace": _adapt_hook_trace,
        "task_snapshot": _adapt_task_snapshot,
        "subagent_event": _adapt_subagent_event,
        "permission_bubble": _adapt_permission_bubble,
        "team_event": _adapt_team_event,
        "runtime_notification": _adapt_runtime_notification,
        "context_compacted": _adapt_context_compacted,
        "recovery_event": _adapt_recovery_event,
        "error": _adapt_error,
    }
