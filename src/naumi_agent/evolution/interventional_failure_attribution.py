"""Failure attribution adapter for authority-bound interventional cohorts."""

from __future__ import annotations

from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionError,
    EvolutionFailureAttributionKernel,
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
)
from naumi_agent.evolution.interventional_green_cohort import (
    EvolutionInterventionalGreenCohortReceipt,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortReceipt,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoreError,
)


class EvolutionInterventionalFailureAttributionBuilder:
    """Validate interventional completion receipts before shared classification."""

    def __init__(self, kernel: EvolutionFailureAttributionKernel | None = None) -> None:
        self._kernel = kernel or EvolutionFailureAttributionKernel()

    def build(
        self,
        *,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        green_receipt: EvolutionInterventionalGreenCohortReceipt,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            red = EvolutionInterventionalRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionInterventionalGreenCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "interventional_attribution_authority_invalid",
                "Interventional Failure Attribution authority 无效或已被篡改。",
            ) from exc
        if not (
            red.validation_plan_id == plan.validation_plan_id
            and red.validation_plan_sha256 == plan.validation_plan_sha256
            and green.validation_plan_id == plan.validation_plan_id
            and green.validation_plan_sha256 == plan.validation_plan_sha256
            and green.red_receipt_id == red.receipt_id
            and green.red_receipt_sha256 == red.receipt_sha256
            and green.candidate_id == plan.candidate_id
            and green.candidate_revision == plan.candidate_revision
            and red.suite_id == green.suite_id
        ):
            raise EvolutionFailureAttributionError(
                "interventional_attribution_authority_mismatch",
                "Interventional Plan、RED 与 GREEN completion authority 不一致。",
            )
        authority = EvolutionFailureAttributionAuthority(
            validation_plan_id=plan.validation_plan_id,
            validation_plan_sha256=plan.validation_plan_sha256,
            red_receipt_id=red.receipt_id,
            red_receipt_sha256=red.receipt_sha256,
            green_receipt_id=green.receipt_id,
            green_receipt_sha256=green.receipt_sha256,
            candidate_id=plan.candidate_id,
            candidate_revision=plan.candidate_revision,
            suite_id=red.suite_id,
            red_batch_id=red.batch_id,
            green_batch_id=green.batch_id,
            red_samples=red.persisted_samples,
            green_samples=green.persisted_samples,
            red_result_sha256=red.sample_result_sha256,
            green_result_sha256=green.sample_result_sha256,
        )
        return self._kernel.build(authority=authority, comparison=comparison)


class EvolutionInterventionalFailureAttributionExecutor:
    """Reload H5c authority, classify it mechanically, and persist one fact."""

    def __init__(
        self,
        *,
        harness_store: HarnessStore,
        attribution_store: EvolutionFailureAttributionStore,
        builder: EvolutionInterventionalFailureAttributionBuilder | None = None,
    ) -> None:
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Interventional Attribution executor 需要 HarnessStore。")
        if not isinstance(attribution_store, EvolutionFailureAttributionStore):
            raise TypeError(
                "Interventional Attribution executor 需要 Attribution Store。"
            )
        self._harness_store = harness_store
        self._attribution_store = attribution_store
        self._builder = builder or EvolutionInterventionalFailureAttributionBuilder()

    async def execute(
        self,
        *,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        green_receipt: EvolutionInterventionalGreenCohortReceipt,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            authoritative = await self._harness_store.get_eval_comparison_receipt(
                comparison.workspace_root,
                comparison.suite_id,
                comparison.baseline_id,
                comparison.current_batch_id,
            )
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "interventional_attribution_comparison_read_failed",
                "无法从 Harness Store 读取 Interventional H5c authority。",
            ) from exc
        if authoritative is None or authoritative != comparison:
            raise EvolutionFailureAttributionError(
                "interventional_attribution_comparison_not_authoritative",
                "传入的 Interventional H5c 不是 Harness Store 当前不可变事实。",
            )
        receipt = self._builder.build(
            validation_plan=validation_plan,
            red_receipt=red_receipt,
            green_receipt=green_receipt,
            comparison=authoritative,
        )
        return await self._attribution_store.record(receipt)


__all__ = [
    "EvolutionInterventionalFailureAttributionBuilder",
    "EvolutionInterventionalFailureAttributionExecutor",
]
