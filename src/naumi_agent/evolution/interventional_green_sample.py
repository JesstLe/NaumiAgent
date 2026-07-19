"""Execute one candidate-overlay GREEN sample through the shared ARC-04 kernel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.candidate_snapshots import (
    EvolutionCandidateSnapshotError,
    EvolutionCandidateWorktreeSnapshot,
    capture_candidate_worktree_snapshot,
    revalidate_candidate_worktree_snapshot,
)
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.interventional_green_request import (
    EvolutionInterventionalGreenCohortRequest,
    EvolutionInterventionalGreenCohortRequestBuilder,
    EvolutionInterventionalGreenRequestError,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortReceipt,
)
from naumi_agent.evolution.interventional_red_sample import (
    INTERVENTIONAL_RED_CHECK_RUNNER,
    INTERVENTIONAL_RED_SAMPLE_RUNNER,
    EvolutionInterventionalRedSampleError,
    build_interventional_configuration,
    validate_interventional_red_authority,
)
from naumi_agent.evolution.interventional_sample_kernel import (
    EvolutionInterventionalRunAuthority,
    EvolutionInterventionalSampleKernel,
    EvolutionInterventionalSampleKernelError,
    EvolutionInterventionalSampleSource,
    build_interventional_sample_suite,
    interventional_lifecycle_digest,
    interventional_run_grant_digest,
    interventional_run_scope,
)
from naumi_agent.evolution.self_review import SELF_REVIEW_STATIC_RUNNER_VERSION
from naumi_agent.evolution.self_review_eval_runtime import (
    SelfReviewEvalRuntimeError,
    run_self_review_static_sample,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_checks import HarnessSandboxSourceOverlay
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

INTERVENTIONAL_GREEN_SAMPLE_POLICY = "evolution-interventional-green-sample-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionInterventionalGreenSampleReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-interventional-green-sample-v1"] = (
        INTERVENTIONAL_GREEN_SAMPLE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvgreencheck_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    green_request_id: str = Field(pattern=r"^evvgreenint_[0-9a-f]{24}$")
    green_request_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(pattern=r"^evvredcohort_[0-9a-f]{24}$")
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    candidate_tree_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    result_sha256: str = Field(pattern=_SHA256_RE)
    candidate_identity_sha256: str = Field(pattern=_SHA256_RE)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    check_statuses: tuple[str, ...] = Field(min_length=1, max_length=80)
    lifecycle_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    profile_trust_revalidated: Literal[True] = True
    red_cohort_revalidated: Literal[True] = True
    candidate_snapshot_revalidated: Literal[True] = True
    exact_revision_overlay_materialized: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    metrics_executed: Literal[True] = True
    sample_complete: Literal[True] = True
    completed_at: str

    @model_validator(mode="after")
    def _receipt_is_consistent(self) -> Self:
        if not (
            len(self.check_ids)
            == len(self.check_statuses)
            == len(self.lifecycle_receipt_sha256)
        ):
            raise ValueError("Interventional GREEN check/status 数量不一致。")
        if self.check_ids != tuple(sorted(set(self.check_ids))):
            raise ValueError("Interventional GREEN checks 必须排序且不得重复。")
        parsed = datetime.fromisoformat(self.completed_at)
        if parsed.utcoffset() is None:
            raise ValueError("Interventional GREEN completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Interventional GREEN receipt 摘要不一致。")
        if self.receipt_id != f"evvgreencheck_{expected[:24]}":
            raise ValueError("Interventional GREEN receipt identity 不一致。")
        return self


class EvolutionInterventionalGreenSampleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _PreparedGreenAuthority:
    request: EvolutionBaselineCohortRequest
    binding: EvolutionMetricRunnerBinding
    plan: EvolutionValidationPlan
    profile: EvolutionValidationProfileBinding
    green: EvolutionInterventionalGreenCohortRequest
    red: EvolutionInterventionalRedCohortReceipt
    lease: ExperimentWorktreeLease
    snapshot: EvolutionCandidateWorktreeSnapshot
    configuration: HarnessEvalConfigurationIdentity
    identity: HarnessEvalBaselineIdentity
    overlays: tuple[HarnessSandboxSourceOverlay, ...]
    checks: tuple[HarnessCheckSpec, ...]


class EvolutionInterventionalGreenSampleExecutor:
    """Run one GREEN sample against an immutable candidate overlay snapshot."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        lease_store: EvolutionExperimentLeaseStore,
        worktree_storage_dir: str | Path,
        sample_kernel: EvolutionInterventionalSampleKernel,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self._store = store
        self._lease_store = lease_store
        self._worktree_storage_dir = Path(worktree_storage_dir).expanduser().resolve()
        self._kernel = sample_kernel
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        parent_receipt_id: str,
        sample_index: int,
        green_request: EvolutionInterventionalGreenCohortRequest,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        lease: ExperimentWorktreeLease,
        run_authority: EvolutionInterventionalRunAuthority | None = None,
    ) -> EvolutionInterventionalGreenSampleReceipt:
        prepared = await self._prepare_authority(
            green_request=green_request,
            baseline_request=baseline_request,
            metric_binding=metric_binding,
            validation_plan=validation_plan,
            profile_binding=profile_binding,
            red_receipt=red_receipt,
            lease=lease,
        )
        request = prepared.request
        binding = prepared.binding
        plan = prepared.plan
        profile = prepared.profile
        green = prepared.green
        red = prepared.red
        candidate_lease = prepared.lease
        snapshot = prepared.snapshot
        configuration = prepared.configuration
        identity = prepared.identity
        overlays = prepared.overlays

        async def source_is_current() -> bool:
            try:
                revalidate_candidate_worktree_snapshot(snapshot)
                observed = await self._lease_store.get(candidate_lease.contract_id)
                return (
                    observed == candidate_lease
                    and datetime.fromisoformat(candidate_lease.expires_at)
                    > self._aware_now()
                )
            except (EvolutionCandidateSnapshotError, EvolutionInterventionalGreenSampleError):
                return False

        def validate_existing(
            stored: HarnessStoredEvalResult,
            checks: tuple[HarnessCheckSpec, ...],
        ) -> None:
            self._validate_existing(
                stored,
                request=request,
                checks=checks,
                binding=binding,
                identity=identity,
            )

        async def build_suite(results, run_scope, grant_sha256):
            metric_result = await self._run_metric_sample(
                snapshot=snapshot,
                request=request,
                binding=binding,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
            return build_interventional_sample_suite(
                request,
                results,
                phase="green",
                metric_result=metric_result,
                identity=identity,
                run_scope=run_scope,
                run_grant_sha256=grant_sha256,
            )

        try:
            stored = await self._kernel.execute(
                phase="green",
                authority_key=green.request_sha256,
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                baseline_request=request,
                profile_binding=profile,
                batch_id=green.batch_id,
                source=EvolutionInterventionalSampleSource(
                    revision=candidate_lease.baseline_commit,
                    revision_tree_sha256=request.baseline_tree_sha256,
                    overlays=overlays,
                    overlay_source_sha256=snapshot.fingerprint.digest.removeprefix("sha256:"),
                    source_is_current=source_is_current,
                ),
                validate_existing=validate_existing,
                build_suite=build_suite,
                run_authority=run_authority,
            )
        except EvolutionInterventionalSampleKernelError as exc:
            raise EvolutionInterventionalGreenSampleError(exc.code, str(exc)) from exc
        if not await source_is_current():
            raise EvolutionInterventionalGreenSampleError(
                "candidate_snapshot_changed_after_persistence",
                "Candidate Snapshot 在 GREEN result 返回前发生变化。",
            )
        return _build_receipt(
            green=green,
            red=red,
            plan=plan,
            profile=profile,
            sample_index=sample_index,
            candidate_tree_sha256=snapshot.fingerprint.digest,
            stored=stored,
        )

    async def revalidate_cohort_authority(
        self,
        *,
        green_request: EvolutionInterventionalGreenCohortRequest,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> HarnessEvalBaselineIdentity:
        """Perform the complete side-effect-free GREEN preflight before cohort authority."""
        prepared = await self._prepare_authority(
            green_request=green_request,
            baseline_request=baseline_request,
            metric_binding=metric_binding,
            validation_plan=validation_plan,
            profile_binding=profile_binding,
            red_receipt=red_receipt,
            lease=lease,
        )
        return prepared.identity

    async def validate_cohort_prefix(
        self,
        *,
        records: tuple[HarnessStoredEvalResult, ...],
        green_request: EvolutionInterventionalGreenCohortRequest,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> tuple[HarnessEvalBaselineIdentity, list[EvolutionInterventionalGreenSampleReceipt]]:
        """Preflight once, then validate a continuous existing GREEN prefix."""
        prepared = await self._prepare_authority(
            green_request=green_request,
            baseline_request=baseline_request,
            metric_binding=metric_binding,
            validation_plan=validation_plan,
            profile_binding=profile_binding,
            red_receipt=red_receipt,
            lease=lease,
        )
        receipts: list[EvolutionInterventionalGreenSampleReceipt] = []
        for stored in records:
            self._validate_existing(
                stored,
                request=prepared.request,
                checks=prepared.checks,
                binding=prepared.binding,
                identity=prepared.identity,
            )
            receipts.append(_build_receipt(
                green=prepared.green,
                red=prepared.red,
                plan=prepared.plan,
                profile=prepared.profile,
                sample_index=stored.sample_index,
                candidate_tree_sha256=prepared.snapshot.fingerprint.digest,
                stored=stored,
            ))
        return prepared.identity, receipts

    async def _prepare_authority(
        self,
        *,
        green_request,
        baseline_request,
        metric_binding,
        validation_plan,
        profile_binding,
        red_receipt,
        lease,
    ) -> _PreparedGreenAuthority:
        request, binding, plan, profile, green, red, candidate_lease = (
            self._validate_authority(
                green_request,
                baseline_request,
                metric_binding,
                validation_plan,
                profile_binding,
                red_receipt,
                lease,
            )
        )
        try:
            checks = await self._kernel.current_checks(
                request,
                profile,
                phase="green",
            )
        except EvolutionInterventionalSampleKernelError as exc:
            raise EvolutionInterventionalGreenSampleError(exc.code, str(exc)) from exc
        now = self._aware_now()
        current_lease = await self._lease_store.get(candidate_lease.contract_id)
        if current_lease != candidate_lease:
            raise EvolutionInterventionalGreenSampleError(
                "candidate_lease_stale",
                "Candidate Lease 已变化或不再存在。",
            )
        if datetime.fromisoformat(candidate_lease.expires_at) <= now:
            raise EvolutionInterventionalGreenSampleError(
                "candidate_lease_expired",
                "Candidate Lease 已过期，不能执行 Interventional GREEN。",
            )
        red_records = await self._load_red_records(request, binding, plan, profile, red)
        try:
            snapshot = capture_candidate_worktree_snapshot(
                candidate_lease,
                plan,
                worktree_storage_dir=self._worktree_storage_dir,
                now=now,
            )
        except EvolutionCandidateSnapshotError as exc:
            raise EvolutionInterventionalGreenSampleError(exc.code, str(exc)) from exc
        red_identity = red_records[0].result.baseline_identity
        assert red_identity is not None
        platform = capture_eval_platform_identity()
        if red_identity.platform != platform:
            raise EvolutionInterventionalGreenSampleError(
                "green_platform_mismatch",
                "GREEN 平台身份已偏离 RED cohort。",
            )
        configuration = build_interventional_configuration(request, binding, plan, profile)
        if red_identity.configuration != configuration:
            raise EvolutionInterventionalGreenSampleError(
                "red_configuration_mismatch",
                "RED cohort configuration 与 GREEN Request 不一致。",
            )
        identity = build_eval_baseline_identity(
            snapshot.root,
            configuration=configuration,
            platform_identity=platform,
            profile_trusted=True,
            source_identity=HarnessEvalSourceIdentity(
                commit=candidate_lease.baseline_commit,
                tree_sha256=snapshot.fingerprint.digest,
                dirty=True,
            ),
        )
        overlays = tuple(
            HarnessSandboxSourceOverlay(
                path=item.path,
                content=item.content,
                sha256=item.sha256,
                executable=item.executable,
            )
            for item in snapshot.blobs
        )
        return _PreparedGreenAuthority(
            request=request,
            binding=binding,
            plan=plan,
            profile=profile,
            green=green,
            red=red,
            lease=candidate_lease,
            snapshot=snapshot,
            configuration=configuration,
            identity=identity,
            overlays=overlays,
            checks=checks,
        )

    def _validate_authority(self, green_request, baseline_request, metric_binding,
                            validation_plan, profile_binding, red_receipt, lease):
        try:
            request, binding, plan, profile = validate_interventional_red_authority(
                baseline_request,
                metric_binding,
                validation_plan,
                profile_binding,
            )
            green = EvolutionInterventionalGreenCohortRequest.model_validate(
                green_request.model_dump(mode="json")
            )
            red = EvolutionInterventionalRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
            expected = EvolutionInterventionalGreenCohortRequestBuilder().build(
                baseline_request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
                red_receipt=red,
                lease=candidate_lease,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            EvolutionInterventionalRedSampleError,
            EvolutionInterventionalGreenRequestError,
        ) as exc:
            raise EvolutionInterventionalGreenSampleError(
                "green_sample_authority_invalid",
                "Interventional GREEN sample authority 无效或已被篡改。",
            ) from exc
        if green != expected:
            raise EvolutionInterventionalGreenSampleError(
                "green_sample_request_mismatch",
                "Interventional GREEN Request 与当前 authority 不一致。",
            )
        return request, binding, plan, profile, green, red, candidate_lease

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EvolutionInterventionalGreenSampleError(
                "green_sample_clock_invalid",
                "Interventional GREEN executor 时钟必须包含时区。",
            )
        return now

    async def _load_red_records(self, request, binding, plan, profile, red):
        records = await self._store.list_eval_results(
            self._workspace_root,
            request.batch_id,
            request.suite_id,
            limit=request.requested_samples + 1,
        )
        expected_configuration = build_interventional_configuration(
            request, binding, plan, profile
        )
        expected_indexes = tuple(range(request.requested_samples))
        expected_platform = (
            records[0].result.baseline_identity.platform
            if records and records[0].result.baseline_identity is not None
            else None
        )
        valid = (
            tuple(item.sample_index for item in records) == expected_indexes
            and tuple(item.result_sha256 for item in records) == red.sample_result_sha256
        )
        for record in records:
            result = record.result
            identity = result.baseline_identity
            checks = tuple(
                item for item in result.cases
                if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
            )
            metrics = tuple(
                item for item in result.cases
                if item.runner == SELF_REVIEW_STATIC_RUNNER_VERSION
            )
            valid = valid and bool(
                identity is not None
                and identity.configuration == expected_configuration
                and identity.platform == expected_platform
                and identity.source.commit == request.baseline_commit
                and identity.source.tree_sha256 == f"sha256:{request.baseline_tree_sha256}"
                and not identity.source.dirty
                and tuple(item.case_id for item in checks)
                == tuple(item.check_id for item in request.checks)
                and all(interventional_lifecycle_digest(item.message) for item in checks)
                and all(interventional_run_scope(item.message) == "cohort" for item in checks)
                and all(interventional_run_grant_digest(item.message) for item in checks)
                and tuple(
                    observation.metric
                    for item in metrics
                    for observation in item.metric_observations
                ) == tuple(item.metric_name for item in request.metrics)
                and len(checks) + len(metrics) == len(result.cases)
            )
        if not valid:
            raise EvolutionInterventionalGreenSampleError(
                "red_cohort_evidence_mismatch",
                "H5a RED cohort 与完成回执或当前 authority 不一致。",
            )
        return records

    def _validate_existing(self, stored, *, request, checks, binding, identity) -> None:
        result = stored.result
        observed_identity = result.baseline_identity
        check_cases = tuple(
            item for item in result.cases if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
        )
        metric_cases = tuple(
            item for item in result.cases if item.runner == SELF_REVIEW_STATIC_RUNNER_VERSION
        )
        if not (
            result.suite_id == request.suite_id
            and observed_identity == identity
            and observed_identity is not None
            and observed_identity.configuration.runner_version
            == INTERVENTIONAL_RED_SAMPLE_RUNNER
            and tuple(item.case_id for item in check_cases) == tuple(item.id for item in checks)
            and all(interventional_lifecycle_digest(item.message) for item in check_cases)
            and all(interventional_run_scope(item.message) for item in check_cases)
            and all(interventional_run_grant_digest(item.message) for item in check_cases)
            and tuple(
                observation.metric
                for item in metric_cases
                for observation in item.metric_observations
            ) == tuple(item.metric_name for item in binding.entries)
            and len(check_cases) + len(metric_cases) == len(result.cases)
        ):
            raise EvolutionInterventionalGreenSampleError(
                "existing_green_sample_conflict",
                "已有 H5a sample 不属于当前 Interventional GREEN authority。",
            )

    async def _run_metric_sample(
        self,
        *,
        snapshot,
        request,
        binding,
        plan,
        configuration,
        identity,
    ):
        try:
            with TemporaryDirectory(prefix="naumi-evo-interventional-green-") as temporary:
                root = Path(temporary).resolve()
                files: list[Path] = []
                for blob in snapshot.blobs:
                    destination = root.joinpath(*PurePosixPath(blob.path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(blob.content)
                    if os.name != "nt":
                        destination.chmod(0o700 if blob.executable else 0o600)
                    files.append(destination)
                return await run_self_review_static_sample(
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
            raise EvolutionInterventionalGreenSampleError(exc.code, str(exc)) from exc


def _build_receipt(
    *,
    green,
    red,
    plan,
    profile,
    sample_index,
    candidate_tree_sha256,
    stored,
):
    identity = stored.result.baseline_identity
    assert identity is not None
    checks = tuple(
        item for item in stored.result.cases if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
    )
    payload = {
        "schema_version": 1,
        "policy_version": INTERVENTIONAL_GREEN_SAMPLE_POLICY,
        "green_request_id": green.request_id,
        "green_request_sha256": green.request_sha256,
        "red_receipt_id": red.receipt_id,
        "red_receipt_sha256": red.receipt_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": profile.binding_id,
        "profile_binding_sha256": profile.binding_sha256,
        "lease_id": green.lease_id,
        "candidate_id": green.candidate_id,
        "candidate_revision": green.candidate_revision,
        "candidate_files_sha256": green.candidate_files_sha256,
        "candidate_tree_sha256": candidate_tree_sha256,
        "sample_index": sample_index,
        "sample_seed": green.sample_seeds[sample_index],
        "suite_id": green.suite_id,
        "batch_id": green.batch_id,
        "result_sha256": stored.result_sha256,
        "candidate_identity_sha256": identity.identity_sha256,
        "check_ids": [item.case_id for item in checks],
        "check_statuses": [item.code for item in checks],
        "lifecycle_receipt_sha256": [
            interventional_lifecycle_digest(item.message) for item in checks
        ],
        "profile_trust_revalidated": True,
        "red_cohort_revalidated": True,
        "candidate_snapshot_revalidated": True,
        "exact_revision_overlay_materialized": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "sample_complete": True,
        "completed_at": stored.created_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionInterventionalGreenSampleReceipt.model_validate({
        **payload,
        "receipt_id": f"evvgreencheck_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EvolutionInterventionalGreenSampleError",
    "EvolutionInterventionalGreenSampleExecutor",
    "EvolutionInterventionalGreenSampleReceipt",
    "INTERVENTIONAL_GREEN_SAMPLE_POLICY",
]
