from __future__ import annotations

import pytest

from naumi_agent.evolution.decision_states import (
    EvolutionDecisionReason,
    EvolutionDecisionStateValue,
    _build_escalation,
    _state_flags,
    resolve_evolution_decision_state,
)
from naumi_agent.user_interaction import normalize_interaction_request


@pytest.mark.parametrize(
    ("gate", "counterfactual", "reward", "risk", "state", "reasons"),
    (
        (
            "veto",
            None,
            None,
            "low",
            EvolutionDecisionStateValue.REJECTED,
            (EvolutionDecisionReason.MECHANICAL_VETO,),
        ),
        (
            "pass",
            "concern",
            "concern",
            "critical",
            EvolutionDecisionStateValue.REVISE,
            (
                EvolutionDecisionReason.COUNTERFACTUAL_CONCERN,
                EvolutionDecisionReason.REWARD_HACKING_CONCERN,
            ),
        ),
        (
            "pass",
            "clear",
            "inconclusive",
            "high",
            EvolutionDecisionStateValue.ESCALATED,
            (
                EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,
                EvolutionDecisionReason.HIGH_RISK_REQUIRES_HUMAN,
            ),
        ),
        (
            "pass",
            "clear",
            "clear",
            "medium",
            EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT,
            (EvolutionDecisionReason.ALL_STRUCTURED_EVIDENCE_CLEAR,),
        ),
    ),
)
def test_decision_policy_covers_all_terminal_states(
    gate: str,
    counterfactual: str | None,
    reward: str | None,
    risk: str,
    state: EvolutionDecisionStateValue,
    reasons: tuple[EvolutionDecisionReason, ...],
) -> None:
    assert resolve_evolution_decision_state(
        mechanical_gate_outcome=gate,
        counterfactual_outcome=counterfactual,
        reward_hacking_outcome=reward,
        risk_level=risk,
    ) == (state, reasons)


def test_mechanical_veto_rejects_forged_downstream_outcome() -> None:
    with pytest.raises(ValueError, match="不得携带"):
        resolve_evolution_decision_state(
            mechanical_gate_outcome="veto",
            counterfactual_outcome="clear",
            reward_hacking_outcome=None,
            risk_level="low",
        )


def test_pass_requires_complete_structured_outcomes() -> None:
    with pytest.raises(ValueError, match="缺少完整"):
        resolve_evolution_decision_state(
            mechanical_gate_outcome="pass",
            counterfactual_outcome="clear",
            reward_hacking_outcome=None,
            risk_level="low",
        )


def test_escalation_is_runtime_interaction_compatible() -> None:
    request = _build_escalation((EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE,))
    normalized = normalize_interaction_request(request.to_public_dict())

    assert normalized.to_public_dict() == request.to_public_dict()
    assert len(request.options) == 3
    assert request.options[0].value == "collect_missing_evidence"
    assert request.options[0].label.endswith("（推荐）")
    assert request.allow_custom is True


def test_high_risk_escalation_recommends_human_review() -> None:
    request = _build_escalation((EvolutionDecisionReason.HIGH_RISK_REQUIRES_HUMAN,))

    assert request.options[0].value == "request_human_review"
    assert request.allow_custom is True


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT, (False, True, True)),
        (EvolutionDecisionStateValue.REVISE, (False, True, False)),
        (EvolutionDecisionStateValue.REJECTED, (False, True, False)),
        (EvolutionDecisionStateValue.ESCALATED, (True, False, False)),
    ),
)
def test_state_readiness_flags_preserve_unresolved_escalation(
    state: EvolutionDecisionStateValue,
    expected: tuple[bool, bool, bool],
) -> None:
    assert _state_flags(state) == expected
