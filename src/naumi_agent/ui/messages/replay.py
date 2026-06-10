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

from naumi_agent.ui.messages.adapter import _detect_preview_format
from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserMessage,
)

_REPLAY_TOOL_PREVIEW_MAX_CHARS = 2_000
_REPLAY_TOOL_PREVIEW_MAX_LINES = 80


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
    tool_names_by_id: dict[str, str] = {}
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
                tool_call_id = str(tc_dict.get("id") or "")
                if tool_call_id:
                    tool_names_by_id[tool_call_id] = name
                args_raw = func.get("arguments", "")
                args_parsed = _parse_args_dict(args_raw)
                primary_arg = _extract_primary_arg(args_parsed)
                result.append(ToolUseMessage(
                    type=MessageType.TOOL_USE,
                    tool_name=name,
                    tool_call_id=tool_call_id,
                    args_summary=_summarize_args(args_raw),
                    primary_arg=primary_arg,
                    file_path=args_parsed.get("file_path", args_parsed.get("path", "")),
                    command=args_parsed.get("command", ""),
                    query=args_parsed.get("query", ""),
                    url=args_parsed.get("url", ""),
                ))

        elif role == "tool":
            content = raw.get("content") or ""
            tool_call_id = str(raw.get("tool_call_id") or "")
            # Derive a status from the content
            status = _infer_tool_result_status(content)
            content_preview = _tool_content_preview(content)
            preview_format, preview_language = _detect_preview_format(
                tool_names_by_id.get(tool_call_id, ""),
                content_preview,
            )
            result.append(ToolResultMessage(
                type=MessageType.TOOL_RESULT,
                tool_name=tool_names_by_id.get(tool_call_id, ""),
                tool_call_id=tool_call_id,
                status=status,
                content_preview=content_preview,
                content_length=len(content) if content else 0,
                preview_format=preview_format,
                preview_language=preview_language,
                content_truncated=len(content_preview) < len(content),
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


def _tool_content_preview(content: str) -> str:
    """Build a replay-safe preview without cutting Markdown fences mid-block."""
    if not content:
        return ""

    lines = content.splitlines(keepends=True)
    if (
        len(content) <= _REPLAY_TOOL_PREVIEW_MAX_CHARS
        and len(lines) <= _REPLAY_TOOL_PREVIEW_MAX_LINES
    ):
        return content

    preview_lines: list[str] = []
    chars = 0
    for line in lines:
        if (
            len(preview_lines) >= _REPLAY_TOOL_PREVIEW_MAX_LINES
            or chars + len(line) > _REPLAY_TOOL_PREVIEW_MAX_CHARS
        ):
            break
        preview_lines.append(line)
        chars += len(line)

    if not preview_lines:
        preview = content[:_REPLAY_TOOL_PREVIEW_MAX_CHARS]
    else:
        preview = "".join(preview_lines)

    hidden_lines = max(len(lines) - len(preview_lines), 0)
    hidden_chars = max(len(content) - len(preview), 0)

    if _has_unclosed_fence(preview):
        if preview and not preview.endswith("\n"):
            preview += "\n"
        preview += "```\n"

    if preview and not preview.endswith("\n"):
        preview += "\n"
    preview += (
        f"\n已隐藏 {hidden_lines} 行 / {hidden_chars} 字符；"
        "如需完整内容请使用 /debug 查看结构化日志。"
    )
    return preview


def _has_unclosed_fence(text: str) -> bool:
    """Return true when a Markdown fenced block was opened but not closed."""
    count = 0
    for line in text.splitlines():
        if line.strip().startswith("```"):
            count += 1
    return count % 2 == 1


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
