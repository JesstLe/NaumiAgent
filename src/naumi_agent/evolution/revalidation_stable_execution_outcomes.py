"""Immutable release-bound outcomes for real stable terminal runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_observation_windows import (
    EvolutionRevalidationStableObservationWindow,
    EvolutionRevalidationStableObservationWindowStatus,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.runs.models import CompletionReceipt, ReceiptOutcome
from naumi_agent.runs.release_provenance import RunReleaseProvenance
from naumi_agent.runs.store import ChatRunRecord
from naumi_agent.runs.usage import RunUsage

EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_POLICY = (
    "evolution-revalidation-stable-execution-outcome-v1"
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


class EvolutionRevalidationStableLivenessSourceRef(_StrictModel):
    intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    window_id: str = Field(pattern=r"^evrestablewindow_[0-9a-f]{24}$")
    window_sha256: str = Field(pattern=_SHA256_RE)
    exposure_id: str = Field(pattern=r"^evrestableexposure_[0-9a-f]{24}$")
    exposure_sha256: str = Field(pattern=_SHA256_RE)
    deployment_receipt_id: str = Field(
        pattern=r"^evrestabledeployment_[0-9a-f]{24}$"
    )
    deployment_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    stage_order: Literal[4] = 4
    stage_name: Literal["stable"] = "stable"
    exposure_percent: Literal[100] = 100
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    liveness_source_authority: Literal[True] = True
    completed_run_authority: Literal[False] = False


class EvolutionRevalidationStableExecutionOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-stable-execution-outcome-v1"
    ] = EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_POLICY
    outcome_id: str = Field(pattern=r"^evrestableoutcome_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    run_status: Literal["completed", "failed", "cancelled"]
    liveness_source: EvolutionRevalidationStableLivenessSourceRef
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
    stable_population_observation_input_authority: Literal[True] = True
    successful_completed_run_authority: bool
    stable_completed_run_authority: bool
    stable_stage_completion_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Execution Outcome workspace 必须 canonical。")
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
            raise ValueError("Stable Execution Outcome run/source projection 不一致。")
        started = _aware(self.execution_started_at)
        completed = _aware(self.execution_completed_at)
        recorded = _aware(self.recorded_at)
        if completed < started or recorded < completed:
            raise ValueError("Stable Execution Outcome 时间窗口无效。")
        _validate_coverage(
            provenance=self.release_provenance,
            samples=self.coverage_samples,
            started_at=started,
            completed_at=completed,
        )
        successful = bool(
            self.result == "completed" and self.run_status == "completed"
        )
        if not (
            self.successful_completed_run_authority is successful
            and self.stable_completed_run_authority is successful
        ):
            raise ValueError("Stable Execution Outcome success projection 不一致。")
        core = self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        digest = _digest(core)
        if self.outcome_sha256 != digest or self.outcome_id != (
            f"evrestableoutcome_{digest[:24]}"
        ):
            raise ValueError("Stable Execution Outcome identity 不一致。")
        return self


class EvolutionRevalidationStableExecutionOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_stable_execution_outcome(
    *,
    window: EvolutionRevalidationStableObservationWindow,
    run: ChatRunRecord,
    coverage_samples: tuple[HarnessRuntimeReleaseObservation, ...],
    recorded_at: str,
) -> EvolutionRevalidationStableExecutionOutcome:
    if not (
        isinstance(window, EvolutionRevalidationStableObservationWindow)
        and isinstance(run, ChatRunRecord)
    ):
        raise TypeError("Stable Execution Outcome 需要 Window 与 ChatRunRecord。")
    if not (
        window.status is EvolutionRevalidationStableObservationWindowStatus.PASSING
        and window.stable_runtime_window_authority
    ):
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_liveness_not_authoritative",
            "Stable Execution Outcome 需要 passing runtime window。",
        )
    if run.receipt is None or run.release_provenance is None or run.usage is None:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_run_evidence_incomplete",
            "Chat run 缺少终态回执、release provenance 或结构化用量。",
        )
    if not run.completed_at:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_run_not_terminal",
            "Chat run 尚未进入持久终态。",
        )
    if not isinstance(coverage_samples, tuple) or not 2 <= len(
        coverage_samples
    ) <= _MAX_COVERAGE_SAMPLES:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_coverage_count_invalid",
            "Heartbeat coverage 样本数必须在 2 到 5000 之间。",
        )
    exposure = window.exposure
    deployment = exposure.deployment
    intent = deployment.preparation.intent
    stage = intent.plan.stages[3]
    member_id = intent.proof.payload.member_id
    if not (
        stage.name.value == "stable"
        and stage.order == 4
        and stage.exposure_percent == 100
        and intent.stable_exposure_percent == 100
        and exposure.binding == run.release_provenance.binding
        and run.release_provenance.workspace_root == window.workspace_root
        and _aware(run.started_at) == _aware(run.release_provenance.run_started_at)
        and _aware(run.completed_at) >= _aware(run.receipt.completed_at)
        and _aware(run.receipt.started_at) >= _aware(exposure.exposed_at)
        and intent.population_snapshot_id == window.population_snapshot_id
        and intent.population_snapshot_sha256 == window.population_snapshot_sha256
        and intent.population_snapshot_sequence == window.population_snapshot_sequence
        and intent.population_denominator == window.population_denominator
        and member_id == window.installation_member_id
    ):
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_release_or_time_mismatch",
            "Chat run 与 Stable release、exposure 或执行时间不一致。",
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
    source = EvolutionRevalidationStableLivenessSourceRef(
        intent_id=intent.intent_id,
        window_id=window.window_id,
        window_sha256=window.window_sha256,
        exposure_id=exposure.exposure_id,
        exposure_sha256=exposure.exposure_sha256,
        deployment_receipt_id=deployment.receipt_id,
        deployment_receipt_sha256=deployment.receipt_sha256,
        binding_id=exposure.binding.binding_id,
        binding_sha256=exposure.binding.binding_sha256,
        candidate_version=intent.candidate_version,
        candidate_target=intent.installation_target,
        exposure_percent=stage.exposure_percent,
        population_snapshot_id=intent.population_snapshot_id,
        population_snapshot_sha256=intent.population_snapshot_sha256,
        population_snapshot_sequence=intent.population_snapshot_sequence,
        population_denominator=intent.population_denominator,
        installation_member_id=member_id,
    )
    successful = bool(receipt.outcome == "completed" and run.status == "completed")
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_POLICY,
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
        "stable_population_observation_input_authority": True,
        "successful_completed_run_authority": successful,
        "stable_completed_run_authority": successful,
        "stable_stage_completion_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationStableExecutionOutcome.model_validate(
            {
                **core,
                "outcome_id": f"evrestableoutcome_{digest[:24]}",
                "outcome_sha256": digest,
            }
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_outcome_invalid",
            "Stable Execution Outcome artifact 无效。",
        ) from exc


def _validate_coverage(
    *,
    provenance: RunReleaseProvenance,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    started_at: datetime,
    completed_at: datetime,
) -> None:
    if not 2 <= len(samples) <= _MAX_COVERAGE_SAMPLES:
        raise ValueError("Stable Execution Outcome coverage 样本数无效。")
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
            raise ValueError("Stable Execution Outcome coverage binding/phase 无效。")
        if previous is not None:
            gap = (
                _aware(sample.observed_at) - _aware(previous.observed_at)
            ).total_seconds()
            if not (
                sample.heartbeat_sequence == previous.heartbeat_sequence + 1
                and sample.previous_sample_sha256 == previous.sample_sha256
                and sample.timeout_seconds == previous.timeout_seconds
                and 0 <= gap <= sample.timeout_seconds
            ):
                raise ValueError("Stable Execution Outcome coverage chain/gap 无效。")
        previous = sample
    if not (
        _aware(samples[0].observed_at) <= started_at
        and _aware(samples[-1].observed_at) >= completed_at
    ):
        raise ValueError("Stable Execution Outcome heartbeat 未覆盖完整运行区间。")


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Execution Outcome timestamp 必须包含 offset。")
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
    "EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_POLICY",
    "EvolutionRevalidationStableExecutionOutcome",
    "EvolutionRevalidationStableExecutionOutcomeError",
    "EvolutionRevalidationStableLivenessSourceRef",
    "build_stable_execution_outcome",
]
