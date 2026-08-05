from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractBuilder,
    EvolutionRevalidationRuntimeContractService,
    EvolutionRevalidationRuntimeContractStore,
    EvolutionRevalidationRuntimeContractView,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanBuilder,
    EvolutionRevalidationValidationPlanStore,
    EvolutionRevalidationValidationPlanView,
)
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionRiskLevel
from naumi_agent.tools.evolution_review import EvolutionRevalidationRuntimeContractTool
from tests.unit.test_evolution_revalidation_validation_plans import (
    _builder_scenario,
)


async def _plan(tmp_path: Path, *, ready: bool = True):
    scenario = await _builder_scenario(
        tmp_path,
        metric_name=(
            "self_review.broad_except.count" if ready else "targeted.correctness"
        ),
        metric_direction="decrease" if ready else "increase",
        metric_target=0,
        metric_verifier="self_review_static" if ready else "harness_replay",
        include_boundary=ready,
    )
    package, fresh, source, contract, harness, target_files, now = scenario
    plan = EvolutionRevalidationValidationPlanBuilder().build(
        workspace_root=tmp_path,
        fresh_plan=fresh,
        source=source,
        package=package,
        contract=contract,
        suite_id="core_revalidation",
        requested_samples=7,
        harness_plan=harness,
        target_files=target_files,
        now=now,
    )
    return plan, source, now


@pytest.mark.asyncio
async def test_runtime_contract_binds_real_metric_runner_and_probe_coverage(
    tmp_path: Path,
) -> None:
    plan, _source, now = await _plan(tmp_path)

    contract = EvolutionRevalidationRuntimeContractBuilder().build(
        plan=plan,
        now=now,
    )

    assert contract.binding_status == "ready"
    assert contract.metric_binding_complete
    assert contract.metric_entries[0].resolution.status == "ready"
    assert contract.metric_entries[0].resolution.runner_version == "self_review_static@1"
    assert contract.adversarial_coverage_complete
    assert contract.probe_requirements[0].probe_id == "boundary-v1"
    assert contract.probe_coverage[0].check_id == "targeted_boundary"
    assert contract.required_duration_seconds == 2 * contract.requested_samples * (
        contract.profile_timeout_seconds_per_sample
        + contract.metric_timeout_seconds_per_sample
    )
    assert not contract.execution_started
    assert not contract.promotion_authority


@pytest.mark.asyncio
async def test_runtime_contract_blocks_unbound_metric_and_probe(tmp_path: Path) -> None:
    plan, _source, now = await _plan(tmp_path, ready=False)

    contract = EvolutionRevalidationRuntimeContractBuilder().build(
        plan=plan,
        now=now,
    )

    assert contract.binding_status == "blocked"
    assert not contract.metric_binding_complete
    assert not contract.adversarial_coverage_complete
    assert set(contract.blocking_codes) == {
        "probe_check_missing",
        "replay_fixture_required",
    }


@pytest.mark.asyncio
async def test_runtime_contract_store_and_dynamic_staleness(tmp_path: Path) -> None:
    plan, source, now = await _plan(tmp_path)
    db_path = tmp_path / ".naumi" / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_evaluation_sources SET "
            "snapshot_id = ?, snapshot_sha256 = ?, snapshot_json = ?",
            (source.snapshot_id, source.snapshot_sha256, source.model_dump_json()),
        )
        await db.commit()
    await EvolutionRevalidationValidationPlanStore(db_path).record(plan)
    state = SimpleNamespace(current=True)

    class _PlanService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationValidationPlanView(
                plan=plan,
                source_current=state.current,
                current_status="ready" if state.current else "stale",
                execution_eligible=state.current,
            )

    store = EvolutionRevalidationRuntimeContractStore(db_path)
    service = EvolutionRevalidationRuntimeContractService(
        validation_plan_service=_PlanService(),
        store=store,
        clock=lambda: now,
    )
    issued = await service.issue(
        workspace_root=tmp_path,
        validation_plan_id=plan.validation_plan_id,
    )
    repeated = await service.issue(
        workspace_root=tmp_path,
        validation_plan_id=plan.validation_plan_id,
    )

    assert issued == repeated
    assert issued.execution_eligible
    state.current = False
    stale = await service.inspect(
        workspace_root=tmp_path,
        contract_id=issued.contract.contract_id,
    )
    assert stale.current_status == "stale"
    assert not stale.execution_eligible


@pytest.mark.asyncio
async def test_runtime_contract_has_agent_and_slash_entrypoints(tmp_path: Path) -> None:
    plan, _source, now = await _plan(tmp_path)
    contract = EvolutionRevalidationRuntimeContractBuilder().build(plan=plan, now=now)
    view = EvolutionRevalidationRuntimeContractView(
        contract=contract,
        source_current=True,
        current_status="ready",
        execution_eligible=True,
    )

    async def issue(**_kwargs):
        return view

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_runtime_contract_service=SimpleNamespace(issue=issue),
    )
    tool = await EvolutionRevalidationRuntimeContractTool(engine).execute(
        plan.validation_plan_id
    )
    slash = await execute_slash_command(
        engine,
        f"/evolution revalidation-runtime-contract {plan.validation_plan_id}",
    )

    assert "Current status：`ready`" in tool
    assert "Evolution Fresh Runtime Contract" in slash
    rule = TOOL_PERMISSIONS["evolution_revalidation_runtime_contract"]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
