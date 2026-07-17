from __future__ import annotations

from pathlib import Path

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalGuardrailResult,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_policy import (
    EvalPolicyVerdict,
    evaluate_eval_policy,
    render_eval_policy_evaluation,
)
from naumi_agent.harness.eval_suite_compare import EvalMechanicalVerdict


def _identity(*, commit: str, policy: HarnessEvalComparisonPolicy):
    return build_eval_baseline_identity(
        Path("."),
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="policy-protocol",
            suite_sha256="a" * 64,
            profile_sha256="b" * 64,
            policy_sha256=policy.sha256,
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


def _guardrails(
    *,
    no_model: EvalGuardrailStatus = EvalGuardrailStatus.PASSED,
    no_side_effect: EvalGuardrailStatus = EvalGuardrailStatus.PASSED,
) -> tuple[HarnessEvalGuardrailResult, ...]:
    return (
        HarnessEvalGuardrailResult(guardrail="no_model", status=no_model),
        HarnessEvalGuardrailResult(
            guardrail="no_side_effect",
            status=no_side_effect,
        ),
    )


def _case(
    case_id: str,
    status: EvalCaseStatus,
    *,
    guardrails: tuple[HarnessEvalGuardrailResult, ...] | None = None,
    primary_metric: str = "protocol_outcome_match",
) -> HarnessEvalCaseResult:
    return HarnessEvalCaseResult(
        case_id=case_id,
        runner="protocol_hello",
        status=status,
        primary_metric=primary_metric,
        guardrails=guardrails if guardrails is not None else _guardrails(),
    )


def _suite(
    *,
    commit: str,
    policy: HarnessEvalComparisonPolicy,
    cases: tuple[HarnessEvalCaseResult, ...],
) -> HarnessEvalSuiteResult:
    return HarnessEvalSuiteResult(
        suite_id="policy-protocol",
        title="Policy 协议评测",
        suite_path="evals/policy.yaml",
        suite_sha256="a" * 64,
        status=(
            EvalRunStatus.PASSED
            if all(case.status is EvalCaseStatus.PASSED for case in cases)
            else EvalRunStatus.FAILED
        ),
        cases=cases,
        comparison_policy=policy,
        baseline_identity=_identity(commit=commit, policy=policy),
    )


def test_strict_policy_passes_stable_all_green_suite() -> None:
    policy = HarnessEvalComparisonPolicy()
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )
    current = _suite(
        commit="2",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )

    result = evaluate_eval_policy(baseline, current)

    assert result.verdict is EvalPolicyVerdict.PASSED
    assert result.code == ""
    assert result.violations == ()
    assert result.mechanical.verdict is EvalMechanicalVerdict.UNCHANGED


def test_strict_policy_reports_all_absolute_and_relative_violations() -> None:
    policy = HarnessEvalComparisonPolicy()
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(
            _case("a", EvalCaseStatus.PASSED),
            _case("b", EvalCaseStatus.PASSED),
        ),
    )
    current = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case("a", EvalCaseStatus.IMPLEMENTATION_FAILURE),
            _case("b", EvalCaseStatus.PASSED),
        ),
    )

    result = evaluate_eval_policy(baseline, current)
    codes = {item.code for item in result.violations}

    assert result.verdict is EvalPolicyVerdict.FAILED
    assert result.mechanical.verdict is EvalMechanicalVerdict.REGRESSED
    assert codes == {
        "min_pass_rate_violated",
        "max_implementation_failures_violated",
        "max_regressions_violated",
        "max_pass_rate_drop_violated",
    }


def test_explicit_tolerance_can_accept_bounded_regression() -> None:
    policy = HarnessEvalComparisonPolicy(
        min_pass_rate=0.5,
        max_regressions=1,
        max_implementation_failures=1,
        max_pass_rate_drop=0.5,
    )
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(
            _case("a", EvalCaseStatus.PASSED),
            _case("b", EvalCaseStatus.PASSED),
        ),
    )
    current = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case("a", EvalCaseStatus.IMPLEMENTATION_FAILURE),
            _case("b", EvalCaseStatus.PASSED),
        ),
    )

    result = evaluate_eval_policy(baseline, current)

    assert result.mechanical.verdict is EvalMechanicalVerdict.REGRESSED
    assert result.verdict is EvalPolicyVerdict.PASSED
    assert result.violations == ()
    assert "回归（门槛允许）" in render_eval_policy_evaluation(result)


def test_fractional_threshold_boundary_is_not_failed_by_float_rounding() -> None:
    policy = HarnessEvalComparisonPolicy(
        min_pass_rate=0,
        max_regressions=1,
        max_implementation_failures=1,
        max_pass_rate_drop=1 / 3,
    )
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=tuple(_case(str(index), EvalCaseStatus.PASSED) for index in range(3)),
    )
    current = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case("0", EvalCaseStatus.IMPLEMENTATION_FAILURE),
            _case("1", EvalCaseStatus.PASSED),
            _case("2", EvalCaseStatus.PASSED),
        ),
    )

    result = evaluate_eval_policy(baseline, current)

    assert result.verdict is EvalPolicyVerdict.PASSED
    assert result.violations == ()


def test_failed_guardrail_is_policy_failure_and_unverified_is_inconclusive() -> None:
    policy = HarnessEvalComparisonPolicy()
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )
    failed = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case(
                "a",
                EvalCaseStatus.PASSED,
                guardrails=_guardrails(no_model=EvalGuardrailStatus.FAILED),
            ),
        ),
    )
    unverified = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case(
                "a",
                EvalCaseStatus.PASSED,
                guardrails=_guardrails(
                    no_side_effect=EvalGuardrailStatus.UNVERIFIED
                ),
            ),
        ),
    )

    failed_result = evaluate_eval_policy(baseline, failed)
    unverified_result = evaluate_eval_policy(baseline, unverified)

    assert failed_result.verdict is EvalPolicyVerdict.FAILED
    assert "guardrail_failed" in {item.code for item in failed_result.violations}
    assert unverified_result.verdict is EvalPolicyVerdict.INCONCLUSIVE
    assert unverified_result.code == "guardrail_unverified"


def test_missing_guardrail_or_primary_metric_is_inconclusive() -> None:
    policy = HarnessEvalComparisonPolicy()
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )
    missing_guardrail = _suite(
        commit="2",
        policy=policy,
        cases=(
            _case(
                "a",
                EvalCaseStatus.PASSED,
                guardrails=(
                    HarnessEvalGuardrailResult(
                        guardrail="no_model",
                        status=EvalGuardrailStatus.PASSED,
                    ),
                ),
            ),
        ),
    )
    missing_primary = _suite(
        commit="2",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED, primary_metric=""),),
    )

    assert evaluate_eval_policy(baseline, missing_guardrail).code == (
        "guardrail_evidence_missing"
    )
    assert evaluate_eval_policy(baseline, missing_primary).code == (
        "primary_metric_missing"
    )


def test_policy_change_is_identity_incompatible_not_a_new_verdict() -> None:
    strict = HarnessEvalComparisonPolicy()
    tolerant = HarnessEvalComparisonPolicy(min_pass_rate=0.5)
    baseline = _suite(
        commit="1",
        policy=strict,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )
    current = _suite(
        commit="2",
        policy=tolerant,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )

    result = evaluate_eval_policy(baseline, current)

    assert result.verdict is EvalPolicyVerdict.INCOMPATIBLE
    assert result.code == "mechanical_incompatible"
    assert result.mechanical.identity is not None
    assert "policy_digest_mismatch" in result.mechanical.identity.blocking_codes


def test_eval_instability_propagates_without_threshold_arithmetic() -> None:
    policy = HarnessEvalComparisonPolicy()
    baseline = _suite(
        commit="1",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.PASSED),),
    )
    current = _suite(
        commit="2",
        policy=policy,
        cases=(_case("a", EvalCaseStatus.EVALUATION_ERROR),),
    )

    result = evaluate_eval_policy(baseline, current)

    assert result.verdict is EvalPolicyVerdict.INCONCLUSIVE
    assert result.code == "mechanical_inconclusive"
    assert result.violations == ()


def test_renderer_lists_policy_violations_without_overstating_mechanics() -> None:
    policy = HarnessEvalComparisonPolicy()
    result = evaluate_eval_policy(
        _suite(
            commit="1",
            policy=policy,
            cases=(_case("a", EvalCaseStatus.PASSED),),
        ),
        _suite(
            commit="2",
            policy=policy,
            cases=(_case("a", EvalCaseStatus.IMPLEMENTATION_FAILURE),),
        ),
    )

    rendered = render_eval_policy_evaluation(result)

    assert "Policy：未通过" in rendered
    assert "通过率" in rendered
    assert "新增回归" in rendered
    assert len(rendered) < 3_000
