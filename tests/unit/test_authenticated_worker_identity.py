from __future__ import annotations

import asyncio
import base64
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityError,
    AuthenticatedWorkerIdentityStore,
    issue_authenticated_worker_identity,
    verify_authenticated_worker_identity,
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
SUPERVISOR_KEY = b"supervisor-worker-identity-key-2026"


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
            system="windows",
            machine="AMD64",
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
        issued_at=T0 if epoch == 1 else T2,
    )


def _public_key(marker: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(marker * 32)
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


@pytest.mark.asyncio
async def test_authenticated_worker_identity_is_durable_concurrent_and_fenced(
    tmp_path: Path,
) -> None:
    registry = WorkerRegistryStore(tmp_path / "workers.db")
    contract = _contract()
    await registry.register(contract, registered_at=T0)
    identity = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_public_key(b"a"),
        enrolled_at=T1,
        supervisor_key=SUPERVISOR_KEY,
    )
    db_path = (tmp_path / "identities.db").resolve()
    key_reads: list[bool] = []

    def supervisor_key_provider() -> bytes:
        key_reads.append(True)
        return SUPERVISOR_KEY

    first_authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=AuthenticatedWorkerIdentityStore(db_path),
        supervisor_key_provider=supervisor_key_provider,
    )
    second_authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=WorkerRegistryStore(registry.db_path),
        store=AuthenticatedWorkerIdentityStore(db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    assert key_reads == []

    first, repeated = await asyncio.gather(
        first_authority.enroll(identity),
        second_authority.enroll(identity),
    )

    assert repeated == first
    assert key_reads
    assert first.identity_authority
    assert first.identity.worker_contract_sha256 == contract.contract_sha256
    assert first.identity.public_key_base64 == _public_key(b"a")
    assert first.identity.private_key_stored is False
    assert first.identity.claim_signature_eligible
    assert not first.execution_authority
    encoded = first.identity.model_dump_json()
    assert base64.b64encode(b"a" * 32).decode("ascii") not in encoded
    assert verify_authenticated_worker_identity(
        first.identity,
        supervisor_key=SUPERVISOR_KEY,
    )

    reopened = AuthenticatedWorkerIdentityAuthority(
        worker_registry=WorkerRegistryStore(registry.db_path),
        store=AuthenticatedWorkerIdentityStore(db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    restored = await reopened.resolve_for_contract(contract)
    assert restored == first

    takeover = _contract(2)
    await registry.register(takeover, registered_at=T2)
    stale = await reopened.inspect(identity)
    assert stale.durable_source_valid
    assert stale.supervisor_attestation_valid
    assert not stale.active_incarnation_valid
    assert not stale.identity_authority


@pytest.mark.asyncio
async def test_authenticated_worker_identity_rejects_key_reuse_and_bad_attestation(
    tmp_path: Path,
) -> None:
    registry = WorkerRegistryStore(tmp_path / "workers.db")
    contract = _contract()
    await registry.register(contract, registered_at=T0)
    authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=AuthenticatedWorkerIdentityStore((tmp_path / "identities.db").resolve()),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    first = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_public_key(b"a"),
        enrolled_at=T1,
        supervisor_key=SUPERVISOR_KEY,
    )
    await authority.enroll(first)
    conflicting = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_public_key(b"b"),
        enrolled_at=T1,
        supervisor_key=SUPERVISOR_KEY,
    )
    with pytest.raises(AuthenticatedWorkerIdentityError) as conflict:
        await authority.enroll(conflicting)
    assert conflict.value.code == "authenticated_worker_identity_conflict"

    bad_authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=authority.store,
        supervisor_key_provider=lambda: b"different-supervisor-key-material!!",
    )
    with pytest.raises(AuthenticatedWorkerIdentityError) as bad_attestation:
        await bad_authority.enroll(first)
    assert (
        bad_attestation.value.code
        == "authenticated_worker_identity_attestation_invalid"
    )
    with pytest.raises(ValueError, match="Base64 无效"):
        issue_authenticated_worker_identity(
            contract=contract,
            public_key_base64=_public_key(b"c") + "\n",
            enrolled_at=T1,
            supervisor_key=SUPERVISOR_KEY,
        )
    with pytest.raises(ValueError, match="Contract 摘要"):
        issue_authenticated_worker_identity(
            contract=replace(contract, contract_sha256="f" * 64),
            public_key_base64=_public_key(b"c"),
            enrolled_at=T1,
            supervisor_key=SUPERVISOR_KEY,
        )


@pytest.mark.asyncio
async def test_authenticated_worker_identity_tamper_and_time_fail_closed(
    tmp_path: Path,
) -> None:
    registry = WorkerRegistryStore(tmp_path / "workers.db")
    contract = _contract()
    await registry.register(contract, registered_at=T1)
    db_path = (tmp_path / "identities.db").resolve()
    authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=AuthenticatedWorkerIdentityStore(db_path),
        supervisor_key_provider=lambda: SUPERVISOR_KEY,
    )
    early = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_public_key(b"a"),
        enrolled_at=T0,
        supervisor_key=SUPERVISOR_KEY,
    )
    with pytest.raises(AuthenticatedWorkerIdentityError) as invalid_time:
        await authority.enroll(early)
    assert invalid_time.value.code == "authenticated_worker_identity_time_invalid"

    current = issue_authenticated_worker_identity(
        contract=contract,
        public_key_base64=_public_key(b"a"),
        enrolled_at=T2,
        supervisor_key=SUPERVISOR_KEY,
    )
    view = await authority.enroll(current)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE authenticated_worker_identities SET identity_json = '{}' "
            "WHERE identity_id = ?",
            (current.identity_id,),
        )
        db.commit()
    stale = await authority.inspect(view.identity)
    assert not stale.durable_source_valid
    assert not stale.identity_authority
