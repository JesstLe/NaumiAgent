"""Deterministic runtime observation windows for percentage installations."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_percentage_runtime_exposures import (
    EvolutionRevalidationPercentageRuntimeExposureReceipt,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)

EVOLUTION_REVALIDATION_PERCENTAGE_OBSERVATION_WINDOW_POLICY = (
    "evolution-revalidation-percentage-observation-window-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SAMPLES = 5_000


class EvolutionRevalidationPercentageObservationWindowStatus(StrEnum):
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationPercentageObservationWindow(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-percentage-observation-window-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_OBSERVATION_WINDOW_POLICY
    window_id: str = Field(pattern=r"^evrepercentwindow_[0-9a-f]{24}$")
    window_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    exposure: EvolutionRevalidationPercentageRuntimeExposureReceipt
    samples: tuple[HarnessRuntimeReleaseObservation, ...] = Field(
        min_length=2,
        max_length=_MAX_SAMPLES,
    )
    assessed_at: str = Field(min_length=1, max_length=100)
    first_observed_at: str = Field(min_length=1, max_length=100)
    last_observed_at: str = Field(min_length=1, max_length=100)
    sample_scope: Literal["origin", "suffix"]
    suffix_anchor_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    first_sequence: int = Field(ge=1, le=2_147_483_647)
    last_sequence: int = Field(ge=2, le=2_147_483_647)
    sample_count: int = Field(ge=2, le=_MAX_SAMPLES)
    operational_sample_count: int = Field(ge=0, le=_MAX_SAMPLES)
    minimum_sample_count: int = Field(ge=2, le=_MAX_SAMPLES)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: int = Field(ge=300, le=604_800)
    maximum_gap_seconds: int = Field(ge=3, le=86_400)
    maximum_observed_gap_seconds: int = Field(ge=0, le=86_400)
    latest_age_seconds: int = Field(ge=0, le=604_800)
    exposure_percent: int = Field(ge=1, le=99)
    population_denominator: int = Field(ge=1, le=1_000_000)
    target_member_count: int = Field(ge=1, le=1_000_000)
    member_rank: int = Field(ge=1, le=1_000_000)
    required_completed_runs: int = Field(ge=10, le=100)
    completed_runs_observed: Literal[0] = 0
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    status: EvolutionRevalidationPercentageObservationWindowStatus
    installation_exposure_binding_authority: Literal[True] = True
    percentage_runtime_window_authority: bool
    cohort_observation_authority: Literal[False] = False
    completed_run_evidence_authority: Literal[False] = False
    percentage_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    pause_input_authority: bool
    rollback_input_authority: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Percentage Observation Window workspace 必须 canonical。")
        projection = _evaluate(
            exposure=self.exposure,
            samples=self.samples,
            assessed_at=self.assessed_at,
        )
        fields = (
            "first_observed_at",
            "last_observed_at",
            "sample_scope",
            "suffix_anchor_sha256",
            "first_sequence",
            "last_sequence",
            "sample_count",
            "operational_sample_count",
            "minimum_sample_count",
            "observation_seconds",
            "minimum_observation_seconds",
            "maximum_gap_seconds",
            "maximum_observed_gap_seconds",
            "latest_age_seconds",
            "exposure_percent",
            "population_denominator",
            "target_member_count",
            "member_rank",
            "required_completed_runs",
            "insufficient_reasons",
            "breach_reasons",
            "status",
            "percentage_runtime_window_authority",
            "pause_input_authority",
            "rollback_input_authority",
        )
        if any(getattr(self, field) != projection[field] for field in fields):
            raise ValueError("Percentage Observation Window evidence 投影不一致。")
        core = self.model_dump(mode="json", exclude={"window_id", "window_sha256"})
        digest = _digest(core)
        if not (
            self.window_sha256 == digest
            and self.window_id == f"evrepercentwindow_{digest[:24]}"
        ):
            raise ValueError("Percentage Observation Window identity 不一致。")
        return self


class EvolutionRevalidationPercentageObservationWindowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_percentage_observation_window(
    *,
    exposure: EvolutionRevalidationPercentageRuntimeExposureReceipt,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> EvolutionRevalidationPercentageObservationWindow:
    if not isinstance(samples, tuple) or not 2 <= len(samples) <= _MAX_SAMPLES:
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_sample_count_invalid",
            "Percentage Observation 样本数必须在 2 到 5000 之间。",
        )
    try:
        source = EvolutionRevalidationPercentageRuntimeExposureReceipt.model_validate_json(
            exposure.model_dump_json()
        )
        observations = tuple(
            HarnessRuntimeReleaseObservation.model_validate_json(
                item.model_dump_json()
            )
            for item in samples
        )
        assessed = _aware(assessed_at).isoformat()
        projection = _evaluate(
            exposure=source,
            samples=observations,
            assessed_at=assessed,
        )
    except EvolutionRevalidationPercentageObservationWindowError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_input_invalid",
            "Percentage Observation Window 输入 artifact 无效。",
        ) from exc
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PERCENTAGE_OBSERVATION_WINDOW_POLICY,
        "workspace_root": source.workspace_root,
        "exposure": source.model_dump(mode="json"),
        "samples": [item.model_dump(mode="json") for item in observations],
        "assessed_at": assessed,
        **{
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in projection.items()
        },
        "completed_runs_observed": 0,
        "installation_exposure_binding_authority": True,
        "cohort_observation_authority": False,
        "completed_run_evidence_authority": False,
        "percentage_stage_completion_authority": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationPercentageObservationWindow.model_validate(
        {
            **core,
            "exposure": source,
            "samples": observations,
            "window_id": f"evrepercentwindow_{digest[:24]}",
            "window_sha256": digest,
        }
    )


def _evaluate(
    *,
    exposure: EvolutionRevalidationPercentageRuntimeExposureReceipt,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> dict[str, object]:
    if not (
        exposure.installation_exposure_observed
        and exposure.percentage_observation_input_authority
    ):
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_exposure_not_authoritative",
            "Percentage Observation Window 需要 installation exposure authority。",
        )
    deployment = exposure.deployment
    intent = deployment.preparation.intent
    assignment = intent.assignment
    stage = intent.plan.stages[2]
    binding = exposure.binding
    if not (
        stage.name == "percentage"
        and stage.exposure == "limited"
        and assignment.plan_id == intent.plan.plan_id
        and assignment.plan_sha256 == intent.plan.plan_sha256
        and assignment.exposure_percent == stage.exposure_percent
        and assignment.member_selected
        and assignment.percentage_cohort_membership_authority
        and assignment.population_assignment_enforced
        and assignment.population_denominator >= assignment.target_member_count
        and 1 <= assignment.member_rank <= assignment.population_denominator
    ):
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_plan_mismatch",
            "Percentage Assignment 与 frozen rollout stage 不一致。",
        )
    if not 2 <= len(samples) <= _MAX_SAMPLES:
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_sample_count_invalid",
            "Percentage Observation 样本数必须在 2 到 5000 之间。",
        )
    starts_at_origin = bool(
        samples[0] == exposure.startup_observation
        and samples[1] == exposure.ready_observation
    )
    starts_at_suffix = bool(
        samples[0].heartbeat_sequence > exposure.ready_observation.heartbeat_sequence
        and samples[0].chain_origin_kind == "startup"
        and samples[0].chain_origin_sequence == 1
        and samples[0].previous_sample_sha256
        and _aware(samples[0].observed_at)
        >= _aware(exposure.ready_observation.observed_at)
        and (
            samples[0].heartbeat_sequence
            != exposure.ready_observation.heartbeat_sequence + 1
            or samples[0].previous_sample_sha256
            == exposure.ready_observation.sample_sha256
        )
    )
    if not (starts_at_origin or starts_at_suffix):
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_origin_mismatch",
            "Percentage Observation 必须从 exact Exposure startup pair 或连续 suffix 开始。",
        )
    sample_scope = "origin" if starts_at_origin else "suffix"
    previous = None
    maximum_gap = 0.0
    timeout = samples[0].timeout_seconds
    for item in samples:
        if not (
            item.workspace_root == exposure.workspace_root
            and item.binding_id == binding.binding_id
            and item.binding_sha256 == binding.binding_sha256
            and item.runtime_identity_id == binding.runtime_identity.identity_id
            and item.runtime_identity_sha256
            == binding.runtime_identity.identity_sha256
            and item.surface == binding.surface
            and item.subject_id == binding.subject_id
            and item.instance_id == binding.instance_id
            and item.epoch == binding.epoch
            and item.timeout_seconds == timeout
            and item.chain_origin_kind == "startup"
            and item.chain_origin_sequence == 1
            and item.heartbeat_observation_authority
            and not item.rollout_observation_window_authority
        ):
            raise EvolutionRevalidationPercentageObservationWindowError(
                "percentage_observation_sample_binding_mismatch",
                "Percentage Observation sample 未绑定 exact Exposure runtime。",
            )
        if previous is not None:
            if not (
                item.heartbeat_sequence == previous.heartbeat_sequence + 1
                and item.previous_sample_sha256 == previous.sample_sha256
            ):
                raise EvolutionRevalidationPercentageObservationWindowError(
                    "percentage_observation_sample_chain_broken",
                    "Percentage Observation sample sequence/hash chain 不连续。",
                )
            gap = (
                _aware(item.observed_at) - _aware(previous.observed_at)
            ).total_seconds()
            if gap < 0:
                raise EvolutionRevalidationPercentageObservationWindowError(
                    "percentage_observation_clock_regression",
                    "Percentage Observation sample 时间发生倒退。",
                )
            maximum_gap = max(maximum_gap, gap)
        previous = item
    assessed = _aware(assessed_at)
    first = _aware(samples[0].observed_at)
    last = _aware(samples[-1].observed_at)
    if assessed < last:
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_assessment_predates_sample",
            "Percentage Observation assessed_at 不能早于末样本。",
        )
    operational_phases = {
        HarnessHeartbeatPhase.RUNNING,
        HarnessHeartbeatPhase.WAITING,
    }
    operational_start = len(samples)
    for index in range(len(samples) - 1, -1, -1):
        if samples[index].phase not in operational_phases:
            break
        operational_start = index
    operational = samples[operational_start:]
    operational_first = (
        _aware(operational[0].observed_at) if operational else last
    )
    observation_seconds = min(
        604_800,
        max(0, int((last - operational_first).total_seconds())),
    )
    latest_age_exact = max(0.0, (assessed - last).total_seconds())
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    maximum_operational_samples = (
        _MAX_SAMPLES - 1 if sample_scope == "origin" else _MAX_SAMPLES
    )
    if minimum_samples > maximum_operational_samples:
        raise EvolutionRevalidationPercentageObservationWindowError(
            "percentage_observation_policy_exceeds_sample_bound",
            "当前 heartbeat timeout 无法在 5000 个样本内证明 percentage 窗口。",
        )
    breaches = []
    if any(item.phase is HarnessHeartbeatPhase.FAILED for item in samples):
        breaches.append("runtime_failed")
    if maximum_gap > timeout:
        breaches.append("heartbeat_gap")
    if latest_age_exact > timeout:
        breaches.append("heartbeat_stale")
    breach_reasons = tuple(sorted(set(breaches)))
    insufficient = []
    if samples[-1].phase not in operational_phases:
        insufficient.append("runtime_not_active")
    if len(operational) < minimum_samples:
        insufficient.append("minimum_sample_count")
    if observation_seconds < stage.minimum_observation_seconds:
        insufficient.append("minimum_observation_seconds")
    insufficient_reasons = tuple(() if breach_reasons else sorted(set(insufficient)))
    status = (
        EvolutionRevalidationPercentageObservationWindowStatus.BREACHED
        if breach_reasons
        else EvolutionRevalidationPercentageObservationWindowStatus.INSUFFICIENT
        if insufficient_reasons
        else EvolutionRevalidationPercentageObservationWindowStatus.PASSING
    )
    breached = status is EvolutionRevalidationPercentageObservationWindowStatus.BREACHED
    passing = status is EvolutionRevalidationPercentageObservationWindowStatus.PASSING
    return {
        "first_observed_at": first.isoformat(),
        "last_observed_at": last.isoformat(),
        "sample_scope": sample_scope,
        "suffix_anchor_sha256": (
            "" if sample_scope == "origin" else samples[0].previous_sample_sha256
        ),
        "first_sequence": samples[0].heartbeat_sequence,
        "last_sequence": samples[-1].heartbeat_sequence,
        "sample_count": len(samples),
        "operational_sample_count": len(operational),
        "minimum_sample_count": minimum_samples,
        "observation_seconds": observation_seconds,
        "minimum_observation_seconds": stage.minimum_observation_seconds,
        "maximum_gap_seconds": timeout,
        "maximum_observed_gap_seconds": min(86_400, math.ceil(maximum_gap)),
        "latest_age_seconds": min(604_800, math.ceil(latest_age_exact)),
        "exposure_percent": stage.exposure_percent,
        "population_denominator": assignment.population_denominator,
        "target_member_count": assignment.target_member_count,
        "member_rank": assignment.member_rank,
        "required_completed_runs": stage.minimum_completed_runs,
        "insufficient_reasons": insufficient_reasons,
        "breach_reasons": breach_reasons,
        "status": status,
        "percentage_runtime_window_authority": passing,
        "pause_input_authority": breached,
        "rollback_input_authority": breached,
    }


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Percentage Observation Window timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_PERCENTAGE_OBSERVATION_WINDOW_POLICY",
    "EvolutionRevalidationPercentageObservationWindow",
    "EvolutionRevalidationPercentageObservationWindowError",
    "EvolutionRevalidationPercentageObservationWindowStatus",
    "build_percentage_observation_window",
]
