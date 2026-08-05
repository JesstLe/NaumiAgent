from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_rollback_requests import (
    EvolutionRevalidationRollbackRequestError,
    EvolutionRevalidationRollbackRequestService,
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlState,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from tests.unit.test_evolution_revalidation_interventional_samples import _SandboxKernel
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_runtime_observations import _monitor


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


def _service(monitor, executor, control):
    return EvolutionRevalidationRollbackRequestService(
        workspace_root=executor.workspace_root,
        observation_store=monitor.store,
        baseline_service=monitor.baseline_service,
        entry_service=executor.entry_service,
        journal_store=monitor.journal_store,
        control_service=control,
        store=EvolutionRevalidationRollbackRequestStore(monitor.store.db_path),
        now=lambda: (T0 + timedelta(hours=9)).isoformat(),
    )


@pytest.mark.asyncio
async def test_breach_atomically_pauses_and_freezes_rollback_request_singleflight(
    tmp_path: Path,
) -> None:
    monitor, executor, entry, parent, control = await _monitor(
        tmp_path, kernel=_FailingKernel()
    )
    await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )
    observation = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)
    services = tuple(_service(monitor, executor, control) for _ in range(8))

    requests = await asyncio.gather(*(
        service.issue(observation_id=observation.observation_id)
        for service in services
    ))
    request = requests[0]

    assert all(item == request for item in requests)
    assert request.observation_sha256 == observation.observation_sha256
    assert request.rollback_plan_sha256 == request.rollback_plan.plan_sha256
    assert request.breach_reasons == observation.breach_reasons
    assert request.automatic_pause_satisfied
    assert request.monitor_pause_created
    assert request.pause_actor is EvolutionRevalidationRolloutControlActor.MONITOR
    assert request.pause_reason_code == "runtime_guardrail_breach"
    assert request.rollback_request_authority
    assert not request.rollback_execution_authority
    assert not request.workspace_write_executed
    assert not request.git_write_executed
    assert not request.rollback_executed
    assert not request.promotion_authority
    latest = await control.store.latest(tmp_path)
    assert latest is not None
    assert latest.state is EvolutionRevalidationRolloutControlState.PAUSED
    fenced = await executor.entry_service.inspect(
        receipt_id=entry.receipt.receipt_id,
        assessed_at=(T0 + timedelta(hours=9)).isoformat(),
    )
    assert not fenced.can_execute_local_canary


@pytest.mark.asyncio
async def test_insufficient_observation_cannot_pause_or_request_rollback(
    tmp_path: Path,
) -> None:
    monitor, executor, entry, _parent, control = await _monitor(tmp_path)
    observation = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)
    service = _service(monitor, executor, control)

    with pytest.raises(EvolutionRevalidationRollbackRequestError) as blocked:
        await service.issue(observation_id=observation.observation_id)

    assert blocked.value.code == "rollback_request_observation_not_breached"
    assert await control.store.latest(tmp_path) is None
    assert await service.store.get_by_observation(observation.observation_id) is None


@pytest.mark.asyncio
async def test_existing_user_pause_satisfies_kill_switch_without_forging_monitor_event(
    tmp_path: Path,
) -> None:
    monitor, executor, entry, _parent, control = await _monitor(tmp_path)
    user_pause = await control.pause(
        reason_code="user_cancelled_rollout",
        actor=EvolutionRevalidationRolloutControlActor.USER,
        changed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )
    observation = await monitor.assess(entry_receipt_id=entry.receipt.receipt_id)

    request = await _service(monitor, executor, control).issue(
        observation_id=observation.observation_id
    )

    assert request.pause_event_id == user_pause.event_id
    assert request.pause_actor is EvolutionRevalidationRolloutControlActor.USER
    assert request.pause_reason_code == "user_cancelled_rollout"
    assert not request.monitor_pause_created
    assert (await control.store.latest(tmp_path)) == user_pause
