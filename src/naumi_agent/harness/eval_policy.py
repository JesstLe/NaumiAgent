"""Threshold and guardrail policy evaluation for Harness suite comparisons."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from naumi_agent.harness.eval_models import (
    EvalGuardrailStatus,
    HarnessEvalCaseResult,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_suite_compare import (
    EvalMechanicalVerdict,
    EvalSuiteComparison,
    compare_eval_suite_results,
)


class EvalPolicyVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class EvalPolicyViolation:
    code: str
    scope: Literal["absolute", "relative", "guardrail"]
    metric: str
    observed: int | float | str
    threshold: int | float | str
    case_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvalPolicyEvaluation:
    verdict: EvalPolicyVerdict
    code: str
    mechanical: EvalSuiteComparison
    violations: tuple[EvalPolicyViolation, ...]


def evaluate_eval_policy(
    baseline: HarnessEvalSuiteResult,
    current: HarnessEvalSuiteResult,
) -> EvalPolicyEvaluation:
    """Apply the identity-bound current policy after mechanical comparison gates."""
    mechanical = compare_eval_suite_results(baseline, current)
    if mechanical.verdict is EvalMechanicalVerdict.INCOMPATIBLE:
        return _terminal(EvalPolicyVerdict.INCOMPATIBLE, "mechanical_incompatible", mechanical)
    if mechanical.verdict is EvalMechanicalVerdict.INCONCLUSIVE:
        return _terminal(EvalPolicyVerdict.INCONCLUSIVE, "mechanical_inconclusive", mechanical)

    baseline_evidence = _evidence_problem(baseline, baseline=True)
    if baseline_evidence is not None:
        return _terminal(EvalPolicyVerdict.INCOMPATIBLE, baseline_evidence, mechanical)
    current_evidence = _evidence_problem(current, baseline=False)
    if current_evidence is not None:
        return _terminal(EvalPolicyVerdict.INCONCLUSIVE, current_evidence, mechanical)

    policy = current.comparison_policy
    metrics = {item.metric: item for item in mechanical.metric_deltas}
    violations: list[EvalPolicyViolation] = []
    _minimum(
        violations,
        code="min_pass_rate_violated",
        metric="pass_rate",
        observed=metrics["pass_rate"].current,
        threshold=policy.min_pass_rate,
    )
    _maximum(
        violations,
        code="max_implementation_failures_violated",
        metric="implementation_failures",
        observed=metrics["implementation_failures"].current,
        threshold=policy.max_implementation_failures,
    )
    _maximum(
        violations,
        code="max_regressions_violated",
        metric="regressions",
        observed=mechanical.regression_count,
        threshold=policy.max_regressions,
        scope="relative",
    )
    pass_rate_drop = max(
        0.0,
        float(metrics["pass_rate"].baseline - metrics["pass_rate"].current),
    )
    _maximum(
        violations,
        code="max_pass_rate_drop_violated",
        metric="pass_rate_drop",
        observed=pass_rate_drop,
        threshold=policy.max_pass_rate_drop,
        scope="relative",
    )
    violations.extend(_guardrail_violations(current))
    return EvalPolicyEvaluation(
        verdict=(EvalPolicyVerdict.FAILED if violations else EvalPolicyVerdict.PASSED),
        code="",
        mechanical=mechanical,
        violations=tuple(violations),
    )


def _evidence_problem(
    result: HarnessEvalSuiteResult,
    *,
    baseline: bool,
) -> str | None:
    for case in result.cases:
        if case.runner == "protocol_hello" and not case.primary_metric:
            return "baseline_primary_metric_missing" if baseline else "primary_metric_missing"
        required = _required_guardrails(case)
        evidence = {item.guardrail: item for item in case.guardrails}
        if not required.issubset(evidence):
            return (
                "baseline_guardrail_evidence_missing"
                if baseline
                else "guardrail_evidence_missing"
            )
        if any(evidence[name].status is EvalGuardrailStatus.UNVERIFIED for name in required):
            return "baseline_guardrail_unverified" if baseline else "guardrail_unverified"
        if baseline and any(
            evidence[name].status is EvalGuardrailStatus.FAILED for name in required
        ):
            return "baseline_guardrail_failed"
    return None


def _required_guardrails(case: HarnessEvalCaseResult) -> set[str]:
    if case.runner == "protocol_hello":
        return {"no_model", "no_side_effect"}
    return set()


def _guardrail_violations(
    current: HarnessEvalSuiteResult,
) -> list[EvalPolicyViolation]:
    violations: list[EvalPolicyViolation] = []
    identity = current.baseline_identity
    assert identity is not None
    for case in current.cases:
        for evidence in case.guardrails:
            failed = evidence.status is EvalGuardrailStatus.FAILED
            if evidence.guardrail == "no_model" and identity.model is not None:
                failed = True
            if failed:
                observed = evidence.status.value
                if evidence.guardrail == "no_model" and identity.model is not None:
                    observed = "model_present"
                violations.append(
                    EvalPolicyViolation(
                        code="guardrail_failed",
                        scope="guardrail",
                        metric=evidence.guardrail,
                        observed=observed,
                        threshold=EvalGuardrailStatus.PASSED.value,
                        case_id=case.case_id,
                    )
                )
    return violations


def _minimum(
    violations: list[EvalPolicyViolation],
    *,
    code: str,
    metric: str,
    observed: int | float,
    threshold: int | float,
) -> None:
    if observed < threshold and not _numerically_equal(observed, threshold):
        violations.append(
            EvalPolicyViolation(
                code=code,
                scope="absolute",
                metric=metric,
                observed=observed,
                threshold=threshold,
            )
        )


def _maximum(
    violations: list[EvalPolicyViolation],
    *,
    code: str,
    metric: str,
    observed: int | float,
    threshold: int | float,
    scope: Literal["absolute", "relative"] = "absolute",
) -> None:
    if observed > threshold and not _numerically_equal(observed, threshold):
        violations.append(
            EvalPolicyViolation(
                code=code,
                scope=scope,
                metric=metric,
                observed=observed,
                threshold=threshold,
            )
        )


def _terminal(
    verdict: EvalPolicyVerdict,
    code: str,
    mechanical: EvalSuiteComparison,
) -> EvalPolicyEvaluation:
    return EvalPolicyEvaluation(
        verdict=verdict,
        code=code,
        mechanical=mechanical,
        violations=(),
    )


def render_eval_policy_evaluation(result: EvalPolicyEvaluation) -> str:
    """Render a compact policy verdict distinct from mechanical state changes."""
    verdict = {
        EvalPolicyVerdict.PASSED: "通过",
        EvalPolicyVerdict.FAILED: "未通过",
        EvalPolicyVerdict.INCONCLUSIVE: "无法判断",
        EvalPolicyVerdict.INCOMPATIBLE: "不可比较",
    }[result.verdict]
    lines = ["## Harness Eval Policy", "", f"- Policy：{verdict}"]
    mechanical = {
        EvalMechanicalVerdict.UNCHANGED: "无变化",
        EvalMechanicalVerdict.IMPROVED: "改善",
        EvalMechanicalVerdict.REGRESSED: "回归",
        EvalMechanicalVerdict.INCONCLUSIVE: "无法判断",
        EvalMechanicalVerdict.INCOMPATIBLE: "不可比较",
    }[result.mechanical.verdict]
    if (
        result.verdict is EvalPolicyVerdict.PASSED
        and result.mechanical.verdict is EvalMechanicalVerdict.REGRESSED
    ):
        mechanical += "（门槛允许）"
    lines.append(f"- 机械变化：{mechanical}")
    if result.code:
        lines.append(f"- 原因：{_CODE_MESSAGES.get(result.code, result.code)}")
    if result.violations:
        lines.append(f"- 违反门槛：{len(result.violations)}")
        for violation in result.violations[:50]:
            lines.append(
                f"  - {_VIOLATION_MESSAGES.get(violation.code, violation.code)}："
                f"{violation.observed}（门槛 {violation.threshold}）"
            )
    return "\n".join(lines)


def _numerically_equal(left: int | float, right: int | float) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


_CODE_MESSAGES = {
    "mechanical_incompatible": "Identity 或 Result 完整性 gate 未通过。",
    "mechanical_inconclusive": "机械比较存在评测错误、跳过或结构异常。",
    "primary_metric_missing": "当前 case 缺少 primary metric 证据。",
    "guardrail_evidence_missing": "当前 case 缺少必要 guardrail evidence。",
    "guardrail_unverified": "当前 guardrail 尚未验证。",
    "baseline_primary_metric_missing": "Baseline 缺少 primary metric 证据。",
    "baseline_guardrail_evidence_missing": "Baseline 缺少必要 guardrail evidence。",
    "baseline_guardrail_unverified": "Baseline guardrail 尚未验证。",
    "baseline_guardrail_failed": "Baseline guardrail 未通过，不能作为比较基准。",
}

_VIOLATION_MESSAGES = {
    "min_pass_rate_violated": "通过率低于下限",
    "max_implementation_failures_violated": "实现失败数超过上限",
    "max_regressions_violated": "新增回归超过上限",
    "max_pass_rate_drop_violated": "通过率下降超过上限",
    "guardrail_failed": "Guardrail 未通过",
}


__all__ = [
    "EvalPolicyEvaluation",
    "EvalPolicyVerdict",
    "EvalPolicyViolation",
    "evaluate_eval_policy",
    "render_eval_policy_evaluation",
]
