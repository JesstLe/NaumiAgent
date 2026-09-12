"""Translate pi stream events into NaumiAgent RuntimeEvents.

The web API streams through ``StreamEventSink``, which only accepts the
internal RuntimeEvent vocabulary (it then maps them to the public transport
envelope).  This translator keeps pi events speaking that same vocabulary so
web frontends render pi sessions with zero protocol changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from naumi_agent.pi_engine.rpc import PiEventType
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType

_RESULT_PREVIEW_CHARS = 400


def _runtime_event(
    event_type: RuntimeEventType,
    data: dict[str, Any],
    *,
    session_id: str = "",
    turn: int = 0,
) -> RuntimeEvent:
    return RuntimeEvent(
        id=uuid4().hex[:12],
        type=event_type,
        data=data,
        timestamp=datetime.now(UTC).isoformat(),
        session_id=session_id,
        turn=turn,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class PiWebEventTranslator:
    """Stateful pi event -> RuntimeEvent translation for one web run."""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.turn = 0
        self.text_parts: list[str] = []
        self.error: str = ""
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_tokens = 0

    def reset(self) -> None:
        self.turn = 0
        self.text_parts = []
        self.error = ""
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_tokens = 0

    def feed(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        kind = str(event.get("type") or "")
        handler = _HANDLERS.get(kind)
        if handler is None:
            return []
        return handler(self, event)

    # -- handlers -----------------------------------------------------------

    def _on_agent_start(self, _: dict[str, Any]) -> list[RuntimeEvent]:
        return [
            _runtime_event(
                RuntimeEventType.RESPONSE_START,
                {"engine": "pi"},
                session_id=self.session_id,
                turn=self.turn,
            )
        ]

    def _on_turn_start(self, _: dict[str, Any]) -> list[RuntimeEvent]:
        self.turn += 1
        return [
            _runtime_event(
                RuntimeEventType.TURN_START,
                {"turn": self.turn, "engine": "pi"},
                session_id=self.session_id,
                turn=self.turn,
            )
        ]

    def _on_message_update(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        update = event.get("assistantMessageEvent") or {}
        phase = str(update.get("type") or "")
        if phase == "text_delta":
            delta = str(update.get("delta") or "")
            if not delta:
                return []
            self.text_parts.append(delta)
            return [
                _runtime_event(
                    RuntimeEventType.TOKEN,
                    {"content": delta},
                    session_id=self.session_id,
                    turn=self.turn,
                )
            ]
        if phase == "thinking_start":
            return [
                _runtime_event(
                    RuntimeEventType.THINKING_START,
                    {},
                    session_id=self.session_id,
                    turn=self.turn,
                )
            ]
        if phase == "thinking_delta":
            delta = str(update.get("delta") or "")
            if not delta:
                return []
            return [
                _runtime_event(
                    RuntimeEventType.THINKING_DELTA,
                    {"content": delta},
                    session_id=self.session_id,
                    turn=self.turn,
                )
            ]
        if phase == "text_start":
            return [
                _runtime_event(
                    RuntimeEventType.THINKING_END,
                    {},
                    session_id=self.session_id,
                    turn=self.turn,
                )
            ]
        return []

    def _on_message_end(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return []
        usage = message.get("usage") or {}
        if isinstance(usage, dict):
            cost = usage.get("cost")
            if isinstance(cost, dict):
                total = cost.get("total")
                if isinstance(total, (int, float)):
                    self.total_cost_usd = float(total)
            for source, target in (
                ("totalTokens", "total_tokens"),
                ("input", "input_tokens"),
                ("output", "output_tokens"),
                ("cacheRead", "cache_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, int):
                    setattr(self, target, value)
        if str(message.get("stopReason") or "") == "error":
            self.error = (
                str(message.get("error") or "")
                or _content_text(message.get("content"))
                or "模型调用失败。"
            )
            return [
                _runtime_event(
                    RuntimeEventType.ERROR,
                    {"message": f"pi 引擎模型调用失败：{self.error}"},
                    session_id=self.session_id,
                    turn=self.turn,
                )
            ]
        return []

    def _on_tool_execution_start(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        name = str(event.get("toolName") or "")
        call_id = str(event.get("toolCallId") or "")
        args = event.get("args") or {}
        return [
            _runtime_event(
                RuntimeEventType.TOOL_START,
                {
                    "call_id": call_id,
                    "name": name,
                    "tool_name": name,
                    "arguments": _json_safe(args),
                    "activity_summary": _tool_activity(name, args),
                },
                session_id=self.session_id,
                turn=self.turn,
            )
        ]

    def _on_tool_execution_end(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        name = str(event.get("toolName") or "")
        call_id = str(event.get("toolCallId") or "")
        is_error = bool(event.get("isError"))
        preview = _content_text((event.get("result") or {}).get("content"))
        data = {
            "call_id": call_id,
            "name": name,
            "tool_name": name,
            "status": "error" if is_error else "success",
            "result": preview[:_RESULT_PREVIEW_CHARS],
        }
        event_type = RuntimeEventType.TOOL_ERROR if is_error else RuntimeEventType.TOOL_END
        return [
            _runtime_event(
                event_type,
                data,
                session_id=self.session_id,
                turn=self.turn,
            )
        ]

    def _on_compaction_end(self, _: dict[str, Any]) -> list[RuntimeEvent]:
        return [
            _runtime_event(
                RuntimeEventType.CONTEXT_COMPACTED,
                {"engine": "pi"},
                session_id=self.session_id,
                turn=self.turn,
            )
        ]

    def final_text(self) -> str:
        return "".join(self.text_parts)


def _json_safe(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:2000]
    except (TypeError, ValueError):
        return str(value)[:2000]


def _tool_activity(name: str, args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "query", "pattern", "url"):
        value = args.get(key)
        if value:
            return f"pi {name}: {str(value)[:120]}"
    return f"pi {name}"


_HANDLERS = {
    PiEventType.AGENT_START: PiWebEventTranslator._on_agent_start,
    PiEventType.TURN_START: PiWebEventTranslator._on_turn_start,
    PiEventType.MESSAGE_UPDATE: PiWebEventTranslator._on_message_update,
    PiEventType.MESSAGE_END: PiWebEventTranslator._on_message_end,
    PiEventType.TOOL_EXECUTION_START: PiWebEventTranslator._on_tool_execution_start,
    PiEventType.TOOL_EXECUTION_END: PiWebEventTranslator._on_tool_execution_end,
    PiEventType.COMPACTION_END: PiWebEventTranslator._on_compaction_end,
}
