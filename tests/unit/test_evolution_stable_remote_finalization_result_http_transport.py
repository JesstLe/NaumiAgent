from __future__ import annotations

import asyncio
import os
import socket
import ssl
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.config.settings import (
    AppConfig,
    HarnessConfig,
    MemoryConfig,
    StableRemoteFinalizationResultHTTPTransportConfig,
    StableRemoteFinalizationResultReturnWorkerConfig,
)
from naumi_agent.evolution.stable_remote_finalization_result_http_transport import (
    STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE,
    STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH,
    STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE,
    MTLSStableRemoteFinalizationResultTransport,
    StableRemoteFinalizationResultHTTPClientPolicy,
    StableRemoteFinalizationResultHTTPServer,
    StableRemoteFinalizationResultHTTPServerPolicy,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    EvolutionStableRemoteFinalizationResultTransportError,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    encode_stable_remote_finalization_submission,
)
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_resources,
    build_runtime_services,
)
from tests.unit.test_evolution_stable_remote_finalization_http_transport import (
    _tls_bundle,
    _TLSBundle,
)
from tests.unit.test_evolution_stable_remote_finalization_result_return_worker import (
    _ready,
    _worker,
)


def _server(
    *,
    bundle: _TLSBundle,
    service,
    member_id: str,
    fingerprints: tuple[str, ...] | None = None,
    **policy,
) -> StableRemoteFinalizationResultHTTPServer:
    return StableRemoteFinalizationResultHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StableRemoteFinalizationResultHTTPServerPolicy(
            server_certificate_path=bundle.server_cert,
            server_private_key_path=bundle.server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                member_id: fingerprints or (bundle.client_fingerprint,)
            },
            **policy,
        ),
        service=service,
    )


def _client(
    *,
    bundle: _TLSBundle,
    port: int,
    next_client: bool = False,
    pins: tuple[str, ...] | None = None,
    request_timeout_seconds: float = 2,
    max_response_bytes: int = 512 * 1024,
) -> MTLSStableRemoteFinalizationResultTransport:
    return MTLSStableRemoteFinalizationResultTransport(
        StableRemoteFinalizationResultHTTPClientPolicy(
            endpoint_url=(
                f"https://127.0.0.1:{port}"
                f"{STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH}"
            ),
            server_ca_path=bundle.ca,
            client_certificate_path=(
                bundle.next_client_cert if next_client else bundle.client_cert
            ),
            client_private_key_path=(
                bundle.next_client_key if next_client else bundle.client_key
            ),
            server_certificate_sha256_pins=pins or (bundle.server_fingerprint,),
            connect_timeout_seconds=1,
            request_timeout_seconds=request_timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
    )


async def _captured_submission(tmp_path: Path):
    fixture, delivery_service, delivery_store, delivery, credential, journal = await _ready(
        tmp_path
    )
    captured = {}

    class CaptureTransport:
        async def submit(self, **kwargs):
            captured.update(kwargs)
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "capture_only", "capture"
            )

    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
        transport=CaptureTransport(),
    )
    result = await worker.run_once()
    assert result.executed == result.retry_scheduled == 1
    return (
        fixture,
        delivery_service,
        delivery_store,
        delivery,
        credential,
        journal,
        captured["submission"],
    )


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_mtls_closes_real_worker_loop_and_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture, service, store, delivery, credential, journal = await _ready(tmp_path)
    bundle = _tls_bundle(tmp_path / "tls")
    member_id = delivery.package.installation_member_id
    server = _server(
        bundle=bundle,
        service=service,
        member_id=member_id,
        fingerprints=(bundle.client_fingerprint, bundle.next_client_fingerprint),
    )
    server.start()
    try:
        transport = _client(
            bundle=bundle,
            port=server.bound_port,
            request_timeout_seconds=5,
        )
        worker = _worker(
            fixture=fixture,
            delivery_service=service,
            credential=credential,
            journal=journal,
            transport=transport,
            claim_lease_seconds=10,
            result_timeout_seconds=8,
        )

        result = await worker.run_once()

        assert result.executed == result.returned == 1
        outbox = await worker.store.get(delivery.package.delivery_id)
        assert outbox is not None and outbox.submission is not None
        assert outbox.receipt is not None
        repeated = await transport.submit(
            package=delivery.package,
            submission=outbox.submission,
            late_recovery=False,
        )
        assert repeated == outbox.receipt
        assert (await store.get(delivery.package.delivery_id)).receipt == repeated
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_mtls_binds_client_leaf_to_delivery_member_and_rotates(
    tmp_path: Path,
) -> None:
    (
        _,
        service,
        _,
        delivery,
        _,
        _,
        submission,
    ) = await _captured_submission(tmp_path)
    bundle = _tls_bundle(tmp_path / "tls")
    real_member = delivery.package.installation_member_id
    other_member = "relpopmember_" + "f" * 24
    server = StableRemoteFinalizationResultHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StableRemoteFinalizationResultHTTPServerPolicy(
            server_certificate_path=bundle.server_cert,
            server_private_key_path=bundle.server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                real_member: (bundle.next_client_fingerprint,),
                other_member: (bundle.client_fingerprint,),
            },
        ),
        service=service,
    )
    server.start()
    try:
        wrong_member_client = _client(bundle=bundle, port=server.bound_port)
        with pytest.raises(EvolutionStableRemoteFinalizationResultTransportError) as denied:
            await wrong_member_client.submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        assert denied.value.code == (
            "stable_remote_result_http_member_identity_forbidden"
        )
        assert not denied.value.retryable

        rotated_client = _client(
            bundle=bundle,
            port=server.bound_port,
            next_client=True,
        )
        receipt = await rotated_client.submit(
            package=delivery.package,
            submission=submission,
            late_recovery=False,
        )
        assert receipt.submission == submission
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_server_derives_late_recovery_from_control_plane_clock(
    tmp_path: Path,
) -> None:
    fixture, service, _, delivery, _, _, submission = await _captured_submission(tmp_path)
    expiry = datetime.fromisoformat(
        delivery.package.execution_package.grant.expires_at
    )
    fixture.data.clock.value = expiry + timedelta(seconds=1)
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        service=service,
        member_id=delivery.package.installation_member_id,
    )
    server.start()
    try:
        receipt = await _client(bundle=bundle, port=server.bound_port).submit(
            package=delivery.package,
            submission=submission,
            late_recovery=False,
        )
        assert receipt.submission == submission
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_client_pins_before_sending_and_server_rejects_ambiguous_length(
    tmp_path: Path,
) -> None:
    _, service, _, delivery, _, _, submission = await _captured_submission(tmp_path)
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        service=service,
        member_id=delivery.package.installation_member_id,
    )
    server.start()
    try:
        bad_pin = _client(
            bundle=bundle,
            port=server.bound_port,
            pins=(bundle.next_server_fingerprint,),
        )
        with pytest.raises(EvolutionStableRemoteFinalizationResultTransportError) as pinned:
            await bad_pin.submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        assert pinned.value.code == "stable_remote_result_http_server_pin_mismatch"

        body = encode_stable_remote_finalization_submission(submission).encode("ascii")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(bundle.ca))
        context.load_cert_chain(str(bundle.client_cert), str(bundle.client_key))

        def raw_request() -> bytes:
            with socket.create_connection(("127.0.0.1", server.bound_port), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname="127.0.0.1") as connection:
                    connection.sendall(
                        (
                            f"POST {STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH} HTTP/1.1\r\n"
                            "Host: 127.0.0.1\r\n"
                            f"Content-Type: {STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE}\r\n"
                            f"Accept: {STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE}\r\n"
                            f"X-Naumi-Delivery-ID: {delivery.package.delivery_id}\r\n"
                            "X-Naumi-Late-Recovery: false\r\n"
                            f"Content-Length: {len(body)}\r\n"
                            f"Content-Length: {len(body)}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                        + body
                    )
                    chunks = []
                    while chunk := connection.recv(4096):
                        chunks.append(chunk)
                    return b"".join(chunks)

        response = await asyncio.to_thread(raw_request)
        assert response.startswith(b"HTTP/1.1 400")
        assert b"content_length_ambiguous" in response
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_server_enforces_one_concurrent_request_slot(tmp_path: Path) -> None:
    _, service, _, delivery, _, _, submission = await _captured_submission(tmp_path)
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        service=service,
        member_id=delivery.package.installation_member_id,
        max_concurrent_requests=1,
    )
    entered = threading.Event()
    release = threading.Event()
    original = server._delivery

    def blocked(delivery_id: str):
        entered.set()
        assert release.wait(timeout=2)
        return original(delivery_id)

    server._delivery = blocked
    server.start()
    try:
        first = asyncio.create_task(
            _client(bundle=bundle, port=server.bound_port).submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(EvolutionStableRemoteFinalizationResultTransportError) as busy:
            await _client(bundle=bundle, port=server.bound_port).submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        assert busy.value.retryable
        release.set()
        assert (await first).submission == submission
    finally:
        release.set()
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_mtls_rejects_unregistered_or_missing_client_and_bounds_receipt(
    tmp_path: Path,
) -> None:
    _, service, _, delivery, _, _, submission = await _captured_submission(tmp_path)
    bundle = _tls_bundle(tmp_path / "tls")
    member_id = delivery.package.installation_member_id
    server = _server(
        bundle=bundle,
        service=service,
        member_id=member_id,
        fingerprints=(bundle.next_client_fingerprint,),
    )
    server.start()
    try:
        unregistered = _client(bundle=bundle, port=server.bound_port)
        with pytest.raises(EvolutionStableRemoteFinalizationResultTransportError) as denied:
            await unregistered.submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        assert denied.value.code == "stable_remote_result_http_client_identity_forbidden"
        assert not denied.value.retryable

        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(bundle.ca))

        def without_client_certificate() -> bytes:
            try:
                with socket.create_connection(
                    ("127.0.0.1", server.bound_port), timeout=2
                ) as raw:
                    with context.wrap_socket(
                        raw, server_hostname="127.0.0.1"
                    ) as connection:
                        connection.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                        return connection.recv(128)
            except (ConnectionError, OSError, ssl.SSLError):
                return b""

        assert await asyncio.to_thread(without_client_certificate) == b""
    finally:
        server.stop()

    rotated = _server(
        bundle=bundle,
        service=service,
        member_id=member_id,
        fingerprints=(bundle.next_client_fingerprint,),
    )
    rotated.start()
    try:
        bounded = _client(
            bundle=bundle,
            port=rotated.bound_port,
            next_client=True,
            max_response_bytes=64,
        )
        with pytest.raises(EvolutionStableRemoteFinalizationResultTransportError) as oversized:
            await bounded.submit(
                package=delivery.package,
                submission=submission,
                late_recovery=False,
            )
        assert oversized.value.code == "stable_remote_result_http_response_oversized"
        assert not oversized.value.retryable
    finally:
        rotated.stop()


def test_result_http_config_composes_transport_and_fails_closed(tmp_path: Path) -> None:
    bundle = _tls_bundle(tmp_path / "tls")
    transport = StableRemoteFinalizationResultHTTPTransportConfig(
        enabled=True,
        endpoint_url=f"https://localhost{STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH}",
        server_ca_path=str(bundle.ca),
        client_certificate_path=str(bundle.client_cert),
        client_private_key_path=str(bundle.client_key),
        server_certificate_sha256_pins=[bundle.server_fingerprint],
        request_timeout_seconds=10,
    )
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        harness=HarnessConfig(
            stable_remote_finalization_result_return=(
                StableRemoteFinalizationResultReturnWorkerConfig(
                    result_timeout_seconds=20
                )
            ),
            stable_remote_finalization_result_http_transport=transport,
        ),
    )
    paths = build_runtime_paths(config)
    services = build_runtime_services(
        config,
        paths=paths,
        resources=build_runtime_resources(paths),
    )
    assert isinstance(
        services.stable_remote_finalization_result_transport,
        MTLSStableRemoteFinalizationResultTransport,
    )

    with pytest.raises(ValidationError, match="未显式 enabled"):
        StableRemoteFinalizationResultHTTPTransportConfig(
            endpoint_url="https://localhost"
        )
    with pytest.raises(ValidationError, match="必须小于 Result Worker timeout"):
        HarnessConfig(
            stable_remote_finalization_result_return=(
                StableRemoteFinalizationResultReturnWorkerConfig(
                    result_timeout_seconds=10
                )
            ),
            stable_remote_finalization_result_http_transport=transport,
        )
