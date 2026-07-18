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
        })
    return payload


__all__ = ["evolution_review_payload"]
