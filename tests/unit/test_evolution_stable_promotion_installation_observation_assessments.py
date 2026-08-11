from __future__ import annotations

import asyncio
import math
import os
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.stable_promotion_installation_observation_assessments import (
    EvolutionStablePromotionInstallationObservationAssessmentError,
    EvolutionStablePromotionInstallationObservationAssessmentService,
    EvolutionStablePromotionInstallationObservationAssessmentStore,
    EvolutionStablePromotionInstallationObservationStatus,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionInstallationObservationAssessmentTool,
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


def _assessment_service(
    data,
    admission_service,
    revision_service,
    *,
    clock,
):
    store = EvolutionStablePromotionInstallationObservationAssessmentStore(
        admission_service.store.db_path,
        contract_store=data.contract_service.store,
        admission_store=admission_service.store,
        revision_store=revision_service.store,
    )
    return EvolutionStablePromotionInstallationObservationAssessmentService(
        workspace_root=data.contract.workspace_root,
        contract_store=data.contract_service.store,
        contract_service=data.contract_service,
        admission_store=admission_service.store,
        admission_service=admission_service,
        revision_store=revision_service.store,
        revision_service=revision_service,
        store=store,
        clock=clock,
    )


async def _deliver_new_revisions(cursor_service, revision_service, admission_id, now):
    cursor = await cursor_service.advance(admission_id=admission_id)
    receipt = None
    for offset in range(1, 5001):
        remote_head = await revision_service.store.remote_head(admission_id)
        after_sequence = 0 if remote_head is None else remote_head[0]
        if after_sequence == cursor.cursor.after_sequence:
            assert receipt is not None
            return receipt
        submission = await revision_service.prepare(
            admission_id=admission_id,
            after_sequence=after_sequence,
        )
        receipt = await revision_service.receive(
            submission=submission,
            received_at=now + timedelta(seconds=offset),
        )
    raise AssertionError("bounded revision delivery 未收敛到 installation cursor head")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_real_remote_installation_assessment_closes_state_authority_and_tamper(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    admission_service = data.build_service()
    admission_view = await admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    admission = admission_view.admission
    delivery_service, dispatch_store, admission_submission, delivery_now = await _delivery_setup(
        data, admission_service, admission, tmp_path
    )
    await _acknowledge(
        delivery_service,
        dispatch_store,
        admission_submission,
        delivery_now,
    )
    cursor_service = _cursor_service(
        data,
        admission_service,
        delivery_service,
        dispatch_store,
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

    assessment_now = [data.runtime_now[0] + timedelta(seconds=3)]
    first_service = _assessment_service(
        data,
        admission_service,
        revision_service,
        clock=lambda: assessment_now[0],
    )
    second_service = _assessment_service(
        data,
        admission_service,
        revision_service,
        clock=lambda: assessment_now[0],
    )
    left, right = await asyncio.gather(
        first_service.assess(admission_id=admission.admission_id),
        second_service.assess(admission_id=admission.admission_id),
    )
    assert left == right
    assert left.receipt.status is (
        EvolutionStablePromotionInstallationObservationStatus.INSUFFICIENT
    )
    assert set(left.receipt.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_operational_samples",
    }
    assert left.current_assessment == left.receipt
    assert left.assessment_temporally_current
    assert not left.installation_long_term_health_authority
    assert not left.installation_health_alert_authority
    assert not left.population_observation_authority
    assert not left.promoted_outcome_authority
    assert not left.learning_authority
    assert not left.promotion_authority
    assert not left.execution_authority

    tool = EvolutionStablePromotionInstallationObservationAssessmentTool(
        SimpleNamespace(
            evolution_stable_promotion_installation_observation_assessment_service=(first_service)
        )
    )
    arguments = {"action": "inspect", "admission_id": admission.admission_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "状态：**不足（insufficient）**" in await tool.execute(**arguments)
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
        "/evolution stable-promotion-installation-observation inspect " + admission.admission_id,
    )
    assert "稳定推广单安装长期观察" in slash

    timeout = admission.timeout_seconds
    pulse_interval = timeout - 1
    pulse_count = max(
        data.contract.minimum_operational_samples - 1,
        math.ceil(data.contract.minimum_observation_seconds / pulse_interval),
    )
    for _ in range(pulse_count):
        data.runtime_now[0] += timedelta(seconds=pulse_interval)
        await data.lifecycle._producer.pulse_now()
    await _deliver_new_revisions(
        cursor_service,
        revision_service,
        admission.admission_id,
        data.runtime_now[0] + timedelta(seconds=1),
    )
    assert len(await revision_service.store.received_chain(admission.admission_id)) > 1

    old = await first_service.inspect(admission_id=admission.admission_id)
    assert not old.observation_ledger_current
    assert old.current_assessment is None
    assert "observation_ledger_stale" in old.invalidation_reasons

    assessment_now[0] = data.runtime_now[0] + timedelta(seconds=10)
    passing = await first_service.assess(admission_id=admission.admission_id)
    assert passing.receipt.status is (
        EvolutionStablePromotionInstallationObservationStatus.PASSING
    ), (
        passing.receipt.breach_reasons,
        passing.receipt.maximum_observed_gap_seconds,
        passing.receipt.latest_age_seconds,
        passing.receipt.observation_seconds,
    )
    assert passing.receipt.observation_seconds >= 3600
    assert passing.receipt.operational_sample_count >= 12
    assert passing.installation_long_term_health_authority
    assert not passing.installation_health_alert_authority

    assessment_now[0] = data.runtime_now[0] + timedelta(seconds=timeout + 1)
    expired = await first_service.inspect(admission_id=admission.admission_id)
    assert not expired.assessment_temporally_current
    assert expired.current_assessment is None
    assert "assessment_expired" in expired.invalidation_reasons
    assert not expired.installation_long_term_health_authority
    assert not expired.installation_health_alert_authority

    breached = await first_service.assess(admission_id=admission.admission_id)
    assert breached.receipt.status is (
        EvolutionStablePromotionInstallationObservationStatus.BREACHED
    )
    assert breached.receipt.breach_reasons == ("heartbeat_stale",)
    assert breached.installation_health_alert_authority
    assert not breached.installation_long_term_health_authority

    assert await data.lifecycle.begin_draining()
    await _deliver_new_revisions(
        cursor_service,
        revision_service,
        admission.admission_id,
        data.runtime_now[0] + timedelta(seconds=1),
    )
    stale_alert = await first_service.inspect(admission_id=admission.admission_id)
    assert not stale_alert.observation_ledger_current
    assert not stale_alert.installation_health_alert_authority

    censored = await first_service.assess(admission_id=admission.admission_id)
    assert censored.receipt.status is (
        EvolutionStablePromotionInstallationObservationStatus.CENSORED
    )
    assert censored.receipt.censor_reasons == ("runtime_draining",)
    assert not censored.installation_long_term_health_authority
    assert not censored.installation_health_alert_authority

    with pytest.raises(ValidationError, match="Input should be False"):
        censored.receipt.model_copy(
            update={"population_observation_authority": True}
        ).model_validate_json(
            censored.receipt.model_copy(
                update={"population_observation_authority": True}
            ).model_dump_json()
        )

    with sqlite3.connect(first_service.store.db_path) as db:
        db.execute(
            "UPDATE "
            "evolution_stable_promotion_installation_observation_assessments "
            "SET assessment_json = '{}' WHERE assessment_id = ?",
            (censored.receipt.assessment_id,),
        )
    with pytest.raises(EvolutionStablePromotionInstallationObservationAssessmentError) as corrupt:
        await first_service.store.get(censored.receipt.assessment_id)
    assert corrupt.value.code == "stable_promotion_installation_assessment_store_corrupt"


@pytest.mark.asyncio
async def test_engine_composes_installation_assessment_service_and_tool(tmp_path) -> None:
    assert (
        evolution_api.EvolutionStablePromotionInstallationObservationAssessmentService
        is EvolutionStablePromotionInstallationObservationAssessmentService
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
            "evolution_stable_promotion_installation_observation_assessment"
        )
        assert isinstance(
            tool,
            EvolutionStablePromotionInstallationObservationAssessmentTool,
        )
        service = engine.evolution_stable_promotion_installation_observation_assessment_service
        store = engine.evolution_stable_promotion_installation_observation_assessment_store
        assert service.store is store
        assert store.db_path == session_db.resolve()
        assert (
            service.contract_store is engine.evolution_stable_promotion_observation_contract_store
        )
        assert (
            service.admission_store
            is engine.evolution_stable_promotion_runtime_observation_admission_store
        )
        assert (
            service.revision_store
            is engine.evolution_stable_promotion_observation_revision_delivery_store
        )
    finally:
        await engine.shutdown()
