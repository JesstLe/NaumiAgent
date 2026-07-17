"""Repeated-sample statistics for compatible Harness Eval suite results."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from naumi_agent.harness.eval_models import HarnessEvalSuiteResult
from naumi_agent.harness.eval_suite_compare import (
    EvalMechanicalVerdict,
    compare_eval_suite_results,
)

_MIN_SAMPLES = 5
_MAX_SAMPLES = 10_000


class EvalStatisticalVerdict(StrEnum):
    UNCHANGED = "unchanged"
    IMPROVED = "improved"
    REGRESSED = "regressed"
    FLAKY = "flaky"
    INCONCLUSIVE = "inconclusive"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class EvalSampleSummary:
    metric: Literal["pass_rate", "duration_ms"]
    samples: int
    mean: float
    standard_deviation: float
    confidence_low: float
    confidence_high: float


@dataclass(frozen=True, slots=True)
class EvalMeanDifference:
    metric: Literal["pass_rate", "duration_ms"]
    baseline_mean: float
    current_mean: float
    delta: float
    confidence_low: float
    confidence_high: float


@dataclass(frozen=True, slots=True)
class EvalFlakyCase:
    case_id: str
    cohort: Literal["baseline", "current"]
    observed_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalStatisticalComparison:
    verdict: EvalStatisticalVerdict
    code: str
    baseline_samples: int
    current_samples: int
    baseline: tuple[EvalSampleSummary, ...]
    current: tuple[EvalSampleSummary, ...]
    differences: tuple[EvalMeanDifference, ...]
    flaky_cases: tuple[EvalFlakyCase, ...]


def compare_eval_repetitions(
    baseline_runs: Sequence[HarnessEvalSuiteResult],
    current_runs: Sequence[HarnessEvalSuiteResult],
    *,
    minimum_samples: int = _MIN_SAMPLES,
) -> EvalStatisticalComparison:
    """Compare repeated compatible suites without overstating noisy evidence."""
    baseline = tuple(baseline_runs)
    current = tuple(current_runs)
    if not _MIN_SAMPLES <= minimum_samples <= _MAX_SAMPLES:
        raise ValueError("minimum_samples 必须在 5..10000 之间")
    if len(baseline) > _MAX_SAMPLES or len(current) > _MAX_SAMPLES:
        return _terminal(
            EvalStatisticalVerdict.INCOMPATIBLE,
            "sample_count_exceeds_limit",
            baseline,
            current,
        )
    if len(baseline) < minimum_samples or len(current) < minimum_samples:
        return _terminal(
            EvalStatisticalVerdict.INCONCLUSIVE,
            "sample_count_insufficient",
            baseline,
            current,
        )

    identity_problem = _identity_sample_problem(baseline, current)
    if identity_problem:
        return _terminal(
            EvalStatisticalVerdict.INCOMPATIBLE,
            identity_problem,
            baseline,
            current,
        )
    evidence_problem = _comparison_problem(baseline, current)
    if evidence_problem:
        verdict, code = evidence_problem
        return _terminal(verdict, code, baseline, current)

    flaky_cases = (
        *_flaky_cases(baseline, cohort="baseline"),
        *_flaky_cases(current, cohort="current"),
    )
    baseline_summaries = _summaries(baseline)
    current_summaries = _summaries(current)
    differences = tuple(
        _mean_difference(left, right)
        for left, right in zip(
            baseline_summaries,
            current_summaries,
            strict=True,
        )
    )
    if flaky_cases:
        verdict = EvalStatisticalVerdict.FLAKY
        code = "case_status_flaky"
    else:
        pass_rate = next(
            item for item in differences if item.metric == "pass_rate"
        )
        if pass_rate.confidence_low > 0:
            verdict = EvalStatisticalVerdict.IMPROVED
            code = ""
        elif pass_rate.confidence_high < 0:
            verdict = EvalStatisticalVerdict.REGRESSED
            code = ""
        elif math.isclose(pass_rate.delta, 0.0, abs_tol=1e-12):
            verdict = EvalStatisticalVerdict.UNCHANGED
            code = ""
        else:
            verdict = EvalStatisticalVerdict.INCONCLUSIVE
            code = "confidence_interval_overlaps_zero"
    return EvalStatisticalComparison(
        verdict=verdict,
        code=code,
        baseline_samples=len(baseline),
        current_samples=len(current),
        baseline=baseline_summaries,
        current=current_summaries,
        differences=differences,
        flaky_cases=tuple(flaky_cases),
    )


def _identity_sample_problem(
    baseline: tuple[HarnessEvalSuiteResult, ...],
    current: tuple[HarnessEvalSuiteResult, ...],
) -> str:
    for runs in (baseline, current):
        for run in runs:
            identity = run.baseline_identity
            if identity is None:
                return "identity_missing"
            if identity.configuration.repetitions != len(runs):
                return "sample_count_identity_mismatch"
    return ""


def _comparison_problem(
    baseline: tuple[HarnessEvalSuiteResult, ...],
    current: tuple[HarnessEvalSuiteResult, ...],
) -> tuple[EvalStatisticalVerdict, str] | None:
    reference = baseline[0]
    for run in (*baseline[1:], *current):
        comparison = compare_eval_suite_results(reference, run)
        if comparison.verdict is EvalMechanicalVerdict.INCOMPATIBLE:
            return EvalStatisticalVerdict.INCOMPATIBLE, comparison.code
        if comparison.verdict is EvalMechanicalVerdict.INCONCLUSIVE:
            return EvalStatisticalVerdict.INCONCLUSIVE, comparison.code
    return None


def _flaky_cases(
    runs: tuple[HarnessEvalSuiteResult, ...],
    *,
    cohort: Literal["baseline", "current"],
) -> tuple[EvalFlakyCase, ...]:
    case_ids = sorted(case.case_id for case in runs[0].cases)
    status_maps = [
        {case.case_id: case.status.value for case in run.cases}
        for run in runs
    ]
    return tuple(
        EvalFlakyCase(
            case_id=case_id,
            cohort=cohort,
            observed_statuses=tuple(
                sorted({statuses[case_id] for statuses in status_maps})
            ),
        )
        for case_id in case_ids
        if len({statuses[case_id] for statuses in status_maps}) > 1
    )


def _summaries(
    runs: tuple[HarnessEvalSuiteResult, ...],
) -> tuple[EvalSampleSummary, ...]:
    values = {
        "pass_rate": tuple(run.passed / len(run.cases) for run in runs),
        "duration_ms": tuple(float(run.duration_ms) for run in runs),
    }
    return tuple(
        _sample_summary(metric, samples)
        for metric, samples in values.items()
    )


def _sample_summary(
    metric: Literal["pass_rate", "duration_ms"],
    values: tuple[float, ...],
) -> EvalSampleSummary:
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values)
    margin = _t_critical(len(values) - 1) * deviation / math.sqrt(len(values))
    low = mean - margin
    high = mean + margin
    if metric == "pass_rate":
        low = max(0.0, low)
        high = min(1.0, high)
    return EvalSampleSummary(
        metric=metric,
        samples=len(values),
        mean=mean,
        standard_deviation=deviation,
        confidence_low=low,
        confidence_high=high,
    )


def _mean_difference(
    baseline: EvalSampleSummary,
    current: EvalSampleSummary,
) -> EvalMeanDifference:
    baseline_variance = baseline.standard_deviation**2 / baseline.samples
    current_variance = current.standard_deviation**2 / current.samples
    standard_error = math.sqrt(baseline_variance + current_variance)
    delta = current.mean - baseline.mean
    if standard_error == 0:
        margin = 0.0
    else:
        numerator = (baseline_variance + current_variance) ** 2
        denominator = (
            baseline_variance**2 / (baseline.samples - 1)
            + current_variance**2 / (current.samples - 1)
        )
        degrees_of_freedom = numerator / denominator if denominator else 1.0
        margin = _t_critical(max(1, int(degrees_of_freedom))) * standard_error
    return EvalMeanDifference(
        metric=baseline.metric,
        baseline_mean=baseline.mean,
        current_mean=current.mean,
        delta=delta,
        confidence_low=delta - margin,
        confidence_high=delta + margin,
    )


def _t_critical(degrees_of_freedom: int) -> float:
    """Two-sided 95% Student-t critical value, conservatively bucketed."""
    table = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
        12: 2.179,
        15: 2.131,
        20: 2.086,
        30: 2.042,
        60: 2.000,
        120: 1.980,
    }
    eligible = [value for value in table if value <= degrees_of_freedom]
    if eligible:
        return table[max(eligible)]
    return 1.960


def _terminal(
    verdict: EvalStatisticalVerdict,
    code: str,
    baseline: Sequence[HarnessEvalSuiteResult],
    current: Sequence[HarnessEvalSuiteResult],
) -> EvalStatisticalComparison:
    return EvalStatisticalComparison(
        verdict=verdict,
        code=code,
        baseline_samples=len(baseline),
        current_samples=len(current),
        baseline=(),
        current=(),
        differences=(),
        flaky_cases=(),
    )


def render_eval_statistical_comparison(result: EvalStatisticalComparison) -> str:
    """Render bounded statistical evidence and explicit uncertainty."""
    verdict = {
        EvalStatisticalVerdict.UNCHANGED: "无显著变化",
        EvalStatisticalVerdict.IMPROVED: "统计改善",
        EvalStatisticalVerdict.REGRESSED: "统计回归",
        EvalStatisticalVerdict.FLAKY: "存在波动",
        EvalStatisticalVerdict.INCONCLUSIVE: "无法判断",
        EvalStatisticalVerdict.INCOMPATIBLE: "不可比较",
    }[result.verdict]
    lines = [
        "## Harness Eval 统计比较",
        "",
        f"- 结论：{verdict}",
        f"- 样本：Baseline {result.baseline_samples} · Current {result.current_samples}",
    ]
    if result.code:
        lines.append(f"- 原因：{_CODE_MESSAGES.get(result.code, result.code)}")
    for difference in result.differences:
        label = "通过率" if difference.metric == "pass_rate" else "耗时 ms"
        lines.append(
            f"- {label}：{difference.baseline_mean:.4f} → "
            f"{difference.current_mean:.4f} · Δ {difference.delta:+.4f} · "
            f"95% CI [{difference.confidence_low:+.4f}, "
            f"{difference.confidence_high:+.4f}]"
        )
    for item in result.flaky_cases[:50]:
        lines.append(
            f"  - `{item.case_id}`（{item.cohort}）："
            f"{', '.join(item.observed_statuses)}"
        )
    return "\n".join(lines)


_CODE_MESSAGES = {
    "sample_count_insufficient": "每组至少需要 5 次重复样本。",
    "sample_count_exceeds_limit": "重复样本超过 10000 次安全上限。",
    "sample_count_identity_mismatch": "Identity repetitions 与实际样本数不一致。",
    "identity_missing": "样本缺少可验证 Baseline Identity。",
    "case_status_flaky": "同一 case 在重复运行中出现不同结果。",
    "confidence_interval_overlaps_zero": "均值有变化，但置信区间仍跨越零。",
    "evaluation_instability": "存在评测错误或跳过，不能形成产品统计结论。",
}


__all__ = [
    "EvalFlakyCase",
    "EvalMeanDifference",
    "EvalSampleSummary",
    "EvalStatisticalComparison",
    "EvalStatisticalVerdict",
    "compare_eval_repetitions",
    "render_eval_statistical_comparison",
]
