from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.evidence import adapt_self_review_static_evidence
from naumi_agent.evolution.self_review import scan_self_review_files
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    FeedbackSourceEnvelope,
    build_agent_interpreted_feedback,
    build_direct_user_feedback,
)

NOW = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)


async def _candidate(
    tmp_path: Path,
    *,
    scope: str = "ui:footer",
    repeats: int = 1,
):
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    intake = FeedbackIntakeService(store)
    result = None
    for offset in range(repeats):
        result = await intake.ingest(
            tmp_path,
            build_direct_user_feedback(
                session_id="eligibility-session",
                category="defect",
                scope=scope,
                topic="truncation",
                summary=f"底栏被截断 {offset}",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert result is not None
    stored = await store.get_candidate(tmp_path, result.candidate_id)
    assert stored is not None
    return stored.draft


@pytest.mark.asyncio
async def test_repeated_direct_feedback_is_review_ready_but_not_experiment_eligible(
    tmp_path: Path,
) -> None:
    assessment = assess_candidate_eligibility(
        await _candidate(tmp_path, repeats=2),
    )

    assert assessment.decision == "review_ready"
    assert assessment.review_ready is True
    assert assessment.experiment_eligible is False
    assert {check.code for check in assessment.checks} == {
        "protected_scope",
        "evidence_strength",
        "mechanical_verifier",
        "cooldown_gate",
        "experiment_contract",
    }


@pytest.mark.asyncio
async def test_single_feedback_needs_more_evidence(tmp_path: Path) -> None:
    assessment = assess_candidate_eligibility(await _candidate(tmp_path))

    assert assessment.decision == "needs_evidence"
    evidence = next(check for check in assessment.checks if check.code == "evidence_strength")
    assert evidence.passed is False
    assert "仅出现 1 次" in evidence.detail


@pytest.mark.asyncio
async def test_bound_active_cooldown_suppresses_review_ready(tmp_path: Path) -> None:
    candidate = await _candidate(tmp_path, repeats=2)
    assessment = assess_candidate_eligibility(
        candidate,
        governance=CandidateGovernanceContext(
            allowed=False,
            reason="cooldown_active",
            proposal_state="rejected",
            proposal_revision=2,
            cooldown_until="2026-08-17T14:00:00+00:00",
        ),
    )

    assert assessment.policy_version == "candidate-eligibility-v2"
    assert assessment.decision == "needs_evidence"
    assert assessment.review_ready is False
    cooldown = next(check for check in assessment.checks if check.code == "cooldown_gate")
    assert cooldown.passed is False
    assert "2026-08-17" in cooldown.detail


@pytest.mark.asyncio
async def test_significant_evidence_context_restores_review_ready(tmp_path: Path) -> None:
    candidate = await _candidate(tmp_path, repeats=4)
    assessment = assess_candidate_eligibility(
        candidate,
        governance=CandidateGovernanceContext(
            allowed=True,
            reason="significant_new_evidence",
            proposal_state="rejected",
            proposal_revision=2,
            cooldown_until="2026-08-17T14:00:00+00:00",
            significant_new_evidence=True,
        ),
    )

    assert assessment.decision == "review_ready"
    assert assessment.review_ready is True
    cooldown = next(check for check in assessment.checks if check.code == "cooldown_gate")
    assert cooldown.passed is True
    assert "显著新证据" in cooldown.detail


@pytest.mark.asyncio
async def test_inconsistent_governance_context_fails_closed(tmp_path: Path) -> None:
    assessment = assess_candidate_eligibility(
        await _candidate(tmp_path, repeats=2),
        governance=CandidateGovernanceContext(
            allowed=True,
            reason="cooldown_active",
            cooldown_until="2026-08-17T14:00:00+00:00",
        ),
    )

    assert assessment.review_ready is False
    cooldown = next(check for check in assessment.checks if check.code == "cooldown_gate")
    assert cooldown.passed is False
    assert "不一致" in cooldown.detail


@pytest.mark.asyncio
async def test_protected_scope_is_blocked_and_requires_human_governance(
    tmp_path: Path,
) -> None:
    assessment = assess_candidate_eligibility(
        await _candidate(tmp_path, scope="safety:permissions", repeats=2),
    )

    assert assessment.decision == "blocked"
    assert assessment.human_review_required is True
    protected = next(check for check in assessment.checks if check.code == "protected_scope")
    assert protected.passed is False
    assert protected.hard_block is True
    assert all(
        not check.hard_block
        for check in assessment.checks
        if check.code in {"evidence_strength", "cooldown_gate", "experiment_contract"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        "src/naumi_agent/safety/permissions.py:PermissionChecker",
        "src/naumi_agent/config/credentials.py:CredentialStore",
        "src/naumi_agent/persistence/migrations.py:MigrationRegistry",
    ],
)
async def test_authority_bearing_source_scopes_are_protected(
    tmp_path: Path,
    scope: str,
) -> None:
    assessment = assess_candidate_eligibility(
        await _candidate(tmp_path, scope=scope, repeats=2),
    )

    assert assessment.decision == "blocked"
    assert assessment.human_review_required is True


def test_single_mechanical_finding_is_review_ready_and_high_risk_requires_human(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text('api_key = "secret-never-render"\n', encoding="utf-8")
    evidence = adapt_self_review_static_evidence(
        scan_self_review_files([source], workspace_root=tmp_path),
    )
    assessment = assess_candidate_eligibility(build_candidate_draft(evidence))

    assert assessment.decision == "review_ready"
    assert assessment.human_review_required is True
    assert "secret-never-render" not in repr(assessment)


@pytest.mark.asyncio
async def test_agent_interpretation_alone_needs_direct_or_mechanical_evidence(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    result = await FeedbackIntakeService(store).ingest(
        tmp_path,
        build_agent_interpreted_feedback(
            FeedbackSourceEnvelope(
                run_id="run-agent-only",
                user_message_id="msg-agent-only",
                content_sha256="a" * 64,
                observed_at=NOW.isoformat(),
            ),
            category="defect",
            scope="ui:footer",
            topic="truncation",
            summary="Agent 认为用户报告了底栏截断",
        ),
    )
    stored = await store.get_candidate(tmp_path, result.candidate_id)
    assert stored is not None

    assessment = assess_candidate_eligibility(stored.draft)

    assert assessment.decision == "needs_evidence"
    evidence = next(check for check in assessment.checks if check.code == "evidence_strength")
    assert "仅有 Agent 解释反馈" in evidence.detail


def test_eligibility_rejects_non_candidate_input() -> None:
    with pytest.raises(TypeError, match="EvolutionCandidateDraft"):
        assess_candidate_eligibility(object())  # type: ignore[arg-type]
