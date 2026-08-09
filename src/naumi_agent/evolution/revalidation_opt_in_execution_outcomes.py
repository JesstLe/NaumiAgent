"""Immutable release-bound outcomes for real opt-in terminal runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_observation_windows import (
    EvolutionRevalidationOptInObservationWindow,
    EvolutionRevalidationOptInObservationWindowStatus,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.runs.models import CompletionReceipt, ReceiptOutcome
from naumi_agent.runs.release_provenance import RunReleaseProvenance
from naumi_agent.runs.store import ChatRunRecord
from naumi_agent.runs.usage import RunUsage

EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_POLICY = (
    "evolution-revalidation-opt-in-execution-outcome-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_COVERAGE_SAMPLES = 5_000


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInLivenessSourceRef(_StrictModel):
    completion_id: str = Field(min_length=1, max_length=128)
    window_id: str = Field(pattern=r"^evreoptinwindow_[0-9a-f]{24}$")
    window_sha256: str = Field(pattern=_SHA256_RE)
    runtime_health_receipt_id: str = Field(
        pattern=r"^evreruntimehealth_[0-9a-f]{24}$"
    )
    runtime_health_receipt_sha256: str = Field(pattern=_SHA256_RE)
    deployment_receipt_id: str = Field(pattern=r"^evredeployment_[0-9a-f]{24}$")
    deployment_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=128)
    stage_order: int = Field(ge=1, le=4)
    stage_name: Literal["opt_in"] = "opt_in"
    liveness_source_authority: Literal[True] = True
    completed_run_authority: Literal[False] = False


class EvolutionRevalidationOptInExecutionOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-execution-outcome-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_POLICY
    outcome_id: str = Field(pattern=r"^evreoptinoutcome_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    run_status: Literal["completed", "failed", "cancelled"]
    liveness_source: EvolutionRevalidationOptInLivenessSourceRef
    release_provenance: RunReleaseProvenance
    completion_receipt: CompletionReceipt
    usage: RunUsage
    coverage_samples: tuple[HarnessRuntimeReleaseObservation, ...] = Field(
        min_length=2,
        max_length=_MAX_COVERAGE_SAMPLES,
    )
    execution_started_at: str = Field(min_length=1, max_length=100)
    execution_completed_at: str = Field(min_length=1, max_length=100)
    duration_ms: int = Field(ge=0)
    result: ReceiptOutcome
    recorded_at: str = Field(min_length=1, max_length=100)
    release_source_authority: Literal[True] = True
    completion_source_authority: Literal[True] = True
    usage_source_authority: Literal[True] = True
    heartbeat_coverage_authority: Literal[True] = True
    execution_outcome_authority: Literal[True] = True
    successful_completed_run_authority: bool
    opt_in_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Opt-in Execution Outcome workspace 必须 canonical。")
        receipt = CompletionReceipt.from_dict(self.completion_receipt.to_dict())
        expected_status = {
            "completed": "completed",
            "partial": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }[receipt.outcome]
        if not (
            self.run_id
            == self.release_provenance.run_id
            == receipt.run_id
            == self.usage.run_id
            and self.workspace_root == self.release_provenance.workspace_root
            and self.run_status == expected_status
            and self.result == receipt.outcome
            and self.execution_started_at
            == receipt.started_at
            == self.release_provenance.run_started_at
            and self.execution_completed_at == receipt.completed_at
            and self.duration_ms == receipt.duration_ms
            and self.liveness_source.binding_id
            == self.release_provenance.binding.binding_id
            and self.liveness_source.binding_sha256
            == self.release_provenance.binding.binding_sha256
        ):
            raise ValueError("Opt-in Execution Outcome run/source projection 不一致。")
        started = _aware(self.execution_started_at)
        completed = _aware(self.execution_completed_at)
        recorded = _aware(self.recorded_at)
        if completed < started or recorded < completed:
            raise ValueError("Opt-in Execution Outcome 时间窗口无效。")
        _validate_coverage(
            provenance=self.release_provenance,
            samples=self.coverage_samples,
            started_at=started,
            completed_at=completed,
        )
        successful = bool(
            self.result == "completed"
            and self.run_status == "completed"
        )
        if self.successful_completed_run_authority is not successful:
            raise ValueError("Opt-in Execution Outcome success projection 不一致。")
        core = self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        digest = _digest(core)
        if self.outcome_sha256 != digest or self.outcome_id != (
            f"evreoptinoutcome_{digest[:24]}"
        ):
            raise ValueError("Opt-in Execution Outcome identity 不一致。")
        return self


class EvolutionRevalidationOptInExecutionOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_opt_in_execution_outcome(
    *,
    window: EvolutionRevalidationOptInObservationWindow,
    run: ChatRunRecord,
    coverage_samples: tuple[HarnessRuntimeReleaseObservation, ...],
    recorded_at: str,
) -> EvolutionRevalidationOptInExecutionOutcome:
    if not (
        isinstance(window, EvolutionRevalidationOptInObservationWindow)
        and isinstance(run, ChatRunRecord)
    ):
        raise TypeError("Opt-in Execution Outcome 需要 Window 与 ChatRunRecord。")
    if not (
        window.status is EvolutionRevalidationOptInObservationWindowStatus.PASSING
        and window.runtime_liveness_window_authority
    ):
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_liveness_not_authoritative",
            "Opt-in Execution Outcome 需要 passing liveness window。",
        )
    if run.receipt is None or run.release_provenance is None or run.usage is None:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_run_evidence_incomplete",
            "Chat run 缺少终态回执、release provenance 或结构化用量。",
        )
    if not run.completed_at:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_run_not_terminal",
            "Chat run 尚未进入持久终态。",
        )
    health = window.runtime_health
    deployment = health.deployment
    cohort = deployment.intent.cohort
    stage = cohort.stage
    if not (
        stage.name.value == "opt_in"
        and window.binding == run.release_provenance.binding
        and run.release_provenance.workspace_root == window.workspace_root
        and _aware(run.started_at) == _aware(run.release_provenance.run_started_at)
        and _aware(run.completed_at) >= _aware(run.receipt.completed_at)
        and _aware(run.receipt.started_at) >= _aware(health.completed_at)
    ):
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_release_or_time_mismatch",
            "Chat run 与 Opt-in release、health 或执行时间不一致。",
        )
    receipt = CompletionReceipt.from_dict(run.receipt.to_dict())
    provenance = type(run.release_provenance).model_validate_json(
        run.release_provenance.model_dump_json()
    )
    usage = type(run.usage).model_validate_json(run.usage.model_dump_json())
    samples = tuple(
        HarnessRuntimeReleaseObservation.model_validate_json(item.model_dump_json())
        for item in coverage_samples
    )
    source = EvolutionRevalidationOptInLivenessSourceRef(
        completion_id=deployment.intent.admission.completion_id,
        window_id=window.window_id,
        window_sha256=window.window_sha256,
        runtime_health_receipt_id=health.receipt_id,
        runtime_health_receipt_sha256=health.receipt_sha256,
        deployment_receipt_id=deployment.receipt_id,
        deployment_receipt_sha256=deployment.receipt_sha256,
        binding_id=window.binding.binding_id,
        binding_sha256=window.binding.binding_sha256,
        candidate_version=cohort.candidate_version,
        candidate_target=cohort.candidate_target,
        stage_order=stage.order,
    )
    successful = bool(
        receipt.outcome == "completed"
        and run.status == "completed"
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_POLICY,
        "workspace_root": window.workspace_root,
        "session_id": run.session_id,
        "run_id": run.id,
        "run_status": run.status,
        "liveness_source": source.model_dump(mode="json"),
        "release_provenance": provenance.model_dump(mode="json"),
        "completion_receipt": receipt.to_dict(),
        "usage": usage.model_dump(mode="json"),
        "coverage_samples": [item.model_dump(mode="json") for item in samples],
        "execution_started_at": receipt.started_at,
        "execution_completed_at": receipt.completed_at,
        "duration_ms": receipt.duration_ms,
        "result": receipt.outcome,
        "recorded_at": _aware(recorded_at).isoformat(),
        "release_source_authority": True,
        "completion_source_authority": True,
        "usage_source_authority": True,
        "heartbeat_coverage_authority": True,
        "execution_outcome_authority": True,
        "successful_completed_run_authority": successful,
        "opt_in_stage_completion_authority": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationOptInExecutionOutcome.model_validate(
            {
                **core,
                "outcome_id": f"evreoptinoutcome_{digest[:24]}",
                "outcome_sha256": digest,
            }
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_outcome_invalid",
            "Opt-in Execution Outcome artifact 无效。",
        ) from exc


def _validate_coverage(
    *,
    provenance: RunReleaseProvenance,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    started_at: datetime,
    completed_at: datetime,
) -> None:
    if not 2 <= len(samples) <= _MAX_COVERAGE_SAMPLES:
        raise ValueError("Opt-in Execution Outcome coverage 样本数无效。")
    binding = provenance.binding
    previous = None
    for sample in samples:
        if not (
            sample.binding_id == binding.binding_id
            and sample.binding_sha256 == binding.binding_sha256
            and sample.runtime_identity_id == binding.runtime_identity.identity_id
            and sample.runtime_identity_sha256
            == binding.runtime_identity.identity_sha256
            and sample.subject_id == binding.subject_id
            and sample.instance_id == binding.instance_id
            and sample.epoch == binding.epoch
            and sample.surface == binding.surface
            and sample.phase
            in {HarnessHeartbeatPhase.RUNNING, HarnessHeartbeatPhase.WAITING}
            and sample.heartbeat_observation_authority
            and not sample.current_liveness_authority
        ):
            raise ValueError("Opt-in Execution Outcome coverage binding/phase 无效。")
        if previous is not None:
            gap = (_aware(sample.observed_at) - _aware(previous.observed_at)).total_seconds()
            if not (
                sample.heartbeat_sequence == previous.heartbeat_sequence + 1
                and sample.previous_sample_sha256 == previous.sample_sha256
                and sample.timeout_seconds == previous.timeout_seconds
                and 0 <= gap <= sample.timeout_seconds
            ):
                raise ValueError("Opt-in Execution Outcome coverage chain/gap 无效。")
        previous = sample
    if not (
        _aware(samples[0].observed_at) <= started_at
        and _aware(samples[-1].observed_at) >= completed_at
    ):
        raise ValueError("Opt-in Execution Outcome heartbeat 未覆盖完整运行区间。")


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Opt-in Execution Outcome timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
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
    "EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_POLICY",
    "EvolutionRevalidationOptInExecutionOutcome",
    "EvolutionRevalidationOptInExecutionOutcomeError",
    "EvolutionRevalidationOptInLivenessSourceRef",
    "build_opt_in_execution_outcome",
]
