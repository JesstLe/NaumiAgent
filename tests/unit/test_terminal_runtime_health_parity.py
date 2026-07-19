"""UI-17.2a golden parity for terminal runtime-health semantics."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import RuntimeHeartbeatRetentionConfig
from naumi_agent.harness.heartbeat_retention_periodic import (
    RuntimeHeartbeatRetentionSnapshot,
    RuntimeHeartbeatRetentionState,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory
from naumi_agent.tui.app import NaumiApp
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.doctor_health import runtime_heartbeat_retention_health_item
from naumi_agent.ui.runtime_health import (
    runtime_heartbeat_retention_status_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "ui17"
    / "runtime-heartbeat-retention-golden.json"
)
_SCENARIO_FIELDS = {
    "id",
    "configured_enabled",
    "available",
    "snapshot",
    "expected",
}
_STATUS_FIELDS = {
    "configured_enabled",
    "state",
    "cycle_count",
    "deleted_count",
    "failure_count",
    "last_error_code",
    "last_cycle_at",
    "next_delay_seconds",
}


def _golden_scenarios() -> list[dict[str, object]]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "scenarios"}
    assert document["schema_version"] == 1
    scenarios = document["scenarios"]
    assert isinstance(scenarios, list) and scenarios
    ids: list[str] = []
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        assert set(scenario) == _SCENARIO_FIELDS
        assert isinstance(scenario["id"], str)
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", scenario["id"])
        assert isinstance(scenario["configured_enabled"], bool)
        assert isinstance(scenario["available"], bool)
        assert scenario["snapshot"] is None or isinstance(
            scenario["snapshot"], dict
        )
        assert isinstance(scenario["expected"], dict)
        assert set(scenario["expected"]) == _STATUS_FIELDS
        ids.append(scenario["id"])
    assert len(ids) == len(set(ids))
    return scenarios


def _retention_snapshot(
    raw: object,
) -> RuntimeHeartbeatRetentionSnapshot | None:
    if raw is None:
        return None
    assert isinstance(raw, dict)
    return RuntimeHeartbeatRetentionSnapshot(
        state=RuntimeHeartbeatRetentionState(str(raw["state"])),
        cycle_count=int(raw["cycle_count"]),
        deleted_count=int(raw["deleted_count"]),
        failure_count=int(raw["failure_count"]),
        last_error_code=str(raw["last_error_code"]),
        last_cycle_at=str(raw["last_cycle_at"]),
        next_delay_seconds=float(raw["next_delay_seconds"]),
    )


@pytest.mark.parametrize("scenario", _golden_scenarios(), ids=lambda item: item["id"])
def test_runtime_health_projection_matches_golden(scenario) -> None:
    payload = runtime_heartbeat_retention_status_payload(
        configured_enabled=scenario["configured_enabled"],
        available=scenario["available"],
        snapshot=_retention_snapshot(scenario["snapshot"]),
    )

    assert payload == scenario["expected"]


@pytest.mark.parametrize("scenario", _golden_scenarios(), ids=lambda item: item["id"])
def test_new_ui_and_tui_adapters_match_same_runtime_health_golden(
    tmp_path: Path,
    scenario,
) -> None:
    configured = scenario["configured_enabled"]
    factory = (
        TerminalRuntimeLifecycleFactory(
            store=HarnessStore(tmp_path / "harness.db"),
            workspace_root=tmp_path,
            retention_config=RuntimeHeartbeatRetentionConfig(
                enabled=configured
            ),
        )
        if scenario["available"]
        else None
    )
    retention = _retention_snapshot(scenario["snapshot"])
    lifecycle = (
        SimpleNamespace(
            snapshot=lambda: SimpleNamespace(retention=retention),
        )
        if factory is not None and scenario["id"] != "policy_disabled"
        else None
    )
    engine = SimpleNamespace(
        _config=SimpleNamespace(
            harness=SimpleNamespace(
                runtime_heartbeat_retention=SimpleNamespace(enabled=configured),
            )
        ),
        terminal_runtime_lifecycle_factory=factory,
    )
    bridge_context = SimpleNamespace(
        engine=engine,
        _terminal_runtime_lifecycle=lifecycle,
    )
    tui_context = SimpleNamespace(
        engine=engine,
        _terminal_runtime_lifecycle=lifecycle,
        _terminal_runtime_lifecycle_factory=factory,
    )

    new_ui_payload = (
        JsonlEngineBridge._runtime_heartbeat_retention_status_payload(
            bridge_context
        )
    )
    tui_payload = NaumiApp._runtime_heartbeat_retention_status_payload(
        tui_context
    )

    assert new_ui_payload == scenario["expected"]
    assert tui_payload == scenario["expected"]
    assert new_ui_payload == tui_payload
    assert (
        runtime_heartbeat_retention_health_item(new_ui_payload)
        == runtime_heartbeat_retention_health_item(tui_payload)
    )
