"""Execute one exact adversarial RED/GREEN lane sample through HAR-08.4e."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.adversarial_batch_requests import (
    AdversarialBatchLane,
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    EvolutionAdversarialProbeContract,
)
from naumi_agent.evolution.candidate_snapshots import (
    EvolutionCandidateSnapshotError,
    EvolutionCandidateWorktreeSnapshot,
    capture_candidate_worktree_snapshot,
    revalidate_candidate_worktree_snapshot,
)
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
    HarnessSandboxSourceOverlay,
)
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionError,
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
    HarnessSandboxEvalSource,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoredEvalResult,
    HarnessStoreError,
)

ADVERSARIAL_SAMPLE_POLICY = "evolution-adversarial-sample-v1"
ADVERSARIAL_SAMPLE_RUNNER = "evolution_adversarial_probe@1"
_SHA256_RE = r"^[0-9a-f]{64}$"
type SourceRevalidator = Callable[[], Awaitable[bool]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionAdversarialSampleReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-adversarial-sample-v1"] = (
        ADVERSARIAL_SAMPLE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evadvsample_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evadvreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    probe_contract_id: str = Field(pattern=r"^evapc_[0-9a-f]{24}$")
    probe_contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    lane_order: int = Field(ge=1, le=6)
    platform: Literal["linux", "macos", "windows"]
    phase: Literal["red", "green"]
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    overlay_source_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    result_sha256: str = Field(pattern=_SHA256_RE)
    baseline_identity_sha256: str = Field(pattern=_SHA256_RE)
    platform_identity: HarnessEvalPlatformIdentity
    platform_sha256: str = Field(pattern=_SHA256_RE)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    check_statuses: tuple[
        Literal["passed", "implementation_failure", "evaluation_error"], ...
    ] = Field(min_length=1, max_length=80)
    lifecycle_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    run_grant_sha256: str = Field(pattern=_SHA256_RE)
    profile_trust_revalidated: Literal[True] = True
    lease_revalidated: Literal[True] = True
    platform_revalidated: Literal[True] = True
    exact_revision_materialized: Literal[True] = True
    candidate_snapshot_revalidated: bool
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    success_rule_revalidated: Literal[True] = True
    harness_result_persisted: Literal[True] = True
    sample_complete: Literal[True] = True
    completed_at: str

    @model_validator(mode="after")
    def _receipt_is_consistent_and_tamper_evident(self) -> Self:
        if not (
            len(self.check_ids)
            == len(self.check_statuses)
            == len(self.lifecycle_receipt_sha256)
        ):
            raise ValueError("Adversarial sample check evidence 数量不一致。")
        if self.check_ids != tuple(sorted(set(self.check_ids))):
            raise ValueError("Adversarial sample checks 必须排序且不得重复。")
        if self.platform_identity.system != self.platform:
            raise ValueError("Adversarial sample platform identity 与 lane 不一致。")
        expected_platform = _sha256_payload(self.platform_identity.model_dump(mode="json"))
        if not hmac.compare_digest(self.platform_sha256, expected_platform):
            raise ValueError("Adversarial sample platform 摘要不一致。")
        if self.phase == "red" and (
            self.overlay_source_sha256 is not None
            or self.candidate_snapshot_revalidated
        ):
            raise ValueError("Adversarial RED 不得声明 candidate overlay。")
        if self.phase == "green" and (
            self.overlay_source_sha256 is None
            or not self.candidate_snapshot_revalidated
        ):
            raise ValueError("Adversarial GREEN 必须绑定 candidate overlay。")
        try:
            completed = datetime.fromisoformat(self.completed_at)
        except ValueError as exc:
            raise ValueError("Adversarial sample completed_at 必须是 ISO-8601。") from exc
        if completed.utcoffset() is None:
            raise ValueError("Adversarial sample completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Adversarial sample receipt 摘要不一致。")
        if self.receipt_id != f"evadvsample_{expected[:24]}":
            raise ValueError("Adversarial sample receipt identity 不一致。")
        return self


class EvolutionAdversarialSampleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _PreparedSample:
    request: EvolutionAdversarialBatchRequest
    probes: EvolutionAdversarialProbeContract
    plan: EvolutionValidationPlan
    lease: ExperimentWorktreeLease
    lane: AdversarialBatchLane
    platform: HarnessEvalPlatformIdentity
    checks: tuple[HarnessCheckSpec, ...]
    source: HarnessSandboxEvalSource
    source_identity: HarnessEvalSourceIdentity
    source_is_current: SourceRevalidator
    candidate_snapshot: EvolutionCandidateWorktreeSnapshot | None


class EvolutionAdversarialSampleExecutor:
    """Run one request lane/sample under an externally governed Batch Run Grant."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        lease_store: EvolutionExperimentLeaseStore,
        worktree_storage_dir: str | Path,
        profile_service: HarnessService,
        sandbox_eval_kernel: HarnessSandboxEvalExecutionKernel,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if Path(sandbox_eval_kernel.workspace_root) != self._workspace_root:
            raise ValueError("Adversarial Sample kernel 与 workspace 不一致。")
        if Path(profile_service.workspace_root) != self._workspace_root:
            raise ValueError("Adversarial Sample Profile Service 与 workspace 不一致。")
        self._store = store
        self._lease_store = lease_store
        self._worktree_storage_dir = Path(worktree_storage_dir).expanduser().resolve()
        self._profile_service = profile_service
        self._kernel = sandbox_eval_kernel
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        parent_receipt_id: str,
        lane_order: int,
        sample_index: int,
        batch_request: EvolutionAdversarialBatchRequest,
        probe_contract: EvolutionAdversarialProbeContract,
        validation_plan: EvolutionValidationPlan,
        lease: ExperimentWorktreeLease,
        run_authority: HarnessSandboxEvalRunAuthority,
    ) -> EvolutionAdversarialSampleReceipt:
        prepared = await self._prepare(
            lane_order=lane_order,
            sample_index=sample_index,
            batch_request=batch_request,
            probe_contract=probe_contract,
            validation_plan=validation_plan,
            lease=lease,
        )
        request = prepared.request
        lane = prepared.lane
        configuration = _configuration(request)
        identity = build_eval_baseline_identity(
            self._workspace_root,
            configuration=configuration,
            platform_identity=prepared.platform,
            profile_trusted=True,
            source_identity=prepared.source_identity,
        )
        existing = await self._store.get_eval_result(
            self._workspace_root,
            lane.batch_id,
            request.suite_id,
            sample_index,
        )
        if existing is not None:
            _validate_stored(
                existing,
                request,
                prepared,
                identity,
                sample_index=sample_index,
            )
            return _build_receipt(request, prepared, existing)
        try:
            authority = HarnessSandboxEvalRunAuthority.model_validate(
                run_authority.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialSampleError(
                "adversarial_run_authority_invalid",
                "Adversarial sample Run authority 无效或已被篡改。",
            ) from exc
        try:
            results = await self._kernel.execute(
                lane="adversarial",
                authority_key=adversarial_lane_authority_key(request, lane.order),
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                checks=prepared.checks,
                profile_digest=request.profile_sha256,
                profile_is_current=lambda: self._profile_is_current(request),
                source=prepared.source,
                run_authority=authority,
            )
        except HarnessSandboxEvalExecutionError as exc:
            raise EvolutionAdversarialSampleError(exc.code, str(exc)) from exc
        suite = _build_suite(
            request,
            prepared,
            identity,
            results,
            authority.grant_sha256,
        )
        if not await prepared.source_is_current():
            raise EvolutionAdversarialSampleError(
                "adversarial_source_changed_before_persistence",
                "Adversarial source 在 H5a 持久化前发生变化。",
            )
        try:
            stored = await self._store.record_eval_result(
                workspace_root=self._workspace_root,
                batch_id=lane.batch_id,
                sample_index=sample_index,
                result=suite,
                created_at=self._aware_now().isoformat(),
            )
        except HarnessStoreConflictError as exc:
            raise EvolutionAdversarialSampleError(
                "adversarial_result_conflict",
                "同一 Adversarial lane/sample 已存在不同结果。",
            ) from exc
        except HarnessStoreError as exc:
            raise EvolutionAdversarialSampleError(
                "adversarial_result_store_failed",
                "Adversarial sample 结果无法持久化。",
            ) from exc
        _validate_stored(
            stored,
            request,
            prepared,
            identity,
            sample_index=sample_index,
        )
        if not await prepared.source_is_current():
            raise EvolutionAdversarialSampleError(
                "adversarial_source_changed_after_persistence",
                "Adversarial source 在 H5a 返回前发生变化。",
            )
        return _build_receipt(request, prepared, stored)

    async def _prepare(
        self,
        *,
        lane_order,
        sample_index,
        batch_request,
        probe_contract,
        validation_plan,
        lease,
    ) -> _PreparedSample:
        try:
            request = EvolutionAdversarialBatchRequest.model_validate(
                batch_request.model_dump(mode="json")
            )
            probes = EvolutionAdversarialProbeContract.model_validate(
                probe_contract.model_dump(mode="json")
            )
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialSampleError(
                "adversarial_sample_authority_invalid",
                "Adversarial sample authority 无效或已被篡改。",
            ) from exc
        if isinstance(lane_order, bool) or not 1 <= lane_order <= len(request.lanes):
            raise EvolutionAdversarialSampleError(
                "adversarial_lane_invalid",
                "Adversarial lane_order 超出请求范围。",
            )
        if isinstance(sample_index, bool) or not 0 <= sample_index < request.requested_samples:
            raise EvolutionAdversarialSampleError(
                "adversarial_sample_index_invalid",
                "Adversarial sample_index 超出请求范围。",
            )
        if not _authority_matches(request, probes, plan, candidate_lease):
            raise EvolutionAdversarialSampleError(
                "adversarial_sample_authority_mismatch",
                "Batch Request、Probe Contract、Plan 与 Lease authority 不一致。",
            )
        now = self._aware_now()
        current_lease = await self._lease_store.get(candidate_lease.contract_id)
        if (
            current_lease != candidate_lease
            or candidate_lease.state is not ExperimentLeaseState.ACTIVE
            or not candidate_lease.worktree_ready
            or datetime.fromisoformat(candidate_lease.expires_at) <= now
        ):
            raise EvolutionAdversarialSampleError(
                "adversarial_lease_stale",
                "Adversarial Experiment Lease 已失效或过期。",
            )
        lane = request.lanes[lane_order - 1]
        platform = capture_eval_platform_identity()
        if platform.system != lane.platform:
            raise EvolutionAdversarialSampleError(
                "adversarial_platform_mismatch",
                f"当前平台 {platform.system} 不能执行 {lane.platform} lane。",
            )
        checks = await self._current_checks(request)
        snapshot: EvolutionCandidateWorktreeSnapshot | None = None
        if lane.phase == "red":
            source = HarnessSandboxEvalSource(
                revision=request.baseline_commit,
                revision_tree_sha256=request.baseline_tree_sha256,
            )
            source_identity = HarnessEvalSourceIdentity(
                commit=request.baseline_commit,
                tree_sha256=f"sha256:{request.baseline_tree_sha256}",
                dirty=False,
            )

            async def source_is_current() -> bool:
                return await self._lease_is_current(candidate_lease)
        else:
            try:
                snapshot = capture_candidate_worktree_snapshot(
                    candidate_lease,
                    plan,
                    worktree_storage_dir=self._worktree_storage_dir,
                    now=now,
                )
            except EvolutionCandidateSnapshotError as exc:
                raise EvolutionAdversarialSampleError(exc.code, str(exc)) from exc
            overlays = tuple(
                HarnessSandboxSourceOverlay(
                    path=item.path,
                    content=item.content,
                    sha256=item.sha256,
                    executable=item.executable,
                )
                for item in snapshot.blobs
            )

            async def source_is_current() -> bool:
                try:
                    revalidate_candidate_worktree_snapshot(snapshot)
                except EvolutionCandidateSnapshotError:
                    return False
                return await self._lease_is_current(candidate_lease)

            source = HarnessSandboxEvalSource(
                revision=request.baseline_commit,
                revision_tree_sha256=request.baseline_tree_sha256,
                overlays=overlays,
                overlay_source_sha256=snapshot.fingerprint.digest.removeprefix(
                    "sha256:"
                ),
                source_is_current=source_is_current,
            )
            source_identity = HarnessEvalSourceIdentity(
                commit=request.baseline_commit,
                tree_sha256=snapshot.fingerprint.digest,
                dirty=True,
            )
        return _PreparedSample(
            request=request,
            probes=probes,
            plan=plan,
            lease=candidate_lease,
            lane=lane,
            platform=platform,
            checks=checks,
            source=source,
            source_identity=source_identity,
            source_is_current=source_is_current,
            candidate_snapshot=snapshot,
        )

    async def _current_checks(
        self,
        request: EvolutionAdversarialBatchRequest,
    ) -> tuple[HarnessCheckSpec, ...]:
        status = await self._profile_service.status()
        if not status.trusted or status.snapshot.profile is None:
            raise EvolutionAdversarialSampleError(
                "adversarial_profile_untrusted",
                "Harness Profile 信任已失效，不能执行 Adversarial sample。",
            )
        if status.profile_digest != request.profile_sha256:
            raise EvolutionAdversarialSampleError(
                "adversarial_profile_drifted",
                "当前 Harness Profile 已偏离 Adversarial Batch Request。",
            )
        by_id = {item.id: item for item in status.snapshot.profile.checks}
        checks: list[HarnessCheckSpec] = []
        for expected in request.checks:
            check = by_id.get(expected.check_id)
            if check is None or not _check_matches(check, expected):
                raise EvolutionAdversarialSampleError(
                    "adversarial_profile_check_drifted",
                    f"Harness Profile adversarial check {expected.check_id} 已漂移。",
                )
            checks.append(check)
        return tuple(checks)

    async def _profile_is_current(
        self,
        request: EvolutionAdversarialBatchRequest,
    ) -> bool:
        try:
            await self._current_checks(request)
        except EvolutionAdversarialSampleError:
            return False
        return True

    async def _lease_is_current(self, lease: ExperimentWorktreeLease) -> bool:
        current = await self._lease_store.get(lease.contract_id)
        return current == lease and datetime.fromisoformat(lease.expires_at) > self._aware_now()

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EvolutionAdversarialSampleError(
                "adversarial_sample_clock_invalid",
                "Adversarial Sample executor 时钟必须包含时区。",
            )
        return now


def _authority_matches(request, probes, plan, lease) -> bool:
    return bool(
        request.probe_contract_id == probes.probe_contract_id
        and request.probe_contract_sha256 == probes.probe_contract_sha256
        and request.validation_plan_id == plan.validation_plan_id
        and request.validation_plan_sha256 == plan.validation_plan_sha256
        and request.profile_binding_id == probes.profile_binding_id
        and request.profile_binding_sha256 == probes.profile_binding_sha256
        and request.profile_sha256 == probes.profile_sha256 == plan.profile_sha256
        and request.registry_sha256 == probes.registry_sha256
        and request.probe_platform_sha256 == probes.platform_sha256
        and request.candidate_id == probes.candidate_id == plan.candidate_id
        and request.candidate_revision
        == probes.candidate_revision
        == plan.candidate_revision
        and request.candidate_files_sha256
        == probes.candidate_files_sha256
        == plan.candidate_files_sha256
        and request.lease_id == lease.lease_id == plan.lease_id
        and request.contract_id == lease.contract_id == plan.contract_id
        and request.contract_manifest_sha256
        == lease.manifest_sha256
        == plan.contract_manifest_sha256
        and request.baseline_commit == lease.baseline_commit == plan.baseline_commit
        and request.source_snapshot_id == plan.source_snapshot_id
        and request.source_snapshot_sha256 == plan.source_snapshot_sha256
        and request.mutation_receipt_id == plan.mutation_receipt_id
        and request.mutation_receipt_sha256 == plan.mutation_receipt_sha256
        and probes.coverage_complete
        and not probes.blockers
        and request.request_ready
        and not request.execution_ready
    )


def _check_matches(check: HarnessCheckSpec, expected) -> bool:
    return bool(
        _sha256_payload(check.model_dump(mode="json")) == expected.spec_sha256
        and _sha256_payload(list(check.argv)) == expected.argv_sha256
        and check.timeout_seconds == expected.timeout_seconds
        and check.adversarial_probes == expected.probes
    )


def _configuration(
    request: EvolutionAdversarialBatchRequest,
) -> HarnessEvalConfigurationIdentity:
    policy = HarnessEvalComparisonPolicy()
    return HarnessEvalConfigurationIdentity.create(
        suite_id=request.suite_id,
        suite_sha256=request.probe_contract_sha256,
        profile_sha256=request.profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=ADVERSARIAL_SAMPLE_RUNNER,
        repetitions=request.requested_samples,
        live=False,
    )


def _build_suite(
    request: EvolutionAdversarialBatchRequest,
    prepared: _PreparedSample,
    identity,
    results: tuple[HarnessSandboxCheckResult, ...],
    run_grant_sha256: str,
) -> HarnessEvalSuiteResult:
    cases = tuple(
        _case_from_result(
            result,
            request=request,
            run_grant_sha256=run_grant_sha256,
        )
        for result in results
    )
    status = (
        EvalRunStatus.EVALUATION_ERROR
        if any(item.status is EvalCaseStatus.EVALUATION_ERROR for item in cases)
        else EvalRunStatus.FAILED
        if any(item.status is EvalCaseStatus.IMPLEMENTATION_FAILURE for item in cases)
        else EvalRunStatus.PASSED
    )
    return HarnessEvalSuiteResult(
        suite_id=request.suite_id,
        title=(
            f"Evolution adversarial {prepared.lane.platform} "
            f"{prepared.lane.phase.upper()} sample"
        ),
        suite_path=(
            f"evolution/adversarial/{prepared.lane.platform}/{prepared.lane.phase}"
        ),
        suite_sha256=request.probe_contract_sha256,
        status=status,
        cases=cases,
        code=f"adversarial_{prepared.lane.phase}_{status.value}",
        message="可信 Profile adversarial checks 已通过 ARC-04 Worker 执行。",
        comparison_policy=HarnessEvalComparisonPolicy(),
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in results),
    )


def _case_from_result(
    result: HarnessSandboxCheckResult,
    *,
    request: EvolutionAdversarialBatchRequest,
    run_grant_sha256: str,
) -> HarnessEvalCaseResult:
    if result.status is HarnessSandboxCheckStatus.PASSED:
        status = EvalCaseStatus.PASSED
    elif result.status is HarnessSandboxCheckStatus.FAILED:
        status = EvalCaseStatus.IMPLEMENTATION_FAILURE
    else:
        status = EvalCaseStatus.EVALUATION_ERROR
    bound = next(item for item in request.checks if item.check_id == result.check_id)
    metric_name = f"adversarial.{result.check_id}.exit_zero"
    observations = (
        ()
        if status is EvalCaseStatus.EVALUATION_ERROR
        else (
            HarnessEvalMetricObservation(
                metric=metric_name,
                value=1.0 if status is EvalCaseStatus.PASSED else 0.0,
                unit="scalar",
                direction="increase",
                target=1.0,
                primary=True,
            ),
        )
    )
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner=ADVERSARIAL_SAMPLE_RUNNER,
        status=status,
        primary_metric=metric_name if observations else "",
        metric_observations=observations,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256="
            f"{result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_grant_sha256={run_grant_sha256} "
            f"probe_kinds={','.join(bound.probes)}"
        ),
        duration_ms=result.duration_ms,
    )


def _validate_stored(
    stored: HarnessStoredEvalResult,
    request: EvolutionAdversarialBatchRequest,
    prepared: _PreparedSample,
    identity,
    *,
    sample_index: int,
) -> None:
    result = stored.result
    cases = result.cases
    expected_ids = tuple(item.check_id for item in request.checks)
    valid = bool(
        stored.batch_id == prepared.lane.batch_id
        and stored.suite_id == request.suite_id
        and stored.sample_index == sample_index
        and result.suite_sha256 == request.probe_contract_sha256
        and result.baseline_identity == identity
        and tuple(item.case_id for item in cases) == expected_ids
        and all(_case_evidence_is_valid(item, request) for item in cases)
        and all(_lifecycle_digest(item.message) is not None for item in cases)
        and len({_run_grant_digest(item.message) for item in cases}) == 1
        and None not in {_run_grant_digest(item.message) for item in cases}
    )
    if not valid:
        raise EvolutionAdversarialSampleError(
            "adversarial_stored_result_invalid",
            "已持久化的 Adversarial sample 与当前 authority 不一致。",
        )


def _case_evidence_is_valid(
    case: HarnessEvalCaseResult,
    request: EvolutionAdversarialBatchRequest,
) -> bool:
    if case.runner != ADVERSARIAL_SAMPLE_RUNNER:
        return False
    expected = next(
        (item for item in request.checks if item.check_id == case.case_id),
        None,
    )
    if expected is None or f"probe_kinds={','.join(expected.probes)}" not in case.message:
        return False
    metric_name = f"adversarial.{case.case_id}.exit_zero"
    if case.status is EvalCaseStatus.EVALUATION_ERROR:
        return not case.primary_metric and not case.metric_observations
    if case.primary_metric != metric_name or len(case.metric_observations) != 1:
        return False
    observation = case.metric_observations[0]
    expected_value = 1.0 if case.status is EvalCaseStatus.PASSED else 0.0
    return bool(
        observation.metric == metric_name
        and observation.value == expected_value
        and observation.unit == "scalar"
        and observation.direction == "increase"
        and observation.target == 1.0
        and observation.primary
    )


def _build_receipt(
    request: EvolutionAdversarialBatchRequest,
    prepared: _PreparedSample,
    stored: HarnessStoredEvalResult,
) -> EvolutionAdversarialSampleReceipt:
    result = stored.result
    identity = result.baseline_identity
    assert identity is not None
    cases = result.cases
    lifecycle = tuple(_required_digest(item.message, "lifecycle") for item in cases)
    grants = tuple(_required_digest(item.message, "run_grant") for item in cases)
    if len(set(grants)) != 1:
        raise EvolutionAdversarialSampleError(
            "adversarial_run_grant_evidence_inconsistent",
            "Adversarial sample checks 未使用同一 Run Grant。",
        )
    overlay_sha = (
        prepared.candidate_snapshot.fingerprint.digest.removeprefix("sha256:")
        if prepared.candidate_snapshot is not None
        else None
    )
    completed_at = stored.created_at
    payload = {
        "schema_version": 1,
        "policy_version": ADVERSARIAL_SAMPLE_POLICY,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "probe_contract_id": prepared.probes.probe_contract_id,
        "probe_contract_sha256": prepared.probes.probe_contract_sha256,
        "validation_plan_id": prepared.plan.validation_plan_id,
        "validation_plan_sha256": prepared.plan.validation_plan_sha256,
        "profile_binding_id": request.profile_binding_id,
        "profile_binding_sha256": request.profile_binding_sha256,
        "lease_id": prepared.lease.lease_id,
        "candidate_id": request.candidate_id,
        "candidate_revision": request.candidate_revision,
        "candidate_files_sha256": request.candidate_files_sha256,
        "lane_order": prepared.lane.order,
        "platform": prepared.lane.platform,
        "phase": prepared.lane.phase,
        "batch_id": prepared.lane.batch_id,
        "sample_index": stored.sample_index,
        "sample_seed": request.sample_seeds[stored.sample_index],
        "source_revision": prepared.source_identity.commit,
        "source_tree_sha256": prepared.source_identity.tree_sha256,
        "overlay_source_sha256": overlay_sha,
        "suite_id": request.suite_id,
        "result_sha256": stored.result_sha256,
        "baseline_identity_sha256": identity.identity_sha256,
        "platform_identity": prepared.platform.model_dump(mode="json"),
        "platform_sha256": _sha256_payload(prepared.platform.model_dump(mode="json")),
        "check_ids": [item.case_id for item in cases],
        "check_statuses": [item.status.value for item in cases],
        "lifecycle_receipt_sha256": list(lifecycle),
        "run_grant_sha256": grants[0],
        "profile_trust_revalidated": True,
        "lease_revalidated": True,
        "platform_revalidated": True,
        "exact_revision_materialized": True,
        "candidate_snapshot_revalidated": prepared.candidate_snapshot is not None,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "success_rule_revalidated": True,
        "harness_result_persisted": True,
        "sample_complete": True,
        "completed_at": completed_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionAdversarialSampleReceipt.model_validate({
        **payload,
        "receipt_id": f"evadvsample_{digest[:24]}",
        "receipt_sha256": digest,
    })


def adversarial_lane_authority_key(
    request: EvolutionAdversarialBatchRequest,
    lane_order: int,
) -> str:
    """Derive the identity shared by one lane coordinator and all its samples."""
    if not isinstance(request, EvolutionAdversarialBatchRequest):
        raise TypeError("Adversarial lane authority 需要 Batch Request。")
    if isinstance(lane_order, bool) or not 1 <= lane_order <= len(request.lanes):
        raise ValueError("Adversarial lane authority order 超出请求范围。")
    lane = request.lanes[lane_order - 1]
    return hashlib.sha256(
        f"{request.request_sha256}:{lane.order}:{lane.batch_id}".encode("ascii")
    ).hexdigest()


def _lifecycle_digest(message: str) -> str | None:
    return _message_digest(message, "lifecycle_sha256")


def _run_grant_digest(message: str) -> str | None:
    return _message_digest(message, "run_grant_sha256")


def _required_digest(message: str, field: Literal["lifecycle", "run_grant"]) -> str:
    digest = (
        _lifecycle_digest(message)
        if field == "lifecycle"
        else _run_grant_digest(message)
    )
    if digest is None:
        raise EvolutionAdversarialSampleError(
            "adversarial_run_evidence_incomplete",
            "Adversarial sample 缺少 ARC-04 lifecycle 或 Run Grant evidence。",
        )
    return digest


def _message_digest(message: str, field: str) -> str | None:
    match = re.search(rf"(?:^| ){field}=([0-9a-f]{{64}})(?:$| )", message)
    return match.group(1) if match is not None else None


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
    "ADVERSARIAL_SAMPLE_POLICY",
    "ADVERSARIAL_SAMPLE_RUNNER",
    "EvolutionAdversarialSampleError",
    "EvolutionAdversarialSampleExecutor",
    "EvolutionAdversarialSampleReceipt",
    "adversarial_lane_authority_key",
]
