"""Strict typed payloads for the Evolution Candidate review page."""

from __future__ import annotations

from typing import Any

from naumi_agent.evolution.review import EvolutionReviewItem, EvolutionReviewSnapshot


def evolution_review_payload(snapshot: EvolutionReviewSnapshot) -> dict[str, Any]:
    """Project a bounded review snapshot into the public UI protocol."""
    return {
        "schema_version": 1,
        "mode": snapshot.mode,
        "filters": {
            "query": snapshot.filters.query,
            "risk": snapshot.filters.risk,
            "source_kind": snapshot.filters.source_kind,
            "limit": snapshot.filters.limit,
        },
        "items": [_item_payload(item, detail=False) for item in snapshot.items[:100]],
        "selected": (
            _item_payload(snapshot.selected, detail=True)
            if snapshot.selected is not None
            else None
        ),
        "events": [
            {
                "revision": event.revision,
                "event_type": event.event_type,
                "added_evidence_count": len(event.added_evidence_ids),
                "occurred_at": event.occurred_at,
            }
            for event in snapshot.events[-100:]
        ],
        "read_only": True,
    }


def _item_payload(item: EvolutionReviewItem, *, detail: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "candidate_id": item.candidate_id,
        "finding_code": item.finding_code,
        "kind": item.kind,
        "scope": item.scope,
        "risk": item.risk,
        "occurrence_count": item.occurrence_count,
        "source_kinds": list(item.source_kinds[:16]),
        "last_observed_at": item.last_observed_at,
        "revision": item.revision,
        "decision": item.eligibility.decision,
        "review_ready": item.eligibility.review_ready,
        "human_review_required": item.eligibility.human_review_required,
        "experiment_eligible": False,
    }
    if detail:
        payload.update({
            "status": item.status,
            "hypothesis": item.hypothesis,
            "providers": list(item.providers[:50]),
            "models": list(item.models[:50]),
            "platforms": list(item.platforms[:50]),
            "first_observed_at": item.first_observed_at,
            "expected_metrics": list(item.expected_metrics[:8]),
            "evidence_refs": list(item.evidence_refs[:200]),
            "policy_version": item.eligibility.policy_version,
            "checks": [
                {
                    "code": check.code,
                    "passed": check.passed,
                    "hard_block": check.hard_block,
                    "detail": check.detail,
                }
                for check in item.eligibility.checks[:16]
            ],
            "aggregation": _aggregation_payload(item),
            "proposal": _proposal_payload(item),
        })
    return payload


def _aggregation_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.aggregation
    if value is None:
        return None
    return {
        "policy_version": value.policy_version,
        "anchor_at": value.anchor_at,
        "span_seconds": value.span_seconds,
        "total_count": value.total_count,
        "count_24h": value.count_24h,
        "count_7d": value.count_7d,
        "count_30d": value.count_30d,
        "previous_7d_count": value.previous_7d_count,
        "trend": value.trend,
        "source_counts": _dimension_payload(value.source_counts),
        "source_unique_count": value.source_unique_count,
        "provider_counts": _dimension_payload(value.provider_counts),
        "provider_unique_count": value.provider_unique_count,
        "model_counts": _dimension_payload(value.model_counts),
        "model_unique_count": value.model_unique_count,
        "platform_counts": _dimension_payload(value.platform_counts),
        "platform_unique_count": value.platform_unique_count,
        "representatives": [
            {
                "evidence_id": entry.evidence_id,
                "source_kind": entry.source_kind,
                "observed_at": entry.observed_at,
                "ref_uri": entry.ref_uri,
                "ref_sha256_prefix": entry.ref_sha256_prefix,
            }
            for entry in value.representatives[:16]
        ],
    }


def _dimension_payload(values) -> list[dict[str, Any]]:
    return [
        {"value": item.value, "count": item.count, "percentage": item.percentage}
        for item in values[:20]
    ]


def _proposal_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.proposal
    if value is None:
        return None
    return {
        "schema_version": value.schema_version,
        "proposal_id": value.proposal_id,
        "generator_version": value.generator_version,
        "proposal_kind": value.proposal_kind,
        "classification_reason": value.classification_reason,
        "title": value.title,
        "summary": value.summary,
        "impact_scope": value.impact_scope,
        "intended_files": list(value.intended_files),
        "validation_plan": [step.model_dump(mode="json") for step in value.validation_plan],
        "risk_level": value.risk_level,
        "review_notes": list(value.review_notes),
        "source": value.source.model_dump(mode="json"),
        "requires_human_review": True,
        "executable": False,
        "experiment_eligible": False,
        "state": value.state,
    }


__all__ = ["evolution_review_payload"]
