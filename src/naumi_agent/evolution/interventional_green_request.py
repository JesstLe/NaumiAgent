"""Tamper-evident requests for interventional candidate cohorts."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortReceipt,
)
from naumi_agent.evolution.interventional_red_sample import (
    EvolutionInterventionalRedSampleError,
    validate_interventional_red_authority,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)

INTERVENTIONAL_GREEN_REQUEST_POLICY = "evolution-interventional-green-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionInterventionalGreenCohortRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-interventional-green-request-v1"] = (
        INTERVENTIONAL_GREEN_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^evvgreenint_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(pattern=r"^evvredcohort_[0-9a-f]{24}$")
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    phase: Literal["green"] = "green"
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    requested_samples: int = Field(ge=5, le=100)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    worktree_name: str = Field(pattern=r"^experiment-[0-9a-f]{16}$")
    branch: str = Field(min_length=1, max_length=255)
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    check_timeout_seconds_per_sample: int = Field(ge=1, le=3_600)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    red_cohort_complete: Literal[True] = True
    same_suite_required: Literal[True] = True
    same_seed_required: Literal[True] = True
    same_platform_required: Literal[True] = True
    candidate_snapshot_revalidation_required: Literal[True] = True
    profile_trust_revalidation_required: Literal[True] = True
    cohort_run_grant_required: Literal[True] = True
    harness_result_store_required: Literal[True] = True
    project_code_execution_allowed: Literal[True] = True
    arc04_worker_required: Literal[True] = True
    execution_ready: Literal[False] = False

    @field_validator("branch")
    @classmethod
    def _safe_branch(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r", "\n")):
            raise ValueError("Interventional GREEN branch 格式无效。")
        return normalized

    @model_validator(mode="after")
    def _request_is_complete_and_tamper_evident(self) -> Self:
        if len(self.sample_seeds) != self.requested_samples:
            raise ValueError("Interventional GREEN sample seed 数量不完整。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, expected):
            raise ValueError("Interventional GREEN Request 摘要不一致。")
        if self.request_id != f"evvgreenint_{expected[:24]}":
            raise ValueError("Interventional GREEN Request identity 不一致。")
        return self


class EvolutionInterventionalGreenRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalGreenCohortRequestBuilder:
    """Freeze RED evidence and candidate Lease authority before execution."""

    def build(
        self,
        *,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionInterventionalGreenCohortRequest:
        try:
            request, binding, plan, profile = validate_interventional_red_authority(
                baseline_request,
                metric_binding,
                validation_plan,
                profile_binding,
            )
            red = EvolutionInterventionalRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            EvolutionInterventionalRedSampleError,
        ) as exc:
            raise EvolutionInterventionalGreenRequestError(
                "interventional_green_authority_invalid",
                "Interventional GREEN Request authority 无效或已被篡改。",
            ) from exc
        _require_authority(request, binding, plan, profile, red, candidate_lease)
        payload = {
            "schema_version": 1,
            "policy_version": INTERVENTIONAL_GREEN_REQUEST_POLICY,
            "baseline_request_id": request.request_id,
            "baseline_request_sha256": request.request_sha256,
            "red_receipt_id": red.receipt_id,
            "red_receipt_sha256": red.receipt_sha256,
            "metric_binding_id": binding.binding_id,
            "metric_binding_sha256": binding.binding_sha256,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "profile_binding_id": profile.binding_id,
            "profile_binding_sha256": profile.binding_sha256,
            "contract_id": plan.contract_id,
            "contract_manifest_sha256": plan.contract_manifest_sha256,
            "lease_id": candidate_lease.lease_id,
            "source_snapshot_id": plan.source_snapshot_id,
            "source_snapshot_sha256": plan.source_snapshot_sha256,
            "mutation_receipt_id": plan.mutation_receipt_id,
            "mutation_receipt_sha256": plan.mutation_receipt_sha256,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "candidate_files_sha256": plan.candidate_files_sha256,
            "phase": "green",
            "suite_id": request.suite_id,
            "batch_id": f"evo:interventional-green:{plan.validation_plan_sha256[:24]}",
            "requested_samples": request.requested_samples,
            "sample_seeds": list(request.sample_seeds),
            "worktree_name": candidate_lease.worktree_name,
            "branch": candidate_lease.branch,
            "baseline_commit": candidate_lease.baseline_commit,
            "check_timeout_seconds_per_sample": request.check_timeout_seconds_per_sample,
            "max_total_duration_seconds": request.max_total_duration_seconds,
            "network_access": False,
            "dependency_installation": False,
            "red_cohort_complete": True,
            "same_suite_required": True,
            "same_seed_required": True,
            "same_platform_required": True,
            "candidate_snapshot_revalidation_required": True,
            "profile_trust_revalidation_required": True,
            "cohort_run_grant_required": True,
            "harness_result_store_required": True,
            "project_code_execution_allowed": True,
            "arc04_worker_required": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionInterventionalGreenCohortRequest.model_validate({
            **payload,
            "request_id": f"evvgreenint_{digest[:24]}",
            "request_sha256": digest,
        })


def _require_authority(request, binding, plan, profile, red, lease) -> None:
    if not (
        red.baseline_request_id == request.request_id
        and red.baseline_request_sha256 == request.request_sha256
        and red.metric_binding_id == binding.binding_id
        and red.metric_binding_sha256 == binding.binding_sha256
        and red.validation_plan_id == plan.validation_plan_id
        and red.validation_plan_sha256 == plan.validation_plan_sha256
        and red.profile_binding_id == profile.binding_id
        and red.profile_binding_sha256 == profile.binding_sha256
        and red.suite_id == request.suite_id
        and red.batch_id == request.batch_id
        and red.requested_samples == red.persisted_samples == request.requested_samples
        and red.sample_seeds == request.sample_seeds
        and lease.state is ExperimentLeaseState.ACTIVE
        and lease.worktree_ready
        and not lease.execution_ready
        and lease.lease_id == plan.lease_id
        and lease.contract_id == plan.contract_id
        and lease.manifest_sha256 == plan.contract_manifest_sha256
        and lease.baseline_commit == plan.baseline_commit
    ):
        raise EvolutionInterventionalGreenRequestError(
            "interventional_green_authority_mismatch",
            "Interventional GREEN 的 RED、Plan、Profile 与 Lease authority 不一致。",
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


__all__ = [
    "EvolutionInterventionalGreenCohortRequest",
    "EvolutionInterventionalGreenCohortRequestBuilder",
    "EvolutionInterventionalGreenRequestError",
    "INTERVENTIONAL_GREEN_REQUEST_POLICY",
]
