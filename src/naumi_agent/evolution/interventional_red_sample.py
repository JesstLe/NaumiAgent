"""Execute one exact-revision RED Profile-check sample through ARC-04."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
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
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckRunner,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

INTERVENTIONAL_RED_CHECK_RUNNER = "evolution_profile_check@1"
INTERVENTIONAL_RED_SAMPLE_RUNNER = "evolution_interventional_red@1"
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
        self._store = store
        self._permission_store = permission_store
        self._run_grant_authority = run_grant_authority
        self._profile_service = profile_service
        self._sandbox_runner = sandbox_runner
        self._shell_admission_composer = shell_admission_composer
        self._now = now or (lambda: datetime.now(UTC).isoformat())

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
        if isinstance(sample_index, bool) or not 0 <= sample_index < request.requested_samples:
            raise EvolutionInterventionalRedCheckSampleError(
                "sample_index_invalid",
                "Interventional RED sample_index 超出请求范围。",
            )
        checks = await self._current_checks(request, profile)
        existing = await self._store.get_eval_result(
            self._workspace_root,
            request.batch_id,
            request.suite_id,
            sample_index,
        )
        if existing is not None:
            _validate_existing(
                existing,
                request,
                checks,
                metrics,
                plan,
                profile,
            )
            return _build_receipt(
                request=request,
                metrics=metrics,
                plan=plan,
                profile=profile,
                sample_index=sample_index,
                stored=existing,
            )

        parent = await self._permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id:
            raise EvolutionInterventionalRedCheckSampleError(
                "parent_permission_invalid",
                "Interventional RED 缺少可执行的父权限回执。",
            )
        if "bash_run" not in parent.delegated_tool_names:
            raise EvolutionInterventionalRedCheckSampleError(
                "parent_delegation_scope_missing",
                "父权限回执未授权 bash_run 运行委托。",
            )
        owned_lease = None
        grant_id: str | None = None
        grant_sha256: str | None = None
        if run_authority is not None:
            authority = EvolutionInterventionalRedRunAuthority.model_validate(
                run_authority.model_dump(mode="json")
            )
            validation = await self._run_grant_authority.validate(
                grant_id=authority.grant_id,
                now=self._now(),
            )
            contract = validation.contract
            if not (
                validation.allowed
                and contract is not None
                and authority.parent_receipt_id == parent_receipt_id
                and authority.run_id == parent.run_id == contract.run_id
                and authority.grant_id == contract.grant_id
                and authority.grant_sha256 == contract.grant_sha256
                and contract.parent_receipt_id == parent_receipt_id
                and "bash_run" in contract.delegated_tool_names
            ):
                raise EvolutionInterventionalRedCheckSampleError(
                    "cohort_run_authority_invalid",
                    "Interventional RED cohort Run authority 已失效或不匹配。",
                )
            grant_id = authority.grant_id
            grant_sha256 = authority.grant_sha256
        else:
            owner_id = f"evo-red-{request.request_sha256[:16]}-{sample_index}"
            started_at = self._now()
            lease_seconds = min(
                3_600,
                max(30, request.check_timeout_seconds_per_sample + 30),
            )
            owned_lease = await self._store.acquire_run_lease(
                workspace_root=self._workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=parent.run_id,
                owner_id=owner_id,
                now=started_at,
                lease_seconds=lease_seconds,
            )
            if owned_lease is None:
                raise EvolutionInterventionalRedCheckSampleError(
                    "runtime_lease_unavailable",
                    "Interventional RED 无法取得独占 Runtime lease。",
                )
            try:
                grant = await self._run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=(
                            f"evo-red-{request.request_sha256[:24]}-{sample_index}"
                        ),
                        parent_receipt_id=parent_receipt_id,
                        run_kind=HarnessRunKind.RUNTIME,
                        lease_owner_id=owner_id,
                        lease_epoch=owned_lease.epoch,
                        delegated_tool_names=("bash_run",),
                    ),
                    now=self._now(),
                    ttl_seconds=lease_seconds,
                )
            except BaseException as exc:
                try:
                    released = await self._store.release_run_lease(
                        workspace_root=self._workspace_root,
                        run_kind=HarnessRunKind.RUNTIME,
                        run_id=parent.run_id,
                        owner_id=owned_lease.owner_id,
                        epoch=owned_lease.epoch,
                        now=self._now(),
                    )
                    if released is None:
                        exc.add_note("Run Grant 签发失败后 Runtime lease 未能释放。")
                except BaseException as cleanup_exc:
                    exc.add_note(f"Runtime lease 清理失败：{cleanup_exc}")
                raise
            grant_id = grant.contract.grant_id
            grant_sha256 = grant.contract.grant_sha256
        results: list[HarnessSandboxCheckResult] = []
        try:
            assert grant_id is not None
            assert grant_sha256 is not None
            for check in checks:
                composed = None

                async def admit(spec, *, _grant_id=grant_id):
                    nonlocal composed
                    composed = await self._shell_admission_composer.compose(
                        parent_receipt_id=parent_receipt_id,
                        spec=spec,
                        run_grant_id=_grant_id,
                    )
                    return composed.admitted

                try:
                    result = await self._sandbox_runner.run(
                        run_id=_sample_run_id(parent.run_id, sample_index, check.id),
                        check=check,
                        profile_digest=request.profile_sha256,
                        profile_is_current=lambda: self._profile_is_current(request, profile),
                        admit_job=admit,
                        source_revision=request.baseline_commit,
                        expected_source_tree_sha256=request.baseline_tree_sha256,
                    )
                    results.append(result)
                finally:
                    if composed is not None:
                        await composed.release()
            if not results or not all(
                item.job_id and item.lifecycle_receipt_sha256 for item in results
            ):
                raise EvolutionInterventionalRedCheckSampleError(
                    "project_code_not_executed",
                    "Interventional RED 未形成完整 ARC-04 Worker 执行证据。",
                )
            configuration, identity = _build_identity(
                request,
                metrics,
                plan,
                profile,
            )
            metric_result = await _run_metric_sample(
                workspace_root=self._workspace_root,
                request=request,
                binding=metrics,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
            suite = _build_suite_result(
                request,
                results,
                metric_result=metric_result,
                configuration=configuration,
                identity=identity,
                run_scope=("cohort" if run_authority is not None else "sample"),
                run_grant_sha256=grant_sha256,
            )
            stored = await self._store.record_eval_result(
                workspace_root=self._workspace_root,
                batch_id=request.batch_id,
                sample_index=sample_index,
                result=suite,
                created_at=self._now(),
            )
            return _build_receipt(
                request=request,
                metrics=metrics,
                plan=plan,
                profile=profile,
                sample_index=sample_index,
                stored=stored,
            )
        finally:
            cleanup_at = self._now()
            cleanup_errors: list[BaseException] = []
            if owned_lease is not None and grant_id is not None:
                try:
                    await self._run_grant_authority.revoke(
                        grant_id=grant_id,
                        reason="sample_finished",
                        revoked_at=cleanup_at,
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if owned_lease is not None:
                try:
                    released = await self._store.release_run_lease(
                        workspace_root=self._workspace_root,
                        run_kind=HarnessRunKind.RUNTIME,
                        run_id=parent.run_id,
                        owner_id=owned_lease.owner_id,
                        epoch=owned_lease.epoch,
                        now=cleanup_at,
                    )
                    if released is None:
                        cleanup_errors.append(RuntimeError("Runtime lease 清理失败。"))
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                detail = "; ".join(str(item) for item in cleanup_errors)
                raise EvolutionInterventionalRedCheckSampleError(
                    "sample_authority_cleanup_failed",
                    f"Interventional RED 权限清理不完整：{detail[:300]}",
                )

    async def _current_checks(
        self,
        request: EvolutionBaselineCohortRequest,
        profile_binding: EvolutionValidationProfileBinding,
    ) -> tuple[HarnessCheckSpec, ...]:
        status = await self._profile_service.status()
        if not status.trusted or status.snapshot.profile is None:
            raise EvolutionInterventionalRedCheckSampleError(
                "profile_trust_revalidation_failed",
                "Harness Profile 信任已失效，不能执行 Interventional RED。",
            )
        if status.profile_digest != request.profile_sha256:
            raise EvolutionInterventionalRedCheckSampleError(
                "profile_digest_drifted",
                "当前 Harness Profile 已偏离 RED Request。",
            )
        by_id = {item.id: item for item in status.snapshot.profile.checks}
        checks: list[HarnessCheckSpec] = []
        for expected in request.checks:
            check = by_id.get(expected.check_id)
            bound = next(
                (item for item in profile_binding.checks if item.check_id == expected.check_id),
                None,
            )
            if check is None or bound is None or not _check_matches(check, expected, bound):
                raise EvolutionInterventionalRedCheckSampleError(
                    "profile_check_drifted",
                    f"Harness Profile check {expected.check_id} 已漂移。",
                )
            checks.append(check)
        return tuple(checks)

    async def _profile_is_current(
        self,
        request: EvolutionBaselineCohortRequest,
        profile_binding: EvolutionValidationProfileBinding,
    ) -> bool:
        try:
            await self._current_checks(request, profile_binding)
        except EvolutionInterventionalRedCheckSampleError:
            return False
        return True


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


def _build_suite_result(
    request: EvolutionBaselineCohortRequest,
    results: list[HarnessSandboxCheckResult],
    *,
    metric_result: HarnessEvalSuiteResult,
    configuration: HarnessEvalConfigurationIdentity,
    identity,
    run_scope: Literal["sample", "cohort"],
    run_grant_sha256: str | None,
) -> HarnessEvalSuiteResult:
    policy = HarnessEvalComparisonPolicy()
    cases = (
        tuple(
            _case_from_result(
                item,
                run_scope=run_scope,
                run_grant_sha256=run_grant_sha256,
            )
            for item in results
        )
        + metric_result.cases
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
        title="Evolution interventional RED sample",
        suite_path="evolution/red/interventional",
        suite_sha256=configuration.suite_sha256,
        status=status,
        cases=cases,
        code=f"interventional_red_{status.value}",
        message="精确 Git baseline 的 Profile checks 与可信 metrics 已完成。",
        comparison_policy=policy,
        baseline_identity=identity,
        duration_ms=(
            sum(item.duration_ms for item in results) + metric_result.duration_ms
        ),
    )


def _build_identity(request, metrics, plan, profile):
    policy = HarnessEvalComparisonPolicy()
    suite_sha256 = _interventional_suite_sha(request, metrics, plan, profile)
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id=request.suite_id,
        suite_sha256=suite_sha256,
        profile_sha256=request.profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=INTERVENTIONAL_RED_SAMPLE_RUNNER,
        repetitions=request.requested_samples,
        live=False,
    )
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


def _case_from_result(
    result: HarnessSandboxCheckResult,
    *,
    run_scope: Literal["sample", "cohort"],
    run_grant_sha256: str | None,
) -> HarnessEvalCaseResult:
    if result.status is HarnessSandboxCheckStatus.PASSED:
        status = EvalCaseStatus.PASSED
    elif result.status is HarnessSandboxCheckStatus.FAILED:
        status = EvalCaseStatus.IMPLEMENTATION_FAILURE
    else:
        status = EvalCaseStatus.EVALUATION_ERROR
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner=INTERVENTIONAL_RED_CHECK_RUNNER,
        status=status,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256="
            f"{result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_scope={run_scope} "
            f"run_grant_sha256={run_grant_sha256 or 'missing'}"
        ),
        duration_ms=result.duration_ms,
    )


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
    expected_suite_sha256 = _interventional_suite_sha(
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
        and all(_lifecycle_digest(item.message) is not None for item in check_cases)
        and all(_run_scope(item.message) is not None for item in check_cases)
        and all(_run_grant_digest(item.message) is not None for item in check_cases)
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


def _interventional_suite_sha(request, metrics, plan, profile) -> str:
    return _sha256_payload({
        "request_sha256": request.request_sha256,
        "metric_binding_sha256": metrics.binding_sha256,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_sha256": profile.binding_sha256,
        "runner": INTERVENTIONAL_RED_SAMPLE_RUNNER,
    })


def _lifecycle_digest(message: str) -> str | None:
    match = re.search(r"(?:^| )lifecycle_sha256=([0-9a-f]{64})(?:$| )", message)
    return match.group(1) if match is not None else None


def _run_scope(message: str) -> str | None:
    match = re.search(r"(?:^| )run_scope=(sample|cohort)(?:$| )", message)
    return match.group(1) if match is not None else None


def _run_grant_digest(message: str) -> str | None:
    match = re.search(r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )", message)
    return match.group(1) if match is not None else None


def _require_lifecycle_digest(message: str) -> str:
    digest = _lifecycle_digest(message)
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
    "validate_interventional_red_authority",
]
