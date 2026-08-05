from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortStore,
)
from naumi_agent.evolution.revalidation_local_canary_runs import (
    EvolutionRevalidationLocalCanaryRunError,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
    EvolutionRevalidationRolloutBaselineStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
)
from naumi_agent.evolution.revalidation_runtime_observations import (
    EvolutionRevalidationRuntimeObservationService,
    EvolutionRevalidationRuntimeObservationStatus,
    EvolutionRevalidationRuntimeObservationStore,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from naumi_agent.harness.store import HarnessStore
from tests.unit.test_evolution_revalidation_interventional_samples import _SandboxKernel
from tests.unit.test_evolution_revalidation_local_canary_runs import T0, _scenario


async def _monitor(root: Path, *, kernel=None):
    scenario = await _scenario(root, kernel=kernel)
    executor, entry, parent, _kernel, journal, _grants, _harness, control = scenario
    plan_service = executor.entry_service.plan_service
    db_path = plan_service.store.db_path
    baseline_service = EvolutionRevalidationRolloutBaselineService(
        workspace_root=root,
        plan_service=plan_service,
        final_store=plan_service.promotion_input_service.final_store,
        cohort_store=EvolutionRevalidationInterventionalCohortStore(db_path),
        harness_store=HarnessStore(root / ".naumi" / "harness.db"),
        store=EvolutionRevalidationRolloutBaselineStore(db_path),
    )
    await baseline_service.issue(plan_id=entry.receipt.plan_id)
    monitor = EvolutionRevalidationRuntimeObservationService(
        workspace_root=root,
        entry_service=executor.entry_service,
        baseline_service=baseline_service,
        journal_store=journal,
        control_store=control.store,
        store=EvolutionRevalidationRuntimeObservationStore(db_path),
        now=lambda: (T0 + timedelta(hours=8)).isoformat(),
    )
    return monitor, executor, entry, parent, control


@pytest.mark.asyncio
async def test_empty_journal_is_insufficient_not_passing(tmp_path: Path) -> None:
    monitor, _executor, entry, _parent, _control = await _monitor(tmp_path)

    receipt = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    assert receipt.status is EvolutionRevalidationRuntimeObservationStatus.INSUFFICIENT
    assert set(receipt.insufficient_reasons) == {
        "minimum_completed_runs",
        "minimum_observation_seconds",
    }
    assert not receipt.breach_reasons
    assert not receipt.pause_input_authority
    assert not receipt.rollback_input_authority
    assert not receipt.stage_advance_authority


@pytest.mark.asyncio
async def test_minimum_real_canary_prefix_can_form_passing_observation(
    tmp_path: Path,
) -> None:
    monitor, executor, entry, parent, _control = await _monitor(tmp_path)
    stage = (
        await executor.entry_service.plan_service.inspect(plan_id=entry.receipt.plan_id)
    ).plan.stages[0]
    for _ in range(stage.minimum_completed_runs):
        view = await executor.execute(
            entry_receipt_id=entry.receipt.receipt_id,
            parent_receipt_id=parent.receipt_id,
        )
        assert view.passed

    receipt = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    assert receipt.status is EvolutionRevalidationRuntimeObservationStatus.PASSING
    assert receipt.completed_runs == stage.minimum_completed_runs
    assert receipt.passed_runs == receipt.completed_runs
    assert receipt.error_rate_basis_points == 0
    assert not receipt.insufficient_reasons
    assert not receipt.breach_reasons
    assert not receipt.stage_advance_authority


@pytest.mark.asyncio
async def test_early_check_failure_breaches_even_before_minimum_samples(
    tmp_path: Path,
) -> None:
    class _FailingKernel(_SandboxKernel):
        async def execute(self, **kwargs):
            results = await super().execute(**kwargs)
            return (
                replace(
                    results[0],
                    status=HarnessSandboxCheckStatus.FAILED,
                    exit_code=1,
                ),
                *results[1:],
            )

    monitor, executor, entry, parent, _control = await _monitor(
        tmp_path, kernel=_FailingKernel()
    )
    await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )

    receipt = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    assert receipt.status is EvolutionRevalidationRuntimeObservationStatus.BREACHED
    assert "error_rate" in receipt.breach_reasons
    assert not receipt.insufficient_reasons
    assert receipt.pause_input_authority and receipt.rollback_input_authority
    assert not receipt.stage_advance_authority


@pytest.mark.asyncio
async def test_user_pause_is_a_withdrawal_breach_even_without_runs(
    tmp_path: Path,
) -> None:
    monitor, _executor, entry, _parent, control = await _monitor(tmp_path)
    await control.pause(
        reason_code="user_cancelled_rollout",
        actor=EvolutionRevalidationRolloutControlActor.USER,
        changed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )

    receipt = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    assert receipt.status is EvolutionRevalidationRuntimeObservationStatus.BREACHED
    assert receipt.breach_reasons == ("user_withdrawal",)
    assert receipt.control_signal_event_ids
    assert receipt.pause_input_authority and receipt.rollback_input_authority


@pytest.mark.asyncio
async def test_deleted_intermediate_journal_event_blocks_observation(
    tmp_path: Path,
) -> None:
    monitor, executor, entry, parent, _control = await _monitor(tmp_path)
    await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )
    with sqlite3.connect(monitor.store.db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_local_canary_events "
            "WHERE entry_receipt_id = ? AND sequence = 2",
            (entry.receipt.receipt_id,),
        )

    with pytest.raises(EvolutionRevalidationLocalCanaryRunError) as blocked:
        await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    assert blocked.value.code == "local_canary_journal_chain_broken"
