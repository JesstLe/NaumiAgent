"""Shared native Sandbox Eval execution service over H5a and ARC-04."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceiptStore,
    permission_arguments_sha256,
)
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.models import HarnessCheckSpec, HarnessProfile
from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchAdmission,
    HarnessSandboxBatchCheckpoint,
    HarnessSandboxBatchCoordinator,
)
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
    HarnessSandboxEvalSource,
)
from naumi_agent.harness.sandbox_request import (
    HarnessSandboxEvalRequest,
    validate_request_checks,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

SANDBOX_EVAL_SERVICE_POLICY = "harness-sandbox-eval-service-v1"
SANDBOX_EVAL_RUNNER = "harness_sandbox_eval@1"
SANDBOX_CHECK_RUNNER = "harness_profile_check@1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_GRANT_MARKER = re.compile(
    r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )"
)
_LIFECYCLE_MARKER = re.compile(
    r"(?:^| )lifecycle_sha256=([0-9a-f]{64})(?:$| )"
)

type SandboxEvalProgressCallback = Callable[
    [HarnessSandboxBatchCheckpoint],
    Awaitable[None],
]
type SandboxEvalProfileAuthorityProvider = Callable[
    [],
    Awaitable["HarnessSandboxEvalProfileAuthority"],
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HarnessSandboxEvalProfileAuthority(_StrictModel):
    """Current typed Profile authority supplied by the owning HarnessService."""

    profile: HarnessProfile
    profile_sha256: str = Field(pattern=_SHA256_RE)
    trusted: Literal[True] = True


class HarnessSandboxEvalSampleReceipt(_StrictModel):
    sample_index: int = Field(ge=0, le=99)
    request_sha256: str = Field(pattern=_SHA256_RE)
    result_sha256: str = Field(pattern=_SHA256_RE)
    identity_sha256: str = Field(pattern=_SHA256_RE)
    run_grant_sha256: str = Field(pattern=_SHA256_RE)


class HarnessSandboxEvalBatchReceipt(_StrictModel):
    """Complete, deterministic receipt for one persisted native Sandbox cohort."""

    schema_version: Literal[1] = 1
    policy_version: Literal["harness-sandbox-eval-service-v1"] = (
        SANDBOX_EVAL_SERVICE_POLICY
    )
    receipt_id: str = Field(pattern=r"^hsevalbatch_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^hseval_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^harness_sandbox_[0-9a-f]{24}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    identity_sha256: str = Field(pattern=_SHA256_RE)
    run_grant_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    continuous_sample_indexes_verified: Literal[True] = True
    profile_trust_revalidated: Literal[True] = True
    exact_revision_materialized: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    h5a_persisted: Literal[True] = True
    batch_complete: Literal[True] = True
    completed_at: str

    @field_validator("sample_result_sha256", "run_grant_sha256")
    @classmethod
    def _sha256_sequence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("Sandbox Eval receipt 含无效 SHA-256。")
        return values

    @field_validator("completed_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Sandbox Eval completed_at 必须包含时区。")
        return parsed.isoformat()

    @field_validator("check_ids")
    @classmethod
    def _valid_check_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None for value in values):
            raise ValueError("Sandbox Eval receipt 含无效 check id。")
        return values

    @model_validator(mode="after")
    def _receipt_is_complete_and_tamper_evident(self) -> Self:
        if not (
            self.requested_samples
            == self.persisted_samples
            == len(self.sample_result_sha256)
        ):
            raise ValueError("Sandbox Eval Batch receipt 样本不完整。")
        if self.run_grant_sha256 != tuple(sorted(set(self.run_grant_sha256))):
            raise ValueError("Sandbox Eval Run Grant 摘要必须排序且不得重复。")
        if self.check_ids != tuple(dict.fromkeys(self.check_ids)):
            raise ValueError("Sandbox Eval check_ids 不得重复。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Sandbox Eval Batch receipt 摘要不一致。")
        if self.receipt_id != f"hsevalbatch_{expected[:24]}":
            raise ValueError("Sandbox Eval Batch receipt identity 不一致。")
        return self


class HarnessSandboxEvalServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessSandboxEvalExecutor:
    """Execute one immutable native request through shared Harness authorities."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        execution_kernel: HarnessSandboxEvalExecutionKernel,
        admission: HarnessSandboxBatchAdmission,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if execution_kernel.workspace_root != self.workspace_root:
            raise ValueError("Sandbox Eval kernel 与 workspace 不一致。")
        if execution_kernel.permission_store is not permission_store:
            raise ValueError("Sandbox Eval kernel 与 Permission Store 不一致。")
        if execution_kernel.run_grant_authority is not run_grant_authority:
            raise ValueError("Sandbox Eval kernel 与 Run Grant authority 不一致。")
        self.store = store
        self.permission_store = permission_store
        self.execution_kernel = execution_kernel
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.coordinator = HarnessSandboxBatchCoordinator(
            workspace_root=self.workspace_root,
            store=store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            now=self.now,
            token=token,
            admission=admission,
        )

    async def execute(
        self,
        *,
        request: HarnessSandboxEvalRequest,
        parent_receipt_id: str,
        current_profile: SandboxEvalProfileAuthorityProvider,
        on_progress: SandboxEvalProgressCallback | None = None,
    ) -> HarnessSandboxEvalBatchReceipt:
        request = HarnessSandboxEvalRequest.model_validate(
            request.model_dump(mode="json")
        )
        if Path(request.workspace_root) != self.workspace_root:
            raise self._error(
                "workspace_mismatch",
                "Sandbox Eval Request 不属于当前工作区。",
            )
        if not callable(current_profile):
            raise TypeError("current_profile 必须可调用。")
        await self._validate_parent(request, parent_receipt_id)
        await self._current_checks(request, current_profile)
        identity, suite_sha256 = _baseline_identity(request)

        async def load_records() -> tuple[HarnessStoredEvalResult, ...]:
            return await self.store.list_eval_results(
                self.workspace_root,
                request.batch_id,
                request.suite_id,
                limit=request.requested_samples + 1,
            )

        async def validate_prefix(
            records: tuple[HarnessStoredEvalResult, ...],
        ) -> list[HarnessSandboxEvalSampleReceipt]:
            await self._current_checks(request, current_profile)
            return [
                _sample_receipt(
                    record,
                    request=request,
                    identity_sha256=identity.identity_sha256,
                    suite_sha256=suite_sha256,
                )
                for record in records
            ]

        def validate_evidence(
            records: tuple[HarnessStoredEvalResult, ...],
        ) -> None:
            for record in records:
                _sample_receipt(
                    record,
                    request=request,
                    identity_sha256=identity.identity_sha256,
                    suite_sha256=suite_sha256,
                )

        async def execute_sample(
            sample_index: int,
            run_authority: HarnessSandboxEvalRunAuthority,
        ) -> HarnessSandboxEvalSampleReceipt:
            current_checks = await self._current_checks(request, current_profile)
            results = await self.execution_kernel.execute(
                lane="sandbox",
                authority_key=request.request_sha256,
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                checks=current_checks,
                profile_digest=request.profile_sha256,
                profile_is_current=lambda: self._profile_is_current(
                    request,
                    current_profile,
                ),
                source=HarnessSandboxEvalSource(
                    revision=request.source_revision,
                    revision_tree_sha256=request.source_tree_sha256,
                ),
                run_authority=run_authority,
            )
            _validate_kernel_results(request, results)
            await self._current_checks(request, current_profile)
            suite = _build_suite_result(
                request,
                results,
                identity=identity,
                suite_sha256=suite_sha256,
                run_grant_sha256=run_authority.grant_sha256,
            )
            stored = await self.store.record_eval_result(
                workspace_root=self.workspace_root,
                batch_id=request.batch_id,
                sample_index=sample_index,
                result=suite,
                created_at=self._now(),
            )
            return _sample_receipt(
                stored,
                request=request,
                identity_sha256=identity.identity_sha256,
                suite_sha256=suite_sha256,
            )

        def build_receipt(
            records: tuple[HarnessStoredEvalResult, ...],
            receipts: list[HarnessSandboxEvalSampleReceipt],
        ) -> HarnessSandboxEvalBatchReceipt:
            return _build_batch_receipt(request, records, receipts)

        return await self.coordinator.execute(
            phase="sandbox",
            authority_key=request.request_sha256,
            parent_receipt_id=parent_receipt_id,
            requested_samples=request.requested_samples,
            max_total_duration_seconds=request.max_total_duration_seconds,
            load_records=load_records,
            validate_existing_prefix=validate_prefix,
            validate_run_evidence=validate_evidence,
            execute_sample=execute_sample,
            build_receipt=build_receipt,
            on_progress=on_progress,
        )

    async def _validate_parent(
        self,
        request: HarnessSandboxEvalRequest,
        parent_receipt_id: str,
    ) -> None:
        parent = await self.permission_store.get(parent_receipt_id)
        expected_arguments = {
            "batch_id": request.batch_id,
            "check_ids": [item.check_id for item in request.checks],
            "samples": request.requested_samples,
        }
        if (
            parent is None
            or not parent.authorizes_execution
            or not parent.run_id
            or parent.tool_name != "harness_eval_sandbox"
            or "bash_run" not in parent.delegated_tool_names
            or parent.arguments_sha256
            != permission_arguments_sha256(expected_arguments)
        ):
            raise self._error(
                "parent_permission_invalid",
                "Sandbox Eval 缺少与 checks、samples、batch 精确匹配的执行权限回执。",
            )

    async def _current_checks(
        self,
        request: HarnessSandboxEvalRequest,
        current_profile: SandboxEvalProfileAuthorityProvider,
    ) -> tuple[HarnessCheckSpec, ...]:
        try:
            authority = await current_profile()
        except HarnessSandboxEvalServiceError:
            raise
        except Exception as exc:
            raise self._error(
                "profile_revalidation_failed",
                "Sandbox Eval 无法复验当前 Harness Profile。",
            ) from exc
        if not isinstance(authority, HarnessSandboxEvalProfileAuthority):
            raise self._error(
                "profile_authority_invalid",
                "Sandbox Eval Profile authority 类型无效。",
            )
        if authority.profile_sha256 != request.profile_sha256:
            raise self._error(
                "profile_digest_drifted",
                "当前 Harness Profile 已偏离 Sandbox Eval Request。",
            )
        try:
            return validate_request_checks(request, authority.profile)
        except Exception as exc:
            raise self._error(
                "profile_check_drifted",
                "当前 Harness Profile checks 已偏离 Sandbox Eval Request。",
            ) from exc

    async def _profile_is_current(
        self,
        request: HarnessSandboxEvalRequest,
        current_profile: SandboxEvalProfileAuthorityProvider,
    ) -> bool:
        try:
            await self._current_checks(request, current_profile)
        except HarnessSandboxEvalServiceError:
            return False
        return True

    def _now(self) -> str:
        value = self.now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise self._error(
                "clock_invalid",
                "Sandbox Eval 时钟格式无效。",
            ) from exc
        if parsed.utcoffset() is None:
            raise self._error(
                "clock_invalid",
                "Sandbox Eval 时钟必须包含时区。",
            )
        return parsed.isoformat()

    @staticmethod
    def _error(suffix: str, message: str) -> HarnessSandboxEvalServiceError:
        return HarnessSandboxEvalServiceError(
            f"sandbox_eval_service_{suffix}",
            message,
        )


def _baseline_identity(
    request: HarnessSandboxEvalRequest,
) -> tuple[HarnessEvalBaselineIdentity, str]:
    comparison_policy = HarnessEvalComparisonPolicy()
    suite_sha256 = _sha256_payload({
        "policy_version": request.policy_version,
        "suite_id": request.suite_id,
        "checks": [item.model_dump(mode="json") for item in request.checks],
        "max_total_duration_seconds": request.max_total_duration_seconds,
        "network_access": request.network_access,
        "dependency_installation": request.dependency_installation,
    })
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id=request.suite_id,
        suite_sha256=suite_sha256,
        profile_sha256=request.profile_sha256,
        policy_sha256=comparison_policy.sha256,
        runner_version=SANDBOX_EVAL_RUNNER,
        repetitions=request.requested_samples,
        live=False,
    )
    identity = build_eval_baseline_identity(
        request.workspace_root,
        configuration=configuration,
        profile_trusted=True,
        source_identity=HarnessEvalSourceIdentity(
            commit=request.source_revision,
            tree_sha256=f"sha256:{request.source_tree_sha256}",
            dirty=False,
        ),
    )
    return identity, suite_sha256


def _build_suite_result(
    request: HarnessSandboxEvalRequest,
    results: tuple[HarnessSandboxCheckResult, ...],
    *,
    identity: HarnessEvalBaselineIdentity,
    suite_sha256: str,
    run_grant_sha256: str,
) -> HarnessEvalSuiteResult:
    cases = tuple(
        _case_from_result(item, run_grant_sha256=run_grant_sha256)
        for item in results
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
        title="Harness Sandbox Profile checks",
        suite_path="harness/sandbox/profile-checks",
        suite_sha256=suite_sha256,
        status=status,
        cases=cases,
        code=f"sandbox_{status.value}",
        message="精确 Git revision 的受信 Profile checks 已通过 ARC-04 Worker 执行。",
        comparison_policy=HarnessEvalComparisonPolicy(),
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in results),
    )


def _validate_kernel_results(
    request: HarnessSandboxEvalRequest,
    results: tuple[HarnessSandboxCheckResult, ...],
) -> None:
    expected_checks = tuple(item.check_id for item in request.checks)
    if (
        tuple(item.check_id for item in results) != expected_checks
        or any(
            item.profile_digest != request.profile_sha256
            or item.source_revision != request.source_revision
            or item.source_tree_sha256 != request.source_tree_sha256
            or not item.job_id
            or item.lifecycle_receipt_sha256 is None
            or re.fullmatch(_SHA256_RE, item.lifecycle_receipt_sha256) is None
            or re.fullmatch(_SHA256_RE, item.snapshot_manifest_sha256) is None
            for item in results
        )
    ):
        raise HarnessSandboxEvalServiceError(
            "sandbox_eval_service_kernel_evidence_invalid",
            "Sandbox Eval kernel 返回的 checks、源码或 ARC-04 证据不完整。",
        )


def _case_from_result(
    result: HarnessSandboxCheckResult,
    *,
    run_grant_sha256: str,
) -> HarnessEvalCaseResult:
    status = _eval_case_status(result.status.value)
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner=SANDBOX_CHECK_RUNNER,
        status=status,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256="
            f"{result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_scope=batch run_grant_sha256={run_grant_sha256}"
        ),
        duration_ms=result.duration_ms,
    )


def _sample_receipt(
    record: HarnessStoredEvalResult,
    *,
    request: HarnessSandboxEvalRequest,
    identity_sha256: str,
    suite_sha256: str,
) -> HarnessSandboxEvalSampleReceipt:
    result = record.result
    expected_checks = tuple(item.check_id for item in request.checks)
    expected_status = (
        EvalRunStatus.EVALUATION_ERROR
        if any(item.status is EvalCaseStatus.EVALUATION_ERROR for item in result.cases)
        else EvalRunStatus.FAILED
        if any(
            item.status is EvalCaseStatus.IMPLEMENTATION_FAILURE
            for item in result.cases
        )
        else EvalRunStatus.PASSED
    )
    if (
        record.workspace_root != request.workspace_root
        or record.batch_id != request.batch_id
        or record.suite_id != request.suite_id
        or record.identity_sha256 != identity_sha256
        or result.baseline_identity is None
        or result.baseline_identity.identity_sha256 != identity_sha256
        or result.suite_sha256 != suite_sha256
        or result.title != "Harness Sandbox Profile checks"
        or result.suite_path != "harness/sandbox/profile-checks"
        or result.status is not expected_status
        or result.code != f"sandbox_{expected_status.value}"
        or tuple(item.case_id for item in result.cases) != expected_checks
        or any(item.runner != SANDBOX_CHECK_RUNNER for item in result.cases)
        or any(
            item.code not in {value.value for value in HarnessSandboxCheckStatus}
            or item.status is not _eval_case_status(item.code)
            for item in result.cases
        )
    ):
        raise HarnessSandboxEvalServiceError(
            "sandbox_eval_service_h5a_authority_mismatch",
            "既有 H5a 样本不属于当前 Sandbox Eval Request。",
        )
    grants = {
        match.group(1)
        for item in result.cases
        if (match := _GRANT_MARKER.search(item.message)) is not None
    }
    lifecycles = tuple(
        _LIFECYCLE_MARKER.search(item.message) for item in result.cases
    )
    if (
        len(grants) != 1
        or any(item is None for item in lifecycles)
        or any("run_scope=batch" not in item.message for item in result.cases)
    ):
        raise HarnessSandboxEvalServiceError(
            "sandbox_eval_service_h5a_execution_evidence_invalid",
            "既有 H5a 样本缺少完整 ARC-04/Run Grant 执行证据。",
        )
    return HarnessSandboxEvalSampleReceipt(
        sample_index=record.sample_index,
        request_sha256=request.request_sha256,
        result_sha256=record.result_sha256,
        identity_sha256=identity_sha256,
        run_grant_sha256=next(iter(grants)),
    )


def _eval_case_status(check_status: str) -> EvalCaseStatus:
    if check_status == HarnessSandboxCheckStatus.PASSED.value:
        return EvalCaseStatus.PASSED
    if check_status == HarnessSandboxCheckStatus.FAILED.value:
        return EvalCaseStatus.IMPLEMENTATION_FAILURE
    return EvalCaseStatus.EVALUATION_ERROR


def _build_batch_receipt(
    request: HarnessSandboxEvalRequest,
    records: tuple[HarnessStoredEvalResult, ...],
    receipts: list[HarnessSandboxEvalSampleReceipt],
) -> HarnessSandboxEvalBatchReceipt:
    if len(records) != request.requested_samples or len(receipts) != len(records):
        raise HarnessSandboxEvalServiceError(
            "sandbox_eval_service_batch_incomplete",
            "Sandbox Eval Batch 尚未完整写入 H5a。",
        )
    identity_sha256 = {item.identity_sha256 for item in receipts}
    if len(identity_sha256) != 1:
        raise HarnessSandboxEvalServiceError(
            "sandbox_eval_service_identity_mismatch",
            "Sandbox Eval Batch 样本 identity 不一致。",
        )
    completed_at = max(
        datetime.fromisoformat(item.created_at) for item in records
    ).isoformat()
    payload = {
        "schema_version": 1,
        "policy_version": SANDBOX_EVAL_SERVICE_POLICY,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "suite_id": request.suite_id,
        "batch_id": request.batch_id,
        "requested_samples": request.requested_samples,
        "persisted_samples": len(records),
        "sample_result_sha256": [item.result_sha256 for item in records],
        "identity_sha256": next(iter(identity_sha256)),
        "run_grant_sha256": sorted({
            item.run_grant_sha256 for item in receipts
        }),
        "check_ids": [item.check_id for item in request.checks],
        "continuous_sample_indexes_verified": True,
        "profile_trust_revalidated": True,
        "exact_revision_materialized": True,
        "arc04_worker_used": True,
        "h5a_persisted": True,
        "batch_complete": True,
        "completed_at": completed_at,
    }
    digest = _sha256_payload(payload)
    return HarnessSandboxEvalBatchReceipt.model_validate({
        **payload,
        "receipt_id": f"hsevalbatch_{digest[:24]}",
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
    "HarnessSandboxEvalBatchReceipt",
    "HarnessSandboxEvalExecutor",
    "HarnessSandboxEvalProfileAuthority",
    "HarnessSandboxEvalSampleReceipt",
    "HarnessSandboxEvalServiceError",
    "SANDBOX_EVAL_RUNNER",
    "SANDBOX_EVAL_SERVICE_POLICY",
    "SandboxEvalProgressCallback",
]
