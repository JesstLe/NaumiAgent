from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.opportunity_discovery import (
    EvolutionOutcomeOpportunityError,
    EvolutionOutcomeOpportunityService,
    adapt_rollback_outcome_evidence,
    adapt_stable_promotion_outcome_evidence,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
)
from naumi_agent.evolution.review import EvolutionReviewFilter, EvolutionReviewService
from naumi_agent.evolution.source_authority import (
    EvolutionCandidateSourceAuthorityRouter,
)
from naumi_agent.evolution.stable_promotion_outcomes import (
    EvolutionStablePromotionOutcome,
    EvolutionStablePromotionOutcomeError,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.evolution_review import EvolutionOutcomeOpportunityTool
from naumi_agent.workbench.models import RiskLevel
from naumi_agent.workbench.proposal_governance import ProposalCooldownDecision


class _OutcomeService:
    def __init__(self, outcomes: tuple[EvolutionRevalidationRollbackOutcome, ...]) -> None:
        self.outcomes = {item.outcome_id: item for item in outcomes}
        self.valid = True

    async def inspect_outcome(self, *, outcome_id: str):
        outcome = self.outcomes.get(outcome_id)
        if outcome is None:
            raise EvolutionRevalidationRollbackOutcomeError(
                "rollback_outcome_not_found",
                "Rollback Outcome 不存在。",
            )
        return SimpleNamespace(
            outcome=outcome,
            outcome_authority=self.valid,
        )


class _StableOutcomeService:
    def __init__(self, outcomes: tuple[EvolutionStablePromotionOutcome, ...]) -> None:
        self.outcomes = {item.outcome_id: item for item in outcomes}
        self.valid = True

    async def inspect(self, *, outcome_id: str):
        outcome = self.outcomes.get(outcome_id)
        if outcome is None:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_missing",
                "Stable Promotion Outcome 不存在。",
            )
        return SimpleNamespace(
            outcome=outcome,
            promoted_outcome_authority=self.valid,
        )


class _GovernanceReader:
    async def evaluate_source_cooldowns(
        self,
        sources: list[tuple[str, int, int, RiskLevel]],
    ):
        return {
            candidate_id: (
                None,
                ProposalCooldownDecision(
                    allowed=True,
                    reason="no_active_cooldown",
                    cooldown_until="",
                    significant_new_evidence=False,
                ),
            )
            for candidate_id, _revision, _count, _risk in sources
        }


def _authority_router(
    service: EvolutionOutcomeOpportunityService,
) -> EvolutionCandidateSourceAuthorityRouter:
    return EvolutionCandidateSourceAuthorityRouter({
        "eval_metric_regression": service,
        "goal_need": service,
        "promoted_outcome": service,
        "rollback_outcome": service,
        "tool_catalog_miss": service,
    })


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _outcome(
    root: Path,
    *,
    marker: str,
    recorded_at: datetime,
    breach_reasons: tuple[str, ...] = ("error_rate",),
) -> EvolutionRevalidationRollbackOutcome:
    token = hashlib.sha256(marker.encode()).hexdigest()
    core = {
        "schema_version": 1,
        "policy_version": "evolution-revalidation-rollback-outcome-v1",
        "workspace_root": str(root.resolve()),
        "status": "rolled_back",
        "rollback_receipt_id": f"evrerollbackexec_{token[:24]}",
        "rollback_receipt_sha256": token,
        "request_id": f"evrerollbackreq_{token[1:25]}",
        "request_sha256": hashlib.sha256(f"request:{marker}".encode()).hexdigest(),
        "plan_id": f"evrerolloutplan_{token[2:26]}",
        "plan_sha256": hashlib.sha256(f"plan:{marker}".encode()).hexdigest(),
        "promotion_input_id": f"evrevalpromoin_{token[3:27]}",
        "promotion_input_sha256": hashlib.sha256(
            f"promotion:{marker}".encode()
        ).hexdigest(),
        "runtime_contract_id": f"evrevalruntime_{token[4:28]}",
        "runtime_contract_sha256": hashlib.sha256(
            f"runtime:{marker}".encode()
        ).hexdigest(),
        "prior_input_id": f"evpromoin_{token[5:29]}",
        "prior_input_sha256": hashlib.sha256(f"prior:{marker}".encode()).hexdigest(),
        "experiment_contract_id": f"evx_{token[6:30]}",
        "experiment_contract_sha256": hashlib.sha256(
            f"contract:{marker}".encode()
        ).hexdigest(),
        "experiment_authority_id": f"evxauth_{token[7:31]}",
        "experiment_authority_sha256": hashlib.sha256(
            f"authority:{marker}".encode()
        ).hexdigest(),
        "workbench_session_id": f"session-{marker}",
        "workbench_proposal_id": f"proposal-{marker}",
        "proposal_id": f"evp_{token[8:32]}",
        "proposal_kind": "code",
        "candidate_id": f"evc_{token[9:33]}",
        "candidate_revision": 1,
        "candidate_sha256": hashlib.sha256(f"candidate:{marker}".encode()).hexdigest(),
        "candidate_slot_id": f"candidate-slot-{marker}",
        "candidate_slot_sha256": hashlib.sha256(
            f"candidate-slot:{marker}".encode()
        ).hexdigest(),
        "baseline_slot_id": f"baseline-slot-{marker}",
        "baseline_slot_sha256": hashlib.sha256(
            f"baseline-slot:{marker}".encode()
        ).hexdigest(),
        "breach_reasons": list(breach_reasons),
        "rollback_fact_verified": True,
        "proposal_binding_verified": True,
        "outcome_recorded": True,
        "promoted": False,
        "superseded": False,
        "long_term_metrics_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "recorded_at": recorded_at.isoformat(),
    }
    digest = _digest(core)
    return EvolutionRevalidationRollbackOutcome.model_validate({
        **core,
        "outcome_id": f"evrerollbackout_{digest[:24]}",
        "outcome_sha256": digest,
    })


def _stable_outcome(
    root: Path,
    *,
    marker: str,
    promoted_at: datetime,
) -> EvolutionStablePromotionOutcome:
    token = hashlib.sha256(marker.encode()).hexdigest()
    core = {
        "schema_version": 1,
        "policy_version": "evolution-stable-promotion-outcome-v1",
        "workspace_root": str(root.resolve()),
        "status": "promoted",
        "sequence": 1,
        "previous_outcome_id": "",
        "previous_outcome_sha256": "",
        "decision_id": f"evstablepromdecision_{token[:24]}",
        "decision_sha256": hashlib.sha256(f"decision:{marker}".encode()).hexdigest(),
        "eligibility_id": f"evstablepromeligible_{token[1:25]}",
        "eligibility_sha256": hashlib.sha256(
            f"eligibility:{marker}".encode()
        ).hexdigest(),
        "contract_id": f"evstablepromobserve_{token[2:26]}",
        "population_assessment_id": f"evstableprompopobserve_{token[3:27]}",
        "population_denominator": 2,
        "passing_count": 2,
        "assessment_coverage_bps": 10_000,
        "duration_coverage_bps": 10_000,
        "workbench_session_id": f"session-{marker}",
        "workbench_proposal_id": f"proposal-{marker}",
        "proposal_id": f"evp_{token[4:28]}",
        "candidate_id": f"evc_{token[5:29]}",
        "candidate_revision": 3,
        "candidate_sha256": hashlib.sha256(f"candidate:{marker}".encode()).hexdigest(),
        "candidate_version": "v3",
        "candidate_target": "src/naumi_agent/model/router.py",
        "promoted_at": promoted_at.isoformat(),
        "post_observation_decision_verified": True,
        "population_sustained_health_verified": True,
        "promoted": True,
        "superseded": False,
        "long_term_metrics_recorded": True,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionOutcome.model_validate({
        **core,
        "outcome_id": f"evstablepromout_{digest[:24]}",
        "outcome_sha256": digest,
    })


@pytest.mark.asyncio
async def test_discovery_is_concurrent_idempotent_and_privacy_bounded(
    tmp_path: Path,
) -> None:
    outcome = _outcome(tmp_path, marker="one", recorded_at=datetime.now(UTC))
    source = _OutcomeService((outcome,))
    store = EvolutionCandidateStore(tmp_path / "candidate.db")
    service = EvolutionOutcomeOpportunityService(
        workspace_root=tmp_path,
        outcome_service=source,  # type: ignore[arg-type]
        stable_outcome_service=_StableOutcomeService(()),  # type: ignore[arg-type]
        candidate_store=store,
    )

    results = await asyncio.gather(
        *(service.discover(outcome_id=outcome.outcome_id) for _ in range(8))
    )

    assert all(result == results[0] for result in results)
    assert results[0].schema_version == 2
    assert results[0].policy_version == "evolution-outcome-opportunity-v2"
    assert results[0].outcome_kind == "rollback"
    assert results[0].occurrence_count == 1
    assert results[0].candidate_revision == 1
    stored = await store.get_candidate(tmp_path, results[0].candidate_id)
    assert stored is not None
    evidence = stored.draft.evidence[0]
    assert evidence.source_kind == "rollback_outcome"
    assert evidence.refs[0].sha256 == outcome.outcome_sha256
    encoded = evidence.model_dump_json()
    assert outcome.workbench_proposal_id not in encoded
    assert outcome.candidate_slot_id not in encoded
    assert str(tmp_path) not in encoded


@pytest.mark.asyncio
async def test_discovery_clusters_same_guardrail_root_and_revalidates_review(
    tmp_path: Path,
) -> None:
    start = datetime.now(UTC)
    first = _outcome(tmp_path, marker="one", recorded_at=start)
    second = _outcome(
        tmp_path,
        marker="two",
        recorded_at=start + timedelta(seconds=1),
    )
    source = _OutcomeService((first, second))
    store = EvolutionCandidateStore(tmp_path / "candidate.db")
    service = EvolutionOutcomeOpportunityService(
        workspace_root=tmp_path,
        outcome_service=source,  # type: ignore[arg-type]
        stable_outcome_service=_StableOutcomeService(()),  # type: ignore[arg-type]
        candidate_store=store,
    )

    one = await service.discover(outcome_id=first.outcome_id)
    two = await service.discover(outcome_id=second.outcome_id)

    assert two.candidate_id == one.candidate_id
    assert two.candidate_revision == 2
    assert two.occurrence_count == 2
    review = EvolutionReviewService(
        store,
        governance_reader=_GovernanceReader(),
        source_authority_reader=_authority_router(service),
    )
    current = await review.detail_snapshot(tmp_path, one.candidate_id)
    assert current.selected is not None
    assert current.selected.eligibility.review_ready
    assert current.selected.proposal is not None

    source.valid = False
    stale = await review.detail_snapshot(tmp_path, one.candidate_id)
    assert stale.selected is not None
    assert stale.selected.eligibility.decision == "blocked"
    assert stale.selected.proposal is None
    authority = next(
        check
        for check in stale.selected.eligibility.checks
        if check.code == "source_authority"
    )
    assert not authority.passed
    assert authority.hard_block


@pytest.mark.asyncio
async def test_promoted_outcome_starts_new_candidate_and_revalidates_authority(
    tmp_path: Path,
) -> None:
    outcome = _stable_outcome(
        tmp_path,
        marker="promoted-one",
        promoted_at=datetime.now(UTC),
    )
    stable_source = _StableOutcomeService((outcome,))
    store = EvolutionCandidateStore(tmp_path / "candidate.db")
    service = EvolutionOutcomeOpportunityService(
        workspace_root=tmp_path,
        outcome_service=_OutcomeService(()),  # type: ignore[arg-type]
        stable_outcome_service=stable_source,  # type: ignore[arg-type]
        candidate_store=store,
    )

    results = await asyncio.gather(
        *(service.discover(outcome_id=outcome.outcome_id) for _ in range(8))
    )

    assert all(result == results[0] for result in results)
    result = results[0]
    assert result.outcome_kind == "promoted"
    assert result.candidate_id != outcome.candidate_id
    assert result.candidate_revision == 1
    assert result.occurrence_count == 1
    stored = await store.get_candidate(tmp_path, result.candidate_id)
    assert stored is not None
    evidence = stored.draft.evidence[0]
    assert evidence.source_kind == "promoted_outcome"
    assert evidence.refs[0].sha256 == outcome.outcome_sha256
    assert stored.draft.finding_code == "stable_promotion_improvement"
    assert stored.draft.expected_metrics[0].name == (
        "harness.stable_promotion_improvement.regression_rate"
    )
    encoded = evidence.model_dump_json()
    assert outcome.candidate_target not in encoded
    assert outcome.workbench_proposal_id not in encoded
    assert str(tmp_path) not in encoded
    assert await service.validate_candidate_sources(stored.draft)
    review = EvolutionReviewService(
        store,
        governance_reader=_GovernanceReader(),
        source_authority_reader=_authority_router(service),
    )
    filtered = await review.list_snapshot(
        tmp_path,
        filters=EvolutionReviewFilter(source_kind="promoted_outcome"),
    )
    assert [item.candidate_id for item in filtered.items] == [result.candidate_id]
    assert filtered.items[0].source_kinds == ("promoted_outcome",)

    stable_source.valid = False
    assert not await service.validate_candidate_sources(stored.draft)
    with pytest.raises(EvolutionOutcomeOpportunityError) as stale:
        await service.discover(outcome_id=outcome.outcome_id)
    assert stale.value.code == "outcome_opportunity_source_stale"


def test_adapter_rejects_non_outcome_and_service_rejects_bad_id(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Rollback Outcome"):
        adapt_rollback_outcome_evidence(SimpleNamespace())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Stable Promotion Outcome"):
        adapt_stable_promotion_outcome_evidence(SimpleNamespace())  # type: ignore[arg-type]

    service = EvolutionOutcomeOpportunityService(
        workspace_root=tmp_path,
        outcome_service=_OutcomeService(()),  # type: ignore[arg-type]
        stable_outcome_service=_StableOutcomeService(()),  # type: ignore[arg-type]
        candidate_store=EvolutionCandidateStore(tmp_path / "candidate.db"),
    )
    with pytest.raises(EvolutionOutcomeOpportunityError) as invalid:
        asyncio.run(service.discover(outcome_id="../forged"))
    assert invalid.value.code == "outcome_opportunity_id_invalid"


@pytest.mark.asyncio
async def test_engine_binds_both_outcome_authorities(tmp_path: Path) -> None:
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "vectors"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        service = engine.evolution_outcome_opportunity_service
        assert service.outcome_service is (
            engine.evolution_revalidation_rollback_outcome_service
        )
        assert service.stable_outcome_service is (
            engine.evolution_stable_promotion_outcome_service
        )
        metric_service = engine.evolution_eval_metric_opportunity_service
        assert metric_service.harness_store is engine._harness_store
        assert metric_service.candidate_store is engine.evolution_candidate_store
        goal_service = engine.evolution_goal_need_opportunity_service
        assert goal_service.goal_store is engine.goal_store
        assert goal_service.candidate_store is engine.evolution_candidate_store
        catalog_service = engine.evolution_tool_catalog_miss_opportunity_service
        assert catalog_service.miss_store is engine.tool_catalog_miss_store
        assert catalog_service.tool_catalog is engine.tool_registry
        assert catalog_service.candidate_store is engine.evolution_candidate_store
        router = engine.evolution_candidate_source_authority_router
        assert router.source_kinds == (
            "eval_metric_regression",
            "goal_need",
            "promoted_outcome",
            "rollback_outcome",
            "tool_catalog_miss",
        )
        assert engine.evolution_review_service._source_authority_reader is router
    finally:
        await engine.shutdown()


def test_tool_schema_accepts_both_outcome_ids() -> None:
    tool = EvolutionOutcomeOpportunityTool(SimpleNamespace())

    assert tool.parameters_schema["properties"]["outcome_id"]["pattern"] == (
        "^(?:evrerollbackout|evstablepromout)_[0-9a-f]{24}$"
    )
    assert "stable promoted" in tool.description
