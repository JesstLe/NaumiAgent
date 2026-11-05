"""Exact policies over production tools and the normal Engine execution path."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from pydantic_settings import BaseSettings

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal.connection_tools import PersonalActionsTool, PersonalConnectionsTool
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from naumi_agent.tools.base import ToolCall, ToolExecutionError, ToolMetadata
from naumi_agent.tools.evolution_review import (
    EvolutionCapabilityArtifactTool,
    EvolutionCapabilityGovernanceTool,
    EvolutionCapabilitySandboxRequestTool,
    EvolutionCapabilityScenarioBindingTool,
    EvolutionCapabilitySpecificationTool,
)
from naumi_agent.tools.output_retention import OutputAssetRetentionRunTool
from naumi_agent.tools.subagent import AgentResultAcknowledgeTool

POLICIES = {
    "agent_result_acknowledge": ("low", False, None, "agent_result_inbox"),
    "output_asset_retention_run": ("high", True, 50, "output_asset_retention"),
    "evolution_capability_specification": (
        "medium",
        False,
        50,
        "evolution_capability_specification",
    ),
    "evolution_capability_governance": ("medium", False, 50, "evolution_capability_governance"),
    "evolution_capability_artifact": ("medium", False, 50, "evolution_capability_artifact"),
    "evolution_capability_scenario_binding": (
        "medium",
        False,
        50,
        "evolution_capability_scenario_binding",
    ),
    "evolution_capability_sandbox_request": (
        "medium",
        False,
        50,
        "evolution_capability_sandbox_request",
    ),
    "personal_connections": ("medium", False, 200, "personal_connections"),
    "personal_actions": ("medium", False, 200, "personal_actions"),
}


@pytest.fixture
def tools():
    engine = SimpleNamespace(_session=None)
    service = SimpleNamespace(engine=engine)
    instances = [
        AgentResultAcknowledgeTool(SimpleNamespace()),
        OutputAssetRetentionRunTool(engine),
        EvolutionCapabilitySpecificationTool(engine),
        EvolutionCapabilityGovernanceTool(engine),
        EvolutionCapabilityArtifactTool(engine),
        EvolutionCapabilityScenarioBindingTool(engine),
        EvolutionCapabilitySandboxRequestTool(engine),
        PersonalConnectionsTool(service),
        PersonalActionsTool(service),
    ]
    return {tool.name: tool for tool in instances}


@pytest.mark.parametrize("name", POLICIES)
@pytest.mark.parametrize(
    "mode", [PermissionMode.PERMISSIVE, PermissionMode.MODERATE, PermissionMode.STRICT]
)
def test_exact_registered_rule_risk_confirmation_family_and_session_scope(tools, name, mode):
    risk, confirmation, cap, family = POLICIES[name]
    rule = TOOL_PERMISSIONS[name]
    decision = PermissionChecker(mode).check(name, {}, tools[name])
    assert rule.tool_name == name and rule.max_calls_per_session == cap
    assert decision.allowed and decision.code == PermissionReasonCode.ALLOWED
    assert decision.risk_level == risk and decision.tool_family == family
    assert decision.requires_confirmation is confirmation
    assert decision.outcome == (
        PermissionOutcome.CONFIRM if confirmation else PermissionOutcome.ALLOW
    )
    assert not decision.allow_session_grant and not decision.requires_double_confirm


@pytest.mark.parametrize("name", POLICIES)
def test_lockdown_blocks_registered_mutating_families_and_bypass_retains_existing_semantics(
    tools, name
):
    blocked = PermissionChecker(PermissionMode.LOCKDOWN).check(name, {}, tools[name])
    assert not blocked.allowed and blocked.code == PermissionReasonCode.MODE_BLOCKED
    bypass = PermissionChecker(PermissionMode.BYPASS).check(name, {}, tools[name])
    assert bypass.allowed and not bypass.requires_confirmation and not bypass.allow_session_grant


@pytest.mark.parametrize("name", POLICIES)
def test_common_path_checks_remain_enforced(tools, name, tmp_path):
    checker = PermissionChecker(PermissionMode.MODERATE, allowed_dirs=[str(tmp_path / "allowed")])
    result = checker.check(name, {"path": str(tmp_path / "outside")}, tools[name])
    assert not result.allowed and result.code == PermissionReasonCode.PATH_OUTSIDE_SANDBOX


@pytest.mark.parametrize("name", [name for name, rule in POLICIES.items() if rule[2]])
def test_caps_are_exact_share_canonical_namespace_and_reset(tools, name):
    checker = PermissionChecker(PermissionMode.MODERATE)
    for _ in range(POLICIES[name][2]):
        assert checker.check(name, {}, tools[name]).allowed
    exceeded = checker.check("default." + name, {}, tools[name])
    assert not exceeded.allowed and exceeded.code == PermissionReasonCode.MAX_CALLS_EXCEEDED
    checker.reset_counts()
    assert checker.check("default__" + name, {}, tools[name]).allowed


@pytest.mark.parametrize(
    "name",
    ["personal_connections", "default.personal_connections", "default__personal_connections"],
)
@pytest.mark.parametrize(
    "mode", [PermissionMode.PERMISSIVE, PermissionMode.MODERATE, PermissionMode.STRICT]
)
def test_event_to_item_confirmed_without_poisoning_read_only_operations(tools, name, mode):
    checker = PermissionChecker(mode)
    rule_before = TOOL_PERMISSIONS.get("personal_connections")
    for operation in ("context", "list", "events", "sync", "event_to_item", "list"):
        result = checker.check(name, {"operation": operation}, tools["personal_connections"])
        assert result.allowed
        assert result.requires_confirmation is (operation == "event_to_item")
        assert result.risk_level == PermissionRiskLevel.MEDIUM
        assert not result.allow_session_grant
    assert TOOL_PERMISSIONS["personal_connections"] is rule_before
    assert rule_before.requires_confirmation is False


def test_dynamic_mcp_does_not_get_downgraded_by_connection_operation_rule(tools):
    result = PermissionChecker(PermissionMode.MODERATE).check(
        "mcp__personal_connections", {"operation": "list"}, tools["personal_connections"]
    )
    assert result.allowed and result.requires_confirmation
    assert result.risk_level == PermissionRiskLevel.HIGH and result.tool_family == "mcp"


def test_unregistered_mutations_still_fail_closed():
    checker = PermissionChecker(PermissionMode.MODERATE)
    tool = SimpleNamespace(metadata=ToolMetadata(read_only=False))
    for name in (
        "personal_unknown",
        "evolution_capability_unregistered",
        "output_asset_unregistered",
    ):
        result = checker.check(name, {}, tool)
        assert not result.allowed and result.code == PermissionReasonCode.UNKNOWN_TOOL


@pytest.mark.parametrize(
    "name,operation",
    [
        ("personal_connections", "grant"),
        ("personal_connections", "oauth_start"),
        ("personal_connections", "event_to_item_typo"),
        ("personal_actions", "approve_action"),
        ("personal_actions", "grant"),
        ("personal_actions", "execute_action"),
    ],
)
async def test_normal_mode_rules_do_not_add_model_administrative_operations(tools, name, operation):
    decision = PermissionChecker(PermissionMode.MODERATE).check(
        name, {"operation": operation}, tools[name]
    )
    assert decision.allowed and not decision.requires_confirmation
    with pytest.raises(ToolExecutionError) as caught:
        await tools[name].execute(operation=operation)
    assert caught.value.code == "invalid_input"


def test_connection_json_parser_rejects_duplicate_operation(tools):
    with pytest.raises(ValueError):
        tools["personal_connections"].parse_arguments(
            '{"operation":"list","operation":"event_to_item"}'
        )


@pytest.fixture
async def normal_engine(tmp_path, monkeypatch):
    def sources(cls, settings_cls, init_settings, **_):
        return (init_settings,)

    monkeypatch.setattr(BaseSettings, "settings_customise_sources", classmethod(sources))
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")

    async def no_provider(self, **_):
        raise AssertionError("Normal-mode tool integration must not request a model")

    monkeypatch.setattr(ModelRouter, "call", no_provider)

    def no_network(*_, **__):
        raise AssertionError("This integration has no external service dependency")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = create_agent_engine(
        AppConfig(
            _env_file=None,
            workspace_root=str(workspace),
            models={"api_key": None, "catalog_path": None},
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "runtime/sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "moderate"},
            browser_daemon={"enabled": False},
        )
    )
    try:
        await engine.get_or_create_session()
        yield engine
    finally:
        await engine.shutdown()


async def test_real_engine_reads_account_tools_but_creation_requires_exact_confirmation(
    normal_engine,
    monkeypatch,
):
    engine = normal_engine
    confirmations = []

    async def deny(payload):
        confirmations.append(payload)
        return "deny"

    engine.set_permission_confirmer(deny)
    for name, operation, field in [
        ("personal_connections", "list", "connections"),
        ("personal_actions", "list_actions", "actions"),
    ]:
        result = await engine.execute_tool(
            ToolCall(id=name, name=name, arguments={"operation": operation})
        )
        assert result.status == "success", result.content
        assert json.loads(result.content)[field] == []
    assert confirmations == []
    dispatched = []
    original = engine.personal_connections.event_to_item

    async def observed_dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine.personal_connections, "event_to_item", observed_dispatch)
    rejected = await engine.execute_tool(
        ToolCall(
            id="create-item",
            name="personal_connections",
            arguments={
                "operation": "event_to_item",
                "connection_id": "pc_" + "1" * 32,
                "event_id": "original-event",
                "payload": {
                    "request_id": "confirm-before-create",
                    "event_revision": 1,
                    "title": "处理日程",
                    "instructions": "只按用户委托处理",
                    "schedule": {
                        "mode": "now",
                        "timezone": "Asia/Shanghai",
                        "misfire_policy": "run_once",
                        "grace_seconds": 3600,
                    },
                },
            },
        )
    )
    assert rejected.status == "error" and "权限拒绝" in rejected.content
    assert len(confirmations) == 1
    assert confirmations[0]["tool_name"] == "personal_connections"
    assert confirmations[0]["scope"] == "call"
    assert confirmations[0]["choices"] == ["allow_once", "deny"]
    assert dispatched == []
