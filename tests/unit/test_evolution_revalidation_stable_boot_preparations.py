from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_stable_boot_preparations import (
    EvolutionRevalidationStableBootPreparationError,
    EvolutionRevalidationStableBootPreparationService,
    EvolutionRevalidationStableBootPreparationStore,
)
from naumi_agent.release.slots import ReleaseSlotError
from tests.unit.test_evolution_revalidation_stable_deployment_intents import (
    _context,
    _issue,
)
from tests.unit.test_evolution_revalidation_stable_deployment_intents import (
    _service as _intent_service,
)
from tests.unit.test_release_slots import _bundle

_BOOTABLE = b"#!/bin/sh\necho 'naumi 1.2.3'\n"


def _boot_service(context, intent_service, *, owner_id: str, clock):
    store = EvolutionRevalidationStableBootPreparationStore(
        intent_service.store.db_path,
        intent_service=intent_service,
        release_slot_store=context["slots"],
        clock=clock,
    )
    return EvolutionRevalidationStableBootPreparationService(
        workspace_root=intent_service.workspace_root,
        owner_id=owner_id,
        intent_service=intent_service,
        store=store,
        clock=clock,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot probe fixture 使用 POSIX shebang")
async def test_stable_boot_preparation_is_claim_fenced_and_dynamically_revoked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _context(
        tmp_path,
        monkeypatch,
        candidate_backend_content=_BOOTABLE,
    )
    members = [item.payload.member_id for item in context["credentials"]]
    intent_service = _intent_service(context, members[0])
    intent = await _issue(intent_service, context, members[0])
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
            owner_id=f"stable-boot-{index}",
            clock=clock,
        )
        for index in range(4)
    )
    views = await asyncio.gather(
        *(service.prepare(intent_id=intent.intent.intent_id) for service in services)
    )
    view = views[0]
    assert all(item == view for item in views)
    assert calls == 1
    assert view.stable_activation_input_authority
    assert view.preparation.boot_executed
    assert view.preparation.probe_process_started
    assert not view.preparation.user_process_started
    assert not view.preparation.active_pointer_switched
    assert not view.preparation.activation_authority
    assert not view.preparation.deployment_receipt_authority
    assert not view.preparation.stable_rollout_authority
    assert context["slots"].active() == context["pointer"]

    async with aiosqlite.connect(services[0].store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_boot_preparations "
            "SET preparation_json = replace(preparation_json, ?, ?) "
            "WHERE preparation_id = ?",
            (
                view.preparation.preparation_sha256,
                "0" * 64,
                view.preparation.preparation_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationStableBootPreparationError) as corrupt:
        await services[0].inspect(intent_id=intent.intent.intent_id)
    assert corrupt.value.code == "stable_boot_preparation_source_invalid"

    second_intent_service = _intent_service(context, members[1])
    second_intent = await _issue(second_intent_service, context, members[1])
    boot_now[0] = context["issue_at"] + timedelta(seconds=1)
    crashed = _boot_service(
        context,
        second_intent_service,
        owner_id="crashed-stable-boot",
        clock=clock,
    )
    first_claim = await crashed.store.acquire_claim(
        second_intent.intent,
        owner_id=crashed.owner_id,
        lease_seconds=2,
    )
    assert first_claim.epoch == 1
    boot_now[0] += timedelta(seconds=3)
    recovery = _boot_service(
        context,
        second_intent_service,
        owner_id="recovered-stable-boot",
        clock=clock,
    )
    recovered = await recovery.prepare(intent_id=second_intent.intent.intent_id)
    assert recovered.preparation.claim_epoch == 2
    assert recovered.preparation.claim_owner_id == recovery.owner_id
    assert recovered.stable_activation_input_authority

    third_intent_service = _intent_service(context, members[2])
    third_intent = await _issue(third_intent_service, context, members[2])
    failing = _boot_service(
        context,
        third_intent_service,
        owner_id="failing-stable-boot",
        clock=clock,
    )

    def timeout_probe(*_args, **_kwargs):
        raise ReleaseSlotError("release_slot_boot_timeout", "probe timed out")

    context["slots"].verify_bootable = timeout_probe
    with pytest.raises(EvolutionRevalidationStableBootPreparationError) as timeout:
        await failing.prepare(intent_id=third_intent.intent.intent_id)
    assert timeout.value.code == "stable_boot_probe_timeout"
    assert await failing.store.get_by_intent(third_intent.intent.intent_id) is None

    async with aiosqlite.connect(failing.store.db_path) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM "
                "evolution_revalidation_stable_boot_preparation_claims "
                "WHERE intent_id = ?",
                (third_intent.intent.intent_id,),
            )
        ).fetchone()
    assert row == (0,)

    fourth_intent_service = _intent_service(context, members[3])
    fourth_intent = await _issue(fourth_intent_service, context, members[3])
    drifting = _boot_service(
        context,
        fourth_intent_service,
        owner_id="drifting-stable-boot",
        clock=clock,
    )
    replacement = context["slots"].install(
        _bundle(
            tmp_path / "stable-pointer-drift",
            version="1.2.1",
            output_name="stable-pointer-drift-release",
            source_commit="d" * 40,
            source_tree_sha256="c" * 64,
        ),
        installed_at=(context["now"] + timedelta(seconds=8)).isoformat(),
    )
    original_probe(replacement.slot_id)

    def drifting_probe(*args, **kwargs):
        receipt = original_probe(*args, **kwargs)
        context["slots"].activate(
            replacement.slot_id,
            activated_at=(context["now"] + timedelta(seconds=9)).isoformat(),
            _expected_pointer_sha256=context["pointer"].pointer_sha256,
        )
        return receipt

    context["slots"].verify_bootable = drifting_probe
    with pytest.raises(EvolutionRevalidationStableBootPreparationError) as changed:
        await drifting.prepare(intent_id=fourth_intent.intent.intent_id)
    assert changed.value.code == "stable_boot_intent_denied"
    assert await drifting.store.get_by_intent(fourth_intent.intent.intent_id) is None

    boot_now[0] = datetime.fromisoformat(second_intent.intent.expires_at)
    expired = await recovery.inspect(intent_id=second_intent.intent.intent_id)
    assert expired.expired
    assert not expired.stable_activation_input_authority
    assert await context["lifecycle"].close()
