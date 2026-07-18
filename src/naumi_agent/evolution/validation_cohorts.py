"""Non-executable HAR-08 cohort requests for evolution validation."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
    ValidationCheckCoverage,
)

BASELINE_COHORT_REQUEST_POLICY = "evolution-baseline-cohort-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SEED = 9_223_372_036_854_775_807


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class BaselineCohortCheckCase(_StrictModel):
    order: int = Field(ge=1, le=80)
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    spec_sha256: str = Field(pattern=_SHA256_RE)
    argv_sha256: str = Field(pattern=_SHA256_RE)
    timeout_seconds: int = Field(ge=1, le=3_600)
    coverage: tuple[ValidationCheckCoverage, ...] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _coverage_matches_check(self) -> Self:
        keys = tuple((item.path, item.check_kind) for item in self.coverage)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Baseline check coverage 必须排序且不得重复。")
        if any(item.check_id != self.check_id for item in self.coverage):
            raise ValueError("Baseline check coverage 与 check_id 不一致。")
        return self


class BaselineCohortMetricCase(_StrictModel):
    order: int = Field(ge=1, le=8)
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    direction: Literal["decrease", "increase"]
    target: float
    verifier: Literal[
        "harness_replay",
        "self_review_static",
        "feedback_recurrence",
    ]
    procedure_sha256: str = Field(pattern=_SHA256_RE)
    baseline_operation: Literal["measure"] = "measure"


class EvolutionBaselineCohortRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-baseline-cohort-request-v1"] = (
        BASELINE_COHORT_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_requirements_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    phase: Literal["red"] = "red"
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    requested_samples: int = Field(ge=5, le=100)
    base_seed: int = Field(ge=0, le=_MAX_SEED)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    experiment_config_sha256: str = Field(pattern=_SHA256_RE)
    toolset_sha256: str = Field(pattern=_SHA256_RE)
    source_materialization: Literal["arc04_ephemeral_git_worktree"] = (
        "arc04_ephemeral_git_worktree"
    )
    checks: tuple[BaselineCohortCheckCase, ...] = Field(min_length=1, max_length=80)
    metrics: tuple[BaselineCohortMetricCase, ...] = Field(min_length=1, max_length=8)
    check_timeout_seconds_per_sample: int = Field(ge=1, le=288_000)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    runtime_identity_required: Literal[True] = True
    profile_trust_revalidation_required: Literal[True] = True
    metric_timeout_binding_required: Literal[True] = True
    continuous_sample_indexes_required: Literal[True] = True
    harness_result_store_required: Literal[True] = True
    har08_comparison_receipt_required: Literal[True] = True
    candidate_request_allowed: Literal[False] = False
    request_ready: Literal[True] = True
    arc04_worker_required: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_bounded_and_tamper_evident(self) -> Self:
        if len(self.sample_seeds) != self.requested_samples:
            raise ValueError("Baseline sample seed 数量与 requested_samples 不一致。")
        expected_seeds = _sample_seeds(
            self.base_seed,
            self.validation_plan_sha256,
            self.requested_samples,
        )
        if self.sample_seeds != expected_seeds:
            raise ValueError("Baseline sample seeds 与 Plan identity 不一致。")
        if len(set(self.sample_seeds)) != len(self.sample_seeds):
            raise ValueError("Baseline sample seeds 不得重复。")
        if tuple(item.order for item in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Baseline checks 必须按连续顺序排列。")
        check_ids = tuple(item.check_id for item in self.checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("Baseline checks 必须按 ID 排序且不得重复。")
        coverage = tuple(item for check in self.checks for item in check.coverage)
        coverage_keys = tuple((item.path, item.check_kind) for item in coverage)
        if len(coverage_keys) != len(set(coverage_keys)):
            raise ValueError("Baseline coverage 不得重复。")
        ordered_coverage = tuple(
            sorted(coverage, key=lambda item: (item.path, item.check_kind))
        )
        actual_requirements = _sha256_payload([
            {"path": item.path, "check_kind": item.check_kind}
            for item in ordered_coverage
        ])
        if not hmac.compare_digest(
            self.profile_binding_requirements_sha256,
            actual_requirements,
        ):
            raise ValueError("Baseline coverage 与 Profile Binding requirements 不一致。")
        if tuple(item.order for item in self.metrics) != tuple(
            range(1, len(self.metrics) + 1)
        ):
            raise ValueError("Baseline metrics 必须按连续顺序排列。")
        metric_names = tuple(item.metric_name for item in self.metrics)
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("Baseline metrics 不得重复。")
        actual_timeout = sum(item.timeout_seconds for item in self.checks)
        if self.check_timeout_seconds_per_sample != actual_timeout:
            raise ValueError("Baseline check timeout 汇总不一致。")
        if actual_timeout * self.requested_samples > self.max_total_duration_seconds:
            raise ValueError("Baseline cohort check timeout 超过 Experiment 总预算。")
        expected_suite = f"evo_{self.validation_plan_sha256[:24]}"
        expected_batch = f"evo:red:{self.validation_plan_sha256[:24]}"
        if self.suite_id != expected_suite or self.batch_id != expected_batch:
            raise ValueError("Baseline HAR-08 suite/batch identity 不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, expected):
            raise ValueError("Baseline Cohort Request 摘要不一致。")
        if self.request_id != f"evvred_{expected[:24]}":
            raise ValueError("Baseline Cohort Request identity 不一致。")
        return self


class EvolutionCohortRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionBaselineCohortRequestBuilder:
    """Compile trusted validation authority into one non-executable RED request."""

    def build(
        self,
        *,
        contract: EvolutionExperimentContract,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        requested_samples: int = 5,
    ) -> EvolutionBaselineCohortRequest:
        if not isinstance(contract, EvolutionExperimentContract):
            raise TypeError("Baseline Cohort Request 需要 EvolutionExperimentContract。")
        if not isinstance(validation_plan, EvolutionValidationPlan):
            raise TypeError("Baseline Cohort Request 需要 EvolutionValidationPlan。")
        if not isinstance(profile_binding, EvolutionValidationProfileBinding):
            raise TypeError("Baseline Cohort Request 需要 Validation Profile Binding。")
        if isinstance(requested_samples, bool) or not 5 <= requested_samples <= 100:
            raise EvolutionCohortRequestError(
                "baseline_sample_count_invalid",
                "Baseline cohort 样本数必须在 5..100。",
            )
        contract = EvolutionExperimentContract.model_validate(
            contract.model_dump(mode="json")
        )
        validation_plan = EvolutionValidationPlan.model_validate(
            validation_plan.model_dump(mode="json")
        )
        profile_binding = EvolutionValidationProfileBinding.model_validate(
            profile_binding.model_dump(mode="json")
        )
        _require_authority(contract, validation_plan, profile_binding)
        checks = tuple(
            BaselineCohortCheckCase(
                order=index,
                check_id=check.check_id,
                spec_sha256=check.spec_sha256,
                argv_sha256=check.argv_sha256,
                timeout_seconds=check.timeout_seconds,
                coverage=tuple(
                    item for item in profile_binding.coverage
                    if item.check_id == check.check_id
                ),
            )
            for index, check in enumerate(profile_binding.checks, start=1)
        )
        check_timeout = sum(item.timeout_seconds for item in checks)
        if check_timeout * requested_samples > contract.budget.max_duration_seconds:
            raise EvolutionCohortRequestError(
                "baseline_duration_budget_exceeded",
                "Baseline cohort 的 Profile check 最坏耗时超过 Experiment 总预算。",
            )
        metrics = tuple(
            BaselineCohortMetricCase(
                order=item.order,
                metric_name=item.metric_name,
                direction=item.direction,
                target=item.target,
                verifier=item.verifier,
                procedure_sha256=hashlib.sha256(
                    item.procedure.encode("utf-8")
                ).hexdigest(),
            )
            for item in validation_plan.metrics
        )
        payload = {
            "schema_version": 1,
            "policy_version": BASELINE_COHORT_REQUEST_POLICY,
            "contract_id": contract.contract_id,
            "contract_manifest_sha256": contract.manifest_sha256,
            "validation_plan_id": validation_plan.validation_plan_id,
            "validation_plan_sha256": validation_plan.validation_plan_sha256,
            "profile_binding_id": profile_binding.binding_id,
            "profile_binding_sha256": profile_binding.binding_sha256,
            "profile_binding_requirements_sha256": (
                profile_binding.plan_requirements_sha256
            ),
            "candidate_id": validation_plan.candidate_id,
            "candidate_revision": validation_plan.candidate_revision,
            "phase": "red",
            "suite_id": f"evo_{validation_plan.validation_plan_sha256[:24]}",
            "batch_id": f"evo:red:{validation_plan.validation_plan_sha256[:24]}",
            "requested_samples": requested_samples,
            "base_seed": contract.seed,
            "sample_seeds": list(_sample_seeds(
                contract.seed,
                validation_plan.validation_plan_sha256,
                requested_samples,
            )),
            "baseline_commit": validation_plan.baseline_commit,
            "baseline_tree_sha256": validation_plan.baseline_tree_sha256,
            "profile_sha256": validation_plan.profile_sha256,
            "experiment_config_sha256": validation_plan.experiment_config_sha256,
            "toolset_sha256": validation_plan.toolset_sha256,
            "source_materialization": "arc04_ephemeral_git_worktree",
            "checks": [item.model_dump(mode="json") for item in checks],
            "metrics": [item.model_dump(mode="json") for item in metrics],
            "check_timeout_seconds_per_sample": check_timeout,
            "max_total_duration_seconds": contract.budget.max_duration_seconds,
            "network_access": False,
            "dependency_installation": False,
            "runtime_identity_required": True,
            "profile_trust_revalidation_required": True,
            "metric_timeout_binding_required": True,
            "continuous_sample_indexes_required": True,
            "harness_result_store_required": True,
            "har08_comparison_receipt_required": True,
            "candidate_request_allowed": False,
            "request_ready": True,
            "arc04_worker_required": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionBaselineCohortRequest.model_validate({
            **payload,
            "request_id": f"evvred_{digest[:24]}",
            "request_sha256": digest,
        })


def _require_authority(
    contract: EvolutionExperimentContract,
    plan: EvolutionValidationPlan,
    binding: EvolutionValidationProfileBinding,
) -> None:
    if not (
        contract.contract_id == plan.contract_id
        and contract.manifest_sha256 == plan.contract_manifest_sha256
        and contract.source.candidate_id == plan.candidate_id
        and contract.source.candidate_revision == plan.candidate_revision
        and contract.seed == plan.seed
        and contract.baseline.commit == plan.baseline_commit
        and binding.validation_plan_id == plan.validation_plan_id
        and binding.validation_plan_sha256 == plan.validation_plan_sha256
        and binding.profile_sha256 == plan.profile_sha256
        and binding.binding_ready
        and not binding.execution_ready
    ):
        raise EvolutionCohortRequestError(
            "baseline_authority_mismatch",
            "Baseline Cohort Request authority binding 不一致。",
        )


def _sample_seeds(base_seed: int, plan_sha256: str, count: int) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(hashlib.sha256(
            f"{base_seed}:{plan_sha256}:{index}".encode("ascii")
        ).digest()[:8], "big") & _MAX_SEED
        for index in range(count)
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
    "BaselineCohortCheckCase",
    "BaselineCohortMetricCase",
    "EvolutionBaselineCohortRequest",
    "EvolutionBaselineCohortRequestBuilder",
    "EvolutionCohortRequestError",
]
