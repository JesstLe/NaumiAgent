from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.experiments import ExperimentBudget, ExperimentCheck
from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceSnapshot,
)
from naumi_agent.evolution.revalidation_evaluation_sources import (
    _sha256_payload as source_sha256,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanBuilder,
    EvolutionRevalidationValidationPlanError,
    EvolutionRevalidationValidationPlanService,
    EvolutionRevalidationValidationPlanStore,
    EvolutionRevalidationValidationPlanView,
)
from naumi_agent.harness.evolution_revalidation import (
    build_harness_evolution_revalidation_plan,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionRiskLevel
from naumi_agent.tools.evolution_review import (
    EvolutionRevalidationValidationPlanTool,
)
from tests.unit.test_evolution_revalidation_evaluation_sources import (
    _source_scenario,
)


def _checks(path: str) -> tuple[HarnessCheckSpec, ...]:
    return tuple(
        HarnessCheckSpec(
            id=f"targeted_{kind}",
            argv=("verify", kind, path),
            timeout_seconds=30,
            when_changed=(path,),
            required_for=("change",),
            provides=(kind,),
        )
        for kind in ("lint", "compile", "unit", "contract")
    )


def _rehash_source(source, *, profile_sha256: str, plan_sha256: str):
    payload = source.model_dump(
        mode="json", exclude={"snapshot_id", "snapshot_sha256"}
    )
    payload.update(
        harness_profile_sha256=profile_sha256,
        harness_plan_sha256=plan_sha256,
    )
    digest = source_sha256(payload)
    return EvolutionRevalidationEvaluationSourceSnapshot.model_validate(
        {
            **payload,
            "snapshot_id": f"evrevalsrc_{digest[:24]}",
            "snapshot_sha256": digest,
        }
    )


async def _builder_scenario(tmp_path: Path):
    package, _old_harness, fresh_view, _store, source_service = (
        await _source_scenario(tmp_path)
    )
    original = await source_service.capture(
        workspace_root=tmp_path,
        plan_id=fresh_view.plan.plan_id,
    )
    path = package.patch.files[0].path
    harness = build_harness_evolution_revalidation_plan(
        profile_sha256="d" * 64,
        changed_paths=(path,),
        checks=_checks(path),
    )
    source = _rehash_source(
        original,
        profile_sha256=harness.profile_sha256,
        plan_sha256=harness.plan_sha256,
    )
    contract = SimpleNamespace(
        contract_id=package.experiment_contract_id,
        manifest_sha256=package.experiment_contract_sha256,
        source=SimpleNamespace(
            candidate_id=package.candidate_id,
            candidate_revision=package.candidate_revision,
        ),
        scope=SimpleNamespace(
            allowed_files=tuple(item.path for item in package.patch.files)
        ),
        seed=8675309,
        budget=ExperimentBudget(
            max_changed_files=1,
            max_changed_lines=100,
            max_tool_calls=20,
            max_duration_seconds=300,
            max_attempts=2,
        ),
        allowed_checks=(
            ExperimentCheck(
                metric_name="targeted.correctness",
                direction="increase",
                target=1.0,
                verifier="harness_replay",
                procedure="运行固定输入的窄范围回归并比较 RED/GREEN。",
            ),
        ),
    )
    target_files = {
        path: ((tmp_path / path).read_bytes(), (tmp_path / path).stat().st_mode & 0o111 > 0)
    }
    now = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    return package, fresh_view.plan, source, contract, harness, target_files, now


@pytest.mark.asyncio
async def test_plan_rebinds_exact_current_red_and_immutable_green(tmp_path: Path) -> None:
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path)
    )

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

    file = plan.files[0]
    assert plan.red_revision == plan.green_revision == source.target_head
    assert plan.red_tree_sha256 == plan.green_tree_sha256
    assert file.red_sha256 != file.green_sha256
    assert file.green_sha256 == source.blobs[0].sha256
    assert plan.green_overlay_source_sha256 == source.overlay_source_sha256
    assert plan.seed == contract.seed
    assert plan.budget == contract.budget
    assert plan.requested_samples == 7
    assert plan.required_platforms == fresh.required_platforms
    assert {item.check_kind for item in plan.coverage} == {
        "lint",
        "compile",
        "unit",
        "contract",
    }
    assert not plan.evaluation_execution_started
    assert not plan.promotion_authority


@pytest.mark.asyncio
async def test_plan_rejects_target_operation_or_check_coverage_drift(
    tmp_path: Path,
) -> None:
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path)
    )
    path = package.patch.files[0].path
    builder = EvolutionRevalidationValidationPlanBuilder()

    with pytest.raises(EvolutionRevalidationValidationPlanError) as operation:
        builder.build(
            workspace_root=tmp_path,
            fresh_plan=fresh,
            source=source,
            package=package,
            contract=contract,
            suite_id="core_revalidation",
            requested_samples=7,
            harness_plan=harness,
            target_files={path: (None, False)},
            now=now,
        )
    assert operation.value.code == "revalidation_validation_plan_target_operation_drift"

    duplicate_unit = HarnessCheckSpec(
        id="second_unit",
        argv=("verify", "unit", path),
        when_changed=(path,),
        required_for=("change",),
        provides=("unit",),
    )
    ambiguous = build_harness_evolution_revalidation_plan(
        profile_sha256=source.harness_profile_sha256,
        changed_paths=(path,),
        checks=(*harness.checks, duplicate_unit),
    )
    source_with_ambiguous_plan = _rehash_source(
        source,
        profile_sha256=ambiguous.profile_sha256,
        plan_sha256=ambiguous.plan_sha256,
    )
    with pytest.raises(EvolutionRevalidationValidationPlanError) as coverage:
        builder.build(
            workspace_root=tmp_path,
            fresh_plan=fresh,
            source=source_with_ambiguous_plan,
            package=package,
            contract=contract,
            suite_id="core_revalidation",
            requested_samples=7,
            harness_plan=ambiguous,
            target_files=target_files,
            now=now,
        )
    assert coverage.value.code == "revalidation_validation_plan_check_coverage_invalid"


@pytest.mark.asyncio
async def test_plan_store_is_immutable_and_idempotent(tmp_path: Path) -> None:
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path)
    )
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
    db_path = tmp_path / ".naumi" / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_evaluation_sources SET "
            "snapshot_id = ?, snapshot_sha256 = ?, snapshot_json = ?",
            (source.snapshot_id, source.snapshot_sha256, source.model_dump_json()),
        )
        await db.commit()
    store = EvolutionRevalidationValidationPlanStore(db_path)

    assert await store.record(plan) == plan
    assert await store.record(plan) == plan
    assert await store.get(plan.validation_plan_id) == plan


@pytest.mark.asyncio
async def test_inspect_recomputes_with_original_authority_timestamp(
    tmp_path: Path,
) -> None:
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path)
    )
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

    class _PlanStore:
        async def get(self, _plan_id):
            return plan

    service = EvolutionRevalidationValidationPlanService(
        fresh_plan_service=SimpleNamespace(),
        source_store=SimpleNamespace(),
        package_store=SimpleNamespace(),
        experiment_store=SimpleNamespace(),
        final_evaluation_store=SimpleNamespace(),
        harness=SimpleNamespace(),
        store=_PlanStore(),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    async def rebuild(_workspace, _snapshot, *, now=None):
        assert now == datetime.fromisoformat(plan.created_at)
        return plan

    service._build_current = rebuild
    view = await service.inspect(
        workspace_root=tmp_path,
        validation_plan_id=plan.validation_plan_id,
    )

    assert view.source_current
    assert view.execution_eligible


@pytest.mark.asyncio
async def test_plan_has_agent_and_slash_entrypoints(tmp_path: Path) -> None:
    package, fresh, source, contract, harness, target_files, now = (
        await _builder_scenario(tmp_path)
    )
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
    view = EvolutionRevalidationValidationPlanView(
        plan=plan,
        source_current=True,
        current_status="ready",
        execution_eligible=True,
    )
    service = SimpleNamespace(issue=lambda **_kwargs: None)

    async def issue(**_kwargs):
        return view

    service.issue = issue
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_validation_plan_service=service,
    )

    tool = await EvolutionRevalidationValidationPlanTool(engine).execute(
        source.snapshot_id
    )
    slash = await execute_slash_command(
        engine,
        f"/evolution revalidation-validation-plan {source.snapshot_id}",
    )

    assert "Same environment / baseline first：`true` / `true`" in tool
    assert "Evolution Revalidation Validation Plan" in slash
    rule = TOOL_PERMISSIONS["evolution_revalidation_validation_plan"]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
