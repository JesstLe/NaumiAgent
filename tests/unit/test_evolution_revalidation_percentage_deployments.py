from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_percentage_deployments import (
    EvolutionRevalidationPercentageDeploymentError,
    EvolutionRevalidationPercentageDeploymentService,
    EvolutionRevalidationPercentageDeploymentStore,
)
from tests.unit.test_evolution_revalidation_percentage_boot_preparations import (
    _BOOTABLE,
    _boot_service,
)
from tests.unit.test_evolution_revalidation_percentage_deployment_intents import (
    _context,
    _intent_service,
    _issue,
)


async def _deployment_context(tmp_path: Path):
    context = await _context(tmp_path, candidate_backend_content=_BOOTABLE)
    intent_service = _intent_service(context)
    intent_view = await _issue(intent_service, context)
    now = [context["issue_at"] + timedelta(seconds=1)]

    def clock():
        return now[0]

    preparation_service = _boot_service(
        context,
        intent_service,
        owner_id="percentage-deployment-boot",
        clock=clock,
    )
    preparation_view = await preparation_service.prepare(
        assignment_id=intent_view.intent.assignment.assignment_id
    )

    def build_service():
        store = EvolutionRevalidationPercentageDeploymentStore(
            preparation_service.store.db_path,
            preparation_store=preparation_service.store,
            release_slot_store=context["slots"],
        )
        service = EvolutionRevalidationPercentageDeploymentService(
            workspace_root=context["assignment_service"].workspace_root,
            preparation_service=preparation_service,
            store=store,
            clock=clock,
        )
        return service, store

    return context, intent_view, preparation_view, now, build_service


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_deployers_share_one_preparation_bound_activation(
    tmp_path: Path,
) -> None:
    context, intent_view, preparation_view, _now, build = (
        await _deployment_context(tmp_path)
    )
    first, first_store = build()
    second, _second_store = build()
    assignment_id = intent_view.intent.assignment.assignment_id

    views = await asyncio.gather(
        *(
            (first if index % 2 == 0 else second).deploy(
                assignment_id=assignment_id
            )
            for index in range(8)
        )
    )
    view = views[0]

    assert all(item == view for item in views)
    assert view.deployment_fact_authority
    assert view.active_deployment_authority
    assert view.percentage_runtime_launch_input_authority
    assert view.receipt.population_assignment_enforced
    assert view.receipt.local_installation_deployed
    assert view.receipt.active_pointer_switched
    assert view.receipt.old_slot_retained
    assert not view.receipt.process_started
    assert not view.receipt.user_process_started
    assert not view.receipt.percentage_exposure_observed
    assert not view.receipt.percentage_rollout_authority
    pointer = context["slots"].active()
    assert pointer == view.receipt.activated_pointer
    assert pointer is not None
    assert pointer.generation == intent_view.intent.expected_activation_generation
    assert pointer.activation_authority is not None
    assert pointer.activation_authority.kind == (
        "evolution_percentage_boot_preparation"
    )
    assert (
        pointer.activation_authority.authority_id
        == preparation_view.preparation.preparation_id
    )
    assert (
        pointer.activation_authority.authority_sha256
        == preparation_view.preparation.preparation_sha256
    )
    assert await first_store.get_by_assignment(assignment_id) == view.receipt

    slot = intent_view.intent.archive_admission.installed_slot
    runtime = Path(slot.bundle_dir) / slot.backend_path
    runtime.chmod(0o700)
    runtime.write_bytes(b"#!/bin/sh\necho tampered\n")
    stale = await first.inspect(assignment_id=assignment_id)
    assert stale.active_pointer_current
    assert not stale.candidate_slot_current
    assert not stale.boot_receipt_current
    assert not stale.active_deployment_authority
    assert not stale.percentage_runtime_launch_input_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_deployment_reconciles_after_receipt_crash_and_expiry(
    tmp_path: Path,
) -> None:
    context, intent_view, _preparation_view, now, build = (
        await _deployment_context(tmp_path)
    )
    service, store = build()
    assignment_id = intent_view.intent.assignment.assignment_id
    original_record = store.record
    calls = 0

    async def fail_once(receipt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EvolutionRevalidationPercentageDeploymentError(
                "injected_percentage_receipt_failure",
                "测试注入：pointer 已切换但 Receipt 尚未写入。",
            )
        return await original_record(receipt)

    store.record = fail_once
    with pytest.raises(EvolutionRevalidationPercentageDeploymentError) as interrupted:
        await service.deploy(assignment_id=assignment_id)
    assert interrupted.value.code == "injected_percentage_receipt_failure"
    activated = context["slots"].active()
    assert activated is not None
    assert activated.current_slot_id == intent_view.intent.candidate_slot_id
    assert await store.get_by_assignment(assignment_id) is None

    rolled_back = context["slots"].rollback(
        activated_at=(context["issue_at"] + timedelta(seconds=2)).isoformat()
    )
    assert rolled_back == context["slots"].active()
    assert rolled_back.current_slot_id == intent_view.intent.previous_pointer.current_slot_id
    now[0] = datetime.fromisoformat(intent_view.intent.expires_at) + timedelta(seconds=1)
    store.record = original_record

    recovered = await service.reconcile(assignment_id=assignment_id)

    assert recovered.receipt.activated_pointer == activated
    assert recovered.deployment_fact_authority
    assert not recovered.active_pointer_current
    assert not recovered.active_deployment_authority
    assert not recovered.percentage_runtime_launch_input_authority
    assert context["slots"].get_activation_event(activated.generation) == activated


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_unbound_percentage_activation_cannot_be_claimed(
    tmp_path: Path,
) -> None:
    context, intent_view, _preparation_view, _now, build = (
        await _deployment_context(tmp_path)
    )
    assignment_id = intent_view.intent.assignment.assignment_id
    external = context["slots"].activate(
        intent_view.intent.candidate_slot_id,
        activated_at=(context["issue_at"] + timedelta(seconds=1)).isoformat(),
        _expected_pointer_sha256=(
            intent_view.intent.expected_previous_pointer_sha256
        ),
    )
    assert external.activation_authority is None

    service, store = build()
    with pytest.raises(EvolutionRevalidationPercentageDeploymentError) as conflict:
        await service.reconcile(assignment_id=assignment_id)

    assert conflict.value.code == "percentage_deployment_generation_conflict"
    assert await store.get_by_assignment(assignment_id) is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_deployment_source_tamper_revokes_fact(
    tmp_path: Path,
) -> None:
    _context_data, intent_view, preparation_view, _now, build = (
        await _deployment_context(tmp_path)
    )
    service, store = build()
    assignment_id = intent_view.intent.assignment.assignment_id
    deployed = await service.deploy(assignment_id=assignment_id)
    assert deployed.deployment_fact_authority

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_percentage_boot_preparations "
            "SET preparation_json = replace(preparation_json, ?, ?) "
            "WHERE preparation_id = ?",
            (
                preparation_view.preparation.preparation_sha256,
                "0" * 64,
                preparation_view.preparation.preparation_id,
            ),
        )
        await db.commit()

    revoked = await service.inspect(assignment_id=assignment_id)
    assert revoked.receipt_source_current
    assert not revoked.preparation_source_current
    assert not revoked.deployment_fact_authority
    assert not revoked.active_deployment_authority
    assert "preparation_source_changed" in revoked.invalidation_reasons
