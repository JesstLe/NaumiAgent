from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.ui.protocol import ClientEventType, ServerEventType
from naumi_agent.ui.protocol_registry import (
    ProtocolRegistryError,
    load_protocol_event_registry,
)

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "terminal-ui"
    / "protocol-contract.json"
)


def test_published_event_registry_exactly_covers_python_protocol_enums() -> None:
    registry = load_protocol_event_registry(CONTRACT)

    assert set(registry.client) == {str(event) for event in ClientEventType}
    assert set(registry.server) == {str(event) for event in ServerEventType}
    assert len(registry.registry_sha256) == 64
    assert registry.policy("server", "permission/request").owner == "safety"
    assert registry.policy("server", "run/completed").criticality == "terminal"
    assert registry.policy("client", "ping").persistence == "never"
    with pytest.raises(TypeError):
        registry.client["future/event"] = registry.policy("client", "ping")  # type: ignore[index]


def test_sensitive_persistent_events_require_explicit_redaction() -> None:
    registry = load_protocol_event_registry(CONTRACT)
    policies = tuple(registry.client.values()) + tuple(registry.server.values())

    assert any(policy.sensitive_fields for policy in policies)
    assert all(
        policy.redaction == "required"
        for policy in policies
        if policy.sensitive_fields
    )
    assert all(
        policy.redaction == "none"
        for policy in policies
        if not policy.sensitive_fields
    )


def test_registry_rejects_missing_event_and_unsafe_sensitive_policy(tmp_path: Path) -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    document["event_registry"]["client"].pop("ping")
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProtocolRegistryError, match="missing=.*ping"):
        load_protocol_event_registry(missing)

    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    document["event_registry"]["server"]["ui/message"]["redaction"] = "none"
    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProtocolRegistryError, match="敏感字段"):
        load_protocol_event_registry(unsafe)


def test_registry_query_rejects_unknown_event() -> None:
    registry = load_protocol_event_registry(CONTRACT)

    with pytest.raises(ProtocolRegistryError, match="未注册"):
        registry.policy("server", "future/unknown")
    with pytest.raises(ProtocolRegistryError, match="未知事件方向"):
        registry.policy("sideways", "ping")  # type: ignore[arg-type]


def test_registry_rejects_unknown_top_level_policy_group(tmp_path: Path) -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    document["event_registry"]["future"] = {}
    path = tmp_path / "extra-group.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProtocolRegistryError, match="只能包含 client/server"):
        load_protocol_event_registry(path)

    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    document["version"] = True
    path = tmp_path / "boolean-version.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProtocolRegistryError, match="contract_version"):
        load_protocol_event_registry(path)
