from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
    EvolutionRevalidationRolloutEntryStatus,
    EvolutionRevalidationRolloutStageEntryError,
    EvolutionRevalidationRolloutStageEntryService,
    EvolutionRevalidationRolloutStageEntryStore,
)
from tests.unit.test_evolution_revalidation_approval_decisions import _keypair
from tests.unit.test_evolution_revalidation_rollout_plans import _approved

CONTROL_KEY = b"rollout-stage-entry-control-key-v1!"
T0 = datetime.fromisoformat("2026-07-19T01:00:00+00:00")


def _services(root, plan_service, *, now):
    db_path = plan_service.store.db_path
    control_store = EvolutionRevalidationRolloutControlStore(
        db_path,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    control_service = EvolutionRevalidationRolloutControlService(
        workspace_root=root,
        store=control_store,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    entry_store = EvolutionRevalidationRolloutStageEntryStore(
        db_path,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    entry_service = EvolutionRevalidationRolloutStageEntryService(
        workspace_root=root,
        plan_service=plan_service,
        control_store=control_store,
        store=entry_store,
        control_plane_key_provider=lambda: CONTROL_KEY,
        now=lambda: now,
    )
    return control_service, entry_service


@pytest.mark.asyncio
async def test_local_canary_entry_is_singleflight_and_kill_switch_fenced(
    tmp_path: Path,
) -> None:
    contract, approved, principals, principal_service, plan_service = await _approved(
        tmp_path
    )
    plan = await plan_service.issue(
        contract_id=contract.contract_id,
        decision_id=approved.receipt.decision_id,
    )
    service_pairs = tuple(
        _services(
            tmp_path,
            plan_service,
            now=(T0 + timedelta(microseconds=index)).isoformat(),
        )
        for index in range(8)
    )

    entries = await asyncio.gather(*(
        entry_service.issue(plan_id=plan.plan.plan_id)
        for _control_service, entry_service in service_pairs
    ))
    entry = entries[0]

    assert len({item.receipt.receipt_sha256 for item in entries}) == 1
    assert entry.status is EvolutionRevalidationRolloutEntryStatus.CURRENT
    assert entry.can_execute_local_canary
    assert entry.receipt.stage == "local_canary"
    assert entry.receipt.allowed_operations == (
        "materialize_immutable_green",
        "run_local_canary",
        "record_canary_observation",
    )
    assert entry.receipt.workspace_mode == "read_only"
    assert entry.receipt.network_mode == "deny"
    assert not entry.receipt.opt_in_authority
    assert not entry.receipt.percentage_authority
    assert not entry.receipt.stable_authority
    assert not entry.receipt.git_write_authority
    assert not entry.receipt.publish_authority

    control_service, entry_service = service_pairs[0]
    paused = await control_service.pause(
        reason_code="operator_emergency_stop",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(T0 + timedelta(seconds=1)).isoformat(),
    )
    assert paused.state is EvolutionRevalidationRolloutControlState.PAUSED
    fenced = await entry_service.inspect(
        receipt_id=entry.receipt.receipt_id,
        assessed_at=(T0 + timedelta(seconds=2)).isoformat(),
    )
    assert fenced.status is EvolutionRevalidationRolloutEntryStatus.FENCED
    assert not fenced.can_execute_local_canary
    assert fenced.fenced_reason == "kill_switch_generation_changed"
    with pytest.raises(EvolutionRevalidationRolloutStageEntryError) as stopped:
        await entry_service.issue(plan_id=plan.plan.plan_id)
    assert stopped.value.code == "rollout_stage_entry_kill_switch_paused"
    with pytest.raises(EvolutionRevalidationRolloutStageEntryError) as auto_resume:
        await control_service.resume(
            reason_code="monitor_attempted_resume",
            actor=EvolutionRevalidationRolloutControlActor.MONITOR,
            changed_at=(T0 + timedelta(seconds=2)).isoformat(),
        )
    assert auto_resume.value.code == "rollout_control_resume_actor_forbidden"

    resumed = await control_service.resume(
        reason_code="operator_incident_cleared",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(T0 + timedelta(seconds=3)).isoformat(),
    )
    assert resumed.state is EvolutionRevalidationRolloutControlState.ACTIVE
    recovered_service = _services(
        tmp_path,
        plan_service,
        now=(T0 + timedelta(seconds=4)).isoformat(),
    )[1]
    recovered = await recovered_service.issue(plan_id=plan.plan.plan_id)
    assert recovered.status is EvolutionRevalidationRolloutEntryStatus.CURRENT
    assert recovered.receipt.attempt == 2
    assert recovered.receipt.previous_receipt_id == entry.receipt.receipt_id
    assert recovered.receipt.control_sequence == resumed.sequence

    _private, public = _keypair()
    await principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principals[0].principal_id,
        public_key_base64=public,
    )
    stale = await recovered_service.inspect(
        receipt_id=recovered.receipt.receipt_id,
        assessed_at=(T0 + timedelta(seconds=5)).isoformat(),
    )
    assert stale.status is EvolutionRevalidationRolloutEntryStatus.PLAN_STALE
    assert not stale.plan_current
    assert not stale.can_execute_local_canary

    with sqlite3.connect(recovered_service.store.db_path) as db:
        row = db.execute(
            "SELECT event_json FROM evolution_revalidation_rollout_control_events "
            "WHERE event_id = ?",
            (resumed.event_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["control_plane_attestation_sha256"] = "0" * 64
        db.execute(
            "UPDATE evolution_revalidation_rollout_control_events "
            "SET event_json = ? WHERE event_id = ?",
            (json.dumps(payload), resumed.event_id),
        )
    with pytest.raises(EvolutionRevalidationRolloutStageEntryError) as tampered:
        await recovered_service.control_store.latest(tmp_path)
    assert tampered.value.code == "rollout_control_plane_attestation_invalid"


@pytest.mark.asyncio
async def test_entry_expires_without_granting_later_stage_authority(
    tmp_path: Path,
) -> None:
    contract, approved, _principals, _principal_service, plan_service = await _approved(
        tmp_path
    )
    plan = await plan_service.issue(
        contract_id=contract.contract_id,
        decision_id=approved.receipt.decision_id,
    )
    _control, service = _services(tmp_path, plan_service, now=T0.isoformat())
    entry = await service.issue(plan_id=plan.plan.plan_id)

    expired = await service.inspect(
        receipt_id=entry.receipt.receipt_id,
        assessed_at=entry.receipt.expires_at,
    )

    assert expired.status is EvolutionRevalidationRolloutEntryStatus.EXPIRED
    assert expired.plan_current
    assert expired.control_current
    assert not expired.can_execute_local_canary
    assert not expired.receipt.opt_in_authority
    assert not expired.receipt.percentage_authority
    assert not expired.receipt.stable_authority
