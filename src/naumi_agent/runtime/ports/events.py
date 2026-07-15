"""Typed outbound event boundary owned by the Agent runtime."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4

type JsonScalar = str | int | float | bool | None
type JsonValue = (
    JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
type LegacyEventCallback = Callable[
    [str, dict[str, object]],
    Awaitable[None],
]

_MAX_EVENT_ID_LENGTH = 128
_MAX_CONTEXT_ID_LENGTH = 500


class RuntimeEventType(StrEnum):
    """Closed vocabulary of facts produced by the Agent runtime."""

    COMPLETION_RECEIPT = "completion_receipt"
    CONTEXT_COMPACTED = "context_compacted"
    ERROR = "error"
    HARNESS_COMPLETION_CORRECTION = "harness_completion_correction"
    HARNESS_COMPLETION_RECEIPT = "harness_completion_receipt"
    HARNESS_KNOWLEDGE = "harness_knowledge"
    HARNESS_KNOWLEDGE_INVALIDATED = "harness_knowledge_invalidated"
    HOOK_TRACE = "hook_trace"
    LATENCY_METRIC = "latency_metric"
    PERF_PHASE = "perf_phase"
    PERMISSION_BUBBLE = "permission_bubble"
    RECOVERY_EVENT = "recovery_event"
    RESPONSE_END = "response_end"
    RESPONSE_START = "response_start"
    RUN_STARTED = "run_started"
    RUNTIME_NOTIFICATION = "runtime_notification"
    SUBAGENT_EVENT = "subagent_event"
    TASK_RECONCILIATION_WARNING = "task_reconciliation_warning"
    TASK_SNAPSHOT = "task_snapshot"
    TEAM_EVENT = "team_event"
    THINKING_DELTA = "thinking_delta"
    THINKING_END = "thinking_end"
    THINKING_START = "thinking_start"
    TOKEN = "token"
    TOOL_END = "tool_end"
    TOOL_ERROR = "tool_error"
    TOOL_PREPARE_END = "tool_prepare_end"
    TOOL_PREPARE_SNAPSHOT = "tool_prepare_snapshot"
    TOOL_PREPARE_START = "tool_prepare_start"
    TOOL_START = "tool_start"
    TURN_START = "turn_start"


def freeze_json_value(value: object, *, path: str = "$data") -> JsonValue:
    """Copy and recursively freeze one strict JSON value."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"事件数据 {path} 的浮点数必须是有限值")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"事件数据 {path} 的键必须是字符串")
            if not key.strip():
                raise ValueError(f"事件数据 {path} 的键不能为空")
            frozen[key] = freeze_json_value(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(
        f"事件数据 {path} 不是可序列化 JSON 值：{type(value).__name__}"
    )


def thaw_event_data(data: Mapping[str, JsonValue]) -> dict[str, object]:
    """Return a mutable JSON-compatible copy for a transport adapter."""

    def thaw(value: JsonValue) -> object:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        return value

    return {key: thaw(value) for key, value in data.items()}


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Immutable, serializable fact delivered to every runtime event Sink."""

    id: str
    type: RuntimeEventType
    data: Mapping[str, JsonValue]
    timestamp: str
    session_id: str = ""
    run_id: str = ""
    turn: int = 0
    sequence: int = 0

    def __post_init__(self) -> None:
        _require_identifier(self.id, field_name="事件 id", maximum=_MAX_EVENT_ID_LENGTH)
        if not isinstance(self.type, RuntimeEventType):
            raise TypeError("RuntimeEvent.type 必须是 RuntimeEventType")
        _require_timestamp(self.timestamp)
        _require_context_id(self.session_id, field_name="session_id")
        _require_context_id(self.run_id, field_name="run_id")
        _require_counter(self.turn, field_name="turn")
        _require_counter(self.sequence, field_name="sequence")
        if not isinstance(self.data, Mapping):
            raise TypeError("事件数据 $data 必须是 JSON 对象")
        frozen = freeze_json_value(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("事件数据 $data 必须是 JSON 对象")
        object.__setattr__(self, "data", frozen)

    @classmethod
    def create(
        cls,
        *,
        event_type: RuntimeEventType,
        data: Mapping[str, object],
        session_id: str = "",
        run_id: str = "",
        turn: int = 0,
        sequence: int = 0,
    ) -> RuntimeEvent:
        """Create one event with a unique id and timezone-aware timestamp."""
        return cls(
            id=uuid4().hex,
            type=event_type,
            data=data,
            timestamp=datetime.now(UTC).astimezone().isoformat(),
            session_id=session_id,
            run_id=run_id,
            turn=turn,
            sequence=sequence,
        )


@runtime_checkable
class EventSink(Protocol):
    """Consume one immutable Runtime event with awaited backpressure."""

    async def emit(self, event: RuntimeEvent) -> None: ...


def _require_identifier(value: object, *, field_name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    if len(value) > maximum:
        raise ValueError(f"{field_name} 长度不能超过 {maximum} 个字符")


def _require_context_id(value: object, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    if len(value) > _MAX_CONTEXT_ID_LENGTH:
        raise ValueError(
            f"{field_name} 长度不能超过 {_MAX_CONTEXT_ID_LENGTH} 个字符"
        )


def _require_timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("RuntimeEvent.timestamp 必须是 ISO-8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("RuntimeEvent.timestamp 必须是有效的 ISO-8601 时间") from exc
    if parsed.utcoffset() is None:
        raise ValueError("RuntimeEvent.timestamp 必须包含 UTC offset")


def _require_counter(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"RuntimeEvent.{field_name} 必须是非负整数")
    if value < 0:
        raise ValueError(f"RuntimeEvent.{field_name} 必须是非负整数")


__all__ = [
    "EventSink",
    "JsonScalar",
    "JsonValue",
    "LegacyEventCallback",
    "RuntimeEvent",
    "RuntimeEventType",
    "freeze_json_value",
    "thaw_event_data",
]
