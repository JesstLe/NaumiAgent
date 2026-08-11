from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.proposal_outcomes import (
    EvolutionProposalOutcomeProjectionService,
)
from naumi_agent.evolution.stable_promotion_outcome_decisions import (
    EvolutionStablePromotionOutcomeDecisionAction,
    EvolutionStablePromotionOutcomeDecisionError,
    EvolutionStablePromotionOutcomeDecisionService,
    EvolutionStablePromotionOutcomeDecisionStore,
    _decision_request,
    _interaction_id,
    build_stable_promotion_outcome_decision,
)
from naumi_agent.evolution.stable_promotion_outcome_eligibilities import (
    EvolutionStablePromotionOutcomeEligibilityError,
    EvolutionStablePromotionOutcomeEligibilityService,
    EvolutionStablePromotionOutcomeEligibilityStore,
)
from naumi_agent.evolution.stable_promotion_outcomes import (
    EvolutionStablePromotionOutcomeError,
    EvolutionStablePromotionOutcomeService,
    EvolutionStablePromotionOutcomeStore,
    build_stable_promotion_outcome_pair,
)
from naumi_agent.evolution.stable_promotion_population_observation_assessments import (
    EvolutionStablePromotionPopulationMemberObservationStatus,
    EvolutionStablePromotionPopulationObservationAssessmentView,
    EvolutionStablePromotionPopulationObservationMember,
    build_stable_promotion_population_observation_assessment,
)
from naumi_agent.harness.interaction import answer_interaction, new_interaction_record
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionOutcomeDecisionTool,
    EvolutionStablePromotionOutcomeEligibilityTool,
    EvolutionStablePromotionOutcomeTool,
)
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_stable_promotion_population_observation_assessments import (
    _contract_for_finalization,
    _digest,
    _real_population_receipt,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)


class _PopulationAssessmentPort:
    def __init__(self, store, assessment) -> None:
        self.store = store
        self.assessment = assessment
        self.assessments = {assessment.assessment_id: assessment}
        self.authority = True

    async def inspect(self, *, assessment_id: str):
        assessment = self.assessments[assessment_id]
        current = (
            assessment
            if self.authority and assessment_id == self.assessment.assessment_id
            else None
        )
        current_authority = current is not None
        return EvolutionStablePromotionPopulationObservationAssessmentView(
            receipt=assessment,
            current_assessment=current,
            durable_receipt_valid=True,
            latest_receipt_current=current_authority,
            contract_authority=True,
            population_finalization_authority=True,
            member_source_set_current=True,
            assessment_temporally_current=current_authority,
            invalidation_reasons=(() if current_authority else ("population_assessment_expired",)),
            population_long_term_observation_authority=current_authority,
            population_health_alert_authority=False,
        )


def _passing_members(finalization, assessed_at):
    valid_until = (assessed_at + timedelta(minutes=1)).isoformat()
    members = []
    for index, member in enumerate(finalization.members, start=1):
        char = format(index, "x")
        core = {
            "schema_version": 1,
            "installation_member_id": member.installation_member_id,
            "installation_credential_id": member.installation_credential_id,
            "finalization_member_source_sha256": member.member_source_sha256,
            "admission_id": f"evstablepromadmit_{char * 24}",
            "admission_sha256": _digest([char, "admission"]),
            "assessment_id": f"evstableprominstallobserve_{char * 24}",
            "assessment_sha256": _digest([char, "assessment"]),
            "status": EvolutionStablePromotionPopulationMemberObservationStatus.PASSING,
            "reason": "",
            "sample_count": 12,
            "operational_sample_count": 12,
            "observation_seconds": 3600,
            "minimum_observation_seconds": 3600,
            "last_observed_at": assessed_at.isoformat(),
            "valid_until": valid_until,
            "assessment_current_at_issue": True,
        }
        members.append(
            EvolutionStablePromotionPopulationObservationMember.model_validate(
                {**core, "source_sha256": _digest(core)}
            )
        )
    return tuple(sorted(members, key=lambda item: item.installation_member_id))


@pytest.mark.asyncio
async def test_engine_composes_outcome_eligibility_service_and_tool(tmp_path) -> None:
    assert (
        evolution_api.EvolutionStablePromotionOutcomeEligibilityService
        is EvolutionStablePromotionOutcomeEligibilityService
    )
    assert (
        evolution_api.EvolutionStablePromotionOutcomeDecisionService
        is EvolutionStablePromotionOutcomeDecisionService
    )
    session_db = tmp_path / ".naumi" / "sessions.db"
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(session_db),
                vector_db_path=str(tmp_path / ".naumi" / "chroma"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        tool = engine.tool_registry.get("evolution_stable_promotion_outcome_eligibility")
        assert isinstance(tool, EvolutionStablePromotionOutcomeEligibilityTool)
        service = engine.evolution_stable_promotion_outcome_eligibility_service
        store = engine.evolution_stable_promotion_outcome_eligibility_store
        assert service.store is store
        assert store.db_path == session_db.resolve()
        assert service.population_assessment_service is (
            engine.evolution_stable_promotion_population_observation_assessment_service
        )
        decision_tool = engine.tool_registry.get("evolution_stable_promotion_outcome_decision")
        assert isinstance(decision_tool, EvolutionStablePromotionOutcomeDecisionTool)
        assert engine.evolution_stable_promotion_outcome_decision_service.store is (
            engine.evolution_stable_promotion_outcome_decision_store
        )
        outcome_tool = engine.tool_registry.get("evolution_stable_promotion_outcome")
        assert isinstance(outcome_tool, EvolutionStablePromotionOutcomeTool)
        assert engine.evolution_stable_promotion_outcome_service.store is (
            engine.evolution_stable_promotion_outcome_store
        )
        assert (
            engine.evolution_proposal_outcome_projection_service.stable_promotion_outcome_store
            is engine.evolution_stable_promotion_outcome_store
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_passing_population_creates_review_only_eligibility_and_revokes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    finalization = _real_population_receipt(data)
    contract = _contract_for_finalization(data, finalization)
    data.contract = contract
    data.contract_service.contract = contract
    data.contract_service.store.contract = contract
    assessed_at = data.runtime_now[0] + timedelta(seconds=3)
    assessment = build_stable_promotion_population_observation_assessment(
        workspace_root=tmp_path,
        contract=contract,
        finalization=finalization,
        members=_passing_members(finalization, assessed_at),
        assessed_at=assessed_at,
    )
    db_path = data.store.db_path
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM evolution_stable_promotion_observation_contracts")
        db.execute(
            "INSERT INTO evolution_stable_promotion_observation_contracts "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                contract.contract_id,
                contract.contract_sha256,
                contract.population_finalization_receipt_id,
                contract.population_completion_receipt_id,
                contract.rollout_plan_id,
                contract.experiment_contract_id,
                contract.model_dump_json(),
                contract.window_not_before_at,
            ),
        )
        db.execute(
            "CREATE TABLE evolution_stable_promotion_population_observation_assessments ("
            "assessment_id TEXT PRIMARY KEY, assessment_sha256 TEXT NOT NULL UNIQUE, "
            "source_set_sha256 TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL, "
            "population_finalization_receipt_id TEXT NOT NULL, status TEXT NOT NULL, "
            "assessment_json TEXT NOT NULL, assessed_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_promotion_population_observation_assessments "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assessment.assessment_id,
                assessment.assessment_sha256,
                assessment.source_set_sha256,
                assessment.contract_id,
                assessment.population_finalization_receipt_id,
                assessment.status.value,
                assessment.model_dump_json(),
                assessment.assessed_at,
            ),
        )

    assessment_store = SimpleNamespace(db_path=db_path.resolve())
    port = _PopulationAssessmentPort(assessment_store, assessment)

    def service():
        store = EvolutionStablePromotionOutcomeEligibilityStore(
            db_path,
            contract_store=data.contract_service.store,
            population_assessment_store=assessment_store,
        )
        return EvolutionStablePromotionOutcomeEligibilityService(
            workspace_root=tmp_path,
            contract_store=data.contract_service.store,
            contract_service=data.contract_service,
            population_assessment_service=port,
            store=store,
        )

    left, right = await asyncio.gather(
        service().record(population_assessment_id=assessment.assessment_id),
        service().record(population_assessment_id=assessment.assessment_id),
    )
    assert left == right
    assert left.current_eligibility == left.eligibility
    assert left.outcome_review_ready_authority
    assert left.eligibility.population_sustained_health_verified
    assert left.eligibility.independent_outcome_decision_required
    assert not left.promoted
    assert not left.outcome_decision_authority
    assert not left.learning_authority
    assert not left.promotion_authority
    assert not left.execution_authority
    tool = EvolutionStablePromotionOutcomeEligibilityTool(
        SimpleNamespace(evolution_stable_promotion_outcome_eligibility_service=service())
    )
    arguments = {
        "action": "inspect",
        "eligibility_id": left.eligibility.eligibility_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "Outcome 审批资格" in await tool.execute(**arguments)
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**registered.parse_arguments(call.arguments)),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-outcome-eligibility inspect "
        + left.eligibility.eligibility_id,
    )
    assert "Outcome 审批资格" in slash

    harness_store = HarnessStore(db_path)
    interaction_authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=tmp_path,
        owner_id="outcome-decision-test-owner",
    )
    answered_at = assessed_at + timedelta(seconds=10)
    callback_calls: list[str] = []
    callback_now = [answered_at]

    async def answer_promote(payload):
        current_answered_at = callback_now[0]
        request = normalize_interaction_request(payload)
        callback_calls.append(str(payload["_interaction_id"]))
        record = await interaction_authority.create(
            request=request,
            interaction_id=str(payload["_interaction_id"]),
            subject_kind=str(payload["_durable_subject_kind"]),
            subject_id=str(payload["_durable_subject_id"]),
            session_id="outcome-decision-session",
            agent_name="main",
            now=(current_answered_at - timedelta(seconds=1)).isoformat(),
        )
        _record, response = await interaction_authority.answer(
            record=record,
            response={"kind": "option", "value": "promote"},
            now=current_answered_at.isoformat(),
        )
        callback_now[0] = current_answered_at + timedelta(seconds=30)
        return response

    eligibility_service_for_decision = service()
    decision_store = EvolutionStablePromotionOutcomeDecisionStore(
        db_path,
        eligibility_store=eligibility_service_for_decision.store,
        interaction_store=harness_store,
    )
    decision_service = EvolutionStablePromotionOutcomeDecisionService(
        workspace_root=tmp_path,
        eligibility_service=eligibility_service_for_decision,
        interaction_store=harness_store,
        store=decision_store,
        request_user_input=answer_promote,
    )
    decision_view = await decision_service.decide(eligibility_id=left.eligibility.eligibility_id)
    assert callback_calls == [
        "ask-evstablepromdecision-"
        + left.eligibility.eligibility_id.removeprefix("evstablepromeligible_")
        + "-1"
    ]
    assert decision_view.outcome_decision_authority
    assert decision_view.promoted_outcome_ready_authority
    assert not decision_view.promoted_outcome_authority
    assert not decision_view.learning_authority
    assert not decision_view.promotion_authority
    assert not decision_view.execution_authority
    forged_promoted = decision_view.decision.model_copy(update={"promoted_outcome_authority": True})
    with pytest.raises(ValidationError, match="Input should be False"):
        forged_promoted.model_validate_json(forged_promoted.model_dump_json())
    deferred_decision = None
    for action in ("reject", "defer"):
        request = _decision_request(
            left.eligibility,
            1,
            timeout_seconds=3_600,
        )
        pending = new_interaction_record(
            request=request,
            subject_kind="tool",
            subject_id=left.eligibility.eligibility_id,
            session_id=f"outcome-{action}-session",
            agent_name="main",
            owner_id=f"outcome-{action}-owner",
            created_at=(answered_at + timedelta(seconds=20)).isoformat(),
            owner_lease_seconds=30,
            timeout_seconds=3_600,
            interaction_id=_interaction_id(left.eligibility.eligibility_id, 1),
        )
        answered = answer_interaction(
            pending,
            owner_id=pending.owner_id,
            owner_epoch=pending.owner_epoch,
            response={"kind": "option", "value": action},
            answered_by="local-user",
            now=(answered_at + timedelta(seconds=21)).isoformat(),
        )
        alternative = build_stable_promotion_outcome_decision(
            eligibility=left.eligibility,
            interaction=answered,
            revision=1,
            previous=None,
        )
        assert alternative.action is EvolutionStablePromotionOutcomeDecisionAction(action)
        assert not alternative.promoted_outcome_authority
        if action == "defer":
            deferred_decision = alternative
            assert (
                alternative.defer_until == (answered_at + timedelta(days=7, seconds=21)).isoformat()
            )
        else:
            assert not alternative.defer_until
    assert deferred_decision is not None
    revision_two_request = _decision_request(
        left.eligibility,
        2,
        timeout_seconds=3_600,
    )
    revision_two_pending = new_interaction_record(
        request=revision_two_request,
        subject_kind="tool",
        subject_id=left.eligibility.eligibility_id,
        session_id="outcome-revision-two-session",
        agent_name="main",
        owner_id="outcome-revision-two-owner",
        created_at=datetime.fromisoformat(deferred_decision.defer_until).isoformat(),
        owner_lease_seconds=30,
        timeout_seconds=3_600,
        interaction_id=_interaction_id(left.eligibility.eligibility_id, 2),
    )
    revision_two_answer = answer_interaction(
        revision_two_pending,
        owner_id=revision_two_pending.owner_id,
        owner_epoch=revision_two_pending.owner_epoch,
        response={"kind": "option", "value": "promote"},
        answered_by="local-user",
        now=(
            datetime.fromisoformat(deferred_decision.defer_until) + timedelta(seconds=1)
        ).isoformat(),
    )
    revision_two = build_stable_promotion_outcome_decision(
        eligibility=left.eligibility,
        interaction=revision_two_answer,
        revision=2,
        previous=deferred_decision,
    )
    assert revision_two.previous_decision_sha256 == deferred_decision.decision_sha256
    assert revision_two.action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE
    duplicate_left, duplicate_right = await asyncio.gather(
        decision_store.record(decision_view.decision),
        decision_store.record(decision_view.decision),
    )
    assert duplicate_left == duplicate_right == decision_view.decision
    decision_tool = EvolutionStablePromotionOutcomeDecisionTool(
        SimpleNamespace(evolution_stable_promotion_outcome_decision_service=decision_service)
    )
    decision_arguments = {
        "action": "inspect",
        "decision_id": decision_view.decision.decision_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        permission = PermissionChecker(mode).check(
            decision_tool.name,
            decision_arguments,
            tool=decision_tool,
        )
        assert permission.allowed and not permission.requires_confirmation
    assert "Outcome 独立决策" in await decision_tool.execute(**decision_arguments)
    assert "promote" not in decision_tool.parameters_schema["properties"]
    registry.register(decision_tool)
    decision_slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-outcome-decision inspect "
        + decision_view.decision.decision_id,
    )
    assert "Outcome 独立决策" in decision_slash

    outcome_store = EvolutionStablePromotionOutcomeStore(
        db_path,
        decision_store=decision_store,
        eligibility_store=eligibility_service_for_decision.store,
    )

    def outcome_service():
        return EvolutionStablePromotionOutcomeService(
            decision_service=decision_service,
            eligibility_service=eligibility_service_for_decision,
            store=outcome_store,
        )

    outcome_left, outcome_right = await asyncio.gather(
        outcome_service().record(decision_id=decision_view.decision.decision_id),
        outcome_service().record(decision_id=decision_view.decision.decision_id),
    )
    assert outcome_left == outcome_right
    assert outcome_left.current_outcome == outcome_left.outcome
    assert outcome_left.promoted_outcome_authority
    assert outcome_left.promoted
    assert not outcome_left.superseded
    assert not outcome_left.learning_authority
    assert not outcome_left.promotion_authority
    assert not outcome_left.execution_authority
    assert outcome_left.supersede_event.sequence == 1
    assert not outcome_left.supersede_event.prior_outcome_superseded
    forged_unpromoted = outcome_left.outcome.model_copy(update={"promoted": False})
    with pytest.raises(ValidationError, match="Input should be True"):
        forged_unpromoted.model_validate_json(forged_unpromoted.model_dump_json())
    with pytest.raises(EvolutionStablePromotionOutcomeError) as rejected_outcome:
        build_stable_promotion_outcome_pair(
            decision=alternative,
            eligibility=left.eligibility,
            head=None,
            previous_event=None,
        )
    assert rejected_outcome.value.code == "stable_promotion_outcome_lineage_mismatch"
    outcome_tool = EvolutionStablePromotionOutcomeTool(
        SimpleNamespace(evolution_stable_promotion_outcome_service=outcome_service())
    )
    outcome_arguments = {
        "action": "inspect",
        "outcome_id": outcome_left.outcome.outcome_id,
    }
    for mode in (
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
        PermissionMode.BYPASS,
    ):
        permission = PermissionChecker(mode).check(
            outcome_tool.name,
            outcome_arguments,
            tool=outcome_tool,
        )
        assert permission.allowed and not permission.requires_confirmation
    lockdown = PermissionChecker(PermissionMode.LOCKDOWN).check(
        outcome_tool.name,
        outcome_arguments,
        tool=outcome_tool,
    )
    assert not lockdown.allowed
    assert "Outcome Ledger" in await outcome_tool.execute(**outcome_arguments)
    registry.register(outcome_tool)
    outcome_record_slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-outcome record "
        + decision_view.decision.decision_id,
    )
    assert "Outcome Ledger" in outcome_record_slash
    outcome_slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-outcome inspect " + outcome_left.outcome.outcome_id,
    )
    assert "Outcome Ledger" in outcome_slash

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_outcome_supersede_events "
            "SET event_json = '{}' WHERE event_id = ?",
            (outcome_left.supersede_event.event_id,),
        )
    with pytest.raises(EvolutionStablePromotionOutcomeError) as corrupt_chain:
        await outcome_store.chain_valid(
            workbench_session_id=outcome_left.outcome.workbench_session_id,
            workbench_proposal_id=outcome_left.outcome.workbench_proposal_id,
        )
    assert corrupt_chain.value.code == "stable_promotion_outcome_store_corrupt"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_outcome_supersede_events "
            "SET event_json = ? WHERE event_id = ?",
            (
                outcome_left.supersede_event.model_dump_json(),
                outcome_left.supersede_event.event_id,
            ),
        )

    assessed_at_two = assessed_at + timedelta(seconds=60)
    assessment_two = build_stable_promotion_population_observation_assessment(
        workspace_root=tmp_path,
        contract=contract,
        finalization=finalization,
        members=_passing_members(finalization, assessed_at_two),
        assessed_at=assessed_at_two,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO evolution_stable_promotion_population_observation_assessments "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assessment_two.assessment_id,
                assessment_two.assessment_sha256,
                assessment_two.source_set_sha256,
                assessment_two.contract_id,
                assessment_two.population_finalization_receipt_id,
                assessment_two.status.value,
                assessment_two.model_dump_json(),
                assessment_two.assessed_at,
            ),
        )
    port.assessment = assessment_two
    port.assessments[assessment_two.assessment_id] = assessment_two
    eligibility_two = await eligibility_service_for_decision.record(
        population_assessment_id=assessment_two.assessment_id
    )
    decision_two = await decision_service.decide(
        eligibility_id=eligibility_two.eligibility.eligibility_id
    )
    outcome_two = await outcome_service().record(decision_id=decision_two.decision.decision_id)
    assert outcome_two.outcome.sequence == 2
    assert outcome_two.outcome.previous_outcome_id == outcome_left.outcome.outcome_id
    assert outcome_two.supersede_event.previous_event_id == (outcome_left.supersede_event.event_id)
    assert outcome_two.supersede_event.prior_outcome_superseded
    assert outcome_two.promoted_outcome_authority
    async def no_rollback_outcomes(_session_id: str):
        return ()

    projection_service = EvolutionProposalOutcomeProjectionService(
        rollback_outcome_store=SimpleNamespace(list_by_session=no_rollback_outcomes),
        rollback_outcome_service=SimpleNamespace(),
        stable_promotion_outcome_store=outcome_store,
        stable_promotion_outcome_service=outcome_service(),
    )
    projected = await projection_service.project_session(
        outcome_two.outcome.workbench_session_id
    )
    promoted_projection = projected[outcome_two.outcome.workbench_proposal_id]
    assert promoted_projection.status == "promoted"
    assert promoted_projection.promoted
    assert promoted_projection.authority_valid
    assert promoted_projection.stable_promotion_sequence == 2
    assert promoted_projection.stable_previous_outcome_id == outcome_left.outcome.outcome_id
    assert promoted_projection.stable_supersede_event_id == (
        outcome_two.supersede_event.event_id
    )
    assert promoted_projection.stable_prior_outcome_superseded
    assert not promoted_projection.learning_authority
    assert not promoted_projection.promotion_authority
    assert not promoted_projection.execution_authority
    forged_projection = promoted_projection.model_copy(
        update={"stable_supersede_event_id": ""}
    )
    with pytest.raises(ValidationError, match="promoted projection"):
        forged_projection.model_validate_json(forged_projection.model_dump_json())
    forged_head_authority = promoted_projection.model_copy(
        update={"projection_head_authority": False}
    )
    with pytest.raises(ValidationError, match="promoted projection"):
        forged_head_authority.model_validate_json(
            forged_head_authority.model_dump_json()
        )
    superseded_first = await outcome_service().inspect(outcome_id=outcome_left.outcome.outcome_id)
    assert superseded_first.superseded
    assert not superseded_first.promoted_outcome_authority
    assert "newer_promoted_outcome_exists" in superseded_first.invalidation_reasons

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_outcome_decisions "
            "SET decision_json = '{}' WHERE decision_id = ?",
            (decision_view.decision.decision_id,),
        )
    with pytest.raises(EvolutionStablePromotionOutcomeDecisionError) as corrupt_decision:
        await decision_store.get(decision_view.decision.decision_id)
    assert corrupt_decision.value.code == "stable_promotion_outcome_decision_store_corrupt"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_outcome_decisions "
            "SET decision_json = ? WHERE decision_id = ?",
            (
                decision_view.decision.model_dump_json(),
                decision_view.decision.decision_id,
            ),
        )

    with pytest.raises(ValidationError, match="Input should be False"):
        left.eligibility.model_copy(update={"promoted": True}).model_validate_json(
            left.eligibility.model_copy(update={"promoted": True}).model_dump_json()
        )

    port.authority = False
    stale_decision = await decision_service.inspect(decision_id=decision_view.decision.decision_id)
    assert not stale_decision.outcome_decision_authority
    assert not stale_decision.promoted_outcome_ready_authority
    assert "eligibility_stale" in stale_decision.invalidation_reasons
    stale = await service().inspect(eligibility_id=left.eligibility.eligibility_id)
    assert stale.current_eligibility is None
    assert not stale.outcome_review_ready_authority
    assert "population_assessment_stale" in stale.invalidation_reasons
    with pytest.raises(EvolutionStablePromotionOutcomeEligibilityError) as not_passing:
        await service().record(population_assessment_id=assessment.assessment_id)
    assert not_passing.value.code == "stable_promotion_outcome_population_not_passing"

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_outcome_eligibilities "
            "SET eligibility_json = '{}' WHERE eligibility_id = ?",
            (left.eligibility.eligibility_id,),
        )
    with pytest.raises(EvolutionStablePromotionOutcomeEligibilityError) as corrupt:
        await service().store.get(left.eligibility.eligibility_id)
    assert corrupt.value.code == "stable_promotion_outcome_eligibility_store_corrupt"
