"""Mechanical failure attribution for Fresh Interventional H5c authority."""

from __future__ import annotations

from pathlib import Path

from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionError,
    EvolutionFailureAttributionKernel,
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
)
from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_interventional_comparisons import (
    EvolutionRevalidationInterventionalComparisonExecutor,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError


class EvolutionRevalidationInterventionalAttributionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationInterventionalAttributionExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        cohort_store: EvolutionRevalidationInterventionalCohortStore,
        comparison_executor: EvolutionRevalidationInterventionalComparisonExecutor,
        contract_service: EvolutionRevalidationRuntimeContractService,
        attribution_store: EvolutionFailureAttributionStore,
        kernel: EvolutionFailureAttributionKernel | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.cohort_store = cohort_store
        self.comparison_executor = comparison_executor
        self.contract_service = contract_service
        self.attribution_store = attribution_store
        self.kernel = kernel or EvolutionFailureAttributionKernel()

    async def execute(self, *, contract_id: str) -> EvolutionFailureAttributionReceipt:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract
        cohort = await self.cohort_store.get_by_contract(contract_id)
        if cohort is None:
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_cohort_missing",
                "Fresh Interventional cohort 尚未完成。",
            )
        if not (
            cohort.contract_sha256 == contract.contract_sha256
            and cohort.validation_plan_id == contract.validation_plan_id
            and cohort.validation_plan_sha256 == contract.validation_plan_sha256
            and cohort.source_snapshot_id == contract.source_snapshot_id
            and cohort.source_snapshot_sha256 == contract.source_snapshot_sha256
            and cohort.suite_id == contract.suite_id
            and cohort.requested_samples == cohort.persisted_samples
            == contract.requested_samples
        ):
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_authority_mismatch",
                "Fresh Contract 与 Interventional cohort authority 不一致。",
            )
        comparison = await self.comparison_executor.execute(contract_id=contract_id)
        if not (
            comparison.receipt.baseline_batch_id == cohort.red_batch_id
            and comparison.receipt.current_batch_id == cohort.green_batch_id
        ):
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_comparison_mismatch",
                "Fresh Interventional H5c 与 cohort batch 不一致。",
            )
        try:
            authoritative = await self.harness_store.get_eval_comparison_receipt(
                comparison.workspace_root,
                comparison.suite_id,
                comparison.baseline_id,
                comparison.current_batch_id,
            )
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_comparison_unavailable",
                "Fresh Interventional H5c authority 无法读取。",
            ) from exc
        if authoritative is None or authoritative != comparison:
            raise EvolutionRevalidationInterventionalAttributionError(
                "fresh_interventional_attribution_comparison_not_authoritative",
                "Fresh Interventional H5c 不是 Harness Store 当前事实。",
            )
        try:
            receipt = self.kernel.build(
                authority=EvolutionFailureAttributionAuthority(
                    validation_plan_id=contract.validation_plan_id,
                    validation_plan_sha256=contract.validation_plan_sha256,
                    red_receipt_id=cohort.receipt_id,
                    red_receipt_sha256=cohort.receipt_sha256,
                    green_receipt_id=cohort.receipt_id,
                    green_receipt_sha256=cohort.receipt_sha256,
                    candidate_id=contract.candidate_id,
                    candidate_revision=contract.candidate_revision,
                    suite_id=contract.suite_id,
                    red_batch_id=cohort.red_batch_id,
                    green_batch_id=cohort.green_batch_id,
                    red_samples=cohort.persisted_samples,
                    green_samples=cohort.persisted_samples,
                    red_result_sha256=cohort.red_result_sha256,
                    green_result_sha256=cohort.green_result_sha256,
                ),
                comparison=authoritative,
            )
            return await self.attribution_store.record(receipt)
        except EvolutionFailureAttributionError as exc:
            raise EvolutionRevalidationInterventionalAttributionError(
                exc.code, str(exc)
            ) from exc


__all__ = [
    "EvolutionRevalidationInterventionalAttributionError",
    "EvolutionRevalidationInterventionalAttributionExecutor",
]
