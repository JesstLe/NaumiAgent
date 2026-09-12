from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import (
    AppConfig,
    HarnessConfig,
    MemoryConfig,
    StableRemoteFinalizationInstallationDaemonConfig,
    StableRemoteFinalizationResultHTTPTransportConfig,
)
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryService,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationDeliveryWorker,
    EvolutionStableRemoteFinalizationDeliveryWorkerPolicy,
    EvolutionStableRemoteFinalizationTransportError,
)
from naumi_agent.evolution.stable_remote_finalization_http_transport import (
    STABLE_REMOTE_FINALIZATION_HTTP_PATH,
    MTLSStableRemoteFinalizationInstallationTransport,
    StableRemoteFinalizationHTTPClientPolicy,
    StableRemoteFinalizationHTTPServerPolicy,
)
from naumi_agent.evolution.stable_remote_finalization_installation_daemon import (
    ResolvingStableRemoteFinalizationInstallationTransport,
    StableRemoteFinalizationInstallationDaemon,
    StableRemoteFinalizationInstallationDaemonPolicy,
    StableRemoteFinalizationInstallationDaemonState,
    StableRemoteFinalizationInstallationDiscovery,
    StableRemoteFinalizationInstallationDiscoveryDescriptor,
)
from naumi_agent.evolution.stable_remote_finalization_result_http_transport import (
    STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH,
    MTLSStableRemoteFinalizationResultTransport,
    StableRemoteFinalizationResultHTTPClientPolicy,
    StableRemoteFinalizationResultHTTPServer,
    StableRemoteFinalizationResultHTTPServerPolicy,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator import engine as engine_module
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_ports,
    build_runtime_resources,
    build_runtime_services,
)
from tests.unit.test_evolution_stable_remote_finalization_deliveries import _delivery
from tests.unit.test_evolution_stable_remote_finalization_http_transport import (
    _tls_bundle,
    _TLSBundle,
)
from tests.unit.test_evolution_stable_remote_finalization_result_return_worker import (
    _ready,
    _worker,
)


def _inbound_policy(bundle: _TLSBundle) -> StableRemoteFinalizationHTTPServerPolicy:
    return StableRemoteFinalizationHTTPServerPolicy(
        server_certificate_path=bundle.server_cert,
        server_private_key_path=bundle.server_key,
        client_ca_path=bundle.ca,
        authorized_client_certificate_sha256=(bundle.client_fingerprint,),
        tls_handshake_timeout_seconds=1,
        request_timeout_seconds=2,
        requests_per_minute=100,
        max_concurrent_requests=4,
    )


def _discovery_descriptor(
    instance_character: str,
) -> StableRemoteFinalizationInstallationDiscoveryDescriptor:
    timestamp = datetime.now(UTC).isoformat()
    core = {
        "schema_version": 1,
        "policy_version": "stable-remote-finalization-installation-discovery-v1",
        "installation_member_id": "relpopmember_" + "a" * 24,
        "endpoint_url": (
            f"https://127.0.0.1:8443{STABLE_REMOTE_FINALIZATION_HTTP_PATH}"
        ),
        "server_certificate_sha256": "b" * 64,
        "instance_id": "stable-installation-" + instance_character * 32,
        "lease_epoch": 1,
        "process_id": 123,
        "started_at": timestamp,
        "updated_at": timestamp,
        "worker_state": "waiting",
        "worker_pass_count": 0,
        "worker_returned_count": 0,
        "worker_dead_lettered_count": 0,
        "worker_failure_count": 0,
        "failure_code": "",
    }
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return StableRemoteFinalizationInstallationDiscoveryDescriptor(
        descriptor_sha256=digest,
        **core,
    )


def _control_client(
    bundle: _TLSBundle, endpoint_url: str
) -> MTLSStableRemoteFinalizationInstallationTransport:
    return MTLSStableRemoteFinalizationInstallationTransport(
        StableRemoteFinalizationHTTPClientPolicy(
            endpoint_url=endpoint_url,
            server_ca_path=bundle.ca,
            client_certificate_path=bundle.client_cert,
            client_private_key_path=bundle.client_key,
            server_certificate_sha256_pins=(bundle.server_fingerprint,),
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
        )
    )


def _result_client(
    bundle: _TLSBundle, port: int
) -> MTLSStableRemoteFinalizationResultTransport:
    return MTLSStableRemoteFinalizationResultTransport(
        StableRemoteFinalizationResultHTTPClientPolicy(
            endpoint_url=(
                f"https://127.0.0.1:{port}"
                f"{STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH}"
            ),
            server_ca_path=bundle.ca,
            client_certificate_path=bundle.next_client_cert,
            client_private_key_path=bundle.next_client_key,
            server_certificate_sha256_pins=(bundle.next_server_fingerprint,),
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
        )
    )


def _daemon(
    *,
    tmp_path: Path,
    fixture,
    delivery_service,
    credential,
    journal,
    bundle: _TLSBundle,
    authority: HarnessStore,
    result_transport=None,
    now_provider=lambda: datetime.now(UTC),
) -> StableRemoteFinalizationInstallationDaemon:
    async def resolve(package):
        assert package.installation_member_id == credential.payload.member_id
        return credential

    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
        transport=result_transport,
    )
    inbound = ResolvingStableRemoteFinalizationInstallationTransport(
        installation_member_id=credential.payload.member_id,
        journal=journal,
        installation_key_service=fixture.data.key_service,
        trust_policy_provider=lambda: fixture.policy,
        credential_resolver=resolve,
        clock=fixture.data.clock,
    )
    return StableRemoteFinalizationInstallationDaemon(
        policy=StableRemoteFinalizationInstallationDaemonPolicy(
            installation_member_id=credential.payload.member_id,
            bind_host="127.0.0.1",
            advertise_host="127.0.0.1",
            port=0,
            lease_seconds=10,
            renew_interval_seconds=3,
            heartbeat_interval_seconds=1,
            heartbeat_timeout_seconds=3,
        ),
        server_policy=_inbound_policy(bundle),
        inbound_transport=inbound,
        result_worker=worker,
        authority=authority,
        workspace_root=tmp_path.resolve(),
        discovery=StableRemoteFinalizationInstallationDiscovery(
            (tmp_path / "runtime" / "installation.json").resolve()
        ),
        now_provider=now_provider,
    )


async def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_daemon_closes_real_delivery_to_result_mtls_loop(
    tmp_path: Path,
) -> None:
    fixture, finalization_service, delivery_store, delivery, credential, journal = (
        await _delivery(tmp_path / "release")
    )
    delivery_service = EvolutionStableRemoteFinalizationDeliveryService(
        finalization_service=finalization_service,
        store=delivery_store,
        clock=fixture.data.clock,
    )
    bundle = _tls_bundle(tmp_path / "tls")
    result_server = StableRemoteFinalizationResultHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StableRemoteFinalizationResultHTTPServerPolicy(
            server_certificate_path=bundle.next_server_cert,
            server_private_key_path=bundle.next_server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                credential.payload.member_id: (bundle.next_client_fingerprint,)
            },
        ),
        service=delivery_service,
    )
    result_server.start()
    daemon = _daemon(
        tmp_path=tmp_path,
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
        bundle=bundle,
        authority=HarnessStore(tmp_path / "harness.db"),
        result_transport=_result_client(bundle, result_server.bound_port),
    )
    try:
        assert await daemon.start()
        descriptor = daemon.discovery.read()
        assert descriptor is not None
        assert descriptor.server_certificate_sha256 == bundle.server_fingerprint
        assert descriptor.endpoint_url.endswith(STABLE_REMOTE_FINALIZATION_HTTP_PATH)

        control_transport = _control_client(bundle, descriptor.endpoint_url)
        control_worker = EvolutionStableRemoteFinalizationDeliveryWorker(
            service=delivery_service,
            store=delivery_store,
            transport=control_transport,
            policy=EvolutionStableRemoteFinalizationDeliveryWorkerPolicy(
                interval_seconds=0.1,
                max_empty_backoff_seconds=1,
                max_failure_backoff_seconds=1,
                claim_lease_seconds=3,
                scan_limit=10,
                ack_timeout_seconds=2,
                retry_base_seconds=0.1,
                retry_max_seconds=1,
                max_attempts=3,
                shutdown_drain_seconds=1,
                jitter_ratio=0,
            ),
            clock=fixture.data.clock,
        )
        delivered = await control_worker.run_once()
        assert delivered.acknowledged == 1

        async def completed_and_recorded() -> bool:
            current = await delivery_store.get(delivery.package.delivery_id)
            return (
                current is not None
                and current.latest_event.state == "completed"
                and daemon.snapshot().worker.returned_count == 1
            )

        async with asyncio.timeout(3):
            while not await completed_and_recorded():
                daemon.result_worker.wake()
                await asyncio.sleep(0.02)
        snapshot = daemon.snapshot()
        assert snapshot.state is StableRemoteFinalizationInstallationDaemonState.RUNNING
        assert snapshot.worker.returned_count == 1
        assert await daemon.supervise_once()
        inspection = await daemon.inspect()
        assert inspection.state == "running"
        assert inspection.heartbeat_health == "healthy"
        assert inspection.worker_returned_count == 1
        heartbeat = await daemon.authority.get_heartbeat(
            workspace_root=tmp_path,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id=(
                "stable-finalization-installation-"
                f"{snapshot.installation_member_sha256[:24]}"
            ),
        )
        assert heartbeat is not None
        assert heartbeat.phase is HarnessHeartbeatPhase.RUNNING
    finally:
        await daemon.stop()
        result_server.stop()

    assert daemon.discovery.read() is None
    assert daemon.snapshot().state is StableRemoteFinalizationInstallationDaemonState.STOPPED
    with pytest.raises(EvolutionStableRemoteFinalizationTransportError):
        await control_transport.receive(delivery.package)


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_daemon_has_single_owner_and_restart_uses_higher_epoch(
    tmp_path: Path,
) -> None:
    fixture, service, _, _, credential, journal = await _ready(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    authority = HarnessStore(tmp_path / "harness.db")
    first = _daemon(
        tmp_path=tmp_path,
        fixture=fixture,
        delivery_service=service,
        credential=credential,
        journal=journal,
        bundle=bundle,
        authority=authority,
    )
    second = _daemon(
        tmp_path=tmp_path,
        fixture=fixture,
        delivery_service=service,
        credential=credential,
        journal=journal,
        bundle=bundle,
        authority=authority,
    )

    assert await first.start()
    first_epoch = first.snapshot().lease_epoch
    assert not await second.start()
    assert second.snapshot().state is StableRemoteFinalizationInstallationDaemonState.STANDBY
    await first.stop()

    assert await second.start()
    assert second.snapshot().lease_epoch == first_epoch + 1
    await second.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_daemon_lost_lease_fails_closed_without_removing_new_descriptor(
    tmp_path: Path,
) -> None:
    fixture, service, _, _, credential, journal = await _ready(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    authority = HarnessStore(tmp_path / "harness.db")

    class Clock:
        value = datetime.now(UTC)

        def __call__(self):
            return self.value

    clock = Clock()
    daemon = _daemon(
        tmp_path=tmp_path,
        fixture=fixture,
        delivery_service=service,
        credential=credential,
        journal=journal,
        bundle=bundle,
        authority=authority,
        now_provider=clock,
    )
    assert await daemon.start()
    first = daemon.discovery.read()
    assert first is not None
    clock.value += timedelta(seconds=11)
    lease = await authority.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=f"stable-finalization-installation-{daemon.snapshot().installation_member_sha256[:24]}",
        owner_id="replacement-owner",
        now=clock().isoformat(),
        lease_seconds=10,
    )
    assert lease is not None and lease.epoch == first.lease_epoch + 1
    replacement = first.model_copy(
        update={
            "instance_id": "stable-installation-" + "f" * 32,
            "lease_epoch": lease.epoch,
            "updated_at": clock().isoformat(),
        }
    )
    core = replacement.model_dump(mode="json", exclude={"descriptor_sha256"})
    replacement = replacement.model_copy(
        update={
            "descriptor_sha256": hashlib.sha256(
                json.dumps(
                    core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        }
    )
    daemon.discovery.publish(replacement)

    assert not await daemon.supervise_once()
    await _wait_for(
        lambda: daemon.snapshot().state
        is StableRemoteFinalizationInstallationDaemonState.FAILED
    )
    terminal = await asyncio.wait_for(daemon.wait_terminated(), timeout=1)
    assert terminal.state is StableRemoteFinalizationInstallationDaemonState.FAILED
    assert daemon.discovery.read() == replacement
    assert daemon.snapshot().failure_code == "stable_installation_lease_lost"


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_daemon_recovers_expired_crash_lease_with_higher_epoch(
    tmp_path: Path,
) -> None:
    fixture, service, _, _, credential, journal = await _ready(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    authority = HarnessStore(tmp_path / "harness.db")

    class Clock:
        value = datetime.now(UTC)

        def __call__(self):
            return self.value

    clock = Clock()
    member_hash = hashlib.sha256(
        credential.payload.member_id.encode("utf-8")
    ).hexdigest()[:24]
    stale = await authority.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=f"stable-finalization-installation-{member_hash}",
        owner_id="crashed-owner",
        now=clock().isoformat(),
        lease_seconds=10,
    )
    assert stale is not None and stale.epoch == 1
    clock.value += timedelta(seconds=11)
    daemon = _daemon(
        tmp_path=tmp_path,
        fixture=fixture,
        delivery_service=service,
        credential=credential,
        journal=journal,
        bundle=bundle,
        authority=authority,
        now_provider=clock,
    )

    assert await daemon.start()
    assert daemon.snapshot().lease_epoch == 2
    assert daemon.discovery.read() is not None
    await daemon.stop()


def test_discovery_rejects_tamper_and_stale_instance_removal(tmp_path: Path) -> None:
    discovery = StableRemoteFinalizationInstallationDiscovery(
        (tmp_path / "runtime" / "installation.json").resolve()
    )
    descriptor = _discovery_descriptor("c")
    discovery.publish(descriptor)
    assert not discovery.remove(instance_id="stable-installation-" + "d" * 32)
    payload = json.loads(discovery.path.read_text(encoding="utf-8"))
    payload["lease_epoch"] = 2
    discovery.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OSError, match="无法验证"):
        discovery.read()


def test_discovery_serializes_exact_remove_against_replacement_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = StableRemoteFinalizationInstallationDiscovery(
        (tmp_path / "runtime" / "installation.json").resolve()
    )
    original_descriptor = _discovery_descriptor("c")
    replacement_descriptor = _discovery_descriptor("d")
    discovery.publish(original_descriptor)
    original_read = discovery.read
    removal_read = threading.Event()
    continue_removal = threading.Event()
    publish_done = threading.Event()
    errors: list[BaseException] = []
    removed: list[bool] = []

    def paused_read():
        descriptor = original_read()
        removal_read.set()
        if not continue_removal.wait(timeout=2):
            raise TimeoutError("test did not release discovery removal")
        return descriptor

    def remove_original() -> None:
        try:
            removed.append(discovery.remove(instance_id=original_descriptor.instance_id))
        except BaseException as exc:  # pragma: no cover - thread evidence
            errors.append(exc)

    def publish_replacement() -> None:
        try:
            discovery.publish(replacement_descriptor)
        except BaseException as exc:  # pragma: no cover - thread evidence
            errors.append(exc)
        finally:
            publish_done.set()

    monkeypatch.setattr(discovery, "read", paused_read)
    removal_thread = threading.Thread(target=remove_original)
    publish_thread = threading.Thread(target=publish_replacement)
    removal_thread.start()
    assert removal_read.wait(timeout=2)
    publish_thread.start()
    assert not publish_done.wait(timeout=0.1)
    continue_removal.set()
    removal_thread.join(timeout=2)
    publish_thread.join(timeout=2)

    assert not errors
    assert removed == [True]
    assert original_read() == replacement_descriptor


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink 需要安装权限")
def test_discovery_rejects_symlink_parent_and_lock(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    linked = StableRemoteFinalizationInstallationDiscovery(
        linked_directory / "installation.json"
    )
    with pytest.raises(OSError, match="普通目录"):
        linked.publish(_discovery_descriptor("c"))

    secure = StableRemoteFinalizationInstallationDiscovery(
        tmp_path / "secure" / "installation.json"
    )
    secure.path.parent.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("0", encoding="utf-8")
    secure.lock_path.symlink_to(lock_target)
    with pytest.raises(OSError):
        secure.publish(_discovery_descriptor("d"))


def test_daemon_policy_rejects_unsafe_advertisement_and_timing() -> None:
    member = "relpopmember_" + "a" * 24
    with pytest.raises(ValueError, match="unspecified"):
        StableRemoteFinalizationInstallationDaemonPolicy(
            installation_member_id=member,
            advertise_host="0.0.0.0",
        )
    with pytest.raises(ValueError, match="三分之一"):
        StableRemoteFinalizationInstallationDaemonPolicy(
            installation_member_id=member,
            lease_seconds=30,
            renew_interval_seconds=11,
        )


@pytest.mark.asyncio
async def test_runtime_composition_builds_enabled_daemon_without_starting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _tls_bundle(tmp_path / "tls")
    member = "relpopmember_" + "a" * 24
    harness = HarnessConfig(
        stable_remote_finalization_result_http_transport=(
            StableRemoteFinalizationResultHTTPTransportConfig(
                enabled=True,
                endpoint_url=(
                    "https://127.0.0.1:8444"
                    f"{STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH}"
                ),
                server_ca_path=str(bundle.ca),
                client_certificate_path=str(bundle.next_client_cert),
                client_private_key_path=str(bundle.next_client_key),
                server_certificate_sha256_pins=(bundle.next_server_fingerprint,),
            )
        ),
        stable_remote_finalization_installation_daemon=(
            StableRemoteFinalizationInstallationDaemonConfig(
                enabled=True,
                installation_member_id=member,
                bind_host="127.0.0.1",
                advertise_host="localhost",
                port=0,
                server_certificate_path=str(bundle.server_cert),
                server_private_key_path=str(bundle.server_key),
                control_plane_ca_path=str(bundle.ca),
                authorized_control_plane_certificate_sha256=(
                    bundle.client_fingerprint,
                ),
            )
        ),
    )
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "runtime" / "sessions.db"),
            vector_db_path=str(tmp_path / "runtime" / "chroma"),
            long_term_enabled=False,
        ),
        harness=harness,
    )
    monkeypatch.setattr(
        engine_module,
        "default_release_root",
        lambda: tmp_path / "release-root",
    )
    paths = build_runtime_paths(config)
    ports = build_runtime_ports(config, paths=paths)
    resources = build_runtime_resources(paths)
    services = build_runtime_services(config, paths=paths, resources=resources)
    assert services.stable_remote_finalization_installation_daemon_factory is not None
    engine = AgentEngine(
        config,
        ports=ports,
        paths=paths,
        resources=resources,
        services=services,
    )

    snapshot = engine.stable_remote_finalization_installation_daemon_snapshot()
    assert snapshot.state is StableRemoteFinalizationInstallationDaemonState.CREATED
    assert snapshot.lease_epoch == 0
    assert not snapshot.discovery_published
    result_worker = engine.evolution_stable_remote_finalization_result_return_worker
    assert result_worker is not None
    assert not result_worker.snapshot().started_at
    from naumi_agent.cli.slash_router import execute_slash_command
    from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
    from naumi_agent.tools.evolution_review import (
        EvolutionStableRemoteFinalizationTool,
    )

    tool = EvolutionStableRemoteFinalizationTool(engine)
    registry = ToolRegistry()
    registry.register(tool)

    class SlashEngine:
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

    direct = await tool.execute(action="inspect-installation-daemon")
    via_slash = await execute_slash_command(
        SlashEngine(),
        "/evolution stable-remote-finalization inspect-installation-daemon",
    )
    assert "**created**" in direct
    assert "**created**" in via_slash
    await engine.shutdown()


@pytest.mark.asyncio
async def test_interactive_engine_does_not_start_daemon_owned_result_worker() -> None:
    result_worker = SimpleNamespace(run_once=AsyncMock(), start=MagicMock())
    engine = SimpleNamespace(
        evolution_patch_set_recovery=SimpleNamespace(recover_pending=AsyncMock()),
        evolution_patch_recovery=SimpleNamespace(recover_pending=AsyncMock()),
        recover_session_reconciliations=AsyncMock(return_value=()),
        _config=SimpleNamespace(
            harness=SimpleNamespace(
                agent_publication_recovery=SimpleNamespace(enabled=False),
                pursuit_terminal_outbox=SimpleNamespace(enabled=False),
                stable_remote_finalization_delivery=SimpleNamespace(enabled=False),
                stable_remote_finalization_result_return=SimpleNamespace(enabled=True),
            )
        ),
        subagent_manager=SimpleNamespace(recover_pending_publications=AsyncMock()),
        evolution_stable_remote_finalization_delivery_worker=None,
        evolution_stable_remote_finalization_installation_daemon=object(),
        evolution_stable_remote_finalization_result_return_worker=result_worker,
        start_session_retention_worker=MagicMock(),
    )

    recovered = await AgentEngine.start_long_running_services(engine)

    assert recovered == ()
    result_worker.run_once.assert_not_awaited()
    result_worker.start.assert_not_called()
    engine.start_session_retention_worker.assert_called_once_with()


def test_daemon_config_fails_closed_for_partial_or_unowned_runtime() -> None:
    member = "relpopmember_" + "a" * 24
    with pytest.raises(ValueError, match="未显式 enabled"):
        StableRemoteFinalizationInstallationDaemonConfig(
            installation_member_id=member,
        )
    daemon = StableRemoteFinalizationInstallationDaemonConfig(
        enabled=True,
        installation_member_id=member,
        server_certificate_path="server.pem",
        server_private_key_path="server.key",
        control_plane_ca_path="control.pem",
        authorized_control_plane_certificate_sha256=("a" * 64,),
    )
    with pytest.raises(ValueError, match="Result Worker"):
        HarnessConfig(
            stable_remote_finalization_installation_daemon=daemon,
            stable_remote_finalization_result_http_transport=(
                StableRemoteFinalizationResultHTTPTransportConfig()
            ),
        )
