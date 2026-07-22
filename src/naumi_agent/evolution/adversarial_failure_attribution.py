"""Failure attribution adapter for authority-bound adversarial cohorts."""

from __future__ import annotations

from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.adversarial_cohort import (
    EvolutionAdversarialCohortReceipt,
)
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionError,
    EvolutionFailureAttributionKernel,
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoreError,
)


class EvolutionAdversarialFailureAttributionBuilder:
    """Resolve one exact same-platform adversarial pair for shared classification."""

    def __init__(self, kernel: EvolutionFailureAttributionKernel | None = None) -> None:
        self._kernel = kernel or EvolutionFailureAttributionKernel()

    def build(
        self,
        *,
        batch_request: EvolutionAdversarialBatchRequest,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionAdversarialCohortReceipt,
        green_receipt: EvolutionAdversarialCohortReceipt,
        comparison: HarnessStoredEvalComparisonReceipt,
    ) -> EvolutionFailureAttributionReceipt:
        try:
            request = EvolutionAdversarialBatchRequest.model_validate(
                batch_request.model_dump(mode="json")
            )
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            red = EvolutionAdversarialCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionAdversarialCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
            red_lane = request.lanes[red.lane_order - 1]
            green_lane = request.lanes[green.lane_order - 1]
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise EvolutionFailureAttributionError(
                "adversarial_attribution_authority_invalid",
                "Adversarial Failure Attribution authority 无效或已被篡改。",
            ) from exc
        receipt_fields = (
            "request_id",
            "request_sha256",
            "probe_contract_id",
            "probe_contract_sha256",
            "validation_plan_id",
            "validation_plan_sha256",
            "lease_id",
            "candidate_id",
            "candidate_revision",
            "candidate_files_sha256",
            "suite_id",
            "requested_samples",
            "sample_seeds",
        )
        receipts_match = all(
            getattr(red, field) == getattr(green, field) == getattr(request, field)
            for field in receipt_fields
        )
        if not (
            request.validation_plan_id == plan.validation_plan_id
            and request.validation_plan_sha256 == plan.validation_plan_sha256
            and request.candidate_id == plan.candidate_id
            and request.candidate_revision == plan.candidate_revision
            and request.candidate_files_sha256 == plan.candidate_files_sha256
            and receipts_match
            and red.receipt_id != green.receipt_id
            and red_lane.order == red.lane_order
            and green_lane.order == green.lane_order
            and red_lane.batch_id == red.batch_id
            and green_lane.batch_id == green.batch_id
            and red_lane.phase == red.phase == "red"
            and green_lane.phase == green.phase == "green"
            and red_lane.platform == red.platform == green_lane.platform == green.platform
            and red.persisted_samples == green.persisted_samples == request.requested_samples
        ):
            raise EvolutionFailureAttributionError(
                "adversarial_attribution_authority_mismatch",
                "Adversarial Batch、Plan、RED 与 GREEN completion authority 不一致。",
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
            suite_id=request.suite_id,
            red_batch_id=red.batch_id,
            green_batch_id=green.batch_id,
            red_samples=red.persisted_samples,
            green_samples=green.persisted_samples,
            red_result_sha256=red.sample_result_sha256,
            green_result_sha256=green.sample_result_sha256,
        )
        return self._kernel.build(authority=authority, comparison=comparison)


class EvolutionAdversarialFailureAttributionExecutor:
    """Reload adversarial H5c authority, classify it, and persist one fact."""

    def __init__(
        self,
        *,
        harness_store: HarnessStore,
        attribution_store: EvolutionFailureAttributionStore,
        builder: EvolutionAdversarialFailureAttributionBuilder | None = None,
    ) -> None:
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Adversarial Attribution executor 需要 HarnessStore。")
        if not isinstance(attribution_store, EvolutionFailureAttributionStore):
            raise TypeError("Adversarial Attribution executor 需要 Attribution Store。")
        self._harness_store = harness_store
        self._attribution_store = attribution_store
        self._builder = builder or EvolutionAdversarialFailureAttributionBuilder()

    async def execute(
        self,
        *,
        batch_request: EvolutionAdversarialBatchRequest,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionAdversarialCohortReceipt,
        green_receipt: EvolutionAdversarialCohortReceipt,
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
                "adversarial_attribution_comparison_read_failed",
                "无法从 Harness Store 读取 Adversarial H5c authority。",
            ) from exc
        if authoritative is None or authoritative != comparison:
            raise EvolutionFailureAttributionError(
                "adversarial_attribution_comparison_not_authoritative",
                "传入的 Adversarial H5c 不是 Harness Store 当前不可变事实。",
            )
        receipt = self._builder.build(
            batch_request=batch_request,
            validation_plan=validation_plan,
            red_receipt=red_receipt,
            green_receipt=green_receipt,
            comparison=authoritative,
        )
        return await self._attribution_store.record(receipt)


__all__ = [
    "EvolutionAdversarialFailureAttributionBuilder",
    "EvolutionAdversarialFailureAttributionExecutor",
]
