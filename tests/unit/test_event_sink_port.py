"""Focused contracts for the ARC-01.3e Runtime event boundary."""

from __future__ import annotations

import inspect

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
