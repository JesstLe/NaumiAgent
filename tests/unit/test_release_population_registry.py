from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.release.population_registry import (
    ReleasePopulationRegistryError,
    ReleasePopulationRegistrySigner,
    ReleasePopulationSnapshot,
    ReleasePopulationSnapshotPayload,
    ReleasePopulationSnapshotStore,
    ReleaseTrustedPopulationRegistryKey,
    _digest,
    create_release_population_trust_policy,
    load_release_population_trust_policy,
)

T0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _signer() -> ReleasePopulationRegistrySigner:
    return ReleasePopulationRegistrySigner.from_private_keys_base64(
        registry_id="naumi-production-registry",
        key_id="population-2026-01",
        key_generation=1,
        signing_private_key_base64=_b64(bytes(range(32))),
        pseudonym_key_base64=_b64(bytes(reversed(range(32)))),
    )


def _policy(signer, *, state="active"):
    return create_release_population_trust_policy(
        (
            ReleaseTrustedPopulationRegistryKey(
                identity=signer.identity,
                state=state,
                valid_from=(T0 - timedelta(days=3)).isoformat(),
                revoked_at=(T0 + timedelta(days=1)).isoformat()
                if state == "revoked"
                else None,
            ),
        )
    )


def _installation_public_key(index: int) -> str:
    seed = hashlib.sha256(f"managed-installation-{index}".encode()).digest()
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    return _b64(public)


def _credentials(signer, *, count: int, channel: str = "stable"):
    return tuple(
        signer.issue_credential(
            installation_public_key_base64=_installation_public_key(index),
            channel=channel,
            registered_at=(T0 - timedelta(days=2)).isoformat(),
            expires_at=(T0 + timedelta(days=30)).isoformat(),
        )
        for index in range(count)
    )


def test_population_trust_policy_loader_is_bounded_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    policy = _policy(_signer())
    source = tmp_path / "trusted-population.json"
    source.write_text(policy.model_dump_json(), encoding="utf-8")
    assert load_release_population_trust_policy(source) == policy

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleasePopulationRegistryError) as invalid:
        load_release_population_trust_policy(malformed)
    assert invalid.value.code == "population_trust_policy_invalid"

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(ReleasePopulationRegistryError) as empty_source:
        load_release_population_trust_policy(empty)
    assert empty_source.value.code == "population_trust_policy_size_invalid"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (512 * 1024 + 1))
    with pytest.raises(ReleasePopulationRegistryError) as oversized_source:
        load_release_population_trust_policy(oversized)
    assert oversized_source.value.code == "population_trust_policy_size_invalid"

    link = tmp_path / "trusted-population-link.json"
    try:
        link.symlink_to(source)
    except OSError:
        pass
    else:
        with pytest.raises(ReleasePopulationRegistryError) as symlink:
            load_release_population_trust_policy(link)
        assert symlink.value.code == "population_trust_policy_file_invalid"


@pytest.mark.asyncio
async def test_signed_population_snapshot_is_private_chained_and_concurrent(
    tmp_path: Path,
) -> None:
    signer = _signer()
    credentials = _credentials(signer, count=128)
    snapshot = signer.issue_snapshot(
        channel="stable",
        credentials=credentials,
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    current_policy = [_policy(signer)]
    db_path = tmp_path / ".naumi" / "population.db"

    def build(clock=lambda: T0 + timedelta(seconds=2)):
        return ReleasePopulationSnapshotStore(
            db_path,
            trust_policy_provider=lambda: current_policy[0],
            clock=clock,
        )

    stores = tuple(build() for _ in range(6))
    views = await asyncio.gather(*(item.record(snapshot) for item in stores))
    view = views[0]

    assert all(item == view for item in views)
    assert view.population_snapshot_authority
    assert snapshot.payload.population_denominator == 128
    assert snapshot.payload.credentials == tuple(
        sorted(credentials, key=lambda item: item.payload.member_id)
    )
    assert len({item.payload.member_id for item in credentials}) == 128
    assert not snapshot.cohort_assignment_authority
    assert not snapshot.percentage_rollout_authority
    encoded = snapshot.model_dump_json()
    assert "/Users/" not in encoded
    assert "@" not in encoded
    assert all(
        not item.payload.raw_machine_identifier_collected
        and not item.payload.raw_user_identifier_collected
        and not item.payload.raw_path_collected
        for item in credentials
    )

    not_yet_valid_store = build(clock=lambda: T0)
    not_yet_valid = await not_yet_valid_store.inspect(
        snapshot_id=snapshot.snapshot_id
    )
    assert not_yet_valid.not_yet_valid
    assert "snapshot_not_yet_valid" in not_yet_valid.invalidation_reasons
    assert not not_yet_valid.population_snapshot_authority

    next_snapshot = signer.issue_snapshot(
        channel="stable",
        credentials=credentials[:-1],
        previous=snapshot,
        generated_at=(T0 + timedelta(days=1)).isoformat(),
        valid_from=(T0 + timedelta(days=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=8)).isoformat(),
    )
    future_store = build(clock=lambda: T0 + timedelta(days=1, seconds=2))
    latest = await future_store.record(next_snapshot)
    historical = await future_store.inspect(snapshot_id=snapshot.snapshot_id)
    assert latest.population_snapshot_authority
    assert next_snapshot.payload.sequence == 2
    assert next_snapshot.payload.previous_snapshot_id == snapshot.snapshot_id
    assert next_snapshot.payload.population_denominator == 127
    assert not historical.latest_for_channel
    assert not historical.population_snapshot_authority

    disconnected_store = ReleasePopulationSnapshotStore(
        tmp_path / ".naumi" / "disconnected.db",
        trust_policy_provider=lambda: current_policy[0],
        clock=lambda: T0 + timedelta(seconds=2),
    )
    with pytest.raises(ReleasePopulationRegistryError) as gap:
        await disconnected_store.record(next_snapshot)
    assert gap.value.code == "population_snapshot_chain_conflict"

    expired_store = build(clock=lambda: T0 + timedelta(days=9))
    expired = await expired_store.inspect(snapshot_id=next_snapshot.snapshot_id)
    assert expired.expired
    assert not expired.population_snapshot_authority

    current_policy[0] = _policy(signer, state="revoked")
    revoked = await stores[0].inspect(snapshot_id=next_snapshot.snapshot_id)
    assert not revoked.trust_current
    assert "registry_trust_changed" in revoked.invalidation_reasons
    assert not revoked.population_snapshot_authority


@pytest.mark.asyncio
async def test_population_snapshot_rejects_duplicate_members_signature_and_tamper(
    tmp_path: Path,
) -> None:
    signer = _signer()
    credential = _credentials(signer, count=1)[0]
    with pytest.raises(ValueError, match="member projection"):
        ReleasePopulationSnapshotPayload(
            registry=signer.identity,
            channel="stable",
            sequence=1,
            credentials=(credential, credential),
            population_denominator=2,
            generated_at=T0.isoformat(),
            valid_from=(T0 + timedelta(seconds=1)).isoformat(),
            expires_at=(T0 + timedelta(days=7)).isoformat(),
        )

    snapshot = signer.issue_snapshot(
        channel="stable",
        credentials=(credential,),
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    payload = snapshot.model_dump(mode="json")
    signature = bytearray(base64.b64decode(payload["signature_base64"]))
    signature[0] ^= 1
    payload["signature_base64"] = _b64(bytes(signature))
    payload["signature_sha256"] = hashlib.sha256(signature).hexdigest()
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"snapshot_id", "snapshot_sha256"}
    }
    digest = _digest(core)
    forged = ReleasePopulationSnapshot.model_validate_json(
        json.dumps(
            {
                **core,
                "snapshot_id": f"relpopsnapshot_{digest[:24]}",
                "snapshot_sha256": digest,
            }
        )
    )
    store = ReleasePopulationSnapshotStore(
        tmp_path / ".naumi" / "population.db",
        trust_policy_provider=lambda: _policy(signer),
        clock=lambda: T0 + timedelta(seconds=2),
    )
    with pytest.raises(ReleasePopulationRegistryError) as invalid_signature:
        await store.record(forged)
    assert invalid_signature.value.code == "population_snapshot_signature_invalid"

    await store.record(snapshot)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE release_population_snapshots SET snapshot_json = replace("
            "snapshot_json, ?, ?) WHERE snapshot_id = ?",
            (snapshot.snapshot_sha256, "0" * 64, snapshot.snapshot_id),
        )
        await db.commit()
    with pytest.raises(ReleasePopulationRegistryError) as corrupt:
        await store.get(snapshot.snapshot_id)
    assert corrupt.value.code == "population_snapshot_source_invalid"
