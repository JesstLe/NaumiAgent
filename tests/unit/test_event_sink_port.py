"""Focused contracts for the ARC-01.3e Runtime event boundary."""

from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.runtime.ports.events import (
    EventSink,
    JsonValue,
    LegacyEventCallback,
    RuntimeEvent,
    RuntimeEventType,
    freeze_json_value,
    thaw_event_data,
)

EXPECTED_RUNTIME_EVENT_TYPES = frozenset({
    "completion_receipt",
    "context_compacted",
    "error",
    "harness_completion_correction",
    "harness_completion_receipt",
    "harness_knowledge",
    "harness_knowledge_invalidated",
    "hook_trace",
    "latency_metric",
    "perf_phase",
    "permission_bubble",
    "recovery_event",
    "response_end",
    "response_start",
    "run_started",
    "runtime_notification",
    "subagent_event",
    "task_reconciliation_warning",
    "task_snapshot",
    "team_event",
    "thinking_delta",
    "thinking_end",
    "thinking_start",
    "token",
    "tool_end",
    "tool_error",
    "tool_prepare_end",
    "tool_prepare_snapshot",
    "tool_prepare_start",
    "tool_start",
    "turn_start",
})


def test_event_sink_exposes_only_emit() -> None:
    methods = {
        name
        for name, value in vars(EventSink).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert methods == {"emit"}


def test_runtime_event_type_manifest_matches_audited_production_events() -> None:
    assert frozenset(event.value for event in RuntimeEventType) == (
        EXPECTED_RUNTIME_EVENT_TYPES
    )


def test_contract_exports_are_runtime_importable() -> None:
    assert RuntimeEvent is not None
    assert JsonValue is not None
    assert LegacyEventCallback is not None
    assert callable(freeze_json_value)
    assert callable(thaw_event_data)


class _CompleteSink:
    async def emit(self, event: RuntimeEvent) -> None:
        del event


class _IncompleteSink:
    pass


def _event(**overrides: object) -> RuntimeEvent:
    values: dict[str, object] = {
        "id": "event-1",
        "type": RuntimeEventType.TOKEN,
        "data": {"content": "你好", "nested": {"values": [1, True, None]}},
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": "session-1",
        "run_id": "run-1",
        "turn": 2,
        "sequence": 3,
    }
    values.update(overrides)
    return RuntimeEvent(**values)  # type: ignore[arg-type]


def test_complete_sink_structurally_implements_contract() -> None:
    assert isinstance(_CompleteSink(), EventSink)
    assert not isinstance(_IncompleteSink(), EventSink)


def test_runtime_event_is_frozen_slotted_and_deeply_immutable() -> None:
    source = {"items": [{"name": "first"}], "flag": True}
    event = _event(data=source)

    source["items"][0]["name"] = "mutated"
    source["items"].append({"name": "second"})

    assert not hasattr(event, "__dict__")
    assert event.data["items"] == ({"name": "first"},)
    with pytest.raises(FrozenInstanceError):
        event.sequence = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.data["new"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        event.data["items"][0]["name"] = "forbidden"  # type: ignore[index]


def test_thaw_event_data_returns_independent_json_containers() -> None:
    event = _event(data={"nested": {"items": [1, 2]}})

    first = thaw_event_data(event.data)
    second = thaw_event_data(event.data)
    first["nested"]["items"].append(3)
    first["nested"]["extra"] = "changed"

    assert first == {"nested": {"items": [1, 2, 3], "extra": "changed"}}
    assert second == {"nested": {"items": [1, 2]}}
    assert event.data["nested"]["items"] == (1, 2)


@pytest.mark.parametrize("bad_id", ["", "   ", "x" * 129])
def test_runtime_event_rejects_invalid_event_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="事件 id"):
        _event(id=bad_id)


@pytest.mark.parametrize("field", ["session_id", "run_id"])
def test_runtime_event_rejects_oversized_context_ids(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _event(**{field: "x" * 501})


@pytest.mark.parametrize(
    ("timestamp", "error_type"),
    [
        ("not-a-time", ValueError),
        ("2026-07-15T09:00:00", ValueError),
        (123, TypeError),
    ],
)
def test_runtime_event_requires_timezone_aware_iso_timestamp(
    timestamp: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="timestamp"):
        _event(timestamp=timestamp)


@pytest.mark.parametrize("field", ["turn", "sequence"])
@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_runtime_event_rejects_non_integer_counters(
    field: str,
    value: object,
) -> None:
    with pytest.raises(TypeError, match=field):
        _event(**{field: value})


@pytest.mark.parametrize("field", ["turn", "sequence"])
def test_runtime_event_rejects_negative_counters(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _event(**{field: -1})


def test_runtime_event_rejects_non_enum_type() -> None:
    with pytest.raises(TypeError, match="RuntimeEvent.type"):
        _event(type="token")


@pytest.mark.parametrize(
    "payload",
    [
        {1: "number-key"},
        {"": "empty-key"},
        {" ": "blank-key"},
        {"bytes": b"secret"},
        {"path": Path("demo")},
        {"set": {1, 2}},
        {"object": object()},
        {"nan": math.nan},
        {"infinity": math.inf},
        ["not-a-mapping"],
    ],
)
def test_runtime_event_rejects_non_json_payload(payload: object) -> None:
    with pytest.raises((TypeError, ValueError), match="事件数据"):
        _event(data=payload)


def test_freeze_json_value_preserves_supported_scalar_types() -> None:
    assert freeze_json_value(None) is None
    assert freeze_json_value("text") == "text"
    assert freeze_json_value(True) is True
    assert freeze_json_value(42) == 42
    assert freeze_json_value(1.25) == 1.25
