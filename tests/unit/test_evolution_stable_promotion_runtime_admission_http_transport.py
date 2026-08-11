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
    StablePromotionRuntimeAdmissionDeliveryWorkerConfig,
    StablePromotionRuntimeAdmissionHTTPTransportConfig,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_deliveries import (
    encode_stable_promotion_runtime_admission_submission,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_delivery_worker import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryWorker,
    EvolutionStablePromotionRuntimeAdmissionDispatchStore,
    EvolutionStablePromotionRuntimeAdmissionTransportError,
    EvolutionStablePromotionRuntimeAdmissionWorkerPolicy,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_http_transport import (
    STABLE_PROMOTION_RUNTIME_ADMISSION_HTTP_PATH,
    STABLE_PROMOTION_RUNTIME_ADMISSION_RECEIPT_MEDIA_TYPE,
    STABLE_PROMOTION_RUNTIME_ADMISSION_SUBMISSION_MEDIA_TYPE,
    MTLSStablePromotionRuntimeAdmissionControlPlaneTransport,
    StablePromotionRuntimeAdmissionHTTPClientPolicy,
    StablePromotionRuntimeAdmissionHTTPServer,
    StablePromotionRuntimeAdmissionHTTPServerPolicy,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_resources,
    build_runtime_services,
)
from tests.unit.test_evolution_stable_promotion_runtime_admission_deliveries import (
    _delivery_service,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)
from tests.unit.test_evolution_stable_remote_finalization_http_transport import (
    _tls_bundle,
    _TLSBundle,
)
from tests.unit.test_release_installation_keys import _MemoryBackend


def _server(*, bundle: _TLSBundle, service, member_id: str, pins=None, **policy):
    return StablePromotionRuntimeAdmissionHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StablePromotionRuntimeAdmissionHTTPServerPolicy(
            server_certificate_path=bundle.server_cert,
            server_private_key_path=bundle.server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                member_id: pins or (bundle.client_fingerprint,)
            },
            **policy,
        ),
        service=service,
        clock=service.installation_key_service.clock,
    )


def _client(
    *,
    bundle: _TLSBundle,
    port: int,
    next_client: bool = False,
    pins=None,
    max_response_bytes: int = 128 * 1024,
):
    return MTLSStablePromotionRuntimeAdmissionControlPlaneTransport(
        StablePromotionRuntimeAdmissionHTTPClientPolicy(
            endpoint_url=(
                f"https://127.0.0.1:{port}{STABLE_PROMOTION_RUNTIME_ADMISSION_HTTP_PATH}"
            ),
            server_ca_path=bundle.ca,
            client_certificate_path=(
                bundle.next_client_cert if next_client else bundle.client_cert
            ),
            client_private_key_path=(bundle.next_client_key if next_client else bundle.client_key),
            server_certificate_sha256_pins=pins or (bundle.server_fingerprint,),
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
            max_response_bytes=max_response_bytes,
        )
    )


async def _ready(tmp_path: Path, monkeypatch):
    data = await _fixture(tmp_path, monkeypatch)
    admission_service = data.build_service()
    admitted = await admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    admission = admitted.admission
    private_key = data.context["keys"][admission.installation_member_id]
    now = [datetime.fromisoformat(admission.admitted_at) + timedelta(seconds=1)]
    key_service = ReleaseInstallationKeyService(
        tmp_path / "installation-release",
        backend=_MemoryBackend(),
        key_factory=lambda _size: private_key.private_bytes_raw(),
        clock=lambda: now[0],
    )
    key_service.provision(channel="stable")
    service = _delivery_service(data, admission_service, key_service)
    submission = await service.prepare(admission_id=admission.admission_id)
    return service, submission, now


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_runtime_admission_mtls_closes_worker_and_protocol_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, submission, now = await _ready(tmp_path, monkeypatch)
    admission = submission.payload.admission
    bundle = _tls_bundle(tmp_path / "tls")
    server = _server(
        bundle=bundle,
        service=service,
        member_id=admission.installation_member_id,
        pins=(bundle.client_fingerprint, bundle.next_client_fingerprint),
    )
    server.start()
    try:
        client = _client(bundle=bundle, port=server.bound_port)
        store = EvolutionStablePromotionRuntimeAdmissionDispatchStore(service.store.db_path)
        worker = EvolutionStablePromotionRuntimeAdmissionDeliveryWorker(
            sender=service,
            store=store,
            transport=client,
            policy=EvolutionStablePromotionRuntimeAdmissionWorkerPolicy(
                interval_seconds=1,
                max_empty_backoff_seconds=2,
                max_failure_backoff_seconds=2,
                claim_lease_seconds=10,
                scan_limit=1,
                receipt_timeout_seconds=5,
                retry_base_seconds=1,
                retry_max_seconds=1,
                max_attempts=3,
                shutdown_drain_seconds=2,
                jitter_ratio=0,
            ),
            clock=lambda: now[0],
            random_value=lambda: 0.5,
        )
        await worker.enqueue(admission.admission_id)
        result = await worker.run_once()
        assert result.claimed == result.acknowledged == 1
        assert result.failures == result.retry_scheduled == result.dead_lettered == 0
        dispatch = await store.get(admission.admission_id)
        assert dispatch is not None and dispatch.receipt is not None
        assert dispatch.latest_event.state == "acknowledged"

        repeated = await client.submit(submission)
        assert repeated == dispatch.receipt

        calls = 0
        original_receive = server._receive

        def counted(item):
            nonlocal calls
            calls += 1
            return original_receive(item)

        server._receive = counted
        bad_pin = _client(
            bundle=bundle,
            port=server.bound_port,
            pins=(bundle.next_server_fingerprint,),
        )
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionTransportError) as pin:
            await bad_pin.submit(submission)
        assert pin.value.code == "stable_promotion_admission_http_server_pin_mismatch"
        assert not pin.value.retryable and calls == 0

        bounded = _client(
            bundle=bundle,
            port=server.bound_port,
            max_response_bytes=64,
        )
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionTransportError) as oversized:
            await bounded.submit(submission)
        assert oversized.value.code == ("stable_promotion_admission_http_response_oversized")
        assert not oversized.value.retryable

        body = encode_stable_promotion_runtime_admission_submission(submission).encode("ascii")
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=str(bundle.ca),
        )
        context.load_cert_chain(str(bundle.client_cert), str(bundle.client_key))

        def ambiguous_length() -> bytes:
            with socket.create_connection(("127.0.0.1", server.bound_port), timeout=2) as raw:
                with context.wrap_socket(raw, server_hostname="127.0.0.1") as connection:
                    connection.sendall(
                        (
                            f"POST {STABLE_PROMOTION_RUNTIME_ADMISSION_HTTP_PATH} HTTP/1.1\r\n"
                            "Host: 127.0.0.1\r\n"
                            "Content-Type: "
                            f"{STABLE_PROMOTION_RUNTIME_ADMISSION_SUBMISSION_MEDIA_TYPE}\r\n"
                            f"Accept: {STABLE_PROMOTION_RUNTIME_ADMISSION_RECEIPT_MEDIA_TYPE}\r\n"
                            f"X-Naumi-Admission-ID: {admission.admission_id}\r\n"
                            f"X-Naumi-Submission-ID: {submission.submission_id}\r\n"
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

        response = await asyncio.to_thread(ambiguous_length)
        assert response.startswith(b"HTTP/1.1 400")
        assert b"content_length_ambiguous" in response

        no_client_context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=str(bundle.ca),
        )

        def no_client_certificate() -> bytes:
            try:
                with socket.create_connection(("127.0.0.1", server.bound_port), timeout=2) as raw:
                    with no_client_context.wrap_socket(
                        raw, server_hostname="127.0.0.1"
                    ) as connection:
                        connection.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                        return connection.recv(128)
            except (ConnectionError, OSError, ssl.SSLError):
                return b""

        assert await asyncio.to_thread(no_client_certificate) == b""
    finally:
        server.stop()

    other_member = "relpopmember_" + "f" * 24
    member_server = StablePromotionRuntimeAdmissionHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StablePromotionRuntimeAdmissionHTTPServerPolicy(
            server_certificate_path=bundle.server_cert,
            server_private_key_path=bundle.server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                admission.installation_member_id: (bundle.next_client_fingerprint,),
                other_member: (bundle.client_fingerprint,),
            },
        ),
        service=service,
        clock=lambda: now[0],
    )
    member_server.start()
    try:
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionTransportError) as member:
            await _client(bundle=bundle, port=member_server.bound_port).submit(submission)
        assert member.value.code == ("stable_promotion_admission_http_member_identity_forbidden")
        assert not member.value.retryable
        rotated = await _client(
            bundle=bundle,
            port=member_server.bound_port,
            next_client=True,
        ).submit(submission)
        assert rotated.admission_id == admission.admission_id
    finally:
        member_server.stop()

    next_server = StablePromotionRuntimeAdmissionHTTPServer(
        bind_host="127.0.0.1",
        port=0,
        policy=StablePromotionRuntimeAdmissionHTTPServerPolicy(
            server_certificate_path=bundle.next_server_cert,
            server_private_key_path=bundle.next_server_key,
            client_ca_path=bundle.ca,
            installation_certificate_sha256_by_member={
                admission.installation_member_id: (bundle.client_fingerprint,),
            },
        ),
        service=service,
        clock=lambda: now[0],
    )
    next_server.start()
    try:
        rotated_server_receipt = await _client(
            bundle=bundle,
            port=next_server.bound_port,
            pins=(bundle.server_fingerprint, bundle.next_server_fingerprint),
        ).submit(submission)
        assert rotated_server_receipt.admission_id == admission.admission_id
    finally:
        next_server.stop()

    concurrency_server = _server(
        bundle=bundle,
        service=service,
        member_id=admission.installation_member_id,
        max_concurrent_requests=1,
    )
    entered = threading.Event()
    release = threading.Event()
    concurrent_receive = concurrency_server._receive

    def blocked_receive(item):
        entered.set()
        assert release.wait(timeout=2)
        return concurrent_receive(item)

    concurrency_server._receive = blocked_receive
    concurrency_server.start()
    try:
        first = asyncio.create_task(
            _client(bundle=bundle, port=concurrency_server.bound_port).submit(submission)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionTransportError) as busy:
            await _client(
                bundle=bundle,
                port=concurrency_server.bound_port,
            ).submit(submission)
        assert busy.value.retryable
        release.set()
        assert (await first).admission_id == admission.admission_id
    finally:
        release.set()
        concurrency_server.stop()

    rate_server = _server(
        bundle=bundle,
        service=service,
        member_id=admission.installation_member_id,
        requests_per_minute=1,
    )
    rate_server.start()
    try:
        rate_client = _client(bundle=bundle, port=rate_server.bound_port)
        assert (await rate_client.submit(submission)).admission_id == (admission.admission_id)
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionTransportError) as limited:
            await rate_client.submit(submission)
        assert limited.value.code == ("stable_promotion_admission_http_rate_limit_exceeded")
        assert limited.value.retryable
    finally:
        rate_server.stop()


def test_runtime_admission_http_config_composes_and_fails_closed(
    tmp_path: Path,
) -> None:
    bundle = _tls_bundle(tmp_path / "tls")
    transport = StablePromotionRuntimeAdmissionHTTPTransportConfig(
        enabled=True,
        endpoint_url=("https://localhost" + STABLE_PROMOTION_RUNTIME_ADMISSION_HTTP_PATH),
        server_ca_path=str(bundle.ca),
        client_certificate_path=str(bundle.client_cert),
        client_private_key_path=str(bundle.client_key),
        server_certificate_sha256_pins=[bundle.server_fingerprint],
        connect_timeout_seconds=1,
        request_timeout_seconds=2,
    )
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        harness=HarnessConfig(
            stable_promotion_runtime_admission_delivery=(
                StablePromotionRuntimeAdmissionDeliveryWorkerConfig(receipt_timeout_seconds=5)
            ),
            stable_promotion_runtime_admission_http_transport=transport,
        ),
    )
    paths = build_runtime_paths(config)
    services = build_runtime_services(
        config,
        paths=paths,
        resources=build_runtime_resources(paths),
    )
    assert isinstance(
        services.stable_promotion_runtime_admission_transport,
        MTLSStablePromotionRuntimeAdmissionControlPlaneTransport,
    )

    with pytest.raises(ValidationError, match="未显式 enabled"):
        StablePromotionRuntimeAdmissionHTTPTransportConfig(endpoint_url="https://localhost")
    with pytest.raises(ValidationError, match="必须小于 Worker Receipt timeout"):
        HarnessConfig(
            stable_promotion_runtime_admission_delivery=(
                StablePromotionRuntimeAdmissionDeliveryWorkerConfig(receipt_timeout_seconds=2)
            ),
            stable_promotion_runtime_admission_http_transport=transport,
        )
