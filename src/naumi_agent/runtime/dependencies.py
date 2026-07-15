"""Explicit dependency bundles consumed by the Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, fields

from naumi_agent.runtime.ports.events import EventSink
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort
from naumi_agent.runtime.ports.tool_execution import ToolExecutionPort

_PORT_CONTRACTS: dict[str, tuple[type[object], str]] = {
    "session_port": (
        SessionPort,
        "session_port 必须实现完整的 SessionPort 契约："
        "create_session/save/load/list_sessions/delete/archive/close",
    ),
    "permission_port": (
        PermissionPort,
        "permission_port 必须实现完整的 PermissionPort 契约："
        "mode/set_mode/check/reset_counts",
    ),
    "model_port": (
        ModelPort,
        "model_port 必须实现完整的 ModelPort 契约："
        "metadata/routing/capability/discovery/reasoning/call/stream",
    ),
    "tool_execution_port": (
        ToolExecutionPort,
        "tool_execution_port 必须实现完整的 ToolExecutionPort 契约：invoke",
    ),
    "event_sink": (
        EventSink,
        "event_sink 必须实现完整的 EventSink 契约：emit",
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimePorts[SessionT]:
    """Complete Port bundle required by one runtime instance."""

    session_port: SessionPort[SessionT]
    permission_port: PermissionPort
    model_port: ModelPort
    tool_execution_port: ToolExecutionPort
    event_sink: EventSink

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_port(item.name, getattr(self, item.name))


@dataclass(frozen=True, slots=True)
class RuntimePortOverrides[SessionT]:
    """Optional caller-selected Ports; None alone requests a default adapter."""

    session_port: SessionPort[SessionT] | None = None
    permission_port: PermissionPort | None = None
    model_port: ModelPort | None = None
    tool_execution_port: ToolExecutionPort | None = None
    event_sink: EventSink | None = None


def validate_runtime_port_overrides[SessionT](
    overrides: RuntimePortOverrides[SessionT],
) -> None:
    """Reject incomplete explicit overrides before default construction starts."""
    for item in fields(overrides):
        value = getattr(overrides, item.name)
        if value is not None:
            _require_port(item.name, value)


def _require_port(name: str, value: object) -> None:
    protocol, message = _PORT_CONTRACTS[name]
    if not isinstance(value, protocol):
        raise TypeError(message)


__all__ = [
    "RuntimePortOverrides",
    "RuntimePorts",
    "validate_runtime_port_overrides",
]
