"""Mutually authenticated HTTPS transport for stable-finalization delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import TracebackType
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
    decode_stable_remote_finalization_delivery_ack,
    decode_stable_remote_finalization_delivery_package,
    encode_stable_remote_finalization_delivery_ack,
    encode_stable_remote_finalization_delivery_package,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationInstallationTransport,
    EvolutionStableRemoteFinalizationTransportError,
)

STABLE_REMOTE_FINALIZATION_HTTP_PATH = "/v1/stable-finalization/deliveries:receive"
STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE = (
    "application/vnd.naumi.stable-finalization-delivery.v1+base64"
)
STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE = (
    "application/vnd.naumi.stable-finalization-delivery-ack.v1+base64"
)

_DELIVERY_ID_RE = re.compile(r"^evstableremotedelivery_[0-9a-f]{24}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_MAX_ACK_BYTES = 512 * 1024
_MAX_HEADER_LINE_BYTES = 8 * 1024
_MAX_HEADER_BYTES = 32 * 1024
_MAX_HEADERS = 64


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationHTTPClientPolicy:
    """Fail-closed outbound TLS and HTTP limits."""

    endpoint_url: str
    server_ca_path: Path
    client_certificate_path: Path
    client_private_key_path: Path
    server_certificate_sha256_pins: tuple[str, ...]
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 20.0
    max_response_bytes: int = _MAX_ACK_BYTES

    def __post_init__(self) -> None:
        endpoint = _endpoint(self.endpoint_url)
        object.__setattr__(self, "endpoint_url", endpoint.geturl())
        object.__setattr__(
            self,
            "server_ca_path",
            validate_tls_file(self.server_ca_path, label="服务端 CA", private=False),
        )
        object.__setattr__(
            self,
            "client_certificate_path",
            validate_tls_file(
                self.client_certificate_path, label="客户端证书", private=False
            ),
        )
        object.__setattr__(
            self,
            "client_private_key_path",
            validate_tls_file(
                self.client_private_key_path, label="客户端私钥", private=True
            ),
        )
        object.__setattr__(
            self,
            "server_certificate_sha256_pins",
            validate_fingerprints(
                self.server_certificate_sha256_pins, label="服务端证书 pin"
            ),
        )
        validate_seconds(self.connect_timeout_seconds, "连接 timeout", maximum=120)
        validate_seconds(self.request_timeout_seconds, "请求 timeout", maximum=600)
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("远程安装请求 timeout 不能小于连接 timeout。")
        if not 1 <= self.max_response_bytes <= _MAX_ACK_BYTES:
            raise ValueError("远程安装 ACK 大小上限无效。")


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationHTTPServerPolicy:
    """Fail-closed inbound TLS, request and abuse limits."""

    server_certificate_path: Path
    server_private_key_path: Path
    client_ca_path: Path
    authorized_client_certificate_sha256: tuple[str, ...]
    max_request_bytes: int = _MAX_PACKAGE_BYTES
    tls_handshake_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 20.0
    requests_per_minute: int = 120
    max_concurrent_requests: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "server_certificate_path",
            validate_tls_file(
                self.server_certificate_path, label="服务端证书", private=False
            ),
        )
        object.__setattr__(
            self,
            "server_private_key_path",
            validate_tls_file(
                self.server_private_key_path, label="服务端私钥", private=True
            ),
        )
        object.__setattr__(
            self,
            "client_ca_path",
            validate_tls_file(self.client_ca_path, label="客户端 CA", private=False),
        )
        object.__setattr__(
            self,
            "authorized_client_certificate_sha256",
            validate_fingerprints(
                self.authorized_client_certificate_sha256,
                label="客户端证书授权指纹",
            ),
        )
        if not 1 <= self.max_request_bytes <= _MAX_PACKAGE_BYTES:
            raise ValueError("远程安装 package 大小上限无效。")
        validate_seconds(
            self.tls_handshake_timeout_seconds,
            "服务端 TLS handshake timeout",
            maximum=120,
        )
        validate_seconds(self.request_timeout_seconds, "服务端请求 timeout", maximum=600)
        if not 1 <= self.requests_per_minute <= 100_000:
            raise ValueError("远程安装请求速率上限无效。")
        if not 1 <= self.max_concurrent_requests <= 1024:
            raise ValueError("远程安装并发请求上限无效。")


class MTLSStableRemoteFinalizationInstallationTransport:
    """Async HTTP/1.1 client with same-connection certificate pinning."""

    def __init__(self, policy: StableRemoteFinalizationHTTPClientPolicy) -> None:
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

    async def receive(
        self,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
    ):
        body = encode_stable_remote_finalization_delivery_package(package).encode("ascii")
        if len(body) > _MAX_PACKAGE_BYTES:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_http_package_oversized",
                "Remote Finalization package 超过网络传输上限。",
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
                writer.write(self._request(package.delivery_id, body))
                await writer.drain()
                status, headers, response = await _read_response(
                    reader,
                    max_body_bytes=self.policy.max_response_bytes,
                )
                if status != 200:
                    raise _status_error(status)
                if headers.get("content-type", "").split(";", 1)[0].strip() != (
                    STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE
                ):
                    raise EvolutionStableRemoteFinalizationTransportError(
                        "stable_remote_http_ack_content_type_invalid",
                        "安装端返回了不受支持的 ACK media type。",
                        retryable=False,
                    )
                try:
                    ack = decode_stable_remote_finalization_delivery_ack(
                        response.decode("ascii")
                    )
                except (UnicodeDecodeError, ValueError) as exc:
                    raise EvolutionStableRemoteFinalizationTransportError(
                        "stable_remote_http_ack_invalid",
                        "安装端返回的 ACK artifact 无效。",
                        retryable=False,
                    ) from exc
                if ack.payload.delivery_id != package.delivery_id:
                    raise EvolutionStableRemoteFinalizationTransportError(
                        "stable_remote_http_ack_delivery_mismatch",
                        "安装端 ACK 与 Delivery 不匹配。",
                        retryable=False,
                    )
                return ack
        except EvolutionStableRemoteFinalizationTransportError:
            raise
        except TimeoutError as exc:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_http_timeout",
                "远程安装端请求超时。",
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_http_tls_identity_invalid",
                "远程安装端 TLS identity 验证失败。",
                retryable=False,
            ) from exc
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_http_unavailable",
                "远程安装端暂时不可用。",
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
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_http_server_pin_mismatch",
                "远程安装端证书不在 current/next pin 集合中。",
                retryable=False,
            )

    def _request(self, delivery_id: str, body: bytes) -> bytes:
        authority = self._endpoint.netloc
        headers = (
            f"POST {STABLE_REMOTE_FINALIZATION_HTTP_PATH} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Content-Type: {STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE}\r\n"
            f"Accept: {STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE}\r\n"
            f"X-Naumi-Delivery-ID: {delivery_id}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        return headers + body


class StableRemoteFinalizationHTTPServer:
    """Dedicated mTLS endpoint; intentionally separate from the general API app."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        policy: StableRemoteFinalizationHTTPServerPolicy,
        transport: EvolutionStableRemoteFinalizationInstallationTransport,
    ) -> None:
        normalized_host = str(bind_host or "").strip()
        if not normalized_host or len(normalized_host) > 255:
            raise ValueError("远程安装服务 bind host 无效。")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("远程安装服务 port 无效。")
        if not isinstance(transport, EvolutionStableRemoteFinalizationInstallationTransport):
            raise TypeError("目标 transport 必须实现认证安装传输契约。")
        self.bind_host = normalized_host
        self.port = port
        self.policy = policy
        self.transport = transport
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

            class Handler(_StableRemoteFinalizationRequestHandler):
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
                name="naumi-stable-finalization-mtls",
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
                raise RuntimeError("远程安装 mTLS 服务未在期限内停止。")
            return True

    def __enter__(self) -> StableRemoteFinalizationHTTPServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def _receive(self, package: EvolutionStableRemoteFinalizationDeliveryPackage):
        async def receive_with_timeout():
            async with asyncio.timeout(self.policy.request_timeout_seconds):
                return await self.transport.receive(package)

        return asyncio.run(receive_with_timeout())


class _StableRemoteFinalizationRequestHandler(BaseHTTPRequestHandler):
    endpoint: StableRemoteFinalizationHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "NaumiInstallationTransport/1"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self.close_connection = True
        self.connection.settimeout(self.endpoint.policy.request_timeout_seconds)
        if self.request_version != "HTTP/1.1":
            self._error(505, "http_version_not_supported")
            return
        if not self._authorize():
            return
        if self.path != STABLE_REMOTE_FINALIZATION_HTTP_PATH:
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
            STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE
        ):
            self._error(415, "content_type_invalid")
            return
        accept = self._single_header("Accept")
        if accept is None:
            return
        if accept.strip() != STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE:
            self._error(406, "accept_invalid")
            return
        delivery_id = self._single_header("X-Naumi-Delivery-ID")
        if delivery_id is None:
            return
        if _DELIVERY_ID_RE.fullmatch(delivery_id) is None:
            self._error(400, "delivery_id_invalid")
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
            package = decode_stable_remote_finalization_delivery_package(
                body.decode("ascii")
            )
        except (UnicodeDecodeError, ValueError):
            self._error(400, "package_invalid")
            return
        if package.delivery_id != delivery_id:
            self._error(409, "delivery_id_mismatch")
            return
        try:
            ack = self.endpoint._receive(package)
        except TimeoutError:
            self._error(503, "target_receive_timeout")
            return
        except EvolutionStableRemoteFinalizationTransportError as exc:
            self._error(503 if exc.retryable else 403, exc.code)
            return
        except EvolutionStableRemoteFinalizationDeliveryError as exc:
            self._error(422, exc.code)
            return
        except Exception:
            self._error(500, "target_receive_failed")
            return
        encoded = encode_stable_remote_finalization_delivery_ack(ack).encode("ascii")
        self.send_response(200)
        self._security_headers(STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE, len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def do_TRACE(self) -> None:  # noqa: N802 - stdlib handler contract
        self._reject_method()

    def _reject_method(self) -> None:
        self.close_connection = True
        if not self._authorize():
            return
        self._error(405, "method_not_allowed")

    def handle_expect_100(self) -> bool:
        self.close_connection = True
        if self._authorize():
            self._error(417, "expectation_not_supported")
        return False

    def log_message(self, _format: str, *args: object) -> None:
        """Do not log request data or certificate identities."""

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace stdlib HTML/parser details with a stable non-leaking artifact."""
        self.close_connection = True
        self._error(code, f"http_parse_error_{code}")

    def _peer_fingerprint(self) -> str:
        certificate = self.connection.getpeercert(binary_form=True)
        return "" if not certificate else hashlib.sha256(certificate).hexdigest()

    def _authorize(self) -> bool:
        identity = self._peer_fingerprint()
        if identity not in self.endpoint.policy.authorized_client_certificate_sha256:
            self._error(403, "client_identity_forbidden")
            return False
        if not self.endpoint._rate_limiter.allow(identity):
            self._error(429, "rate_limit_exceeded")
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
            self._error(413, "package_oversized")
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
    parts = status_line.decode("ascii", errors="strict").rstrip("\r\n").split(" ", 2)
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or not parts[1].isdigit():
        raise EvolutionStableRemoteFinalizationTransportError(
            "stable_remote_http_response_invalid",
            "安装端 HTTP status line 无效。",
            retryable=False,
        )
    status = int(parts[1])
    headers: dict[str, str] = {}
    total = len(status_line)
    for _ in range(_MAX_HEADERS):
        line = await _readline(reader, "header")
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise _invalid_response("安装端 HTTP headers 超过上限。")
        if line == b"\r\n":
            break
        try:
            name, value = line.decode("ascii").rstrip("\r\n").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_response("安装端 HTTP header 无效。") from exc
        key = name.strip().lower()
        if not key or key in headers:
            raise _invalid_response("安装端 HTTP header 重复或缺少名称。")
        headers[key] = value.strip()
    else:
        raise _invalid_response("安装端 HTTP header 数量超过上限。")
    if "transfer-encoding" in headers:
        raise _invalid_response("安装端不得使用 Transfer-Encoding。")
    raw_length = headers.get("content-length", "")
    if not raw_length.isascii() or not raw_length.isdigit():
        raise _invalid_response("安装端缺少有效 Content-Length。")
    length = int(raw_length)
    if not 0 <= length <= max_body_bytes:
        raise EvolutionStableRemoteFinalizationTransportError(
            "stable_remote_http_response_oversized",
            "安装端响应超过大小上限。",
            retryable=False,
        )
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise _truncated_response("安装端响应提前结束。") from exc
    return status, headers, body


async def _readline(reader: asyncio.StreamReader, label: str) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError as exc:
        raise _truncated_response(f"安装端 HTTP {label} 提前结束。") from exc
    except asyncio.LimitOverrunError as exc:
        raise _invalid_response(f"安装端 HTTP {label} 无效。") from exc
    if len(line) > _MAX_HEADER_LINE_BYTES:
        raise _invalid_response(f"安装端 HTTP {label} 超过上限。")
    return line


def _invalid_response(message: str) -> EvolutionStableRemoteFinalizationTransportError:
    return EvolutionStableRemoteFinalizationTransportError(
        "stable_remote_http_response_invalid", message, retryable=False
    )


def _truncated_response(message: str) -> EvolutionStableRemoteFinalizationTransportError:
    return EvolutionStableRemoteFinalizationTransportError(
        "stable_remote_http_response_truncated", message, retryable=True
    )


def _status_error(status: int) -> EvolutionStableRemoteFinalizationTransportError:
    retryable = status in {408, 425, 429} or 500 <= status <= 599
    if status in {400, 401, 403, 404, 405, 409, 411, 413, 415, 422}:
        retryable = False
    return EvolutionStableRemoteFinalizationTransportError(
        f"stable_remote_http_status_{status}",
        f"远程安装端拒绝请求（HTTP {status}）。",
        retryable=retryable,
    )


def _endpoint(value: str) -> SplitResult:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("远程安装 endpoint URL 无效。") from exc
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.netloc.isascii()
        and not any(
            character.isspace() or ord(character) < 0x20
            for character in parsed.netloc
        )
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == STABLE_REMOTE_FINALIZATION_HTTP_PATH
        and (port is None or 1 <= port <= 65535)
    ):
        raise ValueError("远程安装 endpoint 必须是固定路径的 https URL。")
    return parsed


__all__ = [
    "MTLSStableRemoteFinalizationInstallationTransport",
    "STABLE_REMOTE_FINALIZATION_ACK_MEDIA_TYPE",
    "STABLE_REMOTE_FINALIZATION_HTTP_PATH",
    "STABLE_REMOTE_FINALIZATION_PACKAGE_MEDIA_TYPE",
    "StableRemoteFinalizationHTTPClientPolicy",
    "StableRemoteFinalizationHTTPServer",
    "StableRemoteFinalizationHTTPServerPolicy",
]
