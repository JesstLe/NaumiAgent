"""Continuous interventional RED cohorts with one cohort-scoped Run Grant."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
)
from naumi_agent.evolution.interventional_cohort_kernel import (
    EvolutionInterventionalCohortKernel,
    EvolutionInterventionalCohortKernelError,
)
from naumi_agent.evolution.interventional_red_sample import (
    INTERVENTIONAL_RED_CHECK_RUNNER,
    EvolutionInterventionalRedSampleError,
    EvolutionInterventionalRedSampleExecutor,
    EvolutionInterventionalRedSampleReceipt,
    validate_interventional_red_authority,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBinding,
)
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

INTERVENTIONAL_RED_COHORT_POLICY = "evolution-interventional-red-cohort-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class InterventionalRedMetricSummary(_StrictModel):
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    unit: Literal["count", "ratio", "milliseconds", "tokens", "usd", "scalar"]
    direction: Literal["decrease", "increase"]
    target: float
    sample_values: tuple[float, ...] = Field(min_length=5, max_length=100)


class InterventionalRedCheckSummary(_StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    passed: int = Field(ge=0, le=100)
    failed: int = Field(ge=0, le=100)
    evaluation_errors: int = Field(ge=0, le=100)


class EvolutionInterventionalRedCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-interventional-red-cohort-v1"] = (
        INTERVENTIONAL_RED_COHORT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvredcohort_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    sample_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    cohort_run_grant_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    metrics: tuple[InterventionalRedMetricSummary, ...] = Field(
        min_length=1,
        max_length=8,
    )
    checks: tuple[InterventionalRedCheckSummary, ...] = Field(
        min_length=1,
        max_length=80,
    )
    continuous_sample_indexes_verified: Literal[True] = True
    profile_trust_revalidated: Literal[True] = True
    exact_revision_materialized: Literal[True] = True
    cohort_scoped_run_grant_used: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    metrics_executed: Literal[True] = True
    cohort_complete: Literal[True] = True
    completed_at: str

    @field_validator("cohort_run_grant_sha256")
    @classmethod
    def _run_grants_are_ordered_sha256(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(_SHA256_RE, value) is None for value in values
        ):
            raise ValueError("Interventional RED Run Grant 摘要无效或重复。")
        return values

    @model_validator(mode="after")
    def _receipt_is_complete_and_tamper_evident(self) -> Self:
        count = self.requested_samples
        if not (
            self.persisted_samples
            == count
            == len(self.sample_seeds)
            == len(self.sample_receipt_sha256)
            == len(self.sample_result_sha256)
        ):
            raise ValueError("Interventional RED cohort 样本汇总不完整。")
        if any(len(item.sample_values) != count for item in self.metrics):
            raise ValueError("Interventional RED metric 样本数量不完整。")
        if any(
            item.passed + item.failed + item.evaluation_errors != count
            for item in self.checks
        ):
            raise ValueError("Interventional RED check 状态数量不完整。")
        if tuple(item.metric_name for item in self.metrics) != tuple(
            sorted({item.metric_name for item in self.metrics})
        ):
            raise ValueError("Interventional RED metrics 必须排序且不得重复。")
        if tuple(item.check_id for item in self.checks) != tuple(
            sorted({item.check_id for item in self.checks})
        ):
            raise ValueError("Interventional RED checks 必须排序且不得重复。")
        parsed = datetime.fromisoformat(self.completed_at)
        if parsed.utcoffset() is None:
            raise ValueError("Interventional RED completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Interventional RED cohort receipt 摘要不一致。")
        if self.receipt_id != f"evvredcohort_{expected[:24]}":
            raise ValueError("Interventional RED cohort receipt identity 不一致。")
        return self


class EvolutionInterventionalRedCohortError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalRedCohortExecutor:
    """Persist a continuous RED cohort under one revocable Run authority."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        sample_executor: EvolutionInterventionalRedSampleExecutor,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self._store = store
        self._sample_executor = sample_executor
        self._now = now or (lambda: datetime.now(UTC).isoformat())
        self._cohort_kernel = EvolutionInterventionalCohortKernel(
            workspace_root=self._workspace_root,
            store=store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            now=self._now,
            token=token,
        )

    async def execute(
        self,
        *,
        parent_receipt_id: str,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
    ) -> EvolutionInterventionalRedCohortReceipt:
        try:
            request, binding, plan, profile = validate_interventional_red_authority(
                baseline_request,
                metric_binding,
                validation_plan,
                profile_binding,
            )
        except EvolutionInterventionalRedSampleError as exc:
            raise EvolutionInterventionalRedCohortError(exc.code, str(exc)) from exc
        async def load_records():
            return await self._records(request)

        async def validate_existing(records):
            return await self._validate_existing_prefix(
                records,
                request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
            )

        def validate_evidence(records):
            _cohort_run_grant_digests(records, request)

        async def execute_sample(sample_index, authority):
            return await self._sample_executor.execute(
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                baseline_request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
                run_authority=authority,
            )

        def build_receipt(records, receipts):
            return _build_cohort_receipt(
                request,
                binding,
                plan,
                profile,
                records,
                receipts,
            )

        try:
            return await self._cohort_kernel.execute(
                phase="red",
                authority_key=request.request_sha256,
                parent_receipt_id=parent_receipt_id,
                requested_samples=request.requested_samples,
                max_total_duration_seconds=request.max_total_duration_seconds,
                load_records=load_records,
                validate_existing_prefix=validate_existing,
                validate_run_evidence=validate_evidence,
                execute_sample=execute_sample,
                build_receipt=build_receipt,
            )
        except EvolutionInterventionalCohortKernelError as exc:
            raise EvolutionInterventionalRedCohortError(exc.code, str(exc)) from exc

    async def _records(
        self,
        request: EvolutionBaselineCohortRequest,
    ) -> tuple[HarnessStoredEvalResult, ...]:
        return await self._store.list_eval_results(
            self._workspace_root,
            request.batch_id,
            request.suite_id,
            limit=request.requested_samples + 1,
        )

    async def _validate_existing_prefix(
        self,
        records: tuple[HarnessStoredEvalResult, ...],
        *,
        request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
    ) -> list[EvolutionInterventionalRedSampleReceipt]:
        receipts: list[EvolutionInterventionalRedSampleReceipt] = []
        for record in records:
            receipts.append(await self._sample_executor.execute(
                parent_receipt_id="existing-sample",
                sample_index=record.sample_index,
                baseline_request=request,
                metric_binding=metric_binding,
                validation_plan=validation_plan,
                profile_binding=profile_binding,
            ))
        return receipts

def _build_cohort_receipt(request, binding, plan, profile, records, receipts):
    if len(records) != request.requested_samples or len(receipts) != len(records):
        raise EvolutionInterventionalRedCohortError(
            "cohort_receipt_incomplete",
            "Interventional RED cohort completion evidence 不完整。",
        )
    cohort_run_grant_sha256 = _cohort_run_grant_digests(records, request)
    if not cohort_run_grant_sha256:
        raise EvolutionInterventionalRedCohortError(
            "cohort_run_authority_evidence_incomplete",
            "Interventional RED cohort 缺少 Run Grant evidence。",
        )
    metrics: list[InterventionalRedMetricSummary] = []
    for entry in sorted(binding.entries, key=lambda item: item.metric_name):
        observations = [
            observation
            for record in records
            for case in record.result.cases
            for observation in case.metric_observations
            if observation.metric == entry.metric_name
        ]
        if len(observations) != len(records):
            raise EvolutionInterventionalRedCohortError(
                "cohort_metric_evidence_incomplete",
                f"Interventional RED metric {entry.metric_name} 样本不完整。",
            )
        metrics.append(InterventionalRedMetricSummary(
            metric_name=entry.metric_name,
            unit=observations[0].unit,
            direction=entry.direction,
            target=entry.target,
            sample_values=tuple(item.value for item in observations),
        ))
    checks: list[InterventionalRedCheckSummary] = []
    for expected in request.checks:
        statuses = Counter(
            case.status.value
            for record in records
            for case in record.result.cases
            if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
            and case.case_id == expected.check_id
        )
        checks.append(InterventionalRedCheckSummary(
            check_id=expected.check_id,
            passed=statuses["passed"],
            failed=statuses["implementation_failure"],
            evaluation_errors=statuses["evaluation_error"],
        ))
    payload = {
        "schema_version": 1,
        "policy_version": INTERVENTIONAL_RED_COHORT_POLICY,
        "baseline_request_id": request.request_id,
        "baseline_request_sha256": request.request_sha256,
        "metric_binding_id": binding.binding_id,
        "metric_binding_sha256": binding.binding_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": profile.binding_id,
        "profile_binding_sha256": profile.binding_sha256,
        "suite_id": request.suite_id,
        "batch_id": request.batch_id,
        "baseline_commit": request.baseline_commit,
        "baseline_tree_sha256": request.baseline_tree_sha256,
        "requested_samples": request.requested_samples,
        "persisted_samples": len(records),
        "sample_seeds": list(request.sample_seeds),
        "sample_receipt_sha256": [item.receipt_sha256 for item in receipts],
        "sample_result_sha256": [item.result_sha256 for item in records],
        "cohort_run_grant_sha256": list(cohort_run_grant_sha256),
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "checks": [item.model_dump(mode="json") for item in checks],
        "continuous_sample_indexes_verified": True,
        "profile_trust_revalidated": True,
        "exact_revision_materialized": True,
        "cohort_scoped_run_grant_used": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "cohort_complete": True,
        "completed_at": max(item.created_at for item in records),
    }
    digest = _sha256_payload(payload)
    return EvolutionInterventionalRedCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evvredcohort_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _cohort_run_grant_digests(
    records: tuple[HarnessStoredEvalResult, ...],
    request: EvolutionBaselineCohortRequest,
) -> tuple[str, ...]:
    if not records:
        return ()

    check_cases = tuple(
        case
        for record in records
        for case in record.result.cases
        if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
    )
    authority_digests = [
        match.group(1)
        for case in check_cases
        if (match := re.search(
            r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )",
            case.message,
        )) is not None
        and re.search(r"(?:^| )run_scope=cohort(?:$| )", case.message) is not None
    ]
    run_grant_digests = set(authority_digests)
    expected_check_cases = len(records) * len(request.checks)
    if not (
        len(check_cases)
        == len(authority_digests)
        == expected_check_cases
        and run_grant_digests
    ):
        raise EvolutionInterventionalRedCohortError(
            "cohort_run_authority_evidence_incomplete",
            "Interventional RED samples 未完整绑定 cohort-scoped Run Grant。",
        )
    return tuple(sorted(run_grant_digests))


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
    "EvolutionInterventionalRedCohortError",
    "EvolutionInterventionalRedCohortExecutor",
    "EvolutionInterventionalRedCohortReceipt",
    "INTERVENTIONAL_RED_COHORT_POLICY",
    "InterventionalRedCheckSummary",
    "InterventionalRedMetricSummary",
]
