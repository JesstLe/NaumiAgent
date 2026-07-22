"""Bounded terminal projection for one authoritative Evaluation Lane Receipt."""

from __future__ import annotations

from typing import Any

from naumi_agent.evolution.evaluation_lane_receipts import (
    EvolutionEvaluationCohortSummary,
    EvolutionEvaluationLaneReceipt,
)


def evaluation_lane_receipt_payload(
    receipt: EvolutionEvaluationLaneReceipt,
) -> dict[str, Any]:
    """Return the public, path-free projection consumed by terminal clients."""
    return {
        "schema_version": 1,
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": receipt.receipt_sha256,
        "lane_kind": str(receipt.lane_kind),
        "platform": receipt.platform,
        "validation_plan_id": receipt.validation_plan_id,
        "validation_plan_sha256": receipt.validation_plan_sha256,
        "candidate_id": receipt.candidate_id,
        "candidate_revision": receipt.candidate_revision,
        "suite_id": receipt.suite_id,
        "comparison_id": receipt.comparison_id,
        "comparison_receipt_sha256": receipt.comparison_receipt_sha256,
        "comparison_decision": str(receipt.comparison_decision),
        "statistical_verdict": str(receipt.statistical_verdict),
        "statistical_code": receipt.statistical_code,
        "attribution_id": receipt.attribution_id,
        "attribution_sha256": receipt.attribution_sha256,
        "failure_category": str(receipt.failure_category),
        "failure_reason_code": receipt.failure_reason_code,
        "failure_action": str(receipt.failure_action),
        "candidate_fault": receipt.candidate_fault,
        "retryable": receipt.retryable,
        "requires_rerun": receipt.requires_rerun,
        "reflection_eligible": receipt.reflection_eligible,
        "baseline": _cohort_payload(receipt.baseline),
        "candidate": _cohort_payload(receipt.candidate),
        "artifacts": [item.model_dump(mode="json") for item in receipt.artifacts],
        "evidence_first_at": receipt.evidence_first_at,
        "evidence_last_at": receipt.evidence_last_at,
        "candidate_evaluation_complete": receipt.candidate_evaluation_complete,
        "aggregation_required": receipt.aggregation_required,
        "created_at": receipt.created_at,
    }


def _cohort_payload(summary: EvolutionEvaluationCohortSummary) -> dict[str, Any]:
    return summary.model_dump(mode="json")


__all__ = ["evaluation_lane_receipt_payload"]
