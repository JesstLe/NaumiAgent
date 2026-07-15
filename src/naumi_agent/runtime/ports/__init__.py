"""Runtime-owned dependency ports."""

from naumi_agent.runtime.ports.events import (
    EventSink,
    JsonScalar,
    JsonValue,
    LegacyEventCallback,
    RuntimeEvent,
    RuntimeEventType,
    freeze_json_value,
    thaw_event_data,
)
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort, SessionT
from naumi_agent.runtime.ports.tool_execution import (
    ExecutableTool,
    ToolEventCallback,
    ToolExecutionOutcome,
    ToolExecutionPort,
)

__all__ = [
    "EventSink",
    "ExecutableTool",
    "JsonScalar",
    "JsonValue",
    "LegacyEventCallback",
    "ModelPort",
    "PermissionPort",
    "RuntimeEvent",
    "RuntimeEventType",
    "SessionPort",
    "SessionT",
    "ToolEventCallback",
    "ToolExecutionOutcome",
    "ToolExecutionPort",
    "freeze_json_value",
    "thaw_event_data",
]
