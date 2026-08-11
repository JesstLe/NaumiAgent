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

import naumi_agent.release as release_api
import naumi_agent.release.installation_keys as installation_keys
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.release.installation_keys import (
    ReleaseInstallationKeyError,
    ReleaseInstallationKeyService,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import ReleasePopulationRegistrySigner

T0 = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def set_password(self, service: str, account: str, value: str) -> None:
        with self._lock:
            self.values[(service, account)] = value
            self.set_calls.append((service, account))

    def get_password(self, service: str, account: str) -> str | None:
        with self._lock:
            self.get_calls.append((service, account))
            return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        with self._lock:
            self.values.pop((service, account), None)
            self.delete_calls.append((service, account))


class _FailingBackend:
    def set_password(self, _service: str, _account: str, _value: str) -> None:
        raise RuntimeError("backend leaked private value")

    def get_password(self, _service: str, _account: str) -> str | None:
        raise RuntimeError("backend leaked private value")

    def delete_password(self, _service: str, _account: str) -> None:
        raise RuntimeError("backend leaked private value")


def _registry() -> ReleasePopulationRegistrySigner:
    return ReleasePopulationRegistrySigner.from_private_keys_base64(
        registry_id="naumi-production-registry",
        key_id="population-2026-01",
        key_generation=1,
        signing_private_key_base64=base64.b64encode(bytes(range(32))).decode(),
        pseudonym_key_base64=base64.b64encode(bytes(reversed(range(32)))).decode(),
    )


@pytest.mark.asyncio
async def test_installation_key_concurrent_provision_converges_without_secret_file(
    tmp_path: Path,
) -> None:
    backend = _MemoryBackend()
    assert (
        release_api.ReleaseInstallationKeyService
        is ReleaseInstallationKeyService
    )
    factory_calls: list[int] = []
    factory_lock = threading.Lock()

    def factory(size: int) -> bytes:
        with factory_lock:
            factory_calls.append(size)
            index = len(factory_calls)
        return hashlib.sha256(f"installation-{index}".encode()).digest()

    services = tuple(
        ReleaseInstallationKeyService(
            tmp_path / "release-root",
            backend=backend,
            key_factory=factory,
            clock=lambda: T0,
        )
        for _ in range(8)
    )
    handles = await asyncio.gather(*(
        asyncio.to_thread(service.provision, channel="stable")
        for service in services
    ))

    assert len({item.key_id for item in handles}) == 1
    assert factory_calls == [32]
    assert len(backend.set_calls) == 1
    handle = handles[0]
    metadata_path = services[0].metadata_path
    raw = metadata_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in raw
    assert backend.values[(handle.keyring_service, handle.keyring_account)] not in raw
    assert "private" in raw
    assert not os.stat(metadata_path).st_mode & 0o077

    public_only = ReleaseInstallationKeyService(
        tmp_path / "release-root",
        backend=_FailingBackend(),
    )
    assert public_only.inspect() == handle


def test_installation_key_signs_only_matching_credential_and_fixed_domain(
    tmp_path: Path,
) -> None:
    backend = _MemoryBackend()
    service = ReleaseInstallationKeyService(
        tmp_path / "release-root",
        backend=backend,
        key_factory=lambda size: bytes(range(size)),
        clock=lambda: T0,
    )
    handle = service.provision(channel="stable")
    credential = _registry().issue_credential(
        installation_public_key_base64=handle.public_key_base64,
        channel="stable",
        registered_at=(T0 - timedelta(days=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
    )
    payload = b'{"probe":"active-pointer-and-rollback-slot"}'

    first = service.sign_remote_readiness_probe(
        credential=credential,
        payload=payload,
    )
    second = service.sign_remote_readiness_probe(
        credential=credential,
        payload=payload,
    )
    assert first == second
    assert not first.private_key_exposed
    verify_release_installation_signature(
        credential=credential,
        payload=payload,
        artifact=first,
    )
    with pytest.raises(ReleaseInstallationKeyError) as changed:
        verify_release_installation_signature(
            credential=credential,
            payload=payload + b"changed",
            artifact=first,
        )
    assert changed.value.code == "release_installation_signature_binding_mismatch"
    tampered_time = first.model_copy(
        update={"signed_at": (T0 + timedelta(seconds=1)).isoformat()}
    )
    with pytest.raises(ReleaseInstallationKeyError) as unsigned_metadata:
        verify_release_installation_signature(
            credential=credential,
            payload=payload,
            artifact=tampered_time,
        )
    assert unsigned_metadata.value.code == "release_installation_signature_untrusted"

    other = ReleaseInstallationKeyService(
        tmp_path / "other-root",
        backend=_MemoryBackend(),
        key_factory=lambda size: bytes(reversed(range(size))),
        clock=lambda: T0,
    ).provision(channel="stable")
    wrong_credential = _registry().issue_credential(
        installation_public_key_base64=other.public_key_base64,
        channel="stable",
        registered_at=(T0 - timedelta(days=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
    )
    with pytest.raises(ReleaseInstallationKeyError) as mismatch:
        service.sign_remote_readiness_probe(
            credential=wrong_credential,
            payload=payload,
        )
    assert mismatch.value.code == "release_installation_credential_mismatch"
    assert len(backend.get_calls) == 0


def test_installation_key_missing_backend_failure_and_metadata_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release-root"
    missing = ReleaseInstallationKeyService(root, backend=_FailingBackend())
    with pytest.raises(ReleaseInstallationKeyError) as absent:
        missing.inspect()
    assert absent.value.code == "release_installation_key_missing"

    with pytest.raises(ReleaseInstallationKeyError) as unavailable:
        missing.provision(channel="stable")
    assert unavailable.value.code == "release_installation_key_provision_failed"
    assert "backend leaked" not in str(unavailable.value)
    assert not missing.metadata_path.exists()

    backend = _MemoryBackend()
    service = ReleaseInstallationKeyService(
        root,
        backend=backend,
        key_factory=lambda size: b"k" * size,
        clock=lambda: T0,
    )
    handle = service.provision(channel="stable")
    payload = json.loads(service.metadata_path.read_text(encoding="utf-8"))
    payload["public_key_sha256"] = "f" * 64
    service.metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(service.metadata_path, 0o600)
    with pytest.raises(ReleaseInstallationKeyError) as corrupt:
        service.inspect()
    assert corrupt.value.code == "release_installation_key_metadata_invalid"

    service.metadata_path.unlink()
    service.metadata_path.symlink_to(tmp_path / handle.key_id)
    with pytest.raises(ReleaseInstallationKeyError) as symlinked:
        service.inspect()
    assert symlinked.value.code == "release_installation_key_metadata_invalid"

    rollback_root = tmp_path / "rollback-root"
    rollback_backend = _MemoryBackend()
    rollback_service = ReleaseInstallationKeyService(
        rollback_root,
        backend=rollback_backend,
        key_factory=lambda size: b"r" * size,
        clock=lambda: T0,
    )
    original_write = installation_keys._write_handle_once

    def _fail_after_metadata(path, item) -> None:
        original_write(path, item)
        raise OSError("injected post-replace failure")

    monkeypatch.setattr(installation_keys, "_write_handle_once", _fail_after_metadata)
    with pytest.raises(ReleaseInstallationKeyError) as rolled_back:
        rollback_service.provision(channel="stable")
    assert rolled_back.value.code == "release_installation_key_provision_failed"
    assert not rollback_service.metadata_path.exists()
    assert rollback_backend.values == {}
    assert len(rollback_backend.delete_calls) == 1


@pytest.mark.asyncio
async def test_engine_is_keyring_lazy_and_slash_provisions_through_shared_tool(
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
        service = engine.release_installation_key_service
        service._backend_override = backend
        service.key_factory = lambda size: b"e" * size
        service.clock = lambda: T0

        provisioned = await execute_slash_command(
            engine,
            "/evolution installation-key provision stable",
        )
        assert "状态：**已初始化**" in strip_ansi(provisioned)
        assert len(backend.set_calls) == 1
        inspected = await execute_slash_command(
            engine,
            "/evolution installation-key inspect",
        )
        assert strip_ansi(inspected) == strip_ansi(provisioned)
        assert len(backend.get_calls) == 0
    finally:
        await engine.shutdown()
