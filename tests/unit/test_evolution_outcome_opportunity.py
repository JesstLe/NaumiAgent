from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.opportunity_discovery import (
    EvolutionOutcomeOpportunityError,
    EvolutionOutcomeOpportunityService,
    adapt_rollback_outcome_evidence,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
)
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
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
        candidate_store=store,
    )

    results = await asyncio.gather(
        *(service.discover(outcome_id=outcome.outcome_id) for _ in range(8))
    )

    assert all(result == results[0] for result in results)
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
        source_authority_reader=service,
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


def test_adapter_rejects_non_outcome_and_service_rejects_bad_id(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Rollback Outcome"):
        adapt_rollback_outcome_evidence(SimpleNamespace())  # type: ignore[arg-type]

    service = EvolutionOutcomeOpportunityService(
        workspace_root=tmp_path,
        outcome_service=_OutcomeService(()),  # type: ignore[arg-type]
        candidate_store=EvolutionCandidateStore(tmp_path / "candidate.db"),
    )
    with pytest.raises(EvolutionOutcomeOpportunityError) as invalid:
        asyncio.run(service.discover(outcome_id="../forged"))
    assert invalid.value.code == "outcome_opportunity_id_invalid"
