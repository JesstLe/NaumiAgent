from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_rollout_stage_completions import (
    EvolutionRevalidationRolloutStageCompletionError,
    EvolutionRevalidationRolloutStageCompletionService,
    EvolutionRevalidationRolloutStageCompletionStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
)
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_runtime_observations import _monitor


async def _passing(root: Path):
    monitor, executor, entry, parent, control = await _monitor(root)
    plan = (await executor.entry_service.plan_service.inspect(plan_id=entry.receipt.plan_id)).plan
    for _ in range(plan.stages[0].minimum_completed_runs):
        view = await executor.execute(
            entry_receipt_id=entry.receipt.receipt_id,
            parent_receipt_id=parent.receipt_id,
        )
        assert view.passed
    observation = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)
    service = EvolutionRevalidationRolloutStageCompletionService(
        workspace_root=root,
        observation_store=monitor.store,
        plan_service=executor.entry_service.plan_service,
        entry_store=executor.entry_service.store,
        journal_store=monitor.journal_store,
        control_store=control.store,
        store=EvolutionRevalidationRolloutStageCompletionStore(monitor.store.db_path),
        now=lambda: (T0 + timedelta(hours=9)).isoformat(),
    )
    return service, observation, executor, entry, parent, control, plan


@pytest.mark.asyncio
async def test_passing_high_risk_canary_freezes_manual_stage_completion(
    tmp_path: Path,
) -> None:
    service, observation, _executor, _entry, _parent, _control, plan = await _passing(tmp_path)

    completions = await asyncio.gather(
        *(service.complete(observation_id=observation.observation_id) for _ in range(8))
    )
    item = completions[0]

    assert all(candidate == item for candidate in completions)
    assert item.completed_stage == "local_canary"
    assert item.next_stage == "opt_in"
    assert item.completed_runs == plan.stages[0].minimum_completed_runs
    assert item.stage_completed and item.passing_observation_verified
    assert item.manual_advance_required
    assert item.manual_interaction_required
    assert not item.automatic_advance_eligible
    assert not item.next_stage_entry_authority
    assert not item.deployment_authority


@pytest.mark.asyncio
async def test_insufficient_observation_cannot_complete_stage(tmp_path: Path) -> None:
    monitor, executor, entry, _parent, control = await _monitor(tmp_path)
    observation = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)
    service = EvolutionRevalidationRolloutStageCompletionService(
        workspace_root=tmp_path,
        observation_store=monitor.store,
        plan_service=executor.entry_service.plan_service,
        entry_store=executor.entry_service.store,
        journal_store=monitor.journal_store,
        control_store=control.store,
        store=EvolutionRevalidationRolloutStageCompletionStore(monitor.store.db_path),
    )

    with pytest.raises(EvolutionRevalidationRolloutStageCompletionError) as blocked:
        await service.complete(observation_id=observation.observation_id)

    assert blocked.value.code == "rollout_stage_completion_observation_not_passing"


@pytest.mark.asyncio
async def test_deleted_terminal_after_passing_blocks_completion(
    tmp_path: Path,
) -> None:
    service, observation, _executor, entry, _parent, _control, _plan = await _passing(tmp_path)
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_local_canary_events "
            "WHERE entry_receipt_id = ? AND run_index = ?",
            (entry.receipt.receipt_id, observation.completed_runs - 1),
        )

    with pytest.raises(EvolutionRevalidationRolloutStageCompletionError) as blocked:
        await service.complete(observation_id=observation.observation_id)

    assert blocked.value.code == "rollout_stage_completion_terminal_prefix_changed"


@pytest.mark.asyncio
async def test_control_change_after_passing_fences_completion(tmp_path: Path) -> None:
    service, observation, _executor, _entry, _parent, control, _plan = await _passing(tmp_path)
    await control.pause(
        reason_code="user_cancelled_rollout",
        actor=EvolutionRevalidationRolloutControlActor.USER,
        changed_at=(T0 + timedelta(hours=8, minutes=30)).isoformat(),
    )

    with pytest.raises(EvolutionRevalidationRolloutStageCompletionError) as blocked:
        await service.complete(observation_id=observation.observation_id)

    assert blocked.value.code == "rollout_stage_completion_control_changed"
