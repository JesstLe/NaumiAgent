from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_percentage_stage_completions import (
    EvolutionRevalidationPercentageStageCompletionError,
    EvolutionRevalidationPercentageStageCompletionService,
    EvolutionRevalidationPercentageStageCompletionStore,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
    EvolutionRevalidationRolloutBaselineStore,
)
from naumi_agent.evolution.revalidation_stage_completion_metrics import (
    EvolutionRevalidationStageCompletionStatus,
)
from naumi_agent.harness.store import HarnessStore
from tests.unit.test_evolution_revalidation_percentage_execution_outcome_ledger import (
    _sources,
    _terminal_run,
)


def _services(tmp_path: Path, outcome_service, clock):
    window_service = outcome_service.window_service
    plan_service = (
        window_service.exposure_service.deployment_service.intent_service
        .assignment_service.plan_service
    )
    db_path = outcome_service.store.db_path
    baseline_service = EvolutionRevalidationRolloutBaselineService(
        workspace_root=tmp_path,
        plan_service=plan_service,
        final_store=plan_service.promotion_input_service.final_store,
        cohort_store=EvolutionRevalidationInterventionalCohortStore(db_path),
        harness_store=HarnessStore(tmp_path / ".naumi" / "harness.db"),
        store=EvolutionRevalidationRolloutBaselineStore(db_path),
    )
    store = EvolutionRevalidationPercentageStageCompletionStore(
        db_path,
        outcome_service=outcome_service,
        window_service=window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
    )
    service = EvolutionRevalidationPercentageStageCompletionService(
        workspace_root=tmp_path,
        outcome_service=outcome_service,
        window_service=window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        store=store,
        clock=clock,
    )
    return plan_service, store, service


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_percentage_completion_aggregates_runs_and_revalidates_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, lifecycle, exposure, chat_store, _outcome_store, outcome_service = (
        await _sources(tmp_path)
    )
    assignment_id = data["assignment_id"]
    subject_id = exposure.binding.subject_id
    monkeypatch.setattr(
        "naumi_agent.runs.store._now_iso",
        lambda: (data["runtime_now"][0] - timedelta(seconds=1)).isoformat(),
    )
    plan_service, store, service = _services(
        tmp_path,
        outcome_service,
        lambda: data["runtime_now"][0],
    )
    _, _second_store, second_service = _services(
        tmp_path,
        outcome_service,
        lambda: data["runtime_now"][0] + timedelta(microseconds=1),
    )
    plan = (
        await plan_service.inspect(
            plan_id=exposure.deployment.preparation.intent.plan.plan_id
        )
    ).plan
    required = plan.stages[2].minimum_completed_runs
    runs = []
    for index in range(required):
        run = await _terminal_run(
            store=chat_store,
            binding=exposure.binding,
            outcome="completed",
            index=index,
        )
        runs.append(run)
        outcome = await outcome_service.record(
            assignment_id=assignment_id,
            subject_id=subject_id,
            session_id=run.session_id,
            run_id=run.id,
        )
        assert outcome.successful_completed_run_authority

    views = await asyncio.gather(
        *(
            candidate.assess(
                assignment_id=assignment_id,
                subject_id=subject_id,
            )
            for candidate in (service, second_service) * 3
        )
    )
    view = views[0]
    receipt = view.receipt
    assert all(candidate == view for candidate in views)
    assert receipt.metrics.observed_runs == required
    assert receipt.metrics.successful_runs == required
    assert receipt.metrics.unsuccessful_runs == 0
    assert receipt.metrics.completion_rate_basis_points == 10_000
    assert receipt.metrics.mean_reported_cost_microusd == 4_250
    assert receipt.metrics.status is EvolutionRevalidationStageCompletionStatus.INSUFFICIENT
    assert receipt.metrics.insufficient_reasons == (
        "cost_baseline_not_comparable",
    )
    assert not receipt.billing_authority
    assert not view.percentage_stage_completion_authority
    assert view.outcome_set_current
    assert await store.latest(assignment_id) == receipt
    with pytest.raises(ValueError, match="assignment_id"):
        await store.latest("' OR 1=1 --")

    failed_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="failed",
        index=required,
    )
    failed = await outcome_service.record(
        assignment_id=assignment_id,
        subject_id=subject_id,
        session_id=failed_run.session_id,
        run_id=failed_run.id,
    )
    assert failed.percentage_cohort_observation_input_authority
    assert not failed.successful_completed_run_authority

    breached = await service.assess(
        assignment_id=assignment_id,
        subject_id=subject_id,
    )
    assert breached.receipt.metrics.status is (
        EvolutionRevalidationStageCompletionStatus.BREACHED
    )
    assert "error_rate" in breached.receipt.metrics.breach_reasons
    assert breached.pause_input_authority
    assert breached.rollback_input_authority
    historical = await service.inspect(
        evidence_id=receipt.evidence_id,
        subject_id=subject_id,
    )
    assert not historical.latest_assessment
    assert not historical.outcome_set_current

    assert runs[0].usage is not None
    async with aiosqlite.connect(chat_store.db_path) as db:
        await db.execute(
            "UPDATE chat_runs SET usage_id = ? WHERE id = ?",
            ("runusage_" + "0" * 24, runs[0].id),
        )
        await db.commit()
    stale = await service.inspect(
        evidence_id=breached.receipt.evidence_id,
        subject_id=subject_id,
    )
    assert not stale.outcome_set_current
    assert not stale.pause_input_authority
    assert not stale.rollback_input_authority

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_percentage_stage_completions "
            "SET evidence_json = replace(evidence_json, ?, ?) WHERE evidence_id = ?",
            (
                breached.receipt.evidence_sha256,
                "0" * 64,
                breached.receipt.evidence_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationPercentageStageCompletionError) as corrupt:
        await store.get(breached.receipt.evidence_id)
    assert corrupt.value.code == "percentage_stage_completion_source_invalid"
    assert await lifecycle.close()
