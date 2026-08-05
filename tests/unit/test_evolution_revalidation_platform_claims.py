from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.daemons.worker_contract import issue_worker_contract
from naumi_agent.evolution.revalidation_platform_claims import (
    EvolutionRevalidationPlatformClaimError,
    EvolutionRevalidationPlatformClaimService,
    EvolutionRevalidationPlatformClaimStore,
    issue_evolution_revalidation_worker_identity,
)
from tests.unit.test_evolution_revalidation_adversarial_matrices import (
    T0,
    T1,
    _healthy_worker,
)
from tests.unit.test_evolution_revalidation_platform_dispatches import (
    _setup as _dispatch_setup,
)

T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"
T4 = "2026-07-19T00:00:04+00:00"
T7 = "2026-07-19T00:00:07+00:00"
T8 = "2026-07-19T00:00:08+00:00"
SUPERVISOR_KEY = b"platform-claim-test-supervisor-key-v1"


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def _sign(private_key: Ed25519PrivateKey, challenge) -> str:
    return base64.b64encode(
        private_key.sign(challenge.payload.canonical_bytes())
    ).decode()


async def _setup(tmp_path: Path):
    contract, registry, dispatch_store, dispatch_service = await _dispatch_setup(tmp_path)
    platform = contract.required_platforms[0]
    worker, report = _healthy_worker(platform, contract.max_total_duration_seconds)
    await registry.register(worker, registered_at=T0)
    dispatch = await dispatch_service.queue(
        contract_id=contract.contract_id,
        platform=platform,
        worker_health_reports=(report,),
        queued_at=T1,
    )
    store = EvolutionRevalidationPlatformClaimStore(dispatch_store.db_path)
    service = EvolutionRevalidationPlatformClaimService(
        workspace_root=dispatch_service.workspace_root,
        contract_service=dispatch_service.contract_service,
        dispatch_store=dispatch_store,
        worker_registry=registry,
        store=store,
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    private_key = Ed25519PrivateKey.generate()
    identity = issue_evolution_revalidation_worker_identity(
        contract=worker,
        public_key_base64=_public_key(private_key),
        enrolled_at=T1,
        supervisor_key=SUPERVISOR_KEY,
    )
    await service.enroll_identity(identity)
    return contract, worker, registry, dispatch, store, service, private_key


@pytest.mark.asyncio
async def test_concurrent_authenticated_claim_is_singleflight_and_non_authoritative(
    tmp_path: Path,
) -> None:
    _contract, _worker, _registry, dispatch, store, service, private_key = (
        await _setup(tmp_path)
    )

    challenges = await asyncio.gather(
        *(
            service.prepare_claim(
                contract_id=dispatch.contract_id,
                platform=dispatch.platform,
                issued_at=T2,
                lease_seconds=30,
            )
            for _ in range(8)
        )
    )
    challenge = challenges[0]
    assert all(item == challenge for item in challenges)
    signature = _sign(private_key, challenge)
    views = await asyncio.gather(
        *(
            service.submit(
                challenge_id=challenge.payload.challenge_id,
                signature_base64=signature,
                claimed_at=T3,
            )
            for _ in range(8)
        )
    )

    receipt = views[0].receipt
    assert all(item == views[0] for item in views)
    assert views[0].status == "current"
    assert receipt.worker_claimed and receipt.transport_delivered
    assert not receipt.execution_started
    assert not receipt.result_received
    assert not receipt.cohort_authority
    assert not receipt.promotion_authority
    assert await store.get_latest(receipt.claim_id) == receipt
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_revalidation_platform_claim_receipts"
        ).fetchone() == (1,)
        identity_json = db.execute(
            "SELECT identity_json FROM evolution_revalidation_worker_identities"
        ).fetchone()[0]
    persisted_identity = json.loads(identity_json)
    assert persisted_identity["private_key_stored"] is False
    assert not any(key in persisted_identity for key in ("private_key", "secret_key"))


@pytest.mark.asyncio
async def test_wrong_private_key_cannot_claim_dispatch(tmp_path: Path) -> None:
    _contract, _worker, _registry, dispatch, store, service, _private_key = (
        await _setup(tmp_path)
    )
    challenge = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T2,
    )

    with pytest.raises(EvolutionRevalidationPlatformClaimError) as captured:
        await service.submit(
            challenge_id=challenge.payload.challenge_id,
            signature_base64=_sign(Ed25519PrivateKey.generate(), challenge),
            claimed_at=T3,
        )

    assert captured.value.code == "platform_claim_signature_invalid"
    assert await store.get_latest_for_dispatch(dispatch.dispatch_id) is None


@pytest.mark.asyncio
async def test_signed_renewal_advances_hash_chain_and_fences_old_lease(
    tmp_path: Path,
) -> None:
    _contract, _worker, _registry, dispatch, _store, service, private_key = (
        await _setup(tmp_path)
    )
    challenge = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T2,
        lease_seconds=5,
    )
    first = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_sign(private_key, challenge),
        claimed_at=T2,
    )
    renewal = await service.prepare_renewal(
        claim_id=first.receipt.claim_id,
        issued_at=T3,
        challenge_ttl_seconds=5,
        lease_seconds=5,
    )
    second = await service.submit(
        challenge_id=renewal.payload.challenge_id,
        signature_base64=_sign(private_key, renewal),
        claimed_at=T3,
    )

    assert first.receipt.sequence == 1
    assert second.receipt.sequence == 2
    assert second.receipt.lease_epoch == 2
    assert second.receipt.previous_receipt_sha256 == first.receipt.receipt_sha256
    assert second.receipt.receipt_sha256 != first.receipt.receipt_sha256
    assert (await service.inspect(claim_id=second.receipt.claim_id, assessed_at=T7)).status == (
        "current"
    )
    assert (await service.inspect(claim_id=second.receipt.claim_id, assessed_at=T8)).status == (
        "expired"
    )


@pytest.mark.asyncio
async def test_signed_renewal_challenge_cannot_resurrect_expired_lease(
    tmp_path: Path,
) -> None:
    _contract, _worker, _registry, dispatch, _store, service, private_key = (
        await _setup(tmp_path)
    )
    challenge = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T2,
        lease_seconds=5,
    )
    first = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_sign(private_key, challenge),
        claimed_at=T2,
    )
    renewal = await service.prepare_renewal(
        claim_id=first.receipt.claim_id,
        issued_at=T3,
        challenge_ttl_seconds=120,
        lease_seconds=5,
    )

    with pytest.raises(EvolutionRevalidationPlatformClaimError) as captured:
        await service.submit(
            challenge_id=renewal.payload.challenge_id,
            signature_base64=_sign(private_key, renewal),
            claimed_at=T7,
        )

    assert captured.value.code == "platform_claim_lease_expired"


@pytest.mark.asyncio
async def test_higher_worker_epoch_dynamically_stales_existing_claim(tmp_path: Path) -> None:
    _contract, worker, registry, dispatch, _store, service, private_key = await _setup(
        tmp_path
    )
    challenge = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T2,
    )
    current = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_sign(private_key, challenge),
        claimed_at=T3,
    )
    replacement = issue_worker_contract(
        worker_id=worker.worker_id,
        instance_id="process-2",
        epoch=2,
        kind=worker.kind,
        protocol_min=worker.protocol_min,
        protocol_max=worker.protocol_max,
        software_version=worker.software_version,
        platform=worker.platform,
        capabilities=worker.capabilities,
        resources=worker.resources,
        isolation=worker.isolation,
        issued_at=T4,
    )
    await registry.register(replacement, registered_at=T4)

    stale = await service.inspect(claim_id=current.receipt.claim_id, assessed_at=T4)
    assert stale.status == "stale"
    assert not stale.worker_claimed
    assert not stale.transport_delivered
    assert not stale.execution_authority


@pytest.mark.asyncio
async def test_same_incarnation_cannot_replace_attested_identity(tmp_path: Path) -> None:
    _contract, worker, _registry, _dispatch, _store, service, _private_key = (
        await _setup(tmp_path)
    )
    conflicting = issue_evolution_revalidation_worker_identity(
        contract=worker,
        public_key_base64=_public_key(Ed25519PrivateKey.generate()),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )

    with pytest.raises(EvolutionRevalidationPlatformClaimError) as captured:
        await service.enroll_identity(conflicting)

    assert captured.value.code == "platform_claim_identity_conflict"


@pytest.mark.asyncio
async def test_identity_with_foreign_supervisor_attestation_is_rejected(
    tmp_path: Path,
) -> None:
    _contract, worker, _registry, _dispatch, store, service, _private_key = (
        await _setup(tmp_path)
    )
    foreign = issue_worker_contract(
        worker_id="foreign-attestation-worker",
        instance_id="foreign-process-1",
        epoch=1,
        kind=worker.kind,
        protocol_min=worker.protocol_min,
        protocol_max=worker.protocol_max,
        software_version=worker.software_version,
        platform=worker.platform,
        capabilities=worker.capabilities,
        resources=worker.resources,
        isolation=worker.isolation,
        issued_at=T0,
    )
    await service.worker_registry.register(foreign, registered_at=T0)
    identity = issue_evolution_revalidation_worker_identity(
        contract=foreign,
        public_key_base64=_public_key(Ed25519PrivateKey.generate()),
        enrolled_at=T1,
        supervisor_key=b"foreign-platform-claim-supervisor-key",
    )

    with pytest.raises(EvolutionRevalidationPlatformClaimError) as captured:
        await service.enroll_identity(identity)

    assert captured.value.code == "platform_claim_identity_attestation_invalid"
    assert await store.get_identity(identity.identity_id) is None


@pytest.mark.asyncio
async def test_expired_unsigned_challenge_is_replaced_not_replayed(tmp_path: Path) -> None:
    _contract, _worker, _registry, dispatch, _store, service, private_key = (
        await _setup(tmp_path)
    )
    expired = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T2,
        challenge_ttl_seconds=5,
    )
    fresh = await service.prepare_claim(
        contract_id=dispatch.contract_id,
        platform=dispatch.platform,
        issued_at=T7,
        challenge_ttl_seconds=5,
    )

    assert fresh.payload.challenge_id != expired.payload.challenge_id
    with pytest.raises(EvolutionRevalidationPlatformClaimError) as captured:
        await service.submit(
            challenge_id=expired.payload.challenge_id,
            signature_base64=_sign(private_key, expired),
            claimed_at=T7,
        )
    assert captured.value.code == "platform_claim_challenge_expired"
