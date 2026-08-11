from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.stable_promotion_outcome_eligibilities import (
    EvolutionStablePromotionOutcomeEligibilityError,
    EvolutionStablePromotionOutcomeEligibilityService,
    EvolutionStablePromotionOutcomeEligibilityStore,
)
from naumi_agent.evolution.stable_promotion_population_observation_assessments import (
    EvolutionStablePromotionPopulationMemberObservationStatus,
    EvolutionStablePromotionPopulationObservationAssessmentView,
    EvolutionStablePromotionPopulationObservationMember,
    build_stable_promotion_population_observation_assessment,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionOutcomeEligibilityTool,
)
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
        self.authority = True

    async def inspect(self, *, assessment_id: str):
        assert assessment_id == self.assessment.assessment_id
        current = self.assessment if self.authority else None
        return EvolutionStablePromotionPopulationObservationAssessmentView(
            receipt=self.assessment,
            current_assessment=current,
            durable_receipt_valid=True,
            latest_receipt_current=True,
            contract_authority=True,
            population_finalization_authority=True,
            member_source_set_current=True,
            assessment_temporally_current=self.authority,
            invalidation_reasons=() if self.authority else ("population_assessment_expired",),
            population_long_term_observation_authority=self.authority,
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
        tool = engine.tool_registry.get(
            "evolution_stable_promotion_outcome_eligibility"
        )
        assert isinstance(tool, EvolutionStablePromotionOutcomeEligibilityTool)
        service = engine.evolution_stable_promotion_outcome_eligibility_service
        store = engine.evolution_stable_promotion_outcome_eligibility_store
        assert service.store is store
        assert store.db_path == session_db.resolve()
        assert service.population_assessment_service is (
            engine.evolution_stable_promotion_population_observation_assessment_service
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
        SimpleNamespace(
            evolution_stable_promotion_outcome_eligibility_service=service()
        )
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
                content=await registered.execute(
                    **registered.parse_arguments(call.arguments)
                ),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-outcome-eligibility inspect "
        + left.eligibility.eligibility_id,
    )
    assert "Outcome 审批资格" in slash
    with pytest.raises(ValidationError, match="Input should be False"):
        left.eligibility.model_copy(update={"promoted": True}).model_validate_json(
            left.eligibility.model_copy(update={"promoted": True}).model_dump_json()
        )

    port.authority = False
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
