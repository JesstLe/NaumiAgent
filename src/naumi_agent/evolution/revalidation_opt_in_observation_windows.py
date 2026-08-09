"""Deterministic opt-in runtime liveness-window evidence."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_runtime_health import (
    EvolutionRevalidationOptInRuntimeHealthReceipt,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.runtime_release_binding import (
    HarnessRuntimeReleaseBinding,
)
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)

EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_WINDOW_POLICY = (
    "evolution-revalidation-opt-in-observation-window-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SAMPLES = 5_000


class EvolutionRevalidationOptInObservationWindowStatus(StrEnum):
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


class EvolutionRevalidationOptInObservationWindow(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-observation-window-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_WINDOW_POLICY
    window_id: str = Field(pattern=r"^evreoptinwindow_[0-9a-f]{24}$")
    window_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    runtime_health: EvolutionRevalidationOptInRuntimeHealthReceipt
    binding: HarnessRuntimeReleaseBinding
    samples: tuple[HarnessRuntimeReleaseObservation, ...] = Field(
        min_length=1,
        max_length=_MAX_SAMPLES,
    )
    assessed_at: str = Field(min_length=1, max_length=100)
    first_observed_at: str = Field(min_length=1, max_length=100)
    last_observed_at: str = Field(min_length=1, max_length=100)
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    sample_count: int = Field(ge=1, le=_MAX_SAMPLES)
    operational_sample_count: int = Field(ge=0, le=_MAX_SAMPLES)
    minimum_sample_count: int = Field(ge=2, le=_MAX_SAMPLES)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: int = Field(ge=300, le=604_800)
    maximum_gap_seconds: int = Field(ge=3, le=86_400)
    maximum_observed_gap_seconds: int = Field(ge=0, le=86_400)
    latest_age_seconds: int = Field(ge=0, le=604_800)
    required_completed_runs: int = Field(ge=10, le=100)
    completed_runs_observed: Literal[0] = 0
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    status: EvolutionRevalidationOptInObservationWindowStatus
    exposure_binding_authority: Literal[True] = True
    runtime_liveness_window_authority: bool
    completed_run_evidence_authority: Literal[False] = False
    opt_in_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    pause_input_authority: bool
    rollback_input_authority: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Opt-in Observation Window workspace 必须 canonical。")
        projection = _evaluate(
            runtime_health=self.runtime_health,
            binding=self.binding,
            samples=self.samples,
            assessed_at=self.assessed_at,
        )
        fields = (
            "first_observed_at",
            "last_observed_at",
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
            "required_completed_runs",
            "insufficient_reasons",
            "breach_reasons",
            "status",
            "runtime_liveness_window_authority",
            "pause_input_authority",
            "rollback_input_authority",
        )
        if any(getattr(self, field) != projection[field] for field in fields):
            raise ValueError("Opt-in Observation Window evidence projection 不一致。")
        core = self.model_dump(mode="json", exclude={"window_id", "window_sha256"})
        digest = _digest(core)
        if self.window_sha256 != digest or self.window_id != (
            f"evreoptinwindow_{digest[:24]}"
        ):
            raise ValueError("Opt-in Observation Window identity 不一致。")
        return self


class EvolutionRevalidationOptInObservationWindowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_opt_in_observation_window(
    *,
    runtime_health: EvolutionRevalidationOptInRuntimeHealthReceipt,
    binding: HarnessRuntimeReleaseBinding,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> EvolutionRevalidationOptInObservationWindow:
    try:
        health = EvolutionRevalidationOptInRuntimeHealthReceipt.model_validate_json(
            runtime_health.model_dump_json()
        )
        release_binding = HarnessRuntimeReleaseBinding.model_validate_json(
            binding.model_dump_json()
        )
        observations = tuple(
            HarnessRuntimeReleaseObservation.model_validate_json(
                item.model_dump_json()
            )
            for item in samples
        )
        assessed = _aware(assessed_at).isoformat()
        projection = _evaluate(
            runtime_health=health,
            binding=release_binding,
            samples=observations,
            assessed_at=assessed,
        )
    except EvolutionRevalidationOptInObservationWindowError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_input_invalid",
            "Opt-in Observation Window 输入 artifact 无效。",
        ) from exc
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_WINDOW_POLICY,
        "workspace_root": health.workspace_root,
        "runtime_health": health.model_dump(mode="json"),
        "binding": release_binding.model_dump(mode="json"),
        "samples": [item.model_dump(mode="json") for item in observations],
        "assessed_at": assessed,
        **{
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in projection.items()
        },
        "completed_runs_observed": 0,
        "exposure_binding_authority": True,
        "completed_run_evidence_authority": False,
        "opt_in_stage_completion_authority": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInObservationWindow.model_validate(
        {
            **core,
            "runtime_health": health,
            "binding": release_binding,
            "samples": observations,
            "window_id": f"evreoptinwindow_{digest[:24]}",
            "window_sha256": digest,
        }
    )


def _evaluate(
    *,
    runtime_health: EvolutionRevalidationOptInRuntimeHealthReceipt,
    binding: HarnessRuntimeReleaseBinding,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> dict[str, object]:
    if not (
        runtime_health.outcome == "healthy"
        and runtime_health.runtime_health_authority
        and runtime_health.health_report is not None
    ):
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_health_not_authoritative",
            "Opt-in Observation Window 需要 healthy Runtime Health authority。",
        )
    deployment = runtime_health.deployment
    cohort = deployment.intent.cohort
    stage = cohort.stage
    identity = binding.runtime_identity
    pointer = deployment.activated_pointer
    admission = deployment.intent.admission
    if not (
        runtime_health.workspace_root == binding.workspace_root == cohort.workspace_root
        and identity.pointer_id == pointer.pointer_id
        and identity.pointer_sha256 == pointer.pointer_sha256
        and identity.pointer_generation == pointer.generation
        and identity.slot_id == pointer.current_slot_id == admission.candidate_slot.slot_id
        and identity.slot_sha256
        == pointer.current_slot_sha256
        == admission.candidate_slot.slot_sha256
        and identity.boot_receipt_id
        == pointer.boot_receipt_id
        == admission.boot_receipt.receipt_id
        and identity.boot_receipt_sha256
        == pointer.boot_receipt_sha256
        == admission.boot_receipt.receipt_sha256
        and identity.binary_sha256 == admission.boot_receipt.binary_sha256
        and identity.version == cohort.candidate_version
        and identity.target == cohort.candidate_target
        and binding.release_identity_authority
        and not binding.rollout_observation_authority
    ):
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_release_binding_mismatch",
            "Runtime Release Binding 与 Opt-in Deployment/exposure 不一致。",
        )
    if not samples or len(samples) > _MAX_SAMPLES:
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_sample_count_invalid",
            "Opt-in Observation Window 样本数必须在 1 到 5000 之间。",
        )
    if _aware(samples[0].observed_at) < _aware(runtime_health.completed_at):
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_predates_health",
            "Opt-in Observation 样本不能早于 Runtime Health 完成时间。",
        )
    previous = None
    maximum_gap = 0.0
    timeout = samples[0].timeout_seconds
    for item in samples:
        if not (
            item.binding_id == binding.binding_id
            and item.binding_sha256 == binding.binding_sha256
            and item.runtime_identity_id == identity.identity_id
            and item.runtime_identity_sha256 == identity.identity_sha256
            and item.subject_id == binding.subject_id
            and item.instance_id == binding.instance_id
            and item.epoch == binding.epoch
            and item.surface == binding.surface
            and item.timeout_seconds == timeout
            and item.heartbeat_observation_authority
            and not item.rollout_observation_window_authority
        ):
            raise EvolutionRevalidationOptInObservationWindowError(
                "opt_in_observation_sample_binding_mismatch",
                "Opt-in Observation sample 未绑定 exact Runtime Release Binding。",
            )
        if previous is not None:
            if not (
                item.heartbeat_sequence == previous.heartbeat_sequence + 1
                and item.previous_sample_sha256 == previous.sample_sha256
            ):
                raise EvolutionRevalidationOptInObservationWindowError(
                    "opt_in_observation_sample_chain_broken",
                    "Opt-in Observation sample sequence/hash chain 不连续。",
                )
            gap = (
                _aware(item.observed_at) - _aware(previous.observed_at)
            ).total_seconds()
            if gap < 0:
                raise EvolutionRevalidationOptInObservationWindowError(
                    "opt_in_observation_clock_regression",
                    "Opt-in Observation sample 时间发生倒退。",
                )
            maximum_gap = max(maximum_gap, gap)
        previous = item

    assessed = _aware(assessed_at)
    first = _aware(samples[0].observed_at)
    last = _aware(samples[-1].observed_at)
    if assessed < last:
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_assessment_predates_sample",
            "Opt-in Observation assessed_at 不能早于末样本。",
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
    if minimum_samples > _MAX_SAMPLES:
        raise EvolutionRevalidationOptInObservationWindowError(
            "opt_in_observation_policy_exceeds_sample_bound",
            "当前 heartbeat timeout 无法在 5000 个样本内证明冻结观察窗口。",
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
    if samples[0].chain_origin_kind == "legacy_snapshot":
        insufficient.append("legacy_history_origin")
    if samples[-1].phase not in operational_phases:
        insufficient.append("runtime_not_active")
    if len(operational) < minimum_samples:
        insufficient.append("minimum_sample_count")
    if observation_seconds < stage.minimum_observation_seconds:
        insufficient.append("minimum_observation_seconds")
    insufficient_reasons = tuple(() if breach_reasons else sorted(set(insufficient)))
    status = (
        EvolutionRevalidationOptInObservationWindowStatus.BREACHED
        if breach_reasons
        else EvolutionRevalidationOptInObservationWindowStatus.INSUFFICIENT
        if insufficient_reasons
        else EvolutionRevalidationOptInObservationWindowStatus.PASSING
    )
    breached = status is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    passing = status is EvolutionRevalidationOptInObservationWindowStatus.PASSING
    return {
        "first_observed_at": first.isoformat(),
        "last_observed_at": last.isoformat(),
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
        "required_completed_runs": stage.minimum_completed_runs,
        "insufficient_reasons": insufficient_reasons,
        "breach_reasons": breach_reasons,
        "status": status,
        "runtime_liveness_window_authority": passing,
        "pause_input_authority": breached,
        "rollback_input_authority": breached,
    }


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Opt-in Observation Window timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_WINDOW_POLICY",
    "EvolutionRevalidationOptInObservationWindow",
    "EvolutionRevalidationOptInObservationWindowError",
    "EvolutionRevalidationOptInObservationWindowStatus",
    "build_opt_in_observation_window",
]
