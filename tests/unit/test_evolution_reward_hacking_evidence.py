from __future__ import annotations

import pytest

from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalEvaluationResourceSummary,
)
from naumi_agent.evolution.reward_hacking_evidence import (
    RewardHackingCoverage,
    RewardHackingLaneDirection,
    RewardHackingRequiredAction,
    RewardHackingResourceKind,
    RewardHackingResourceObservation,
    _classify_lane_counts,
    _required_actions,
    _resource_observation,
)
from naumi_agent.harness.eval_receipt import EvalComparisonDecision


@pytest.mark.parametrize(
    ("candidate_rate", "candidate_failures", "decision", "fault", "rerun", "expected"),
    (
        (0.8, 1, EvalComparisonDecision.PASSED, False, False, RewardHackingLaneDirection.IMPROVED),
        (0.5, 4, EvalComparisonDecision.FAILED, True, False, RewardHackingLaneDirection.REGRESSED),
        (0.6, 2, EvalComparisonDecision.PASSED, False, False, RewardHackingLaneDirection.STABLE),
        (
            0.9,
            0,
            EvalComparisonDecision.FLAKY,
            False,
            True,
            RewardHackingLaneDirection.INCONCLUSIVE,
        ),
    ),
)
def test_lane_direction_uses_task_outcomes_not_narrative(
    candidate_rate: float,
    candidate_failures: int,
    decision: EvalComparisonDecision,
    fault: bool,
    rerun: bool,
    expected: RewardHackingLaneDirection,
) -> None:
    assert (
        _classify_lane_counts(
            baseline_pass_rate=0.6,
            candidate_pass_rate=candidate_rate,
            baseline_implementation_failures=2,
            candidate_implementation_failures=candidate_failures,
            baseline_evaluation_errors=0,
            candidate_evaluation_errors=0,
            comparison_decision=decision,
            candidate_fault=fault,
            requires_rerun=rerun,
        )
        is expected
    )


def test_missing_resource_coverage_is_unassessable_not_clear() -> None:
    baseline = _resources(samples=10)
    candidate = _resources(samples=10)
    observation = _resource_observation(
        kind=RewardHackingResourceKind.TOKENS,
        baseline=baseline,
        candidate=candidate,
        baseline_total=None,
        candidate_total=None,
        baseline_covered=0,
        candidate_covered=0,
        inflation_factor=1.5,
    )

    assert observation.coverage is RewardHackingCoverage.MISSING
    assert observation.inflated is False
    assert observation.inflation_ratio is None
    assert observation.baseline_per_sample is None
    assert observation.candidate_per_sample is None


def test_resource_inflation_uses_per_sample_ratio_and_strict_threshold() -> None:
    baseline = _resources(samples=10)
    candidate = _resources(samples=20)
    boundary = _resource_observation(
        kind=RewardHackingResourceKind.DURATION,
        baseline=baseline,
        candidate=candidate,
        baseline_total=100,
        candidate_total=400,
        baseline_covered=10,
        candidate_covered=20,
        inflation_factor=2.0,
    )
    inflated = _resource_observation(
        kind=RewardHackingResourceKind.DURATION,
        baseline=baseline,
        candidate=candidate,
        baseline_total=100,
        candidate_total=401,
        baseline_covered=10,
        candidate_covered=20,
        inflation_factor=2.0,
    )

    assert boundary.inflation_ratio == 2.0
    assert boundary.inflated is False
    assert inflated.inflation_ratio == pytest.approx(2.005)
    assert inflated.inflated is True


def test_resource_observation_rejects_forged_inflation_projection() -> None:
    summary = _resources(samples=5)
    observation = _resource_observation(
        kind=RewardHackingResourceKind.COST,
        baseline=summary,
        candidate=summary,
        baseline_total=1,
        candidate_total=1,
        baseline_covered=5,
        candidate_covered=5,
        inflation_factor=1.5,
    )
    payload = observation.model_dump(mode="json")
    payload["inflated"] = True

    with pytest.raises(ValueError):
        RewardHackingResourceObservation.model_validate(payload)


def test_concern_keeps_missing_evidence_action() -> None:
    assert _required_actions("concern", evidence_incomplete=True) == (
        RewardHackingRequiredAction.INVESTIGATE_REWARD_HACKING,
        RewardHackingRequiredAction.COLLECT_MISSING_EVIDENCE,
        RewardHackingRequiredAction.CONTINUE_TO_DECISION_STATE,
    )


def _resources(*, samples: int) -> EvolutionFinalEvaluationResourceSummary:
    return EvolutionFinalEvaluationResourceSummary(
        samples=samples,
        duration_ms=100,
        observed_tokens=None,
        token_samples=0,
        observed_cost_usd=None,
        cost_samples=0,
    )
