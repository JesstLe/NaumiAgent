"""Execute one exact-revision RED Profile-check sample through ARC-04."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
)
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.evolution.interventional_sample_kernel import (
    INTERVENTIONAL_CHECK_RUNNER,
    INTERVENTIONAL_SAMPLE_RUNNER,
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
    validate_self_review_cohort_authority,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedBaselineError,
    load_exact_validation_blobs,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBinding,
)
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import HarnessEvalComparisonPolicy, HarnessEvalSuiteResult
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckRunner
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

INTERVENTIONAL_RED_CHECK_RUNNER = INTERVENTIONAL_CHECK_RUNNER
INTERVENTIONAL_RED_SAMPLE_RUNNER = INTERVENTIONAL_SAMPLE_RUNNER
INTERVENTIONAL_RED_SAMPLE_POLICY = "evolution-interventional-red-sample-v2"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionInterventionalRedCheckSampleReceipt(_StrictModel):
    schema_version: Literal[2] = 2
    policy_version: Literal["evolution-interventional-red-sample-v2"] = (
        INTERVENTIONAL_RED_SAMPLE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvredcheck_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    result_sha256: str = Field(pattern=_SHA256_RE)
    baseline_identity_sha256: str = Field(pattern=_SHA256_RE)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    check_statuses: tuple[str, ...] = Field(min_length=1, max_length=80)
    lifecycle_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    profile_trust_revalidated: Literal[True] = True
    exact_revision_materialized: Literal[True] = True
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
            raise ValueError("Interventional RED check/status 数量不一致。")
        if self.check_ids != tuple(sorted(set(self.check_ids))):
            raise ValueError("Interventional RED checks 必须排序且不得重复。")
        parsed = datetime.fromisoformat(self.completed_at)
        if parsed.utcoffset() is None:
            raise ValueError("Interventional RED completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Interventional RED receipt 摘要不一致。")
        if self.receipt_id != f"evvredcheck_{expected[:24]}":
            raise ValueError("Interventional RED receipt identity 不一致。")
        return self


class EvolutionInterventionalRedCheckSampleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalRedRunAuthority(_StrictModel):
    parent_receipt_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_sha256: str = Field(pattern=_SHA256_RE)


class EvolutionInterventionalRedCheckSampleExecutor:
    """Run and persist exactly one Profile-check sample, not a whole cohort."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        profile_service: HarnessService,
        sandbox_runner: HarnessSandboxCheckRunner,
        shell_admission_composer: ShellWorkerAdmissionComposer,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self._kernel = EvolutionInterventionalSampleKernel(
            workspace_root=self._workspace_root,
            store=store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            profile_service=profile_service,
            sandbox_runner=sandbox_runner,
            shell_admission_composer=shell_admission_composer,
            now=now,
        )

    @property
    def sample_kernel(self) -> EvolutionInterventionalSampleKernel:
        return self._kernel

    async def execute(
        self,
        *,
        parent_receipt_id: str,
        sample_index: int,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        run_authority: EvolutionInterventionalRedRunAuthority | None = None,
    ) -> EvolutionInterventionalRedCheckSampleReceipt:
        request, metrics, plan, profile = _validate_authority(
            baseline_request,
            metric_binding,
            validation_plan,
            profile_binding,
        )
        configuration, identity = build_interventional_identity(
            request,
            metrics,
            plan,
            profile,
        )

        def validate_existing(
            stored: HarnessStoredEvalResult,
            checks: tuple[HarnessCheckSpec, ...],
        ) -> None:
            _validate_existing(stored, request, checks, metrics, plan, profile)

        async def build_suite(results, run_scope, grant_sha256):
            metric_result = await _run_metric_sample(
                workspace_root=self._workspace_root,
                request=request,
                binding=metrics,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
            return build_interventional_sample_suite(
                request,
                results,
                phase="red",
                metric_result=metric_result,
                identity=identity,
                run_scope=run_scope,
                run_grant_sha256=grant_sha256,
            )

        try:
            stored = await self._kernel.execute(
                phase="red",
                authority_key=request.request_sha256,
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                baseline_request=request,
                profile_binding=profile,
                batch_id=request.batch_id,
                source=EvolutionInterventionalSampleSource(
                    revision=request.baseline_commit,
                    revision_tree_sha256=request.baseline_tree_sha256,
                ),
                validate_existing=validate_existing,
                build_suite=build_suite,
                run_authority=run_authority,
            )
        except EvolutionInterventionalSampleKernelError as exc:
            raise EvolutionInterventionalRedCheckSampleError(exc.code, str(exc)) from exc
        return _build_receipt(
            request=request,
            metrics=metrics,
            plan=plan,
            profile=profile,
            sample_index=sample_index,
            stored=stored,
        )


def _validate_authority(baseline_request, metric_binding, validation_plan, profile_binding):
    try:
        request = EvolutionBaselineCohortRequest.model_validate(
            baseline_request.model_dump(mode="json")
        )
        metrics = EvolutionMetricRunnerBinding.model_validate(
            metric_binding.model_dump(mode="json")
        )
        plan = EvolutionValidationPlan.model_validate(validation_plan.model_dump(mode="json"))
        profile = EvolutionValidationProfileBinding.model_validate(
            profile_binding.model_dump(mode="json")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionInterventionalRedCheckSampleError(
            "sample_authority_invalid", "Interventional RED authority artifact 无效。"
        ) from exc
    if not (
        request.validation_plan_id == plan.validation_plan_id
        and request.validation_plan_sha256 == plan.validation_plan_sha256
        and request.profile_binding_id == profile.binding_id
        and request.profile_binding_sha256 == profile.binding_sha256
        and request.profile_sha256 == profile.profile_sha256 == plan.profile_sha256
        and metrics.baseline_request_id == request.request_id
        and metrics.baseline_request_sha256 == request.request_sha256
        and metrics.validation_plan_id == plan.validation_plan_id
        and metrics.validation_plan_sha256 == plan.validation_plan_sha256
    ):
        raise EvolutionInterventionalRedCheckSampleError(
            "sample_authority_mismatch", "Interventional RED authority binding 不一致。"
        )
    try:
        validate_self_review_cohort_authority(request, metrics, plan)
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionInterventionalRedCheckSampleError(
            exc.code,
            str(exc),
        ) from exc
    return request, metrics, plan, profile


def validate_interventional_red_authority(
    baseline_request: EvolutionBaselineCohortRequest,
    metric_binding: EvolutionMetricRunnerBinding,
    validation_plan: EvolutionValidationPlan,
    profile_binding: EvolutionValidationProfileBinding,
) -> tuple[
    EvolutionBaselineCohortRequest,
    EvolutionMetricRunnerBinding,
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
]:
    """Revalidate the complete immutable authority chain before execution."""
    return _validate_authority(
        baseline_request,
        metric_binding,
        validation_plan,
        profile_binding,
    )


def _check_matches(check, expected, bound) -> bool:
    return (
        _sha256_payload(check.model_dump(mode="json")) == expected.spec_sha256 == bound.spec_sha256
        and _sha256_payload(list(check.argv)) == expected.argv_sha256 == bound.argv_sha256
        and check.timeout_seconds == expected.timeout_seconds == bound.timeout_seconds
    )


def _sample_run_id(parent_run_id: str, sample_index: int, check_id: str) -> str:
    digest = hashlib.sha256(f"{parent_run_id}:{sample_index}:{check_id}".encode()).hexdigest()
    return f"evored-{digest[:32]}"


def build_interventional_configuration(request, metrics, plan, profile):
    policy = HarnessEvalComparisonPolicy()
    return HarnessEvalConfigurationIdentity.create(
        suite_id=request.suite_id,
        suite_sha256=interventional_suite_sha(request, metrics, plan, profile),
        profile_sha256=request.profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=INTERVENTIONAL_RED_SAMPLE_RUNNER,
        repetitions=request.requested_samples,
        live=False,
    )


def build_interventional_identity(request, metrics, plan, profile):
    configuration = build_interventional_configuration(request, metrics, plan, profile)
    identity = build_eval_baseline_identity(
        ".",
        configuration=configuration,
        platform_identity=capture_eval_platform_identity(),
        profile_trusted=True,
        source_identity=HarnessEvalSourceIdentity(
            commit=request.baseline_commit,
            tree_sha256=f"sha256:{request.baseline_tree_sha256}",
            dirty=False,
        ),
    )
    return configuration, identity


async def _run_metric_sample(
    *,
    workspace_root: Path,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity,
) -> HarnessEvalSuiteResult:
    try:
        blobs = load_exact_validation_blobs(workspace_root, request, plan)
        with TemporaryDirectory(prefix="naumi-evo-interventional-red-") as temporary:
            scan_root = Path(temporary).resolve()
            files: list[Path] = []
            for relative, content in blobs:
                destination = scan_root.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                files.append(destination)
            return await run_self_review_static_sample(
                files=files,
                scan_root=scan_root,
                phase="red",
                request=request,
                binding=binding,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
    except (EvolutionSelfReviewRedBaselineError, SelfReviewEvalRuntimeError) as exc:
        raise EvolutionInterventionalRedCheckSampleError(
            getattr(exc, "code", "metric_execution_failed"),
            str(exc),
        ) from exc


def _validate_existing(
    stored: HarnessStoredEvalResult,
    request: EvolutionBaselineCohortRequest,
    checks: tuple[HarnessCheckSpec, ...],
    metrics: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    profile: EvolutionValidationProfileBinding,
) -> None:
    result = stored.result
    identity = result.baseline_identity
    check_cases = tuple(
        item for item in result.cases
        if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
    )
    metric_cases = tuple(
        item for item in result.cases
        if item.runner == SELF_REVIEW_STATIC_RUNNER_VERSION
    )
    expected_suite_sha256 = interventional_suite_sha(
        request,
        metrics,
        plan,
        profile,
    )
    observed_metrics = tuple(
        case.metric_observations[0].metric
        for case in metric_cases
        if len(case.metric_observations) == 1
    )
    if not (
        result.suite_id == request.suite_id
        and identity is not None
        and identity.source.commit == request.baseline_commit
        and identity.source.tree_sha256 == f"sha256:{request.baseline_tree_sha256}"
        and identity.configuration.profile_sha256 == request.profile_sha256
        and identity.configuration.runner_version == INTERVENTIONAL_RED_SAMPLE_RUNNER
        and identity.configuration.suite_sha256 == expected_suite_sha256
        and result.suite_sha256 == identity.configuration.suite_sha256
        and tuple(item.case_id for item in check_cases) == tuple(item.id for item in checks)
        and all(interventional_lifecycle_digest(item.message) is not None for item in check_cases)
        and all(interventional_run_scope(item.message) is not None for item in check_cases)
        and all(interventional_run_grant_digest(item.message) is not None for item in check_cases)
        and len(metric_cases) == len(request.metrics)
        and all(item.metric_observations for item in metric_cases)
        and observed_metrics == tuple(item.metric_name for item in request.metrics)
        and len(check_cases) + len(metric_cases) == len(result.cases)
    ):
        raise EvolutionInterventionalRedCheckSampleError(
            "existing_sample_conflict", "已有 H5a sample 不属于当前 Interventional RED authority。"
        )


def _build_receipt(*, request, metrics, plan, profile, sample_index, stored):
    identity = stored.result.baseline_identity
    assert identity is not None
    check_cases = tuple(
        item for item in stored.result.cases
        if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
    )
    payload = {
        "schema_version": 2,
        "policy_version": INTERVENTIONAL_RED_SAMPLE_POLICY,
        "baseline_request_id": request.request_id,
        "baseline_request_sha256": request.request_sha256,
        "metric_binding_id": metrics.binding_id,
        "metric_binding_sha256": metrics.binding_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": profile.binding_id,
        "profile_binding_sha256": profile.binding_sha256,
        "sample_index": sample_index,
        "sample_seed": request.sample_seeds[sample_index],
        "suite_id": request.suite_id,
        "batch_id": request.batch_id,
        "result_sha256": stored.result_sha256,
        "baseline_identity_sha256": identity.identity_sha256,
        "check_ids": [item.case_id for item in check_cases],
        "check_statuses": [item.code for item in check_cases],
        "lifecycle_receipt_sha256": [
            _require_lifecycle_digest(item.message) for item in check_cases
        ],
        "profile_trust_revalidated": True,
        "exact_revision_materialized": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "sample_complete": True,
        "completed_at": stored.created_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionInterventionalRedCheckSampleReceipt.model_validate({
        **payload,
        "receipt_id": f"evvredcheck_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def interventional_suite_sha(request, metrics, plan, profile) -> str:
    return _sha256_payload({
        "request_sha256": request.request_sha256,
        "metric_binding_sha256": metrics.binding_sha256,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_sha256": profile.binding_sha256,
        "runner": INTERVENTIONAL_RED_SAMPLE_RUNNER,
    })


def _require_lifecycle_digest(message: str) -> str:
    digest = interventional_lifecycle_digest(message)
    if digest is None:
        raise EvolutionInterventionalRedCheckSampleError(
            "lifecycle_receipt_missing",
            "H5a sample 缺少 ARC-04 lifecycle receipt 摘要。",
        )
    return digest


EvolutionInterventionalRedSampleError = EvolutionInterventionalRedCheckSampleError
EvolutionInterventionalRedSampleExecutor = EvolutionInterventionalRedCheckSampleExecutor
EvolutionInterventionalRedSampleReceipt = EvolutionInterventionalRedCheckSampleReceipt
INTERVENTIONAL_RED_CHECK_POLICY = INTERVENTIONAL_RED_SAMPLE_POLICY


__all__ = [
    "EvolutionInterventionalRedCheckSampleError",
    "EvolutionInterventionalRedCheckSampleExecutor",
    "EvolutionInterventionalRedCheckSampleReceipt",
    "EvolutionInterventionalRedSampleError",
    "EvolutionInterventionalRedSampleExecutor",
    "EvolutionInterventionalRedSampleReceipt",
    "EvolutionInterventionalRedRunAuthority",
    "INTERVENTIONAL_RED_CHECK_POLICY",
    "INTERVENTIONAL_RED_CHECK_RUNNER",
    "INTERVENTIONAL_RED_SAMPLE_POLICY",
    "INTERVENTIONAL_RED_SAMPLE_RUNNER",
    "build_interventional_configuration",
    "build_interventional_identity",
    "interventional_suite_sha",
    "validate_interventional_red_authority",
]
