from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from naumi_agent.config.settings import (
    AppConfig,
    HarnessConfig,
    MemoryConfig,
    StableRemoteFinalizationDeliveryWorkerConfig,
    StableRemoteFinalizationHTTPTransportConfig,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationTransportError,
    LocalStableRemoteFinalizationInstallationTransport,
)
from naumi_agent.evolution.stable_remote_finalization_http_transport import (
    STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE,
    STABLE_REMOTE_FINALIZATION_HTTP_PATH,
    STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE,
    MTLSStableRemoteFinalizationInstallationTransport,
    StableRemoteFinalizationHTTPClientPolicy,
    StableRemoteFinalizationHTTPServer,
    StableRemoteFinalizationHTTPServerPolicy,
)
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_resources,
    build_runtime_services,
)
from tests.unit.test_evolution_stable_remote_finalization_deliveries import _delivery


@dataclass(frozen=True)
class _TLSBundle:
    ca: Path
    server_cert: Path
    server_key: Path
    server_fingerprint: str
    next_server_cert: Path
    next_server_key: Path
    next_server_fingerprint: str
    client_cert: Path
    client_key: Path
    client_fingerprint: str


def _write(path: Path, value: bytes, *, private: bool = False) -> Path:
    path.write_bytes(value)
    if os.name != "nt":
        path.chmod(0o600 if private else 0o644)
    return path


def _tls_bundle(tmp_path: Path) -> _TLSBundle:
    tmp_path.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Naumi test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(name: str, usage: ExtendedKeyUsageOID) -> tuple[bytes, bytes, str]:
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
        )
        if usage == ExtendedKeyUsageOID.SERVER_AUTH:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
        certificate = builder.sign(ca_key, hashes.SHA256())
        der = certificate.public_bytes(serialization.Encoding.DER)
        return (
            certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            hashlib.sha256(der).hexdigest(),
        )

    server_cert, server_key, server_fingerprint = leaf(
        "localhost", ExtendedKeyUsageOID.SERVER_AUTH
    )
    next_cert, next_key, next_fingerprint = leaf(
        "localhost-next", ExtendedKeyUsageOID.SERVER_AUTH
    )
    client_cert, client_key, client_fingerprint = leaf(
        "naumi-control-plane", ExtendedKeyUsageOID.CLIENT_AUTH
    )
    return _TLSBundle(
        ca=_write(tmp_path / "ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM)),
        server_cert=_write(tmp_path / "server.pem", server_cert),
        server_key=_write(tmp_path / "server.key", server_key, private=True),
        server_fingerprint=server_fingerprint,
        next_server_cert=_write(tmp_path / "server-next.pem", next_cert),
        next_server_key=_write(tmp_path / "server-next.key", next_key, private=True),
        next_server_fingerprint=next_fingerprint,
        client_cert=_write(tmp_path / "client.pem", client_cert),
        client_key=_write(tmp_path / "client.key", client_key, private=True),
        client_fingerprint=client_fingerprint,
    )


def _server(*, bundle: _TLSBundle, transport, next_certificate: bool = False, **policy):
    return StableRemoteFinalizationHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StableRemoteFinalizationHTTPServerPolicy(
            server_certificate_path=(
                bundle.next_server_cert if next_certificate else bundle.server_cert
            ),
            server_private_key_path=(
                bundle.next_server_key if next_certificate else bundle.server_key
            ),
            client_ca_path=bundle.ca,
            authorized_client_certificate_sha256=(bundle.client_fingerprint,),
            **policy,
        ),
        transport=transport,
    )


def _client(*, bundle: _TLSBundle, port: int, pins: tuple[str, ...] | None = None):
    return MTLSStableRemoteFinalizationInstallationTransport(
        StableRemoteFinalizationHTTPClientPolicy(
            endpoint_url=(
                f"https://127.0.0.1:{port}{STABLE_REMOTE_FINALIZATION_HTTP_PATH}"
            ),
            server_ca_path=bundle.ca,
            client_certificate_path=bundle.client_cert,
            client_private_key_path=bundle.client_key,
            server_certificate_sha256_pins=pins or (bundle.server_fingerprint,),
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
        )
    )


@pytest.mark.asyncio
async def test_mtls_transport_delivers_real_idempotent_installation_ack(
    tmp_path: Path,
) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    transport = LocalStableRemoteFinalizationInstallationTransport(
        journal=journal,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    server = _server(bundle=bundle, transport=transport)
    server.start()
    try:
        client = _client(bundle=bundle, port=server.bound_port)
        first = await client.receive(delivery.package)
        second = await client.receive(delivery.package)
    finally:
        server.stop()

    assert first == second
    assert first.payload.delivery_id == delivery.package.delivery_id
    assert first.payload.durable_journal_written
    assert not first.payload.writer_executed
    assert journal.db_path.is_file()


@pytest.mark.asyncio
async def test_server_pin_is_checked_before_package_is_sent(tmp_path: Path) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    try:
        client = _client(bundle=bundle, port=server.bound_port, pins=("0" * 64,))
        with pytest.raises(EvolutionStableRemoteFinalizationTransportError) as caught:
            await client.receive(delivery.package)
    finally:
        server.stop()

    assert caught.value.code == "stable_remote_http_server_pin_mismatch"
    assert not caught.value.retryable
    assert not journal.db_path.exists()


@pytest.mark.asyncio
async def test_current_next_server_pin_rotation_accepts_next_certificate(
    tmp_path: Path,
) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        next_certificate=True,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    try:
        client = _client(
            bundle=bundle,
            port=server.bound_port,
            pins=(bundle.server_fingerprint, bundle.next_server_fingerprint),
        )
        ack = await client.receive(delivery.package)
    finally:
        server.stop()

    assert ack.payload.delivery_id == delivery.package.delivery_id


@pytest.mark.asyncio
async def test_valid_ca_client_without_endpoint_authorization_is_permanent_failure(
    tmp_path: Path,
) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = StableRemoteFinalizationHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StableRemoteFinalizationHTTPServerPolicy(
            server_certificate_path=bundle.server_cert,
            server_private_key_path=bundle.server_key,
            client_ca_path=bundle.ca,
            authorized_client_certificate_sha256=("f" * 64,),
        ),
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    try:
        with pytest.raises(EvolutionStableRemoteFinalizationTransportError) as caught:
            await _client(bundle=bundle, port=server.bound_port).receive(delivery.package)
    finally:
        server.stop()

    assert caught.value.code == "stable_remote_http_status_403"
    assert not caught.value.retryable
    assert not journal.db_path.exists()


@pytest.mark.asyncio
async def test_mtls_server_rejects_client_without_certificate(tmp_path: Path) -> None:
    fixture, _, _, _, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(bundle.ca))
    try:
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                server.bound_port,
                ssl=context,
                server_hostname="127.0.0.1",
            )
        except (ConnectionError, OSError, ssl.SSLError):
            return
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), timeout=1) == b""
        except (BrokenPipeError, ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError, ssl.SSLError):
                pass
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_mtls_server_rejects_declared_oversized_body_without_reading_it(
    tmp_path: Path,
) -> None:
    fixture, _, _, _, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        max_request_bytes=128,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(bundle.ca))
    context.load_cert_chain(bundle.client_cert, bundle.client_key)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            server.bound_port,
            ssl=context,
            server_hostname="127.0.0.1",
        )
        request = (
            f"POST {STABLE_REMOTE_FINALIZATION_HTTP_PATH} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Type: {STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE}\r\n"
            f"Accept: {STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE}\r\n"
            "X-Naumi-Delivery-ID: evstableremotedelivery_000000000000000000000000\r\n"
            "Content-Length: 129\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        writer.close()
        await writer.wait_closed()
    finally:
        server.stop()

    assert response.startswith(b"HTTP/1.1 413 ")
    assert b'"code":"package_oversized"' in response
    assert b"Traceback" not in response


@pytest.mark.asyncio
async def test_target_processing_timeout_is_retryable_and_bounded(tmp_path: Path) -> None:
    _, _, _, delivery, _, _ = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")

    class HangingTransport:
        async def receive(self, _package):
            await asyncio.sleep(60)

    server = _server(
        bundle=bundle,
        transport=HangingTransport(),
        request_timeout_seconds=0.1,
    )
    server.start()
    try:
        with pytest.raises(EvolutionStableRemoteFinalizationTransportError) as caught:
            await _client(bundle=bundle, port=server.bound_port).receive(delivery.package)
    finally:
        server.stop()

    assert caught.value.code == "stable_remote_http_status_503"
    assert caught.value.retryable


@pytest.mark.asyncio
async def test_server_concurrency_cap_rejects_excess_without_extra_target_work(
    tmp_path: Path,
) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    local = LocalStableRemoteFinalizationInstallationTransport(
        journal=journal,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    class ControlledTransport:
        async def receive(self, package):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            entered.set()
            try:
                assert await asyncio.to_thread(release.wait, 2)
                return await local.receive(package)
            finally:
                with lock:
                    active -= 1

    server = _server(
        bundle=bundle,
        transport=ControlledTransport(),
        max_concurrent_requests=1,
    )
    server.start()
    client = _client(bundle=bundle, port=server.bound_port)
    first = asyncio.create_task(client.receive(delivery.package))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert server.active_requests == 1
        with pytest.raises(EvolutionStableRemoteFinalizationTransportError) as caught:
            await client.receive(delivery.package)
        assert caught.value.retryable
        release.set()
        ack = await first
    finally:
        release.set()
        if not first.done():
            await first
        server.stop()

    assert ack.payload.delivery_id == delivery.package.delivery_id
    assert max_active == 1


@pytest.mark.asyncio
async def test_slow_tls_handshake_is_bounded_and_listener_recovers(tmp_path: Path) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        max_concurrent_requests=1,
        tls_handshake_timeout_seconds=0.1,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    slow_socket = socket.create_connection(("127.0.0.1", server.bound_port), timeout=1)
    client = _client(bundle=bundle, port=server.bound_port)
    try:
        for _ in range(100):
            if server.active_requests == 1:
                break
            await asyncio.sleep(0.01)
        assert server.active_requests == 1
        with pytest.raises(EvolutionStableRemoteFinalizationTransportError) as caught:
            await client.receive(delivery.package)
        assert caught.value.retryable
        await asyncio.sleep(0.12)
        ack = await client.receive(delivery.package)
    finally:
        slow_socket.close()
        server.stop()

    assert ack.payload.delivery_id == delivery.package.delivery_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_headers", "expected_status", "expected_code"),
    [
        (
            "Content-Length: 1\r\nContent-Length: 1\r\n",
            b"HTTP/1.1 400 ",
            b'"code":"content_length_ambiguous"',
        ),
        (
            "Content-Length: 1\r\nExpect: 100-continue\r\n",
            b"HTTP/1.1 417 ",
            b'"code":"expectation_not_supported"',
        ),
    ],
)
async def test_ambiguous_length_and_expectation_are_rejected_before_body(
    tmp_path: Path,
    extra_headers: str,
    expected_status: bytes,
    expected_code: bytes,
) -> None:
    fixture, _, _, _, credential, journal = await _delivery(tmp_path / "release")
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )
    server.start()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(bundle.ca))
    context.load_cert_chain(bundle.client_cert, bundle.client_key)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            server.bound_port,
            ssl=context,
            server_hostname="127.0.0.1",
        )
        writer.write((
            f"POST {STABLE_REMOTE_FINALIZATION_HTTP_PATH} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Type: {STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE}\r\n"
            f"Accept: {STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE}\r\n"
            "X-Naumi-Delivery-ID: evstableremotedelivery_000000000000000000000000\r\n"
            f"{extra_headers}Connection: close\r\n\r\n"
        ).encode("ascii"))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        writer.close()
        await writer.wait_closed()
    finally:
        server.stop()

    assert response.startswith(expected_status)
    assert expected_code in response
    assert not journal.db_path.exists()


def test_tls_policy_rejects_insecure_private_key_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not authoritative on Windows")
    bundle = _tls_bundle(tmp_path)
    bundle.client_key.chmod(0o644)

    with pytest.raises(ValueError, match="不能允许 group/world 访问"):
        StableRemoteFinalizationHTTPClientPolicy(
            endpoint_url=f"https://localhost{STABLE_REMOTE_FINALIZATION_HTTP_PATH}",
            server_ca_path=bundle.ca,
            client_certificate_path=bundle.client_cert,
            client_private_key_path=bundle.client_key,
            server_certificate_sha256_pins=(bundle.server_fingerprint,),
        )


def test_endpoint_rejects_plain_http_userinfo_and_noncanonical_path(tmp_path: Path) -> None:
    bundle = _tls_bundle(tmp_path)
    common = {
        "server_ca_path": bundle.ca,
        "client_certificate_path": bundle.client_cert,
        "client_private_key_path": bundle.client_key,
        "server_certificate_sha256_pins": (bundle.server_fingerprint,),
    }

    for endpoint in (
        f"http://localhost{STABLE_REMOTE_FINALIZATION_HTTP_PATH}",
        f"https://user:secret@localhost{STABLE_REMOTE_FINALIZATION_HTTP_PATH}",
        "https://localhost/v1/other",
    ):
        with pytest.raises(ValueError, match="固定路径的 https URL"):
            StableRemoteFinalizationHTTPClientPolicy(endpoint_url=endpoint, **common)


def test_server_stop_is_idempotent_before_and_after_lifecycle(tmp_path: Path) -> None:
    bundle = _tls_bundle(tmp_path)

    class NeverCalledTransport:
        async def receive(self, _package):
            raise AssertionError("transport must not be called")

    server = _server(bundle=bundle, transport=NeverCalledTransport())
    assert not server.stop()
    assert server.start()
    assert not server.start()
    with socket.create_connection(("127.0.0.1", server.bound_port), timeout=1):
        pass
    assert server.stop()
    assert not server.stop()


def test_runtime_composition_builds_enabled_mtls_transport(tmp_path: Path) -> None:
    bundle = _tls_bundle(tmp_path / "tls")
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
        harness=HarnessConfig(
            stable_remote_finalization_delivery=(
                StableRemoteFinalizationDeliveryWorkerConfig(ack_timeout_seconds=20)
            ),
            stable_remote_finalization_http_transport=(
                StableRemoteFinalizationHTTPTransportConfig(
                    enabled=True,
                    endpoint_url=(
                        f"https://localhost{STABLE_REMOTE_FINALIZATION_HTTP_PATH}"
                    ),
                    server_ca_path=str(bundle.ca),
                    client_certificate_path=str(bundle.client_cert),
                    client_private_key_path=str(bundle.client_key),
                    server_certificate_sha256_pins=[bundle.server_fingerprint],
                    request_timeout_seconds=15,
                )
            ),
        ),
    )
    paths = build_runtime_paths(config)
    services = build_runtime_services(
        config,
        paths=paths,
        resources=build_runtime_resources(paths),
    )

    assert isinstance(
        services.stable_remote_finalization_transport,
        MTLSStableRemoteFinalizationInstallationTransport,
    )


def test_http_transport_config_fails_closed_when_partial_or_timeout_is_unsafe() -> None:
    with pytest.raises(ValidationError, match="未显式 enabled"):
        StableRemoteFinalizationHTTPTransportConfig(endpoint_url="https://localhost")

    with pytest.raises(ValidationError, match="必须完整配置"):
        StableRemoteFinalizationHTTPTransportConfig(enabled=True)

    with pytest.raises(ValidationError, match="必须小于 Worker ACK timeout"):
        HarnessConfig(
            stable_remote_finalization_delivery=(
                StableRemoteFinalizationDeliveryWorkerConfig(ack_timeout_seconds=10)
            ),
            stable_remote_finalization_http_transport=(
                StableRemoteFinalizationHTTPTransportConfig(
                    enabled=True,
                    endpoint_url=(
                        f"https://localhost{STABLE_REMOTE_FINALIZATION_HTTP_PATH}"
                    ),
                    server_ca_path="ca",
                    client_certificate_path="cert",
                    client_private_key_path="key",
                    server_certificate_sha256_pins=["0" * 64],
                    connect_timeout_seconds=5,
                    request_timeout_seconds=10,
                )
            ),
        )
