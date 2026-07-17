"""Mechanical case and metric comparison for compatible Harness eval suites."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from naumi_agent.harness.eval_compare import (
    EvalIdentityComparison,
    EvalIdentityComparisonStatus,
    compare_eval_identities,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalSuiteResult,
)


class EvalMechanicalVerdict(StrEnum):
    UNCHANGED = "unchanged"
    IMPROVED = "improved"
    REGRESSED = "regressed"
    INCONCLUSIVE = "inconclusive"
    INCOMPATIBLE = "incompatible"


class EvalCaseTransitionKind(StrEnum):
    UNCHANGED_PASS = "unchanged_pass"
    UNCHANGED_IMPLEMENTATION_FAILURE = "unchanged_implementation_failure"
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"
    EVALUATION_INSTABILITY = "evaluation_instability"


@dataclass(frozen=True, slots=True)
class EvalCaseTransition:
    case_id: str
    runner: str
    baseline_status: EvalCaseStatus
    current_status: EvalCaseStatus
    kind: EvalCaseTransitionKind


MetricName = Literal[
    "cases",
    "passed",
    "implementation_failures",
    "evaluation_errors",
    "skipped",
    "pass_rate",
]


@dataclass(frozen=True, slots=True)
class EvalMetricDelta:
    metric: MetricName
    baseline: int | float
    current: int | float
    delta: int | float
    relative_delta: float | None


@dataclass(frozen=True, slots=True)
class EvalSuiteComparison:
    verdict: EvalMechanicalVerdict
    code: str
    identity: EvalIdentityComparison | None
    transitions: tuple[EvalCaseTransition, ...]
    metric_deltas: tuple[EvalMetricDelta, ...]
    regression_count: int
    improvement_count: int


def compare_eval_suite_results(
    baseline: HarnessEvalSuiteResult,
    current: HarnessEvalSuiteResult,
) -> EvalSuiteComparison:
    """Compare two suite results only after identity and structure validation."""
    baseline_identity = baseline.baseline_identity
    current_identity = current.baseline_identity
    if baseline_identity is None or current_identity is None:
        return _terminal(EvalMechanicalVerdict.INCOMPATIBLE, "identity_missing")

    identity = compare_eval_identities(baseline_identity, current_identity)
    if not _result_matches_identity(baseline) or not _result_matches_identity(current):
        return _terminal(
            EvalMechanicalVerdict.INCOMPATIBLE,
            "result_identity_mismatch",
            identity=identity,
        )
    if identity.status is EvalIdentityComparisonStatus.INCOMPATIBLE:
        return _terminal(
            EvalMechanicalVerdict.INCOMPATIBLE,
            "identity_incompatible",
            identity=identity,
        )

    baseline_cases = _case_map(baseline.cases)
    current_cases = _case_map(current.cases)
    if baseline_cases is None or current_cases is None:
        return _terminal(
            EvalMechanicalVerdict.INCONCLUSIVE,
            "duplicate_case_id",
            identity=identity,
        )
    if not baseline_cases or baseline_cases.keys() != current_cases.keys():
        return _terminal(
            EvalMechanicalVerdict.INCONCLUSIVE,
            "case_set_mismatch",
            identity=identity,
        )
    if any(
        baseline_cases[case_id].runner != current_cases[case_id].runner
        for case_id in baseline_cases
    ):
        return _terminal(
            EvalMechanicalVerdict.INCONCLUSIVE,
            "case_runner_mismatch",
            identity=identity,
        )
    if not _run_status_consistent(baseline) or not _run_status_consistent(current):
        return _terminal(
            EvalMechanicalVerdict.INCONCLUSIVE,
            "result_status_inconsistent",
            identity=identity,
        )

    transitions = tuple(
        _transition(baseline_cases[case_id], current_cases[case_id])
        for case_id in sorted(baseline_cases)
    )
    metrics = _metric_deltas(baseline, current)
    regressions = sum(
        item.kind is EvalCaseTransitionKind.REGRESSION for item in transitions
    )
    improvements = sum(
        item.kind is EvalCaseTransitionKind.IMPROVEMENT for item in transitions
    )
    if any(
        item.kind is EvalCaseTransitionKind.EVALUATION_INSTABILITY
        for item in transitions
    ):
        verdict = EvalMechanicalVerdict.INCONCLUSIVE
        code = "evaluation_instability"
    elif regressions:
        verdict = EvalMechanicalVerdict.REGRESSED
        code = ""
    elif improvements:
        verdict = EvalMechanicalVerdict.IMPROVED
        code = ""
    else:
        verdict = EvalMechanicalVerdict.UNCHANGED
        code = ""
    return EvalSuiteComparison(
        verdict=verdict,
        code=code,
        identity=identity,
        transitions=transitions,
        metric_deltas=metrics,
        regression_count=regressions,
        improvement_count=improvements,
    )


def _result_matches_identity(result: HarnessEvalSuiteResult) -> bool:
    identity = result.baseline_identity
    if identity is None or result.baseline_identity_code:
        return False
    configuration = identity.configuration
    return (
        result.suite_id == configuration.suite_id
        and result.suite_sha256 == configuration.suite_sha256
    )


def _case_map(
    cases: tuple[HarnessEvalCaseResult, ...],
) -> dict[str, HarnessEvalCaseResult] | None:
    mapped = {case.case_id: case for case in cases}
    return mapped if len(mapped) == len(cases) else None


def _run_status_consistent(result: HarnessEvalSuiteResult) -> bool:
    if not result.cases:
        return False
    expected = (
        EvalRunStatus.PASSED
        if all(case.status is EvalCaseStatus.PASSED for case in result.cases)
        else EvalRunStatus.FAILED
    )
    return result.status is expected


def _transition(
    baseline: HarnessEvalCaseResult,
    current: HarnessEvalCaseResult,
) -> EvalCaseTransition:
    unstable = {EvalCaseStatus.EVALUATION_ERROR, EvalCaseStatus.SKIPPED}
    if baseline.status in unstable or current.status in unstable:
        kind = EvalCaseTransitionKind.EVALUATION_INSTABILITY
    elif (
        baseline.status is EvalCaseStatus.PASSED
        and current.status is EvalCaseStatus.IMPLEMENTATION_FAILURE
    ):
        kind = EvalCaseTransitionKind.REGRESSION
    elif (
        baseline.status is EvalCaseStatus.IMPLEMENTATION_FAILURE
        and current.status is EvalCaseStatus.PASSED
    ):
        kind = EvalCaseTransitionKind.IMPROVEMENT
    elif baseline.status is EvalCaseStatus.PASSED:
        kind = EvalCaseTransitionKind.UNCHANGED_PASS
    else:
        kind = EvalCaseTransitionKind.UNCHANGED_IMPLEMENTATION_FAILURE
    return EvalCaseTransition(
        case_id=baseline.case_id,
        runner=baseline.runner,
        baseline_status=baseline.status,
        current_status=current.status,
        kind=kind,
    )


def _metric_deltas(
    baseline: HarnessEvalSuiteResult,
    current: HarnessEvalSuiteResult,
) -> tuple[EvalMetricDelta, ...]:
    baseline_values = _metrics(baseline)
    current_values = _metrics(current)
    order: tuple[MetricName, ...] = (
        "cases",
        "passed",
        "implementation_failures",
        "evaluation_errors",
        "skipped",
        "pass_rate",
    )
    return tuple(
        _metric_delta(metric, baseline_values[metric], current_values[metric])
        for metric in order
    )


def _metrics(result: HarnessEvalSuiteResult) -> dict[MetricName, int | float]:
    cases = len(result.cases)
    return {
        "cases": cases,
        "passed": result.passed,
        "implementation_failures": result.implementation_failures,
        "evaluation_errors": result.evaluation_errors,
        "skipped": result.skipped,
        "pass_rate": result.passed / cases if cases else 0.0,
    }


def _metric_delta(
    metric: MetricName,
    baseline: int | float,
    current: int | float,
) -> EvalMetricDelta:
    delta = current - baseline
    relative = float(delta / abs(baseline)) if baseline != 0 else None
    return EvalMetricDelta(
        metric=metric,
        baseline=baseline,
        current=current,
        delta=delta,
        relative_delta=relative,
    )


def _terminal(
    verdict: EvalMechanicalVerdict,
    code: str,
    *,
    identity: EvalIdentityComparison | None = None,
) -> EvalSuiteComparison:
    return EvalSuiteComparison(
        verdict=verdict,
        code=code,
        identity=identity,
        transitions=(),
        metric_deltas=(),
        regression_count=0,
        improvement_count=0,
    )


def render_eval_suite_comparison(result: EvalSuiteComparison) -> str:
    """Render a bounded mechanical verdict without overstating unstable evals."""
    verdict = {
        EvalMechanicalVerdict.UNCHANGED: "无变化",
        EvalMechanicalVerdict.IMPROVED: "改善",
        EvalMechanicalVerdict.REGRESSED: "回归",
        EvalMechanicalVerdict.INCONCLUSIVE: "无法判断",
        EvalMechanicalVerdict.INCOMPATIBLE: "不可比较",
    }[result.verdict]
    lines = ["## Harness Eval 机械比较", "", f"- 结论：{verdict}"]
    if result.code:
        lines.append(f"- 原因：{_CODE_MESSAGES.get(result.code, result.code)}")
    if result.identity is not None:
        lines.append(
            f"- Identity：`{result.identity.baseline_identity_sha256[:12]}` → "
            f"`{result.identity.current_identity_sha256[:12]}`"
        )
    if result.transitions:
        if result.verdict is EvalMechanicalVerdict.INCONCLUSIVE:
            lines.append(f"- Case：状态变化待复核 · 总计 {len(result.transitions)}")
        else:
            lines.append(
                f"- Case：回归 {result.regression_count} · "
                f"改善 {result.improvement_count} · 总计 {len(result.transitions)}"
            )
        for item in result.transitions:
            if item.kind in {
                EvalCaseTransitionKind.REGRESSION,
                EvalCaseTransitionKind.IMPROVEMENT,
                EvalCaseTransitionKind.EVALUATION_INSTABILITY,
            }:
                lines.append(
                    f"  - `{item.case_id}`：{item.baseline_status} → {item.current_status}"
                )
    return "\n".join(lines)


_CODE_MESSAGES = {
    "identity_missing": "一侧缺少可验证 Baseline Identity。",
    "identity_incompatible": "Identity gate 已拒绝这组结果。",
    "result_identity_mismatch": "Suite Result 与其 Identity 绑定不一致。",
    "duplicate_case_id": "Result 中存在重复 case ID。",
    "case_set_mismatch": "Baseline 与当前的 case 集合不同。",
    "case_runner_mismatch": "同一 case 使用了不同 Runner。",
    "result_status_inconsistent": "Suite 汇总状态与 case 状态不一致。",
    "evaluation_instability": "存在评测基础设施错误或跳过，无法形成产品结论。",
}


__all__ = [
    "EvalCaseTransition",
    "EvalCaseTransitionKind",
    "EvalMechanicalVerdict",
    "EvalMetricDelta",
    "EvalSuiteComparison",
    "compare_eval_suite_results",
    "render_eval_suite_comparison",
]
