from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.daemons.authenticated_worker_transport_key import (
    AuthenticatedWorkerTransportKeyAuthority,
    AuthenticatedWorkerTransportKeyStore,
    issue_authenticated_worker_transport_key,
)
from naumi_agent.evolution.post_rollback_remote_deliveries import (
    EvolutionPostRollbackRemoteDeliveryEnvelope,
    EvolutionPostRollbackRemoteDeliveryError,
    EvolutionPostRollbackRemoteDeliveryService,
    EvolutionPostRollbackRemoteDeliveryStore,
    open_post_rollback_remote_delivery_envelope,
)
from naumi_agent.release.artifact_fetch import (
    HttpxReleaseArtifactTransport,
    ReleaseArtifactDownloadStore,
    ReleaseArtifactFetchService,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionPostRollbackRemoteDeliveryTool
from tests.unit.test_post_rollback_remote_lane_placements import (
    _claim_fixture,
    _sign_claim,
)
from tests.unit.test_release_artifact_fetch import _response


async def _delivery_fixture(tmp_path: Path):
    claim_service, _registry, dispatch, worker, signing_private = await _claim_fixture(
        tmp_path
    )
    challenge = await claim_service.prepare_claim(
        dispatch_id=dispatch.dispatch.dispatch_id,
        issued_at="2026-08-10T08:03:03+00:00",
        challenge_ttl_seconds=2,
        lease_seconds=20,
    )
    claim = await claim_service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_sign_claim(signing_private, challenge),
        claimed_at="2026-08-10T08:03:04+00:00",
    )
    identity_view = await claim_service.identity_authority.resolve_for_contract(worker)
    assert identity_view is not None and identity_view.identity_authority
    identity = identity_view.identity
    transport_private = X25519PrivateKey.from_private_bytes(b"e" * 32)
    public_raw = transport_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    transport_authority = AuthenticatedWorkerTransportKeyAuthority(
        identity_authority=claim_service.identity_authority,
        store=AuthenticatedWorkerTransportKeyStore(claim_service.store.db_path),
        supervisor_key_provider=lambda: b"post-rollback-worker-supervisor-key",
    )
    transport_key = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=1,
        public_key_base64=base64.b64encode(public_raw).decode("ascii"),
        enrolled_at="2026-08-10T08:03:04+00:00",
        supervisor_key=b"post-rollback-worker-supervisor-key",
    )
    await transport_authority.enroll(transport_key)

    catalog_store = (
        claim_service.dispatch_service.target_baseline_service.catalog_store
    )
    archive_bytes = b"source-free-windows-x64"
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(200, archive_bytes)

    download_store = ReleaseArtifactDownloadStore(
        catalog_store.db_path,
        catalog_store=catalog_store,
        clock=lambda: datetime(2026, 8, 10, 8, 3, 5, tzinfo=UTC),
    )
    fetch_service = ReleaseArtifactFetchService(
        download_root=tmp_path / "downloads",
        catalog_store=catalog_store,
        store=download_store,
        transport=HttpxReleaseArtifactTransport(transport=httpx.MockTransport(handler)),
        owner_id="remote-delivery-test",
        lease_seconds=5,
        wait_timeout_seconds=5,
        clock=lambda: datetime(2026, 8, 10, 8, 3, 5, tzinfo=UTC),
    )
    delivery_service = EvolutionPostRollbackRemoteDeliveryService(
        claim_service=claim_service,
        claim_store=claim_service.store,
        dispatch_service=claim_service.dispatch_service,
        dispatch_store=claim_service.dispatch_store,
        baseline_service=claim_service.dispatch_service.target_baseline_service,
        baseline_store=claim_service.dispatch_service.target_baseline_service.store,
        identity_authority=claim_service.identity_authority,
        transport_key_authority=transport_authority,
        artifact_fetch_service=fetch_service,
        store=EvolutionPostRollbackRemoteDeliveryStore(claim_service.store.db_path),
        random_bytes=lambda size: bytes([size]) * size,
        ephemeral_key_factory=lambda: X25519PrivateKey.from_private_bytes(b"f" * 32),
    )
    return (
        delivery_service,
        claim,
        signing_private,
        transport_private,
        archive_bytes,
        lambda: calls,
    )


@pytest.mark.asyncio
async def test_remote_delivery_encrypts_descriptor_and_accepts_exact_worker_ack(
    tmp_path: Path,
) -> None:
    service, claim, signing_private, transport_private, archive_bytes, calls = (
        await _delivery_fixture(tmp_path)
    )

    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )

    assert prepared.status == "awaiting_ack"
    assert not prepared.transport_delivered
    assert not prepared.execution_authority
    assert calls() == 1
    descriptor = open_post_rollback_remote_delivery_envelope(
        prepared.offer.envelope,
        transport_private_key=transport_private,
    )
    assert descriptor.archive_sha256 == prepared.offer.ack_payload.archive_sha256
    assert descriptor.archive_size_bytes == len(archive_bytes)
    assert descriptor.channel_resolution.archive_urls
    assert not descriptor.installation_authority
    signature = base64.b64encode(
        signing_private.sign(prepared.offer.ack_payload.canonical_bytes())
    ).decode("ascii")

    delivered = await service.submit_ack(
        delivery_id=prepared.offer.delivery_id,
        worker_signature_base64=signature,
        acknowledged_at="2026-08-10T08:03:06+00:00",
    )
    replay = await service.submit_ack(
        delivery_id=prepared.offer.delivery_id,
        worker_signature_base64=signature,
        acknowledged_at="2026-08-10T08:03:06+00:00",
    )

    assert replay == delivered
    assert delivered.status == "delivered"
    assert delivered.transport_delivered
    assert delivered.receipt is not None
    assert delivered.receipt.worker_signature_verified
    assert not delivered.receipt.installation_authority
    assert not delivered.receipt.execution_authority
    assert not delivered.receipt.result_authority


@pytest.mark.asyncio
async def test_remote_delivery_tool_and_slash_share_service_without_confirmation(
    tmp_path: Path,
) -> None:
    service, claim, _signing, _transport, _archive, _calls = await _delivery_fixture(
        tmp_path
    )
    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )

    class _Service:
        async def prepare(self, **arguments):
            assert arguments == {"claim_id": claim.receipt.claim_id}
            return prepared

    tool = EvolutionPostRollbackRemoteDeliveryTool(
        SimpleNamespace(evolution_post_rollback_remote_delivery_service=_Service())
    )
    arguments = {"action": "prepare", "claim_id": claim.receipt.claim_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed
        assert not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert prepared.offer.delivery_id in rendered
    assert prepared.offer.model_dump_json() in rendered
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**parsed),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-deliver-behavior prepare {claim.receipt.claim_id}",
    )
    assert prepared.offer.delivery_id in slash


@pytest.mark.asyncio
async def test_remote_delivery_rejects_wrong_decryption_key_and_forged_ack(
    tmp_path: Path,
) -> None:
    service, claim, _signing_private, _transport_private, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )
    with pytest.raises(EvolutionPostRollbackRemoteDeliveryError) as decrypt:
        open_post_rollback_remote_delivery_envelope(
            prepared.offer.envelope,
            transport_private_key=X25519PrivateKey.from_private_bytes(b"g" * 32),
        )
    assert decrypt.value.code == "post_rollback_remote_delivery_decrypt_failed"

    forged_signature = base64.b64encode(b"x" * 64).decode("ascii")
    with pytest.raises(EvolutionPostRollbackRemoteDeliveryError) as forged:
        await service.submit_ack(
            delivery_id=prepared.offer.delivery_id,
            worker_signature_base64=forged_signature,
            acknowledged_at="2026-08-10T08:03:06+00:00",
        )
    assert (
        forged.value.code
        == "post_rollback_remote_delivery_ack_signature_invalid"
    )


@pytest.mark.asyncio
async def test_remote_delivery_rejects_tampered_ciphertext_before_decryption(
    tmp_path: Path,
) -> None:
    service, claim, _signing, transport_private, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )
    payload = prepared.offer.envelope.model_dump(mode="json")
    ciphertext = bytearray(base64.b64decode(payload["ciphertext_base64"]))
    ciphertext[-1] ^= 1
    payload["ciphertext_base64"] = base64.b64encode(ciphertext).decode("ascii")

    with pytest.raises(ValueError, match="摘要不一致"):
        envelope = EvolutionPostRollbackRemoteDeliveryEnvelope.model_validate(payload)
        open_post_rollback_remote_delivery_envelope(
            envelope,
            transport_private_key=transport_private,
        )


@pytest.mark.asyncio
async def test_remote_delivery_revokes_when_download_changes_or_offer_expires(
    tmp_path: Path,
) -> None:
    service, claim, signing_private, _transport_private, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=2,
    )
    expired = await service.inspect(
        delivery_id=prepared.offer.delivery_id,
        assessed_at="2026-08-10T08:03:07+00:00",
    )
    assert expired.status == "expired"
    signature = base64.b64encode(
        signing_private.sign(prepared.offer.ack_payload.canonical_bytes())
    ).decode("ascii")
    with pytest.raises(EvolutionPostRollbackRemoteDeliveryError) as late:
        await service.submit_ack(
            delivery_id=prepared.offer.delivery_id,
            worker_signature_base64=signature,
            acknowledged_at="2026-08-10T08:03:07+00:00",
        )
    assert late.value.code == "post_rollback_remote_delivery_ack_time_invalid"

    archive_path = Path(
        (await service.artifact_fetch_service.inspect(
            source_id=prepared.offer.download_source_id
        )).receipt.archive_path
    )
    archive_path.chmod(0o600)
    archive_path.write_bytes(b"tampered" + archive_path.read_bytes())
    stale = await service.inspect(
        delivery_id=prepared.offer.delivery_id,
        assessed_at="2026-08-10T08:03:06+00:00",
    )
    assert stale.status == "stale"
    assert not stale.download_authority
    assert not stale.transport_delivered


@pytest.mark.asyncio
async def test_remote_delivery_rechecks_clock_after_download_and_rejects_time_rollback(
    tmp_path: Path,
) -> None:
    service, claim, signing_private, _transport, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    current = [datetime(2026, 8, 10, 8, 3, 5, tzinfo=UTC)]
    service.clock = lambda: current[0]
    original_fetch = service.artifact_fetch_service.fetch

    async def delayed_fetch(resolution):
        view = await original_fetch(resolution)
        current[0] = datetime(2026, 8, 10, 8, 3, 16, tzinfo=UTC)
        return view

    service.artifact_fetch_service.fetch = delayed_fetch  # type: ignore[method-assign]
    with pytest.raises(EvolutionPostRollbackRemoteDeliveryError) as elapsed:
        await service.prepare(claim_id=claim.receipt.claim_id)
    assert elapsed.value.code == "post_rollback_remote_delivery_claim_stale"

    rollback_root = tmp_path / "rollback"
    rollback_root.mkdir()
    rollback_service, rollback_claim, rollback_signing, _private, _archive, _calls = (
        await _delivery_fixture(rollback_root)
    )
    prepared = await rollback_service.prepare(
        claim_id=rollback_claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )
    signature = base64.b64encode(
        rollback_signing.sign(prepared.offer.ack_payload.canonical_bytes())
    ).decode("ascii")
    with pytest.raises(EvolutionPostRollbackRemoteDeliveryError) as rollback:
        await rollback_service.submit_ack(
            delivery_id=prepared.offer.delivery_id,
            worker_signature_base64=signature,
            acknowledged_at="2026-08-10T08:03:04.500000+00:00",
        )
    assert rollback.value.code == "post_rollback_remote_delivery_ack_time_invalid"


@pytest.mark.asyncio
async def test_remote_delivery_concurrent_services_converge_to_one_offer(
    tmp_path: Path,
) -> None:
    first, claim, _signing, _transport, _archive, calls = await _delivery_fixture(
        tmp_path
    )
    second = EvolutionPostRollbackRemoteDeliveryService(
        claim_service=first.claim_service,
        claim_store=first.claim_store,
        dispatch_service=first.dispatch_service,
        dispatch_store=first.dispatch_store,
        baseline_service=first.baseline_service,
        baseline_store=first.baseline_store,
        identity_authority=first.identity_authority,
        transport_key_authority=first.transport_key_authority,
        artifact_fetch_service=first.artifact_fetch_service,
        store=EvolutionPostRollbackRemoteDeliveryStore(first.store.db_path),
        random_bytes=lambda size: bytes([size + 1]) * size,
        ephemeral_key_factory=lambda: X25519PrivateKey.from_private_bytes(b"h" * 32),
    )

    left, right = await asyncio.gather(
        first.prepare(
            claim_id=claim.receipt.claim_id,
            issued_at="2026-08-10T08:03:05+00:00",
            offer_ttl_seconds=5,
        ),
        second.prepare(
            claim_id=claim.receipt.claim_id,
            issued_at="2026-08-10T08:03:05+00:00",
            offer_ttl_seconds=5,
        ),
    )

    assert left.offer == right.offer
    assert left.offer.delivery_id == right.offer.delivery_id
    assert calls() == 1


@pytest.mark.asyncio
async def test_remote_delivery_is_fenced_by_claim_renewal_and_transport_key_rotation(
    tmp_path: Path,
) -> None:
    service, claim, signing_private, _transport, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    prepared = await service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )
    renewal = await service.claim_service.prepare_renewal(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:06+00:00",
        challenge_ttl_seconds=1,
        lease_seconds=5,
    )
    await service.claim_service.submit(
        challenge_id=renewal.payload.challenge_id,
        signature_base64=_sign_claim(signing_private, renewal),
        claimed_at="2026-08-10T08:03:06.500000+00:00",
    )
    stale_claim = await service.inspect(
        delivery_id=prepared.offer.delivery_id,
        assessed_at="2026-08-10T08:03:07+00:00",
    )
    assert stale_claim.status == "stale"
    assert not stale_claim.claim_authority

    rotation_root = tmp_path / "rotation"
    rotation_root.mkdir()
    second_service, second_claim, _signing, _private, _archive, _calls = (
        await _delivery_fixture(rotation_root)
    )
    second_offer = await second_service.prepare(
        claim_id=second_claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=5,
    )
    identity = await second_service.identity_authority.store.get(
        second_claim.receipt.identity_id
    )
    assert identity is not None
    next_private = X25519PrivateKey.from_private_bytes(b"i" * 32)
    next_public = next_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    next_key = issue_authenticated_worker_transport_key(
        identity=identity,
        key_generation=2,
        public_key_base64=base64.b64encode(next_public).decode("ascii"),
        enrolled_at="2026-08-10T08:03:06+00:00",
        supervisor_key=b"post-rollback-worker-supervisor-key",
    )
    await second_service.transport_key_authority.enroll(next_key)
    stale_key = await second_service.inspect(
        delivery_id=second_offer.offer.delivery_id,
        assessed_at="2026-08-10T08:03:07+00:00",
    )
    assert stale_key.status == "stale"
    assert not stale_key.transport_key_authority


def test_remote_delivery_rejects_split_authority_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="共享 SQLite"):
        # Construction reaches the path invariant before any dependency is used.
        EvolutionPostRollbackRemoteDeliveryService(
            claim_service=object(),  # type: ignore[arg-type]
            claim_store=SimpleNamespace(db_path=tmp_path / "one.db"),  # type: ignore[arg-type]
            dispatch_service=object(),  # type: ignore[arg-type]
            dispatch_store=SimpleNamespace(db_path=tmp_path / "two.db"),  # type: ignore[arg-type]
            baseline_service=object(),  # type: ignore[arg-type]
            baseline_store=SimpleNamespace(db_path=tmp_path / "one.db"),  # type: ignore[arg-type]
            identity_authority=SimpleNamespace(
                store=SimpleNamespace(db_path=tmp_path / "one.db")
            ),  # type: ignore[arg-type]
            transport_key_authority=SimpleNamespace(
                store=SimpleNamespace(db_path=tmp_path / "one.db")
            ),  # type: ignore[arg-type]
            artifact_fetch_service=object(),  # type: ignore[arg-type]
            store=EvolutionPostRollbackRemoteDeliveryStore(tmp_path / "one.db"),
        )
