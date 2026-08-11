"""Mutually authenticated HTTP transport for stable observation revisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import threading
from collections.abc import Callable, Mapping
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
from naumi_agent.evolution.stable_promotion_observation_revision_deliveries import (
    EvolutionStablePromotionObservationRevisionDeliveryError,
    EvolutionStablePromotionObservationRevisionDeliveryService,
    EvolutionStablePromotionObservationRevisionSubmission,
    decode_stable_promotion_observation_revision_receipt,
    decode_stable_promotion_observation_revision_submission,
    encode_stable_promotion_observation_revision_receipt,
    encode_stable_promotion_observation_revision_submission,
    stable_promotion_observation_revision_receipt_matches_submission,
)
from naumi_agent.evolution.stable_promotion_observation_revision_delivery_worker import (
    EvolutionStablePromotionObservationRevisionTransportError,
)

STABLE_PROMOTION_OBSERVATION_REVISION_HTTP_PATH = (
    "/v1/stable-promotion/observation-revisions:receive"
)
STABLE_PROMOTION_OBSERVATION_REVISION_SUBMISSION_MEDIA_TYPE = (
    "application/vnd.naumi.stable-promotion-observation-revision.v1+base64"
)
STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.naumi.stable-promotion-observation-revision-receipt.v1+base64"
)

_ADMISSION_ID_RE = re.compile(r"^evstablepromadmit_[0-9a-f]{24}$")
_SUBMISSION_ID_RE = re.compile(r"^evstablepromrevsubmit_[0-9a-f]{24}$")
_MEMBER_ID_RE = re.compile(r"^relpopmember_[0-9a-f]{24}$")
_MAX_SUBMISSION_BYTES = 128 * 1024
_MAX_RECEIPT_BYTES = 128 * 1024
_MAX_HEADER_LINE_BYTES = 8 * 1024
_MAX_HEADER_BYTES = 32 * 1024
_MAX_HEADERS = 64


@dataclass(frozen=True, slots=True)
class StablePromotionObservationRevisionHTTPClientPolicy:
    """Fail-closed installation-side mTLS and response bounds."""

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
                self.server_ca_path,
                label="Observation Revision Control Plane CA",
                private=False,
            ),
        )
        object.__setattr__(
            self,
            "client_certificate_path",
            validate_tls_file(
                self.client_certificate_path,
                label="Observation Revision installation 证书",
                private=False,
            ),
        )
        object.__setattr__(
            self,
            "client_private_key_path",
            validate_tls_file(
                self.client_private_key_path,
                label="Observation Revision installation 私钥",
                private=True,
            ),
        )
        object.__setattr__(
            self,
            "server_certificate_sha256_pins",
            validate_fingerprints(
                self.server_certificate_sha256_pins,
                label="Observation Revision Control Plane 证书 pin",
            ),
        )
        validate_seconds(
            self.connect_timeout_seconds,
            "Observation Revision 连接 timeout",
            maximum=120,
        )
        validate_seconds(
            self.request_timeout_seconds,
            "Observation Revision 请求 timeout",
            maximum=600,
        )
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("Observation Revision 请求 timeout 不能小于连接 timeout。")
        if not 1 <= self.max_response_bytes <= _MAX_RECEIPT_BYTES:
            raise ValueError("Observation Revision Receipt 大小上限无效。")


@dataclass(frozen=True, slots=True)
class StablePromotionObservationRevisionHTTPServerPolicy:
    """Control Plane mTLS policy with certificate-to-member authorization."""

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
                self.server_certificate_path,
                label="Observation Revision Control Plane 证书",
                private=False,
            ),
        )
        object.__setattr__(
            self,
            "server_private_key_path",
            validate_tls_file(
                self.server_private_key_path,
                label="Observation Revision Control Plane 私钥",
                private=True,
            ),
        )
        object.__setattr__(
            self,
            "client_ca_path",
            validate_tls_file(
                self.client_ca_path,
                label="Observation Revision installation CA",
                private=False,
            ),
        )
        raw = self.installation_certificate_sha256_by_member
        if not isinstance(raw, Mapping) or not 1 <= len(raw) <= 10_000:
            raise ValueError("Observation Revision 成员证书表必须包含 1–10000 个成员。")
        normalized: dict[str, tuple[str, ...]] = {}
        for member_id, values in raw.items():
            member = str(member_id or "").strip()
            if _MEMBER_ID_RE.fullmatch(member) is None or member in normalized:
                raise ValueError("Observation Revision 成员证书表包含无效或重复成员。")
            normalized[member] = validate_fingerprints(
                tuple(values),
                label=f"Observation Revision 成员 {member} 证书 pin",
            )
        object.__setattr__(
            self,
            "installation_certificate_sha256_by_member",
            MappingProxyType(normalized),
        )
        if not 1 <= self.max_request_bytes <= _MAX_SUBMISSION_BYTES:
            raise ValueError("Observation Revision Submission 大小上限无效。")
        validate_seconds(
            self.tls_handshake_timeout_seconds,
            "Observation Revision TLS handshake timeout",
            maximum=120,
        )
        validate_seconds(
            self.request_timeout_seconds,
            "Observation Revision 服务端请求 timeout",
            maximum=600,
        )
        if not 1 <= self.requests_per_minute <= 100_000:
            raise ValueError("Observation Revision 请求速率上限无效。")
        if not 1 <= self.max_concurrent_requests <= 1024:
            raise ValueError("Observation Revision 并发请求上限无效。")

    @property
    def authorized_fingerprints(self) -> frozenset[str]:
        return frozenset(
            fingerprint
            for values in self.installation_certificate_sha256_by_member.values()
            for fingerprint in values
        )


class MTLSStablePromotionObservationRevisionControlPlaneTransport:
    """Installation-side client with same-connection Control Plane pinning."""

    def __init__(self, policy: StablePromotionObservationRevisionHTTPClientPolicy) -> None:
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

    async def submit(self, submission: EvolutionStablePromotionObservationRevisionSubmission):
        item = EvolutionStablePromotionObservationRevisionSubmission.model_validate(submission)
        body = encode_stable_promotion_observation_revision_submission(item).encode("ascii")
        if len(body) > _MAX_SUBMISSION_BYTES:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_http_submission_oversized",
                "Observation Revision Submission 超过网络传输上限。",
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
                writer.write(self._request(item, body))
                await writer.drain()
                status, headers, response = await _read_response(
                    reader,
                    max_body_bytes=self.policy.max_response_bytes,
                )
                if status != 200:
                    raise _status_error(status, response)
                if headers.get("content-type", "").split(";", 1)[0].strip() != (
                    STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE
                ):
                    raise EvolutionStablePromotionObservationRevisionTransportError(
                        "stable_promotion_observation_revision_http_receipt_content_type_invalid",
                        "Control Plane 返回了不受支持的 Receipt media type。",
                        retryable=False,
                    )
                try:
                    receipt = decode_stable_promotion_observation_revision_receipt(
                        response.decode("ascii")
                    )
                except (
                    UnicodeDecodeError,
                    ValueError,
                    EvolutionStablePromotionObservationRevisionDeliveryError,
                ) as exc:
                    raise EvolutionStablePromotionObservationRevisionTransportError(
                        "stable_promotion_observation_revision_http_receipt_invalid",
                        "Control Plane 返回的 Observation Revision Receipt 无效。",
                        retryable=False,
                    ) from exc
                if not (
                    stable_promotion_observation_revision_receipt_matches_submission(
                        receipt, item
                    )
                ):
                    raise EvolutionStablePromotionObservationRevisionTransportError(
                        "stable_promotion_observation_revision_http_receipt_mismatch",
                        "Control Plane Receipt 与本次 Submission 不匹配。",
                        retryable=False,
                    )
                return receipt
        except EvolutionStablePromotionObservationRevisionTransportError:
            raise
        except TimeoutError as exc:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_http_timeout",
                "Control Plane Observation Revision 请求超时。",
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_http_tls_identity_invalid",
                "Control Plane TLS identity 验证失败。",
                retryable=False,
            ) from exc
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_http_unavailable",
                "Control Plane Observation Revision 端点暂时不可用。",
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
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_http_server_pin_mismatch",
                "Control Plane 证书不在 current/next pin 集合中。",
                retryable=False,
            )

    def _request(
        self,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        body: bytes,
    ) -> bytes:
        admission_id = submission.payload.admission_id
        headers = (
            f"POST {STABLE_PROMOTION_OBSERVATION_REVISION_HTTP_PATH} HTTP/1.1\r\n"
            f"Host: {self._endpoint.netloc}\r\n"
            f"Content-Type: {STABLE_PROMOTION_OBSERVATION_REVISION_SUBMISSION_MEDIA_TYPE}\r\n"
            f"Accept: {STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE}\r\n"
            f"X-Naumi-Admission-ID: {admission_id}\r\n"
            f"X-Naumi-Submission-ID: {submission.submission_id}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        return headers + body


class StablePromotionObservationRevisionHTTPServer:
    """Dedicated Control Plane mTLS endpoint for signed Observation Revisions."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        policy: StablePromotionObservationRevisionHTTPServerPolicy,
        service: EvolutionStablePromotionObservationRevisionDeliveryService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        host = str(bind_host or "").strip()
        if not host or len(host) > 255:
            raise ValueError("Observation Revision 服务 bind host 无效。")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Observation Revision 服务 port 无效。")
        if not isinstance(service, EvolutionStablePromotionObservationRevisionDeliveryService):
            raise TypeError("Observation Revision HTTP 必须使用 7b3b1 Delivery Service。")
        self.bind_host = host
        self.port = port
        self.policy = policy
        self.service = service
        self.clock = clock
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

            class Handler(_StablePromotionObservationRevisionRequestHandler):
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
                    handshake_timeout_seconds=(self.policy.tls_handshake_timeout_seconds),
                )
            except Exception:
                server.server_close()
                raise
            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="naumi-stable-observation-revision-mtls",
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
                raise RuntimeError("Observation Revision mTLS 服务未在期限内停止。")
            return True

    def __enter__(self) -> StablePromotionObservationRevisionHTTPServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def _receive(self, submission: EvolutionStablePromotionObservationRevisionSubmission):
        async def receive():
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                return await self.service.receive(
                    submission=submission,
                    received_at=_aware(self.clock()),
                )

        return asyncio.run(receive())


class _StablePromotionObservationRevisionRequestHandler(BaseHTTPRequestHandler):
    endpoint: StablePromotionObservationRevisionHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "NaumiPromotionControlPlane/1"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        self.connection.settimeout(self.endpoint.policy.request_timeout_seconds)
        if self.request_version != "HTTP/1.1":
            self._error(505, "http_version_not_supported")
            return
        identity = self._authorize()
        if identity is None or not self._header_envelope_valid():
            return
        if self.path != STABLE_PROMOTION_OBSERVATION_REVISION_HTTP_PATH:
            self._error(404, "endpoint_not_found")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(400, "transfer_encoding_forbidden")
            return
        host = self._single_header("Host")
        if host is None:
            return
        if (
            not host.isascii()
            or not 1 <= len(host) <= 255
            or any(character.isspace() or ord(character) < 0x20 for character in host)
        ):
            self._error(400, "host_invalid")
            return
        content_type = self._single_header("Content-Type")
        if content_type is None:
            return
        if content_type.split(";", 1)[0].strip() != (
            STABLE_PROMOTION_OBSERVATION_REVISION_SUBMISSION_MEDIA_TYPE
        ):
            self._error(415, "content_type_invalid")
            return
        accept = self._single_header("Accept")
        if accept is None:
            return
        if accept.strip() != STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE:
            self._error(406, "accept_invalid")
            return
        admission_id = self._single_header("X-Naumi-Admission-ID")
        if admission_id is None:
            return
        if _ADMISSION_ID_RE.fullmatch(admission_id) is None:
            self._error(400, "admission_id_invalid")
            return
        submission_id = self._single_header("X-Naumi-Submission-ID")
        if submission_id is None:
            return
        if _SUBMISSION_ID_RE.fullmatch(submission_id) is None:
            self._error(400, "submission_id_invalid")
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
            submission = decode_stable_promotion_observation_revision_submission(
                body.decode("ascii")
            )
        except (
            UnicodeDecodeError,
            ValueError,
            EvolutionStablePromotionObservationRevisionDeliveryError,
        ):
            self._error(400, "submission_invalid")
            return
        if not (
            submission.payload.admission_id == admission_id
            and submission.submission_id == submission_id
        ):
            self._error(409, "header_artifact_identity_mismatch")
            return
        allowed = self.endpoint.policy.installation_certificate_sha256_by_member.get(
            submission.signature.installation_member_id,
            (),
        )
        if identity not in allowed:
            self._error(403, "member_identity_forbidden")
            return
        try:
            view = self.endpoint._receive(submission)
        except TimeoutError:
            self._error(503, "revision_receive_timeout")
            return
        except EvolutionStablePromotionObservationRevisionDeliveryError as exc:
            self._error(422, exc.code)
            return
        except Exception:
            self._error(500, "revision_receive_failed")
            return
        if not (
            view.remote_revision_delivery_authority
            and stable_promotion_observation_revision_receipt_matches_submission(
                view.receipt, submission
            )
        ):
            self._error(500, "receipt_authority_invalid")
            return
        encoded = encode_stable_promotion_observation_revision_receipt(view.receipt).encode("ascii")
        self.send_response(200)
        self._security_headers(
            STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE,
            len(encoded),
        )
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
            size = (
                len(name.encode("ascii", errors="ignore"))
                + len(value.encode("ascii", errors="ignore"))
                + 4
            )
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
            {"error": {"code": safe_code}},
            separators=(",", ":"),
            sort_keys=True,
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
        raise _invalid_response("Control Plane HTTP status line 无效。") from exc
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or not parts[1].isdigit():
        raise _invalid_response("Control Plane HTTP status line 无效。")
    status = int(parts[1])
    headers: dict[str, str] = {}
    total = len(status_line)
    for _ in range(_MAX_HEADERS):
        line = await _readline(reader, "header")
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise _invalid_response("Control Plane HTTP headers 超过上限。")
        if line == b"\r\n":
            break
        try:
            name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_response("Control Plane HTTP header 无效。") from exc
        key = name.strip().lower()
        if not key or key in headers:
            raise _invalid_response("Control Plane HTTP header 重复或缺少名称。")
        headers[key] = value.strip()
    else:
        raise _invalid_response("Control Plane HTTP header 数量超过上限。")
    if "transfer-encoding" in headers:
        raise _invalid_response("Control Plane 不得使用 Transfer-Encoding。")
    raw_length = headers.get("content-length", "")
    if not raw_length.isascii() or not raw_length.isdigit():
        raise _invalid_response("Control Plane 缺少有效 Content-Length。")
    length = int(raw_length)
    if not 0 <= length <= max_body_bytes:
        raise EvolutionStablePromotionObservationRevisionTransportError(
            "stable_promotion_observation_revision_http_response_oversized",
            "Control Plane 响应超过大小上限。",
            retryable=False,
        )
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise EvolutionStablePromotionObservationRevisionTransportError(
            "stable_promotion_observation_revision_http_response_truncated",
            "Control Plane 响应提前结束。",
        ) from exc
    return status, headers, body


async def _readline(reader: asyncio.StreamReader, label: str) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError as exc:
        raise EvolutionStablePromotionObservationRevisionTransportError(
            "stable_promotion_observation_revision_http_response_truncated",
            f"Control Plane HTTP {label} 提前结束。",
        ) from exc
    except asyncio.LimitOverrunError as exc:
        raise _invalid_response(f"Control Plane HTTP {label} 无效。") from exc
    if len(line) > _MAX_HEADER_LINE_BYTES:
        raise _invalid_response(f"Control Plane HTTP {label} 超过上限。")
    return line


def _invalid_response(
    message: str,
) -> EvolutionStablePromotionObservationRevisionTransportError:
    return EvolutionStablePromotionObservationRevisionTransportError(
        "stable_promotion_observation_revision_http_response_invalid",
        message,
        retryable=False,
    )


def _status_error(
    status: int,
    body: bytes,
) -> EvolutionStablePromotionObservationRevisionTransportError:
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
        f"stable_promotion_observation_revision_http_{remote_code}"
        if remote_code
        else f"stable_promotion_observation_revision_http_status_{status}"
    )
    return EvolutionStablePromotionObservationRevisionTransportError(
        code,
        f"Control Plane 拒绝 Observation Revision（HTTP {status}，{remote_code or 'unknown'}）。",
        retryable=retryable,
    )


def _endpoint(value: str) -> SplitResult:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Observation Revision endpoint URL 无效。") from exc
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.netloc.isascii()
        and not any(character.isspace() or ord(character) < 0x20 for character in parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == STABLE_PROMOTION_OBSERVATION_REVISION_HTTP_PATH
        and (port is None or 1 <= port <= 65535)
    ):
        raise ValueError("Observation Revision endpoint 必须是固定路径的 https URL。")
    return parsed


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not (
        isinstance(parsed, datetime)
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    ):
        raise ValueError("Observation Revision HTTP timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


__all__ = [
    "MTLSStablePromotionObservationRevisionControlPlaneTransport",
    "STABLE_PROMOTION_OBSERVATION_REVISION_HTTP_PATH",
    "STABLE_PROMOTION_OBSERVATION_REVISION_RECEIPT_MEDIA_TYPE",
    "STABLE_PROMOTION_OBSERVATION_REVISION_SUBMISSION_MEDIA_TYPE",
    "StablePromotionObservationRevisionHTTPClientPolicy",
    "StablePromotionObservationRevisionHTTPServer",
    "StablePromotionObservationRevisionHTTPServerPolicy",
]
