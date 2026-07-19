"""Non-executable matrix requests for governed adversarial probe batches."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.adversarial_probe_contracts import (
    AdversarialProbeCheckBinding,
    AdversarialProbeCoverage,
    AdversarialProbeKind,
    AdversarialProbeRequirement,
    EvolutionAdversarialProbeContract,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan

ADVERSARIAL_BATCH_REQUEST_POLICY = "evolution-adversarial-batch-request-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_CODE_RE = r"^[a-z][a-z0-9_-]{0,63}$"
_MAX_SEED = 9_223_372_036_854_775_807

type AdversarialBatchPlatform = Literal["linux", "macos", "windows"]
type AdversarialBatchPhase = Literal["red", "green"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class AdversarialBatchProbeCase(_StrictModel):
    order: int = Field(ge=1, le=96)
    path: str = Field(min_length=1, max_length=1_024)
    probe_id: str = Field(pattern=_SAFE_CODE_RE)
    kind: AdversarialProbeKind
    evidence_type: Literal["harness_check_receipt"]
    success_rule: Literal["exit_zero"]
    platform_scope: Literal["current", "matrix"]
    check_id: str = Field(pattern=_SAFE_CODE_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Adversarial Batch probe path 必须是安全相对路径。")
        return normalized


class AdversarialBatchCheckCase(_StrictModel):
    order: int = Field(ge=1, le=80)
    check_id: str = Field(pattern=_SAFE_CODE_RE)
    spec_sha256: str = Field(pattern=_SHA256_RE)
    argv_sha256: str = Field(pattern=_SHA256_RE)
    timeout_seconds: int = Field(ge=1, le=3_600)
    probes: tuple[AdversarialProbeKind, ...] = Field(min_length=1, max_length=6)
    coverage: tuple[AdversarialProbeCoverage, ...] = Field(min_length=1, max_length=96)

    @model_validator(mode="after")
    def _coverage_matches_check(self) -> Self:
        if self.probes != tuple(sorted(set(self.probes))):
            raise ValueError("Adversarial Batch check probes 必须排序且不得重复。")
        keys = tuple((item.path, item.kind, item.probe_id) for item in self.coverage)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Adversarial Batch check coverage 必须排序且不得重复。")
        if any(
            item.check_id != self.check_id or item.kind not in self.probes
            for item in self.coverage
        ):
            raise ValueError("Adversarial Batch check coverage 与能力不一致。")
        return self


class AdversarialBatchLane(_StrictModel):
    order: int = Field(ge=1, le=6)
    platform: AdversarialBatchPlatform
    phase: AdversarialBatchPhase
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    max_duration_seconds: int = Field(ge=60, le=3_600)


class EvolutionAdversarialBatchRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-adversarial-batch-request-v1"] = (
        ADVERSARIAL_BATCH_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^evadvreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    probe_contract_id: str = Field(pattern=r"^evapc_[0-9a-f]{24}$")
    probe_contract_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    registry_sha256: str = Field(pattern=_SHA256_RE)
    probe_platform_sha256: str = Field(pattern=_SHA256_RE)
    origin_platform: Literal["linux", "macos", "windows", "unknown"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    experiment_config_sha256: str = Field(pattern=_SHA256_RE)
    toolset_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=_SAFE_CODE_RE)
    requested_samples: int = Field(ge=5, le=100)
    base_seed: int = Field(ge=0, le=_MAX_SEED)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    required_platforms: tuple[AdversarialBatchPlatform, ...] = Field(
        min_length=1,
        max_length=3,
    )
    phases: tuple[AdversarialBatchPhase, ...] = ("red", "green")
    probes: tuple[AdversarialBatchProbeCase, ...] = Field(
        min_length=1,
        max_length=96,
    )
    checks: tuple[AdversarialBatchCheckCase, ...] = Field(
        min_length=1,
        max_length=80,
    )
    lanes: tuple[AdversarialBatchLane, ...] = Field(min_length=2, max_length=6)
    check_timeout_seconds_per_sample: int = Field(ge=1, le=288_000)
    lane_budget_seconds: int = Field(ge=60, le=3_600)
    matrix_budget_seconds: int = Field(ge=120, le=21_600)
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    red_green_pair_required: Literal[True] = True
    same_probe_order_required: Literal[True] = True
    same_seed_required: Literal[True] = True
    platform_receipt_required: Literal[True] = True
    candidate_snapshot_revalidation_required: Literal[True] = True
    profile_trust_revalidation_required: Literal[True] = True
    batch_run_grant_required: Literal[True] = True
    continuous_sample_indexes_required: Literal[True] = True
    harness_result_store_required: Literal[True] = True
    har08_comparison_receipt_required: Literal[True] = True
    project_code_execution_allowed: Literal[True] = True
    request_ready: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_bounded_ordered_and_tamper_evident(self) -> Self:
        if self.phases != ("red", "green"):
            raise ValueError("Adversarial Batch phases 必须固定为 RED→GREEN。")
        if self.required_platforms != tuple(sorted(set(self.required_platforms))):
            raise ValueError("Adversarial Batch platforms 必须排序且不得重复。")
        if len(self.sample_seeds) != self.requested_samples:
            raise ValueError("Adversarial Batch sample seeds 数量不一致。")
        expected_seeds = _sample_seeds(
            self.base_seed,
            self.probe_contract_sha256,
            self.requested_samples,
        )
        if self.sample_seeds != expected_seeds or len(set(self.sample_seeds)) != len(
            self.sample_seeds
        ):
            raise ValueError("Adversarial Batch sample seeds 与 Probe Contract 不一致。")
        if tuple(item.order for item in self.probes) != tuple(
            range(1, len(self.probes) + 1)
        ):
            raise ValueError("Adversarial Batch probes 必须按连续顺序排列。")
        probe_keys = tuple(
            (item.path, item.kind, item.probe_id) for item in self.probes
        )
        if probe_keys != tuple(sorted(set(probe_keys))):
            raise ValueError("Adversarial Batch probes 必须排序且不得重复。")
        matrix_required = any(item.platform_scope == "matrix" for item in self.probes)
        if matrix_required and self.required_platforms != ("linux", "macos", "windows"):
            raise ValueError("Adversarial Batch matrix probe 必须覆盖三平台。")
        if not matrix_required and (
            self.origin_platform == "unknown"
            or self.required_platforms != (self.origin_platform,)
        ):
            raise ValueError("Adversarial Batch current probe 与 origin platform 不一致。")
        if tuple(item.order for item in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Adversarial Batch checks 必须按连续顺序排列。")
        check_ids = tuple(item.check_id for item in self.checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("Adversarial Batch checks 必须按 ID 排序且不得重复。")
        coverage = tuple(item for check in self.checks for item in check.coverage)
        coverage_keys = tuple(
            (item.path, item.kind, item.probe_id) for item in coverage
        )
        if set(coverage_keys) != set(probe_keys) or len(coverage_keys) != len(
            set(coverage_keys)
        ):
            raise ValueError("Adversarial Batch coverage 未精确覆盖 probes。")
        probe_check_ids = {
            (item.path, item.kind, item.probe_id): item.check_id for item in self.probes
        }
        if any(
            probe_check_ids[(item.path, item.kind, item.probe_id)] != item.check_id
            for item in coverage
        ):
            raise ValueError("Adversarial Batch probe/check mapping 不一致。")
        expected_lanes = tuple(
            (platform, phase)
            for platform in self.required_platforms
            for phase in self.phases
        )
        actual_lanes = tuple((item.platform, item.phase) for item in self.lanes)
        if actual_lanes != expected_lanes or tuple(item.order for item in self.lanes) != tuple(
            range(1, len(self.lanes) + 1)
        ):
            raise ValueError("Adversarial Batch lanes 未精确覆盖 platform/phase matrix。")
        expected_suite = f"evo_adv_{self.probe_contract_sha256[:20]}"
        if self.suite_id != expected_suite:
            raise ValueError("Adversarial Batch suite identity 不一致。")
        expected_batch_ids = tuple(
            f"evo:adversarial:{platform}:{phase}:{self.probe_contract_sha256[:16]}"
            for platform, phase in expected_lanes
        )
        if tuple(item.batch_id for item in self.lanes) != expected_batch_ids:
            raise ValueError("Adversarial Batch lane identity 不一致。")
        actual_timeout = sum(item.timeout_seconds for item in self.checks)
        if self.check_timeout_seconds_per_sample != actual_timeout:
            raise ValueError("Adversarial Batch check timeout 汇总不一致。")
        expected_lane_budget = max(60, actual_timeout * self.requested_samples)
        if self.lane_budget_seconds != expected_lane_budget or any(
            item.max_duration_seconds != expected_lane_budget for item in self.lanes
        ):
            raise ValueError("Adversarial Batch lane budget 不一致。")
        expected_matrix_budget = expected_lane_budget * len(self.lanes)
        if self.matrix_budget_seconds != expected_matrix_budget:
            raise ValueError("Adversarial Batch matrix budget 不一致。")
        if self.matrix_budget_seconds > self.max_total_duration_seconds:
            raise ValueError("Adversarial Batch matrix 超过 Experiment 总预算。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, expected):
            raise ValueError("Adversarial Batch Request 摘要不一致。")
        if self.request_id != f"evadvreq_{expected[:24]}":
            raise ValueError("Adversarial Batch Request identity 不一致。")
        return self


class EvolutionAdversarialBatchRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionAdversarialBatchRequestBuilder:
    """Freeze complete probe authority into a bounded RED/GREEN platform matrix."""

    def build(
        self,
        *,
        experiment_contract: EvolutionExperimentContract,
        validation_plan: EvolutionValidationPlan,
        probe_contract: EvolutionAdversarialProbeContract,
        requested_samples: int = 5,
    ) -> EvolutionAdversarialBatchRequest:
        if not isinstance(experiment_contract, EvolutionExperimentContract):
            raise TypeError("Adversarial Batch Request 需要 Experiment Contract。")
        if not isinstance(validation_plan, EvolutionValidationPlan):
            raise TypeError("Adversarial Batch Request 需要 Validation Plan。")
        if not isinstance(probe_contract, EvolutionAdversarialProbeContract):
            raise TypeError("Adversarial Batch Request 需要 Adversarial Probe Contract。")
        if isinstance(requested_samples, bool) or not 5 <= requested_samples <= 100:
            raise EvolutionAdversarialBatchRequestError(
                "adversarial_sample_count_invalid",
                "Adversarial Batch 样本数必须在 5..100。",
            )
        try:
            contract = EvolutionExperimentContract.model_validate(
                experiment_contract.model_dump(mode="json")
            )
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            probes = EvolutionAdversarialProbeContract.model_validate(
                probe_contract.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialBatchRequestError(
                "adversarial_authority_invalid",
                "Adversarial Batch Request authority 无效或已被篡改。",
            ) from exc
        _require_authority(contract, plan, probes)
        if not probes.coverage_complete or probes.blockers:
            raise EvolutionAdversarialBatchRequestError(
                "adversarial_probe_coverage_incomplete",
                "Adversarial Probe Contract 仍有缺失或歧义 check，不能创建 Batch Request。",
            )
        platforms = _required_platforms(probes)
        phases: tuple[AdversarialBatchPhase, ...] = ("red", "green")
        probe_cases = _probe_cases(probes.requirements, probes.coverage)
        check_cases = _check_cases(probes.checks, probes.coverage)
        check_timeout = sum(item.timeout_seconds for item in check_cases)
        lane_budget = max(60, check_timeout * requested_samples)
        matrix_budget = lane_budget * len(platforms) * len(phases)
        if lane_budget > 3_600 or matrix_budget > contract.budget.max_duration_seconds:
            raise EvolutionAdversarialBatchRequestError(
                "adversarial_duration_budget_exceeded",
                "Adversarial RED/GREEN 平台矩阵的最坏 check 耗时超过 Experiment 总预算。",
            )
        lanes = tuple(
            AdversarialBatchLane(
                order=index,
                platform=platform,
                phase=phase,
                batch_id=(
                    f"evo:adversarial:{platform}:{phase}:"
                    f"{probes.probe_contract_sha256[:16]}"
                ),
                max_duration_seconds=lane_budget,
            )
            for index, (platform, phase) in enumerate(
                (
                    (platform, phase)
                    for platform in platforms
                    for phase in phases
                ),
                start=1,
            )
        )
        payload = {
            "schema_version": 1,
            "policy_version": ADVERSARIAL_BATCH_REQUEST_POLICY,
            "contract_id": contract.contract_id,
            "contract_manifest_sha256": contract.manifest_sha256,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "probe_contract_id": probes.probe_contract_id,
            "probe_contract_sha256": probes.probe_contract_sha256,
            "profile_binding_id": probes.profile_binding_id,
            "profile_binding_sha256": probes.profile_binding_sha256,
            "profile_sha256": probes.profile_sha256,
            "registry_sha256": probes.registry_sha256,
            "probe_platform_sha256": probes.platform_sha256,
            "origin_platform": probes.platform_identity.system,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "candidate_files_sha256": plan.candidate_files_sha256,
            "lease_id": plan.lease_id,
            "source_snapshot_id": plan.source_snapshot_id,
            "source_snapshot_sha256": plan.source_snapshot_sha256,
            "mutation_receipt_id": plan.mutation_receipt_id,
            "mutation_receipt_sha256": plan.mutation_receipt_sha256,
            "baseline_commit": plan.baseline_commit,
            "baseline_tree_sha256": plan.baseline_tree_sha256,
            "experiment_config_sha256": plan.experiment_config_sha256,
            "toolset_sha256": plan.toolset_sha256,
            "suite_id": f"evo_adv_{probes.probe_contract_sha256[:20]}",
            "requested_samples": requested_samples,
            "base_seed": contract.seed,
            "sample_seeds": list(_sample_seeds(
                contract.seed,
                probes.probe_contract_sha256,
                requested_samples,
            )),
            "required_platforms": list(platforms),
            "phases": list(phases),
            "probes": [item.model_dump(mode="json") for item in probe_cases],
            "checks": [item.model_dump(mode="json") for item in check_cases],
            "lanes": [item.model_dump(mode="json") for item in lanes],
            "check_timeout_seconds_per_sample": check_timeout,
            "lane_budget_seconds": lane_budget,
            "matrix_budget_seconds": matrix_budget,
            "max_total_duration_seconds": contract.budget.max_duration_seconds,
            "network_access": False,
            "dependency_installation": False,
            "red_green_pair_required": True,
            "same_probe_order_required": True,
            "same_seed_required": True,
            "platform_receipt_required": True,
            "candidate_snapshot_revalidation_required": True,
            "profile_trust_revalidation_required": True,
            "batch_run_grant_required": True,
            "continuous_sample_indexes_required": True,
            "harness_result_store_required": True,
            "har08_comparison_receipt_required": True,
            "project_code_execution_allowed": True,
            "request_ready": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionAdversarialBatchRequest.model_validate({
            **payload,
            "request_id": f"evadvreq_{digest[:24]}",
            "request_sha256": digest,
        })


def _require_authority(
    contract: EvolutionExperimentContract,
    plan: EvolutionValidationPlan,
    probes: EvolutionAdversarialProbeContract,
) -> None:
    if not (
        contract.contract_id == plan.contract_id
        and contract.manifest_sha256 == plan.contract_manifest_sha256
        and contract.source.candidate_id == plan.candidate_id
        and contract.source.candidate_revision == plan.candidate_revision
        and contract.seed == plan.seed
        and contract.baseline.commit == plan.baseline_commit
        and probes.validation_plan_id == plan.validation_plan_id
        and probes.validation_plan_sha256 == plan.validation_plan_sha256
        and probes.profile_sha256 == plan.profile_sha256
        and probes.candidate_id == plan.candidate_id
        and probes.candidate_revision == plan.candidate_revision
        and probes.candidate_files_sha256 == plan.candidate_files_sha256
        and probes.har08_batch_required
        and probes.runner_binding_status == "required"
        and not probes.execution_ready
    ):
        raise EvolutionAdversarialBatchRequestError(
            "adversarial_authority_mismatch",
            "Experiment、Validation Plan 与 Probe Contract authority 不一致。",
        )


def _required_platforms(
    contract: EvolutionAdversarialProbeContract,
) -> tuple[AdversarialBatchPlatform, ...]:
    if any(item.platform_scope == "matrix" for item in contract.requirements):
        return ("linux", "macos", "windows")
    current = contract.platform_identity.system
    if current not in {"linux", "macos", "windows"}:
        raise EvolutionAdversarialBatchRequestError(
            "adversarial_platform_unknown",
            "当前平台身份未知，不能创建 Adversarial Batch Request。",
        )
    return (current,)


def _probe_cases(
    requirements: tuple[AdversarialProbeRequirement, ...],
    coverage: tuple[AdversarialProbeCoverage, ...],
) -> tuple[AdversarialBatchProbeCase, ...]:
    coverage_by_key = {
        (item.path, item.kind, item.probe_id): item for item in coverage
    }
    return tuple(
        AdversarialBatchProbeCase(
            order=index,
            path=item.path,
            probe_id=item.probe_id,
            kind=item.kind,
            evidence_type=item.evidence_type,
            success_rule=item.success_rule,
            platform_scope=item.platform_scope,
            check_id=coverage_by_key[(item.path, item.kind, item.probe_id)].check_id,
        )
        for index, item in enumerate(requirements, start=1)
    )


def _check_cases(
    checks: tuple[AdversarialProbeCheckBinding, ...],
    coverage: tuple[AdversarialProbeCoverage, ...],
) -> tuple[AdversarialBatchCheckCase, ...]:
    return tuple(
        AdversarialBatchCheckCase(
            order=index,
            check_id=check.check_id,
            spec_sha256=check.spec_sha256,
            argv_sha256=check.argv_sha256,
            timeout_seconds=check.timeout_seconds,
            probes=check.probes,
            coverage=tuple(
                item for item in coverage if item.check_id == check.check_id
            ),
        )
        for index, check in enumerate(checks, start=1)
    )


def _sample_seeds(base_seed: int, authority_sha256: str, count: int) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(
            hashlib.sha256(
                f"{base_seed}:{authority_sha256}:{index}".encode("ascii")
            ).digest()[:8],
            "big",
        )
        & _MAX_SEED
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
    "ADVERSARIAL_BATCH_REQUEST_POLICY",
    "AdversarialBatchCheckCase",
    "AdversarialBatchLane",
    "AdversarialBatchPhase",
    "AdversarialBatchPlatform",
    "AdversarialBatchProbeCase",
    "EvolutionAdversarialBatchRequest",
    "EvolutionAdversarialBatchRequestBuilder",
    "EvolutionAdversarialBatchRequestError",
]
