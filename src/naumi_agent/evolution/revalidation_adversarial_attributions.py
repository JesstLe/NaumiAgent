"""Failure attribution for every completed Fresh Adversarial platform lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionError,
    EvolutionFailureAttributionKernel,
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
    FailureAttributionCategory,
)
from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCohortStore,
)
from naumi_agent.evolution.revalidation_adversarial_comparisons import (
    EvolutionRevalidationAdversarialComparisonExecutor,
)
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixService,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError


class EvolutionRevalidationAdversarialAttributionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationAdversarialAttributionKernel:
    """Preserve generic failures while treating unchanged guardrails as success."""

    def __init__(self, kernel: EvolutionFailureAttributionKernel | None = None) -> None:
        self.kernel = kernel or EvolutionFailureAttributionKernel()

    def build(self, *, authority, comparison):
        receipt = self.kernel.build(authority=authority, comparison=comparison)
        if not (
            receipt.category is FailureAttributionCategory.OBJECTIVE_NOT_IMPROVED
            and comparison.receipt.decision == "passed"
            and comparison.receipt.statistical_verdict == "unchanged"
        ):
            return receipt
        payload = receipt.model_dump(
            mode="json", exclude={"attribution_id", "attribution_sha256"}
        )
        payload.update({
            "category": "none",
            "reason_code": "adversarial_guardrail_preserved",
            "action": "continue_to_reflection",
            "candidate_fault": False,
            "retryable": False,
            "requires_rerun": False,
            "reflection_eligible": True,
        })
        digest = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        return EvolutionFailureAttributionReceipt.model_validate({
            **payload,
            "attribution_id": f"evattr_{digest[:24]}",
            "attribution_sha256": digest,
        })


class EvolutionRevalidationAdversarialAttributionExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        cohort_store: EvolutionRevalidationAdversarialCohortStore,
        matrix_service: EvolutionRevalidationAdversarialMatrixService,
        comparison_executor: EvolutionRevalidationAdversarialComparisonExecutor,
        contract_service: EvolutionRevalidationRuntimeContractService,
        attribution_store: EvolutionFailureAttributionStore,
        kernel: EvolutionRevalidationAdversarialAttributionKernel | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.cohort_store = cohort_store
        self.matrix_service = matrix_service
        self.comparison_executor = comparison_executor
        self.contract_service = contract_service
        self.attribution_store = attribution_store
        self.kernel = kernel or EvolutionRevalidationAdversarialAttributionKernel()

    async def execute(
        self, *, contract_id: str
    ) -> tuple[EvolutionFailureAttributionReceipt, ...]:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationAdversarialAttributionError(
                "fresh_adversarial_attribution_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        matrix = await self.matrix_service.inspect(contract_id=contract_id)
        if not matrix.matrix_complete:
            raise EvolutionRevalidationAdversarialAttributionError(
                "fresh_adversarial_attribution_matrix_incomplete",
                "Fresh Adversarial required-platform matrix 尚未完成。",
            )
        comparisons = await self.comparison_executor.execute(contract_id=contract_id)
        if len(comparisons) != len(matrix.lanes):
            raise EvolutionRevalidationAdversarialAttributionError(
                "fresh_adversarial_attribution_comparison_incomplete",
                "Fresh Adversarial platform comparison 数量不完整。",
            )
        contract = view.contract
        receipts = []
        for lane, comparison in zip(matrix.lanes, comparisons, strict=True):
            cohort = await self.cohort_store.get(contract_id, lane.platform)
            if cohort is None or not (
                lane.cohort_receipt_id == cohort.receipt_id
                and lane.cohort_receipt_sha256 == cohort.receipt_sha256
                and cohort.contract_sha256 == contract.contract_sha256
                and cohort.validation_plan_id == contract.validation_plan_id
                and cohort.validation_plan_sha256 == contract.validation_plan_sha256
                and cohort.platform == lane.platform
                and comparison.receipt.baseline_batch_id == cohort.red_batch_id
                and comparison.receipt.current_batch_id == cohort.green_batch_id
            ):
                raise EvolutionRevalidationAdversarialAttributionError(
                    "fresh_adversarial_attribution_authority_mismatch",
                    f"平台 {lane.platform} 的 matrix、cohort 与 H5c authority 不一致。",
                )
            try:
                authoritative = await self.harness_store.get_eval_comparison_receipt(
                    comparison.workspace_root,
                    comparison.suite_id,
                    comparison.baseline_id,
                    comparison.current_batch_id,
                )
            except (HarnessStoreError, ValueError) as exc:
                raise EvolutionRevalidationAdversarialAttributionError(
                    "fresh_adversarial_attribution_comparison_unavailable",
                    f"平台 {lane.platform} 的 H5c authority 无法读取。",
                ) from exc
            if authoritative is None or authoritative != comparison:
                raise EvolutionRevalidationAdversarialAttributionError(
                    "fresh_adversarial_attribution_comparison_not_authoritative",
                    f"平台 {lane.platform} 的 H5c 不是 Harness Store 当前事实。",
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
                receipts.append(await self.attribution_store.record(receipt))
            except EvolutionFailureAttributionError as exc:
                raise EvolutionRevalidationAdversarialAttributionError(
                    exc.code, str(exc)
                ) from exc
        return tuple(receipts)


__all__ = [
    "EvolutionRevalidationAdversarialAttributionError",
    "EvolutionRevalidationAdversarialAttributionExecutor",
    "EvolutionRevalidationAdversarialAttributionKernel",
]
