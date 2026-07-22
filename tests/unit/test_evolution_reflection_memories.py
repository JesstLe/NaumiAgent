from __future__ import annotations

import pytest

import naumi_agent.evolution.reflection_memories as reflection_module
from naumi_agent.evolution.decision_resolutions import (
    EvolutionDecisionResolutionOutcome,
)
from naumi_agent.evolution.decision_states import EvolutionDecisionStateValue
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionAction,
    EvolutionReflectionLessonKind,
)


@pytest.mark.parametrize(
    ("state", "outcome", "lesson", "action", "decided", "learning"),
    [
        (
            EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT,
            None,
            EvolutionReflectionLessonKind.VALIDATED_EXPERIMENT,
            EvolutionReflectionAction.REVIEW_FOR_PROMOTION,
            True,
            True,
        ),
        (
            EvolutionDecisionStateValue.REVISE,
            None,
            EvolutionReflectionLessonKind.STRUCTURED_REVISION,
            EvolutionReflectionAction.REVISE_CANDIDATE,
            True,
            True,
        ),
        (
            EvolutionDecisionStateValue.REJECTED,
            None,
            EvolutionReflectionLessonKind.MECHANICAL_REJECTION,
            EvolutionReflectionAction.TERMINATE_CANDIDATE,
            True,
            True,
        ),
        (
            EvolutionDecisionStateValue.ESCALATED,
            EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED,
            EvolutionReflectionLessonKind.EVIDENCE_GAP,
            EvolutionReflectionAction.COLLECT_EVIDENCE,
            False,
            True,
        ),
        (
            EvolutionDecisionStateValue.ESCALATED,
            EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED,
            EvolutionReflectionLessonKind.RISK_ESCALATION,
            EvolutionReflectionAction.HUMAN_REVIEW,
            False,
            True,
        ),
        (
            EvolutionDecisionStateValue.ESCALATED,
            EvolutionDecisionResolutionOutcome.REVISE,
            EvolutionReflectionLessonKind.USER_DIRECTED_REVISION,
            EvolutionReflectionAction.REVISE_CANDIDATE,
            True,
            True,
        ),
        (
            EvolutionDecisionStateValue.ESCALATED,
            EvolutionDecisionResolutionOutcome.REJECTED,
            EvolutionReflectionLessonKind.USER_DIRECTED_REJECTION,
            EvolutionReflectionAction.TERMINATE_CANDIDATE,
            True,
            True,
        ),
        (
            EvolutionDecisionStateValue.ESCALATED,
            EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP,
            EvolutionReflectionLessonKind.CUSTOM_FOLLOW_UP,
            EvolutionReflectionAction.CONSTRAINED_CUSTOM_FOLLOW_UP,
            False,
            False,
        ),
    ],
)
def test_reflection_projection_is_total_and_deterministic(
    state: EvolutionDecisionStateValue,
    outcome: EvolutionDecisionResolutionOutcome | None,
    lesson: EvolutionReflectionLessonKind,
    action: EvolutionReflectionAction,
    decided: bool,
    learning: bool,
) -> None:
    assert reflection_module._projection_for_values(state, outcome) == (  # noqa: SLF001
        lesson,
        action,
        decided,
        learning,
    )


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (
            EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT,
            EvolutionDecisionResolutionOutcome.REVISE,
        ),
        (EvolutionDecisionStateValue.ESCALATED, None),
    ],
)
def test_reflection_projection_rejects_impossible_state_resolution_pairs(
    state: EvolutionDecisionStateValue,
    outcome: EvolutionDecisionResolutionOutcome | None,
) -> None:
    with pytest.raises(ValueError):
        reflection_module._projection_for_values(state, outcome)  # noqa: SLF001


def test_reflection_memory_contract_is_available_from_public_evolution_api() -> None:
    from naumi_agent.evolution import (  # noqa: PLC0415
        EvolutionReflectionMemoryBuilder,
        EvolutionReflectionMemoryRevoker,
    )

    assert EvolutionReflectionMemoryBuilder is reflection_module.EvolutionReflectionMemoryBuilder
    assert EvolutionReflectionMemoryRevoker is reflection_module.EvolutionReflectionMemoryRevoker
