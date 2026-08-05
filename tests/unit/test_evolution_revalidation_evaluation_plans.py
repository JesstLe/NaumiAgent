from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.revalidation_evaluation_plans import (
    EvolutionRevalidationEvaluationLaneKind,
    EvolutionRevalidationEvaluationPlanError,
    EvolutionRevalidationEvaluationPlanService,
    EvolutionRevalidationEvaluationPlanStore,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationStore,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionRiskLevel
from naumi_agent.tools.evolution_review import (
    EvolutionRevalidationEvaluationPlanTool,
)
from tests.unit.test_evolution_revalidation_outcomes import _scenario
from tests.unit.test_evolution_revalidation_validations import _StaticStore


async def _plan_scenario(tmp_path: Path, *, status: HarnessSandboxCheckStatus):
    (
        package,
        request_view,
        validation_receipt,
        harness,
        _validation_service,
        outcome_store,
        outcome_service,
    ) = await _scenario(tmp_path, status=status)
    outcome_view = await outcome_service.issue(
        workspace_root=tmp_path,
        request_id=request_view.request.request_id,
    )
    service = EvolutionRevalidationEvaluationPlanService(
        outcome_service=outcome_service,
        outcome_store=outcome_store,
        validation_store=EvolutionRevalidationValidationStore(
            tmp_path / ".naumi" / "state.db"
        ),
        package_input_store=_StaticStore(
            type("PackageView", (), {"package_input": package})()
        ),
        store=EvolutionRevalidationEvaluationPlanStore(
            tmp_path / ".naumi" / "state.db"
        ),
        clock=lambda: datetime.fromisoformat(validation_receipt.created_at),
    )
    return package, validation_receipt, harness, outcome_view, outcome_service, service


@pytest.mark.asyncio
async def test_validated_outcome_creates_exact_fresh_evaluation_coverage(
    tmp_path: Path,
) -> None:
    package, validation, _harness, outcome, _outcome_service, service = (
        await _plan_scenario(tmp_path, status=HarnessSandboxCheckStatus.PASSED)
    )

    first = await service.issue(
        workspace_root=tmp_path,
        outcome_id=outcome.outcome.outcome_id,
    )
    repeated = await service.issue(
        workspace_root=tmp_path,
        outcome_id=outcome.outcome.outcome_id,
    )

    assert first == repeated
    assert first.execution_eligible
    assert first.plan.overlay_source_sha256 == validation.overlay_source_sha256
    assert first.plan.old_final_evaluation_id == package.final_evaluation_receipt_id
    assert first.plan.fresh_evidence_after == outcome.outcome.created_at
    assert first.plan.old_final_evaluation_invalidated
    assert first.plan.fresh_validation_plan_required
    assert first.plan.fresh_aggregation_contract_required
    assert first.plan.fresh_final_evaluation_required
    assert not first.plan.promotion_authority
    assert first.plan.lanes[0].kind is (
        EvolutionRevalidationEvaluationLaneKind.INTERVENTIONAL
    )
    assert tuple(item.platform for item in first.plan.lanes[1:]) == (
        package.required_platforms
    )


@pytest.mark.asyncio
async def test_failed_revalidation_outcome_cannot_plan_fresh_evaluation(
    tmp_path: Path,
) -> None:
    _package, _validation, _harness, outcome, _outcome_service, service = (
        await _plan_scenario(tmp_path, status=HarnessSandboxCheckStatus.FAILED)
    )

    with pytest.raises(EvolutionRevalidationEvaluationPlanError) as exc:
        await service.issue(
            workspace_root=tmp_path,
            outcome_id=outcome.outcome.outcome_id,
        )

    assert exc.value.code == "revalidation_evaluation_outcome_ineligible"


@pytest.mark.asyncio
async def test_profile_drift_makes_fresh_evaluation_plan_stale(tmp_path: Path) -> None:
    _package, _validation, harness, outcome, _outcome_service, service = (
        await _plan_scenario(tmp_path, status=HarnessSandboxCheckStatus.PASSED)
    )
    issued = await service.issue(
        workspace_root=tmp_path,
        outcome_id=outcome.outcome.outcome_id,
    )
    harness.check = harness.check.model_copy(update={"timeout_seconds": 31})

    inspected = await service.inspect(
        workspace_root=tmp_path,
        plan_id=issued.plan.plan_id,
    )

    assert not inspected.source_current
    assert inspected.current_status == "stale"
    assert not inspected.execution_eligible


@pytest.mark.asyncio
async def test_missing_durable_invalidation_blocks_plan_issue(tmp_path: Path) -> None:
    package, validation, _harness, outcome, outcome_service, _service = (
        await _plan_scenario(tmp_path, status=HarnessSandboxCheckStatus.PASSED)
    )
    service = EvolutionRevalidationEvaluationPlanService(
        outcome_service=outcome_service,
        outcome_store=EvolutionRevalidationOutcomeStore(
            tmp_path / ".naumi" / "empty-state.db"
        ),
        validation_store=EvolutionRevalidationValidationStore(
            tmp_path / ".naumi" / "state.db"
        ),
        package_input_store=_StaticStore(
            type("PackageView", (), {"package_input": package})()
        ),
        store=EvolutionRevalidationEvaluationPlanStore(
            tmp_path / ".naumi" / "empty-state.db"
        ),
        clock=lambda: datetime.fromisoformat(validation.created_at),
    )

    with pytest.raises(EvolutionRevalidationEvaluationPlanError) as exc:
        await service.issue(
            workspace_root=tmp_path,
            outcome_id=outcome.outcome.outcome_id,
        )

    assert exc.value.code == "revalidation_evaluation_invalidation_missing"


@pytest.mark.asyncio
async def test_fresh_evaluation_plan_has_agent_and_slash_entrypoints(
    tmp_path: Path,
) -> None:
    _package, _validation, _harness, outcome, _outcome_service, service = (
        await _plan_scenario(tmp_path, status=HarnessSandboxCheckStatus.PASSED)
    )
    engine = type(
        "Engine",
        (),
        {
            "workspace_root": tmp_path,
            "evolution_revalidation_evaluation_plan_service": service,
        },
    )()

    tool_output = await EvolutionRevalidationEvaluationPlanTool(engine).execute(
        outcome.outcome.outcome_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution revalidation-evaluation-plan {outcome.outcome.outcome_id}",
    )

    assert "Fresh plan / aggregation / final receipt：`required`" in tool_output
    assert "Fresh plan / aggregation / final receipt" in slash_output
    rule = TOOL_PERMISSIONS["evolution_revalidation_evaluation_plan"]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
