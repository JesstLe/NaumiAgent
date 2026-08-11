from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import naumi_agent.release as release_api
import naumi_agent.release.rollout_control_keys as rollout_keys
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlKeyError,
    ReleaseRolloutControlKeyService,
    ReleaseRolloutControlSignature,
    ReleaseRolloutControlSignerIdentity,
    ReleaseTrustedRolloutControlKey,
    create_release_rollout_control_trust_policy,
    load_release_rollout_control_trust_policy,
    rollout_control_signature_message,
    verify_release_rollout_control_signature,
)
from naumi_agent.tools.evolution_review import (
    EvolutionRolloutControlKeyTool,
    EvolutionStableRemoteFinalizationAuthorizationTool,
)

T0 = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def set_password(self, service: str, account: str, value: str) -> None:
        with self.lock:
            self.values[(service, account)] = value
            self.set_calls.append((service, account))

    def get_password(self, service: str, account: str) -> str | None:
        with self.lock:
            self.get_calls.append((service, account))
            return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        with self.lock:
            self.values.pop((service, account), None)
            self.delete_calls.append((service, account))


class _FailingBackend:
    def set_password(self, _service: str, _account: str, _value: str) -> None:
        raise RuntimeError("secret backend failure")

    def get_password(self, _service: str, _account: str) -> str | None:
        raise RuntimeError("secret backend failure")

    def delete_password(self, _service: str, _account: str) -> None:
        raise RuntimeError("secret backend failure")


def _trusted(handle, *, state="active", channels=("stable",)):
    return ReleaseTrustedRolloutControlKey(
        identity=handle.signer_identity(),
        state=state,
        channels=channels,
        valid_from=(T0 - timedelta(days=1)).isoformat(),
        valid_until=(T0 + timedelta(days=1)).isoformat(),
        revoked_at=T0.isoformat() if state == "revoked" else None,
    )


@pytest.mark.asyncio
async def test_rollout_control_key_concurrent_provision_is_keyring_lazy(
    tmp_path: Path,
) -> None:
    assert release_api.ReleaseRolloutControlKeyService is ReleaseRolloutControlKeyService
    backend = _MemoryBackend()
    calls: list[int] = []
    call_lock = threading.Lock()

    def factory(size: int) -> bytes:
        with call_lock:
            calls.append(size)
            index = len(calls)
        return hashlib.sha256(f"rollout-control-{index}".encode()).digest()

    services = tuple(
        ReleaseRolloutControlKeyService(
            tmp_path / "release-root",
            backend=backend,
            key_factory=factory,
            clock=lambda: T0,
        )
        for _ in range(8)
    )
    handles = await asyncio.gather(*(
        asyncio.to_thread(
            service.provision,
            control_plane_id="naumi-control-plane",
            key_generation=1,
        )
        for service in services
    ))
    assert len({item.key_id for item in handles}) == 1
    assert calls == [32]
    assert len(backend.set_calls) == 1
    handle = handles[0]
    raw = services[0].metadata_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in raw
    assert backend.values[(handle.keyring_service, handle.keyring_account)] not in raw
    assert not os.stat(services[0].metadata_path).st_mode & 0o077
    assert not handle.automatic_trust_policy_installation

    public_only = ReleaseRolloutControlKeyService(
        tmp_path / "release-root",
        backend=_FailingBackend(),
    )
    assert public_only.inspect() == handle


def test_rollout_control_signature_requires_exact_active_trust_and_channel(
    tmp_path: Path,
) -> None:
    backend = _MemoryBackend()
    service = ReleaseRolloutControlKeyService(
        tmp_path / "release-root",
        backend=backend,
        key_factory=lambda size: bytes(range(size)),
        clock=lambda: T0,
    )
    handle = service.provision(
        control_plane_id="naumi-control-plane",
        key_generation=3,
    )
    policy = create_release_rollout_control_trust_policy((_trusted(handle),))
    payload = b'{"authorization":"exact-member-finalization"}'
    artifact = service.sign_authorization(channel="stable", payload=payload)
    trusted = verify_release_rollout_control_signature(
        trust_policy=policy,
        channel="stable",
        payload=payload,
        artifact=artifact,
    )
    assert trusted.identity == handle.signer_identity()
    assert not artifact.private_key_exposed
    assert not artifact.rollout_authority

    ambiguous_identity = handle.signer_identity().model_copy(
        update={"key_id": "relrolloutkey_" + "f" * 24}
    )
    ambiguous = _trusted(handle).model_copy(
        update={"identity": ambiguous_identity}
    )
    with pytest.raises(ValueError, match="唯一"):
        create_release_rollout_control_trust_policy((
            _trusted(handle),
            ambiguous,
        ))

    with pytest.raises(ReleaseRolloutControlKeyError) as channel:
        verify_release_rollout_control_signature(
            trust_policy=policy,
            channel="beta",
            payload=payload,
            artifact=artifact,
        )
    assert channel.value.code == "release_rollout_control_signature_binding_mismatch"

    revoked = create_release_rollout_control_trust_policy((
        _trusted(handle, state="revoked"),
    ))
    with pytest.raises(ReleaseRolloutControlKeyError) as inactive:
        verify_release_rollout_control_signature(
            trust_policy=revoked,
            channel="stable",
            payload=payload,
            artifact=artifact,
        )
    assert inactive.value.code == "release_rollout_control_signer_not_authorized"

    forged_bytes = b"f" * 64
    forged_core = artifact.model_dump(
        mode="json",
        exclude={"signature_id", "signature_artifact_sha256"},
    )
    forged_core["signature_base64"] = base64.b64encode(forged_bytes).decode()
    forged_core["signature_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    digest = hashlib.sha256(json.dumps(
        forged_core,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    forged = ReleaseRolloutControlSignature.model_validate({
        **forged_core,
        "signature_id": f"relrolloutsig_{digest[:24]}",
        "signature_artifact_sha256": digest,
    })
    with pytest.raises(ReleaseRolloutControlKeyError) as untrusted:
        verify_release_rollout_control_signature(
            trust_policy=policy,
            channel="stable",
            payload=payload,
            artifact=forged,
        )
    assert untrusted.value.code == "release_rollout_control_signature_untrusted"

    attacker = Ed25519PrivateKey.generate()
    attacker_public = attacker.public_key().public_bytes_raw()
    substituted_signer = ReleaseRolloutControlSignerIdentity(
        control_plane_id=artifact.signer.control_plane_id,
        key_id=artifact.signer.key_id,
        key_generation=artifact.signer.key_generation,
        public_key_base64=base64.b64encode(attacker_public).decode(),
        public_key_sha256=hashlib.sha256(attacker_public).hexdigest(),
    )
    substituted_core = artifact.model_dump(
        mode="json",
        exclude={"signature_id", "signature_artifact_sha256"},
    )
    substituted_core["signer"] = substituted_signer.model_dump(mode="json")
    substituted_bytes = attacker.sign(rollout_control_signature_message(
        signer=substituted_signer,
        channel="stable",
        payload=payload,
        signed_at=artifact.signed_at,
    ))
    substituted_core["signature_base64"] = base64.b64encode(
        substituted_bytes
    ).decode()
    substituted_core["signature_sha256"] = hashlib.sha256(
        substituted_bytes
    ).hexdigest()
    substituted_digest = hashlib.sha256(json.dumps(
        substituted_core,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    substituted = ReleaseRolloutControlSignature.model_validate({
        **substituted_core,
        "signature_id": f"relrolloutsig_{substituted_digest[:24]}",
        "signature_artifact_sha256": substituted_digest,
    })
    with pytest.raises(ReleaseRolloutControlKeyError) as identity_swap:
        verify_release_rollout_control_signature(
            trust_policy=policy,
            channel="stable",
            payload=payload,
            artifact=substituted,
        )
    assert identity_swap.value.code == "release_rollout_control_signer_untrusted"


def test_rollout_control_trust_loader_and_metadata_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release-root"
    backend = _MemoryBackend()
    service = ReleaseRolloutControlKeyService(
        root,
        backend=backend,
        key_factory=lambda size: b"k" * size,
        clock=lambda: T0,
    )
    handle = service.provision()
    policy = create_release_rollout_control_trust_policy((_trusted(handle),))
    policy_path = root / "trust" / "trusted-rollout-controls.json"
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    assert load_release_rollout_control_trust_policy(policy_path) == policy

    if os.name != "nt":
        os.chmod(policy_path, 0o666)
        with pytest.raises(ReleaseRolloutControlKeyError) as writable_policy:
            load_release_rollout_control_trust_policy(policy_path)
        assert writable_policy.value.code == (
            "release_rollout_control_trust_permissions_invalid"
        )
        os.chmod(policy_path, 0o644)

    symlink = root / "trust" / "rollout-policy-link.json"
    symlink.symlink_to(policy_path)
    with pytest.raises(ReleaseRolloutControlKeyError) as unsafe_policy:
        load_release_rollout_control_trust_policy(symlink)
    assert unsafe_policy.value.code == "release_rollout_control_trust_file_invalid"

    metadata = json.loads(service.metadata_path.read_text(encoding="utf-8"))
    metadata["public_key_sha256"] = "f" * 64
    service.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    os.chmod(service.metadata_path, 0o600)
    with pytest.raises(ReleaseRolloutControlKeyError) as corrupt:
        service.inspect()
    assert corrupt.value.code == "release_rollout_control_key_metadata_invalid"

    rollback_root = tmp_path / "rollback-root"
    rollback_backend = _MemoryBackend()
    rollback_service = ReleaseRolloutControlKeyService(
        rollback_root,
        backend=rollback_backend,
        key_factory=lambda size: b"r" * size,
        clock=lambda: T0,
    )
    original_write = rollout_keys._write_handle_once

    def _fail_after_metadata(path, item) -> None:
        original_write(path, item)
        raise OSError("injected post-replace failure")

    monkeypatch.setattr(rollout_keys, "_write_handle_once", _fail_after_metadata)
    with pytest.raises(ReleaseRolloutControlKeyError) as rolled_back:
        rollback_service.provision()
    assert rolled_back.value.code == "release_rollout_control_key_provision_failed"
    assert not rollback_service.metadata_path.exists()
    assert rollback_backend.values == {}
    assert len(rollback_backend.delete_calls) == 1


@pytest.mark.asyncio
async def test_engine_is_rollout_keyring_lazy_and_slash_uses_shared_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "installed-release"
    monkeypatch.setattr(
        "naumi_agent.orchestrator.engine.default_release_root",
        lambda: release_root,
    )
    backend = _MemoryBackend()
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
                vector_db_path=str(tmp_path / ".naumi" / "chroma"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        assert backend.get_calls == backend.set_calls == []
        service = engine.release_rollout_control_key_service
        service._backend_override = backend
        service.key_factory = lambda size: b"e" * size
        service.clock = lambda: T0
        tool = engine.tool_registry.get("evolution_rollout_control_key")
        assert isinstance(tool, EvolutionRolloutControlKeyTool)
        assert tool._engine is engine
        assert engine.evolution_release_rollout_control_trust_policy_path == (
            release_root / "trust" / "trusted-rollout-controls.json"
        )
        remote_service = (
            engine.evolution_stable_remote_finalization_authorization_service
        )
        assert remote_service.rollout_key_service is service
        assert remote_service.trust_policy_path == (
            engine.evolution_release_rollout_control_trust_policy_path
        )
        assert isinstance(
            engine.tool_registry.get(
                "evolution_stable_remote_finalization_authorization"
            ),
            EvolutionStableRemoteFinalizationAuthorizationTool,
        )

        provisioned = await execute_slash_command(
            engine,
            "/evolution rollout-control-key provision naumi-control-plane 1",
        )
        assert "状态：**已初始化**" in strip_ansi(provisioned)
        assert len(backend.set_calls) == 1
        inspected = await execute_slash_command(
            engine,
            "/evolution rollout-control-key inspect",
        )
        assert strip_ansi(inspected) == strip_ansi(provisioned)
        assert len(backend.get_calls) == 0
    finally:
        await engine.shutdown()
