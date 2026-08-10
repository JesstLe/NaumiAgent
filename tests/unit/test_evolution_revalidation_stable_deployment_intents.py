from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.evolution.revalidation_stable_deployment_intents import (
    EvolutionRevalidationStableDeploymentIntentError,
    EvolutionRevalidationStableDeploymentIntentService,
    EvolutionRevalidationStableDeploymentIntentStore,
)
from naumi_agent.release.archive_admission import ReleaseArchiveAdmissionService
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistrySigner,
    ReleasePopulationSnapshotStore,
    ReleaseTrustedPopulationRegistryKey,
    create_release_population_trust_policy,
)
from naumi_agent.release.slots import host_release_target
from tests.unit.test_evolution_revalidation_percentage_stage_advances import (
    _fixture as _stable_advance_fixture,
)
from tests.unit.test_release_archive_admission import _runtime as _release_runtime
from tests.unit.test_release_slots import _bundle


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


async def _context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_backend_content: bytes | None = None,
):
    (
        lifecycle,
        passing,
        _declined,
        plan,
        _control,
        build_advance,
        now,
    ) = await _stable_advance_fixture(tmp_path, monkeypatch)
    advance_service = build_advance("advance", [])
    advance = await advance_service.authorize(
        evidence_id=passing.evidence_id,
        subject_id=passing.subject_id,
    )

    registry = ReleasePopulationRegistrySigner.from_private_keys_base64(
        registry_id="naumi-production-registry",
        key_id="population-stable-2026-01",
        key_generation=1,
        signing_private_key_base64=_b64(bytes(range(32))),
        pseudonym_key_base64=_b64(bytes(reversed(range(32)))),
    )
    keys = {}
    credentials = []
    for index in range(4):
        private_key = Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(f"stable-intent-installation-{index}".encode()).digest()
        )
        credential = registry.issue_credential(
            installation_public_key_base64=_b64(private_key.public_key().public_bytes_raw()),
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
    trust = [
        create_release_population_trust_policy(
            (
                ReleaseTrustedPopulationRegistryKey(
                    identity=registry.identity,
                    state="active",
                    valid_from=(now - timedelta(days=2)).isoformat(),
                ),
            )
        )
    ]
    population_clock = [now + timedelta(seconds=6)]
    population_store = ReleasePopulationSnapshotStore(
        tmp_path / ".naumi" / "stable-population.db",
        trust_policy_provider=lambda: trust[0],
        clock=lambda: population_clock[0],
    )
    await population_store.record(snapshot)

    target = host_release_target()
    release = await _release_runtime(
        tmp_path / "stable-release",
        archive_format="tar.gz",
        target=target,
        source_commit=plan.target_head,
        source_tree_sha256=plan.target_tree_sha256,
        base_time=now,
        backend_content=candidate_backend_content,
    )
    slots = release["slot_store"]
    baseline = slots.install(
        _bundle(
            tmp_path / "stable-baseline",
            version="1.2.2",
            output_name="stable-baseline-release",
            source_commit="f" * 40,
            source_tree_sha256="e" * 64,
        ),
        installed_at=(now + timedelta(seconds=1)).isoformat(),
    )
    slots.verify_bootable(baseline.slot_id, checked_at=(now + timedelta(seconds=2)).isoformat())
    pointer = slots.activate(
        baseline.slot_id, activated_at=(now + timedelta(seconds=3)).isoformat()
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
    issue_at = now + timedelta(seconds=6)
    target_state = [target]
    return {
        "lifecycle": lifecycle,
        "passing": passing,
        "plan": plan,
        "now": now,
        "issue_at": issue_at,
        "advance_service": advance_service,
        "advance": advance,
        "registry": registry,
        "trust": trust,
        "population_store": population_store,
        "population_clock": population_clock,
        "snapshot": snapshot,
        "credentials": credentials,
        "keys": keys,
        "archive_service": archive_service,
        "admission": admission,
        "download_source_id": release["download"].receipt.source_id,
        "slots": slots,
        "pointer": pointer,
        "target_state": target_state,
        "target_provider": lambda: target_state[0],
    }


def _service(context, member_id: str, *, offset: int = 0, wrong_key=False):
    def clock():
        return context["issue_at"] + timedelta(microseconds=offset)

    private_key = Ed25519PrivateKey.generate() if wrong_key else context["keys"][member_id]

    async def sign(challenge: bytes) -> str:
        return _b64(private_key.sign(challenge))

    store = EvolutionRevalidationStableDeploymentIntentStore(
        context["advance_service"].store.db_path,
        stage_advance_service=context["advance_service"],
        plan_service=context["advance_service"].plan_service,
        population_store=context["population_store"],
        archive_service=context["archive_service"],
        installation_target_provider=context["target_provider"],
        clock=clock,
    )
    return EvolutionRevalidationStableDeploymentIntentService(
        workspace_root=context["advance_service"].workspace_root,
        channel="stable",
        store=store,
        sign_challenge=sign,
        clock=clock,
    )


async def _issue(service, context, member_id):
    return await service.issue(
        evidence_id=context["passing"].evidence_id,
        snapshot_id=context["snapshot"].snapshot_id,
        member_id=member_id,
        download_source_id=context["download_source_id"],
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 baseline fixture 使用 POSIX shebang")
async def test_stable_intent_converges_and_allows_each_population_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _context(tmp_path, monkeypatch)
    first_member = context["credentials"][0].payload.member_id
    services = tuple(_service(context, first_member, offset=index) for index in range(4))
    views = await asyncio.gather(*(_issue(service, context, first_member) for service in services))
    view = views[0]
    assert all(item == view for item in views)
    assert view.stable_deployment_intent_authority
    assert view.boot_preparation_authority
    assert view.intent.population_membership_verified
    assert not view.intent.proof.population_membership_verified
    assert view.intent.stable_exposure_percent == 100
    assert not view.intent.stable_rollout_authority
    assert not view.intent.boot_executed
    assert context["slots"].active() == context["pointer"]

    second_member = context["credentials"][1].payload.member_id
    second = await _issue(_service(context, second_member), context, second_member)
    assert second.stable_deployment_intent_authority
    assert second.intent.archive_admission == view.intent.archive_admission
    assert second.intent.proof.installation_credential != view.intent.proof.installation_credential
    assert second.intent.intent_id != view.intent.intent_id

    with pytest.raises(EvolutionRevalidationStableDeploymentIntentError) as replay:
        await _issue(_service(context, first_member, wrong_key=True), context, first_member)
    assert replay.value.code == "stable_installation_proof_invalid"

    with pytest.raises(EvolutionRevalidationStableDeploymentIntentError) as missing:
        await _issue(_service(context, first_member), context, "relpopmember_" + "0" * 24)
    assert missing.value.code == "stable_deployment_intent_member_missing"

    third_member = context["credentials"][2].payload.member_id
    with pytest.raises(EvolutionRevalidationStableDeploymentIntentError) as wrong:
        await _issue(_service(context, third_member, wrong_key=True), context, third_member)
    assert wrong.value.code == "stable_installation_proof_invalid"

    active_trust = context["trust"][0]
    context["trust"][0] = create_release_population_trust_policy(
        (
            ReleaseTrustedPopulationRegistryKey(
                identity=context["registry"].identity,
                state="revoked",
                valid_from=(context["now"] - timedelta(days=2)).isoformat(),
                revoked_at=(context["now"] + timedelta(seconds=7)).isoformat(),
            ),
        )
    )
    revoked = await services[0].inspect(
        stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
        credential_id=view.intent.proof.installation_credential.credential_id,
        admission_id=view.intent.archive_admission.admission_id,
    )
    assert not revoked.population_snapshot_current
    assert not revoked.credential_current
    assert not revoked.stable_deployment_intent_authority
    context["trust"][0] = active_trust

    context["target_state"][0] = "linux-x64"
    target_changed = await services[0].inspect(
        stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
        credential_id=view.intent.proof.installation_credential.credential_id,
        admission_id=view.intent.archive_admission.admission_id,
    )
    assert not target_changed.installation_target_current
    context["target_state"][0] = view.intent.installation_target

    next_snapshot = context["registry"].issue_snapshot(
        channel="stable",
        credentials=tuple(context["credentials"]),
        previous=context["snapshot"],
        generated_at=(context["now"] + timedelta(seconds=7)).isoformat(),
        valid_from=(context["now"] + timedelta(seconds=8)).isoformat(),
        expires_at=(context["now"] + timedelta(days=7, seconds=7)).isoformat(),
    )
    context["population_clock"][0] = context["now"] + timedelta(seconds=9)
    await context["population_store"].record(next_snapshot)
    superseded = await services[0].inspect(
        stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
        credential_id=view.intent.proof.installation_credential.credential_id,
        admission_id=view.intent.archive_admission.admission_id,
    )
    assert not superseded.population_snapshot_current
    assert not superseded.stable_deployment_intent_authority

    replacement = context["slots"].install(
        _bundle(
            tmp_path / "replacement-baseline",
            version="1.2.1",
            output_name="replacement-baseline-release",
            source_commit="d" * 40,
            source_tree_sha256="c" * 64,
        ),
        installed_at=(context["now"] + timedelta(seconds=7)).isoformat(),
    )
    context["slots"].verify_bootable(
        replacement.slot_id,
        checked_at=(context["now"] + timedelta(seconds=8)).isoformat(),
    )
    context["slots"].activate(
        replacement.slot_id,
        activated_at=(context["now"] + timedelta(seconds=9)).isoformat(),
        _expected_pointer_sha256=context["pointer"].pointer_sha256,
    )
    fenced = await services[0].inspect(
        stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
        credential_id=view.intent.proof.installation_credential.credential_id,
        admission_id=view.intent.archive_admission.admission_id,
    )
    assert not fenced.previous_pointer_current
    assert not fenced.stable_deployment_intent_authority

    context["issue_at"] = datetime.fromisoformat(view.intent.expires_at) + timedelta(microseconds=1)
    expired = await services[0].inspect(
        stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
        credential_id=view.intent.proof.installation_credential.credential_id,
        admission_id=view.intent.archive_admission.admission_id,
    )
    assert expired.expired
    assert not expired.stable_deployment_intent_authority

    async with aiosqlite.connect(services[0].store.db_path) as db:
        row = await (
            await db.execute(
                "SELECT intent_json FROM evolution_revalidation_stable_deployment_intents "
                "WHERE intent_id = ?",
                (view.intent.intent_id,),
            )
        ).fetchone()
        payload = json.loads(row[0])
        payload["population_denominator"] += 1
        await db.execute(
            "UPDATE evolution_revalidation_stable_deployment_intents SET intent_json = ? "
            "WHERE intent_id = ?",
            (json.dumps(payload), view.intent.intent_id),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationStableDeploymentIntentError) as corrupt:
        await services[0].inspect(
            stage_advance_receipt_id=view.intent.stage_advance.receipt_id,
            credential_id=view.intent.proof.installation_credential.credential_id,
            admission_id=view.intent.archive_admission.admission_id,
        )
    assert corrupt.value.code == "stable_deployment_intent_source_invalid"
    assert await context["lifecycle"].close()
