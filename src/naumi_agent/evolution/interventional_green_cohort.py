"""Continuous interventional GREEN cohorts with candidate identity closure."""

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
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.interventional_cohort_kernel import (
    EvolutionInterventionalCohortKernel,
    EvolutionInterventionalCohortKernelError,
)
from naumi_agent.evolution.interventional_green_request import (
    EvolutionInterventionalGreenCohortRequest,
)
from naumi_agent.evolution.interventional_green_sample import (
    EvolutionInterventionalGreenSampleError,
    EvolutionInterventionalGreenSampleExecutor,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortReceipt,
)
from naumi_agent.evolution.interventional_red_sample import (
    INTERVENTIONAL_RED_CHECK_RUNNER,
)
from naumi_agent.evolution.interventional_sample_kernel import (
    interventional_run_grant_digest,
    interventional_run_scope,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.store import HarnessStore

INTERVENTIONAL_GREEN_COHORT_POLICY = "evolution-interventional-green-cohort-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class InterventionalGreenMetricSummary(_StrictModel):
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    unit: Literal["count", "ratio", "milliseconds", "tokens", "usd", "scalar"]
    direction: Literal["decrease", "increase"]
    target: float
    sample_values: tuple[float, ...] = Field(min_length=5, max_length=100)


class InterventionalGreenCheckSummary(_StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    passed: int = Field(ge=0, le=100)
    failed: int = Field(ge=0, le=100)
    evaluation_errors: int = Field(ge=0, le=100)


class EvolutionInterventionalGreenCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-interventional-green-cohort-v1"] = (
        INTERVENTIONAL_GREEN_COHORT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvgreencohort_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    green_request_id: str = Field(pattern=r"^evvgreenint_[0-9a-f]{24}$")
    green_request_sha256: str = Field(pattern=_SHA256_RE)
    red_receipt_id: str = Field(pattern=r"^evvredcohort_[0-9a-f]{24}$")
    red_receipt_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    candidate_identity_sha256: str = Field(pattern=_SHA256_RE)
    candidate_tree_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    sample_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    cohort_run_grant_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    metrics: tuple[InterventionalGreenMetricSummary, ...] = Field(
        min_length=1,
        max_length=8,
    )
    checks: tuple[InterventionalGreenCheckSummary, ...] = Field(
        min_length=1,
        max_length=80,
    )
    continuous_sample_indexes_verified: Literal[True] = True
    red_cohort_revalidated: Literal[True] = True
    profile_trust_revalidated: Literal[True] = True
    candidate_snapshot_revalidated: Literal[True] = True
    same_configuration_platform_verified: Literal[True] = True
    cohort_scoped_run_grant_used: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    metrics_executed: Literal[True] = True
    cohort_complete: Literal[True] = True
    completed_at: str

    @field_validator("sample_receipt_sha256", "sample_result_sha256")
    @classmethod
    def _sample_digests_are_sha256(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("Interventional GREEN sample 摘要格式无效。")
        return values

    @field_validator("cohort_run_grant_sha256")
    @classmethod
    def _run_grants_are_ordered_sha256(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(_SHA256_RE, value) is None for value in values
        ):
            raise ValueError("Interventional GREEN Run Grant 摘要无效或重复。")
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
            raise ValueError("Interventional GREEN cohort 样本汇总不完整。")
        if any(len(item.sample_values) != count for item in self.metrics):
            raise ValueError("Interventional GREEN metric 样本数量不完整。")
        if any(
            item.passed + item.failed + item.evaluation_errors != count
            for item in self.checks
        ):
            raise ValueError("Interventional GREEN check 状态数量不完整。")
        if tuple(item.metric_name for item in self.metrics) != tuple(
            sorted({item.metric_name for item in self.metrics})
        ):
            raise ValueError("Interventional GREEN metrics 必须排序且不得重复。")
        if tuple(item.check_id for item in self.checks) != tuple(
            sorted({item.check_id for item in self.checks})
        ):
            raise ValueError("Interventional GREEN checks 必须排序且不得重复。")
        parsed = datetime.fromisoformat(self.completed_at)
        if parsed.utcoffset() is None:
            raise ValueError("Interventional GREEN completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Interventional GREEN cohort receipt 摘要不一致。")
        if self.receipt_id != f"evvgreencohort_{expected[:24]}":
            raise ValueError("Interventional GREEN cohort receipt identity 不一致。")
        return self


class EvolutionInterventionalGreenCohortError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalGreenCohortExecutor:
    """Persist a continuous GREEN cohort under one revocable Run authority."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        sample_executor: EvolutionInterventionalGreenSampleExecutor,
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
        green_request: EvolutionInterventionalGreenCohortRequest,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionInterventionalGreenCohortReceipt:
        try:
            green = EvolutionInterventionalGreenCohortRequest.model_validate(
                green_request.model_dump(mode="json")
            )
            request = EvolutionBaselineCohortRequest.model_validate(
                baseline_request.model_dump(mode="json")
            )
            binding = EvolutionMetricRunnerBinding.model_validate(
                metric_binding.model_dump(mode="json")
            )
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            profile = EvolutionValidationProfileBinding.model_validate(
                profile_binding.model_dump(mode="json")
            )
            red = EvolutionInterventionalRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            candidate_lease = ExperimentWorktreeLease.model_validate(
                lease.model_dump(mode="json")
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionInterventionalGreenCohortError(
                "green_cohort_authority_invalid",
                "Interventional GREEN cohort authority 无效或已被篡改。",
            ) from exc
        expected_identity = None

        async def load_records():
            return await self._store.list_eval_results(
                self._workspace_root,
                green.batch_id,
                green.suite_id,
                limit=green.requested_samples + 1,
            )

        async def validate_existing(records):
            nonlocal expected_identity
            expected_identity, receipts = await self._sample_executor.validate_cohort_prefix(
                records=records,
                green_request=green,
                baseline_request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
                red_receipt=red,
                lease=candidate_lease,
            )
            return receipts

        def validate_evidence(records):
            if expected_identity is None:
                raise EvolutionInterventionalGreenCohortError(
                    "green_cohort_preflight_missing",
                    "Interventional GREEN cohort 缺少当前 preflight identity。",
                )
            _cohort_run_grant_digests(records, request, expected_identity)

        async def execute_sample(sample_index, authority):
            return await self._sample_executor.execute(
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
                green_request=green,
                baseline_request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
                red_receipt=red,
                lease=candidate_lease,
                run_authority=authority,
            )

        def build_receipt(records, receipts):
            if expected_identity is None:
                raise EvolutionInterventionalGreenCohortError(
                    "green_cohort_preflight_missing",
                    "Interventional GREEN cohort 缺少当前 preflight identity。",
                )
            return _build_cohort_receipt(
                green,
                red,
                request,
                binding,
                plan,
                profile,
                candidate_lease,
                expected_identity,
                records,
                receipts,
            )

        try:
            receipt = await self._cohort_kernel.execute(
                phase="green",
                authority_key=green.request_sha256,
                parent_receipt_id=parent_receipt_id,
                requested_samples=green.requested_samples,
                max_total_duration_seconds=green.max_total_duration_seconds,
                load_records=load_records,
                validate_existing_prefix=validate_existing,
                validate_run_evidence=validate_evidence,
                execute_sample=execute_sample,
                build_receipt=build_receipt,
            )
            final_identity = await self._sample_executor.revalidate_cohort_authority(
                green_request=green,
                baseline_request=request,
                metric_binding=binding,
                validation_plan=plan,
                profile_binding=profile,
                red_receipt=red,
                lease=candidate_lease,
            )
        except EvolutionInterventionalCohortKernelError as exc:
            raise EvolutionInterventionalGreenCohortError(exc.code, str(exc)) from exc
        except EvolutionInterventionalGreenSampleError as exc:
            raise EvolutionInterventionalGreenCohortError(exc.code, str(exc)) from exc
        if final_identity != expected_identity:
            raise EvolutionInterventionalGreenCohortError(
                "candidate_identity_changed_after_cohort",
                "Candidate identity 在 GREEN cohort 完成前发生变化。",
            )
        return receipt


def _cohort_run_grant_digests(records, request, expected_identity) -> tuple[str, ...]:
    check_cases = tuple(
        case
        for record in records
        for case in record.result.cases
        if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
    )
    digests = tuple(
        digest
        for case in check_cases
        if interventional_run_scope(case.message) == "cohort"
        and (digest := interventional_run_grant_digest(case.message)) is not None
    )
    identities_match = all(
        record.result.baseline_identity == expected_identity
        for record in records
    )
    expected_cases = len(records) * len(request.checks)
    if not (
        identities_match
        and len(check_cases) == len(digests) == expected_cases
        and (not records or digests)
    ):
        raise EvolutionInterventionalGreenCohortError(
            "green_cohort_run_evidence_incomplete",
            "Interventional GREEN samples 未完整绑定同一 Candidate 与 cohort Run Grant。",
        )
    return tuple(sorted(set(digests)))


def _build_cohort_receipt(
    green,
    red,
    request,
    binding,
    plan,
    profile,
    lease,
    identity,
    records,
    receipts,
):
    if len(records) != green.requested_samples or len(receipts) != len(records):
        raise EvolutionInterventionalGreenCohortError(
            "green_cohort_receipt_incomplete",
            "Interventional GREEN cohort completion evidence 不完整。",
        )
    run_grants = _cohort_run_grant_digests(records, request, identity)
    metrics: list[InterventionalGreenMetricSummary] = []
    for entry in sorted(binding.entries, key=lambda item: item.metric_name):
        observations = [
            observation
            for record in records
            for case in record.result.cases
            for observation in case.metric_observations
            if observation.metric == entry.metric_name
        ]
        if len(observations) != len(records):
            raise EvolutionInterventionalGreenCohortError(
                "green_cohort_metric_evidence_incomplete",
                f"Interventional GREEN metric {entry.metric_name} 样本不完整。",
            )
        metrics.append(InterventionalGreenMetricSummary(
            metric_name=entry.metric_name,
            unit=observations[0].unit,
            direction=entry.direction,
            target=entry.target,
            sample_values=tuple(item.value for item in observations),
        ))
    checks: list[InterventionalGreenCheckSummary] = []
    for expected in request.checks:
        statuses = Counter(
            case.status.value
            for record in records
            for case in record.result.cases
            if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
            and case.case_id == expected.check_id
        )
        checks.append(InterventionalGreenCheckSummary(
            check_id=expected.check_id,
            passed=statuses["passed"],
            failed=statuses["implementation_failure"],
            evaluation_errors=statuses["evaluation_error"],
        ))
    checks.sort(key=lambda item: item.check_id)
    payload = {
        "schema_version": 1,
        "policy_version": INTERVENTIONAL_GREEN_COHORT_POLICY,
        "green_request_id": green.request_id,
        "green_request_sha256": green.request_sha256,
        "red_receipt_id": red.receipt_id,
        "red_receipt_sha256": red.receipt_sha256,
        "metric_binding_id": binding.binding_id,
        "metric_binding_sha256": binding.binding_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": profile.binding_id,
        "profile_binding_sha256": profile.binding_sha256,
        "lease_id": lease.lease_id,
        "candidate_id": green.candidate_id,
        "candidate_revision": green.candidate_revision,
        "candidate_files_sha256": green.candidate_files_sha256,
        "candidate_identity_sha256": identity.identity_sha256,
        "candidate_tree_sha256": identity.source.tree_sha256,
        "suite_id": green.suite_id,
        "batch_id": green.batch_id,
        "requested_samples": green.requested_samples,
        "persisted_samples": len(records),
        "sample_seeds": list(green.sample_seeds),
        "sample_receipt_sha256": [item.receipt_sha256 for item in receipts],
        "sample_result_sha256": [item.result_sha256 for item in records],
        "cohort_run_grant_sha256": list(run_grants),
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "checks": [item.model_dump(mode="json") for item in checks],
        "continuous_sample_indexes_verified": True,
        "red_cohort_revalidated": True,
        "profile_trust_revalidated": True,
        "candidate_snapshot_revalidated": True,
        "same_configuration_platform_verified": True,
        "cohort_scoped_run_grant_used": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "cohort_complete": True,
        "completed_at": max(item.created_at for item in records),
    }
    digest = _sha256_payload(payload)
    return EvolutionInterventionalGreenCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evvgreencohort_{digest[:24]}",
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
    "EvolutionInterventionalGreenCohortError",
    "EvolutionInterventionalGreenCohortExecutor",
    "EvolutionInterventionalGreenCohortReceipt",
    "INTERVENTIONAL_GREEN_COHORT_POLICY",
    "InterventionalGreenCheckSummary",
    "InterventionalGreenMetricSummary",
]
