from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.prioritization import (
    CandidatePrioritySubject,
    prioritize_candidates,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(
    label: str,
    *,
    source_kind: str,
    observed_at: datetime,
    finding_code: str,
    scope: str,
    root: str | None = None,
) -> EvolutionEvidence:
    scheme = {
        "goal_need": "goal",
        "tool_catalog_miss": "tool-search",
    }.get(source_kind, "artifact")
    uri = f"{scheme}://priority/{label}"
    values: dict[str, object] = {
        "evidence_id": f"eve_{_digest(f'{label}:{observed_at.isoformat()}')[:24]}",
        "source_kind": source_kind,
        "source_uri": uri,
        "observed_at": observed_at.isoformat(),
        "finding_code": finding_code,
        "scope": scope,
        "root_fingerprint": root or _digest(label),
        "refs": (EvolutionEvidenceRef(uri=uri, sha256=_digest(uri)),),
        "provider": "openai",
        "model": "test-model",
        "platform": "darwin",
    }
    if source_kind in {"user_feedback", "agent_interpreted_feedback"}:
        values.update(feedback_category="defect", feedback_topic="priority")
    return EvolutionEvidence.model_validate(values)


def _subject(*evidence: EvolutionEvidence, authority: bool = True):
    candidate = build_candidate_draft(evidence)
    return CandidatePrioritySubject(
        candidate=candidate,
        eligibility=assess_candidate_eligibility(
            candidate,
            governance=CandidateGovernanceContext(
                allowed=True,
                reason="no_active_cooldown",
            ),
            source_authority_valid=authority,
        ),
    )


def _capability(label: str, *, source_kind: str, at: datetime = NOW):
    finding = "user_explicit_need" if source_kind == "goal_need" else "missing_tool_capability"
    scope = (
        f"capability:need:{_digest(label)}"
        if source_kind == "goal_need"
        else f"capability:tool:{label}"
    )
    return _subject(_evidence(
        label,
        source_kind=source_kind,
        observed_at=at,
        finding_code=finding,
        scope=scope,
    ))


def test_priority_is_replay_stable_explainable_and_cross_source_clustered() -> None:
    goal = _capability("browser-audit", source_kind="goal_need")
    tool = _capability("browser_trace_compare", source_kind="tool_catalog_miss")

    first = prioritize_candidates((goal, tool))
    second = prioritize_candidates((tool, goal))

    assert first == second
    assert first.anchor_at == NOW.isoformat()
    assert first.window_days == 30
    assert first.ranked_count == 2
    assert first.excluded_count == 0
    assert first.formula == "severity*frequency*confidence*5/(cost*change_risk)"
    assert [item.candidate_id for item in first.priorities] == [
        goal.candidate.candidate_id,
        tool.candidate.candidate_id,
    ]
    assert [item.confidence for item in first.priorities] == [75, 55]
    cluster = first.clusters[0]
    assert cluster.domain == "capability"
    assert cluster.source_kinds == ("goal_need", "tool_catalog_miss")
    assert cluster.impact.candidate_count == 2
    assert cluster.impact.source_kind_count == 2
    assert cluster.impact.scope_count == 2
    assert cluster.score == first.priorities[0].score


def test_repetition_same_authority_lane_and_day_cannot_raise_frequency() -> None:
    root = _digest("same-feedback-root")
    evidence = tuple(
        _evidence(
            f"feedback-{index}",
            source_kind="user_feedback",
            observed_at=NOW + timedelta(minutes=index),
            finding_code="user_reported_defect",
            scope="ui:footer",
            root=root,
        )
        for index in range(20)
    )

    priority = prioritize_candidates((_subject(*evidence),)).priorities[0]

    assert priority.rankable
    assert priority.frequency == 1
    assert priority.qualifying_observations == 1
    assert priority.confidence == 75


def test_frequency_caps_at_four_days_and_cluster_impact_uses_same_window() -> None:
    root = _digest("multi-day-root")
    evidence = tuple(
        _evidence(
            f"day-{index}",
            source_kind="user_feedback",
            observed_at=NOW - timedelta(days=index),
            finding_code="user_reported_defect",
            scope="ui:timeline",
            root=root,
        )
        for index in range(10)
    )
    old = _evidence(
        "old-provider",
        source_kind="user_feedback",
        observed_at=NOW - timedelta(days=40),
        finding_code="user_reported_defect",
        scope="ui:timeline",
        root=root,
    ).model_copy(update={"provider": "legacy-provider"})

    portfolio = prioritize_candidates((_subject(old, *evidence),))
    priority = portfolio.priorities[0]

    assert priority.frequency == 4
    assert priority.qualifying_observations == 10
    assert portfolio.clusters[0].impact.provider_count == 1


def test_agent_only_old_revoked_and_not_review_ready_candidates_are_excluded() -> None:
    old = _subject(_evidence(
        "old-static",
        source_kind="self_review_static",
        observed_at=NOW - timedelta(days=31),
        finding_code="long_function",
        scope="module.py:old_function",
    ))
    revoked = _capability("revoked-goal", source_kind="goal_need")
    revoked = CandidatePrioritySubject(
        candidate=revoked.candidate,
        eligibility=assess_candidate_eligibility(
            revoked.candidate,
            governance=CandidateGovernanceContext(
                allowed=True,
                reason="no_active_cooldown",
            ),
            source_authority_valid=False,
        ),
    )
    agent = _subject(*tuple(
        _evidence(
            f"agent-{index}",
            source_kind="agent_interpreted_feedback",
            observed_at=NOW - timedelta(hours=index),
            finding_code="user_reported_defect",
            scope="ui:agent-only",
            root=_digest("agent-root"),
        )
        for index in range(2)
    ))
    current = _capability("current-goal", source_kind="goal_need")

    portfolio = prioritize_candidates((old, revoked, agent, current))
    by_id = {item.candidate_id: item for item in portfolio.priorities}

    assert portfolio.ranked_count == 1
    assert portfolio.excluded_count == 3
    assert by_id[current.candidate.candidate_id].rank == 1
    assert "outside_30d_window" in by_id[old.candidate.candidate_id].exclusion_reasons
    assert "source_authority_invalid" in by_id[
        revoked.candidate.candidate_id
    ].exclusion_reasons
    assert by_id[agent.candidate.candidate_id].exclusion_reasons == (
        "review_not_ready",
        "no_qualifying_observation",
    )
    assert all(
        agent.candidate.candidate_id not in item.candidate_ids
        for item in portfolio.clusters
    )


def test_missing_governance_context_fails_closed_even_if_review_policy_is_ready() -> None:
    current = _capability("governance-required", source_kind="goal_need")
    without_governance = CandidatePrioritySubject(
        candidate=current.candidate,
        eligibility=assess_candidate_eligibility(current.candidate),
    )

    priority = prioritize_candidates((without_governance,)).priorities[0]

    assert without_governance.eligibility.review_ready is True
    assert priority.rankable is False
    assert priority.score == 0
    assert priority.exclusion_reasons == ("governance_unavailable_or_blocked",)


def test_cluster_score_is_max_not_sum_and_input_is_bounded() -> None:
    first = _capability("goal-1", source_kind="goal_need")
    second = _capability("goal-2", source_kind="goal_need")
    one = prioritize_candidates((first,))
    two = prioritize_candidates((first, second))

    assert one.clusters[0].score == two.clusters[0].score
    assert two.clusters[0].impact.candidate_count == 2
    with pytest.raises(ValueError, match="最多处理 500"):
        prioritize_candidates((first,) * 501)
    with pytest.raises(ValueError, match="重复 Candidate"):
        prioritize_candidates((first, first))
