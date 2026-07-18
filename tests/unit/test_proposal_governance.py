from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import (
    ProposalSourceKind,
    ProposalState,
    RiskLevel,
    WorkbenchProposal,
)
from naumi_agent.workbench.proposal_governance import (
    GOVERNANCE_POLICY_VERSION,
    ProposalAction,
    ProposalGovernanceConflictError,
    evaluate_proposal_cooldown,
    plan_proposal_transition,
    validate_merge_target,
)
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore

NOW = datetime(2026, 7, 19, 1, 0, tzinfo=UTC)


def _proposal(**changes) -> WorkbenchProposal:
    values = {
        "id": "proposal-1",
        "session_id": "session-1",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "agent_id": "Evolution-Agent",
        "title": "改进 footer",
        "impact_scope": "src/ui/footer.py",
        "risk_level": RiskLevel.MEDIUM,
        "source_kind": ProposalSourceKind.EVOLUTION_CANDIDATE,
        "source_id": "evc_" + "1" * 24,
        "source_revision": 2,
        "source_occurrence_count": 4,
        "source_sha256": "2" * 64,
        "source_proposal_id": "evp_" + "3" * 24,
        "generator_version": "evolution-proposal-v1",
        "proposal_kind": "code",
        "idempotency_key": "evolution:evp_" + "3" * 24,
    }
    values.update(changes)
    return WorkbenchProposal(**values)


def test_reject_and_defer_plans_have_versioned_cooldown() -> None:
    proposal = _proposal()
    with pytest.raises(ValueError, match="必须填写原因"):
        plan_proposal_transition(proposal, action=ProposalAction.REJECT, now=NOW)

    rejected = plan_proposal_transition(
        proposal,
        action=ProposalAction.REJECT,
        now=NOW,
        decision_note="暂不接受该方向",
    )
    assert rejected.target_state is ProposalState.REJECTED
    assert rejected.cooldown_until == (NOW + timedelta(days=30)).isoformat(timespec="seconds")
    assert rejected.policy_version == GOVERNANCE_POLICY_VERSION

    deferred = plan_proposal_transition(
        proposal,
        action=ProposalAction.DEFER,
        now=NOW,
        decision_note="等待上游接口稳定",
        defer_until=(NOW + timedelta(days=7)).isoformat(),
    )
    assert deferred.target_state is ProposalState.DEFERRED
    assert deferred.cooldown_until == (NOW + timedelta(days=7)).isoformat(timespec="seconds")
    with pytest.raises(ValueError, match="1 小时至 90 天"):
        plan_proposal_transition(
            proposal,
            action=ProposalAction.DEFER,
            now=NOW,
            decision_note="过短",
            defer_until=(NOW + timedelta(minutes=30)).isoformat(),
        )


def test_cooldown_allows_expiry_or_significant_new_evidence_only() -> None:
    previous = _proposal(
        state=ProposalState.REJECTED,
        cooldown_until=(NOW + timedelta(days=30)).isoformat(),
    )
    blocked = evaluate_proposal_cooldown(
        previous,
        candidate_revision=3,
        occurrence_count=5,
        risk_level=RiskLevel.MEDIUM,
        now=NOW + timedelta(days=1),
    )
    assert blocked.allowed is False
    assert blocked.reason == "cooldown_active"

    growth = evaluate_proposal_cooldown(
        previous,
        candidate_revision=3,
        occurrence_count=6,
        risk_level=RiskLevel.MEDIUM,
        now=NOW + timedelta(days=1),
    )
    assert growth.allowed is True
    assert growth.significant_new_evidence is True

    escalated = evaluate_proposal_cooldown(
        previous,
        candidate_revision=3,
        occurrence_count=5,
        risk_level=RiskLevel.HIGH,
        now=NOW + timedelta(days=1),
    )
    assert escalated.allowed is True
    assert escalated.reason == "significant_new_evidence"

    expired = evaluate_proposal_cooldown(
        previous,
        candidate_revision=2,
        occurrence_count=4,
        risk_level=RiskLevel.MEDIUM,
        now=NOW + timedelta(days=31),
    )
    assert expired.allowed is True
    assert expired.reason == "cooldown_expired"

    legacy = evaluate_proposal_cooldown(
        _proposal(state=ProposalState.REJECTED, cooldown_until=""),
        candidate_revision=3,
        occurrence_count=10,
        risk_level=RiskLevel.CRITICAL,
        now=NOW,
    )
    assert legacy.allowed is False
    assert legacy.reason == "cooldown_record_missing"


def test_merge_target_must_be_same_candidate_and_not_older() -> None:
    source = _proposal(source_revision=2)
    target = _proposal(
        id="proposal-2",
        source_revision=3,
        source_proposal_id="evp_" + "4" * 24,
        idempotency_key="evolution:evp_" + "4" * 24,
    )
    validate_merge_target(source, target)
    with pytest.raises(ValueError, match="同一 Candidate"):
        validate_merge_target(source, _proposal(id="proposal-3", source_id="evc_" + "9" * 24))
    with pytest.raises(ValueError, match="较新"):
        validate_merge_target(source, _proposal(id="proposal-4", source_revision=1))


async def _stored_proposal(
    store: WorkbenchStore,
    *,
    revision: int,
    suffix: str,
) -> WorkbenchProposal:
    return await store.create_proposal(
        session_id="session-1",
        mission_id="mission-1",
        task_id=f"task-{revision}",
        agent_id="Evolution-Agent",
        title=f"Proposal revision {revision}",
        impact_scope="src/ui/footer.py",
        source_kind=ProposalSourceKind.EVOLUTION_CANDIDATE,
        source_id="evc_" + "1" * 24,
        source_revision=revision,
        source_occurrence_count=revision + 2,
        source_sha256=suffix * 64,
        source_proposal_id="evp_" + suffix * 24,
        generator_version="evolution-proposal-v1",
        proposal_kind="code",
        idempotency_key="evolution:evp_" + suffix * 24,
    )


@pytest.mark.asyncio
async def test_service_defer_is_concurrency_safe_and_audited_once(tmp_path) -> None:
    database = str(tmp_path / "governance.db")
    store = WorkbenchStore(database)
    service = WorkbenchService(task_store=TaskStore(database), workbench_store=store)
    proposal = await _stored_proposal(store, revision=2, suffix="2")
    until = (NOW + timedelta(days=7)).isoformat()

    results = await asyncio.gather(
        *(
            service.govern_proposal(
                "session-1",
                proposal.id,
                action=ProposalAction.DEFER,
                reviewer="Human",
                decision_note="等待稳定窗口",
                defer_until=until,
                now=NOW,
            )
            for _ in range(8)
        )
    )

    assert {result["state"] for result in results if result} == {"deferred"}
    assert {result["cooldown_until"] for result in results if result} == {until}
    events = await store.list_events("session-1", event_type="proposal.deferred")
    assert len(events) == 1
    with pytest.raises(ValueError, match="冷却期尚未结束"):
        await service.reopen_proposal(
            "session-1", proposal.id, actor="Human", now=NOW + timedelta(days=1)
        )
    reopened = await service.reopen_proposal(
        "session-1", proposal.id, actor="Human", now=NOW + timedelta(days=8)
    )
    assert reopened is not None and reopened["state"] == "open"
    assert len(await store.list_events("session-1", event_type="proposal.reopened")) == 1


@pytest.mark.asyncio
async def test_conflicting_concurrent_decisions_have_one_winner_and_one_event(
    tmp_path,
) -> None:
    database = str(tmp_path / "conflict.db")
    store = WorkbenchStore(database)
    service = WorkbenchService(task_store=TaskStore(database), workbench_store=store)
    proposal = await _stored_proposal(store, revision=2, suffix="2")

    results = await asyncio.gather(
        service.govern_proposal(
            "session-1",
            proposal.id,
            action=ProposalAction.APPROVE,
            reviewer="Reviewer-A",
            now=NOW,
        ),
        service.govern_proposal(
            "session-1",
            proposal.id,
            action=ProposalAction.REJECT,
            reviewer="Reviewer-B",
            decision_note="风险仍然过高",
            now=NOW,
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [
        result
        for result in results
        if isinstance(result, ProposalGovernanceConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0]["state"] in {"approved", "rejected"}
    events = await store.list_events("session-1")
    decision_events = [event for event in events if event.type.startswith("proposal.")]
    assert len(decision_events) == 1
    assert decision_events[0].type == f"proposal.{successes[0]['state']}"


@pytest.mark.asyncio
async def test_service_merge_preserves_open_target_and_audits_source(tmp_path) -> None:
    database = str(tmp_path / "merge.db")
    store = WorkbenchStore(database)
    service = WorkbenchService(task_store=TaskStore(database), workbench_store=store)
    source = await _stored_proposal(store, revision=2, suffix="2")
    target = await _stored_proposal(store, revision=3, suffix="3")

    merged = await service.govern_proposal(
        "session-1",
        source.id,
        action=ProposalAction.MERGE,
        reviewer="Human",
        decision_note="同根因合并",
        merge_into_id=target.id,
        now=NOW,
    )

    assert merged is not None
    assert merged["state"] == "merged"
    assert merged["merged_into_id"] == target.id
    assert (await store.get_proposal("session-1", target.id)).state is ProposalState.OPEN
    events = await store.list_events("session-1", event_type="proposal.merged")
    assert len(events) == 1
