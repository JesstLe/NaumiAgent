from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.agent_control import AGENT_CONTROL_SCHEMA_VERSION
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
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert registry.compatible_registry_sha256 == (
        registry.registry_sha256,
        *document["compatibility"]["previous_registry_sha256"],
    )
    assert registry.policy("server", "permission/request").owner == "safety"
    assert registry.policy("server", "run/completed").criticality == "terminal"
    assert registry.policy("client", "ping").persistence == "never"
    assert registry.required_capability(
        "client", "evolution/evaluation-lane/request"
    ) == "evolution_evaluation_lane"
    assert registry.required_capability(
        "server", "evolution/evaluation-lane"
    ) == "evolution_evaluation_lane"
    assert registry.required_capability(
        "client", "pursuit/recovery/resume"
    ) == "pursuit_recovery_actions"
    assert registry.required_capability(
        "server", "pursuit/recovery/action_result"
    ) == "pursuit_recovery_actions"
    assert registry.required_capability(
        "client", "agents/recovery/resolve_unknown"
    ) == "agent_recovery_actions"
    assert registry.required_capability(
        "server", "agents/recovery/action_result"
    ) == "agent_recovery_actions"
    assert registry.required_capability("client", "submit") is None
    with pytest.raises(TypeError):
        registry.client["future/event"] = registry.policy("client", "ping")  # type: ignore[index]


def test_published_agent_control_contract_tracks_recovery_schema() -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    contract = document["agent_control"]

    assert contract["schema_version"] == AGENT_CONTROL_SCHEMA_VERSION
    assert contract["recovery_catalog_fields"] == [
        "assessed_at",
        "items",
        "truncated",
    ]
    assert "receipt_sha256" in contract["recovery_item_fields"]
    assert set(contract["recovery_kinds"]) == {"job", "publication"}
    assert set(contract["recovery_session_scopes"]) == {
        "current",
        "other",
        "unknown",
    }
    assert contract["recovery_action_fields"] == [
        "action",
        "job_id",
        "accepted",
        "applied",
        "code",
        "message",
        "job_state",
        "claim_epoch",
        "receipt_sha256",
    ]


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


def test_registry_accepts_only_explicit_unique_previous_digests(
    tmp_path: Path,
) -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    previous_digest = "a" * 64
    document["compatibility"]["previous_registry_sha256"] = [previous_digest]
    path = tmp_path / "compatible.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    registry = load_protocol_event_registry(path)

    assert registry.compatible_registry_sha256 == (
        registry.registry_sha256,
        previous_digest,
    )

    document["compatibility"]["previous_registry_sha256"] = [
        registry.registry_sha256,
    ]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ProtocolRegistryError, match="不得重复当前"):
        load_protocol_event_registry(path)

    document["compatibility"]["previous_registry_sha256"] = ["not-a-digest"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ProtocolRegistryError, match="唯一 SHA-256"):
        load_protocol_event_registry(path)


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: document["event_capabilities"].update(
                {"unknown_feature": {"client_events": ["ping"], "server_events": []}}
            ),
            "未发布能力",
        ),
        (
            lambda document: document["event_capabilities"][
                "evolution_evaluation_lane"
            ]["client_events"].append("future/request"),
            "未注册 client 事件",
        ),
        (
            lambda document: document["event_capabilities"].update(
                {
                    "goal_snapshot": {
                        "client_events": ["evolution/evaluation-lane/request"],
                        "server_events": [],
                    }
                }
            ),
            "多个能力重复绑定",
        ),
    ],
)
def test_registry_rejects_unsafe_capability_bindings(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    mutation(document)  # type: ignore[operator]
    path = tmp_path / "unsafe-capability.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProtocolRegistryError, match=message):
        load_protocol_event_registry(path)
