"""Authority-bound HAR-08 comparison for Self-Review RED/GREEN cohorts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from naumi_agent.evolution.self_review_eval_runtime import (
    SelfReviewEvalRuntimeError,
    build_self_review_eval_configuration,
    require_continuous_eval_prefix,
    validate_self_review_cohort_authority,
)
from naumi_agent.evolution.self_review_green_cohort import (
    EvolutionSelfReviewGreenCohortReceipt,
    EvolutionSelfReviewGreenCohortRequest,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedCohortReceipt,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoredEvalResult,
    HarnessStoreError,
)


class EvolutionSelfReviewComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionSelfReviewComparisonExecutor:
    """Persist the native H5c authority after verifying the full EVO chain."""

    def __init__(self, store: HarnessStore) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("Self-Review Comparison executor 需要 HarnessStore。")
        self._store = store

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionSelfReviewRedCohortReceipt,
        green_request: EvolutionSelfReviewGreenCohortRequest,
        green_receipt: EvolutionSelfReviewGreenCohortReceipt,
    ) -> HarnessStoredEvalComparisonReceipt:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
            request, binding, plan = validate_self_review_cohort_authority(
                baseline_request,
                metric_binding,
                validation_plan,
            )
            red = EvolutionSelfReviewRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionSelfReviewGreenCohortRequest.model_validate(
                green_request.model_dump(mode="json")
            )
            green_run = EvolutionSelfReviewGreenCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
        except (AttributeError, OSError, TypeError, ValueError, SelfReviewEvalRuntimeError) as exc:
            raise EvolutionSelfReviewComparisonError(
                "comparison_authority_invalid",
                "Self-Review Comparison authority 无效或已被篡改。",
            ) from exc
        _require_authority(request, binding, plan, red, green, green_run)
        try:
            red_records = await self._store.list_eval_results(
                workspace,
                request.batch_id,
                request.suite_id,
                limit=request.requested_samples + 1,
            )
            green_records = await self._store.list_eval_results(
                workspace,
                green.batch_id,
                green.suite_id,
                limit=green.requested_samples + 1,
            )
            _require_stored_cohort(
                red_records,
                expected_count=request.requested_samples,
                expected_digests=red.sample_result_sha256,
                phase="red",
            )
            _require_stored_cohort(
                green_records,
                expected_count=green.requested_samples,
                expected_digests=green_run.sample_result_sha256,
                phase="green",
            )
            _require_comparable_identities(
                red_records,
                green_records,
                request=request,
                binding=binding,
                plan=plan,
                green=green_run,
            )
            reference = await self._store.register_eval_comparison_reference(
                workspace_root=workspace,
                batch_id=request.batch_id,
                suite_id=request.suite_id,
                registered_by="evolution-validator",
                registration_reason=(
                    f"EVO-03 RED reference {plan.validation_plan_id}"
                ),
                created_at=red.completed_at,
            )
            if reference.purpose != "comparison_reference":
                raise EvolutionSelfReviewComparisonError(
                    "comparison_reference_purpose_mismatch",
                    "RED cohort 已被占用为非 reference Baseline。",
                )
            receipt = build_eval_comparison_receipt(
                workspace_root=workspace,
                suite_id=request.suite_id,
                baseline_id=reference.id,
                baseline_batch_id=reference.batch_id,
                baseline_samples_sha256=reference.samples_sha256,
                baseline_samples=_receipt_samples(red_records),
                current_batch_id=green.batch_id,
                current_samples=_receipt_samples(green_records),
                created_at=green_run.completed_at,
            )
            stored = await self._store.record_eval_comparison_receipt(receipt)
        except EvolutionSelfReviewComparisonError:
            raise
        except HarnessStoreConflictError as exc:
            raise EvolutionSelfReviewComparisonError(
                "comparison_persistence_conflict",
                "Self-Review Comparison 与既有不可变证据冲突。",
            ) from exc
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionSelfReviewComparisonError(
                "comparison_persistence_failed",
                "Self-Review Comparison 无法从可信 H5 evidence 持久化。",
            ) from exc
        if stored.receipt != receipt or stored.receipt_sha256 != receipt.receipt_sha256:
            raise EvolutionSelfReviewComparisonError(
                "comparison_restore_mismatch",
                "H5c 恢复的 Comparison Receipt 与写入 authority 不一致。",
            )
        return stored


def _require_authority(
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    red: EvolutionSelfReviewRedCohortReceipt,
    green: EvolutionSelfReviewGreenCohortRequest,
    green_run: EvolutionSelfReviewGreenCohortReceipt,
) -> None:
    if not (
        red.baseline_request_id == request.request_id
        and red.baseline_request_sha256 == request.request_sha256
        and red.metric_binding_id == binding.binding_id
        and red.metric_binding_sha256 == binding.binding_sha256
        and red.validation_plan_id == plan.validation_plan_id
        and red.validation_plan_sha256 == plan.validation_plan_sha256
        and green.baseline_request_id == request.request_id
        and green.baseline_request_sha256 == request.request_sha256
        and green.red_receipt_id == red.receipt_id
        and green.red_receipt_sha256 == red.receipt_sha256
        and green.metric_binding_id == binding.binding_id
        and green.metric_binding_sha256 == binding.binding_sha256
        and green.validation_plan_id == plan.validation_plan_id
        and green.validation_plan_sha256 == plan.validation_plan_sha256
        and green.candidate_id == plan.candidate_id
        and green.candidate_revision == plan.candidate_revision
        and green.candidate_files_sha256 == plan.candidate_files_sha256
        and green.suite_id == request.suite_id
        and green.requested_samples == request.requested_samples
        and green.sample_seeds == request.sample_seeds
        and green_run.green_request_id == green.request_id
        and green_run.green_request_sha256 == green.request_sha256
        and green_run.red_receipt_id == red.receipt_id
        and green_run.red_receipt_sha256 == red.receipt_sha256
        and green_run.validation_plan_id == plan.validation_plan_id
        and green_run.validation_plan_sha256 == plan.validation_plan_sha256
        and green_run.lease_id == green.lease_id
        and green_run.candidate_id == plan.candidate_id
        and green_run.candidate_revision == plan.candidate_revision
        and green_run.suite_id == request.suite_id
        and green_run.batch_id == green.batch_id
        and red.persisted_samples == green_run.persisted_samples
        == request.requested_samples
    ):
        raise EvolutionSelfReviewComparisonError(
            "comparison_authority_mismatch",
            "RED、GREEN、Plan 与 Metric Binding authority 不一致。",
        )


def _require_stored_cohort(
    records: tuple[HarnessStoredEvalResult, ...],
    *,
    expected_count: int,
    expected_digests: tuple[str, ...],
    phase: Literal["red", "green"],
) -> None:
    try:
        require_continuous_eval_prefix(records, expected_count, phase=phase)
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionSelfReviewComparisonError(exc.code, str(exc)) from exc
    if (
        len(records) != expected_count
        or tuple(item.result_sha256 for item in records) != expected_digests
    ):
        raise EvolutionSelfReviewComparisonError(
            f"{phase}_cohort_evidence_mismatch",
            f"H5a {phase.upper()} cohort 与 completion receipt 不一致。",
        )


def _require_comparable_identities(
    red_records: tuple[HarnessStoredEvalResult, ...],
    green_records: tuple[HarnessStoredEvalResult, ...],
    *,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    green: EvolutionSelfReviewGreenCohortReceipt,
) -> None:
    configuration = build_self_review_eval_configuration(request, binding, plan)
    red_identities = {item.result.baseline_identity for item in red_records}
    green_identities = {item.result.baseline_identity for item in green_records}
    if None in red_identities or None in green_identities:
        raise EvolutionSelfReviewComparisonError(
            "comparison_identity_missing",
            "RED/GREEN cohort 缺少可比较 Identity。",
        )
    if len(red_identities) != 1 or len(green_identities) != 1:
        raise EvolutionSelfReviewComparisonError(
            "comparison_identity_unstable",
            "RED/GREEN cohort 内部 Identity 不统一。",
        )
    red_identity = next(iter(red_identities))
    green_identity = next(iter(green_identities))
    assert red_identity is not None and green_identity is not None
    if not (
        red_identity.configuration == green_identity.configuration == configuration
        and red_identity.platform == green_identity.platform
        and red_identity.source.commit == request.baseline_commit
        and red_identity.source.tree_sha256
        == f"sha256:{request.baseline_tree_sha256}"
        and not red_identity.source.dirty
        and green_identity.source.commit == green.candidate_head
        and green_identity.source.tree_sha256 == green.candidate_tree_sha256
        and green_identity.source.dirty
    ):
        raise EvolutionSelfReviewComparisonError(
            "comparison_identity_mismatch",
            "RED/GREEN configuration、平台或 source identity 不可比较。",
        )


def _receipt_samples(
    records: tuple[HarnessStoredEvalResult, ...],
) -> tuple[EvalReceiptSample, ...]:
    return tuple(
        EvalReceiptSample(
            sample_index=item.sample_index,
            result_sha256=item.result_sha256,
            result=item.result,
        )
        for item in records
    )


__all__ = [
    "EvolutionSelfReviewComparisonError",
    "EvolutionSelfReviewComparisonExecutor",
]
