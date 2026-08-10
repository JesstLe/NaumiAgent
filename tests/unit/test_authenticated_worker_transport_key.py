from __future__ import annotations

import asyncio
import base64
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityStore,
    issue_authenticated_worker_identity,
)
from naumi_agent.daemons.authenticated_worker_transport_key import (
    AuthenticatedWorkerTransportKeyAuthority,
    AuthenticatedWorkerTransportKeyError,
    AuthenticatedWorkerTransportKeyStore,
    issue_authenticated_worker_transport_key,
    verify_authenticated_worker_transport_key,
)
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerPlatform,
    WorkerResourceEnvelope,
    issue_worker_contract,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore

T0 = "2026-08-10T09:00:00+00:00"
T1 = "2026-08-10T09:00:01+00:00"
T2 = "2026-08-10T09:00:02+00:00"
T3 = "2026-08-10T09:00:03+00:00"
T4 = "2026-08-10T09:00:04+00:00"
SUPERVISOR_KEY = b"worker-transport-supervisor-key-2026"


def _contract(epoch: int = 1):
    capabilities = tuple(
        sorted(
            (
                WorkerCapability.ARTIFACT_DIGEST,
                WorkerCapability.ENVIRONMENT_ALLOWLIST,
                WorkerCapability.NETWORK_POLICY,
                WorkerCapability.PROCESS_TREE_CANCEL,
                WorkerCapability.RESOURCE_LIMITS,
                WorkerCapability.SHELL_NON_PTY,
                WorkerCapability.WORKSPACE_EPHEMERAL,
            ),
            key=str,
        )
    )
    return issue_worker_contract(
        worker_id="remote-eval-worker",
        instance_id=f"remote-eval-worker-instance-{epoch}",
        epoch=epoch,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="1.2.3",
        platform=WorkerPlatform(
            system="linux",
            machine="x86_64",
            python_implementation="cpython",
            python_version="3.13.5",
        ),
        capabilities=capabilities,
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=300,
            max_wall_seconds=300,
            max_output_bytes=16 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(True, True, True, True, True, True),
        issued_at=T0 if epoch == 1 else T4,
    )


def _ed25519_public(marker: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(marker * 32)
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _x25519_public(marker: bytes) -> str:
    private = X25519PrivateKey.from_private_bytes(marker * 32)
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


async def _authorities(tmp_path: Path):
    registry = WorkerRegistryStore(tmp_path / "workers.db")
    contract = _contract()
    await registry.register(contract, registered_at=T0)
    db_path = tmp_path / "authority.db"
    identity_authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=AuthenticatedWorkerIdentityStore(db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    identity = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_ed25519_public(b"a"),
        enrolled_at=T1,
        supervisor_key=SUPERVISOR_KEY,
    )
    await identity_authority.enroll(identity)
    authority = AuthenticatedWorkerTransportKeyAuthority(
        identity_authority=identity_authority,
        store=AuthenticatedWorkerTransportKeyStore(db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    return registry, contract, identity, identity_authority, authority


@pytest.mark.asyncio
async def test_transport_key_is_durable_concurrent_rotatable_and_fenced(
    tmp_path: Path,
) -> None:
    registry, _contract_one, identity, identity_authority, authority = (
        await _authorities(tmp_path)
    )
    first = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=1,
        public_key_base64=_x25519_public(b"b"),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )
    second_authority = AuthenticatedWorkerTransportKeyAuthority(
        identity_authority=identity_authority,
        store=AuthenticatedWorkerTransportKeyStore(authority.store.db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )

    first_view, replay = await asyncio.gather(
        authority.enroll(first),
        second_authority.enroll(first),
    )

    assert replay == first_view
    assert first_view.transport_key_authority
    assert first_view.transport_key.transport_encryption_eligible
    assert not first_view.transport_delivered
    assert not first_view.execution_authority
    assert verify_authenticated_worker_transport_key(
        first,
        supervisor_key=SUPERVISOR_KEY,
    )
    encoded = first.model_dump_json()
    assert base64.b64encode(b"b" * 32).decode("ascii") not in encoded

    second = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=2,
        public_key_base64=_x25519_public(b"c"),
        enrolled_at=T3,
        supervisor_key=SUPERVISOR_KEY,
    )
    second_view = await authority.enroll(second)
    assert second_view.transport_key_authority
    assert not (await authority.inspect(first)).transport_key_authority
    assert await second_authority.resolve_for_identity(identity) == second_view

    takeover = _contract(2)
    await registry.register(takeover, registered_at=T4)
    fenced = await authority.inspect(second)
    assert fenced.durable_source_valid
    assert fenced.latest_generation_valid
    assert not fenced.identity_authority
    assert not fenced.transport_key_authority


@pytest.mark.asyncio
async def test_transport_key_rejects_bad_attestation_rotation_and_store_split(
    tmp_path: Path,
) -> None:
    _registry, _contract_one, identity, identity_authority, authority = (
        await _authorities(tmp_path)
    )
    first = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=1,
        public_key_base64=_x25519_public(b"b"),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )
    bad_authority = AuthenticatedWorkerTransportKeyAuthority(
        identity_authority=identity_authority,
        store=authority.store,
        supervisor_key_provider=lambda: b"different-worker-transport-key!",
    )
    with pytest.raises(AuthenticatedWorkerTransportKeyError) as bad_attestation:
        await bad_authority.enroll(first)
    assert (
        bad_attestation.value.code
        == "authenticated_worker_transport_key_attestation_invalid"
    )

    skipped = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=2,
        public_key_base64=_x25519_public(b"c"),
        enrolled_at=T3,
        supervisor_key=SUPERVISOR_KEY,
    )
    with pytest.raises(AuthenticatedWorkerTransportKeyError) as rotation:
        await authority.enroll(skipped)
    assert rotation.value.code == "authenticated_worker_transport_key_rotation_invalid"

    await authority.enroll(first)
    conflicting = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=1,
        public_key_base64=_x25519_public(b"d"),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )
    with pytest.raises(AuthenticatedWorkerTransportKeyError) as conflict:
        await authority.enroll(conflicting)
    assert conflict.value.code == "authenticated_worker_transport_key_conflict"

    with pytest.raises(ValueError, match="共享 SQLite"):
        AuthenticatedWorkerTransportKeyAuthority(
            identity_authority=identity_authority,
            store=AuthenticatedWorkerTransportKeyStore(tmp_path / "other.db"),
        )
    with pytest.raises(ValueError, match="shared secret"):
        issue_authenticated_worker_transport_key(
            identity=identity,
            key_generation=2,
            public_key_base64=base64.b64encode(bytes(32)).decode("ascii"),
            enrolled_at=T3,
            supervisor_key=SUPERVISOR_KEY,
        )


@pytest.mark.asyncio
async def test_transport_key_tamper_fails_closed(tmp_path: Path) -> None:
    _registry, _contract_one, identity, _identity_authority, authority = (
        await _authorities(tmp_path)
    )
    item = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=1,
        public_key_base64=_x25519_public(b"b"),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )
    await authority.enroll(item)
    with sqlite3.connect(authority.store.db_path) as db:
        db.execute(
            "UPDATE authenticated_worker_transport_keys SET transport_key_json = '{}' "
            "WHERE transport_key_id = ?",
            (item.transport_key_id,),
        )
        db.commit()

    view = await authority.inspect(item)

    assert not view.durable_source_valid
    assert not view.transport_key_authority
