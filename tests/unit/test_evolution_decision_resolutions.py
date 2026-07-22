from __future__ import annotations

from types import SimpleNamespace

import pytest

from naumi_agent.evolution.decision_resolutions import (
    EvolutionDecisionResolutionAction,
    EvolutionDecisionResolutionOutcome,
    resolve_escalation_answer,
)
from naumi_agent.evolution.decision_states import (
    EvolutionDecisionReason,
    EvolutionDecisionStateValue,
    _build_escalation,
)
from naumi_agent.harness.interaction import (
    HarnessInteractionRecord,
    answer_interaction,
    new_interaction_record,
)
from naumi_agent.user_interaction import normalize_interaction_request


def _answered(
    *,
    reason: EvolutionDecisionReason,
    response: dict[str, object],
) -> tuple[SimpleNamespace, HarnessInteractionRecord]:
    escalation = _build_escalation((reason,))
    decision = SimpleNamespace(
        state=EvolutionDecisionStateValue.ESCALATED,
        escalation=escalation,
        decision_id=f"evdecision_{'a' * 24}",
    )
    record = new_interaction_record(
        request=normalize_interaction_request(escalation.to_public_dict()),
        subject_kind="tool",
        subject_id=decision.decision_id,
        session_id="session-resolution",
        agent_name="main",
        owner_id="resolution-test-owner",
        created_at="2026-07-22T12:00:00+00:00",
        owner_lease_seconds=30,
        interaction_id=f"ask-evolution-{'a' * 24}-1",
    )
    record = answer_interaction(
        record,
        owner_id=record.owner_id,
        owner_epoch=record.owner_epoch,
        response=response,
        answered_by="user",
        now="2026-07-22T12:00:01+00:00",
    )
    return decision, record


@pytest.mark.parametrize(
    ("reason", "value", "action", "outcome", "follow_up", "decided"),
    (
        (
            EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,
            "collect_missing_evidence",
            EvolutionDecisionResolutionAction.COLLECT_MISSING_EVIDENCE,
            EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED,
            True,
            False,
        ),
        (
            EvolutionDecisionReason.HIGH_RISK_REQUIRES_HUMAN,
            "request_human_review",
            EvolutionDecisionResolutionAction.REQUEST_HUMAN_REVIEW,
            EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED,
            True,
            False,
        ),
        (
            EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,
            "revise_candidate",
            EvolutionDecisionResolutionAction.REVISE_CANDIDATE,
            EvolutionDecisionResolutionOutcome.REVISE,
            False,
            True,
        ),
        (
            EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,
            "reject_candidate",
            EvolutionDecisionResolutionAction.REJECT_CANDIDATE,
            EvolutionDecisionResolutionOutcome.REJECTED,
            False,
            True,
        ),
    ),
)
def test_fenced_option_maps_to_non_promoting_resolution(
    reason: EvolutionDecisionReason,
    value: str,
    action: EvolutionDecisionResolutionAction,
    outcome: EvolutionDecisionResolutionOutcome,
    follow_up: bool,
    decided: bool,
) -> None:
    decision, record = _answered(
        reason=reason,
        response={"kind": "option", "value": value},
    )

    assert resolve_escalation_answer(decision, record) == (
        action,
        outcome,
        follow_up,
        decided,
    )


def test_custom_answer_remains_follow_up_and_cannot_accept() -> None:
    decision, record = _answered(
        reason=EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,
        response={"kind": "custom", "custom_text": "先补充 Windows 实机证据"},
    )

    assert resolve_escalation_answer(decision, record) == (
        EvolutionDecisionResolutionAction.CUSTOM_INSTRUCTION,
        EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP,
        True,
        False,
    )


def test_pending_interaction_cannot_form_resolution() -> None:
    escalation = _build_escalation((EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,))
    decision = SimpleNamespace(
        state=EvolutionDecisionStateValue.ESCALATED,
        escalation=escalation,
        decision_id=f"evdecision_{'b' * 24}",
    )
    pending = new_interaction_record(
        request=normalize_interaction_request(escalation.to_public_dict()),
        subject_kind="tool",
        subject_id=decision.decision_id,
        session_id="session-resolution",
        agent_name="main",
        owner_id="resolution-test-owner",
        created_at="2026-07-22T12:00:00+00:00",
        owner_lease_seconds=30,
        interaction_id=f"ask-evolution-{'b' * 24}-1",
    )

    with pytest.raises(ValueError, match="尚未 answered"):
        resolve_escalation_answer(decision, pending)
