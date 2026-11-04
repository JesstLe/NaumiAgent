"""Fixed-provider desktop OAuth and a short-lived, single-use loopback transport."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
CERT_URI = "https://www.googleapis.com/oauth2/v1/certs"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
OPERATION_MARKER = "naumi_operation_id"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PATH = re.compile(r"^/[A-Za-z0-9_/-]{1,160}$")


class GoogleProtocolError(RuntimeError):
    """Only stable, non-sensitive codes/status escape the provider boundary."""

    def __init__(
        self,
        code: str,
        status: int | None = None,
        *,
        unknown=False,
        retryable=False,
        retry_after_seconds=None,
    ):
        if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid provider error code")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("invalid provider status")
        self.code = code
        self.status = status
        self.unknown = bool(unknown)
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


def _text(value, maximum=8192, *, minimum=1):
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise GoogleProtocolError("invalid_input")
    return value


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    result = json.loads(
        raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
    )
    if not isinstance(result, dict):
        raise ValueError("object required")
    return result


def _http_error(status, body, mutation, headers=None):
    code = {
        401: "authentication_required",
        403: "access_denied",
        404: "not_found",
        409: "conflict",
        410: "sync_token_expired",
        412: "precondition_failed",
        429: "rate_limited",
    }.get(status, "provider_error")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error in {
            "invalid_grant",
            "invalid_client",
            "invalid_request",
            "invalid_scope",
            "access_denied",
            "invalid_token",
            "unsupported_grant_type",
        }:
            code = error
        if status == 403 and isinstance(error, dict):
            reasons = (
                {
                    row.get("reason")
                    for row in error.get("errors", [])
                    if isinstance(row, dict) and isinstance(row.get("reason"), str)
                }
                if isinstance(error.get("errors", []), list)
                else set()
            )
            if reasons & {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "quotaExceeded",
                "dailyLimitExceeded",
            }:
                code = "quota_exceeded"
            elif reasons & {"insufficientPermissions", "insufficientAuthenticationScopes"}:
                code = "scope_denied"
    if status >= 500:
        code = "service_unavailable"
    if 300 <= status < 400:
        code = "redirect_rejected"
    retry_after = None
    raw_delay = (headers or {}).get("Retry-After")
    if isinstance(raw_delay, str) and len(raw_delay) <= 128:
        try:
            if raw_delay.isascii() and raw_delay.isdecimal():
                retry_after = min(86400, int(raw_delay))
            else:
                due = parsedate_to_datetime(raw_delay)
                if due.tzinfo is not None:
                    retry_after = min(86400, max(0, (due - datetime.now(UTC)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            retry_after = None
    return GoogleProtocolError(
        code,
        status,
        unknown=mutation and (status >= 500 or 300 <= status < 400),
        retryable=status == 429 or code == "quota_exceeded" or status >= 500,
        retry_after_seconds=retry_after,
    )


async def _request_json(
    client,
    method,
    url,
    *,
    mutation=False,
    maximum=256 * 1024,
    empty_ok=False,
    params=None,
    data=None,
    body=None,
    headers=None,
):
    """No retry or redirect; stream and bound even an injected client's response."""

    async def request(active):
        status = None
        try:
            async with active.stream(
                method,
                url,
                params=params,
                data=data,
                json=body,
                headers=headers,
                follow_redirects=False,
                timeout=httpx.Timeout(15, connect=5),
            ) as response:
                status = response.status_code
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > maximum:
                        raise GoogleProtocolError("response_too_large", status, unknown=mutation)
                    chunks.append(chunk)
                raw = b"".join(chunks)
            try:
                parsed = _json(raw) if raw else None
            except (ValueError, UnicodeError):
                parsed = None
            if not 200 <= status < 300:
                raise _http_error(status, parsed, mutation, response.headers)
            if empty_ok and not raw:
                return None
            if parsed is None:
                raise GoogleProtocolError("invalid_response", status, unknown=mutation)
            return parsed
        except GoogleProtocolError:
            raise
        except (httpx.RequestError, TimeoutError, OSError):
            raise GoogleProtocolError(
                "transport_unknown" if mutation else "transport_error",
                status,
                unknown=mutation,
                retryable=not mutation,
            ) from None

    if client is not None:
        return await request(client)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as owned:
        return await request(owned)


def _redirect(value):
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "http"
            and parts.hostname == "127.0.0.1"
            and parts.port is not None
            and 1 <= parts.port <= 65535
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and _PATH.fullmatch(parts.path)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise GoogleProtocolError("invalid_redirect_uri")
    return value


@dataclass(frozen=True, slots=True)
class GoogleOAuthClient:
    client_id: str
    client_secret: str = field(default="", repr=False)
    _http_client: Any = field(default=None, repr=False, compare=False)
    _cert_request: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_json(cls, value, *, http_client=None, cert_request=None):
        try:
            raw = (
                value.encode("utf-8")
                if isinstance(value, str)
                else json.dumps(value, allow_nan=False).encode("utf-8")
            )
            if len(raw) > 16 * 1024:
                raise ValueError()
            data = _json(raw)
            if set(data) != {"installed"} or not isinstance(data["installed"], dict):
                raise ValueError()
            config = data["installed"]
            if set(config) - {
                "client_id",
                "client_secret",
                "project_id",
                "auth_uri",
                "token_uri",
                "auth_provider_x509_cert_url",
                "redirect_uris",
            }:
                raise ValueError()
            client_id = _text(config.get("client_id"), 256)
            if not _CLIENT_ID.fullmatch(client_id):
                raise ValueError()
            if (
                config.get("auth_uri")
                not in {AUTH_URI, "https://accounts.google.com/o/oauth2/auth"}
                or config.get("token_uri") != TOKEN_URI
            ):
                raise ValueError()
            if config.get("auth_provider_x509_cert_url", CERT_URI) != CERT_URI:
                raise ValueError()
            secret = config.get("client_secret", "")
            if secret:
                _text(secret, 8192)
            elif not isinstance(secret, str):
                raise ValueError()
            redirects = config.get("redirect_uris", [])
            if not isinstance(redirects, list) or len(redirects) > 8:
                raise ValueError()
            for uri in redirects:
                parts = urlsplit(uri)
                if (
                    parts.scheme != "http"
                    or parts.hostname not in {"localhost", "127.0.0.1"}
                    or parts.username
                    or parts.password
                    or parts.query
                    or parts.fragment
                ):
                    raise ValueError()
            return cls(client_id, secret, http_client, cert_request)
        except (ValueError, TypeError, UnicodeError, GoogleProtocolError):
            raise GoogleProtocolError("invalid_desktop_client") from None

    def authorization_url(self, redirect_uri, state, nonce, code_challenge, *, write=False):
        _redirect(redirect_uri)
        _text(state, 512, minimum=16)
        _text(nonce, 512, minimum=16)
        if (
            not isinstance(code_challenge, str)
            or not _CHALLENGE.fullmatch(code_challenge)
            or type(write) is not bool
        ):
            raise GoogleProtocolError("invalid_input")
        return (
            AUTH_URI
            + "?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "openid email " + (WRITE_SCOPE if write else READ_SCOPE),
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "consent",
                }
            )
        )

    def _client_fields(self):
        return {
            "client_id": self.client_id,
            **({"client_secret": self.client_secret} if self.client_secret else {}),
        }

    async def _tokens(self, data):
        result = await _request_json(self._http_client, "POST", TOKEN_URI, mutation=True, data=data)
        try:
            _text(result.get("access_token"), 65536)
            if str(result.get("token_type", "")).lower() != "bearer":
                raise ValueError()
            if type(result.get("expires_in")) is not int or not 1 <= result["expires_in"] <= 604800:
                raise ValueError()
            for key in ("refresh_token", "id_token", "scope"):
                if key in result:
                    if (
                        not isinstance(result[key], str)
                        or not result[key]
                        or len(result[key]) > 65536
                        or any(ord(c) < 32 for c in result[key])
                    ):
                        raise ValueError()
        except (ValueError, GoogleProtocolError):
            raise GoogleProtocolError("invalid_token_response", 200, unknown=True) from None
        return {
            key: result[key]
            for key in (
                "access_token",
                "refresh_token",
                "id_token",
                "scope",
                "token_type",
                "expires_in",
                "refresh_token_expires_in",
            )
            if key in result
        }

    async def exchange_code(self, code, *, redirect_uri, code_verifier):
        _text(code, 8192)
        _redirect(redirect_uri)
        if not isinstance(code_verifier, str) or not _VERIFIER.fullmatch(code_verifier):
            raise GoogleProtocolError("invalid_input")
        return await self._tokens(
            {
                **self._client_fields(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token):
        _text(refresh_token, 65536)
        return await self._tokens(
            {**self._client_fields(), "grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, token):
        _text(token, 65536)
        await _request_json(
            self._http_client,
            "POST",
            REVOKE_URI,
            mutation=True,
            data={"token": token},
            empty_ok=True,
        )

    async def verify_id_token(self, token, *, nonce):
        _text(token, 16 * 1024)
        _text(nonce, 512, minimum=16)

        def verify():
            session = None
            try:
                from google.auth.transport.requests import Request
                from google.oauth2 import id_token

                if self._cert_request is None:
                    import requests

                    session = requests.Session()
                    session.trust_env = False
                    request = Request(session=session)

                    def transport_request(url, method="GET", **kwargs):
                        if url != CERT_URI or method != "GET":
                            raise ValueError()
                        kwargs.pop("timeout", None)
                        return request(
                            url, method=method, timeout=10, allow_redirects=False, **kwargs
                        )
                else:
                    transport_request = self._cert_request

                def certificate_request(url, method="GET", **kwargs):
                    if url != CERT_URI or method != "GET":
                        raise ValueError()
                    response = transport_request(url, method=method, **kwargs)
                    if response.status != 200 or len(response.data) > 256 * 1024:
                        raise ValueError()
                    certificates = _json(response.data)
                    # This fixed endpoint serves X509 maps. Reject JWK documents so
                    # google-auth never delegates a second fetch to PyJWKClient.
                    if (
                        not certificates
                        or "keys" in certificates
                        or len(certificates) > 20
                        or any(
                            not isinstance(value, str) or len(value) > 16384
                            for value in certificates.values()
                        )
                    ):
                        raise ValueError()
                    return response

                claims = id_token.verify_oauth2_token(
                    token, certificate_request, audience=self.client_id, clock_skew_in_seconds=0
                )
                if (
                    claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
                    or claims.get("aud") != self.client_id
                    or claims.get("azp", self.client_id) != self.client_id
                    or not isinstance(claims.get("nonce"), str)
                    or not hmac.compare_digest(claims["nonce"], nonce)
                    or claims.get("email_verified") is not True
                    or not isinstance(claims.get("email"), str)
                    or "@" not in claims["email"]
                    or len(claims["email"]) > 320
                    or any(ord(c) < 33 for c in claims["email"])
                    or not isinstance(claims.get("sub"), str)
                    or not 1 <= len(claims["sub"]) <= 255
                    or any(ord(c) < 33 or ord(c) > 126 for c in claims["sub"])
                    or type(claims.get("exp")) is not int
                    or claims["exp"] <= time.time()
                ):
                    raise ValueError()
                return claims
            except ImportError:
                raise GoogleProtocolError("identity_verifier_unavailable") from None
            except Exception:
                raise GoogleProtocolError("identity_verification_failed") from None
            finally:
                if session is not None:
                    session.close()

        return await asyncio.to_thread(verify)


class OAuthLoopback:
    """A callback transports one code; it never approves an action or selects an account."""

    def __init__(self, *, state, expires_at, path=None, max_request_bytes=8192):
        self._state = _text(state, 512, minimum=16)
        if (
            not isinstance(expires_at, datetime)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
        ):
            raise GoogleProtocolError("invalid_expiry")
        seconds = (expires_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        if (
            not 0 < seconds <= 900
            or type(max_request_bytes) is not int
            or not 512 <= max_request_bytes <= 65536
        ):
            raise GoogleProtocolError("invalid_expiry")
        self.expires_at = expires_at.astimezone(UTC)
        self.path = path or "/oauth/" + secrets.token_urlsafe(24)
        if not isinstance(self.path, str) or not _PATH.fullmatch(self.path):
            raise GoogleProtocolError("invalid_redirect_uri")
        self._maximum = max_request_bytes
        self._server = self._future = self._timer = None
        self._uri = None
        self._closed = self._expired = self._delivered = False
        self._connections = set()

    @property
    def redirect_uri(self):
        if self._uri is None:
            raise GoogleProtocolError("oauth_not_started")
        return self._uri

    async def start(self):
        if self._server is not None or self._closed:
            raise GoogleProtocolError("oauth_already_started")
        seconds = (self.expires_at - datetime.now(UTC)).total_seconds()
        if seconds <= 0:
            raise GoogleProtocolError("oauth_expired")
        self._future = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=self._maximum + 1
        )
        port = self._server.sockets[0].getsockname()[1]
        self._uri = f"http://127.0.0.1:{port}{self.path}"
        self._timer = asyncio.get_running_loop().call_later(seconds, self._expire)
        return self

    def _expire(self):
        self._expired = True
        if self._server is not None:
            self._server.close()
        if self._future is not None and not self._future.done():
            self._future.cancel()
        for writer in tuple(self._connections):
            writer.close()

    async def _handle(self, reader, writer):
        self._connections.add(writer)
        status = 400
        try:
            if self._closed or self._expired:
                status = 410
                raise ValueError()
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(raw) > self._maximum:
                raise ValueError()
            lines = raw.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            parts = urlsplit(target)
            query = parse_qs(
                parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16
            )
            if (
                method != "GET"
                or version != "HTTP/1.1"
                or parts.scheme
                or parts.netloc
                or parts.fragment
                or parts.path != self.path
                or any(len(values) != 1 for values in query.values())
            ):
                raise ValueError()
            headers = {}
            for line in lines[1:]:
                if not line:
                    break
                key, value = line.split(":", 1)
                key = key.lower()
                if key in headers:
                    raise ValueError()
                headers[key] = value.strip()
            if headers.get("host") != urlsplit(self.redirect_uri).netloc:
                raise ValueError()
            state = query.get("state", [""])[0]
            if not hmac.compare_digest(state, self._state) or "error" in query:
                raise ValueError()
            code = _text(query.get("code", [""])[0], 8192)
            if self._expired or datetime.now(UTC) >= self.expires_at:
                status = 410
            elif self._closed or self._future.done():
                status = 409
            else:
                self._future.set_result(code)
                status = 200
        except (
            ValueError,
            TypeError,
            UnicodeError,
            GoogleProtocolError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            OSError,
        ):
            pass
        finally:
            # No callback input is reflected into HTML, headers or errors.
            text = (
                "授权已接收，可以关闭此窗口。"
                if status == 200
                else "此授权回调未被接收，请返回应用继续。"
            )
            body = text.encode("utf-8")
            response_headers = (
                f"HTTP/1.1 {status} Result\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Content-Security-Policy: default-src 'none'\r\n"
                "X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
            )
            if not writer.is_closing():
                writer.write(response_headers.encode("ascii") + body)
            try:
                await writer.drain()
            except (OSError, ConnectionError):
                pass
            self._connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def wait(self, timeout=None):
        if timeout is not None and (
            type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0
        ):
            raise GoogleProtocolError("invalid_input")
        if self._future is None:
            raise GoogleProtocolError("oauth_not_started")
        if self._delivered:
            raise GoogleProtocolError("oauth_callback_consumed")
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise GoogleProtocolError("oauth_expired")
        try:
            code = await asyncio.wait_for(
                asyncio.shield(self._future),
                timeout=min(remaining, timeout) if timeout is not None else remaining,
            )
        except TimeoutError:
            raise GoogleProtocolError(
                "oauth_expired" if datetime.now(UTC) >= self.expires_at else "oauth_wait_timeout"
            ) from None
        except asyncio.CancelledError:
            if not self._future.cancelled():
                raise
            raise GoogleProtocolError(
                "oauth_expired" if self._expired else "oauth_cancelled"
            ) from None
        if self._delivered:
            raise GoogleProtocolError("oauth_callback_consumed")
        self._delivered = True
        return code

    async def close(self):
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._future is not None and not self._future.done():
            self._future.cancel()
        for writer in tuple(self._connections):
            writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for writer in tuple(self._connections)),
            return_exceptions=True,
        )

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.close()
