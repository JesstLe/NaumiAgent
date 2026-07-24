from __future__ import annotations

import pytest
from pydantic import ValidationError

from naumi_agent.orchestrator.pursuit_terminal import (
    PursuitBoundaryDecision,
    PursuitBoundaryFacts,
    decide_pursuit_boundary,
    pursuit_boundary_decision_sha256,
)


@pytest.mark.parametrize(
    ("updates", "status", "code", "terminal", "resumable"),
    [
        ({}, "running", "criteria_incomplete", False, False),
        (
            {"verified_count": 2, "hard_evidence_count": 2},
            "running",
            "final_verification_required",
            False,
            False,
        ),
        (
            {
                "verified_count": 2,
                "hard_evidence_count": 2,
                "final_verification": "failed",
            },
            "running",
            "final_verification_failed",
            False,
            False,
        ),
        (
            {
                "verified_count": 2,
                "hard_evidence_count": 2,
                "final_verification": "passed",
            },
            "completed",
            "completed_verified",
            True,
            False,
        ),
        (
            {"waiting_kind": "background", "waiting_count": 3},
            "waiting",
            "waiting_for_background",
            False,
            True,
        ),
        (
            {"waiting_kind": "interaction", "waiting_count": 1},
            "waiting",
            "waiting_for_interaction",
            False,
            True,
        ),
        (
            {"blocker": "planner_empty"},
            "blocked",
            "planner_empty",
            True,
            True,
        ),
        (
            {"blocker": "stagnation_no_recovery"},
            "blocked",
            "stagnation_no_recovery",
            True,
            True,
        ),
        (
            {"cancel_requested": True},
            "cancelled",
            "user_cancelled",
            True,
            False,
        ),
        (
            {"budget_breach": "time"},
            "budget_exceeded",
            "time_budget_exceeded",
            True,
            False,
        ),
        (
            {"budget_breach": "cost"},
            "budget_exceeded",
            "cost_budget_exceeded",
            True,
            False,
        ),
        (
            {"budget_breach": "iterations"},
            "budget_exceeded",
            "iteration_budget_exceeded",
            True,
            False,
        ),
    ],
)
def test_boundary_decision_covers_every_runtime_outcome(
    updates: dict[str, object],
    status: str,
    code: str,
    terminal: bool,
    resumable: bool,
) -> None:
    facts = PursuitBoundaryFacts(
        criterion_count=2,
        verified_count=0,
        hard_evidence_count=0,
    ).model_copy(update=updates)
    facts = PursuitBoundaryFacts.model_validate(facts.model_dump(mode="python"))

    first = decide_pursuit_boundary(facts)
    second = decide_pursuit_boundary(facts)

    assert first == second
    assert first.status == status
    assert first.code == code
    assert first.terminal is terminal
    assert first.resumable is resumable
    assert first.facts == facts
    assert first.decision_id == pursuit_boundary_decision_sha256(first)


def test_cancel_precedes_other_boundary_facts_without_losing_identity() -> None:
    facts = PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=0,
        hard_evidence_count=0,
        cancel_requested=True,
        budget_breach="cost",
        waiting_kind="background",
        waiting_count=1,
        blocker="planner_empty",
    )

    decision = decide_pursuit_boundary(facts)

    assert decision.status == "cancelled"
    assert decision.code == "user_cancelled"
    assert len(decision.facts_sha256) == 64


def test_zero_criteria_fails_closed_as_explicit_blocker() -> None:
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=0,
        verified_count=0,
        hard_evidence_count=0,
    ))

    assert decision.status == "blocked"
    assert decision.code == "no_success_criteria"
    assert decision.resumable is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "criterion_count": 1,
            "verified_count": 2,
            "hard_evidence_count": 0,
        },
        {
            "criterion_count": 2,
            "verified_count": 1,
            "hard_evidence_count": 2,
        },
        {
            "criterion_count": 1,
            "verified_count": 0,
            "hard_evidence_count": 0,
            "waiting_kind": "background",
            "waiting_count": 0,
        },
        {
            "criterion_count": 1,
            "verified_count": 0,
            "hard_evidence_count": 0,
            "waiting_kind": "interaction",
            "waiting_count": 2,
        },
        {
            "criterion_count": 1,
            "verified_count": 0,
            "hard_evidence_count": 0,
            "waiting_kind": "background",
            "waiting_count": 1,
            "blocker": "planner_empty",
        },
        {
            "criterion_count": 1,
            "verified_count": 0,
            "hard_evidence_count": 0,
            "budget_breach": "time",
            "blocker": "planner_empty",
        },
        {
            "criterion_count": 2,
            "verified_count": 1,
            "hard_evidence_count": 1,
            "final_verification": "passed",
        },
        {
            "criterion_count": 2,
            "verified_count": 2,
            "hard_evidence_count": 2,
            "final_verification": "passed",
            "blocker": "planner_empty",
        },
    ],
)
def test_boundary_facts_reject_ambiguous_or_impossible_states(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PursuitBoundaryFacts.model_validate(payload)


def test_decision_model_rejects_tampered_status_or_digest() -> None:
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    payload = decision.model_dump(mode="json")
    payload["status"] = "cancelled"

    with pytest.raises(ValidationError):
        PursuitBoundaryDecision.model_validate(payload)

    payload = decision.model_dump(mode="json")
    payload["decision_id"] = "0" * 64
    with pytest.raises(ValidationError, match="内容不一致"):
        PursuitBoundaryDecision.model_validate(payload)

    payload = decision.model_dump(mode="json")
    payload["facts"]["criterion_count"] = 2
    payload["facts"]["verified_count"] = 2
    payload["facts"]["hard_evidence_count"] = 2
    with pytest.raises(ValidationError, match="机械事实不一致"):
        PursuitBoundaryDecision.model_validate(payload)
