"""Fail-closed metric runner bindings for evolution baseline cohorts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.self_review import (
    SELF_REVIEW_STATIC_RUNNER_VERSION,
    SelfReviewFindingCode,
)
from naumi_agent.evolution.validation_cohorts import (
    BaselineCohortMetricCase,
    EvolutionBaselineCohortRequest,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_replay import SAFE_REPLAY_EVAL_RUNNER_VERSION

METRIC_RUNNER_BINDING_POLICY = "evolution-metric-runner-binding-v1"
SELF_REVIEW_METRIC_TIMEOUT_SECONDS = 30
_SHA256_RE = r"^[0-9a-f]{64}$"
_SELF_REVIEW_METRIC_RE = re.compile(
    r"^self_review\.([a-z][a-z0-9_]*)\.count$"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class MetricRunnerResolution(_StrictModel):
    verifier: Literal[
        "harness_replay",
        "self_review_static",
        "feedback_recurrence",
    ]
    status: Literal["ready", "blocked"]
    runner_version: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]*@[1-9][0-9]*$",
    )
    timeout_seconds_per_sample: int | None = Field(default=None, ge=1, le=3_600)
    fixture_kind: Literal[
        "validation_paths",
        "harness_replay_baseline",
        "feedback_observation_window",
    ]
    fixture_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    finding_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    blocking_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    model_access: Literal[False] = False
    network_access: Literal[False] = False
    side_effect_free: Literal[True] = True

    @model_validator(mode="after")
    def _state_is_consistent(self) -> Self:
        if self.status == "ready":
            if (
                self.runner_version is None
                or self.timeout_seconds_per_sample is None
                or self.fixture_sha256 is None
                or self.blocking_code is not None
            ):
                raise ValueError("Ready metric runner 必须绑定 runner、timeout 与 fixture。")
        elif self.blocking_code is None:
            raise ValueError("Blocked metric runner 必须提供 blocking_code。")
        if self.verifier == "self_review_static" and self.finding_code is None:
            raise ValueError("Self-review runner 必须绑定 finding code。")
        if self.verifier != "self_review_static" and self.finding_code is not None:
            raise ValueError("非 self-review runner 不得携带 finding code。")
        return self


class MetricRunnerBindingEntry(_StrictModel):
    order: int = Field(ge=1, le=8)
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    direction: Literal["decrease", "increase"]
    target: float
    procedure_sha256: str = Field(pattern=_SHA256_RE)
    resolution: MetricRunnerResolution


class EvolutionMetricRunnerBinding(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-metric-runner-binding-v1"] = (
        METRIC_RUNNER_BINDING_POLICY
    )
    binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    phase: Literal["red"] = "red"
    requested_samples: int = Field(ge=5, le=100)
    entries: tuple[MetricRunnerBindingEntry, ...] = Field(min_length=1, max_length=8)
    binding_status: Literal["ready", "blocked"]
    blocking_codes: tuple[str, ...] = Field(max_length=8)
    profile_timeout_seconds_total: int = Field(ge=1, le=360_000)
    metric_timeout_seconds_total: int = Field(ge=0, le=2_880_000)
    required_duration_seconds: int = Field(ge=1, le=2_883_600)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    budget_headroom_seconds: int = Field(ge=0, le=3_600)
    metric_binding_complete: bool
    arc04_worker_required: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _binding_is_ordered_and_tamper_evident(self) -> Self:
        if tuple(item.order for item in self.entries) != tuple(
            range(1, len(self.entries) + 1)
        ):
            raise ValueError("Metric runner bindings 必须按连续顺序排列。")
        expected_codes = tuple(sorted({
            item.resolution.blocking_code
            for item in self.entries
            if item.resolution.blocking_code is not None
        }))
        if self.blocking_codes != expected_codes:
            raise ValueError("Metric runner blocking codes 汇总不一致。")
        expected_status = "blocked" if expected_codes else "ready"
        if self.binding_status != expected_status:
            raise ValueError("Metric runner binding status 不一致。")
        if self.metric_binding_complete is not (expected_status == "ready"):
            raise ValueError("Metric runner complete 状态不一致。")
        metric_timeout = sum(
            (item.resolution.timeout_seconds_per_sample or 0)
            * self.requested_samples
            for item in self.entries
        )
        if self.metric_timeout_seconds_total != metric_timeout:
            raise ValueError("Metric runner timeout 汇总不一致。")
        required = self.profile_timeout_seconds_total + metric_timeout
        if self.required_duration_seconds != required:
            raise ValueError("Metric runner 总预算汇总不一致。")
        expected_headroom = max(0, self.max_total_duration_seconds - required)
        if self.budget_headroom_seconds != expected_headroom:
            raise ValueError("Metric runner budget headroom 不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"binding_id", "binding_sha256"})
        )
        if not hmac.compare_digest(self.binding_sha256, expected):
            raise ValueError("Metric Runner Binding 摘要不一致。")
        if self.binding_id != f"evvmetric_{expected[:24]}":
            raise ValueError("Metric Runner Binding identity 不一致。")
        return self


class EvolutionMetricBindingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMetricRunnerRegistry:
    """Resolve only concrete, locally implemented metric runners."""

    def resolve(
        self,
        metric: BaselineCohortMetricCase,
        *,
        validation_paths: tuple[str, ...],
    ) -> MetricRunnerResolution:
        if not isinstance(metric, BaselineCohortMetricCase):
            raise TypeError("Metric runner registry 需要 BaselineCohortMetricCase。")
        metric = BaselineCohortMetricCase.model_validate(metric.model_dump(mode="json"))
        if (
            not validation_paths
            or validation_paths != tuple(sorted(set(validation_paths)))
            or any(
                not path
                or "\\" in path
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                for path in validation_paths
            )
        ):
            raise EvolutionMetricBindingError(
                "validation_paths_invalid",
                "Metric runner validation paths 必须是已排序且唯一的安全相对路径。",
            )
        if metric.verifier == "self_review_static":
            return self._resolve_self_review(metric, validation_paths)
        if metric.verifier == "harness_replay":
            return MetricRunnerResolution(
                verifier=metric.verifier,
                status="blocked",
                runner_version=SAFE_REPLAY_EVAL_RUNNER_VERSION,
                fixture_kind="harness_replay_baseline",
                blocking_code="replay_fixture_required",
            )
        if metric.verifier == "feedback_recurrence":
            return MetricRunnerResolution(
                verifier=metric.verifier,
                status="blocked",
                fixture_kind="feedback_observation_window",
                blocking_code="feedback_window_runner_unavailable",
            )
        raise EvolutionMetricBindingError(
            "metric_verifier_unsupported",
            "Metric verifier 没有受信任的机械执行器。",
        )

    @staticmethod
    def _resolve_self_review(
        metric: BaselineCohortMetricCase,
        validation_paths: tuple[str, ...],
    ) -> MetricRunnerResolution:
        match = _SELF_REVIEW_METRIC_RE.fullmatch(metric.metric_name)
        finding_code = match.group(1) if match else None
        supported_codes = {item.value for item in SelfReviewFindingCode}
        if finding_code not in supported_codes:
            return MetricRunnerResolution(
                verifier=metric.verifier,
                status="blocked",
                runner_version=SELF_REVIEW_STATIC_RUNNER_VERSION,
                fixture_kind="validation_paths",
                finding_code=finding_code or "invalid_metric",
                blocking_code="self_review_metric_unsupported",
            )
        fixture_sha256 = _sha256_payload({
            "finding_code": finding_code,
            "validation_paths": validation_paths,
        })
        return MetricRunnerResolution(
            verifier=metric.verifier,
            status="ready",
            runner_version=SELF_REVIEW_STATIC_RUNNER_VERSION,
            timeout_seconds_per_sample=SELF_REVIEW_METRIC_TIMEOUT_SECONDS,
            fixture_kind="validation_paths",
            fixture_sha256=fixture_sha256,
            finding_code=finding_code,
        )


class EvolutionMetricRunnerBindingBuilder:
    """Bind metric authority without executing a baseline cohort."""

    def __init__(self, registry: EvolutionMetricRunnerRegistry | None = None) -> None:
        self._registry = registry or EvolutionMetricRunnerRegistry()

    def build(
        self,
        *,
        baseline_request: EvolutionBaselineCohortRequest,
        validation_plan: EvolutionValidationPlan,
    ) -> EvolutionMetricRunnerBinding:
        if not isinstance(baseline_request, EvolutionBaselineCohortRequest):
            raise TypeError("Metric Runner Binding 需要 Baseline Cohort Request。")
        if not isinstance(validation_plan, EvolutionValidationPlan):
            raise TypeError("Metric Runner Binding 需要 EvolutionValidationPlan。")
        baseline_request = EvolutionBaselineCohortRequest.model_validate(
            baseline_request.model_dump(mode="json")
        )
        validation_plan = EvolutionValidationPlan.model_validate(
            validation_plan.model_dump(mode="json")
        )
        self._require_authority(baseline_request, validation_plan)
        validation_paths = tuple(item.path for item in validation_plan.files)
        entries = tuple(
            MetricRunnerBindingEntry(
                order=metric.order,
                metric_name=metric.metric_name,
                direction=metric.direction,
                target=metric.target,
                procedure_sha256=metric.procedure_sha256,
                resolution=self._registry.resolve(
                    metric,
                    validation_paths=validation_paths,
                ),
            )
            for metric in baseline_request.metrics
        )
        metric_timeout = sum(
            (item.resolution.timeout_seconds_per_sample or 0)
            * baseline_request.requested_samples
            for item in entries
        )
        profile_timeout = (
            baseline_request.check_timeout_seconds_per_sample
            * baseline_request.requested_samples
        )
        required_duration = profile_timeout + metric_timeout
        entries = _apply_duration_budget(
            entries,
            required_duration_seconds=required_duration,
            max_total_duration_seconds=baseline_request.max_total_duration_seconds,
        )
        blocking_codes = tuple(sorted({
            item.resolution.blocking_code
            for item in entries
            if item.resolution.blocking_code is not None
        }))
        payload = {
            "schema_version": 1,
            "policy_version": METRIC_RUNNER_BINDING_POLICY,
            "baseline_request_id": baseline_request.request_id,
            "baseline_request_sha256": baseline_request.request_sha256,
            "validation_plan_id": validation_plan.validation_plan_id,
            "validation_plan_sha256": validation_plan.validation_plan_sha256,
            "phase": "red",
            "requested_samples": baseline_request.requested_samples,
            "entries": [item.model_dump(mode="json") for item in entries],
            "binding_status": "blocked" if blocking_codes else "ready",
            "blocking_codes": list(blocking_codes),
            "profile_timeout_seconds_total": profile_timeout,
            "metric_timeout_seconds_total": metric_timeout,
            "required_duration_seconds": required_duration,
            "max_total_duration_seconds": baseline_request.max_total_duration_seconds,
            "budget_headroom_seconds": max(
                0,
                baseline_request.max_total_duration_seconds - required_duration,
            ),
            "metric_binding_complete": not blocking_codes,
            "arc04_worker_required": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionMetricRunnerBinding.model_validate({
            **payload,
            "binding_id": f"evvmetric_{digest[:24]}",
            "binding_sha256": digest,
        })

    @staticmethod
    def _require_authority(
        request: EvolutionBaselineCohortRequest,
        plan: EvolutionValidationPlan,
    ) -> None:
        metrics_match = tuple(
            (
                item.order,
                item.metric_name,
                item.direction,
                item.target,
                item.verifier,
                item.procedure_sha256,
            )
            for item in request.metrics
        ) == tuple(
            (
                item.order,
                item.metric_name,
                item.direction,
                item.target,
                item.verifier,
                hashlib.sha256(item.procedure.encode("utf-8")).hexdigest(),
            )
            for item in plan.metrics
        )
        if not (
            request.validation_plan_id == plan.validation_plan_id
            and request.validation_plan_sha256 == plan.validation_plan_sha256
            and request.candidate_id == plan.candidate_id
            and request.candidate_revision == plan.candidate_revision
            and request.baseline_commit == plan.baseline_commit
            and request.baseline_tree_sha256 == plan.baseline_tree_sha256
            and metrics_match
        ):
            raise EvolutionMetricBindingError(
                "metric_binding_authority_mismatch",
                "Metric Runner Binding authority 不一致。",
            )


def _sha256_payload(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _apply_duration_budget(
    entries: tuple[MetricRunnerBindingEntry, ...],
    *,
    required_duration_seconds: int,
    max_total_duration_seconds: int,
) -> tuple[MetricRunnerBindingEntry, ...]:
    if required_duration_seconds <= max_total_duration_seconds:
        return entries
    return tuple(
        item.model_copy(update={
            "resolution": item.resolution.model_copy(update={
                "status": "blocked",
                "blocking_code": "metric_duration_budget_exceeded",
            })
            if item.resolution.status == "ready"
            else item.resolution,
        })
        for item in entries
    )


__all__ = [
    "EvolutionMetricBindingError",
    "EvolutionMetricRunnerBinding",
    "EvolutionMetricRunnerBindingBuilder",
    "EvolutionMetricRunnerRegistry",
    "MetricRunnerBindingEntry",
    "MetricRunnerResolution",
]
