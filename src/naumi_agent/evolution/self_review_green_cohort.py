"""Trusted Self-Review GREEN cohorts from one active candidate Lease."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.candidate_snapshots import (
    EvolutionCandidateSnapshotError,
    capture_candidate_worktree_snapshot,
    revalidate_candidate_worktree_snapshot,
)
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.self_review import SelfReviewFindingCode
from naumi_agent.evolution.self_review_eval_runtime import (
    SelfReviewEvalRuntimeError,
    build_self_review_eval_configuration,
    require_continuous_eval_prefix,
    run_self_review_static_repetitions,
    validate_self_review_cohort_authority,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedCohortReceipt,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import HarnessEvalSuiteResult
from naumi_agent.harness.fingerprint import TreeFingerprint
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoredEvalResult,
)
from naumi_agent.harness.trust import HarnessTrustStore

SELF_REVIEW_GREEN_REQUEST_POLICY = "evolution-self-review-green-request-v1"
SELF_REVIEW_GREEN_COHORT_POLICY = "evolution-self-review-green-cohort-v1"
_GIT_TIMEOUT_SECONDS = 10
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionSelfReviewGreenCohortRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-self-review-green-request-v1"] = (
        SELF_REVIEW_GREEN_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^evvgreen_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(pattern=r"^evvredrun_[0-9a-f]{24}$")
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
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
    candidate_request_allowed: Literal[True] = True
    red_cohort_complete: Literal[True] = True
    same_fixture_required: Literal[True] = True
    same_seed_required: Literal[True] = True
    profile_trust_revalidation_required: Literal[True] = True
    harness_result_store_required: Literal[True] = True
    project_code_execution_allowed: Literal[False] = False
    arc04_worker_required: Literal[False] = False
    static_execution_ready: Literal[True] = True

    @field_validator("branch")
    @classmethod
    def _safe_branch(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r", "\n")):
            raise ValueError("GREEN request branch 格式无效。")
        return normalized

    @model_validator(mode="after")
    def _request_is_tamper_evident(self) -> Self:
        if len(self.sample_seeds) != self.requested_samples:
            raise ValueError("GREEN request sample seed 数量不完整。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        )
        if not hmac.compare_digest(self.request_sha256, expected):
            raise ValueError("GREEN Cohort Request 摘要不一致。")
        if self.request_id != f"evvgreen_{expected[:24]}":
            raise ValueError("GREEN Cohort Request identity 不一致。")
        return self


class SelfReviewGreenMetricSummary(_StrictModel):
    metric_name: str = Field(pattern=r"^self_review\.[a-z][a-z0-9_]*\.count$")
    finding_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    direction: Literal["decrease"]
    target: float
    sample_values: tuple[int, ...] = Field(min_length=5, max_length=100)

    @model_validator(mode="after")
    def _summary_is_consistent(self) -> Self:
        if (
            self.metric_name != f"self_review.{self.finding_code}.count"
            or self.finding_code not in {item.value for item in SelfReviewFindingCode}
            or self.target < 0
            or not float(self.target).is_integer()
            or any(isinstance(value, bool) or value < 0 for value in self.sample_values)
        ):
            raise ValueError("Self-Review GREEN metric summary 合同不一致。")
        return self


class EvolutionSelfReviewGreenCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-self-review-green-cohort-v1"] = (
        SELF_REVIEW_GREEN_COHORT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvgreenrun_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    green_request_id: str = Field(pattern=r"^evvgreen_[0-9a-f]{24}$")
    green_request_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(pattern=r"^evvredrun_[0-9a-f]{24}$")
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    phase: Literal["green"] = "green"
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    candidate_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidate_tree_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    metrics: tuple[SelfReviewGreenMetricSummary, ...] = Field(min_length=1, max_length=8)
    profile_trust_revalidated: Literal[True] = True
    candidate_status_revalidated: Literal[True] = True
    model_access: Literal[False] = False
    network_access: Literal[False] = False
    project_code_executed: Literal[False] = False
    arc04_worker_used: Literal[False] = False
    cohort_complete: Literal[True] = True
    completed_at: str

    @field_validator("sample_result_sha256")
    @classmethod
    def _valid_result_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("GREEN sample digest 格式无效。")
        return values

    @field_validator("completed_at")
    @classmethod
    def _aware_completed_at(cls, value: str) -> str:
        return _aware_time(value).isoformat()

    @model_validator(mode="after")
    def _receipt_is_complete_and_tamper_evident(self) -> Self:
        if not (
            self.persisted_samples == self.requested_samples
            == len(self.sample_result_sha256)
        ):
            raise ValueError("Self-Review GREEN cohort 样本汇总不完整。")
        if any(
            len(item.sample_values) != self.requested_samples
            for item in self.metrics
        ):
            raise ValueError("Self-Review GREEN metric 样本数量不完整。")
        names = tuple(item.metric_name for item in self.metrics)
        if len(names) != len(set(names)):
            raise ValueError("Self-Review GREEN metric summary 不得重复。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Self-Review GREEN receipt 摘要不一致。")
        if self.receipt_id != f"evvgreenrun_{expected[:24]}":
            raise ValueError("Self-Review GREEN receipt identity 不一致。")
        return self


class EvolutionSelfReviewGreenCohortError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionSelfReviewGreenCohortRequestBuilder:
    def build(
        self,
        *,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionSelfReviewRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionSelfReviewGreenCohortRequest:
        try:
            request, binding, plan = validate_self_review_cohort_authority(
                baseline_request,
                metric_binding,
                validation_plan,
            )
            red = EvolutionSelfReviewRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError, SelfReviewEvalRuntimeError) as exc:
            raise EvolutionSelfReviewGreenCohortError(
                "green_request_authority_invalid",
                "GREEN Cohort Request authority 无效或已被篡改。",
            ) from exc
        _require_green_authority(request, binding, plan, red, candidate_lease)
        payload = {
            "schema_version": 1,
            "policy_version": SELF_REVIEW_GREEN_REQUEST_POLICY,
            "baseline_request_id": request.request_id,
            "baseline_request_sha256": request.request_sha256,
            "red_receipt_id": red.receipt_id,
            "red_receipt_sha256": red.receipt_sha256,
            "metric_binding_id": binding.binding_id,
            "metric_binding_sha256": binding.binding_sha256,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "lease_id": candidate_lease.lease_id,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "candidate_files_sha256": plan.candidate_files_sha256,
            "phase": "green",
            "suite_id": request.suite_id,
            "batch_id": f"evo:green:{plan.validation_plan_sha256[:24]}",
            "requested_samples": request.requested_samples,
            "sample_seeds": list(request.sample_seeds),
            "worktree_name": candidate_lease.worktree_name,
            "branch": candidate_lease.branch,
            "candidate_request_allowed": True,
            "red_cohort_complete": True,
            "same_fixture_required": True,
            "same_seed_required": True,
            "profile_trust_revalidation_required": True,
            "harness_result_store_required": True,
            "project_code_execution_allowed": False,
            "arc04_worker_required": False,
            "static_execution_ready": True,
        }
        digest = _sha256_payload(payload)
        return EvolutionSelfReviewGreenCohortRequest.model_validate({
            **payload,
            "request_id": f"evvgreen_{digest[:24]}",
            "request_sha256": digest,
        })


class EvolutionSelfReviewGreenCohortExecutor:
    def __init__(
        self,
        *,
        store: HarnessStore,
        trust_store: HarnessTrustStore,
        lease_store: EvolutionExperimentLeaseStore,
        worktree_storage_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("GREEN executor 需要 HarnessStore。")
        if not isinstance(trust_store, HarnessTrustStore):
            raise TypeError("GREEN executor 需要 HarnessTrustStore。")
        if not isinstance(lease_store, EvolutionExperimentLeaseStore):
            raise TypeError("GREEN executor 需要 EvolutionExperimentLeaseStore。")
        self._store = store
        self._trust_store = trust_store
        self._lease_store = lease_store
        self._worktree_storage_dir = Path(worktree_storage_dir).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        green_request: EvolutionSelfReviewGreenCohortRequest,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionSelfReviewRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionSelfReviewGreenCohortReceipt:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise EvolutionSelfReviewGreenCohortError(
                "green_workspace_invalid",
                "GREEN workspace 不存在或无法读取。",
            ) from exc
        _require_workspace_repository_root(workspace)
        try:
            request, binding, plan = validate_self_review_cohort_authority(
                baseline_request,
                metric_binding,
                validation_plan,
            )
            red = EvolutionSelfReviewRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
            green = EvolutionSelfReviewGreenCohortRequest.model_validate(
                green_request.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError, OSError, SelfReviewEvalRuntimeError) as exc:
            raise EvolutionSelfReviewGreenCohortError(
                "green_authority_invalid",
                "Self-Review GREEN authority 无效或已被篡改。",
            ) from exc
        _require_green_authority(request, binding, plan, red, candidate_lease)
        _require_green_request(green, request, binding, plan, red, candidate_lease)
        current_lease = await self._lease_store.get(candidate_lease.contract_id)
        if current_lease != candidate_lease:
            raise EvolutionSelfReviewGreenCohortError(
                "candidate_lease_stale",
                "Candidate Lease 已变化或不再存在。",
            )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EvolutionSelfReviewGreenCohortError(
                "green_clock_invalid",
                "GREEN executor 时钟必须包含时区。",
            )
        if _aware_time(candidate_lease.expires_at) <= now:
            raise EvolutionSelfReviewGreenCohortError(
                "candidate_lease_expired",
                "Candidate Lease 已过期，不能生成 GREEN evidence。",
            )
        if not await self._trust_store.is_trusted(workspace, request.profile_sha256):
            raise EvolutionSelfReviewGreenCohortError(
                "profile_trust_revalidation_failed",
                "Harness Profile 信任已失效，不能执行 GREEN cohort。",
            )
        red_records = await _load_and_validate_red_records(
            self._store,
            workspace,
            request,
            binding,
            plan,
            red,
        )
        try:
            candidate_snapshot = capture_candidate_worktree_snapshot(
                candidate_lease,
                plan,
                worktree_storage_dir=self._worktree_storage_dir,
                now=now,
            )
        except EvolutionCandidateSnapshotError as exc:
            raise EvolutionSelfReviewGreenCohortError(exc.code, str(exc)) from exc
        candidate_root = candidate_snapshot.root
        blobs = candidate_snapshot.blobs
        fingerprint = candidate_snapshot.fingerprint
        platform = capture_eval_platform_identity()
        red_identity = red_records[0].result.baseline_identity
        if red_identity is None or red_identity.platform != platform:
            raise EvolutionSelfReviewGreenCohortError(
                "green_platform_mismatch",
                "GREEN 平台身份已偏离 RED cohort。",
            )
        configuration = build_self_review_eval_configuration(request, binding, plan)
        if red_identity.configuration != configuration:
            raise EvolutionSelfReviewGreenCohortError(
                "red_configuration_mismatch",
                "RED cohort configuration 与 GREEN Request 不一致。",
            )
        identity = build_eval_baseline_identity(
            candidate_root,
            configuration=configuration,
            platform_identity=platform,
            profile_trusted=True,
            source_identity=HarnessEvalSourceIdentity(
                commit=fingerprint.head,
                tree_sha256=fingerprint.digest,
                dirty=True,
            ),
        )
        results = await _run_green_repetitions(
            blobs=blobs,
            request=request,
            binding=binding,
            plan=plan,
            configuration=configuration,
            identity=identity,
        )
        try:
            revalidate_candidate_worktree_snapshot(candidate_snapshot)
        except EvolutionCandidateSnapshotError as exc:
            if exc.code == "candidate_fingerprint_read_failed":
                raise EvolutionSelfReviewGreenCohortError(exc.code, str(exc)) from exc
            raise EvolutionSelfReviewGreenCohortError(
                "candidate_worktree_changed_during_scan",
                "Candidate worktree 在 GREEN 扫描期间发生变化。",
            ) from exc
        existing = await self._store.list_eval_results(
            workspace,
            green.batch_id,
            green.suite_id,
            limit=green.requested_samples + 1,
        )
        _require_prefix(existing, green.requested_samples)
        for stored, expected in zip(existing, results, strict=False):
            if stored.result.canonical_payload() != expected.canonical_payload():
                raise EvolutionSelfReviewGreenCohortError(
                    "existing_green_cohort_conflict",
                    "已有 GREEN sample 与当前可信 candidate 不一致。",
                )
        if len(existing) < green.requested_samples:
            created_at = now.isoformat()
            for index in range(len(existing), green.requested_samples):
                try:
                    await self._store.record_eval_result(
                        workspace_root=workspace,
                        batch_id=green.batch_id,
                        sample_index=index,
                        result=results[index],
                        created_at=created_at,
                    )
                except HarnessStoreConflictError as exc:
                    raced = await self._store.get_eval_result(
                        workspace,
                        green.batch_id,
                        green.suite_id,
                        index,
                    )
                    if (
                        raced is None
                        or raced.result.canonical_payload()
                        != results[index].canonical_payload()
                    ):
                        raise EvolutionSelfReviewGreenCohortError(
                            "green_persistence_conflict",
                            "GREEN cohort 并发写入发生冲突。",
                        ) from exc
        persisted = await self._store.list_eval_results(
            workspace,
            green.batch_id,
            green.suite_id,
            limit=green.requested_samples + 1,
        )
        _require_prefix(persisted, green.requested_samples)
        if len(persisted) != green.requested_samples:
            raise EvolutionSelfReviewGreenCohortError(
                "green_persistence_incomplete",
                "GREEN cohort 未完整写入 H5a。",
            )
        return _build_green_receipt(green, red, plan, fingerprint, persisted)


def _require_green_authority(
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    red: EvolutionSelfReviewRedCohortReceipt,
    lease: ExperimentWorktreeLease,
) -> None:
    if not (
        red.baseline_request_id == request.request_id
        and red.baseline_request_sha256 == request.request_sha256
        and red.metric_binding_id == binding.binding_id
        and red.metric_binding_sha256 == binding.binding_sha256
        and red.validation_plan_id == plan.validation_plan_id
        and red.validation_plan_sha256 == plan.validation_plan_sha256
        and red.suite_id == request.suite_id
        and red.batch_id == request.batch_id
        and red.requested_samples == request.requested_samples
        and red.persisted_samples == request.requested_samples
        and lease.state is ExperimentLeaseState.ACTIVE
        and lease.worktree_ready
        and not lease.execution_ready
        and lease.lease_id == plan.lease_id
        and lease.contract_id == plan.contract_id
        and lease.manifest_sha256 == plan.contract_manifest_sha256
        and lease.baseline_commit == plan.baseline_commit
    ):
        raise EvolutionSelfReviewGreenCohortError(
            "green_authority_mismatch",
            "GREEN Request 的 RED、Plan 与 Lease authority 不一致。",
        )


def _require_green_request(
    green: EvolutionSelfReviewGreenCohortRequest,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    red: EvolutionSelfReviewRedCohortReceipt,
    lease: ExperimentWorktreeLease,
) -> None:
    expected = EvolutionSelfReviewGreenCohortRequestBuilder().build(
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    if green != expected:
        raise EvolutionSelfReviewGreenCohortError(
            "green_request_mismatch",
            "GREEN Cohort Request 与当前 authority 不一致。",
        )


async def _load_and_validate_red_records(
    store: HarnessStore,
    workspace: Path,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    red: EvolutionSelfReviewRedCohortReceipt,
) -> tuple[HarnessStoredEvalResult, ...]:
    records = await store.list_eval_results(
        workspace,
        request.batch_id,
        request.suite_id,
        limit=request.requested_samples + 1,
    )
    try:
        require_continuous_eval_prefix(records, request.requested_samples, phase="red")
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionSelfReviewGreenCohortError(exc.code, str(exc)) from exc
    expected_config = build_self_review_eval_configuration(request, binding, plan)
    if not (
        len(records) == request.requested_samples
        and tuple(item.result_sha256 for item in records) == red.sample_result_sha256
        and all(
            item.result.baseline_identity is not None
            and item.result.baseline_identity.configuration == expected_config
            and item.result.baseline_identity.source.commit == request.baseline_commit
            and item.result.baseline_identity.source.tree_sha256
            == f"sha256:{request.baseline_tree_sha256}"
            and not item.result.baseline_identity.source.dirty
            for item in records
        )
    ):
        raise EvolutionSelfReviewGreenCohortError(
            "red_cohort_evidence_mismatch",
            "H5a RED cohort 与完成回执或当前 authority 不一致。",
        )
    return records


async def _run_green_repetitions(
    *,
    blobs: tuple[tuple[str, bytes], ...],
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity: HarnessEvalBaselineIdentity,
) -> tuple[HarnessEvalSuiteResult, ...]:
    with tempfile.TemporaryDirectory(prefix="naumi-evo-green-") as temporary:
        root = Path(temporary).resolve()
        files: list[Path] = []
        for relative, content in blobs:
            destination = root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            files.append(destination)
        try:
            return await run_self_review_static_repetitions(
                files=files,
                scan_root=root,
                phase="green",
                request=request,
                binding=binding,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
        except SelfReviewEvalRuntimeError as exc:
            raise EvolutionSelfReviewGreenCohortError(exc.code, str(exc)) from exc


def _require_prefix(
    records: tuple[HarnessStoredEvalResult, ...],
    requested_samples: int,
) -> None:
    try:
        require_continuous_eval_prefix(records, requested_samples, phase="green")
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionSelfReviewGreenCohortError(exc.code, str(exc)) from exc


def _build_green_receipt(
    green: EvolutionSelfReviewGreenCohortRequest,
    red: EvolutionSelfReviewRedCohortReceipt,
    plan: EvolutionValidationPlan,
    fingerprint: TreeFingerprint,
    records: tuple[HarnessStoredEvalResult, ...],
) -> EvolutionSelfReviewGreenCohortReceipt:
    metrics: list[SelfReviewGreenMetricSummary] = []
    for metric in red.metrics:
        values: list[int] = []
        for record in records:
            observation = next(
                item
                for case in record.result.cases
                for item in case.metric_observations
                if item.metric == metric.metric_name
            )
            values.append(int(observation.value))
        metrics.append(SelfReviewGreenMetricSummary(
            metric_name=metric.metric_name,
            finding_code=metric.finding_code,
            direction="decrease",
            target=metric.target,
            sample_values=tuple(values),
        ))
    payload = {
        "schema_version": 1,
        "policy_version": SELF_REVIEW_GREEN_COHORT_POLICY,
        "green_request_id": green.request_id,
        "green_request_sha256": green.request_sha256,
        "red_receipt_id": red.receipt_id,
        "red_receipt_sha256": red.receipt_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "lease_id": green.lease_id,
        "candidate_id": green.candidate_id,
        "candidate_revision": green.candidate_revision,
        "phase": "green",
        "suite_id": green.suite_id,
        "batch_id": green.batch_id,
        "candidate_head": fingerprint.head,
        "candidate_tree_sha256": fingerprint.digest,
        "requested_samples": green.requested_samples,
        "persisted_samples": len(records),
        "sample_result_sha256": [item.result_sha256 for item in records],
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "profile_trust_revalidated": True,
        "candidate_status_revalidated": True,
        "model_access": False,
        "network_access": False,
        "project_code_executed": False,
        "arc04_worker_used": False,
        "cohort_complete": True,
        "completed_at": max(item.created_at for item in records),
    }
    digest = _sha256_payload(payload)
    return EvolutionSelfReviewGreenCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evvgreenrun_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "--literal-pathspecs",
                "-C",
                str(root),
                *args,
            ],
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvolutionSelfReviewGreenCohortError(
            "candidate_git_read_failed",
            "无法读取 candidate Git 状态。",
        ) from exc
    if completed.returncode != 0:
        raise EvolutionSelfReviewGreenCohortError(
            "candidate_git_read_failed",
            "无法读取 candidate Git 状态。",
        )
    return completed.stdout


def _require_workspace_repository_root(workspace: Path) -> None:
    try:
        top = Path(
            _git(workspace, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve(strict=True)
    except (OSError, UnicodeDecodeError, EvolutionSelfReviewGreenCohortError) as exc:
        raise EvolutionSelfReviewGreenCohortError(
            "green_workspace_invalid",
            "GREEN workspace 必须是可验证 Git 仓库的根目录。",
        ) from exc
    if top != workspace:
        raise EvolutionSelfReviewGreenCohortError(
            "green_workspace_not_repository_root",
            "GREEN workspace 必须精确指向 Git 仓库根目录。",
        )


def _aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间格式无效。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed


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
    "EvolutionSelfReviewGreenCohortError",
    "EvolutionSelfReviewGreenCohortExecutor",
    "EvolutionSelfReviewGreenCohortReceipt",
    "EvolutionSelfReviewGreenCohortRequest",
    "EvolutionSelfReviewGreenCohortRequestBuilder",
    "SelfReviewGreenMetricSummary",
]
