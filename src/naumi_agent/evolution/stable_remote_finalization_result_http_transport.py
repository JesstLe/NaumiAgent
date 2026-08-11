"""Authenticated installation-to-control-plane finalization Result transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import MappingProxyType, TracebackType
from urllib.parse import SplitResult, urlsplit

from naumi_agent.evolution._stable_remote_finalization_http_common import (
    BoundedTLSHTTPServer,
    FingerprintRateLimiter,
    validate_fingerprints,
    validate_seconds,
    validate_tls_file,
)
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationDeliveryPackage,
    EvolutionStableRemoteFinalizationDeliveryService,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    EvolutionStableRemoteFinalizationResultTransportError,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationReceipt,
    EvolutionStableRemoteFinalizationSubmission,
    decode_stable_remote_finalization_receipt,
    decode_stable_remote_finalization_submission,
    encode_stable_remote_finalization_receipt,
    encode_stable_remote_finalization_submission,
)

STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH = (
    "/v1/stable-finalization/results:ingest"
)
STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE = (
    "application/vnd.naumi.stable-finalization-submission.v1+base64"
)
STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.naumi.stable-finalization-receipt.v1+base64"
)

_DELIVERY_ID_RE = re.compile(r"^evstableremotedelivery_[0-9a-f]{24}$")
_MEMBER_ID_RE = re.compile(r"^relpopmember_[0-9a-f]{24}$")
_MAX_SUBMISSION_BYTES = 512 * 1024
_MAX_RECEIPT_BYTES = 512 * 1024
_MAX_HEADER_LINE_BYTES = 8 * 1024
_MAX_HEADER_BYTES = 32 * 1024
_MAX_HEADERS = 64


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationResultHTTPClientPolicy:
    """Fail-closed outbound Result-return TLS and HTTP limits."""

    endpoint_url: str
    server_ca_path: Path
    client_certificate_path: Path
    client_private_key_path: Path
    server_certificate_sha256_pins: tuple[str, ...]
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 15.0
    max_response_bytes: int = _MAX_RECEIPT_BYTES

    def __post_init__(self) -> None:
        endpoint = _endpoint(self.endpoint_url)
        object.__setattr__(self, "endpoint_url", endpoint.geturl())
        object.__setattr__(
            self,
            "server_ca_path",
            validate_tls_file(
                self.server_ca_path, label="控制平面 CA", private=False
            ),
        )
        object.__setattr__(
            self,
            "client_certificate_path",
            validate_tls_file(
                self.client_certificate_path, label="安装端证书", private=False
            ),
        )
        object.__setattr__(
            self,
            "client_private_key_path",
            validate_tls_file(
                self.client_private_key_path, label="安装端私钥", private=True
            ),
        )
        object.__setattr__(
            self,
            "server_certificate_sha256_pins",
            validate_fingerprints(
                self.server_certificate_sha256_pins,
                label="控制平面证书 pin",
            ),
        )
        validate_seconds(
            self.connect_timeout_seconds, "Result 连接 timeout", maximum=120
        )
        validate_seconds(
            self.request_timeout_seconds, "Result 请求 timeout", maximum=600
        )
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("Result 请求 timeout 不能小于连接 timeout。")
        if not 1 <= self.max_response_bytes <= _MAX_RECEIPT_BYTES:
            raise ValueError("Finalization Receipt 大小上限无效。")


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationResultHTTPServerPolicy:
    """Control-plane mTLS policy with certificate-to-member authorization."""

    server_certificate_path: Path
    server_private_key_path: Path
    client_ca_path: Path
    installation_certificate_sha256_by_member: Mapping[str, tuple[str, ...]]
    max_request_bytes: int = _MAX_SUBMISSION_BYTES
    tls_handshake_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 15.0
    requests_per_minute: int = 120
    max_concurrent_requests: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "server_certificate_path",
            validate_tls_file(
                self.server_certificate_path, label="控制平面证书", private=False
            ),
        )
        object.__setattr__(
            self,
            "server_private_key_path",
            validate_tls_file(
                self.server_private_key_path, label="控制平面私钥", private=True
            ),
        )
        object.__setattr__(
            self,
            "client_ca_path",
            validate_tls_file(self.client_ca_path, label="安装端 CA", private=False),
        )
        raw = self.installation_certificate_sha256_by_member
        if not isinstance(raw, Mapping) or not 1 <= len(raw) <= 10_000:
            raise ValueError("安装成员证书授权表必须包含 1–10000 个成员。")
        normalized: dict[str, tuple[str, ...]] = {}
        for member_id, values in raw.items():
            member = str(member_id or "").strip()
            if _MEMBER_ID_RE.fullmatch(member) is None or member in normalized:
                raise ValueError("安装成员证书授权表包含无效或重复成员。")
            normalized[member] = validate_fingerprints(
                tuple(values), label=f"成员 {member} 的证书 pin"
            )
        object.__setattr__(
            self,
            "installation_certificate_sha256_by_member",
            MappingProxyType(normalized),
        )
        if not 1 <= self.max_request_bytes <= _MAX_SUBMISSION_BYTES:
            raise ValueError("Finalization Submission 大小上限无效。")
        validate_seconds(
            self.tls_handshake_timeout_seconds,
            "Result TLS handshake timeout",
            maximum=120,
        )
        validate_seconds(
            self.request_timeout_seconds,
            "Result 服务端请求 timeout",
            maximum=600,
        )
        if not 1 <= self.requests_per_minute <= 100_000:
            raise ValueError("Result 请求速率上限无效。")
        if not 1 <= self.max_concurrent_requests <= 1024:
            raise ValueError("Result 并发请求上限无效。")

    @property
    def authorized_fingerprints(self) -> frozenset[str]:
        return frozenset(
            fingerprint
            for values in self.installation_certificate_sha256_by_member.values()
            for fingerprint in values
        )


class MTLSStableRemoteFinalizationResultTransport:
    """Installation-side Result client with same-connection server pinning."""

    def __init__(self, policy: StableRemoteFinalizationResultHTTPClientPolicy) -> None:
        self.policy = policy
        self._endpoint = _endpoint(policy.endpoint_url)
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=str(policy.server_ca_path),
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(
            certfile=str(policy.client_certificate_path),
            keyfile=str(policy.client_private_key_path),
        )
        self._ssl_context = context

    async def submit(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        submission: EvolutionStableRemoteFinalizationSubmission,
        late_recovery: bool,
    ) -> EvolutionStableRemoteFinalizationReceipt:
        if not _submission_matches(package, submission):
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_submission_mismatch",
                "Result 与 Delivery Package 不一致。",
                retryable=False,
            )
        body = encode_stable_remote_finalization_submission(submission).encode("ascii")
        if len(body) > _MAX_SUBMISSION_BYTES:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_submission_oversized",
                "Finalization Submission 超过网络传输上限。",
                retryable=False,
            )
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self._endpoint.hostname,
                        self._endpoint.port or 443,
                        ssl=self._ssl_context,
                        server_hostname=self._endpoint.hostname,
                        ssl_handshake_timeout=self.policy.connect_timeout_seconds,
                    ),
                    timeout=self.policy.connect_timeout_seconds,
                )
                self._verify_peer_pin(writer)
                writer.write(
                    self._request(
                        package.delivery_id,
                        body,
                        late_recovery=bool(late_recovery),
                    )
                )
                await writer.drain()
                status, headers, response = await _read_response(
                    reader, max_body_bytes=self.policy.max_response_bytes
                )
                if status != 200:
                    raise _status_error(status, response)
                if headers.get("content-type", "").split(";", 1)[0].strip() != (
                    STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE
                ):
                    raise EvolutionStableRemoteFinalizationResultTransportError(
                        "stable_remote_result_http_receipt_content_type_invalid",
                        "控制平面返回了不受支持的 Receipt media type。",
                        retryable=False,
                    )
                try:
                    receipt = decode_stable_remote_finalization_receipt(
                        response.decode("ascii")
                    )
                except (
                    UnicodeDecodeError,
                    ValueError,
                    EvolutionStableRemoteFinalizationError,
                ) as exc:
                    raise EvolutionStableRemoteFinalizationResultTransportError(
                        "stable_remote_result_http_receipt_invalid",
                        "控制平面返回的 Finalization Receipt 无效。",
                        retryable=False,
                    ) from exc
                if not _receipt_matches(package, submission, receipt):
                    raise EvolutionStableRemoteFinalizationResultTransportError(
                        "stable_remote_result_http_receipt_mismatch",
                        "控制平面 Receipt 与本次 Result 不匹配。",
                        retryable=False,
                    )
                return receipt
        except EvolutionStableRemoteFinalizationResultTransportError:
            raise
        except TimeoutError as exc:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_timeout",
                "控制平面 Result 请求超时。",
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_tls_identity_invalid",
                "控制平面 TLS identity 验证失败。",
                retryable=False,
            ) from exc
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_unavailable",
                "控制平面 Result 端点暂时不可用。",
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)
                except (ConnectionError, OSError, TimeoutError, ssl.SSLError):
                    pass

    def _verify_peer_pin(self, writer: asyncio.StreamWriter) -> None:
        ssl_object = writer.get_extra_info("ssl_object")
        certificate = None if ssl_object is None else ssl_object.getpeercert(binary_form=True)
        fingerprint = "" if not certificate else hashlib.sha256(certificate).hexdigest()
        if fingerprint not in self.policy.server_certificate_sha256_pins:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_http_server_pin_mismatch",
                "控制平面证书不在 current/next pin 集合中。",
                retryable=False,
            )

    def _request(self, delivery_id: str, body: bytes, *, late_recovery: bool) -> bytes:
        headers = (
            f"POST {STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH} HTTP/1.1\r\n"
            f"Host: {self._endpoint.netloc}\r\n"
            f"Content-Type: {STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE}\r\n"
            f"Accept: {STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE}\r\n"
            f"X-Naumi-Delivery-ID: {delivery_id}\r\n"
            f"X-Naumi-Late-Recovery: {'true' if late_recovery else 'false'}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        return headers + body


class StableRemoteFinalizationResultHTTPServer:
    """Dedicated Control Plane mTLS Result endpoint."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        policy: StableRemoteFinalizationResultHTTPServerPolicy,
        service: EvolutionStableRemoteFinalizationDeliveryService,
    ) -> None:
        host = str(bind_host or "").strip()
        if not host or len(host) > 255:
            raise ValueError("Result 服务 bind host 无效。")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Result 服务 port 无效。")
        if not isinstance(service, EvolutionStableRemoteFinalizationDeliveryService):
            raise TypeError("Result 服务必须使用 Delivery Service。")
        self.bind_host = host
        self.port = port
        self.policy = policy
        self.service = service
        self._server: BoundedTLSHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._rate_limiter = FingerprintRateLimiter(policy.requests_per_minute)
        self._lifecycle_lock = threading.Lock()

    @property
    def bound_port(self) -> int:
        return 0 if self._server is None else int(self._server.server_address[1])

    @property
    def active_requests(self) -> int:
        return 0 if self._server is None else self._server.active_requests

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            owner = self

            class Handler(_StableRemoteFinalizationResultRequestHandler):
                endpoint = owner

            server = BoundedTLSHTTPServer(
                (self.bind_host, self.port),
                Handler,
                max_concurrent_requests=self.policy.max_concurrent_requests,
            )
            server.daemon_threads = True
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.verify_mode = ssl.CERT_REQUIRED
                context.load_verify_locations(cafile=str(self.policy.client_ca_path))
                context.load_cert_chain(
                    certfile=str(self.policy.server_certificate_path),
                    keyfile=str(self.policy.server_private_key_path),
                )
                server.configure_tls(
                    context,
                    handshake_timeout_seconds=self.policy.tls_handshake_timeout_seconds,
                )
            except Exception:
                server.server_close()
                raise
            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="naumi-stable-finalization-result-mtls",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lifecycle_lock:
            server, thread = self._server, self._thread
            if server is None or thread is None:
                return False
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self._server = None
            self._thread = None
            if thread.is_alive():
                raise RuntimeError("Result mTLS 服务未在期限内停止。")
            return True

    def __enter__(self) -> StableRemoteFinalizationResultHTTPServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def _delivery(self, delivery_id: str):
        async def load():
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                return await self.service.store.get(delivery_id)

        return asyncio.run(load())

    def _ingest(
        self,
        delivery_id: str,
        submission: EvolutionStableRemoteFinalizationSubmission,
        *,
        late_recovery: bool,
    ):
        async def ingest():
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                return await self.service.ingest_result(
                    delivery_id=delivery_id,
                    submission_base64=encode_stable_remote_finalization_submission(submission),
                    late_recovery=late_recovery,
                )

        return asyncio.run(ingest())


class _StableRemoteFinalizationResultRequestHandler(BaseHTTPRequestHandler):
    endpoint: StableRemoteFinalizationResultHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "NaumiFinalizationControlPlane/1"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        self.connection.settimeout(self.endpoint.policy.request_timeout_seconds)
        if self.request_version != "HTTP/1.1":
            self._error(505, "http_version_not_supported")
            return
        identity = self._authorize()
        if identity is None:
            return
        if not self._header_envelope_valid():
            return
        if self.path != STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH:
            self._error(404, "endpoint_not_found")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(400, "transfer_encoding_forbidden")
            return
        host = self._single_header("Host")
        if host is None:
            return
        if not host.isascii() or not 1 <= len(host) <= 255 or any(
            character.isspace() or ord(character) < 0x20 for character in host
        ):
            self._error(400, "host_invalid")
            return
        content_type = self._single_header("Content-Type")
        if content_type is None:
            return
        if content_type.split(";", 1)[0].strip() != (
            STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE
        ):
            self._error(415, "content_type_invalid")
            return
        accept = self._single_header("Accept")
        if accept is None:
            return
        if accept.strip() != STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE:
            self._error(406, "accept_invalid")
            return
        delivery_id = self._single_header("X-Naumi-Delivery-ID")
        if delivery_id is None:
            return
        if _DELIVERY_ID_RE.fullmatch(delivery_id) is None:
            self._error(400, "delivery_id_invalid")
            return
        requested_late = self._single_header("X-Naumi-Late-Recovery")
        if requested_late not in {"true", "false"}:
            self._error(400, "late_recovery_invalid")
            return
        length = self._content_length()
        if length is None:
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._error(408, "request_body_timeout")
            return
        if len(body) != length:
            self._error(400, "request_body_truncated")
            return
        try:
            submission = decode_stable_remote_finalization_submission(body.decode("ascii"))
        except (UnicodeDecodeError, ValueError, EvolutionStableRemoteFinalizationError):
            self._error(400, "submission_invalid")
            return
        try:
            delivery = self.endpoint._delivery(delivery_id)
        except TimeoutError:
            self._error(503, "delivery_lookup_timeout")
            return
        except Exception:
            self._error(500, "delivery_lookup_failed")
            return
        if delivery is None:
            self._error(404, "delivery_not_found")
            return
        member_id = delivery.package.installation_member_id
        allowed = self.endpoint.policy.installation_certificate_sha256_by_member.get(
            member_id, ()
        )
        if identity not in allowed:
            self._error(403, "member_identity_forbidden")
            return
        if not _submission_matches(delivery.package, submission):
            self._error(409, "submission_delivery_mismatch")
            return
        now = _aware(self.endpoint.service.clock())
        expiry = _aware(delivery.package.execution_package.grant.expires_at)
        authoritative_late_recovery = now >= expiry
        try:
            completed = self.endpoint._ingest(
                delivery_id,
                submission,
                late_recovery=authoritative_late_recovery,
            )
        except TimeoutError:
            self._error(503, "result_ingest_timeout")
            return
        except EvolutionStableRemoteFinalizationDeliveryError as exc:
            self._error(422, exc.code)
            return
        except EvolutionStableRemoteFinalizationError as exc:
            self._error(422, exc.code)
            return
        except Exception:
            self._error(500, "result_ingest_failed")
            return
        receipt = completed.receipt
        if receipt is None:
            self._error(500, "receipt_missing")
            return
        encoded = encode_stable_remote_finalization_receipt(receipt).encode("ascii")
        self.send_response(200)
        self._security_headers(STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE, len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._reject_method()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._reject_method()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_method()

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_method()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_method()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method()

    def do_TRACE(self) -> None:  # noqa: N802
        self._reject_method()

    def _reject_method(self) -> None:
        self.close_connection = True
        if self._authorize() is not None:
            self._error(405, "method_not_allowed")

    def handle_expect_100(self) -> bool:
        self.close_connection = True
        if self._authorize() is not None:
            self._error(417, "expectation_not_supported")
        return False

    def log_message(self, _format: str, *args: object) -> None:
        """Never log request data or certificate identities."""

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self.close_connection = True
        self._error(code, f"http_parse_error_{code}")

    def _authorize(self) -> str | None:
        certificate = self.connection.getpeercert(binary_form=True)
        identity = "" if not certificate else hashlib.sha256(certificate).hexdigest()
        if identity not in self.endpoint.policy.authorized_fingerprints:
            self._error(403, "client_identity_forbidden")
            return None
        if not self.endpoint._rate_limiter.allow(identity):
            self._error(429, "rate_limit_exceeded")
            return None
        return identity

    def _header_envelope_valid(self) -> bool:
        pairs = list(self.headers.raw_items())
        total = 0
        if len(pairs) > _MAX_HEADERS:
            self._error(431, "headers_count_exceeded")
            return False
        for name, value in pairs:
            size = len(name.encode("ascii", errors="ignore")) + len(
                value.encode("ascii", errors="ignore")
            ) + 4
            if size > _MAX_HEADER_LINE_BYTES:
                self._error(431, "header_line_oversized")
                return False
            total += size
        if total > _MAX_HEADER_BYTES:
            self._error(431, "headers_oversized")
            return False
        return True

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            self._error(400, f"{name.lower().replace('-', '_')}_ambiguous")
            return None
        return values[0]

    def _content_length(self) -> int | None:
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1:
            self._error(400, "content_length_ambiguous")
            return None
        raw = values[0]
        if raw is None or not raw.isascii() or not raw.isdigit():
            self._error(411, "content_length_required")
            return None
        length = int(raw)
        if not 1 <= length <= self.endpoint.policy.max_request_bytes:
            self._error(413, "submission_oversized")
            return None
        return length

    def _error(self, status: int, code: str) -> None:
        safe_code = code if re.fullmatch(r"[a-z0-9_]{1,128}", code) else "request_rejected"
        body = json.dumps(
            {"error": {"code": safe_code}}, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        self.send_response(status)
        self._security_headers("application/json", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")


async def _read_response(
    reader: asyncio.StreamReader,
    *,
    max_body_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    status_line = await _readline(reader, "status line")
    try:
        parts = status_line.decode("ascii").rstrip("\r\n").split(" ", 2)
    except UnicodeDecodeError as exc:
        raise _invalid_response("控制平面 HTTP status line 无效。") from exc
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or not parts[1].isdigit():
        raise _invalid_response("控制平面 HTTP status line 无效。")
    status = int(parts[1])
    headers: dict[str, str] = {}
    total = len(status_line)
    for _ in range(_MAX_HEADERS):
        line = await _readline(reader, "header")
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise _invalid_response("控制平面 HTTP headers 超过上限。")
        if line == b"\r\n":
            break
        try:
            name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_response("控制平面 HTTP header 无效。") from exc
        key = name.strip().lower()
        if not key or key in headers:
            raise _invalid_response("控制平面 HTTP header 重复或缺少名称。")
        headers[key] = value.strip()
    else:
        raise _invalid_response("控制平面 HTTP header 数量超过上限。")
    if "transfer-encoding" in headers:
        raise _invalid_response("控制平面不得使用 Transfer-Encoding。")
    raw_length = headers.get("content-length", "")
    if not raw_length.isascii() or not raw_length.isdigit():
        raise _invalid_response("控制平面缺少有效 Content-Length。")
    length = int(raw_length)
    if not 0 <= length <= max_body_bytes:
        raise EvolutionStableRemoteFinalizationResultTransportError(
            "stable_remote_result_http_response_oversized",
            "控制平面响应超过大小上限。",
            retryable=False,
        )
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise EvolutionStableRemoteFinalizationResultTransportError(
            "stable_remote_result_http_response_truncated",
            "控制平面响应提前结束。",
        ) from exc
    return status, headers, body


async def _readline(reader: asyncio.StreamReader, label: str) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError as exc:
        raise EvolutionStableRemoteFinalizationResultTransportError(
            "stable_remote_result_http_response_truncated",
            f"控制平面 HTTP {label} 提前结束。",
        ) from exc
    except asyncio.LimitOverrunError as exc:
        raise _invalid_response(f"控制平面 HTTP {label} 无效。") from exc
    if len(line) > _MAX_HEADER_LINE_BYTES:
        raise _invalid_response(f"控制平面 HTTP {label} 超过上限。")
    return line


def _invalid_response(message: str) -> EvolutionStableRemoteFinalizationResultTransportError:
    return EvolutionStableRemoteFinalizationResultTransportError(
        "stable_remote_result_http_response_invalid", message, retryable=False
    )


def _status_error(
    status: int,
    body: bytes,
) -> EvolutionStableRemoteFinalizationResultTransportError:
    retryable = status in {408, 425, 429} or 500 <= status <= 599
    remote_code = ""
    try:
        payload = json.loads(body.decode("ascii"))
        candidate = payload.get("error", {}).get("code", "")
        if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,80}", candidate):
            remote_code = candidate
    except (UnicodeDecodeError, ValueError, AttributeError):
        pass
    code = (
        f"stable_remote_result_http_{remote_code}"
        if remote_code
        else f"stable_remote_result_http_status_{status}"
    )
    return EvolutionStableRemoteFinalizationResultTransportError(
        code,
        f"控制平面拒绝 Result 请求（HTTP {status}，{remote_code or 'unknown'}）。",
        retryable=retryable,
    )


def _endpoint(value: str) -> SplitResult:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Result endpoint URL 无效。") from exc
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.netloc.isascii()
        and not any(character.isspace() or ord(character) < 0x20 for character in parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH
        and (port is None or 1 <= port <= 65535)
    ):
        raise ValueError("Result endpoint 必须是固定路径的 https URL。")
    return parsed


def _submission_matches(package, submission) -> bool:
    result = submission.result
    execution = package.execution_package
    return bool(
        result.grant_id == execution.grant.grant_id
        and result.grant_sha256 == execution.grant.grant_sha256
        and result.authorization_id == execution.grant.authorization_id
        and result.authorization_sha256 == execution.grant.authorization_sha256
        and result.installation_member_id == package.installation_member_id
    )


def _receipt_matches(package, submission, receipt) -> bool:
    return bool(
        receipt.execution_package == package.execution_package
        and receipt.submission == submission
    )


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


__all__ = [
    "MTLSStableRemoteFinalizationResultTransport",
    "STABLE_REMOTE_FINALIZATION_RECEIPT_MEDIA_TYPE",
    "STABLE_REMOTE_FINALIZATION_RESULT_HTTP_PATH",
    "STABLE_REMOTE_FINALIZATION_SUBMISSION_MEDIA_TYPE",
    "StableRemoteFinalizationResultHTTPClientPolicy",
    "StableRemoteFinalizationResultHTTPServer",
    "StableRemoteFinalizationResultHTTPServerPolicy",
]
