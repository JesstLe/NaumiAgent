"""Shared H5b2/H5c persistence kernel for authority-validated evolution cohorts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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


class EvolutionComparisonKernelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionComparisonKernel:
    """Persist one native HAR-08 comparison after lane-specific validation."""

    def __init__(self, store: HarnessStore) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("Evolution Comparison kernel 需要 HarnessStore。")
        self._store = store

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        suite_id: str,
        red_batch_id: str,
        green_batch_id: str,
        red_completed_at: str,
        green_completed_at: str,
        validation_plan_id: str,
        lane_label: Literal["Self-Review", "Interventional"],
        red_records: tuple[HarnessStoredEvalResult, ...],
        green_records: tuple[HarnessStoredEvalResult, ...],
    ) -> HarnessStoredEvalComparisonReceipt:
        try:
            reference = await self._store.register_eval_comparison_reference(
                workspace_root=workspace_root,
                batch_id=red_batch_id,
                suite_id=suite_id,
                registered_by="evolution-validator",
                registration_reason=(
                    f"EVO-03 RED reference {validation_plan_id}"
                ),
                created_at=red_completed_at,
            )
            if reference.purpose != "comparison_reference":
                raise EvolutionComparisonKernelError(
                    "comparison_reference_purpose_mismatch",
                    "RED cohort 已被占用为非 reference Baseline。",
                )
            receipt = build_eval_comparison_receipt(
                workspace_root=workspace_root,
                suite_id=suite_id,
                baseline_id=reference.id,
                baseline_batch_id=reference.batch_id,
                baseline_samples_sha256=reference.samples_sha256,
                baseline_samples=_receipt_samples(red_records),
                current_batch_id=green_batch_id,
                current_samples=_receipt_samples(green_records),
                created_at=green_completed_at,
            )
            stored = await self._store.record_eval_comparison_receipt(receipt)
        except EvolutionComparisonKernelError:
            raise
        except HarnessStoreConflictError as exc:
            raise EvolutionComparisonKernelError(
                "comparison_persistence_conflict",
                f"{lane_label} Comparison 与既有不可变证据冲突。",
            ) from exc
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionComparisonKernelError(
                "comparison_persistence_failed",
                f"{lane_label} Comparison 无法从可信 H5 evidence 持久化。",
            ) from exc
        if stored.receipt != receipt or stored.receipt_sha256 != receipt.receipt_sha256:
            raise EvolutionComparisonKernelError(
                "comparison_restore_mismatch",
                "H5c 恢复的 Comparison Receipt 与写入 authority 不一致。",
            )
        return stored


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
    "EvolutionComparisonKernel",
    "EvolutionComparisonKernelError",
]
