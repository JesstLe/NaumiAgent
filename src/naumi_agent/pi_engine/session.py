"""Stateful translation of a pi event stream into typed UIMessages.

One :class:`PiSessionMapper` tracks one pi run (one ``prompt`` command from
submission to ``agent_settled``).  Feed it every event in arrival order via
:meth:`feed`; it returns the UIMessages that the terminal timeline should
render, and exposes ``final_text`` / ``error`` / ``settled`` for the run
completion payload.

Field shapes below were captured from a live pi 0.85.1 session (see
``docs`` in the module history of this slice): ``message_update`` carries
``assistantMessageEvent`` with ``text_start`` / ``text_delta`` /
``thinking_delta`` / ``toolcall_start`` / ``toolcall_delta`` plus a
``contentIndex``; ``tool_execution_*`` carries ``toolCallId`` / ``toolName``
/ ``args`` / ``result.content``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from naumi_agent.pi_engine.rpc import PiEventType
from naumi_agent.ui.messages.base import MessageType
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ErrorMessage,
    SystemNoticeMessage,
    ThinkingMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
)

_TOOL_RESULT_PREVIEW_CHARS = 1200
_TOOL_RESULT_PREVIEW_LINES = 40
_TOOL_ARGS_PREVIEW_CHARS = 600


def _content_text(content: Any) -> str:
    """Join the text parts of a pi message content array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _preview(text: str) -> tuple[str, bool]:
    """Truncate tool output for display; return (preview, truncated)."""
    if len(text) <= _TOOL_RESULT_PREVIEW_CHARS:
        return text, False
    clipped = text[:_TOOL_RESULT_PREVIEW_CHARS]
    lines = clipped.splitlines()
    if len(lines) > _TOOL_RESULT_PREVIEW_LINES:
        clipped = "\n".join(lines[:_TOOL_RESULT_PREVIEW_LINES])
    return clipped + "\n…（输出已截断）", True


@dataclass
class PiSessionMapper:
    """Translate one pi run's events into UI messages."""

    final_text: str = field(default="", init=False)
    error: str = field(default="", init=False)
    settled: bool = field(default=False, init=False)
    aborted: bool = field(default=False, init=False)

    _turn_text_started: bool = field(default=False, init=False)
    _turn_text: str = field(default="", init=False)
    _turns_text: list[str] = field(default_factory=list, init=False)
    _tool_started_at: dict[str, float] = field(default_factory=dict, init=False)
    _toolcall_args_chars: dict[str, int] = field(default_factory=dict, init=False)

    def reset(self) -> None:
        """Prepare for a brand-new run while keeping per-process state lean."""
        self.final_text = ""
        self.error = ""
        self.settled = False
        self.aborted = False
        self._turn_text_started = False
        self._turn_text = ""
        self._turns_text = []
        self._tool_started_at.clear()
        self._toolcall_args_chars.clear()

    def feed(self, event: dict[str, Any]) -> list[Any]:
        """Translate one pi event into zero or more UIMessages."""
        kind = str(event.get("type") or "")
        handler = _HANDLERS.get(kind)
        if handler is None:
            return []
        return handler(self, event)

    # -- individual event handlers ------------------------------------------

    def _on_message_start(self, event: dict[str, Any]) -> list[Any]:
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return []
        # A fresh assistant message invalidates the previous turn's text.
        self._turn_text_started = False
        self._turn_text = ""
        return []

    def _on_message_update(self, event: dict[str, Any]) -> list[Any]:
        update = event.get("assistantMessageEvent") or {}
        phase = str(update.get("type") or "")
        if phase == "text_start":
            messages: list[Any] = []
            if not self._turn_text_started:
                messages.append(
                    AssistantStreamMessage(
                        type=MessageType.ASSISTANT_STREAM, phase="start"
                    )
                )
                self._turn_text_started = True
            return messages
        if phase == "text_delta":
            delta = str(update.get("delta") or update.get("text") or "")
            if not delta:
                return []
            self._turn_text += delta
            return [
                AssistantStreamMessage(
                    type=MessageType.ASSISTANT_STREAM, phase="token", content=delta
                )
            ]
        if phase == "thinking_start":
            return [
                ThinkingMessage(type=MessageType.THINKING, phase="start")
            ]
        if phase == "thinking_delta":
            delta = str(update.get("delta") or update.get("thinking") or "")
            if not delta:
                return []
            return [
                ThinkingMessage(type=MessageType.THINKING, phase="delta", content=delta)
            ]
        if phase == "toolcall_start":
            content_index = int(update.get("contentIndex") or 0)
            key = f"pi-tc-{content_index}"
            self._toolcall_args_chars[key] = 0
            tool_name = str(update.get("name") or "")
            return [
                ToolPrepareMessage(
                    type=MessageType.TOOL_PREPARE,
                    phase="start",
                    tool_name=tool_name,
                    tool_call_id=key,
                )
            ]
        if phase == "toolcall_delta":
            content_index = int(update.get("contentIndex") or 0)
            key = f"pi-tc-{content_index}"
            delta = str(update.get("delta") or "")
            self._toolcall_args_chars[key] = (
                self._toolcall_args_chars.get(key, 0) + len(delta)
            )
            return [
                ToolPrepareMessage(
                    type=MessageType.TOOL_PREPARE,
                    phase="snapshot",
                    tool_call_id=key,
                    argument_chars=self._toolcall_args_chars[key],
                )
            ]
        return []

    def _on_message_end(self, event: dict[str, Any]) -> list[Any]:
        message = event.get("message") or {}
        role = message.get("role")
        if role == "assistant":
            stop_reason = str(message.get("stopReason") or "")
            if stop_reason == "error":
                detail = str(message.get("error") or "") or _content_text(
                    message.get("content")
                )
                self.error = detail or "模型调用失败。"
                return [
                    ErrorMessage(
                        type=MessageType.ERROR,
                        message=f"pi 引擎模型调用失败：{self.error}",
                    )
                ]
            if self._turn_text_started:
                self._turn_text_started = False
                self._turns_text.append(self._turn_text)
                return [
                    AssistantStreamMessage(
                        type=MessageType.ASSISTANT_STREAM,
                        phase="end",
                        content=self._turn_text,
                    )
                ]
            if self._turn_text:
                self._turns_text.append(self._turn_text)
        return []

    def _on_tool_execution_start(self, event: dict[str, Any]) -> list[Any]:
        tool_call_id = str(event.get("toolCallId") or "")
        tool_name = str(event.get("toolName") or "")
        args = event.get("args") or {}
        self._tool_started_at[tool_call_id] = time.monotonic()
        args_raw = _args_preview(args)
        primary = _primary_argument(args)
        return [
            ToolUseMessage(
                type=MessageType.TOOL_USE,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args_summary=primary[:200],
                args_raw=args_raw,
                primary_arg=primary,
                file_path=str(args.get("file_path") or args.get("path") or ""),
                command=str(args.get("command") or ""),
                query=str(args.get("query") or args.get("pattern") or ""),
                url=str(args.get("url") or ""),
            )
        ]

    def _on_tool_execution_end(self, event: dict[str, Any]) -> list[Any]:
        tool_call_id = str(event.get("toolCallId") or "")
        tool_name = str(event.get("toolName") or "")
        is_error = bool(event.get("isError"))
        result = event.get("result") or {}
        text = _content_text(result.get("content"))
        started = self._tool_started_at.pop(tool_call_id, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started else 0
        preview, truncated = _preview(text)
        return [
            ToolResultMessage(
                type=MessageType.TOOL_RESULT,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status="error" if is_error else "success",
                duration_ms=duration_ms,
                content_preview=preview,
                content_length=len(text),
                content_bytes=len(text.encode("utf-8")),
                content_truncated=truncated,
            )
        ]

    def _on_compaction_start(self, event: dict[str, Any]) -> list[Any]:
        return [
            SystemNoticeMessage(
                type=MessageType.SYSTEM_NOTICE,
                title="pi 上下文压缩",
                content="pi 引擎正在压缩会话上下文，期间对话会短暂停顿。",
                level="info",
            )
        ]

    def _on_compaction_end(self, event: dict[str, Any]) -> list[Any]:
        saved = event.get("savedTokens") or event.get("tokensSaved") or ""
        detail = f"约节省 {saved} tokens。" if saved else ""
        return [
            SystemNoticeMessage(
                type=MessageType.SYSTEM_NOTICE,
                title="pi 上下文压缩完成",
                content=f"pi 引擎已完成上下文压缩。{detail}".strip(),
                level="success",
            )
        ]

    def _on_auto_retry(self, event: dict[str, Any]) -> list[Any]:
        kind = str(event.get("type") or "")
        attempt = event.get("attempt") or event.get("retryCount") or "?"
        detail = str(event.get("error") or event.get("message") or "")
        starting = kind == PiEventType.AUTO_RETRY_START
        text = f"pi 引擎{'开始重试' if starting else '重试结束'}（第 {attempt} 次）"
        if detail:
            text += f"：{detail[:200]}"
        return [
            SystemNoticeMessage(
                type=MessageType.SYSTEM_NOTICE,
                title="pi 自动重试",
                content=text,
                level="warning" if starting else "info",
            )
        ]

    def _on_extension_error(self, event: dict[str, Any]) -> list[Any]:
        detail = str(event.get("error") or event.get("message") or "未知扩展错误")
        source = str(event.get("extensionPath") or "")
        text = f"pi 扩展错误：{detail[:300]}"
        if source:
            text += f"（{source}）"
        return [ErrorMessage(type=MessageType.ERROR, message=text)]

    def _on_agent_settled(self, event: dict[str, Any]) -> list[Any]:
        self.settled = True
        self.final_text = self._turn_text if self._turn_text else (
            self._turns_text[-1] if self._turns_text else ""
        )
        return []


def _args_preview(args: dict[str, Any]) -> str:
    try:
        raw = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(args)
    return raw[:_TOOL_ARGS_PREVIEW_CHARS]


def _primary_argument(args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "query", "pattern", "url", "skill"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


_HANDLERS = {
    PiEventType.MESSAGE_START: PiSessionMapper._on_message_start,
    PiEventType.MESSAGE_UPDATE: PiSessionMapper._on_message_update,
    PiEventType.MESSAGE_END: PiSessionMapper._on_message_end,
    PiEventType.TOOL_EXECUTION_START: PiSessionMapper._on_tool_execution_start,
    PiEventType.TOOL_EXECUTION_END: PiSessionMapper._on_tool_execution_end,
    PiEventType.COMPACTION_START: PiSessionMapper._on_compaction_start,
    PiEventType.COMPACTION_END: PiSessionMapper._on_compaction_end,
    PiEventType.AUTO_RETRY_START: PiSessionMapper._on_auto_retry,
    PiEventType.AUTO_RETRY_END: PiSessionMapper._on_auto_retry,
    PiEventType.EXTENSION_ERROR: PiSessionMapper._on_extension_error,
    PiEventType.AGENT_SETTLED: PiSessionMapper._on_agent_settled,
}
