"""Continuous fresh RED/GREEN interventional cohorts over paired H5a samples."""

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

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.evolution.interventional_cohort_kernel import (
    EvolutionInterventionalCohortKernel,
    EvolutionInterventionalCohortKernelError,
)
from naumi_agent.evolution.revalidation_interventional_samples import (
    EvolutionRevalidationInterventionalSampleError,
    EvolutionRevalidationInterventionalSampleExecutor,
    EvolutionRevalidationInterventionalSampleStore,
    revalidation_interventional_batch_id,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.harness.eval_models import EvalCaseStatus
from naumi_agent.harness.sandbox_batch import HarnessSandboxBatchAdmission
from naumi_agent.harness.store import HarnessStore

EVOLUTION_REVALIDATION_INTERVENTIONAL_COHORT_POLICY = (
    "evolution-revalidation-interventional-cohort-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_PROFILE_RUNNER = "evolution_profile_check@1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationInterventionalMetricSummary(_StrictModel):
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    unit: Literal["count", "ratio", "milliseconds", "tokens", "usd", "scalar"]
    direction: Literal["decrease", "increase"]
    target: float
    red_values: tuple[float, ...] = Field(min_length=5, max_length=100)
    green_values: tuple[float, ...] = Field(min_length=5, max_length=100)

    @model_validator(mode="after")
    def _paired(self) -> Self:
        if len(self.red_values) != len(self.green_values):
            raise ValueError("Fresh cohort metric RED/GREEN 样本数不一致。")
        return self


class EvolutionRevalidationInterventionalCheckSummary(_StrictModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    red_passed: int = Field(ge=0, le=100)
    red_failed: int = Field(ge=0, le=100)
    red_evaluation_errors: int = Field(ge=0, le=100)
    green_passed: int = Field(ge=0, le=100)
    green_failed: int = Field(ge=0, le=100)
    green_evaluation_errors: int = Field(ge=0, le=100)


class EvolutionRevalidationInterventionalCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-interventional-cohort-v1"
    ] = EVOLUTION_REVALIDATION_INTERVENTIONAL_COHORT_POLICY
    receipt_id: str = Field(pattern=r"^evrevalcohort_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    red_batch_id: str = Field(min_length=1, max_length=128)
    green_batch_id: str = Field(min_length=1, max_length=128)
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_seeds: tuple[int, ...] = Field(min_length=5, max_length=100)
    sample_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    red_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    green_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    cohort_run_grant_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    platform_sha256: tuple[str, ...] = Field(min_length=1, max_length=1)
    metrics: tuple[EvolutionRevalidationInterventionalMetricSummary, ...] = Field(
        min_length=1,
        max_length=8,
    )
    checks: tuple[EvolutionRevalidationInterventionalCheckSummary, ...] = Field(
        min_length=1,
        max_length=80,
    )
    continuous_sample_indexes_verified: Literal[True] = True
    identical_environment_verified: Literal[True] = True
    profile_trust_revalidated: Literal[True] = True
    source_current_after_execution: Literal[True] = True
    cohort_scoped_run_grant_used: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    metrics_executed: Literal[True] = True
    cohort_complete: Literal[True] = True
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @field_validator("cohort_run_grant_sha256", "platform_sha256")
    @classmethod
    def _ordered_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(_SHA256_RE, item) is None for item in values
        ):
            raise ValueError("Fresh cohort digest 集合无效。")
        return values

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        count = self.requested_samples
        if not (
            self.persisted_samples
            == count
            == len(self.sample_seeds)
            == len(self.sample_receipt_sha256)
            == len(self.red_result_sha256)
            == len(self.green_result_sha256)
        ):
            raise ValueError("Fresh cohort 样本前缀不完整。")
        if any(
            len(item.red_values) != count or len(item.green_values) != count
            for item in self.metrics
        ):
            raise ValueError("Fresh cohort metric 样本不完整。")
        if any(
            item.red_passed + item.red_failed + item.red_evaluation_errors != count
            or item.green_passed + item.green_failed + item.green_evaluation_errors
            != count
            for item in self.checks
        ):
            raise ValueError("Fresh cohort check 样本不完整。")
        if datetime.fromisoformat(self.completed_at).utcoffset() is None:
            raise ValueError("Fresh cohort completed_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Fresh cohort receipt digest 不一致。")
        if self.receipt_id != f"evrevalcohort_{digest[:24]}":
            raise ValueError("Fresh cohort receipt identity 不一致。")
        return self


class EvolutionRevalidationInterventionalCohortError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationInterventionalCohortStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, item: EvolutionRevalidationInterventionalCohortReceipt):
        receipt = EvolutionRevalidationInterventionalCohortReceipt.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (
                await db.execute(
                    "SELECT contract_sha256 FROM "
                    "evolution_revalidation_runtime_contracts WHERE contract_id = ?",
                    (receipt.contract_id,),
                )
            ).fetchone()
            if dependency is None or dependency["contract_sha256"] != receipt.contract_sha256:
                await db.rollback()
                raise EvolutionRevalidationInterventionalCohortError(
                    "fresh_cohort_contract_mismatch",
                    "持久化 Runtime Contract 不存在或 digest 不一致。",
                )
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_interventional_cohorts "
                    "WHERE contract_id = ?",
                    (receipt.contract_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationInterventionalCohortReceipt.model_validate_json(
                    row["receipt_json"]
                )
                await db.rollback()
                if restored != receipt:
                    raise EvolutionRevalidationInterventionalCohortError(
                        "fresh_cohort_receipt_conflict",
                        "同一 Runtime Contract 已绑定不同 cohort receipt。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_interventional_cohorts "
                "(receipt_id, receipt_sha256, contract_id, receipt_json, completed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.receipt_sha256,
                    receipt.contract_id,
                    receipt.model_dump_json(),
                    receipt.completed_at,
                ),
            )
            await db.commit()
        return receipt

    async def get_by_contract(
        self,
        contract_id: str,
    ) -> EvolutionRevalidationInterventionalCohortReceipt | None:
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_interventional_cohorts "
                    "WHERE contract_id = ?",
                    (contract_id,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationInterventionalCohortReceipt.model_validate_json(
                row["receipt_json"]
            )
        )


class EvolutionRevalidationInterventionalCohortExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        sample_store: EvolutionRevalidationInterventionalSampleStore,
        receipt_store: EvolutionRevalidationInterventionalCohortStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        sample_executor: EvolutionRevalidationInterventionalSampleExecutor,
        contract_service: EvolutionRevalidationRuntimeContractService,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
        batch_admission: HarnessSandboxBatchAdmission | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.sample_store = sample_store
        self.receipt_store = receipt_store
        self.sample_executor = sample_executor
        self.contract_service = contract_service
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.kernel = EvolutionInterventionalCohortKernel(
            workspace_root=self.workspace_root,
            store=harness_store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            now=self.now,
            token=token,
            admission=batch_admission,
        )

    async def execute(self, *, contract_id: str, parent_receipt_id: str):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationInterventionalCohortError(
                "fresh_cohort_runtime_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract

        async def load_records():
            return await self._records(contract, "red")

        async def validate_prefix(records):
            receipts = []
            for record in records:
                receipts.append(await self.sample_executor.execute(
                    contract_id=contract.contract_id,
                    parent_receipt_id="existing-cohort-sample",
                    sample_index=record.sample_index,
                    run_scope="cohort",
                ))
            return receipts

        def validate_evidence(records):
            _validate_red_run_evidence(records, contract)

        async def execute_sample(index, authority):
            return await self.sample_executor.execute(
                contract_id=contract.contract_id,
                parent_receipt_id=parent_receipt_id,
                sample_index=index,
                run_scope="cohort",
                run_authority=authority,
            )

        try:
            await self.kernel.execute(
                phase="red",
                authority_key=contract.contract_sha256,
                parent_receipt_id=parent_receipt_id,
                requested_samples=contract.requested_samples,
                max_total_duration_seconds=contract.max_total_duration_seconds,
                load_records=load_records,
                validate_existing_prefix=validate_prefix,
                validate_run_evidence=validate_evidence,
                execute_sample=execute_sample,
                build_receipt=lambda _records, receipts: receipts[-1],
            )
        except (
            EvolutionInterventionalCohortKernelError,
            EvolutionRevalidationInterventionalSampleError,
        ) as exc:
            raise EvolutionRevalidationInterventionalCohortError(
                getattr(exc, "code", "fresh_cohort_execution_failed"),
                str(exc),
            ) from exc
        red = await self._records(contract, "red")
        green = await self._records(contract, "green")
        receipts = tuple([
            await self.sample_store.get_by_sample(
                contract.contract_id,
                index,
                "cohort",
            )
            for index in range(contract.requested_samples)
        ])
        if any(item is None or item.run_scope != "cohort" for item in receipts):
            raise EvolutionRevalidationInterventionalCohortError(
                "fresh_cohort_sample_receipts_incomplete",
                "Fresh cohort sample receipts 不完整。",
            )
        built = _build_receipt(contract, red, green, receipts)
        return await self.receipt_store.record(built)

    async def _records(self, contract, phase):
        return await self.harness_store.list_eval_results(
            self.workspace_root,
            revalidation_interventional_batch_id(contract, phase, "cohort"),
            contract.suite_id,
            limit=contract.requested_samples + 1,
        )


def _build_receipt(contract, red, green, receipts):
    count = contract.requested_samples
    indexes = tuple(item.sample_index for item in red)
    if not (
        len(red) == len(green) == len(receipts) == count
        and indexes == tuple(range(count))
        and tuple(item.sample_index for item in green) == indexes
    ):
        raise EvolutionRevalidationInterventionalCohortError(
            "fresh_cohort_prefix_incomplete",
            "Fresh cohort RED/GREEN 前缀不连续。",
        )
    typed_receipts = tuple(receipts)
    metrics = []
    for entry in sorted(contract.metric_entries, key=lambda item: item.metric_name):
        red_values = _metric_values(red, entry.metric_name)
        green_values = _metric_values(green, entry.metric_name)
        if len(red_values) != count or len(green_values) != count:
            raise EvolutionRevalidationInterventionalCohortError(
                "fresh_cohort_metric_incomplete",
                f"Fresh cohort metric {entry.metric_name} 不完整。",
            )
        unit = next(
            observation.unit
            for case in red[0].result.cases
            for observation in case.metric_observations
            if observation.metric == entry.metric_name
        )
        metrics.append(EvolutionRevalidationInterventionalMetricSummary(
            metric_name=entry.metric_name,
            unit=unit,
            direction=entry.direction,
            target=entry.target,
            red_values=red_values,
            green_values=green_values,
        ))
    checks = []
    for check_id in sorted(typed_receipts[0].check_ids):
        red_statuses = _check_statuses(red, check_id)
        green_statuses = _check_statuses(green, check_id)
        checks.append(EvolutionRevalidationInterventionalCheckSummary(
            check_id=check_id,
            red_passed=red_statuses[EvalCaseStatus.PASSED.value],
            red_failed=red_statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE.value],
            red_evaluation_errors=red_statuses[EvalCaseStatus.EVALUATION_ERROR.value],
            green_passed=green_statuses[EvalCaseStatus.PASSED.value],
            green_failed=green_statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE.value],
            green_evaluation_errors=green_statuses[EvalCaseStatus.EVALUATION_ERROR.value],
        ))
    grants = tuple(sorted({
        digest
        for receipt in typed_receipts
        for digest in (receipt.red_run_grant_sha256, receipt.green_run_grant_sha256)
    }))
    if any(
        receipt.red_run_grant_sha256 != receipt.green_run_grant_sha256
        for receipt in typed_receipts
    ):
        raise EvolutionRevalidationInterventionalCohortError(
            "fresh_cohort_phase_grant_mismatch",
            "Fresh cohort 单 sample 的 RED/GREEN 未共享 cohort Run Grant。",
        )
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_INTERVENTIONAL_COHORT_POLICY,
        "workspace_root": contract.workspace_root,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "validation_plan_id": contract.validation_plan_id,
        "validation_plan_sha256": contract.validation_plan_sha256,
        "source_snapshot_id": contract.source_snapshot_id,
        "source_snapshot_sha256": contract.source_snapshot_sha256,
        "suite_id": contract.suite_id,
        "red_batch_id": red[0].batch_id,
        "green_batch_id": green[0].batch_id,
        "requested_samples": count,
        "persisted_samples": count,
        "sample_seeds": [item.sample_seed for item in typed_receipts],
        "sample_receipt_sha256": [item.receipt_sha256 for item in typed_receipts],
        "red_result_sha256": [item.result_sha256 for item in red],
        "green_result_sha256": [item.result_sha256 for item in green],
        "cohort_run_grant_sha256": list(grants),
        "platform_sha256": sorted({item.platform_sha256 for item in typed_receipts}),
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "checks": [item.model_dump(mode="json") for item in checks],
        "continuous_sample_indexes_verified": True,
        "identical_environment_verified": True,
        "profile_trust_revalidated": True,
        "source_current_after_execution": True,
        "cohort_scoped_run_grant_used": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "cohort_complete": True,
        "comparison_authority": False,
        "promotion_authority": False,
        "completed_at": max(item.completed_at for item in typed_receipts),
    }
    digest = _sha256_payload(payload)
    return EvolutionRevalidationInterventionalCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evrevalcohort_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _metric_values(records, metric_name):
    return tuple(
        observation.value
        for record in records
        for case in record.result.cases
        for observation in case.metric_observations
        if observation.metric == metric_name
    )


def _check_statuses(records, check_id):
    return Counter(
        case.status.value
        for record in records
        for case in record.result.cases
        if case.runner == _PROFILE_RUNNER and case.case_id == check_id
    )


def _validate_red_run_evidence(records, contract):
    if len(records) > contract.requested_samples:
        raise EvolutionRevalidationInterventionalCohortError(
            "fresh_cohort_run_evidence_overflow",
            "Fresh cohort RED H5a 超出 Runtime Contract 样本预算。",
        )
    expected_check_ids: tuple[str, ...] | None = None
    for record in records:
        cases = tuple(item for item in record.result.cases if item.runner == _PROFILE_RUNNER)
        check_ids = tuple(sorted(item.case_id for item in cases))
        grants = {
            match.group(1)
            for item in cases
            if "run_scope=cohort" in item.message
            and (match := re.search(r"run_grant_sha256=([0-9a-f]{64})(?:\s|$)", item.message))
        }
        if expected_check_ids is None:
            expected_check_ids = check_ids
        if (
            not cases
            or len(grants) != 1
            or check_ids != expected_check_ids
            or len(set(check_ids)) != len(check_ids)
        ):
            raise EvolutionRevalidationInterventionalCohortError(
                "fresh_cohort_run_evidence_incomplete",
                "Fresh cohort RED H5a 缺少 cohort-scoped Run Grant evidence。",
            )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_interventional_cohorts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, "
        "completed_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_INTERVENTIONAL_COHORT_POLICY",
    "EvolutionRevalidationInterventionalCheckSummary",
    "EvolutionRevalidationInterventionalCohortError",
    "EvolutionRevalidationInterventionalCohortExecutor",
    "EvolutionRevalidationInterventionalCohortReceipt",
    "EvolutionRevalidationInterventionalCohortStore",
    "EvolutionRevalidationInterventionalMetricSummary",
]
