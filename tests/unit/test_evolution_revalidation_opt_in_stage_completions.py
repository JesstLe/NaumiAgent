from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_opt_in_stage_completions import (
    EvolutionRevalidationOptInStageCompletion,
    EvolutionRevalidationOptInStageCompletionError,
    EvolutionRevalidationOptInStageCompletionService,
    EvolutionRevalidationOptInStageCompletionStatus,
    EvolutionRevalidationOptInStageCompletionStore,
    _digest,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
    EvolutionRevalidationRolloutBaselineStore,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore
from naumi_agent.runs.usage import RunUsageTotals, build_run_usage
from tests.unit.test_evolution_revalidation_opt_in_execution_outcome_ledger import (
    _sources,
)


def _baseline_service(tmp_path: Path, outcome_service):
    plan_service = (
        outcome_service.window_service.runtime_health_service.deployment_service
        .intent_service.candidate_service.plan_service
    )
    db_path = outcome_service.store.db_path
    return plan_service, EvolutionRevalidationRolloutBaselineService(
        workspace_root=tmp_path,
        plan_service=plan_service,
        final_store=plan_service.promotion_input_service.final_store,
        cohort_store=EvolutionRevalidationInterventionalCohortStore(db_path),
        harness_store=HarnessStore(tmp_path / ".naumi" / "harness.db"),
        store=EvolutionRevalidationRolloutBaselineStore(db_path),
    )


async def _fixed_terminal_run(
    *,
    store: ChatRunStore,
    binding,
    outcome: str,
    index: int,
) -> ChatRunRecord:
    run = await store.start_run(
        session_id="session-opt-in-stage",
        user_message_id=f"message-{index}",
        release_binding=binding,
    )
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": f"receipt-{run.id}",
            "run_id": run.id,
            "outcome": outcome,
            "summary": "真实 opt-in managed run 已结束。",
            "git_state": {"available": True, "dirty": False},
            "started_at": run.started_at,
            "completed_at": run.started_at,
            "duration_ms": 0,
        }
    )
    usage = build_run_usage(
        run_id=run.id,
        before=RunUsageTotals(0, 0, 0, 0, Decimal("0")),
        after=RunUsageTotals(120, 30, 8, 2, Decimal("0.00425")),
    )
    await store.finish_run(
        run.id,
        status="completed" if outcome == "completed" else outcome,
        receipt=receipt,
        usage=usage,
    )
    restored = await store.get_run(run.session_id, run.id)
    assert restored is not None
    return restored


def _stage_service(
    *,
    tmp_path: Path,
    outcome_service,
    plan_service,
    baseline_service,
    clock,
):
    store = EvolutionRevalidationOptInStageCompletionStore(
        outcome_service.store.db_path,
        outcome_service=outcome_service,
        window_service=outcome_service.window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
    )
    return store, EvolutionRevalidationOptInStageCompletionService(
        workspace_root=tmp_path,
        outcome_service=outcome_service,
        window_service=outcome_service.window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        store=store,
        clock=clock,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_stage_completion_aggregates_current_runs_and_fails_closed(
    tmp_path: Path,
) -> None:
    completion, binding, samples, chat_store, _outcomes, outcome_service = (
        await _sources(tmp_path)
    )
    plan_service, baseline_service = _baseline_service(tmp_path, outcome_service)
    store, service = _stage_service(
        tmp_path=tmp_path,
        outcome_service=outcome_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        clock=lambda: datetime.fromisoformat(samples[-1].observed_at),
    )
    _, second_service = _stage_service(
        tmp_path=tmp_path,
        outcome_service=outcome_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        clock=lambda: datetime.fromisoformat(samples[-1].observed_at)
        + timedelta(microseconds=1),
    )
    plan = (await plan_service.inspect(plan_id=completion.plan_id)).plan
    required = plan.stages[1].minimum_completed_runs
    runs = []
    for index in range(required):
        run = await _fixed_terminal_run(
            store=chat_store,
            binding=binding,
            outcome="completed",
            index=index,
        )
        runs.append(run)
        recorded = await outcome_service.record(
            completion_id=completion.completion_id,
            subject_id=binding.subject_id,
            session_id=run.session_id,
            run_id=run.id,
        )
        assert recorded.successful_completed_run_authority

    views = await asyncio.gather(
        *(
            candidate.assess(
                completion_id=completion.completion_id,
                subject_id=binding.subject_id,
            )
            for candidate in (service, second_service) * 3
        )
    )
    view = views[0]
    receipt = view.receipt

    assert all(candidate == view for candidate in views)
    assert receipt.observed_runs == required
    assert receipt.successful_runs == required
    assert receipt.unsuccessful_runs == 0
    assert receipt.error_rate_basis_points == 0
    assert receipt.completion_rate_basis_points == 10_000
    assert receipt.duration_micros == (0,) * required
    assert receipt.mean_reported_cost_microusd == 4_250
    assert receipt.status is EvolutionRevalidationOptInStageCompletionStatus.INSUFFICIENT
    assert receipt.insufficient_reasons == ("cost_baseline_not_comparable",)
    assert not receipt.cost_comparable
    assert not receipt.billing_authority
    assert not receipt.opt_in_stage_completion_authority
    assert not view.opt_in_stage_completion_authority
    assert view.outcome_set_current
    assert await store.latest(completion.completion_id) == receipt

    passing = receipt.model_dump(mode="json")
    passing.update(
        {
            "baseline_cost_source": "live_evidence",
            "baseline_mean_cost_microusd": receipt.mean_reported_cost_microusd,
            "cost_comparable": True,
            "cost_regression_basis_points": 0,
            "insufficient_reasons": [],
            "status": "passing",
            "opt_in_stage_completion_authority": True,
        }
    )
    passing.pop("evidence_id")
    passing.pop("evidence_sha256")
    digest = _digest(passing)
    projected = EvolutionRevalidationOptInStageCompletion.model_validate(
        {
            **passing,
            "evidence_id": f"evreoptincomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )
    assert projected.opt_in_stage_completion_authority
    with pytest.raises(EvolutionRevalidationOptInStageCompletionError) as forged:
        await store.record(projected)
    assert forged.value.code == "opt_in_stage_completion_live_source_changed"

    failed_run = await _fixed_terminal_run(
        store=chat_store,
        binding=binding,
        outcome="failed",
        index=required,
    )
    failed_outcome = await outcome_service.record(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
        session_id=failed_run.session_id,
        run_id=failed_run.id,
    )
    assert failed_outcome.execution_outcome_authority
    assert not failed_outcome.successful_completed_run_authority

    breached = await service.assess(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert breached.receipt.status is (
        EvolutionRevalidationOptInStageCompletionStatus.BREACHED
    )
    assert "error_rate" in breached.receipt.breach_reasons
    assert breached.pause_input_authority
    assert breached.rollback_input_authority
    historical = await service.inspect(
        evidence_id=receipt.evidence_id,
        subject_id=binding.subject_id,
    )
    assert not historical.latest_assessment
    assert not historical.outcome_set_current
    assert not historical.opt_in_stage_completion_authority

    assert runs[0].usage is not None
    async with aiosqlite.connect(chat_store.db_path) as db:
        await db.execute(
            "UPDATE chat_runs SET usage_id = ? WHERE id = ?",
            ("runusage_" + "0" * 24, runs[0].id),
        )
        await db.commit()
    stale = await service.inspect(
        evidence_id=breached.receipt.evidence_id,
        subject_id=binding.subject_id,
    )
    assert not stale.outcome_set_current
    assert not stale.pause_input_authority
    assert not stale.rollback_input_authority

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_opt_in_stage_completions "
            "SET evidence_json = replace(evidence_json, ?, ?) WHERE evidence_id = ?",
            (
                breached.receipt.evidence_sha256,
                "0" * 64,
                breached.receipt.evidence_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationOptInStageCompletionError) as corrupt:
        await store.get(breached.receipt.evidence_id)
    assert corrupt.value.code == "opt_in_stage_completion_source_invalid"
