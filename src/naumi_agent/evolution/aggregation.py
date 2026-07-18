"""Deterministic temporal and dimension aggregation for Candidates."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import chain
from typing import Literal

from naumi_agent.evolution.candidate import EvolutionCandidateDraft

CandidateTrend = Literal["new", "increasing", "stable", "decreasing", "insufficient"]


@dataclass(frozen=True, slots=True)
class DimensionCount:
    value: str
    count: int
    percentage: float


@dataclass(frozen=True, slots=True)
class EvidenceRepresentative:
    evidence_id: str
    source_kind: str
    observed_at: str
    ref_uri: str
    ref_sha256_prefix: str


@dataclass(frozen=True, slots=True)
class CandidateAggregation:
    policy_version: str
    anchor_at: str
    first_observed_at: str
    last_observed_at: str
    span_seconds: int
    total_count: int
    count_24h: int
    count_7d: int
    count_30d: int
    previous_7d_count: int
    trend: CandidateTrend
    source_counts: tuple[DimensionCount, ...]
    source_unique_count: int
    provider_counts: tuple[DimensionCount, ...]
    provider_unique_count: int
    model_counts: tuple[DimensionCount, ...]
    model_unique_count: int
    platform_counts: tuple[DimensionCount, ...]
    platform_unique_count: int
    representatives: tuple[EvidenceRepresentative, ...]


def aggregate_candidate(candidate: EvolutionCandidateDraft) -> CandidateAggregation:
    """Aggregate one validated Candidate relative to its last observation."""
    if not isinstance(candidate, EvolutionCandidateDraft):
        raise TypeError("aggregation 只能处理 EvolutionCandidateDraft。")
    evidence = candidate.evidence
    observed = tuple(_parse(item.observed_at) for item in evidence)
    anchor = max(observed)
    first = min(observed)
    count_24h = _window_count(observed, anchor, timedelta(hours=24))
    count_7d = _window_count(observed, anchor, timedelta(days=7))
    count_30d = _window_count(observed, anchor, timedelta(days=30))
    previous_7d = sum(
        anchor - timedelta(days=14) < value <= anchor - timedelta(days=7)
        for value in observed
    )
    source_counts, source_unique_count = _dimension(item.source_kind for item in evidence)
    provider_counts, provider_unique_count = _dimension(item.provider for item in evidence)
    model_counts, model_unique_count = _dimension(item.model for item in evidence)
    platform_counts, platform_unique_count = _dimension(item.platform for item in evidence)
    return CandidateAggregation(
        policy_version="candidate-aggregation-v1",
        anchor_at=candidate.last_observed_at,
        first_observed_at=candidate.first_observed_at,
        last_observed_at=candidate.last_observed_at,
        span_seconds=max(0, int((anchor - first).total_seconds())),
        total_count=len(evidence),
        count_24h=count_24h,
        count_7d=count_7d,
        count_30d=count_30d,
        previous_7d_count=previous_7d,
        trend=_trend(
            total=len(evidence),
            span=anchor - first,
            current=count_7d,
            previous=previous_7d,
        ),
        source_counts=source_counts,
        source_unique_count=source_unique_count,
        provider_counts=provider_counts,
        provider_unique_count=provider_unique_count,
        model_counts=model_counts,
        model_unique_count=model_unique_count,
        platform_counts=platform_counts,
        platform_unique_count=platform_unique_count,
        representatives=_representatives(candidate),
    )


def _window_count(
    observed: tuple[datetime, ...],
    anchor: datetime,
    window: timedelta,
) -> int:
    start = anchor - window
    return sum(start < value <= anchor for value in observed)


def _trend(*, total: int, span: timedelta, current: int, previous: int) -> CandidateTrend:
    if total == 1:
        return "new"
    if total < 4 or span < timedelta(days=7):
        return "insufficient"
    if current > previous and current >= max(2, previous * 1.5):
        return "increasing"
    if previous > current and previous >= max(2, current * 1.5):
        return "decreasing"
    return "stable"


def _dimension(values) -> tuple[tuple[DimensionCount, ...], int]:
    normalized = [str(value).strip() or "unknown" for value in values]
    total = len(normalized)
    counts = Counter(normalized)
    entries = tuple(
        DimensionCount(
            value=value,
            count=count,
            percentage=round(count / total * 100, 1),
        )
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:20]
    )
    return entries, len(counts)


def _representatives(candidate: EvolutionCandidateDraft) -> tuple[EvidenceRepresentative, ...]:
    selected = []
    selected_ids: set[str] = set()
    seen_sources: set[str] = set()
    boundary_ids = {candidate.evidence[0].evidence_id, candidate.evidence[-1].evidence_id}
    ordered = chain(
        candidate.evidence[:1],
        candidate.evidence[-1:],
        reversed(candidate.evidence),
    )
    for item in ordered:
        if item.evidence_id in selected_ids:
            continue
        is_boundary = item.evidence_id in boundary_ids
        if item.source_kind in seen_sources and not is_boundary:
            continue
        ref = item.refs[0]
        selected.append(EvidenceRepresentative(
            evidence_id=item.evidence_id,
            source_kind=item.source_kind,
            observed_at=item.observed_at,
            ref_uri=ref.uri,
            ref_sha256_prefix=ref.sha256[:12],
        ))
        selected_ids.add(item.evidence_id)
        seen_sources.add(item.source_kind)
        if len(selected) >= 16:
            break
    return tuple(selected)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = [
    "CandidateAggregation",
    "DimensionCount",
    "EvidenceRepresentative",
    "aggregate_candidate",
]
