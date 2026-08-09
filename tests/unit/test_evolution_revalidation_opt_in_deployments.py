from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_opt_in_deployments import (
    EvolutionRevalidationOptInDeploymentError,
    EvolutionRevalidationOptInDeploymentService,
    EvolutionRevalidationOptInDeploymentStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.release.build_attestations import (
    ReleaseTrustedBuilderKey,
    create_release_build_trust_policy,
)
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_opt_in_deployment_intents import (
    _intent_service,
)


async def _deployment_service(root: Path):
    fixture = await _intent_service(root)
    intent_service = fixture[0]
    completion = fixture[1]
    admission = fixture[2]
    slots = fixture[3]
    original_pointer = fixture[4]
    await intent_service.authorize(completion_id=completion.completion_id)
    store = EvolutionRevalidationOptInDeploymentStore(
        intent_service.store.db_path,
        intent_store=intent_service.store,
        release_slot_store=slots,
    )

    def build_service():
        return EvolutionRevalidationOptInDeploymentService(
            workspace_root=root,
            intent_service=intent_service,
            interaction_store=intent_service.interaction_store,
            store=store,
            clock=lambda: T0 + timedelta(hours=9, minutes=15, seconds=4),
        )

    return build_service, completion, admission, slots, original_pointer, store


@pytest.mark.asyncio
async def test_concurrent_deployers_share_one_authority_bound_activation(
    tmp_path: Path,
) -> None:
    build, completion, admission, slots, original_pointer, store = (
        await _deployment_service(tmp_path)
    )
    first = build()
    second = build()

    views = await asyncio.gather(
        *(
            (first if index % 2 == 0 else second).deploy(
                completion_id=completion.completion_id
            )
            for index in range(8)
        )
    )
    view = views[0]

    assert all(candidate == view for candidate in views)
    assert view.deployment_fact_authority
    assert view.active_deployment_authority
    assert view.receipt.local_installation_deployed
    assert view.receipt.active_pointer_switched
    assert view.receipt.old_slot_retained
    assert not view.receipt.process_started
    assert not view.receipt.population_assignment_enforced
    assert not view.receipt.percentage_rollout_authority
    assert not view.receipt.stable_rollout_authority
    pointer = slots.active()
    assert pointer == view.receipt.activated_pointer
    assert pointer is not None
    assert pointer.generation == original_pointer.generation + 1
    assert pointer.activation_authority is not None
    assert pointer.activation_authority.authority_id == view.receipt.intent.intent_id
    assert pointer.current_slot_id == admission.admission.candidate_slot.slot_id
    assert pointer.previous_slot_id == original_pointer.current_slot_id
    assert await store.get_by_completion(completion.completion_id) == view.receipt

    original_policy = first.intent_service.candidate_service.trust_policy_provider()
    original_key = original_policy.keys[0]
    revoked_policy = create_release_build_trust_policy(
        (
            ReleaseTrustedBuilderKey(
                identity=original_key.identity,
                state="revoked",
                valid_from=original_key.valid_from,
                valid_until=original_key.valid_until,
                revoked_at="2026-07-19T09:15:05+00:00",
            ),
        )
    )
    first.intent_service.candidate_service.trust_policy_provider = (
        lambda: revoked_policy
    )
    fenced = await first.inspect(completion_id=completion.completion_id)
    assert fenced.deployment_fact_authority
    assert fenced.active_pointer_current
    assert not fenced.build_trust_current
    assert not fenced.active_deployment_authority

    first.intent_service.candidate_service.trust_policy_provider = (
        lambda: original_policy
    )
    control_store = (
        first.intent_service.candidate_service.stage_advance_service.control_store
    )
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=tmp_path,
        store=control_store,
        control_plane_key_provider=control_store._key_provider,
    )
    await control.pause(
        reason_code="post_deployment_pause_test",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at="2026-07-19T09:15:06+00:00",
    )
    paused = await first.inspect(completion_id=completion.completion_id)
    assert paused.deployment_fact_authority
    assert paused.build_trust_current
    assert not paused.rollout_control_current
    assert not paused.active_deployment_authority

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_opt_in_deployment_intents "
            "WHERE intent_id = ?",
            (view.receipt.intent.intent_id,),
        )
    source_broken = await first.inspect(completion_id=completion.completion_id)
    assert not source_broken.intent_source_current
    assert not source_broken.deployment_fact_authority
    assert not source_broken.active_deployment_authority


@pytest.mark.asyncio
async def test_crash_after_activation_reconciles_historical_generation(
    tmp_path: Path,
) -> None:
    build, completion, admission, slots, original_pointer, store = (
        await _deployment_service(tmp_path)
    )
    service = build()
    original_record = store.record
    failures = 0

    async def fail_once(receipt):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise EvolutionRevalidationOptInDeploymentError(
                "injected_receipt_write_failure",
                "测试注入：pointer 已切换但 receipt 尚未写入。",
            )
        return await original_record(receipt)

    store.record = fail_once
    with pytest.raises(EvolutionRevalidationOptInDeploymentError) as interrupted:
        await service.deploy(completion_id=completion.completion_id)
    assert interrupted.value.code == "injected_receipt_write_failure"
    activated = slots.active()
    assert activated is not None
    assert activated.current_slot_id == admission.admission.candidate_slot.slot_id
    assert await store.get_by_completion(completion.completion_id) is None

    rolled_back = slots.rollback(
        activated_at="2026-07-19T09:15:05+00:00"
    )
    assert rolled_back.current_slot_id == original_pointer.current_slot_id
    store.record = original_record

    recovered = await service.reconcile(completion_id=completion.completion_id)

    assert recovered.receipt.activated_pointer == activated
    assert recovered.deployment_fact_authority
    assert not recovered.active_pointer_current
    assert not recovered.active_deployment_authority
    assert slots.get_activation_event(activated.generation) == activated


@pytest.mark.asyncio
async def test_unbound_matching_pointer_cannot_be_claimed_by_intent(
    tmp_path: Path,
) -> None:
    build, completion, admission, slots, _original_pointer, store = (
        await _deployment_service(tmp_path)
    )
    external = slots.activate(
        admission.admission.candidate_slot.slot_id,
        activated_at="2026-07-19T09:15:04+00:00",
    )
    assert external.activation_authority is None

    with pytest.raises(EvolutionRevalidationOptInDeploymentError) as conflict:
        await build().reconcile(completion_id=completion.completion_id)

    assert conflict.value.code == "opt_in_deployment_generation_conflict"
    assert await store.get_by_completion(completion.completion_id) is None
