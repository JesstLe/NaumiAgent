from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.eval_compare import EvalIdentityComparisonStatus
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
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_suite_compare import (
    EvalCaseTransitionKind,
    EvalMechanicalVerdict,
    compare_eval_suite_results,
    render_eval_suite_comparison,
)


def _identity(
    *,
    commit: str,
    suite_sha256: str = "a" * 64,
):
    return build_eval_baseline_identity(
        Path("."),
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="protocol-regression",
            suite_sha256=suite_sha256,
            profile_sha256="b" * 64,
            runner_version="protocol_hello@1",
            repetitions=1,
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


def _case(
    case_id: str,
    status: EvalCaseStatus,
    *,
    runner: str = "protocol_hello",
) -> HarnessEvalCaseResult:
    return HarnessEvalCaseResult(
        case_id=case_id,
        runner=runner,
        status=status,
        code="fixture_error" if status is EvalCaseStatus.EVALUATION_ERROR else "",
        message=str(status),
    )


def _suite(
    identity,
    cases: tuple[HarnessEvalCaseResult, ...],
    *,
    suite_id: str = "protocol-regression",
    suite_sha256: str = "a" * 64,
) -> HarnessEvalSuiteResult:
    status = (
        EvalRunStatus.PASSED
        if all(case.status is EvalCaseStatus.PASSED for case in cases)
        else EvalRunStatus.FAILED
    )
    return HarnessEvalSuiteResult(
        suite_id=suite_id,
        title="协议回归",
        suite_path="evals/protocol.yaml",
        suite_sha256=suite_sha256,
        status=status,
        cases=cases,
        baseline_identity=identity,
    )


def test_unchanged_passes_produce_zero_mechanical_delta() -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("a", EvalCaseStatus.PASSED), _case("b", EvalCaseStatus.PASSED)),
    )
    current = _suite(
        _identity(commit="2"),
        (_case("a", EvalCaseStatus.PASSED), _case("b", EvalCaseStatus.PASSED)),
    )

    result = compare_eval_suite_results(baseline, current)
    metrics = {item.metric: item for item in result.metric_deltas}

    assert result.identity.status is EvalIdentityComparisonStatus.COMPARABLE
    assert result.verdict is EvalMechanicalVerdict.UNCHANGED
    assert result.code == ""
    assert result.regression_count == 0
    assert result.improvement_count == 0
    assert all(
        item.kind is EvalCaseTransitionKind.UNCHANGED_PASS
        for item in result.transitions
    )
    assert metrics["pass_rate"].baseline == 1.0
    assert metrics["pass_rate"].current == 1.0
    assert metrics["pass_rate"].delta == 0.0


def test_pass_to_implementation_failure_is_regression() -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("stable", EvalCaseStatus.PASSED),),
    )
    current = _suite(
        _identity(commit="2"),
        (_case("stable", EvalCaseStatus.IMPLEMENTATION_FAILURE),),
    )

    result = compare_eval_suite_results(baseline, current)
    metrics = {item.metric: item for item in result.metric_deltas}

    assert result.verdict is EvalMechanicalVerdict.REGRESSED
    assert result.regression_count == 1
    assert result.transitions[0].kind is EvalCaseTransitionKind.REGRESSION
    assert metrics["implementation_failures"].delta == 1
    assert metrics["implementation_failures"].relative_delta is None


def test_implementation_failure_to_pass_is_improvement() -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("fixed", EvalCaseStatus.IMPLEMENTATION_FAILURE),),
    )
    current = _suite(
        _identity(commit="2"),
        (_case("fixed", EvalCaseStatus.PASSED),),
    )

    result = compare_eval_suite_results(baseline, current)

    assert result.verdict is EvalMechanicalVerdict.IMPROVED
    assert result.improvement_count == 1
    assert result.transitions[0].kind is EvalCaseTransitionKind.IMPROVEMENT


@pytest.mark.parametrize(
    "unstable_status",
    [EvalCaseStatus.EVALUATION_ERROR, EvalCaseStatus.SKIPPED],
)
def test_eval_error_or_skip_is_inconclusive_not_product_regression(
    unstable_status: EvalCaseStatus,
) -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("case", EvalCaseStatus.PASSED),),
    )
    current = _suite(
        _identity(commit="2"),
        (_case("case", unstable_status),),
    )

    result = compare_eval_suite_results(baseline, current)

    assert result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert result.code == "evaluation_instability"
    assert result.regression_count == 0
    assert result.transitions[0].kind is EvalCaseTransitionKind.EVALUATION_INSTABILITY


def test_missing_or_incompatible_identity_stops_before_metrics() -> None:
    baseline_identity = _identity(commit="1")
    current_identity = _identity(commit="2", suite_sha256="c" * 64)
    missing = compare_eval_suite_results(
        _suite(baseline_identity, (_case("a", EvalCaseStatus.PASSED),)),
        _suite(None, (_case("a", EvalCaseStatus.PASSED),)),
    )
    incompatible = compare_eval_suite_results(
        _suite(baseline_identity, (_case("a", EvalCaseStatus.PASSED),)),
        _suite(
            current_identity,
            (_case("a", EvalCaseStatus.PASSED),),
            suite_sha256="c" * 64,
        ),
    )

    assert missing.verdict is EvalMechanicalVerdict.INCOMPATIBLE
    assert missing.code == "identity_missing"
    assert missing.metric_deltas == ()
    assert incompatible.verdict is EvalMechanicalVerdict.INCOMPATIBLE
    assert incompatible.code == "identity_incompatible"
    assert "suite_digest_mismatch" in incompatible.identity.blocking_codes


@pytest.mark.parametrize("mutation", ["case_set", "runner"])
def test_case_set_or_runner_drift_is_inconclusive(
    mutation: str,
) -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("a", EvalCaseStatus.PASSED),),
    )
    current_cases = (
        (_case("b", EvalCaseStatus.PASSED),)
        if mutation == "case_set"
        else (_case("a", EvalCaseStatus.PASSED, runner="other_runner"),)
    )

    result = compare_eval_suite_results(
        baseline,
        _suite(_identity(commit="2"), current_cases),
    )

    assert result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert result.code == (
        "case_set_mismatch" if mutation == "case_set" else "case_runner_mismatch"
    )
    assert result.metric_deltas == ()


def test_result_and_identity_suite_binding_mismatch_is_rejected() -> None:
    identity = _identity(commit="1")
    baseline = _suite(identity, (_case("a", EvalCaseStatus.PASSED),))
    forged = _suite(
        _identity(commit="2"),
        (_case("a", EvalCaseStatus.PASSED),),
        suite_id="different-result-id",
    )

    result = compare_eval_suite_results(baseline, forged)

    assert result.verdict is EvalMechanicalVerdict.INCOMPATIBLE
    assert result.code == "result_identity_mismatch"
    assert result.metric_deltas == ()


def test_duplicate_case_or_inconsistent_summary_is_inconclusive() -> None:
    baseline = _suite(
        _identity(commit="1"),
        (_case("a", EvalCaseStatus.PASSED),),
    )
    duplicate = _suite(
        _identity(commit="2"),
        (
            _case("a", EvalCaseStatus.PASSED),
            _case("a", EvalCaseStatus.PASSED),
        ),
    )
    inconsistent = _suite(
        _identity(commit="2"),
        (_case("a", EvalCaseStatus.PASSED),),
    ).model_copy(update={"status": EvalRunStatus.FAILED})

    duplicate_result = compare_eval_suite_results(baseline, duplicate)
    inconsistent_result = compare_eval_suite_results(baseline, inconsistent)

    assert duplicate_result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert duplicate_result.code == "duplicate_case_id"
    assert inconsistent_result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert inconsistent_result.code == "result_status_inconsistent"


def test_renderer_separates_verdict_from_eval_infrastructure_error() -> None:
    result = compare_eval_suite_results(
        _suite(
            _identity(commit="1"),
            (_case("case", EvalCaseStatus.PASSED),),
        ),
        _suite(
            _identity(commit="2"),
            (_case("case", EvalCaseStatus.EVALUATION_ERROR),),
        ),
    )

    rendered = render_eval_suite_comparison(result)

    assert "无法判断" in rendered
    assert "评测基础设施" in rendered
    assert "产品回归" not in rendered
    assert "状态变化待复核" in rendered
    assert "Case：回归" not in rendered
    assert len(rendered) < 2_500


def test_regression_candidate_is_not_final_when_another_case_is_unstable() -> None:
    baseline = _suite(
        _identity(commit="1"),
        (
            _case("product", EvalCaseStatus.PASSED),
            _case("fixture", EvalCaseStatus.PASSED),
        ),
    )
    current = _suite(
        _identity(commit="2"),
        (
            _case("product", EvalCaseStatus.IMPLEMENTATION_FAILURE),
            _case("fixture", EvalCaseStatus.EVALUATION_ERROR),
        ),
    )

    result = compare_eval_suite_results(baseline, current)

    assert result.verdict is EvalMechanicalVerdict.INCONCLUSIVE
    assert result.regression_count == 1
    assert "Case：回归" not in render_eval_suite_comparison(result)
