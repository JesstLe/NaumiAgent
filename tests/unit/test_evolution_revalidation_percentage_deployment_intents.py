from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.evolution.revalidation_percentage_cohort_assignments import (
    EvolutionRevalidationPercentageCohortAssignmentService,
    EvolutionRevalidationPercentageCohortAssignmentStore,
    _selection,
)
from naumi_agent.evolution.revalidation_percentage_deployment_intents import (
    EvolutionRevalidationPercentageDeploymentIntentError,
    EvolutionRevalidationPercentageDeploymentIntentService,
    EvolutionRevalidationPercentageDeploymentIntentStore,
)
from naumi_agent.release.archive_admission import (
    ReleaseArchiveAdmissionService,
)
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistrySigner,
    ReleasePopulationSnapshotStore,
    ReleaseTrustedPopulationRegistryKey,
    create_release_population_trust_policy,
)
from naumi_agent.release.slots import host_release_target
from tests.unit.test_evolution_revalidation_opt_in_stage_advances import _fixture
from tests.unit.test_release_archive_admission import _runtime
from tests.unit.test_release_slots import _bundle


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


async def _context(tmp_path: Path):
    passing, _declined, plan, _control, build_advance, now = await _fixture(tmp_path)
    advance_service = build_advance("advance", [])
    advance = await advance_service.authorize(
        evidence_id=passing.evidence_id,
        subject_id=passing.subject_id,
    )

    registry = ReleasePopulationRegistrySigner.from_private_keys_base64(
        registry_id="naumi-production-registry",
        key_id="population-2026-01",
        key_generation=1,
        signing_private_key_base64=_b64(bytes(range(32))),
        pseudonym_key_base64=_b64(bytes(reversed(range(32)))),
    )
    keys: dict[str, Ed25519PrivateKey] = {}
    credentials = []
    for index in range(8):
        seed = hashlib.sha256(f"percentage-intent-installation-{index}".encode()).digest()
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        credential = registry.issue_credential(
            installation_public_key_base64=_b64(
                private_key.public_key().public_bytes_raw()
            ),
            channel="stable",
            registered_at=(now - timedelta(days=1)).isoformat(),
            expires_at=(now + timedelta(days=30)).isoformat(),
        )
        keys[credential.payload.member_id] = private_key
        credentials.append(credential)
    snapshot = registry.issue_snapshot(
        channel="stable",
        credentials=tuple(credentials),
        previous=None,
        generated_at=now.isoformat(),
        valid_from=(now + timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
    )
    trust = create_release_population_trust_policy(
        (
            ReleaseTrustedPopulationRegistryKey(
                identity=registry.identity,
                state="active",
                valid_from=(now - timedelta(days=2)).isoformat(),
            ),
        )
    )
    population_clock = [now + timedelta(seconds=2)]
    population_store = ReleasePopulationSnapshotStore(
        tmp_path / ".naumi" / "population.db",
        trust_policy_provider=lambda: trust,
        clock=lambda: population_clock[0],
    )
    await population_store.record(snapshot)
    selections = {
        member_id: _selection(plan, snapshot, member_id) for member_id in keys
    }
    selected_member = next(
        member_id for member_id, value in selections.items() if value["member_selected"]
    )
    unselected_member = next(
        member_id for member_id, value in selections.items() if not value["member_selected"]
    )

    async def sign(payload: bytes) -> str:
        return _b64(keys[selected_member].sign(payload))

    assignment_store = EvolutionRevalidationPercentageCohortAssignmentStore(
        advance_service.store.db_path,
        advance_service=advance_service,
        population_store=population_store,
        plan_service=advance_service.plan_service,
    )
    assignment_service = EvolutionRevalidationPercentageCohortAssignmentService(
        workspace_root=tmp_path,
        channel="stable",
        advance_service=advance_service,
        population_store=population_store,
        plan_service=advance_service.plan_service,
        store=assignment_store,
        sign_assignment_challenge=sign,
        clock=lambda: now + timedelta(seconds=3),
    )
    assignment = await assignment_service.assign(
        stage_completion_evidence_id=passing.evidence_id,
        member_id=selected_member,
    )

    target = host_release_target()
    release = await _runtime(
        tmp_path / "release",
        archive_format="tar.gz",
        target=target,
        source_commit=plan.target_head,
        source_tree_sha256=plan.target_tree_sha256,
        base_time=now,
    )
    slots = release["slot_store"]
    baseline = slots.install(
        _bundle(
            tmp_path / "baseline",
            version="1.2.2",
            output_name="baseline-release",
            source_commit="f" * 40,
            source_tree_sha256="e" * 64,
        ),
        installed_at=(now + timedelta(seconds=1)).isoformat(),
    )
    slots.verify_bootable(
        baseline.slot_id,
        checked_at=(now + timedelta(seconds=2)).isoformat(),
    )
    previous_pointer = slots.activate(
        baseline.slot_id,
        activated_at=(now + timedelta(seconds=3)).isoformat(),
    )
    archive_service = ReleaseArchiveAdmissionService(
        staging_root=release["staging_root"],
        fetch_service=release["fetch_service"],
        slot_store=slots,
        store=release["admission_store"],
        clock=lambda: now + timedelta(seconds=4),
    )
    admission = await archive_service.admit(
        download_source_id=release["download"].receipt.source_id
    )
    issue_at = max(
        now + timedelta(seconds=4),
        datetime.fromisoformat(admission.receipt.admitted_at) + timedelta(seconds=1),
    )
    assert issue_at < datetime.fromisoformat(advance.receipt.expires_at)

    target_state = [target]

    def target_provider():
        return target_state[0]

    return {
        "passing": passing,
        "plan": plan,
        "now": now,
        "issue_at": issue_at,
        "advance": advance,
        "snapshot": snapshot,
        "population_clock": population_clock,
        "registry": registry,
        "credentials": credentials,
        "selected_member": selected_member,
        "unselected_member": unselected_member,
        "keys": keys,
        "assignment": assignment,
        "assignment_service": assignment_service,
        "archive_service": archive_service,
        "admission": admission,
        "download_source_id": release["download"].receipt.source_id,
        "slots": slots,
        "previous_pointer": previous_pointer,
        "target_provider": target_provider,
        "target_state": target_state,
    }


def _intent_service(context, *, offset_microseconds: int = 0):
    def clock():
        return context["issue_at"] + timedelta(microseconds=offset_microseconds)

    store = EvolutionRevalidationPercentageDeploymentIntentStore(
        context["assignment_service"].store.db_path,
        assignment_service=context["assignment_service"],
        archive_service=context["archive_service"],
        installation_target_provider=context["target_provider"],
        clock=clock,
    )
    return EvolutionRevalidationPercentageDeploymentIntentService(
        workspace_root=context["assignment_service"].workspace_root,
        assignment_service=context["assignment_service"],
        archive_service=context["archive_service"],
        store=store,
        installation_target_provider=context["target_provider"],
        clock=clock,
    )


async def _issue(service, context, *, member_id: str | None = None):
    return await service.issue(
        advance_receipt_id=context["advance"].receipt.receipt_id,
        snapshot_id=context["snapshot"].snapshot_id,
        member_id=member_id or context["selected_member"],
        download_source_id=context["download_source_id"],
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot baseline 夹具使用 POSIX shebang")
async def test_percentage_deployment_intent_is_one_time_short_lived_and_fenced(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path)
    services = tuple(_intent_service(context, offset_microseconds=index) for index in range(4))
    views = await asyncio.gather(*(_issue(service, context) for service in services))
    view = views[0]
    assert all(item == view for item in views)
    assert view.deployment_intent_authority
    assert view.intent.one_time_issuance
    assert not view.intent.explicit_user_prompt_required
    assert view.intent.boot_required and not view.intent.boot_executed
    assert not view.intent.activation_intent_authority
    assert not view.intent.active_pointer_switched
    assert not view.intent.deployment_receipt_authority
    assert not view.intent.percentage_rollout_authority
    assert context["slots"].active() == context["previous_pointer"]
    assert datetime.fromisoformat(view.intent.expires_at) <= (
        datetime.fromisoformat(view.intent.issued_at) + timedelta(minutes=5)
    )

    repeated = await _issue(services[-1], context)
    assert repeated == view

    service = services[0]
    service.clock = lambda: datetime.fromisoformat(view.intent.expires_at)
    expired = await service.inspect(assignment_id=view.intent.assignment.assignment_id)
    assert expired.expired
    assert not expired.deployment_intent_authority
    assert "intent_expired" in expired.invalidation_reasons
    expired_store = EvolutionRevalidationPercentageDeploymentIntentStore(
        service.store.db_path,
        assignment_service=context["assignment_service"],
        archive_service=context["archive_service"],
        installation_target_provider=context["target_provider"],
        clock=lambda: datetime.fromisoformat(view.intent.expires_at),
    )
    with pytest.raises(EvolutionRevalidationPercentageDeploymentIntentError) as replay:
        await expired_store.record(view.intent)
    assert replay.value.code == "percentage_deployment_intent_not_current"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot baseline 夹具使用 POSIX shebang")
async def test_percentage_deployment_intent_rejects_nonmember_and_pointer_drift(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path)

    async def sign_unselected(payload: bytes) -> str:
        return _b64(context["keys"][context["unselected_member"]].sign(payload))

    assignment_service = context["assignment_service"]
    unselected_service = EvolutionRevalidationPercentageCohortAssignmentService(
        workspace_root=tmp_path,
        channel="stable",
        advance_service=assignment_service.advance_service,
        population_store=assignment_service.population_store,
        plan_service=assignment_service.plan_service,
        store=assignment_service.store,
        sign_assignment_challenge=sign_unselected,
        clock=lambda: context["now"] + timedelta(seconds=3),
    )
    await unselected_service.assign(
        stage_completion_evidence_id=context["passing"].evidence_id,
        member_id=context["unselected_member"],
    )
    service = _intent_service(context)
    with pytest.raises(EvolutionRevalidationPercentageDeploymentIntentError) as denied:
        await _issue(service, context, member_id=context["unselected_member"])
    assert denied.value.code == "percentage_deployment_intent_member_not_selected"

    issued = await _issue(service, context)
    next_baseline = context["slots"].install(
        _bundle(
            tmp_path / "next-baseline",
            version="1.2.4",
            output_name="next-baseline-release",
            source_commit="d" * 40,
            source_tree_sha256="c" * 64,
        )
    )
    context["slots"].verify_bootable(next_baseline.slot_id)
    context["slots"].activate(next_baseline.slot_id)
    drifted = await service.inspect(
        assignment_id=issued.intent.assignment.assignment_id
    )
    assert not drifted.previous_pointer_current
    assert not drifted.deployment_intent_authority
    assert "previous_pointer_changed" in drifted.invalidation_reasons


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot baseline 夹具使用 POSIX shebang")
async def test_percentage_deployment_intent_revokes_on_population_and_slot_change(
    tmp_path: Path,
) -> None:
    context = await _context(tmp_path)
    service = _intent_service(context)
    issued = await _issue(service, context)

    original_target = context["target_state"][0]
    context["target_state"][0] = (
        "linux-x64" if original_target != "linux-x64" else "macos-arm64"
    )
    stale_target = await service.inspect(
        assignment_id=issued.intent.assignment.assignment_id
    )
    assert not stale_target.installation_target_current
    assert not stale_target.deployment_intent_authority
    context["target_state"][0] = original_target

    next_snapshot = context["registry"].issue_snapshot(
        channel="stable",
        credentials=tuple(context["credentials"][:-1]),
        previous=context["snapshot"],
        generated_at=(context["now"] + timedelta(seconds=5)).isoformat(),
        valid_from=(context["now"] + timedelta(seconds=6)).isoformat(),
        expires_at=(context["now"] + timedelta(days=7)).isoformat(),
    )
    context["population_clock"][0] = context["now"] + timedelta(seconds=7)
    await context["assignment_service"].population_store.record(next_snapshot)
    stale_population = await service.inspect(
        assignment_id=issued.intent.assignment.assignment_id
    )
    assert not stale_population.assignment_current
    assert not stale_population.credential_current
    assert not stale_population.deployment_intent_authority

    candidate = issued.intent.archive_admission.installed_slot
    runtime = Path(candidate.bundle_dir) / candidate.backend_path
    runtime.chmod(0o700)
    runtime.write_bytes(b"tampered-after-intent")
    stale_slot = await service.inspect(
        assignment_id=issued.intent.assignment.assignment_id
    )
    assert not stale_slot.archive_admission_current
    assert not stale_slot.candidate_slot_current
    assert not stale_slot.deployment_intent_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 boot baseline 夹具使用 POSIX shebang")
async def test_percentage_deployment_intent_tamper_fails_closed(tmp_path: Path) -> None:
    context = await _context(tmp_path)
    service = _intent_service(context)
    issued = await _issue(service, context)
    async with aiosqlite.connect(service.store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_percentage_deployment_intents "
            "SET intent_json = replace(intent_json, ?, ?) WHERE intent_id = ?",
            (
                issued.intent.intent_sha256,
                "0" * 64,
                issued.intent.intent_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationPercentageDeploymentIntentError) as corrupt:
        await service.inspect(assignment_id=issued.intent.assignment.assignment_id)
    assert corrupt.value.code == "percentage_deployment_intent_source_invalid"
