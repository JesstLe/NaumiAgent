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
        "portfolio": _portfolio_payload(snapshot),
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
        "priority": _priority_payload(item),
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
            "governance": _governance_payload(item),
            "aggregation": _aggregation_payload(item),
            "proposal": _proposal_payload(item),
            "capability_proposal": _capability_proposal_payload(item),
            "capability_specification": _capability_specification_payload(item),
            "capability_governance": _capability_governance_payload(item),
            "capability_artifact": _capability_artifact_payload(item),
        })
    return payload


def _priority_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.priority
    if value is None:
        return None
    return {
        "policy_version": value.policy_version,
        "formula": value.formula,
        "domain": value.domain,
        "rankable": value.rankable,
        "rank": value.rank,
        "score": value.score,
        "severity": value.severity,
        "frequency": value.frequency,
        "qualifying_observations": value.qualifying_observations,
        "confidence": value.confidence,
        "confidence_lanes": list(value.confidence_lanes),
        "implementation_cost": value.implementation_cost,
        "change_risk": value.change_risk,
        "exclusion_reasons": list(value.exclusion_reasons),
    }


def _portfolio_payload(snapshot: EvolutionReviewSnapshot) -> dict[str, Any] | None:
    value = snapshot.portfolio
    if value is None:
        return None
    return {
        "policy_version": value.policy_version,
        "formula": value.formula,
        "anchor_at": value.anchor_at,
        "window_start_at": value.window_start_at,
        "window_days": value.window_days,
        "considered_count": value.considered_count,
        "ranked_count": value.ranked_count,
        "excluded_count": value.excluded_count,
        "clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "rank": cluster.rank,
                "domain": cluster.domain,
                "score": cluster.score,
                "primary_candidate_id": cluster.primary_candidate_id,
                "candidate_ids": list(cluster.candidate_ids),
                "source_kinds": list(cluster.source_kinds),
                "impact": {
                    "candidate_count": cluster.impact.candidate_count,
                    "source_kind_count": cluster.impact.source_kind_count,
                    "scope_count": cluster.impact.scope_count,
                    "provider_count": cluster.impact.provider_count,
                    "model_count": cluster.impact.model_count,
                    "platform_count": cluster.impact.platform_count,
                },
            }
            for cluster in value.clusters[:20]
        ],
    }


def _governance_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.governance
    if value is None:
        return None
    return {
        "policy_version": value.policy_version,
        "allowed": value.allowed,
        "reason": value.reason,
        "proposal_state": value.proposal_state,
        "proposal_revision": value.proposal_revision,
        "cooldown_until": value.cooldown_until,
        "significant_new_evidence": value.significant_new_evidence,
    }


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


def _capability_proposal_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.capability_proposal
    if value is None:
        return None
    return value.model_dump(mode="json")


def _capability_specification_payload(
    item: EvolutionReviewItem,
) -> dict[str, Any] | None:
    value = item.capability_specification
    if value is None:
        return None
    return value.model_dump(mode="json")


def _capability_governance_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.capability_governance
    if value is None:
        return None
    return value.model_dump(mode="json")


def _capability_artifact_payload(item: EvolutionReviewItem) -> dict[str, Any] | None:
    value = item.capability_artifact
    if value is None:
        return None
    payload = value.model_dump(mode="json")
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        artifact.pop("source_text", None)
    return payload


__all__ = ["evolution_review_payload"]
