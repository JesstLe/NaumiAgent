"""Shared fail-closed infrastructure for stable-finalization HTTPS endpoints."""

from __future__ import annotations

import os
import re
import socket
import ssl
import stat
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class FingerprintRateLimiter:
    """Thread-safe fixed-window limiter keyed by authenticated leaf fingerprint."""

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.clock = clock
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, identity: str) -> bool:
        now = self.clock()
        cutoff = now - 60.0
        with self._lock:
            entries = self._entries[identity]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self.limit:
                return False
            entries.append(now)
            return True


class BoundedTLSHTTPServer(ThreadingHTTPServer):
    """Bound request concurrency and complete TLS before invoking a handler."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        *,
        max_concurrent_requests: int,
    ) -> None:
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._active_requests = 0
        self._active_lock = threading.Lock()
        self._ssl_context: ssl.SSLContext | None = None
        self._handshake_timeout_seconds = 5.0
        super().__init__(server_address, request_handler)

    def configure_tls(
        self,
        context: ssl.SSLContext,
        *,
        handshake_timeout_seconds: float,
    ) -> None:
        self._ssl_context = context
        self._handshake_timeout_seconds = handshake_timeout_seconds

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active_requests -= 1
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: object,
    ) -> None:
        try:
            context = self._ssl_context
            if context is None:
                self.shutdown_request(request)
                return
            request.settimeout(self._handshake_timeout_seconds)
            try:
                tls_request = context.wrap_socket(request, server_side=True)
            except (ConnectionError, OSError, TimeoutError, ssl.SSLError):
                self.shutdown_request(request)
                return
            super().process_request_thread(tls_request, client_address)
        finally:
            with self._active_lock:
                self._active_requests -= 1
            self._request_slots.release()

    @property
    def active_requests(self) -> int:
        with self._active_lock:
            return self._active_requests

    def handle_error(self, request: object, client_address: object) -> None:
        """Avoid leaking handler exceptions or peer identities to stderr."""


def validate_fingerprints(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Normalize a current/next SHA-256 leaf-certificate pin set."""

    normalized = tuple(dict.fromkeys(str(item or "").strip().lower() for item in values))
    if not 1 <= len(normalized) <= 2 or any(
        _FINGERPRINT_RE.fullmatch(item) is None for item in normalized
    ):
        raise ValueError(f"{label} 必须包含 1–2 个 SHA-256 指纹。")
    return normalized


def validate_tls_file(value: str | Path, *, label: str, private: bool) -> Path:
    """Resolve a bounded regular TLS file and reject unsafe POSIX permissions."""

    source = Path(value).expanduser()
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ValueError(f"{label}不存在或不可读。") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label}必须是普通文件，不能是符号链接。")
    if not 1 <= metadata.st_size <= 4 * 1024 * 1024:
        raise ValueError(f"{label}为空或超过 4 MiB。")
    if os.name != "nt" and metadata.st_mode & (0o077 if private else 0o022):
        requirement = "不能允许 group/world 访问" if private else "不能允许 group/world 写入"
        raise ValueError(f"{label}{requirement}。")
    return source.resolve()


def validate_seconds(value: float, label: str, *, maximum: float) -> None:
    """Validate a finite operational timeout range."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}无效。")
    if not 0.1 <= float(value) <= maximum:
        raise ValueError(f"{label}无效。")


__all__ = [
    "BoundedTLSHTTPServer",
    "FingerprintRateLimiter",
    "validate_fingerprints",
    "validate_seconds",
    "validate_tls_file",
]
