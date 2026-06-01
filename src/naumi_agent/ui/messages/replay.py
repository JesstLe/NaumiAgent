"""Convert stored session messages into typed UIMessages for replay.

When a session is loaded/resumed, its raw message dicts (role/content/tool_calls)
must be converted back into the typed UIMessage model so that the adapter/renderer
pipeline can display them consistently with live output.

This module is the "inverse" of the EngineEventAdapter: instead of converting
live engine events, it converts persisted session history back into renderable
UIMessages.
"""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserMessage,
)


def replay_messages(raw_messages: list[dict[str, Any]]) -> list[UIMessage]:
    """Convert a session's raw message list into typed UIMessages.

    Args:
        raw_messages: List of dicts with ``role`` and ``content`` keys,
            as stored by the session persistence layer.

    Returns:
        Ordered list of UIMessages suitable for rendering through the
        CLIRenderer / TUI renderer pipeline.
    """
    result: list[UIMessage] = []
    for raw in raw_messages:
        role = raw.get("role", "")
        if role == "system":
            continue  # never replay system prompts

        if role == "user":
            content = raw.get("content") or ""
            result.append(UserMessage(
                type=MessageType.USER,
                content=content,
                is_command=content.startswith("/"),
            ))

        elif role == "assistant":
            content = raw.get("content") or ""
            tool_calls = raw.get("tool_calls", [])

            # Emit assistant text as a single stream-like message
            if content:
                result.append(AssistantStreamMessage(
                    type=MessageType.ASSISTANT_STREAM,
                    phase="token",
                    content=content,
                ))

            # Emit each tool call as a ToolUseMessage
            for tc in tool_calls:
                tc_dict = tc if isinstance(tc, dict) else {}
                func = tc_dict.get("function", {})
                name = func.get("name", "tool")
                args_raw = func.get("arguments", "")
                args_parsed = _parse_args_dict(args_raw)
                primary_arg = _extract_primary_arg(args_parsed)
                result.append(ToolUseMessage(
                    type=MessageType.TOOL_USE,
                    tool_name=name,
                    args_summary=_summarize_args(args_raw),
                    primary_arg=primary_arg,
                    file_path=args_parsed.get("file_path", args_parsed.get("path", "")),
                    command=args_parsed.get("command", ""),
                    query=args_parsed.get("query", ""),
                    url=args_parsed.get("url", ""),
                ))

        elif role == "tool":
            content = raw.get("content") or ""
            # Derive a status from the content
            status = _infer_tool_result_status(content)
            result.append(ToolResultMessage(
                type=MessageType.TOOL_RESULT,
                tool_name="",  # not available in tool-role messages
                status=status,
                content_preview=content[:500] if content else "",
                content_length=len(content) if content else 0,
            ))

    return result


def _infer_tool_result_status(content: str) -> str:
    """Best-effort inference of tool result status from content text."""
    if not content:
        return "success"
    lower = content.lower()[:300]
    if "工具调用结果缺失" in content:
        return "skipped"
    if "error" in lower or "失败" in content[:200]:
        return "error"
    return "success"


def _parse_args_dict(args_raw: Any) -> dict[str, str]:
    """Parse tool arguments into a flat string dict."""
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


def _summarize_args(args_raw: Any) -> str:
    """Build a compact argument summary string."""
    if not args_raw:
        return ""
    text = args_raw if isinstance(args_raw, str) else json.dumps(args_raw, ensure_ascii=False)
    if len(text) <= 200:
        return text
    return text[:199] + "…"
