"""Exact policies for registered tools without permitting adjacent names."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from naumi_agent.tools.base import ToolMetadata
from naumi_agent.tools.evolution_review import (
    EvolutionCapabilityArtifactTool,
    EvolutionCapabilityGovernanceTool,
    EvolutionCapabilitySandboxExecuteTool,
    EvolutionCapabilitySandboxRequestTool,
    EvolutionCapabilityScenarioBindingTool,
    EvolutionCapabilitySpecificationTool,
)
from naumi_agent.tools.output_retention import OutputAssetRetentionRunTool
from naumi_agent.tools.subagent import AgentResultAcknowledgeTool

CAPABILITIES = (
    (EvolutionCapabilitySpecificationTool, "advance"),
    (EvolutionCapabilityGovernanceTool, "decide"),
    (EvolutionCapabilityArtifactTool, "create"),
    (EvolutionCapabilityScenarioBindingTool, "advance"),
    (EvolutionCapabilitySandboxRequestTool, "prepare"),
)
ACTIVE_MODES = (PermissionMode.PERMISSIVE, PermissionMode.MODERATE, PermissionMode.STRICT)


@pytest.mark.parametrize("mode", ACTIVE_MODES)
@pytest.mark.parametrize("tool_type,mutation", CAPABILITIES)
@pytest.mark.parametrize("inspect", (False, True))
def test_capability_tools_have_exact_bounded_artifact_policies(mode, tool_type, mutation, inspect):
    tool = tool_type(object())
    checker = PermissionChecker(mode)
    decision = checker.check(tool.name, {"action": "inspect" if inspect else mutation}, tool=tool)
    assert decision.allowed
    assert decision.code is not PermissionReasonCode.UNKNOWN_TOOL
    assert decision.outcome is PermissionOutcome.ALLOW
    assert decision.risk_level is PermissionRiskLevel.MEDIUM
    assert not decision.requires_confirmation
    assert not decision.allow_session_grant
    assert tool.metadata.delegated_tool_names == ()


@pytest.mark.parametrize("mode", ACTIVE_MODES)
def test_acknowledge_is_low_risk_but_retention_requires_individual_confirmation(mode):
    checker = PermissionChecker(mode)
    acknowledge = AgentResultAcknowledgeTool(object(), session_id_getter=lambda: "session")
    ack = checker.check(acknowledge.name, {}, tool=acknowledge)
    assert ack.allowed and ack.outcome is PermissionOutcome.ALLOW
    assert ack.risk_level is PermissionRiskLevel.LOW
    assert not ack.requires_confirmation and not ack.allow_session_grant
    retention = OutputAssetRetentionRunTool(object())
    delete = checker.check(retention.name, {}, tool=retention)
    assert delete.allowed and delete.outcome is PermissionOutcome.CONFIRM
    assert delete.risk_level is PermissionRiskLevel.HIGH
    assert delete.requires_confirmation and not delete.allow_session_grant


@pytest.mark.parametrize(
    "factory",
    [item[0] for item in CAPABILITIES]
    + [
        AgentResultAcknowledgeTool,
        OutputAssetRetentionRunTool,
    ],
)
def test_new_mutating_rules_do_not_enable_lockdown(factory):
    tool = factory(object())
    decision = PermissionChecker(PermissionMode.LOCKDOWN).check(tool.name, {}, tool=tool)
    assert decision.outcome is PermissionOutcome.BLOCK
    assert decision.code is PermissionReasonCode.MODE_BLOCKED


@pytest.mark.parametrize("tool_type,_mutation", CAPABILITIES)
def test_capability_session_limit_is_enforced(tool_type, _mutation):
    tool = tool_type(object())
    checker = PermissionChecker(PermissionMode.MODERATE)
    for _ in range(50):
        assert checker.check(tool.name, {}, tool=tool).allowed
    result = checker.check(tool.name, {}, tool=tool)
    assert result.code is PermissionReasonCode.MAX_CALLS_EXCEEDED
    assert not result.allowed


def test_artifact_source_path_still_enforces_workspace(tmp_path: Path):
    allowed = tmp_path / "workspace"
    allowed.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1", encoding="utf-8")
    tool = EvolutionCapabilityArtifactTool(object())
    checker = PermissionChecker(PermissionMode.MODERATE, [str(allowed)], str(allowed))
    result = checker.check(tool.name, {"action": "create", "source_path": str(outside)}, tool=tool)
    assert not result.allowed
    assert result.code is PermissionReasonCode.PATH_OUTSIDE_SANDBOX


@pytest.mark.parametrize(
    "name",
    (
        "evolution_capability_unregistered_execute",
        "agent_result_acknowledge_extra",
        "output_asset_retention_run_unchecked",
    ),
)
def test_similar_unregistered_names_remain_rejected(name):
    tool = SimpleNamespace(metadata=ToolMetadata(read_only=False))
    result = PermissionChecker(PermissionMode.MODERATE).check(name, {}, tool=tool)
    assert not result.allowed and result.code is PermissionReasonCode.UNKNOWN_TOOL


def test_existing_sandbox_execution_delegation_is_unchanged():
    assert EvolutionCapabilitySandboxExecuteTool(object()).metadata.delegated_tool_names == (
        "bash_run",
    )
