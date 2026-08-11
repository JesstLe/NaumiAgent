from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.stable_remote_finalization_authorizations import (
    EvolutionStableRemoteFinalizationAuthorizationEnvelope,
    EvolutionStableRemoteFinalizationAuthorizationError,
    EvolutionStableRemoteFinalizationAuthorizationService,
    EvolutionStableRemoteFinalizationAuthorizationStore,
    decode_stable_remote_finalization_authorization,
    encode_stable_remote_finalization_authorization,
    verify_stable_remote_finalization_authorization,
)
from naumi_agent.evolution.stable_remote_readiness_probes import (
    encode_stable_remote_readiness_probe_submission,
    execute_stable_remote_readiness_probe,
)
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlKeyError,
    ReleaseRolloutControlKeyService,
    ReleaseTrustedRolloutControlKey,
    create_release_rollout_control_trust_policy,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStableRemoteFinalizationAuthorizationTool,
)
from tests.unit.test_evolution_stable_remote_readiness_probes import (
    _fixture,
    _MemoryBackend,
)


async def _authorization_fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = await _fixture(tmp_path)
    challenge = await data.service.prepare(
        claim_receipt_id=data.claim.receipt.receipt_id,
        validity_seconds=180,
    )
    submission = execute_stable_remote_readiness_probe(
        challenge=challenge,
        credential=data.credential,
        release_slot_store=data.fixture.store,
        installation_key_service=data.key_service,
        clock=data.clock,
    )
    probe = await data.service.ingest(
        challenge_id=challenge.challenge_id,
        submission_base64=encode_stable_remote_readiness_probe_submission(submission),
    )
    db_path = data.fixture.data["store"].db_path
    control_store = EvolutionRevalidationRolloutControlStore(
        db_path,
        control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
    )
    rollout_key = ReleaseRolloutControlKeyService(
        tmp_path / "control-plane-release",
        backend=_MemoryBackend(),
        key_factory=lambda size: b"c" * size,
        clock=data.clock,
    )
    handle = rollout_key.provision(
        control_plane_id="naumi-control-plane",
        key_generation=1,
    )
    trusted = ReleaseTrustedRolloutControlKey(
        identity=handle.signer_identity(),
        state="active",
        channels=("stable",),
        valid_from=(data.clock.value - timedelta(minutes=1)).isoformat(),
        valid_until=(data.clock.value + timedelta(minutes=30)).isoformat(),
    )
    policy = create_release_rollout_control_trust_policy((trusted,))
    policy_path = tmp_path / "target-trust" / "trusted-rollout-controls.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    if os.name != "nt":
        os.chmod(policy_path, 0o644)

    def make_service():
        return EvolutionStableRemoteFinalizationAuthorizationService(
            workspace_root=data.fixture.root,
            probe_service=data.service,
            control_store=control_store,
            rollout_key_service=rollout_key,
            trust_policy_path=policy_path,
            store=EvolutionStableRemoteFinalizationAuthorizationStore(db_path),
            clock=data.clock,
            random_bytes=lambda size: b"a" * size,
        )

    return SimpleNamespace(
        data=data,
        probe=probe,
        policy=policy,
        policy_path=policy_path,
        control_store=control_store,
        rollout_key=rollout_key,
        db_path=db_path,
        make_service=make_service,
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_signed_remote_authorization_is_portable_concurrent_and_single_use(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    assert (
        evolution_api.EvolutionStableRemoteFinalizationAuthorizationService
        is EvolutionStableRemoteFinalizationAuthorizationService
    )
    first = fixture.make_service()
    second = fixture.make_service()
    views = await asyncio.gather(*(
        (first if index % 2 == 0 else second).issue(
            probe_receipt_id=fixture.probe.receipt.receipt_id,
            validity_seconds=120,
        )
        for index in range(8)
    ))
    assert len({item.envelope.authorization.authorization_id for item in views}) == 1
    view = views[0]
    item = view.envelope.authorization
    assert view.remote_finalization_authority
    assert item.single_use_required
    assert item.release_store_finalization_authority
    assert item.remote_execution_authority
    assert not item.config_data_mutation_allowed
    assert not item.deployment_authority
    assert not item.rollback_authority
    assert not item.promotion_authority
    assert item.workspace_root_sha256 == hashlib.sha256(
        str(fixture.data.fixture.root).encode()
    ).hexdigest()
    assert str(fixture.data.fixture.root) not in view.envelope.model_dump_json()

    encoded = encode_stable_remote_finalization_authorization(view.envelope)
    decoded = decode_stable_remote_finalization_authorization(encoded)
    assert decoded == view.envelope
    assert (
        verify_stable_remote_finalization_authorization(
            trust_policy=fixture.policy,
            envelope=decoded,
            expected_member_id=item.installation_member_id,
            now=fixture.data.clock(),
        )
        == item
    )
    with pytest.raises(EvolutionStableRemoteFinalizationAuthorizationError) as member:
        verify_stable_remote_finalization_authorization(
            trust_policy=fixture.policy,
            envelope=decoded,
            expected_member_id="relpopmember_" + "f" * 24,
            now=fixture.data.clock(),
        )
    assert member.value.code == "stable_remote_finalization_member_mismatch"

    receipts = await asyncio.gather(*(
        (first if index % 2 == 0 else second).consume(
            authorization_id=item.authorization_id,
            nonce_base64=item.start_nonce_base64,
        )
        for index in range(8)
    ))
    assert len({receipt.receipt_id for receipt in receipts}) == 1
    consumed = await first.inspect(authorization_id=item.authorization_id)
    assert consumed.consumed
    assert not consumed.remote_finalization_authority
    with pytest.raises(EvolutionStableRemoteFinalizationAuthorizationError) as replay:
        await first.consume(
            authorization_id=item.authorization_id,
            nonce_base64=base64.b64encode(b"z" * 32).decode(),
        )
    assert replay.value.code == "stable_remote_finalization_authorization_consumed"
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_remote_finalization_authorizations"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_remote_finalization_consumptions"
        ).fetchone()[0] == 1

    slash_fixture = await _authorization_fixture(tmp_path / "slash-case")
    slash_service = slash_fixture.make_service()
    tool = EvolutionStableRemoteFinalizationAuthorizationTool(
        SimpleNamespace(
            evolution_stable_remote_finalization_authorization_service=(
                slash_service
            )
        )
    )
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None) -> ToolResult:
            assert agent_name == "cli"
            registered = self.tool_registry.get(call.name)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(
                    **registered.parse_arguments(call.arguments)
                ),
            )

    issued = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization-authorization issue "
        f"{slash_fixture.probe.receipt.receipt_id} 120",
    )
    auth_id = re.search(
        r"evstableremotefinalauth_[0-9a-f]{24}",
        strip_ansi(issued),
    )
    assert auth_id is not None
    inspected = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization-authorization inspect "
        f"{auth_id.group()}",
    )
    assert "Authorization Envelope Base64" not in strip_ansi(inspected)
    exported = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization-authorization export "
        f"{auth_id.group()}",
    )
    assert "Authorization Envelope Base64" in " ".join(
        strip_ansi(exported).split()
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_authorization_revokes_on_control_trust_and_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    service = fixture.make_service()
    first = await service.issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
        validity_seconds=60,
    )
    item = first.envelope.authorization
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=fixture.data.fixture.root,
        store=fixture.control_store,
        control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
    )
    fixture.data.clock.value += timedelta(seconds=1)
    await control.pause(
        reason_code="remote_finalization_test_pause",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=fixture.data.clock().isoformat(),
    )
    paused = await service.inspect(authorization_id=item.authorization_id)
    assert not paused.control_current
    assert not paused.remote_finalization_authority
    assert "rollout_control_changed" in paused.invalidation_reasons

    # Restore no control event in a fresh fixture to isolate policy revocation.
    trust_fixture = await _authorization_fixture(tmp_path / "trust-case")
    trust_service = trust_fixture.make_service()
    trusted = await trust_service.issue(
        probe_receipt_id=trust_fixture.probe.receipt.receipt_id,
        validity_seconds=60,
    )
    revoked_key = trust_fixture.policy.keys[0].model_copy(
        update={
            "state": "revoked",
            "revoked_at": trust_fixture.data.clock().isoformat(),
        }
    )
    revoked_policy = create_release_rollout_control_trust_policy((revoked_key,))
    trust_fixture.policy_path.write_text(
        revoked_policy.model_dump_json(), encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(trust_fixture.policy_path, 0o644)
    revoked = await trust_service.inspect(
        authorization_id=trusted.envelope.authorization.authorization_id
    )
    assert not revoked.trust_policy_current
    assert not revoked.remote_finalization_authority

    race_fixture = await _authorization_fixture(tmp_path / "consume-race")
    race_service = race_fixture.make_service()
    race_view = await race_service.issue(
        probe_receipt_id=race_fixture.probe.receipt.receipt_id,
        validity_seconds=60,
    )
    race_item = race_view.envelope.authorization
    original_consume = race_service.store.consume

    async def _pause_before_writer(**kwargs):
        race_fixture.data.clock.value += timedelta(seconds=1)
        race_control = EvolutionRevalidationRolloutControlService(
            workspace_root=race_fixture.data.fixture.root,
            store=race_fixture.control_store,
            control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
        )
        await race_control.pause(
            reason_code="remote_finalization_consume_race",
            actor=EvolutionRevalidationRolloutControlActor.MONITOR,
            changed_at=race_fixture.data.clock().isoformat(),
        )
        return await original_consume(**kwargs)

    monkeypatch.setattr(race_service.store, "consume", _pause_before_writer)
    with pytest.raises(EvolutionStableRemoteFinalizationAuthorizationError) as fenced:
        await race_service.consume(
            authorization_id=race_item.authorization_id,
            nonce_base64=race_item.start_nonce_base64,
        )
    assert fenced.value.code == "stable_remote_finalization_control_changed"
    assert (
        await EvolutionStableRemoteFinalizationAuthorizationStore(
            race_fixture.db_path
        ).consumption(race_item.authorization_id)
        is None
    )

    expiry_fixture = await _authorization_fixture(tmp_path / "expiry-case")
    expiry_service = expiry_fixture.make_service()
    expiring = await expiry_service.issue(
        probe_receipt_id=expiry_fixture.probe.receipt.receipt_id,
        validity_seconds=60,
    )
    expiry_fixture.data.clock.value += timedelta(seconds=61)
    expired = await expiry_service.inspect(
        authorization_id=expiring.envelope.authorization.authorization_id
    )
    assert expired.expired
    assert not expired.remote_finalization_authority
    reissued = await expiry_service.issue(
        probe_receipt_id=expiry_fixture.probe.receipt.receipt_id,
        validity_seconds=60,
    )
    reissued_item = reissued.envelope.authorization
    assert reissued_item.attempt == 2
    assert (
        reissued_item.previous_authorization_id
        == expiring.envelope.authorization.authorization_id
    )
    with pytest.raises(EvolutionStableRemoteFinalizationAuthorizationError) as nonce:
        await expiry_service.consume(
            authorization_id=reissued_item.authorization_id,
            nonce_base64=base64.b64encode(b"z" * 32).decode(),
        )
    assert nonce.value.code == "stable_remote_finalization_nonce_mismatch"
    assert (
        await expiry_service.store.consumption(reissued_item.authorization_id)
        is None
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_authorization_rejects_forged_signature_and_corrupt_store(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    service = fixture.make_service()
    view = await service.issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
    )
    envelope = view.envelope
    attacker = Ed25519PrivateKey.generate()
    forged_bytes = attacker.sign(envelope.authorization.canonical_bytes())
    core = envelope.signature.model_dump(
        mode="json",
        exclude={"signature_id", "signature_artifact_sha256"},
    )
    core["signature_base64"] = base64.b64encode(forged_bytes).decode()
    core["signature_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    digest = hashlib.sha256(json.dumps(
        core,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    forged_signature = envelope.signature.model_validate({
        **core,
        "signature_id": f"relrolloutsig_{digest[:24]}",
        "signature_artifact_sha256": digest,
    })
    forged = EvolutionStableRemoteFinalizationAuthorizationEnvelope(
        authorization=envelope.authorization,
        signature=forged_signature,
    )
    with pytest.raises(ReleaseRolloutControlKeyError) as untrusted:
        verify_stable_remote_finalization_authorization(
            trust_policy=fixture.policy,
            envelope=forged,
            expected_member_id=envelope.authorization.installation_member_id,
            now=fixture.data.clock(),
        )
    assert getattr(untrusted.value, "code", "") == (
        "release_rollout_control_signature_untrusted"
    )

    with sqlite3.connect(fixture.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_remote_finalization_authorizations "
            "SET envelope_json = '{}' WHERE authorization_id = ?",
            (envelope.authorization.authorization_id,),
        )
        db.commit()
    with pytest.raises(EvolutionStableRemoteFinalizationAuthorizationError) as corrupt:
        await service.inspect(
            authorization_id=envelope.authorization.authorization_id
        )
    assert corrupt.value.code == "stable_remote_finalization_store_corrupt"
