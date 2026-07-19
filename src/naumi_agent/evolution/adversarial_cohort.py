"""Continuous adversarial lane cohorts governed by HAR-08.4f."""

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
from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    EvolutionAdversarialProbeContract,
)
from naumi_agent.evolution.adversarial_samples import (
    ADVERSARIAL_SAMPLE_RUNNER,
    EvolutionAdversarialSampleError,
    EvolutionAdversarialSampleExecutor,
    adversarial_lane_authority_key,
)
from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.interventional_cohort_kernel import (
    EvolutionInterventionalCohortKernel,
    EvolutionInterventionalCohortKernelError,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_models import EvalCaseStatus
from naumi_agent.harness.sandbox_batch import BatchProgressCallback
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

ADVERSARIAL_COHORT_POLICY = "evolution-adversarial-cohort-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class AdversarialCohortCheckSummary(_StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    metric_name: str = Field(pattern=r"^adversarial\.[a-z][a-z0-9_-]{0,63}\.exit_zero$")
    passed: int = Field(ge=0, le=100)
    failed: int = Field(ge=0, le=100)
    evaluation_errors: int = Field(ge=0, le=100)
    sample_values: tuple[float | None, ...] = Field(min_length=5, max_length=100)


class EvolutionAdversarialCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-adversarial-cohort-v1"] = (
        ADVERSARIAL_COHORT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evadvcohort_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evadvreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    probe_contract_id: str = Field(pattern=r"^evapc_[0-9a-f]{24}$")
    probe_contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    lane_order: int = Field(ge=1, le=6)
    platform: Literal["linux", "macos", "windows"]
    phase: Literal["red", "green"]
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    authority_key: str = Field(pattern=_SHA256_RE)
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    sample_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    run_grant_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    baseline_identity_sha256: str = Field(pattern=_SHA256_RE)
    source_tree_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    overlay_source_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    checks: tuple[AdversarialCohortCheckSummary, ...] = Field(
        min_length=1,
        max_length=80,
    )
    continuous_sample_indexes_verified: Literal[True] = True
    harness_batch_coordinator_used: Literal[True] = True
    cohort_scoped_run_grant_used: Literal[True] = True
    profile_trust_revalidated: Literal[True] = True
    lease_revalidated: Literal[True] = True
    platform_revalidated: Literal[True] = True
    source_revalidated: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    cohort_complete: Literal[True] = True
    completed_at: str

    @field_validator(
        "sample_receipt_sha256",
        "sample_result_sha256",
        "run_grant_sha256",
    )
    @classmethod
    def _valid_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("Adversarial cohort digest 格式无效。")
        return values

    @model_validator(mode="after")
    def _complete_and_tamper_evident(self) -> Self:
        count = self.requested_samples
        if not (
            self.persisted_samples
            == count
            == len(self.sample_seeds)
            == len(self.sample_receipt_sha256)
            == len(self.sample_result_sha256)
        ):
            raise ValueError("Adversarial cohort 样本前缀不完整。")
        if self.run_grant_sha256 != tuple(sorted(set(self.run_grant_sha256))):
            raise ValueError("Adversarial cohort Run Grant digest 必须排序且不得重复。")
        if len(set(self.sample_seeds)) != count:
            raise ValueError("Adversarial cohort sample seeds 必须唯一。")
        expected_authority_key = hashlib.sha256(
            f"{self.request_sha256}:{self.lane_order}:{self.batch_id}".encode("ascii")
        ).hexdigest()
        if not hmac.compare_digest(self.authority_key, expected_authority_key):
            raise ValueError("Adversarial cohort authority key 不一致。")
        if tuple(item.check_id for item in self.checks) != tuple(
            sorted({item.check_id for item in self.checks})
        ):
            raise ValueError("Adversarial cohort checks 必须排序且不得重复。")
        if any(
            len(item.sample_values) != count
            or item.passed + item.failed + item.evaluation_errors != count
            or item.metric_name != f"adversarial.{item.check_id}.exit_zero"
            for item in self.checks
        ):
            raise ValueError("Adversarial cohort check 样本汇总不完整。")
        if (self.phase == "red") != (self.overlay_source_sha256 is None):
            raise ValueError("Adversarial cohort phase 与 overlay 不一致。")
        completed = datetime.fromisoformat(self.completed_at)
        if completed.utcoffset() is None:
            raise ValueError("Adversarial cohort completed_at 必须包含时区。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Adversarial cohort receipt digest 不一致。")
        if self.receipt_id != f"evadvcohort_{expected[:24]}":
            raise ValueError("Adversarial cohort receipt identity 不一致。")
        return self


class EvolutionAdversarialCohortError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionAdversarialCohortExecutor:
    """Adapt one exact adversarial lane to the shared Harness Batch coordinator."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        sample_executor: EvolutionAdversarialSampleExecutor,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not isinstance(sample_executor, EvolutionAdversarialSampleExecutor):
            raise TypeError("Adversarial Cohort 需要 Sample Executor。")
        if (
            sample_executor._workspace_root != self._workspace_root  # noqa: SLF001
            or sample_executor._store is not store  # noqa: SLF001
        ):
            raise ValueError("Adversarial Cohort Sample Executor composition 不一致。")
        self._store = store
        self._sample_executor = sample_executor
        self._coordinator = EvolutionInterventionalCohortKernel(
            workspace_root=self._workspace_root,
            store=store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            now=now or (lambda: datetime.now(UTC).isoformat()),
            token=token,
        )

    async def execute(
        self,
        *,
        parent_receipt_id: str,
        lane_order: int,
        batch_request: EvolutionAdversarialBatchRequest,
        probe_contract: EvolutionAdversarialProbeContract,
        validation_plan: EvolutionValidationPlan,
        lease: ExperimentWorktreeLease,
        on_progress: BatchProgressCallback | None = None,
    ) -> EvolutionAdversarialCohortReceipt:
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
            raise EvolutionAdversarialCohortError(
                "adversarial_cohort_authority_invalid",
                "Adversarial cohort authority 无效或已被篡改。",
            ) from exc
        if isinstance(lane_order, bool) or not 1 <= lane_order <= len(request.lanes):
            raise EvolutionAdversarialCohortError(
                "adversarial_cohort_lane_invalid",
                "Adversarial cohort lane_order 超出请求范围。",
            )
        lane = request.lanes[lane_order - 1]
        try:
            await self._sample_executor.preflight(
                lane_order=lane_order,
                batch_request=request,
                probe_contract=probes,
                validation_plan=plan,
                lease=candidate_lease,
            )

            async def load_records() -> tuple[HarnessStoredEvalResult, ...]:
                return await self._store.list_eval_results(
                    self._workspace_root,
                    lane.batch_id,
                    request.suite_id,
                    limit=request.requested_samples + 1,
                )

            async def validate_existing(records):
                return [
                    await self._sample_executor.validate_existing(
                        lane_order=lane_order,
                        sample_index=record.sample_index,
                        batch_request=request,
                        probe_contract=probes,
                        validation_plan=plan,
                        lease=candidate_lease,
                    )
                    for record in records
                ]

            def validate_run_evidence(records):
                _validate_run_evidence(records, request)

            async def execute_sample(sample_index, authority):
                return await self._sample_executor.execute(
                    parent_receipt_id=parent_receipt_id,
                    lane_order=lane_order,
                    sample_index=sample_index,
                    batch_request=request,
                    probe_contract=probes,
                    validation_plan=plan,
                    lease=candidate_lease,
                    run_authority=authority,
                )

            def build_receipt(records, receipts):
                return _build_receipt(
                    request,
                    probes,
                    plan,
                    candidate_lease,
                    lane_order,
                    records,
                    receipts,
                )

            receipt = await self._coordinator.execute(
                phase="adversarial",
                authority_key=adversarial_lane_authority_key(request, lane_order),
                parent_receipt_id=parent_receipt_id,
                requested_samples=request.requested_samples,
                max_total_duration_seconds=lane.max_duration_seconds,
                load_records=load_records,
                validate_existing_prefix=validate_existing,
                validate_run_evidence=validate_run_evidence,
                execute_sample=execute_sample,
                build_receipt=build_receipt,
                on_progress=on_progress,
            )
            final_records = await load_records()
            final_receipts = await validate_existing(final_records)
            final = build_receipt(final_records, final_receipts)
        except EvolutionInterventionalCohortKernelError as exc:
            raise EvolutionAdversarialCohortError(exc.code, str(exc)) from exc
        except EvolutionAdversarialSampleError as exc:
            raise EvolutionAdversarialCohortError(exc.code, str(exc)) from exc
        if final != receipt:
            raise EvolutionAdversarialCohortError(
                "adversarial_cohort_final_revalidation_mismatch",
                "Adversarial cohort 完成后 authority 复验不一致。",
            )
        return receipt


def _validate_run_evidence(records, request) -> tuple[str, ...]:
    grants: set[str] = set()
    expected_ids = tuple(item.check_id for item in request.checks)
    for record in records:
        cases = record.result.cases
        sample_grants = {_message_digest(item.message, "run_grant") for item in cases}
        valid = bool(
            tuple(item.case_id for item in cases) == expected_ids
            and all(item.runner == ADVERSARIAL_SAMPLE_RUNNER for item in cases)
            and all(_message_digest(item.message, "lifecycle") for item in cases)
            and len(sample_grants) == 1
            and None not in sample_grants
        )
        if not valid:
            raise EvolutionAdversarialCohortError(
                "adversarial_cohort_run_evidence_incomplete",
                "Adversarial cohort sample 缺少完整 lifecycle/Run Grant evidence。",
            )
        grants.update(item for item in sample_grants if item is not None)
    return tuple(sorted(grants))


def _build_receipt(request, probes, plan, lease, lane_order, records, receipts):
    lane = request.lanes[lane_order - 1]
    if len(records) != request.requested_samples or len(receipts) != len(records):
        raise EvolutionAdversarialCohortError(
            "adversarial_cohort_receipt_incomplete",
            "Adversarial cohort completion evidence 不完整。",
        )
    if any(
        receipt.sample_index != index
        or receipt.result_sha256 != records[index].result_sha256
        or receipt.lane_order != lane_order
        for index, receipt in enumerate(receipts)
    ):
        raise EvolutionAdversarialCohortError(
            "adversarial_cohort_receipt_mismatch",
            "Adversarial cohort sample receipt 与 H5a 不一致。",
        )
    identities = {item.baseline_identity_sha256 for item in receipts}
    sources = {item.source_tree_sha256 for item in receipts}
    overlays = {item.overlay_source_sha256 for item in receipts}
    if len(identities) != 1 or len(sources) != 1 or len(overlays) != 1:
        raise EvolutionAdversarialCohortError(
            "adversarial_cohort_identity_mismatch",
            "Adversarial cohort samples 未绑定同一 source identity。",
        )
    summaries: list[AdversarialCohortCheckSummary] = []
    for expected in request.checks:
        cases = [
            next(item for item in record.result.cases if item.case_id == expected.check_id)
            for record in records
        ]
        statuses = Counter(item.status for item in cases)
        values = tuple(
            None if item.status is EvalCaseStatus.EVALUATION_ERROR
            else item.metric_observations[0].value
            for item in cases
        )
        summaries.append(AdversarialCohortCheckSummary(
            check_id=expected.check_id,
            metric_name=f"adversarial.{expected.check_id}.exit_zero",
            passed=statuses[EvalCaseStatus.PASSED],
            failed=statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE],
            evaluation_errors=statuses[EvalCaseStatus.EVALUATION_ERROR],
            sample_values=values,
        ))
    payload = {
        "schema_version": 1,
        "policy_version": ADVERSARIAL_COHORT_POLICY,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "probe_contract_id": probes.probe_contract_id,
        "probe_contract_sha256": probes.probe_contract_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "lease_id": lease.lease_id,
        "candidate_id": request.candidate_id,
        "candidate_revision": request.candidate_revision,
        "candidate_files_sha256": request.candidate_files_sha256,
        "lane_order": lane.order,
        "platform": lane.platform,
        "phase": lane.phase,
        "batch_id": lane.batch_id,
        "suite_id": request.suite_id,
        "authority_key": adversarial_lane_authority_key(request, lane_order),
        "requested_samples": request.requested_samples,
        "persisted_samples": len(records),
        "sample_seeds": list(request.sample_seeds),
        "sample_receipt_sha256": [item.receipt_sha256 for item in receipts],
        "sample_result_sha256": [item.result_sha256 for item in records],
        "run_grant_sha256": list(_validate_run_evidence(records, request)),
        "baseline_identity_sha256": next(iter(identities)),
        "source_tree_sha256": next(iter(sources)),
        "overlay_source_sha256": next(iter(overlays)),
        "checks": [item.model_dump(mode="json") for item in summaries],
        "continuous_sample_indexes_verified": True,
        "harness_batch_coordinator_used": True,
        "cohort_scoped_run_grant_used": True,
        "profile_trust_revalidated": True,
        "lease_revalidated": True,
        "platform_revalidated": True,
        "source_revalidated": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "cohort_complete": True,
        "completed_at": max(item.created_at for item in records),
    }
    digest = _sha256_payload(payload)
    return EvolutionAdversarialCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evadvcohort_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _message_digest(message: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^| ){name}_sha256=([0-9a-f]{{64}})(?:$| )",
        message,
    )
    return match.group(1) if match is not None else None


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "ADVERSARIAL_COHORT_POLICY",
    "AdversarialCohortCheckSummary",
    "EvolutionAdversarialCohortError",
    "EvolutionAdversarialCohortExecutor",
    "EvolutionAdversarialCohortReceipt",
]
