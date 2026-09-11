"""Shared public tool evidence for engine and HTTP run recorders."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.runs.public_activity import public_excerpt, tool_action
from naumi_agent.safety.guardrails import OutputGuardrail

OUTPUT_PREVIEW_LIMIT = 64_000


def public_tool_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    safe = OutputGuardrail.redact(text)
    return safe[:OUTPUT_PREVIEW_LIMIT]


def tool_output(data: dict[str, Any]) -> str:
    for key in ("content", "result", "message", "error"):
        if data.get(key) is not None:
            return public_tool_text(data[key])
    return ""


def tool_metadata(data: dict[str, Any], *, ended: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if not ended and any(
        data.get(key) is not None for key in ("activity_summary", "arguments", "args")
    ):
        metadata["public_action"] = (
            public_excerpt(data.get("activity_summary"), 400) or tool_action(data)
        )
    call_id = data.get("call_id") or data.get("tool_call_id") or data.get("request_id")
    if call_id:
        metadata["tool_call_id"] = str(call_id)
    for key in ("arguments", "args"):
        if data.get(key) is not None:
            metadata["input"] = public_tool_text(data[key])
            break
    if ended:
        metadata["output_recorded"] = True
        preview = tool_output(data)
        length = data.get("content_length")
        metadata["output_truncated"] = isinstance(length, int) and length > len(preview)
        for key in (
            "output_artifact_id",
            "output_page_count",
            "output_page_chars",
            "output_sha256",
            "content_length",
        ):
            if key in data:
                metadata[key] = data[key]
    return metadata


def tool_end_status(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "unknown").lower()
    if status in {"success", "succeeded", "completed", "passed"}:
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status in {"failed", "error", "denied", "aborted"}:
        return "failed"
    return "unknown"
