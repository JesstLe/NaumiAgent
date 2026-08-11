from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
)
from naumi_agent.evolution.stable_promotion_population_observation_assessments import (
    EvolutionStablePromotionPopulationMemberObservationStatus,
    EvolutionStablePromotionPopulationObservationAssessmentError,
    EvolutionStablePromotionPopulationObservationAssessmentService,
    EvolutionStablePromotionPopulationObservationAssessmentStore,
    EvolutionStablePromotionPopulationObservationMember,
    EvolutionStablePromotionPopulationObservationStatus,
    _aggregate,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationMember,
    EvolutionStableRemotePopulationFinalizationReceipt,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionPopulationObservationAssessmentTool,
)
from tests.unit.test_evolution_stable_promotion_installation_observation_assessments import (
    _assessment_service,
    _deliver_new_revisions,
)
from tests.unit.test_evolution_stable_promotion_observation_chain_cursors import (
    _acknowledge,
    _delivery_setup,
)
from tests.unit.test_evolution_stable_promotion_observation_chain_cursors import (
    _service as _cursor_service,
)
from tests.unit.test_evolution_stable_promotion_observation_revision_deliveries import (
    _revision_service,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def _real_population_receipt(data):
    members = [data.member]
    for index, char in enumerate(("a", "b", "c"), start=2):
        core = data.member.model_dump(mode="json", exclude={"member_source_sha256"})
        core.update(
            {
                "installation_member_id": f"relpopmember_{char * 24}",
                "installation_credential_id": f"relpopcred_{char * 24}",
                "installation_credential_sha256": _digest([index, "credential"]),
                "installation_public_key_sha256": _digest([index, "public-key"]),
                "member_receipt_id": f"evstableremotefinalreceipt_{char * 24}",
                "member_receipt_sha256": _digest([index, "receipt"]),
                "authorization_id": f"evstableremotefinalauth_{char * 24}",
                "authorization_sha256": _digest([index, "authorization"]),
                "grant_id": f"evstableremotefinalgrant_{char * 24}",
                "grant_sha256": _digest([index, "grant"]),
                "result_id": f"evstableremotefinalresult_{char * 24}",
                "result_sha256": _digest([index, "result"]),
                "release_finalization_id": f"relstablefinal_{char * 24}",
                "release_finalization_sha256": _digest([index, "finalization"]),
                "expected_pointer_id": f"relactive_{char * 24}",
                "expected_pointer_sha256": _digest([index, "pointer"]),
            }
        )
        members.append(
            EvolutionStableRemotePopulationFinalizationMember.model_validate(
                {**core, "member_source_sha256": _digest(core)}
            )
        )
    members = tuple(sorted(members, key=lambda item: item.installation_member_id))
    source_set = _digest(
        {
            "population_snapshot_id": data.contract.population_snapshot_id,
            "population_snapshot_sha256": data.contract.population_snapshot_sha256,
            "member_source_sha256": tuple(item.member_source_sha256 for item in members),
        }
    )
    core = {
        "schema_version": 1,
        "policy_version": "evolution-stable-remote-population-finalization-v1",
        "source_set_sha256": source_set,
        "workspace_root_sha256": "f" * 64,
        "population_snapshot_id": data.contract.population_snapshot_id,
        "population_snapshot_sha256": data.contract.population_snapshot_sha256,
        "population_sequence": data.contract.population_snapshot_sequence,
        "population_denominator": len(members),
        "population_completion_receipt_id": (
            data.contract.population_completion_receipt_id
        ),
        "population_completion_receipt_sha256": (
            data.contract.population_completion_receipt_sha256
        ),
        "candidate_version": data.contract.candidate_version,
        "control_sequence": 0,
        "control_event_id": "",
        "control_event_sha256": "",
        "members": members,
        "finalized_at": data.member.recorded_at,
        "remote_member_receipts_complete": True,
        "binary_only": True,
        "stable_population_finalization_fact": True,
        "population_snapshot_prevalidated": True,
        "config_data_finalization_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRemotePopulationFinalizationReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstableremotepopfinal_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _contract_for_finalization(data, finalization):
    core = data.contract.model_dump(
        mode="json", exclude={"contract_id", "contract_sha256"}
    )
    core.update(
        {
            "population_finalization_receipt_id": finalization.receipt_id,
            "population_finalization_receipt_sha256": finalization.receipt_sha256,
        }
    )
    digest = _digest(core)
    return EvolutionStablePromotionObservationContract.model_validate(
        {
            **core,
            "contract_id": f"evstablepromobserve_{digest[:24]}",
            "contract_sha256": digest,
        }
    )


def _population_service(data, admission_service, installation_service, *, clock):
    store = EvolutionStablePromotionPopulationObservationAssessmentStore(
        admission_service.store.db_path,
        contract_store=data.contract_service.store,
        finalization_store=data.finalization_service.store,
        admission_store=admission_service.store,
        installation_assessment_store=installation_service.store,
    )
    return EvolutionStablePromotionPopulationObservationAssessmentService(
        workspace_root=data.contract.workspace_root,
        contract_store=data.contract_service.store,
        contract_service=data.contract_service,
        finalization_service=data.finalization_service,
        admission_store=admission_service.store,
        admission_service=admission_service,
        installation_assessment_store=installation_service.store,
        installation_assessment_service=installation_service,
        store=store,
        clock=clock,
    )


def _observation_member(char: str, status, *, seconds: int = 0):
    missing = status is EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
    nonterminal = status in {
        EvolutionStablePromotionPopulationMemberObservationStatus.INSUFFICIENT,
        EvolutionStablePromotionPopulationMemberObservationStatus.PASSING,
    }
    core = {
        "schema_version": 1,
        "installation_member_id": f"relpopmember_{char * 24}",
        "installation_credential_id": f"relpopcred_{char * 24}",
        "finalization_member_source_sha256": _digest([char, "finalization"]),
        "admission_id": "" if missing else f"evstablepromadmit_{char * 24}",
        "admission_sha256": "" if missing else _digest([char, "admission"]),
        "assessment_id": (
            "" if missing else f"evstableprominstallobserve_{char * 24}"
        ),
        "assessment_sha256": "" if missing else _digest([char, "assessment"]),
        "status": status,
        "reason": "runtime_admission_missing" if missing else "",
        "sample_count": 0 if missing else 12,
        "operational_sample_count": 0 if missing else 12,
        "observation_seconds": seconds,
        "minimum_observation_seconds": 3600,
        "last_observed_at": "" if missing else "2026-08-12T00:00:00+00:00",
        "valid_until": "2026-08-12T00:01:00+00:00" if nonterminal else "",
        "assessment_current_at_issue": not missing,
    }
    return EvolutionStablePromotionPopulationObservationMember.model_validate(
        {**core, "source_sha256": _digest(core)}
    )


def test_population_projection_recomputes_five_states_and_breach_precedence() -> None:
    members = tuple(
        sorted(
            (
                _observation_member(
                    "1", EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
                ),
                _observation_member(
                    "2",
                    EvolutionStablePromotionPopulationMemberObservationStatus.INSUFFICIENT,
                    seconds=1_800,
                ),
                _observation_member(
                    "3",
                    EvolutionStablePromotionPopulationMemberObservationStatus.PASSING,
                    seconds=3_600,
                ),
                _observation_member(
                    "4",
                    EvolutionStablePromotionPopulationMemberObservationStatus.BREACHED,
                    seconds=900,
                ),
                _observation_member(
                    "5",
                    EvolutionStablePromotionPopulationMemberObservationStatus.CENSORED,
                    seconds=2_700,
                ),
            ),
            key=lambda item: item.installation_member_id,
        )
    )
    projection = _aggregate(members)
    assert projection["status"] is EvolutionStablePromotionPopulationObservationStatus.BREACHED
    assert projection["assessment_coverage_bps"] == 8_000
    assert projection["duration_coverage_bps"] == 5_000
    assert projection["members_meeting_duration"] == 1
    assert projection["missing_member_ids"] == (members[0].installation_member_id,)
    with pytest.raises(ValueError):
        EvolutionStablePromotionPopulationObservationMember.model_validate(
            {**members[2].model_dump(mode="json"), "source_sha256": "0" * 64}
        )


@pytest.mark.asyncio
async def test_engine_composes_population_observation_service_and_tool(tmp_path) -> None:
    assert (
        evolution_api.EvolutionStablePromotionPopulationObservationAssessmentService
        is EvolutionStablePromotionPopulationObservationAssessmentService
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
            "evolution_stable_promotion_population_observation_assessment"
        )
        assert isinstance(
            tool,
            EvolutionStablePromotionPopulationObservationAssessmentTool,
        )
        service = (
            engine.evolution_stable_promotion_population_observation_assessment_service
        )
        store = engine.evolution_stable_promotion_population_observation_assessment_store
        assert service.store is store
        assert store.db_path == session_db.resolve()
        assert (
            service.installation_assessment_service
            is engine.evolution_stable_promotion_installation_observation_assessment_service
        )
        assert service.finalization_service is (
            engine.evolution_stable_remote_population_finalization_service
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_real_population_observation_missing_insufficient_and_timeout(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    finalization = _real_population_receipt(data)
    contract = _contract_for_finalization(data, finalization)
    data.contract = contract
    data.contract_service.contract = contract
    data.contract_service.store.contract = contract
    object.__setattr__(data.finalization_service, "receipt", finalization)
    with sqlite3.connect(data.store.db_path) as db:
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
            "ALTER TABLE evolution_stable_remote_population_finalizations "
            "ADD COLUMN receipt_json TEXT"
        )
        db.execute("DELETE FROM evolution_stable_remote_population_finalizations")
        db.execute(
            "INSERT INTO evolution_stable_remote_population_finalizations "
            "(receipt_id, receipt_sha256, receipt_json) VALUES (?, ?, ?)",
            (
                finalization.receipt_id,
                finalization.receipt_sha256,
                finalization.model_dump_json(),
            ),
        )
    admission_service = data.build_service()
    admission_view = await admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    admission = admission_view.admission
    delivery_service, dispatch_store, submission, delivery_now = await _delivery_setup(
        data, admission_service, admission, tmp_path
    )
    await _acknowledge(delivery_service, dispatch_store, submission, delivery_now)
    cursor_service = _cursor_service(
        data, admission_service, delivery_service, dispatch_store
    )
    revision_service = _revision_service(data, cursor_service, delivery_service)
    delivery_service.installation_key_service.clock = lambda: (
        data.runtime_now[0] + timedelta(seconds=1)
    )
    await _deliver_new_revisions(
        cursor_service,
        revision_service,
        admission.admission_id,
        data.runtime_now[0] + timedelta(seconds=1),
    )
    now = [data.runtime_now[0] + timedelta(seconds=3)]
    installation_service = _assessment_service(
        data,
        admission_service,
        revision_service,
        clock=lambda: now[0],
    )

    service = _population_service(
        data,
        admission_service,
        installation_service,
        clock=lambda: now[0],
    )
    missing = await service.assess(
        finalization_receipt_id=finalization.receipt_id
    )
    assert missing.receipt.status is (
        EvolutionStablePromotionPopulationObservationStatus.INSUFFICIENT
    )
    assert missing.receipt.missing_count == 4
    assert {item.reason for item in missing.receipt.members} == {
        "runtime_admission_missing",
        "installation_assessment_missing",
    }
    assert not missing.population_long_term_observation_authority

    installation = await installation_service.assess(admission_id=admission.admission_id)
    assert installation.receipt.status.value == "insufficient"
    second_service = _population_service(
        data,
        admission_service,
        installation_service,
        clock=lambda: now[0] + timedelta(seconds=1),
    )
    insufficient, concurrent = await asyncio.gather(
        service.assess(finalization_receipt_id=finalization.receipt_id),
        second_service.assess(finalization_receipt_id=finalization.receipt_id),
    )
    assert concurrent == insufficient
    assert insufficient.receipt.insufficient_count == 1
    assert insufficient.receipt.missing_count == 3
    assert insufficient.receipt.assessment_coverage_bps == 2_500
    assert insufficient.receipt.duration_coverage_bps < 10_000
    assert insufficient.current_assessment == insufficient.receipt

    tool = EvolutionStablePromotionPopulationObservationAssessmentTool(
        SimpleNamespace(
            evolution_stable_promotion_population_observation_assessment_service=(service)
        )
    )
    arguments = {
        "action": "inspect",
        "assessment_id": insufficient.receipt.assessment_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "稳定推广群体长期观察" in await tool.execute(**arguments)
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
        "/evolution stable-promotion-population-observation inspect "
        + insufficient.receipt.assessment_id,
    )
    assert "稳定推广群体长期观察" in slash

    old = await service.inspect(assessment_id=missing.receipt.assessment_id)
    assert old.current_assessment is None
    assert "newer_population_assessment_exists" in old.invalidation_reasons
    assert "population_member_source_set_changed" in old.invalidation_reasons

    now[0] += timedelta(seconds=admission.timeout_seconds + 1)
    expired = await service.inspect(assessment_id=insufficient.receipt.assessment_id)
    assert expired.current_assessment is None
    assert not expired.assessment_temporally_current
    assert "population_assessment_expired" in expired.invalidation_reasons

    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_population_observation_assessments "
            "SET assessment_json = '{}' WHERE assessment_id = ?",
            (insufficient.receipt.assessment_id,),
        )
    with pytest.raises(
        EvolutionStablePromotionPopulationObservationAssessmentError
    ) as corrupt:
        await service.store.get(insufficient.receipt.assessment_id)
    assert corrupt.value.code == "stable_promotion_population_assessment_store_corrupt"
