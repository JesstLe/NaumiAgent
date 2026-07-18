from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_policy import EvalPolicyVerdict, evaluate_eval_policy
from naumi_agent.harness.eval_receipt import (
    EvalComparisonDecision,
    EvalReceiptSample,
    build_eval_comparison_receipt,
    eval_result_sha256,
    eval_sample_set_sha256,
)
from naumi_agent.harness.eval_statistics import (
    EvalStatisticalVerdict,
    compare_eval_repetitions,
)
from naumi_agent.harness.eval_suite_compare import (
    EvalMechanicalVerdict,
    compare_eval_suite_results,
    render_eval_suite_comparison,
)
from naumi_agent.harness.store import HarnessStore

METRIC = "self_review.broad_except.count"


def _identity(*, commit: str, repetitions: int = 1):
    policy = HarnessEvalComparisonPolicy()
    return build_eval_baseline_identity(
        Path("."),
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="self-review-static",
            suite_sha256="a" * 64,
            profile_sha256="b" * 64,
            policy_sha256=policy.sha256,
            runner_version="self_review_static@1",
            repetitions=repetitions,
            live=False,
        ),
        source_identity=HarnessEvalSourceIdentity(
            commit=commit * 40,
            tree_sha256=f"sha256:{commit * 64}",
            dirty=False,
        ),
        platform_identity=HarnessEvalPlatformIdentity(
            system="linux",
            release="6.12",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
            naumi_version="0.1.214",
        ),
    )


def _observation(
    value: float,
    *,
    target: float = 0,
    unit: str = "count",
    direction: str = "decrease",
) -> HarnessEvalMetricObservation:
    return HarnessEvalMetricObservation(
        metric=METRIC,
        value=value,
        unit=unit,
        direction=direction,
        target=target,
        primary=True,
    )


def _suite(
    *,
    commit: str,
    value: float,
    repetitions: int = 1,
    target: float = 0,
    unit: str = "count",
    direction: str = "decrease",
) -> HarnessEvalSuiteResult:
    observation = _observation(
        value,
        target=target,
        unit=unit,
        direction=direction,
    )
    status = (
        EvalCaseStatus.PASSED
        if observation.target_met
        else EvalCaseStatus.IMPLEMENTATION_FAILURE
    )
    case = HarnessEvalCaseResult(
        case_id="static-scan",
        runner="self_review_static@1",
        status=status,
        primary_metric=METRIC,
        metric_observations=(observation,),
    )
    return HarnessEvalSuiteResult(
        suite_id="self-review-static",
        title="Self-Review 静态指标",
        suite_path="evolution:self-review-static",
        suite_sha256="a" * 64,
        status=(
            EvalRunStatus.PASSED
            if status is EvalCaseStatus.PASSED
            else EvalRunStatus.FAILED
        ),
        cases=(case,),
        baseline_identity=_identity(commit=commit, repetitions=repetitions),
    )


def test_quantitative_observation_is_finite_ordered_and_status_bound() -> None:
    case = _suite(commit="1", value=3).cases[0]

    assert case.primary_metric == METRIC
    assert case.metric_observations[0].value == 3
    assert case.metric_observations[0].target_met is False
    assert case.status is EvalCaseStatus.IMPLEMENTATION_FAILURE

    with pytest.raises(ValidationError):
        _observation(float("nan"))
    with pytest.raises(ValidationError, match="整数"):
        _observation(1.5)
    with pytest.raises(ValidationError, match="布尔"):
        HarnessEvalMetricObservation(
            metric=METRIC,
            value=True,
            unit="count",
            direction="decrease",
            target=0,
            primary=True,
        )
    with pytest.raises(ValidationError, match="status"):
        HarnessEvalCaseResult(
            case_id="forged-pass",
            runner="self_review_static@1",
            status=EvalCaseStatus.PASSED,
            primary_metric=METRIC,
            metric_observations=(_observation(3),),
        )
    with pytest.raises(ValidationError, match="排序|重复"):
        HarnessEvalCaseResult(
            case_id="duplicate",
            runner="self_review_static@1",
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
            primary_metric=METRIC,
            metric_observations=(_observation(3), _observation(3)),
        )

    large_target = HarnessEvalMetricObservation(
        metric="quality.score",
        value=1_000_000_000_000_000 - 100,
        unit="scalar",
        direction="increase",
        target=1_000_000_000_000_000,
        primary=True,
    )
    assert large_target.target_met is False


def test_mechanical_comparator_uses_directional_numeric_delta_before_target() -> None:
    baseline = _suite(commit="1", value=5)
    improved = compare_eval_suite_results(
        baseline,
        _suite(commit="2", value=2),
    )
    regressed = compare_eval_suite_results(
        baseline,
        _suite(commit="2", value=7),
    )

    metric = next(
        item for item in improved.metric_deltas if item.metric.endswith(METRIC)
    )
    assert improved.verdict is EvalMechanicalVerdict.IMPROVED
    assert improved.improvement_count == 0
    assert improved.metric_improvement_count == 1
    assert metric.delta == -3
    assert metric.direction == "decrease"
    assert metric.unit == "count"
    assert metric.target == 0
    rendered = render_eval_suite_comparison(improved)
    assert METRIC in rendered
    assert "5 → 2 count" in rendered
    assert regressed.verdict is EvalMechanicalVerdict.REGRESSED
    assert regressed.metric_regression_count == 1


def test_metric_contract_drift_is_inconclusive_not_a_product_change() -> None:
    result = compare_eval_suite_results(
        _suite(commit="1", value=5),
        _suite(commit="2", value=5, target=1),
    )

    assert result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert result.code == "metric_observation_contract_mismatch"
    assert result.metric_deltas == ()


def test_increase_metric_uses_the_opposite_direction() -> None:
    result = compare_eval_suite_results(
        _suite(commit="1", value=5, target=10, direction="increase"),
        _suite(commit="2", value=8, target=10, direction="increase"),
    )

    assert result.verdict is EvalMechanicalVerdict.IMPROVED
    assert result.metric_improvement_count == 1


def test_numeric_regression_inside_target_still_fails_zero_regression_policy() -> None:
    baseline = _suite(commit="1", value=5, target=10)
    current = _suite(commit="2", value=7, target=10)

    policy = evaluate_eval_policy(baseline, current)

    assert policy.mechanical.verdict is EvalMechanicalVerdict.REGRESSED
    assert policy.mechanical.regression_count == 0
    assert policy.mechanical.metric_regression_count == 1
    assert policy.verdict is EvalPolicyVerdict.FAILED
    assert "max_regressions_violated" in {
        item.code for item in policy.violations
    }


def test_repeated_numeric_metric_reports_directional_confidence_interval() -> None:
    baseline = tuple(
        _suite(commit="1", value=value, repetitions=5)
        for value in (5, 5, 6, 5, 4)
    )
    current = tuple(
        _suite(commit="2", value=value, repetitions=5)
        for value in (2, 2, 3, 2, 1)
    )

    result = compare_eval_repetitions(baseline, current)
    metric = next(item for item in result.differences if item.metric.endswith(METRIC))

    assert result.verdict is EvalStatisticalVerdict.IMPROVED
    assert metric.direction == "decrease"
    assert metric.primary is True
    assert metric.delta == pytest.approx(-3)
    assert metric.confidence_high < 0


@pytest.mark.asyncio
async def test_quantitative_metric_round_trips_through_h5a_store(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = _suite(commit="1", value=3)

    stored = await store.record_eval_result(
        workspace_root=workspace,
        batch_id="evolution:red:self-review",
        sample_index=0,
        result=result,
        created_at=datetime(2026, 7, 19, 3, 0, tzinfo=UTC).isoformat(),
    )
    restored = await store.get_eval_result(
        workspace,
        "evolution:red:self-review",
        "self-review-static",
        0,
    )

    assert restored == stored
    assert restored is not None
    observation = restored.result.cases[0].metric_observations[0]
    assert observation.metric == METRIC
    assert observation.value == 3
    assert observation.unit == "count"


def test_quantitative_red_green_cohorts_enter_h5c_receipt() -> None:
    baseline_results = tuple(
        _suite(commit="1", value=3, repetitions=5)
        for _ in range(5)
    )
    current_results = tuple(
        _suite(commit="2", value=0, repetitions=5)
        for _ in range(5)
    )
    baseline = tuple(
        EvalReceiptSample(
            sample_index=index,
            result_sha256=eval_result_sha256(result),
            result=result,
        )
        for index, result in enumerate(baseline_results)
    )
    current = tuple(
        EvalReceiptSample(
            sample_index=index,
            result_sha256=eval_result_sha256(result),
            result=result,
        )
        for index, result in enumerate(current_results)
    )

    receipt = build_eval_comparison_receipt(
        workspace_root=".",
        suite_id="self-review-static",
        baseline_id="c" * 64,
        baseline_batch_id="evolution:red:self-review",
        baseline_samples_sha256=eval_sample_set_sha256(baseline),
        baseline_samples=baseline,
        current_batch_id="evolution:green:self-review",
        current_samples=current,
        created_at=datetime(2026, 7, 19, 3, 30, tzinfo=UTC).isoformat(),
    )

    assert receipt.statistical_verdict is EvalStatisticalVerdict.IMPROVED
    assert receipt.decision is EvalComparisonDecision.PASSED
    assert all(
        item.mechanical_verdict is EvalMechanicalVerdict.IMPROVED
        for item in receipt.sample_evidence
    )
