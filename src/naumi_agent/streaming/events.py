"""事件类型与数据模型."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from naumi_agent.runtime.ports.events import (
    RuntimeEvent,
    RuntimeEventType,
    thaw_event_data,
)


class EventType(StrEnum):
    # LLM 响应
    TOKEN_DELTA = "token_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_START = "thinking_start"
    THINKING_END = "thinking_end"

    # 工具调用
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    PERMISSION_REQUEST = "permission_request"

    # 规划
    PLAN_CREATED = "plan_created"
    PLAN_STEP_START = "plan_step_start"
    PLAN_STEP_UPDATE = "plan_step_update"
    PLAN_STEP_END = "plan_step_end"

    # 记忆
    MEMORY_STORED = "memory_stored"
    MEMORY_RECALLED = "memory_recalled"
    CONTEXT_COMPACTED = "context_compacted"

    # 生命周期
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_ERROR = "agent_error"
    COMPLETION_RECEIPT = "completion_receipt"
    TURN_START = "turn_start"
    TURN_END = "turn_end"

    # 资源
    BUDGET_UPDATE = "budget_update"
    TOKEN_COUNT = "token_count"

    # Workbench / Mac app
    WORKBENCH_EVENT = "workbench_event"
    WORKBENCH_SNAPSHOT = "workbench_snapshot"

    # Runtime facts without a dedicated public transport event.
    RUNTIME_EVENT = "runtime_event"


@dataclass(frozen=True)
class StreamEvent:
    """统一事件模型."""

    type: EventType
    data: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    session_id: str = ""
    turn: int = 0
    source_event: str = ""
    event_id: str = ""
    run_id: str = ""
    sequence: int = 0

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn": self.turn,
        }
        if self.source_event:
            payload["source_event"] = self.source_event
        if self.event_id:
            payload["event_id"] = self.event_id
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.sequence:
            payload["sequence"] = self.sequence
        return payload

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"

    def to_ws(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


type StreamEventConsumer = Callable[[StreamEvent], Awaitable[None]]


_RUNTIME_EVENT_TYPE_MAP: dict[RuntimeEventType, EventType | None] = {
    RuntimeEventType.COMPLETION_RECEIPT: EventType.COMPLETION_RECEIPT,
    RuntimeEventType.CONTEXT_COMPACTED: EventType.CONTEXT_COMPACTED,
    RuntimeEventType.ERROR: EventType.AGENT_ERROR,
    RuntimeEventType.HARNESS_COMPLETION_CORRECTION: None,
    RuntimeEventType.HARNESS_COMPLETION_RECEIPT: None,
    RuntimeEventType.HARNESS_KNOWLEDGE: None,
    RuntimeEventType.HARNESS_KNOWLEDGE_INVALIDATED: None,
    RuntimeEventType.HOOK_TRACE: None,
    RuntimeEventType.LATENCY_METRIC: None,
    RuntimeEventType.PERF_PHASE: None,
    RuntimeEventType.PERMISSION_BUBBLE: EventType.PERMISSION_REQUEST,
    RuntimeEventType.RECOVERY_EVENT: None,
    RuntimeEventType.RESPONSE_END: EventType.AGENT_END,
    RuntimeEventType.RESPONSE_START: EventType.AGENT_START,
    RuntimeEventType.RUN_STARTED: None,
    RuntimeEventType.RUNTIME_NOTIFICATION: None,
    RuntimeEventType.SUBAGENT_EVENT: None,
    RuntimeEventType.TASK_RECONCILIATION_WARNING: None,
    RuntimeEventType.TASK_SNAPSHOT: None,
    RuntimeEventType.TEAM_EVENT: None,
    RuntimeEventType.THINKING_DELTA: EventType.THINKING_DELTA,
    RuntimeEventType.THINKING_END: EventType.THINKING_END,
    RuntimeEventType.THINKING_START: EventType.THINKING_START,
    RuntimeEventType.TOKEN: EventType.TOKEN_DELTA,
    RuntimeEventType.TOOL_END: EventType.TOOL_CALL_END,
    RuntimeEventType.TOOL_ERROR: EventType.TOOL_CALL_ERROR,
    RuntimeEventType.TOOL_PREPARE_END: None,
    RuntimeEventType.TOOL_PREPARE_SNAPSHOT: None,
    RuntimeEventType.TOOL_PREPARE_START: None,
    RuntimeEventType.TOOL_START: EventType.TOOL_CALL_START,
    RuntimeEventType.TURN_START: EventType.TURN_START,
}

if frozenset(_RUNTIME_EVENT_TYPE_MAP) != frozenset(RuntimeEventType):
    raise RuntimeError("RuntimeEvent transport 映射必须穷尽全部事件类型")


class StreamEventSink:
    """Convert one authoritative RuntimeEvent into an awaited transport event."""

    def __init__(self, consumer: StreamEventConsumer) -> None:
        if not callable(consumer):
            raise TypeError("StreamEventSink 需要可调用的异步发送器")
        self._consumer = consumer

    async def emit(self, event: RuntimeEvent) -> None:
        await self._consumer(runtime_event_to_stream_event(event))


def runtime_event_to_stream_event(event: RuntimeEvent) -> StreamEvent:
    """Map a closed Runtime event vocabulary to the public transport envelope."""
    if not isinstance(event, RuntimeEvent):
        raise TypeError("StreamEventSink 只能消费 RuntimeEvent")

    event_type = _RUNTIME_EVENT_TYPE_MAP[event.type]
    payload = thaw_event_data(event.data)

    if event.type in {RuntimeEventType.THINKING_DELTA, RuntimeEventType.THINKING_END}:
        payload = {}
    elif event.type is RuntimeEventType.PERMISSION_BUBBLE:
        safe_fields = (
            "agent_name",
            "tool_name",
            "call_id",
            "status",
            "reason",
            "risk_level",
            "requires_confirmation",
        )
        payload = {field: payload[field] for field in safe_fields if field in payload}
    elif event.type is RuntimeEventType.TOOL_START:
        call_id = payload.get("call_id") or payload.get("tool_call_id")
        payload = {"name": str(payload.get("name") or "tool")}
        if call_id:
            payload["call_id"] = str(call_id)
    elif event.type is RuntimeEventType.TOKEN and "content" in payload:
        payload["token"] = payload.pop("content")

    if event.type is RuntimeEventType.TOOL_END and payload.get("status") not in (
        None,
        "success",
    ):
        event_type = EventType.TOOL_CALL_ERROR

    transport_data = (
        payload
        if event_type is not None
        else {"event": event.type.value, "data": payload}
    )
    return StreamEvent(
        id=event.id,
        type=event_type or EventType.RUNTIME_EVENT,
        data=transport_data,
        timestamp=event.timestamp,
        session_id=event.session_id,
        turn=event.turn,
        source_event=event.type.value,
        event_id=event.id,
        run_id=event.run_id,
        sequence=event.sequence,
    )


__all__ = [
    "EventType",
    "StreamEvent",
    "StreamEventConsumer",
    "StreamEventSink",
    "runtime_event_to_stream_event",
]
