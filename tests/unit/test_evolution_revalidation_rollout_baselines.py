from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineError,
    EvolutionRevalidationRolloutBaselineService,
    EvolutionRevalidationRolloutBaselineStore,
    EvolutionRevalidationRolloutCostSource,
    _costs,
)
from naumi_agent.harness.store import HarnessStore
from tests.unit.test_evolution_revalidation_rollout_plans import _approved


async def _services(root: Path):
    contract, approved, _principals, _principal_service, plan_service = await _approved(
        root
    )
    plan = await plan_service.issue(
        contract_id=contract.contract_id,
        decision_id=approved.receipt.decision_id,
    )
    db_path = plan_service.store.db_path

    def build():
        return EvolutionRevalidationRolloutBaselineService(
            workspace_root=root,
            plan_service=plan_service,
            final_store=plan_service.promotion_input_service.final_store,
            cohort_store=EvolutionRevalidationInterventionalCohortStore(db_path),
            harness_store=HarnessStore(root / ".naumi" / "harness.db"),
            store=EvolutionRevalidationRolloutBaselineStore(db_path),
        )

    return plan, build, db_path


@pytest.mark.asyncio
async def test_baseline_freezes_exact_green_h5a_metrics_singleflight(
    tmp_path: Path,
) -> None:
    plan_view, build, _db_path = await _services(tmp_path)
    services = tuple(build() for _ in range(8))

    views = await asyncio.gather(*(
        service.issue(plan_id=plan_view.plan.plan_id) for service in services
    ))
    view = views[0]
    baseline = view.baseline

    assert all(item == view for item in views)
    assert view.source_current and view.monitor_input_ready
    assert not view.stage_advance_authority
    assert baseline.plan_sha256 == plan_view.plan.plan_sha256
    assert baseline.sample_count == len(baseline.green_result_sha256)
    assert baseline.sample_count == len(baseline.duration_micros)
    assert baseline.sample_count == baseline.completed_runs
    assert baseline.failed_runs == 0
    assert baseline.completion_rate_basis_points == 10_000
    assert baseline.error_rate_basis_points == 0
    assert baseline.p95_duration_micros == max(baseline.duration_micros)
    assert baseline.cost_source is (
        EvolutionRevalidationRolloutCostSource.NO_MODEL_EXECUTION
    )
    assert baseline.cost_microusd == (0,) * baseline.sample_count
    assert baseline.mean_cost_microusd == 0
    assert baseline.raw_h5a_verified and baseline.monitor_input_authority
    assert not baseline.stage_advance_authority
    assert not baseline.rollback_authority
    assert not baseline.promotion_authority


@pytest.mark.asyncio
async def test_missing_raw_green_h5a_dynamically_fences_baseline(
    tmp_path: Path,
) -> None:
    plan_view, build, _db_path = await _services(tmp_path)
    service = build()
    issued = await service.issue(plan_id=plan_view.plan.plan_id)

    with sqlite3.connect(tmp_path / ".naumi" / "harness.db") as db:
        db.execute(
            "DELETE FROM harness_eval_results WHERE result_sha256 = ?",
            (issued.baseline.green_result_sha256[0],),
        )

    stale = await service.inspect(plan_id=plan_view.plan.plan_id)

    assert not stale.source_current
    assert not stale.monitor_input_ready
    assert not stale.stage_advance_authority


@pytest.mark.asyncio
async def test_store_rejects_missing_final_dependency(tmp_path: Path) -> None:
    plan_view, build, db_path = await _services(tmp_path)
    service = build()
    baseline = (await service.issue(plan_id=plan_view.plan.plan_id)).baseline

    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_rollout_baselines WHERE plan_id = ?",
            (plan_view.plan.plan_id,),
        )
        db.execute(
            "DELETE FROM evolution_revalidation_final_evaluations WHERE receipt_id = ?",
            (baseline.final_evaluation_id,),
        )

    with pytest.raises(EvolutionRevalidationRolloutBaselineError) as blocked:
        await service.store.record(baseline)

    assert blocked.value.code == "rollout_baseline_dependency_mismatch"


def test_live_configuration_without_cost_evidence_is_not_zero_filled() -> None:
    record = SimpleNamespace(
        result=SimpleNamespace(
            baseline_identity=SimpleNamespace(
                configuration=SimpleNamespace(live=True),
            ),
            cases=(SimpleNamespace(live_evidence=None),),
        )
    )

    with pytest.raises(EvolutionRevalidationRolloutBaselineError) as blocked:
        _costs((record,))

    assert blocked.value.code == "rollout_baseline_live_evidence_mismatch"
