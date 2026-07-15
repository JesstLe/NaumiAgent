from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from naumi_agent.config.settings import MemoryConfig, ModelConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.dependencies import (
    RuntimePortOverrides,
    RuntimePorts,
    validate_runtime_port_overrides,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tools.execution import LocalToolExecutor


class _FalseySink(NullEventSink):
    def __bool__(self) -> bool:
        return False


def _complete_ports(tmp_path):
    return {
        "session_port": SessionStore(MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        )),
        "permission_port": PermissionChecker(PermissionMode.MODERATE),
        "model_port": ModelRouter(ModelConfig()),
        "tool_execution_port": LocalToolExecutor(),
        "event_sink": _FalseySink(),
    }


def test_runtime_ports_are_frozen_and_preserve_complete_falsey_identity(
    tmp_path,
):
    values = _complete_ports(tmp_path)
    ports = RuntimePorts[Session](**values)

    assert ports.event_sink is values["event_sink"]
    with pytest.raises(FrozenInstanceError):
        ports.event_sink = NullEventSink()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("session_port", "session_port 必须实现完整的 SessionPort 契约"),
        ("permission_port", "permission_port 必须实现完整的 PermissionPort 契约"),
        ("model_port", "model_port 必须实现完整的 ModelPort 契约"),
        (
            "tool_execution_port",
            "tool_execution_port 必须实现完整的 ToolExecutionPort 契约",
        ),
        ("event_sink", "event_sink 必须实现完整的 EventSink 契约"),
    ],
)
def test_runtime_ports_reject_each_incomplete_field_before_use(
    tmp_path,
    field,
    message,
):
    values = _complete_ports(tmp_path)
    values[field] = object()

    with pytest.raises(TypeError, match=message):
        RuntimePorts[Session](**values)


def test_overrides_allow_none_and_reject_non_none_partial_port():
    empty = RuntimePortOverrides[Session]()
    validate_runtime_port_overrides(empty)

    invalid = RuntimePortOverrides[Session](
        event_sink=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(
        TypeError,
        match="event_sink 必须实现完整的 EventSink 契约",
    ):
        validate_runtime_port_overrides(invalid)
