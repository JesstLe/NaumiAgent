"""Shared mechanical metrics for release stage-completion evidence."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutCostSource,
)


class EvolutionRevalidationStageCompletionStatus(StrEnum):
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"


class EvolutionRevalidationStageCompletionMetrics(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    observed_runs: int = Field(ge=0, le=100)
    minimum_completed_runs: int = Field(ge=10, le=100)
    successful_runs: int = Field(ge=0, le=100)
    unsuccessful_runs: int = Field(ge=0, le=100)
    error_rate_basis_points: int = Field(ge=0, le=10_000)
    max_error_rate_basis_points: int = Field(ge=0, le=10_000)
    completion_rate_basis_points: int = Field(ge=0, le=10_000)
    baseline_completion_rate_basis_points: int = Field(ge=0, le=10_000)
    completion_rate_drop_basis_points: int = Field(ge=0, le=10_000)
    max_completion_rate_drop_basis_points: int = Field(ge=0, le=10_000)
    p95_duration_micros: int = Field(ge=0)
    baseline_p95_duration_micros: int = Field(ge=0)
    p95_latency_regression_basis_points: int = Field(ge=0, le=10_000)
    max_p95_latency_regression_basis_points: int = Field(ge=0, le=10_000)
    mean_reported_cost_microusd: int = Field(ge=0)
    baseline_mean_cost_microusd: int = Field(ge=0)
    baseline_cost_source: EvolutionRevalidationRolloutCostSource
    cost_comparable: bool
    cost_regression_basis_points: int = Field(ge=0, le=10_000)
    max_cost_regression_basis_points: int = Field(ge=0, le=10_000)
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    status: EvolutionRevalidationStageCompletionStatus

    @model_validator(mode="after")
    def _exact(self) -> Self:
        insufficient, breaches = _reasons(
            observed_runs=self.observed_runs,
            minimum_completed_runs=self.minimum_completed_runs,
            cost_comparable=self.cost_comparable,
            error_rate_basis_points=self.error_rate_basis_points,
            max_error_rate_basis_points=self.max_error_rate_basis_points,
            completion_rate_drop_basis_points=self.completion_rate_drop_basis_points,
            max_completion_rate_drop_basis_points=(
                self.max_completion_rate_drop_basis_points
            ),
            p95_latency_regression_basis_points=(
                self.p95_latency_regression_basis_points
            ),
            max_p95_latency_regression_basis_points=(
                self.max_p95_latency_regression_basis_points
            ),
            cost_regression_basis_points=self.cost_regression_basis_points,
            max_cost_regression_basis_points=self.max_cost_regression_basis_points,
        )
        expected_status = _status(insufficient, breaches)
        if not (
            self.unsuccessful_runs == self.observed_runs - self.successful_runs
            and self.error_rate_basis_points
            == _basis_points(self.unsuccessful_runs, self.observed_runs)
            and self.completion_rate_basis_points
            == _basis_points(self.successful_runs, self.observed_runs)
            and self.completion_rate_drop_basis_points
            == max(
                0,
                self.baseline_completion_rate_basis_points
                - self.completion_rate_basis_points,
            )
            and self.cost_comparable
            is (
                self.baseline_cost_source
                is EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE
            )
            and self.insufficient_reasons == (() if breaches else insufficient)
            and self.breach_reasons == breaches
            and self.status is expected_status
        ):
            raise ValueError("Stage Completion metric/status projection 不一致。")
        return self


def calculate_stage_completion_metrics(
    *,
    durations_micros: tuple[int, ...],
    reported_cost_microusd: tuple[int, ...],
    successful_runs: int,
    minimum_completed_runs: int,
    max_error_rate_basis_points: int,
    max_completion_rate_drop_basis_points: int,
    max_p95_latency_regression_basis_points: int,
    max_cost_regression_basis_points: int,
    baseline_completion_rate_basis_points: int,
    baseline_p95_duration_micros: int,
    baseline_mean_cost_microusd: int,
    baseline_cost_source: EvolutionRevalidationRolloutCostSource,
) -> EvolutionRevalidationStageCompletionMetrics:
    observed = len(durations_micros)
    if len(reported_cost_microusd) != observed or not 0 <= successful_runs <= observed:
        raise ValueError("Stage Completion run metric source 数量不一致。")
    if any(value < 0 for value in (*durations_micros, *reported_cost_microusd)):
        raise ValueError("Stage Completion run metric 不得为负数。")
    unsuccessful = observed - successful_runs
    error_rate = _basis_points(unsuccessful, observed)
    completion_rate = _basis_points(successful_runs, observed)
    p95 = _p95(durations_micros)
    mean_cost = _rounded_div(sum(reported_cost_microusd), observed)
    cost_comparable = (
        baseline_cost_source is EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE
    )
    payload = {
        "observed_runs": observed,
        "minimum_completed_runs": minimum_completed_runs,
        "successful_runs": successful_runs,
        "unsuccessful_runs": unsuccessful,
        "error_rate_basis_points": error_rate,
        "max_error_rate_basis_points": max_error_rate_basis_points,
        "completion_rate_basis_points": completion_rate,
        "baseline_completion_rate_basis_points": baseline_completion_rate_basis_points,
        "completion_rate_drop_basis_points": max(
            0,
            baseline_completion_rate_basis_points - completion_rate,
        ),
        "max_completion_rate_drop_basis_points": (
            max_completion_rate_drop_basis_points
        ),
        "p95_duration_micros": p95,
        "baseline_p95_duration_micros": baseline_p95_duration_micros,
        "p95_latency_regression_basis_points": _regression(
            p95,
            baseline_p95_duration_micros,
        ),
        "max_p95_latency_regression_basis_points": (
            max_p95_latency_regression_basis_points
        ),
        "mean_reported_cost_microusd": mean_cost,
        "baseline_mean_cost_microusd": baseline_mean_cost_microusd,
        "baseline_cost_source": baseline_cost_source,
        "cost_comparable": cost_comparable,
        "cost_regression_basis_points": (
            _regression(mean_cost, baseline_mean_cost_microusd)
            if cost_comparable
            else 0
        ),
        "max_cost_regression_basis_points": max_cost_regression_basis_points,
    }
    insufficient, breaches = _reasons(**payload)
    return EvolutionRevalidationStageCompletionMetrics(
        **payload,
        insufficient_reasons=() if breaches else insufficient,
        breach_reasons=breaches,
        status=_status(insufficient, breaches),
    )


def _reasons(**values) -> tuple[tuple[str, ...], tuple[str, ...]]:
    observed = values["observed_runs"]
    insufficient = tuple(
        reason
        for condition, reason in (
            (
                observed < values["minimum_completed_runs"],
                "minimum_completed_runs",
            ),
            (not values["cost_comparable"], "cost_baseline_not_comparable"),
        )
        if condition
    )
    breaches = tuple(
        reason
        for condition, reason in (
            (
                observed > 0
                and values["error_rate_basis_points"]
                > values["max_error_rate_basis_points"],
                "error_rate",
            ),
            (
                observed > 0
                and values["completion_rate_drop_basis_points"]
                > values["max_completion_rate_drop_basis_points"],
                "completion_rate_drop",
            ),
            (
                observed > 0
                and values["p95_latency_regression_basis_points"]
                > values["max_p95_latency_regression_basis_points"],
                "p95_latency_regression",
            ),
            (
                observed > 0
                and values["cost_comparable"]
                and values["cost_regression_basis_points"]
                > values["max_cost_regression_basis_points"],
                "cost_regression",
            ),
        )
        if condition
    )
    return insufficient, breaches


def _status(insufficient, breaches) -> EvolutionRevalidationStageCompletionStatus:
    if breaches:
        return EvolutionRevalidationStageCompletionStatus.BREACHED
    if insufficient:
        return EvolutionRevalidationStageCompletionStatus.INSUFFICIENT
    return EvolutionRevalidationStageCompletionStatus.PASSING


def _basis_points(numerator: int, denominator: int) -> int:
    return 0 if denominator == 0 else (numerator * 10_000 + denominator // 2) // denominator


def _p95(values: tuple[int, ...]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _regression(current: int, baseline: int) -> int:
    if current <= baseline:
        return 0
    if baseline == 0:
        return 10_000
    return min(10_000, ((current - baseline) * 10_000 + baseline // 2) // baseline)


def _rounded_div(total: int, count: int) -> int:
    return 0 if count == 0 else (total + count // 2) // count


__all__ = [
    "EvolutionRevalidationStageCompletionMetrics",
    "EvolutionRevalidationStageCompletionStatus",
    "calculate_stage_completion_metrics",
]
