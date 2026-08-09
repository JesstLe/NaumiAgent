from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_percentage_boot_preparations import (
    EvolutionRevalidationPercentageBootPreparationError,
    EvolutionRevalidationPercentageBootPreparationService,
    EvolutionRevalidationPercentageBootPreparationStore,
)
from tests.unit.test_evolution_revalidation_percentage_deployment_intents import (
    _context,
    _intent_service,
    _issue,
)
from tests.unit.test_release_slots import _bundle

_BOOTABLE = b"#!/bin/sh\necho 'naumi 1.2.3'\n"


def _boot_service(
    context,
    intent_service,
    *,
    owner_id: str,
    clock,
    timeout_seconds: int = 20,
):
    store = EvolutionRevalidationPercentageBootPreparationStore(
        intent_service.store.db_path,
        intent_service=intent_service,
        release_slot_store=context["slots"],
        clock=clock,
    )
    return EvolutionRevalidationPercentageBootPreparationService(
        workspace_root=context["assignment_service"].workspace_root,
        owner_id=owner_id,
        intent_service=intent_service,
        store=store,
        boot_timeout_seconds=timeout_seconds,
        clock=clock,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_boot_preparation_claims_once_and_dynamically_revokes(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path, candidate_backend_content=_BOOTABLE)
    intent_service = _intent_service(context)
    intent = await _issue(intent_service, context)
    boot_now = [context["issue_at"] + timedelta(seconds=1)]
    clock_ticks = [0]

    def clock():
        current = boot_now[0] + timedelta(microseconds=clock_ticks[0])
        clock_ticks[0] += 1
        return current

    original_probe = context["slots"].verify_bootable
    calls = 0
    calls_lock = threading.Lock()

    def counted_probe(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return original_probe(*args, **kwargs)

    context["slots"].verify_bootable = counted_probe
    services = tuple(
        _boot_service(
            context,
            intent_service,
            owner_id=f"percentage-boot-{index}",
            clock=clock,
        )
        for index in range(4)
    )
    views = await asyncio.gather(
        *(
            service.prepare(assignment_id=intent.intent.assignment.assignment_id)
            for service in services
        )
    )
    view = views[0]
    assert all(item == view for item in views)
    assert calls == 1
    assert view.percentage_activation_input_authority
    assert view.preparation.boot_executed
    assert view.preparation.probe_process_started
    assert not view.preparation.user_process_started
    assert not view.preparation.active_pointer_switched
    assert not view.preparation.activation_authority
    assert not view.preparation.deployment_receipt_authority
    assert not view.preparation.percentage_rollout_authority
    assert context["slots"].active() == context["previous_pointer"]

    boot_now[0] = datetime.fromisoformat(intent.intent.expires_at)
    expired = await services[0].inspect(intent_id=intent.intent.intent_id)
    assert expired.expired
    assert not expired.percentage_activation_input_authority
    assert "deployment_intent_expired" in expired.invalidation_reasons

    boot_now[0] = context["issue_at"] + timedelta(seconds=2)
    slot = intent.intent.archive_admission.installed_slot
    runtime = Path(slot.bundle_dir) / slot.backend_path
    runtime.chmod(0o700)
    runtime.write_bytes(b"#!/bin/sh\necho tampered\n")
    stale = await services[0].inspect(intent_id=intent.intent.intent_id)
    assert not stale.candidate_slot_current
    assert not stale.boot_receipt_current
    assert not stale.percentage_activation_input_authority

    async with aiosqlite.connect(services[0].store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_percentage_boot_preparations "
            "SET preparation_json = replace(preparation_json, ?, ?) "
            "WHERE preparation_id = ?",
            (
                view.preparation.preparation_sha256,
                "0" * 64,
                view.preparation.preparation_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationPercentageBootPreparationError) as corrupt:
        await services[0].inspect(intent_id=intent.intent.intent_id)
    assert corrupt.value.code == "percentage_boot_preparation_source_invalid"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_boot_preparation_recovers_expired_claim_and_fences_pointer(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path, candidate_backend_content=_BOOTABLE)
    intent_service = _intent_service(context)
    intent = await _issue(intent_service, context)
    boot_now = [context["issue_at"] + timedelta(seconds=1)]

    def clock():
        return boot_now[0]

    crashed = _boot_service(
        context,
        intent_service,
        owner_id="crashed-percentage-boot",
        clock=clock,
    )
    first_claim = await crashed.store.acquire_claim(
        intent.intent,
        owner_id=crashed.owner_id,
        lease_seconds=2,
    )
    assert first_claim.epoch == 1
    boot_now[0] += timedelta(seconds=3)
    recovery = _boot_service(
        context,
        intent_service,
        owner_id="recovered-percentage-boot",
        clock=clock,
    )
    prepared = await recovery.prepare(
        assignment_id=intent.intent.assignment.assignment_id
    )
    assert prepared.preparation.claim_epoch == 2
    assert prepared.preparation.claim_owner_id == recovery.owner_id
    assert prepared.percentage_activation_input_authority

    next_baseline = context["slots"].install(
        _bundle(
            tmp_path / "pointer-drift",
            version="1.2.4",
            output_name="pointer-drift-release",
            source_commit="8" * 40,
            source_tree_sha256="7" * 64,
        )
    )
    context["slots"].verify_bootable(next_baseline.slot_id)
    context["slots"].activate(next_baseline.slot_id)
    drifted = await recovery.inspect(intent_id=intent.intent.intent_id)
    assert not drifted.previous_pointer_current
    assert not drifted.percentage_activation_input_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
@pytest.mark.parametrize(
    ("backend", "timeout_seconds", "expected_code"),
    [
        (
            b"#!/bin/sh\necho 'naumi wrong-version'\nexit 1\n",
            2,
            "percentage_boot_probe_failed",
        ),
        (
            b"#!/bin/sh\nsleep 2\necho 'naumi 1.2.3'\n",
            1,
            "percentage_boot_probe_timeout",
        ),
    ],
)
async def test_percentage_boot_preparation_fails_closed_and_releases_claim(
    tmp_path: Path,
    backend: bytes,
    timeout_seconds: int,
    expected_code: str,
) -> None:
    context = await _context(tmp_path, candidate_backend_content=backend)
    intent_service = _intent_service(context)
    intent = await _issue(intent_service, context)
    boot_now = context["issue_at"] + timedelta(seconds=1)

    def clock():
        return boot_now

    service = _boot_service(
        context,
        intent_service,
        owner_id="failing-percentage-boot",
        clock=clock,
        timeout_seconds=timeout_seconds,
    )
    with pytest.raises(EvolutionRevalidationPercentageBootPreparationError) as failed:
        await service.prepare(assignment_id=intent.intent.assignment.assignment_id)
    assert failed.value.code == expected_code
    assert await service.store.get_by_intent(intent.intent.intent_id) is None
    assert context["slots"].active() == context["previous_pointer"]
    async with aiosqlite.connect(service.store.db_path) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM "
                "evolution_revalidation_percentage_boot_preparation_claims "
                "WHERE intent_id = ?",
                (intent.intent.intent_id,),
            )
        ).fetchone()
    assert row == (0,)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe 夹具使用 POSIX shebang")
async def test_percentage_boot_preparation_rechecks_pointer_after_probe(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path, candidate_backend_content=_BOOTABLE)
    intent_service = _intent_service(context)
    intent = await _issue(intent_service, context)
    boot_now = context["issue_at"] + timedelta(seconds=1)

    def clock():
        return boot_now

    next_baseline = context["slots"].install(
        _bundle(
            tmp_path / "during-probe-drift",
            version="1.2.4",
            output_name="during-probe-drift-release",
            source_commit="6" * 40,
            source_tree_sha256="5" * 64,
        )
    )
    context["slots"].verify_bootable(next_baseline.slot_id)
    original_probe = context["slots"].verify_bootable

    def drifting_probe(*args, **kwargs):
        receipt = original_probe(*args, **kwargs)
        context["slots"].activate(next_baseline.slot_id)
        return receipt

    context["slots"].verify_bootable = drifting_probe
    service = _boot_service(
        context,
        intent_service,
        owner_id="drifting-percentage-boot",
        clock=clock,
    )
    with pytest.raises(EvolutionRevalidationPercentageBootPreparationError) as changed:
        await service.prepare(assignment_id=intent.intent.assignment.assignment_id)
    assert changed.value.code == "percentage_boot_intent_denied"
    assert await service.store.get_by_intent(intent.intent.intent_id) is None
