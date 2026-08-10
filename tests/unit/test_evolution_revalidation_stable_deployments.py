from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_stable_deployments import (
    EvolutionRevalidationStableDeploymentError,
    EvolutionRevalidationStableDeploymentService,
    EvolutionRevalidationStableDeploymentStore,
)
from tests.unit.test_evolution_revalidation_stable_boot_preparations import (
    _BOOTABLE,
    _boot_service,
)
from tests.unit.test_evolution_revalidation_stable_deployment_intents import (
    _context,
    _issue,
)
from tests.unit.test_evolution_revalidation_stable_deployment_intents import (
    _service as _intent_service,
)


async def _prepare(context, member_index: int, *, offset: int, clock, owner_id: str):
    member_id = context["credentials"][member_index].payload.member_id
    intent_service = _intent_service(context, member_id, offset=offset)
    intent_view = await _issue(intent_service, context, member_id)
    preparation_service = _boot_service(
        context,
        intent_service,
        owner_id=owner_id,
        clock=clock,
    )
    preparation_view = await preparation_service.prepare(
        intent_id=intent_view.intent.intent_id
    )
    return intent_view, preparation_service, preparation_view


def _deployment_service(context, preparation_service, clock):
    store = EvolutionRevalidationStableDeploymentStore(
        preparation_service.store.db_path,
        preparation_store=preparation_service.store,
        release_slot_store=context["slots"],
    )
    service = EvolutionRevalidationStableDeploymentService(
        workspace_root=preparation_service.workspace_root,
        preparation_service=preparation_service,
        store=store,
        clock=clock,
    )
    return service, store


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe fixture 使用 POSIX shebang")
async def test_stable_activation_converges_and_recovers_receipt_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _context(
        tmp_path,
        monkeypatch,
        candidate_backend_content=_BOOTABLE,
    )
    now = [context["issue_at"] + timedelta(seconds=1)]

    def clock():
        return now[0]

    first_intent, first_preparation_service, first_preparation = await _prepare(
        context,
        0,
        offset=0,
        clock=clock,
        owner_id="stable-deployment-boot-1",
    )
    first, first_store = _deployment_service(
        context,
        first_preparation_service,
        clock,
    )
    second, _ = _deployment_service(context, first_preparation_service, clock)
    intent_id = first_intent.intent.intent_id
    views = await asyncio.gather(
        *(
            (first if index % 2 == 0 else second).deploy(intent_id=intent_id)
            for index in range(8)
        )
    )
    deployed = views[0]

    assert all(item == deployed for item in views)
    assert deployed.deployment_fact_authority
    assert deployed.active_deployment_authority
    assert deployed.stable_runtime_launch_input_authority
    assert deployed.receipt.population_membership_enforced
    assert deployed.receipt.proof_of_possession_enforced
    assert deployed.receipt.local_installation_deployed
    assert deployed.receipt.active_pointer_switched
    assert deployed.receipt.old_slot_retained
    assert not deployed.receipt.process_started
    assert not deployed.receipt.user_process_started
    assert not deployed.receipt.stable_installation_exposure_observed
    assert not deployed.receipt.percentage_rollout_authority
    assert not deployed.receipt.stable_rollout_authority
    pointer = context["slots"].active()
    assert pointer == deployed.receipt.activated_pointer
    assert pointer is not None
    assert pointer.activation_authority is not None
    assert pointer.activation_authority.kind == "evolution_stable_boot_preparation"
    assert pointer.activation_authority.authority_id == (
        first_preparation.preparation.preparation_id
    )
    assert await first_store.get_by_intent(intent_id) == deployed.receipt

    rolled_back = context["slots"].rollback(
        activated_at=(context["issue_at"] + timedelta(seconds=2)).isoformat()
    )
    assert rolled_back.current_slot_id == first_intent.intent.previous_pointer.current_slot_id

    now[0] = context["issue_at"] + timedelta(seconds=4)
    second_intent, second_preparation_service, _ = await _prepare(
        context,
        1,
        offset=3_000_000,
        clock=clock,
        owner_id="stable-deployment-boot-2",
    )
    recovering, recovering_store = _deployment_service(
        context,
        second_preparation_service,
        clock,
    )
    original_record = recovering_store.record
    calls = 0

    async def fail_once(receipt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EvolutionRevalidationStableDeploymentError(
                "injected_stable_receipt_failure",
                "测试注入：pointer 已切换但 Stable Receipt 尚未写入。",
            )
        return await original_record(receipt)

    recovering_store.record = fail_once
    second_intent_id = second_intent.intent.intent_id
    with pytest.raises(EvolutionRevalidationStableDeploymentError) as interrupted:
        await recovering.deploy(intent_id=second_intent_id)
    assert interrupted.value.code == "injected_stable_receipt_failure"
    activated = context["slots"].active()
    assert activated is not None
    assert activated.current_slot_id == second_intent.intent.candidate_slot_id
    assert await recovering_store.get_by_intent(second_intent_id) is None

    context["slots"].rollback(
        activated_at=(context["issue_at"] + timedelta(seconds=5)).isoformat()
    )
    now[0] = datetime.fromisoformat(second_intent.intent.expires_at) + timedelta(seconds=1)
    recovering_store.record = original_record
    recovered = await recovering.reconcile(intent_id=second_intent_id)

    assert recovered.receipt.activated_pointer == activated
    assert recovered.deployment_fact_authority
    assert not recovered.active_pointer_current
    assert not recovered.active_deployment_authority
    assert not recovered.intent_unexpired
    assert not recovered.stable_runtime_launch_input_authority
    assert context["slots"].get_activation_event(activated.generation) == activated

    async with aiosqlite.connect(first_store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_boot_preparations "
            "SET preparation_json = replace(preparation_json, ?, ?) "
            "WHERE preparation_id = ?",
            (
                first_preparation.preparation.preparation_sha256,
                "0" * 64,
                first_preparation.preparation.preparation_id,
            ),
        )
        await db.commit()
    revoked = await first.inspect(intent_id=intent_id)
    assert revoked.receipt_source_current
    assert not revoked.preparation_source_current
    assert not revoked.deployment_fact_authority
    assert "preparation_source_changed" in revoked.invalidation_reasons
    assert await context["lifecycle"].close()
