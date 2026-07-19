"""Shared governed ARC-04 execution kernel for interventional eval samples."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_plans import EvolutionValidationProfileBinding
from naumi_agent.harness.eval_identity import HarnessEvalBaselineIdentity
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
    HarnessSandboxSourceOverlay,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

_SHA256_RE = r"^[0-9a-f]{64}$"
INTERVENTIONAL_CHECK_RUNNER = "evolution_profile_check@1"
INTERVENTIONAL_SAMPLE_RUNNER = "evolution_interventional_red@1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EvolutionInterventionalRunAuthority(_StrictModel):
    parent_receipt_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_sha256: str = Field(pattern=_SHA256_RE)


class EvolutionInterventionalSampleKernelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


type SourceCurrent = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class EvolutionInterventionalSampleSource:
    revision: str
    revision_tree_sha256: str
    overlays: tuple[HarnessSandboxSourceOverlay, ...] = ()
    overlay_source_sha256: str | None = None
    source_is_current: SourceCurrent | None = None


type ExistingValidator = Callable[
    [HarnessStoredEvalResult, tuple[HarnessCheckSpec, ...]],
    None,
]
type SuiteBuilder = Callable[
    [
        list[HarnessSandboxCheckResult],
        Literal["sample", "cohort"],
        str,
    ],
    Awaitable[HarnessEvalSuiteResult],
]


class EvolutionInterventionalSampleKernel:
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
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.store = store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.profile_service = profile_service
        self.sandbox_runner = sandbox_runner
        self.shell_admission_composer = shell_admission_composer
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def execute(
        self,
        *,
        phase: Literal["red", "green"],
        authority_key: str,
        parent_receipt_id: str,
        sample_index: int,
        baseline_request: EvolutionBaselineCohortRequest,
        profile_binding: EvolutionValidationProfileBinding,
        batch_id: str,
        source: EvolutionInterventionalSampleSource,
        validate_existing: ExistingValidator,
        build_suite: SuiteBuilder,
        run_authority: EvolutionInterventionalRunAuthority | None = None,
    ) -> HarnessStoredEvalResult:
        request = EvolutionBaselineCohortRequest.model_validate(
            baseline_request.model_dump(mode="json")
        )
        profile = EvolutionValidationProfileBinding.model_validate(
            profile_binding.model_dump(mode="json")
        )
        if re.fullmatch(_SHA256_RE, authority_key) is None:
            raise EvolutionInterventionalSampleKernelError(
                "sample_authority_key_invalid",
                "Interventional sample authority key 必须是 SHA-256。",
            )
        if isinstance(sample_index, bool) or not 0 <= sample_index < request.requested_samples:
            raise EvolutionInterventionalSampleKernelError(
                "sample_index_invalid",
                f"Interventional {phase.upper()} sample_index 超出请求范围。",
            )
        checks = await self._current_checks(request, profile, phase=phase)
        existing = await self.store.get_eval_result(
            self.workspace_root,
            batch_id,
            request.suite_id,
            sample_index,
        )
        if existing is not None:
            validate_existing(existing, checks)
            return existing
        parent = await self.permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id:
            raise EvolutionInterventionalSampleKernelError(
                "parent_permission_invalid",
                f"Interventional {phase.upper()} 缺少可执行的父权限回执。",
            )
        if "bash_run" not in parent.delegated_tool_names:
            raise EvolutionInterventionalSampleKernelError(
                "parent_delegation_scope_missing",
                "父权限回执未授权 bash_run 运行委托。",
            )
        owned_lease = None
        grant_id: str | None = None
        grant_sha256: str | None = None
        if run_authority is not None:
            authority = EvolutionInterventionalRunAuthority.model_validate(
                run_authority.model_dump(mode="json")
            )
            validation = await self.run_grant_authority.validate(
                grant_id=authority.grant_id,
                now=self.now(),
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
                raise EvolutionInterventionalSampleKernelError(
                    "cohort_run_authority_invalid",
                    f"Interventional {phase.upper()} cohort Run authority 已失效或不匹配。",
                )
            grant_id = authority.grant_id
            grant_sha256 = authority.grant_sha256
        else:
            owner_id = f"evo-{phase}-{authority_key[:16]}-{sample_index}"
            lease_seconds = min(
                3_600,
                max(30, request.check_timeout_seconds_per_sample + 30),
            )
            owned_lease = await self.store.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=parent.run_id,
                owner_id=owner_id,
                now=self.now(),
                lease_seconds=lease_seconds,
            )
            if owned_lease is None:
                raise EvolutionInterventionalSampleKernelError(
                    "runtime_lease_unavailable",
                    f"Interventional {phase.upper()} 无法取得独占 Runtime lease。",
                )
            try:
                grant = await self.run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=(
                            f"evo-{phase}-{authority_key[:24]}-"
                            f"{sample_index}"
                        ),
                        parent_receipt_id=parent_receipt_id,
                        run_kind=HarnessRunKind.RUNTIME,
                        lease_owner_id=owner_id,
                        lease_epoch=owned_lease.epoch,
                        delegated_tool_names=("bash_run",),
                    ),
                    now=self.now(),
                    ttl_seconds=lease_seconds,
                )
            except BaseException as exc:
                await self._release_lease_after_issue_failure(parent.run_id, owned_lease, exc)
                raise
            grant_id = grant.contract.grant_id
            grant_sha256 = grant.contract.grant_sha256
        results: list[HarnessSandboxCheckResult] = []
        try:
            assert grant_id is not None and grant_sha256 is not None
            for check in checks:
                composed = None

                async def admit(spec, *, _grant_id=grant_id):
                    nonlocal composed
                    composed = await self.shell_admission_composer.compose(
                        parent_receipt_id=parent_receipt_id,
                        spec=spec,
                        run_grant_id=_grant_id,
                    )
                    return composed.admitted

                try:
                    result = await self.sandbox_runner.run(
                        run_id=_sample_run_id(parent.run_id, phase, sample_index, check.id),
                        check=check,
                        profile_digest=request.profile_sha256,
                        profile_is_current=lambda: self._profile_is_current(
                            request,
                            profile,
                            phase=phase,
                        ),
                        admit_job=admit,
                        source_revision=source.revision,
                        expected_source_tree_sha256=source.revision_tree_sha256,
                        source_overlays=source.overlays,
                        overlay_source_sha256=source.overlay_source_sha256,
                        source_is_current=source.source_is_current,
                    )
                    results.append(result)
                finally:
                    if composed is not None:
                        await composed.release()
            if not results or not all(
                item.job_id and item.lifecycle_receipt_sha256 for item in results
            ):
                raise EvolutionInterventionalSampleKernelError(
                    "project_code_not_executed",
                    f"Interventional {phase.upper()} 未形成完整 ARC-04 Worker 执行证据。",
                )
            suite = await build_suite(
                results,
                "cohort" if run_authority is not None else "sample",
                grant_sha256,
            )
            if source.source_is_current is not None and not await source.source_is_current():
                raise EvolutionInterventionalSampleKernelError(
                    "candidate_snapshot_changed_before_persistence",
                    "Candidate Snapshot 在 H5a 持久化前发生变化。",
                )
            return await self.store.record_eval_result(
                workspace_root=self.workspace_root,
                batch_id=batch_id,
                sample_index=sample_index,
                result=suite,
                created_at=self.now(),
            )
        finally:
            await self._cleanup_owned_authority(
                owned_lease=owned_lease,
                grant_id=grant_id,
                run_id=parent.run_id,
            )

    async def _current_checks(self, request, profile, *, phase: str):
        status = await self.profile_service.status()
        if not status.trusted or status.snapshot.profile is None:
            raise EvolutionInterventionalSampleKernelError(
                "profile_trust_revalidation_failed",
                f"Harness Profile 信任已失效，不能执行 Interventional {phase.upper()}。",
            )
        if status.profile_digest != request.profile_sha256:
            raise EvolutionInterventionalSampleKernelError(
                "profile_digest_drifted",
                f"当前 Harness Profile 已偏离 {phase.upper()} Request。",
            )
        by_id = {item.id: item for item in status.snapshot.profile.checks}
        checks = []
        for expected in request.checks:
            check = by_id.get(expected.check_id)
            bound = next(
                (item for item in profile.checks if item.check_id == expected.check_id),
                None,
            )
            if check is None or bound is None or not _check_matches(check, expected, bound):
                raise EvolutionInterventionalSampleKernelError(
                    "profile_check_drifted",
                    f"Harness Profile check {expected.check_id} 已漂移。",
                )
            checks.append(check)
        return tuple(checks)

    async def _profile_is_current(self, request, profile, *, phase: str) -> bool:
        try:
            await self._current_checks(request, profile, phase=phase)
        except EvolutionInterventionalSampleKernelError:
            return False
        return True

    async def _release_lease_after_issue_failure(self, run_id, lease, exc) -> None:
        try:
            released = await self.store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
                owner_id=lease.owner_id,
                epoch=lease.epoch,
                now=self.now(),
            )
            if released is None:
                exc.add_note("Run Grant 签发失败后 Runtime lease 未能释放。")
        except BaseException as cleanup_exc:
            exc.add_note(f"Runtime lease 清理失败：{cleanup_exc}")

    async def _cleanup_owned_authority(self, *, owned_lease, grant_id, run_id) -> None:
        if owned_lease is None:
            return
        cleanup_at = self.now()
        errors = []
        if grant_id is not None:
            try:
                await self.run_grant_authority.revoke(
                    grant_id=grant_id,
                    reason="sample_finished",
                    revoked_at=cleanup_at,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            released = await self.store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
                owner_id=owned_lease.owner_id,
                epoch=owned_lease.epoch,
                now=cleanup_at,
            )
            if released is None:
                errors.append(RuntimeError("Runtime lease 清理失败。"))
        except BaseException as exc:
            errors.append(exc)
        if errors:
            detail = "; ".join(str(item) for item in errors)
            raise EvolutionInterventionalSampleKernelError(
                "sample_authority_cleanup_failed",
                f"Interventional sample 权限清理不完整：{detail[:300]}",
            )


def build_interventional_sample_suite(
    request: EvolutionBaselineCohortRequest,
    results: list[HarnessSandboxCheckResult],
    *,
    phase: Literal["red", "green"],
    metric_result: HarnessEvalSuiteResult,
    identity: HarnessEvalBaselineIdentity,
    run_scope: Literal["sample", "cohort"],
    run_grant_sha256: str,
) -> HarnessEvalSuiteResult:
    """Build the identical comparable suite shape for RED and GREEN samples."""
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
        title=f"Evolution interventional {phase.upper()} sample",
        suite_path=f"evolution/{phase}/interventional",
        suite_sha256=identity.configuration.suite_sha256,
        status=status,
        cases=cases,
        code=f"interventional_{phase}_{status.value}",
        message=(
            f"精确 Git {'baseline' if phase == 'red' else 'candidate overlay'} 的 "
            "Profile checks 与可信 metrics 已完成。"
        ),
        comparison_policy=HarnessEvalComparisonPolicy(),
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in results) + metric_result.duration_ms,
    )


def interventional_lifecycle_digest(message: str) -> str | None:
    match = re.search(r"(?:^| )lifecycle_sha256=([0-9a-f]{64})(?:$| )", message)
    return match.group(1) if match is not None else None


def interventional_run_scope(message: str) -> str | None:
    match = re.search(r"(?:^| )run_scope=(sample|cohort)(?:$| )", message)
    return match.group(1) if match is not None else None


def interventional_run_grant_digest(message: str) -> str | None:
    match = re.search(r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )", message)
    return match.group(1) if match is not None else None


def _case_from_result(
    result: HarnessSandboxCheckResult,
    *,
    run_scope: Literal["sample", "cohort"],
    run_grant_sha256: str,
) -> HarnessEvalCaseResult:
    if result.status is HarnessSandboxCheckStatus.PASSED:
        status = EvalCaseStatus.PASSED
    elif result.status is HarnessSandboxCheckStatus.FAILED:
        status = EvalCaseStatus.IMPLEMENTATION_FAILURE
    else:
        status = EvalCaseStatus.EVALUATION_ERROR
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner=INTERVENTIONAL_CHECK_RUNNER,
        status=status,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256="
            f"{result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_scope={run_scope} run_grant_sha256={run_grant_sha256}"
        ),
        duration_ms=result.duration_ms,
    )


def _check_matches(check, expected, bound) -> bool:
    return (
        _sha256_payload(check.model_dump(mode="json")) == expected.spec_sha256 == bound.spec_sha256
        and _sha256_payload(list(check.argv)) == expected.argv_sha256 == bound.argv_sha256
        and check.timeout_seconds == expected.timeout_seconds == bound.timeout_seconds
    )


def _sample_run_id(parent_run_id: str, phase: str, sample_index: int, check_id: str) -> str:
    material = (
        f"{parent_run_id}:{sample_index}:{check_id}"
        if phase == "red"
        else f"{parent_run_id}:green:{sample_index}:{check_id}"
    )
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"evo{phase}-{digest[:32]}"


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EvolutionInterventionalRunAuthority",
    "EvolutionInterventionalSampleKernel",
    "EvolutionInterventionalSampleKernelError",
    "EvolutionInterventionalSampleSource",
    "INTERVENTIONAL_CHECK_RUNNER",
    "INTERVENTIONAL_SAMPLE_RUNNER",
    "build_interventional_sample_suite",
    "interventional_lifecycle_digest",
    "interventional_run_grant_digest",
    "interventional_run_scope",
]
