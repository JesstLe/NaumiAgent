from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.capability_proposal import (
    EvolutionCapabilityProposal,
    generate_capability_proposal,
)
from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.prioritization import (
    CandidatePrioritySubject,
    prioritize_candidates,
)
from naumi_agent.evolution.review import EvolutionReviewService, render_evolution_review
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.evolution.tool_catalog_miss_opportunities import (
    EvolutionToolCatalogMissOpportunityService,
    ToolCatalogMissStore,
)
from naumi_agent.tools.base import ToolRegistry
from naumi_agent.tools.evolution_review import EvolutionCandidatesTool
from naumi_agent.tools.search import ToolSearchTool
from naumi_agent.ui.evolution_review import evolution_review_payload
from naumi_agent.workbench.proposal_governance import ProposalCooldownDecision

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _candidate(*, source_kind: str, scope: str, finding: str):
    uri = "tool-search://misses/tsm_" + "a" * 24
    if source_kind == "goal_need":
        uri = "goal://needs/goal-1"
    root = _digest(f"{finding}:{scope}")
    evidence = EvolutionEvidence(
        evidence_id=f"eve_{_digest(uri)[:24]}",
        source_kind=source_kind,
        source_uri=uri,
        observed_at=NOW.isoformat(),
        finding_code=finding,
        scope=scope,
        root_fingerprint=root,
        refs=(EvolutionEvidenceRef(uri=uri, sha256=_digest(uri)),),
    )
    return build_candidate_draft((evidence,))


def _contexts(candidate):
    governance = CandidateGovernanceContext(
        allowed=True,
        reason="no_active_cooldown",
    )
    eligibility = assess_candidate_eligibility(
        candidate,
        governance=governance,
        source_authority_valid=True,
    )
    portfolio = prioritize_candidates(
        (
            CandidatePrioritySubject(
                candidate=candidate,
                eligibility=eligibility,
            ),
        )
    )
    return governance, eligibility, portfolio


@pytest.mark.asyncio
async def test_exact_tool_miss_produces_stable_fail_closed_contract(tmp_path: Path) -> None:
    candidate = _candidate(
        source_kind="tool_catalog_miss",
        scope="capability:tool:browser.trace_compare",
        finding="missing_tool_capability",
    )
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    stored = await store.upsert_candidate(tmp_path, candidate)
    _, eligibility, portfolio = _contexts(candidate)
    priority = portfolio.priorities[0]

    first = generate_capability_proposal(
        stored,
        eligibility=eligibility,
        priority=priority,
        portfolio=portfolio,
    )
    second = generate_capability_proposal(
        stored,
        eligibility=eligibility,
        priority=priority,
        portfolio=portfolio,
    )

    assert first == second
    assert first is not None
    assert first.proposal_id.startswith("evcp_")
    assert first.interface.requested_name == "browser.trace_compare"
    assert first.interface.parameters_schema_status == "unresolved"
    assert first.permissions.granted_families == ()
    assert first.lifecycle.sandbox_eligible is False
    assert first.lifecycle.executable is False
    assert "api.parameters_schema" in first.unresolved_requirements
    assert first.verification.source_metrics == (
        "tool.catalog.requested_capability.availability:increase:1:tool_catalog_presence",
    )


@pytest.mark.asyncio
async def test_goal_need_never_reconstructs_private_objective_or_tool_name(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        source_kind="goal_need",
        scope="capability:need:0123456789abcdef",
        finding="user_explicit_need",
    )
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    stored = await store.upsert_candidate(tmp_path, candidate)
    _, eligibility, portfolio = _contexts(candidate)

    proposal = generate_capability_proposal(
        stored,
        eligibility=eligibility,
        priority=portfolio.priorities[0],
        portfolio=portfolio,
    )

    assert proposal is not None
    assert proposal.interface.requested_name is None
    assert proposal.interface.name_status == "unresolved"
    assert proposal.unresolved_requirements[0] == "api.tool_name"
    assert "objective" not in proposal.model_dump_json()


@pytest.mark.asyncio
async def test_authority_cooldown_priority_and_digest_fail_closed(tmp_path: Path) -> None:
    candidate = _candidate(
        source_kind="tool_catalog_miss",
        scope="capability:tool:browser.trace_compare",
        finding="missing_tool_capability",
    )
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    stored = await store.upsert_candidate(tmp_path, candidate)
    _, eligibility, portfolio = _contexts(candidate)
    priority = portfolio.priorities[0]

    revoked = assess_candidate_eligibility(
        candidate,
        governance=CandidateGovernanceContext(allowed=True, reason="no_active_cooldown"),
        source_authority_valid=False,
    )
    assert (
        generate_capability_proposal(
            stored,
            eligibility=revoked,
            priority=priority,
            portfolio=portfolio,
        )
        is None
    )
    assert (
        generate_capability_proposal(
            stored,
            eligibility=eligibility,
            priority=replace(priority, rankable=False, rank=None),
            portfolio=portfolio,
        )
        is None
    )
    with pytest.raises(ValueError, match="摘要"):
        generate_capability_proposal(
            replace(stored, draft_sha256="0" * 64),
            eligibility=eligibility,
            priority=priority,
            portfolio=portfolio,
        )


def test_capability_proposal_rejects_authority_escalation() -> None:
    with pytest.raises(ValidationError):
        EvolutionCapabilityProposal.model_validate(
            {
                "schema_version": 1,
                "proposal_id": "evcp_" + "0" * 24,
                "generator_version": "evolution-capability-proposal-v1",
                "status": "needs_specification",
                "title": "invalid",
                "summary": "invalid",
                "impact_scope": "capability:tool:test",
                "source": {},
                "interface": {},
                "permissions": {"bypass_grants_execution": True},
                "data": {},
                "verification": {},
                "operations": {},
                "lifecycle": {"executable": True},
                "unresolved_requirements": ["api.parameters_schema"],
                "requires_human_review": True,
            }
        )


class _Authority:
    async def validate_candidate_sources(self, _candidate) -> bool:
        return True


class _Governance:
    async def evaluate_source_cooldowns(self, sources):
        return {
            candidate_id: (
                None,
                ProposalCooldownDecision(
                    allowed=True,
                    reason="no_active_cooldown",
                    cooldown_until="",
                    significant_new_evidence=False,
                    policy_version="proposal-governance-v1",
                ),
            )
            for candidate_id, _revision, _count, _risk in sources
        }


@pytest.mark.asyncio
async def test_shared_review_service_exposes_same_contract_to_text_and_typed_ui(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        source_kind="tool_catalog_miss",
        scope="capability:tool:browser.trace_compare",
        finding="missing_tool_capability",
    )
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    stored = await store.upsert_candidate(tmp_path, candidate)
    service = EvolutionReviewService(
        store,
        governance_reader=_Governance(),
        source_authority_reader=_Authority(),
    )

    snapshot = await service.detail_snapshot(tmp_path, stored.draft.candidate_id)
    payload = evolution_review_payload(snapshot)
    text = render_evolution_review(snapshot)

    proposal = snapshot.selected.capability_proposal  # type: ignore[union-attr]
    assert proposal is not None
    assert payload["selected"]["capability_proposal"]["proposal_id"] == proposal.proposal_id  # type: ignore[index]
    assert proposal.proposal_id in text
    assert "可注册：否" in text
    assert payload["read_only"] is True


@pytest.mark.asyncio
async def test_real_exact_search_to_capability_proposal_e2e(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state" / "evolution.db"
    candidate_store = EvolutionCandidateStore(db_path)
    miss_store = ToolCatalogMissStore(db_path)
    registry = ToolRegistry()
    search = ToolSearchTool(
        registry,
        miss_store=miss_store,
        workspace_root=workspace,
    )
    registry.register(search)
    authority = EvolutionToolCatalogMissOpportunityService(
        workspace_root=workspace,
        miss_store=miss_store,
        tool_catalog=registry,
        candidate_store=candidate_store,
    )

    search_result = await search.execute(query="select:browser_trace_compare")
    match = re.search(r"`(tsm_[0-9a-f]{24})`", search_result)
    assert match is not None
    discovered = await authority.discover(miss_id=match.group(1))
    review = EvolutionReviewService(
        candidate_store,
        governance_reader=_Governance(),
        source_authority_reader=authority,
    )
    snapshot = await review.detail_snapshot(workspace, discovered.candidate_id)

    assert snapshot.selected is not None
    assert snapshot.selected.capability_proposal is not None
    assert snapshot.selected.capability_proposal.interface.requested_name == "browser_trace_compare"
    assert snapshot.selected.capability_proposal.lifecycle.executable is False
    agent_output = await EvolutionCandidatesTool(
        SimpleNamespace(workspace_root=workspace),
        review,
    ).execute(action="detail", candidate_id=discovered.candidate_id)
    assert snapshot.selected.capability_proposal.proposal_id in agent_output
