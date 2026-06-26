from __future__ import annotations

from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    FailureKind,
    IssueMetadata,
    LeaseState,
    ParallelMode,
    RiskLevel,
    WorkbenchEvent,
)


def test_issue_metadata_defaults_are_safe() -> None:
    issue = IssueMetadata(session_id="s", task_id="1", mission_id="m1")

    assert issue.parallel_mode == ParallelMode.EXCLUSIVE
    assert issue.risk_level == RiskLevel.MEDIUM
    assert issue.requires_human_approval is True
    assert issue.acceptance_criteria == []
    assert issue.expected_artifacts == []


def test_event_payload_is_json_ready() -> None:
    event = WorkbenchEvent(
        session_id="s",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="1",
        payload={"lease_id": "lease-1"},
    )

    assert event.to_dict()["type"] == "issue.claimed"
    assert event.to_dict()["payload"]["lease_id"] == "lease-1"


def test_enum_values_are_stable_for_api_contract() -> None:
    assert ParallelMode.COMPETITIVE.value == "competitive"
    assert RiskLevel.CRITICAL.value == "critical"
    assert LeaseState.EXPIRED.value == "expired"
    assert ApprovalState.WAITING.value == "waiting"
    assert DecisionKind.ARCHITECTURE.value == "architecture"
    assert FailureKind.SCOPE_VIOLATION.value == "scope_violation"
    assert ContextHealth.STALE.value == "stale"
