"""Deterministic, anti-gaming prioritization across Evolution Candidates."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from naumi_agent.evolution.candidate import EvolutionCandidateDraft
from naumi_agent.evolution.eligibility import CandidateEligibilityAssessment

OpportunityDomain = Literal[
    "capability",
    "correctness",
    "maintainability",
    "performance",
    "reliability",
    "safety",
]

_POLICY_VERSION = "evolution-priority-v1"
_FORMULA = "severity*frequency*confidence*5/(cost*change_risk)"
_WINDOW = timedelta(days=30)
_MAX_CANDIDATES = 500
_AGENT_ONLY_SOURCE = "agent_interpreted_feedback"

_SOURCE_LANES: dict[str, tuple[str, int]] = {
    "rollback_outcome": ("outcome", 100),
    "promoted_outcome": ("outcome", 100),
    "eval_metric_regression": ("quantitative_eval", 95),
    "harness_failure": ("harness", 90),
    "self_review_static": ("static_scan", 80),
    "goal_need": ("explicit_user", 75),
    "user_feedback": ("explicit_user", 75),
    "tool_catalog_miss": ("catalog_fact", 55),
}

_KIND_SEVERITY = {
    "capability": 3,
    "correctness": 4,
    "maintainability": 2,
    "reliability": 4,
    "safety": 5,
}
_FINDING_SEVERITY = {
    "rollback_guardrail_breach": 5,
    "hardcoded_secret": 5,
    "scope_invalid": 5,
    "scope_prohibited": 5,
    "eval_latency_regression": 4,
    "eval_cost_regression": 4,
    "eval_token_regression": 4,
    "eval_metric_regression": 4,
    "stable_promotion_improvement": 2,
}
_KIND_COST = {
    "capability": 5,
    "correctness": 3,
    "maintainability": 2,
    "reliability": 4,
    "safety": 5,
}
_CHANGE_RISK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True, slots=True)
class CandidatePrioritySubject:
    candidate: EvolutionCandidateDraft
    eligibility: CandidateEligibilityAssessment


@dataclass(frozen=True, slots=True)
class CandidatePriority:
    candidate_id: str
    policy_version: str
    formula: str
    domain: OpportunityDomain
    rankable: bool
    rank: int | None
    score: float
    score_basis_points: int
    severity: int
    frequency: int
    qualifying_observations: int
    confidence: int
    confidence_lanes: tuple[str, ...]
    implementation_cost: int
    change_risk: int
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpportunityImpact:
    candidate_count: int
    source_kind_count: int
    scope_count: int
    provider_count: int
    model_count: int
    platform_count: int


@dataclass(frozen=True, slots=True)
class OpportunityCluster:
    cluster_id: str
    rank: int
    domain: OpportunityDomain
    score: float
    primary_candidate_id: str
    candidate_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    impact: OpportunityImpact


@dataclass(frozen=True, slots=True)
class OpportunityPortfolio:
    policy_version: str
    formula: str
    anchor_at: str
    window_start_at: str
    window_days: int
    considered_count: int
    ranked_count: int
    excluded_count: int
    priorities: tuple[CandidatePriority, ...]
    clusters: tuple[OpportunityCluster, ...]


def prioritize_candidates(
    subjects: Iterable[CandidatePrioritySubject],
) -> OpportunityPortfolio:
    """Rank a bounded verified snapshot without granting execution authority."""
    materialized = tuple(subjects)
    if len(materialized) > _MAX_CANDIDATES:
        raise ValueError(f"Opportunity priority 最多处理 {_MAX_CANDIDATES} 个 Candidate。")
    if any(not isinstance(item, CandidatePrioritySubject) for item in materialized):
        raise TypeError("Opportunity priority 只能处理 CandidatePrioritySubject。")
    candidate_ids = [item.candidate.candidate_id for item in materialized]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Opportunity priority 不接受重复 Candidate。")
    if not materialized:
        return OpportunityPortfolio(
            policy_version=_POLICY_VERSION,
            formula=_FORMULA,
            anchor_at="",
            window_start_at="",
            window_days=30,
            considered_count=0,
            ranked_count=0,
            excluded_count=0,
            priorities=(),
            clusters=(),
        )

    anchor = max(_parse(item.candidate.last_observed_at) for item in materialized)
    window_start = anchor - _WINDOW
    preliminary = tuple(
        _priority(item, anchor=anchor, window_start=window_start)
        for item in materialized
    )
    rankable = sorted(
        (item for item in preliminary if item.rankable),
        key=lambda item: (
            -item.score_basis_points,
            -item.severity,
            item.candidate_id,
        ),
    )
    ranks = {item.candidate_id: index for index, item in enumerate(rankable, start=1)}
    priorities = tuple(
        replace(item, rank=ranks.get(item.candidate_id))
        for item in sorted(
            preliminary,
            key=lambda item: (
                0 if item.rankable else 1,
                ranks.get(item.candidate_id, _MAX_CANDIDATES + 1),
                item.candidate_id,
            ),
        )
    )
    by_id = {item.candidate.candidate_id: item.candidate for item in materialized}
    clusters = _clusters(
        priorities,
        by_id,
        window_start=window_start,
        anchor=anchor,
    )
    return OpportunityPortfolio(
        policy_version=_POLICY_VERSION,
        formula=_FORMULA,
        anchor_at=_timestamp(anchor),
        window_start_at=_timestamp(window_start),
        window_days=30,
        considered_count=len(materialized),
        ranked_count=len(rankable),
        excluded_count=len(materialized) - len(rankable),
        priorities=priorities,
        clusters=clusters,
    )


def _priority(
    subject: CandidatePrioritySubject,
    *,
    anchor: datetime,
    window_start: datetime,
) -> CandidatePriority:
    candidate = subject.candidate
    evidence = tuple(
        item
        for item in candidate.evidence
        if window_start < _parse(item.observed_at) <= anchor
        and item.source_kind != _AGENT_ONLY_SOURCE
        and item.source_kind in _SOURCE_LANES
    )
    reasons: list[str] = []
    if _parse(candidate.last_observed_at) <= window_start:
        reasons.append("outside_30d_window")
    authority = next(
        (check for check in subject.eligibility.checks if check.code == "source_authority"),
        None,
    )
    if authority is None or not authority.passed:
        reasons.append("source_authority_invalid")
    governance = next(
        (check for check in subject.eligibility.checks if check.code == "cooldown_gate"),
        None,
    )
    if governance is None or not governance.passed:
        reasons.append("governance_unavailable_or_blocked")
    if not subject.eligibility.review_ready:
        reasons.append("review_not_ready")
    if not evidence:
        reasons.append("no_qualifying_observation")

    source_days = {
        (_SOURCE_LANES[item.source_kind][0], _parse(item.observed_at).astimezone(UTC).date())
        for item in evidence
    }
    qualifying = len(source_days)
    frequency = min(4, qualifying)
    lanes = tuple(sorted({_SOURCE_LANES[item.source_kind][0] for item in evidence}))
    lane_confidences = [_SOURCE_LANES[item.source_kind][1] for item in evidence]
    confidence = min(
        100,
        (max(lane_confidences) if lane_confidences else 0) + max(0, len(lanes) - 1) * 5,
    )
    severity = _FINDING_SEVERITY.get(
        candidate.finding_code,
        _KIND_SEVERITY[candidate.kind],
    )
    cost = _KIND_COST[candidate.kind]
    change_risk = _CHANGE_RISK[candidate.risk.level]
    rankable = not reasons
    score_basis_points = (
        min(10_000, severity * frequency * confidence * 5 // (cost * change_risk))
        if rankable
        else 0
    )
    return CandidatePriority(
        candidate_id=candidate.candidate_id,
        policy_version=_POLICY_VERSION,
        formula=_FORMULA,
        domain=_domain(candidate),
        rankable=rankable,
        rank=None,
        score=score_basis_points / 100,
        score_basis_points=score_basis_points,
        severity=severity,
        frequency=frequency,
        qualifying_observations=qualifying,
        confidence=confidence,
        confidence_lanes=lanes,
        implementation_cost=cost,
        change_risk=change_risk,
        exclusion_reasons=tuple(dict.fromkeys(reasons)),
    )


def _clusters(
    priorities: tuple[CandidatePriority, ...],
    candidates: dict[str, EvolutionCandidateDraft],
    *,
    window_start: datetime,
    anchor: datetime,
) -> tuple[OpportunityCluster, ...]:
    grouped: dict[OpportunityDomain, list[CandidatePriority]] = {}
    for item in priorities:
        if item.rankable:
            grouped.setdefault(item.domain, []).append(item)
    clusters: list[OpportunityCluster] = []
    for domain, members in grouped.items():
        ordered = tuple(sorted(members, key=lambda item: item.rank or _MAX_CANDIDATES + 1))
        drafts = tuple(candidates[item.candidate_id] for item in ordered)
        evidence = tuple(
            item
            for draft in drafts
            for item in draft.evidence
            if window_start < _parse(item.observed_at) <= anchor
            and item.source_kind != _AGENT_ONLY_SOURCE
            and item.source_kind in _SOURCE_LANES
        )
        sources = tuple(sorted({item.source_kind for item in evidence}))
        scopes = {draft.scope for draft in drafts}
        providers = {item.provider for item in evidence if item.provider}
        models = {item.model for item in evidence if item.model}
        platforms = {item.platform for item in evidence if item.platform}
        cluster_hash = hashlib.sha256(
            f"{_POLICY_VERSION}:{domain}".encode()
        ).hexdigest()[:24]
        clusters.append(OpportunityCluster(
            cluster_id=f"eoc_{cluster_hash}",
            rank=0,
            domain=domain,
            # A cluster is not allowed to gain priority merely by accumulating members.
            score=max(item.score for item in ordered),
            primary_candidate_id=ordered[0].candidate_id,
            candidate_ids=tuple(item.candidate_id for item in ordered),
            source_kinds=sources,
            impact=OpportunityImpact(
                candidate_count=len(ordered),
                source_kind_count=len(sources),
                scope_count=len(scopes),
                provider_count=len(providers),
                model_count=len(models),
                platform_count=len(platforms),
            ),
        ))
    ordered_clusters = sorted(
        clusters,
        key=lambda item: (-item.score, item.domain, item.cluster_id),
    )
    return tuple(
        replace(item, rank=index)
        for index, item in enumerate(ordered_clusters, start=1)
    )


def _domain(candidate: EvolutionCandidateDraft) -> OpportunityDomain:
    finding = candidate.finding_code
    if candidate.kind == "capability" or finding == "stable_promotion_improvement":
        return "capability"
    if finding.startswith("eval_"):
        return "performance"
    if finding == "rollback_guardrail_breach" or candidate.kind == "reliability":
        return "reliability"
    return candidate.kind


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: datetime) -> str:
    return value.isoformat()


__all__ = [
    "CandidatePriority",
    "CandidatePrioritySubject",
    "OpportunityCluster",
    "OpportunityImpact",
    "OpportunityPortfolio",
    "prioritize_candidates",
]
