from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from naumi_agent.personal.google_oauth import (
    AUTH_URI,
    CERT_URI,
    READ_SCOPE,
    TOKEN_URI,
    WRITE_SCOPE,
    GoogleOAuthClient,
    GoogleProtocolError,
    OAuthLoopback,
)

CLIENT_ID = "fixture.apps.googleusercontent.com"
STATE = "fixture-state-0123456789abcdef"
NONCE = "fixture-nonce-0123456789abcdef"
VERIFIER = "a" * 64


def config():
    return {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": "fixture-secret-do-not-echo",
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": CERT_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


@pytest.fixture
def endpoint():
    state = SimpleNamespace(requests=[], status=200, body=None, reset=False, headers={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.command, self.path, dict(self.headers), body))
            self.answer()

        def do_GET(self):
            state.requests.append((self.command, self.path, dict(self.headers), b""))
            self.answer()

        def answer(self):
            if state.reset:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            body = state.body
            if body is None:
                body = {
                    "access_token": "fixture-access",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": READ_SCOPE,
                }
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


class Forward(httpx.AsyncBaseTransport):
    """Preserve production endpoint construction while executing real local HTTP."""

    def __init__(self, base):
        self.base = base
        self.urls = []
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        url = httpx.URL(self.base).copy_with(raw_path=request.url.raw_path)
        return await self.inner.handle_async_request(
            httpx.Request(
                request.method,
                url,
                headers=request.headers,
                content=await request.aread(),
            )
        )

    async def aclose(self):
        await self.inner.aclose()


def test_desktop_config_boundaries_repr_and_authorization():
    client = GoogleOAuthClient.from_json(json.dumps(config()))
    assert client.client_id == CLIENT_ID
    assert "fixture-secret" not in repr(client)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
    )
    for write, scope in [(False, READ_SCOPE), (True, WRITE_SCOPE)]:
        url = client.authorization_url(
            "http://127.0.0.1:45678/oauth/fixture", STATE, NONCE, challenge, write=write
        )
        query = parse_qs(urlsplit(url).query)
        assert url.startswith(AUTH_URI + "?")
        assert query["scope"] == ["openid email " + scope]
        assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
        assert query["code_challenge_method"] == ["S256"]
        assert "include_granted_scopes" not in query and "client_secret" not in query


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(web=c.pop("installed")),
        lambda c: c["installed"].update(auth_uri="https://example.invalid/auth"),
        lambda c: c["installed"].update(token_uri="http://127.0.0.1/token"),
        lambda c: c["installed"].update(
            auth_provider_x509_cert_url="https://example.invalid/certs"
        ),
        lambda c: c["installed"].update(client_id="wrong"),
        lambda c: c["installed"].update(redirect_uris=["https://example.invalid/callback"]),
        lambda c: c["installed"].update(project_id="a" * 17000),
    ],
)
def test_client_config_rejects_external_endpoints_and_oversize(mutation):
    value = config()
    mutation(value)
    with pytest.raises(GoogleProtocolError) as caught:
        GoogleOAuthClient.from_json(value)
    assert str(caught.value) == "invalid_desktop_client"
    assert "fixture-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "redirect",
    [
        "https://example.invalid/callback",
        "http://localhost:45678/callback",
        "http://127.0.0.1:45678/callback?state=other",
        "http://user@127.0.0.1:45678/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:45678/%2e%2e/callback",
    ],
)
def test_authorization_rejects_nonexact_loopback_redirect(redirect):
    with pytest.raises(GoogleProtocolError):
        GoogleOAuthClient.from_json(config()).authorization_url(redirect, STATE, NONCE, "a" * 43)


@pytest.mark.asyncio
async def test_code_refresh_revoke_use_real_http_and_do_not_invent_refresh_token(endpoint):
    transport = Forward(endpoint.base)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as http:
        client = GoogleOAuthClient.from_json(config(), http_client=http)
        tokens = await client.exchange_code(
            "fixture-code",
            redirect_uri="http://127.0.0.1:45678/oauth/fixture",
            code_verifier=VERIFIER,
        )
        assert tokens["access_token"] == "fixture-access" and "refresh_token" not in tokens
        first = parse_qs(endpoint.requests[0][3].decode())
        assert first["grant_type"] == ["authorization_code"] and first["code_verifier"] == [
            VERIFIER
        ]
        assert first["client_secret"] == ["fixture-secret-do-not-echo"]
        await client.refresh("fixture-refresh")
        second = parse_qs(endpoint.requests[1][3].decode())
        assert second["grant_type"] == ["refresh_token"] and second["refresh_token"] == [
            "fixture-refresh"
        ]
        endpoint.body = b""
        await client.revoke("fixture-refresh")
        assert endpoint.requests[-1][1] == "/revoke"
        assert len(endpoint.requests) == 3
        assert transport.urls[:2] == [TOKEN_URI, TOKEN_URI]


@pytest.mark.parametrize(
    "case,status,expected,unknown",
    [
        ("reset", 200, "transport_unknown", True),
        ("redirect", 302, "redirect_rejected", True),
        ("unavailable", 503, "service_unavailable", True),
        ("invalid_grant", 400, "invalid_grant", False),
        ("oversize", 200, "response_too_large", True),
        ("invalid_json", 200, "invalid_response", True),
        ("invalid_token", 200, "invalid_token_response", True),
    ],
)
@pytest.mark.asyncio
async def test_token_failure_is_typed_sanitized_and_never_retried(
    endpoint, case, status, expected, unknown
):
    endpoint.status = status
    endpoint.body = {"error": "invalid_grant", "error_description": "fixture-secret-do-not-echo"}
    if case == "reset":
        endpoint.reset = True
    elif case == "redirect":
        endpoint.headers = {"Location": endpoint.base + "/leaked-secret"}
    elif case == "oversize":
        endpoint.body = b"x" * (256 * 1024 + 1)
    elif case == "invalid_json":
        endpoint.body = b'{"duplicate":1,"duplicate":2}'
    async with httpx.AsyncClient(transport=Forward(endpoint.base), follow_redirects=True) as http:
        with pytest.raises(GoogleProtocolError) as caught:
            await GoogleOAuthClient.from_json(config(), http_client=http).refresh("fixture-refresh")
    assert caught.value.code == expected and caught.value.unknown is unknown
    assert len(endpoint.requests) == 1
    assert "secret" not in str(caught.value) and "refresh" not in str(caught.value)


@pytest.fixture(scope="module")
def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


def signed_token(key, claims):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = encode(
        json.dumps(
            {"alg": "RS256", "kid": "fixture-key", "typ": "JWT"}, separators=(",", ":")
        ).encode()
    )
    payload = encode(json.dumps(claims, separators=(",", ":")).encode())
    message = header + b"." + payload
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return (message + b"." + encode(signature)).decode()


@pytest.mark.parametrize(
    "change", [None, "issuer", "audience", "expired", "nonce", "email_verified", "signature"]
)
@pytest.mark.asyncio
async def test_oidc_maintained_verifier_checks_real_signature_and_claims(
    endpoint, signing_material, change
):
    key, certificate = signing_material
    endpoint.body = {"fixture-key": certificate}
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "fixture-stable-sub",
        "iat": int(time.time()) - 10,
        "exp": int(time.time()) + 300,
        "nonce": NONCE,
        "email": "fixture@example.invalid",
        "email_verified": True,
    }
    changes = {
        "issuer": ("iss", "https://example.invalid"),
        "audience": ("aud", "other.apps.googleusercontent.com"),
        "expired": ("exp", int(time.time()) - 5),
        "nonce": ("nonce", "wrong-nonce-0123456789"),
        "email_verified": ("email_verified", "true"),
    }
    if change in changes:
        claims[changes[change][0]] = changes[change][1]
    signing_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if change == "signature"
        else key
    )
    token = signed_token(signing_key, claims)

    def cert_request(url, method="GET", **_):
        assert url == CERT_URI and method == "GET"
        with urllib.request.urlopen(endpoint.base + "/certs", timeout=5) as response:
            return SimpleNamespace(
                status=response.status, data=response.read(), headers=dict(response.headers)
            )

    client = GoogleOAuthClient.from_json(config(), cert_request=cert_request)
    if change is None:
        assert (await client.verify_id_token(token, nonce=NONCE))["sub"] == claims["sub"]
    else:
        with pytest.raises(GoogleProtocolError) as caught:
            await client.verify_id_token(token, nonce=NONCE)
        assert caught.value.code == "identity_verification_failed"
        assert token not in str(caught.value)
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
async def test_loopback_real_socket_invalid_callbacks_do_not_consume_and_success_once():
    async with OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=30)
    ) as listener:
        async with httpx.AsyncClient(trust_env=False) as http:
            for query in [
                {"state": "wrong-state", "code": "<script>fixture</script>"},
                {
                    "state": STATE,
                    "error": "access_denied",
                    "error_description": "<script>fixture</script>",
                },
                {"state": "中文", "code": "fixture"},
            ]:
                response = await http.get(listener.redirect_uri, params=query)
                assert response.status_code == 400 and "<script>" not in response.text
            duplicate = await http.get(
                listener.redirect_uri + "?" + urlencode({"state": STATE, "code": "a"}) + "&code=b"
            )
            assert duplicate.status_code == 400
            response = await http.get(
                listener.redirect_uri, params={"state": STATE, "code": "fixture-final-code"}
            )
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert await listener.wait() == "fixture-final-code"
            assert (
                await http.get(listener.redirect_uri, params={"state": STATE, "code": "second"})
            ).status_code == 409
            with pytest.raises(GoogleProtocolError, match="oauth_callback_consumed"):
                await listener.wait()


@pytest.mark.asyncio
async def test_loopback_wrong_path_wait_timeout_cancel_and_expiry():
    listener = await OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=30)
    ).start()
    async with httpx.AsyncClient(trust_env=False) as http:
        assert (
            await http.get(
                listener.redirect_uri + "/wrong", params={"state": STATE, "code": "fixture"}
            )
        ).status_code == 400
    with pytest.raises(GoogleProtocolError, match="oauth_wait_timeout"):
        await listener.wait(timeout=0.01)
    await listener.close()
    with pytest.raises(GoogleProtocolError, match="oauth_cancelled"):
        await listener.wait()
    expiring = await OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=0.05)
    ).start()
    with pytest.raises(GoogleProtocolError, match="oauth_expired"):
        await expiring.wait()
    await expiring.close()


@pytest.mark.asyncio
async def test_loopback_concurrent_waiters_deliver_code_only_once():
    async with OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=30)
    ) as listener:
        waiters = [asyncio.create_task(listener.wait()) for _ in range(2)]
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.get(listener.redirect_uri, params={"state": STATE, "code": "one-code"})
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert results.count("one-code") == 1
        assert (
            sum(
                isinstance(result, GoogleProtocolError) and result.code == "oauth_callback_consumed"
                for result in results
            )
            == 1
        )


@pytest.mark.asyncio
async def test_jwk_document_is_rejected_without_a_second_certificate_fetch(
    endpoint, signing_material
):
    key, _ = signing_material
    endpoint.body = {"keys": []}

    def cert_request(url, method="GET", **_):
        assert url == CERT_URI and method == "GET"
        with urllib.request.urlopen(endpoint.base + "/certs", timeout=5) as response:
            return SimpleNamespace(status=response.status, data=response.read(), headers={})

    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "fixture-sub",
        "iat": int(time.time()) - 1,
        "exp": int(time.time()) + 300,
        "nonce": NONCE,
        "email": "fixture@example.invalid",
        "email_verified": True,
    }
    with pytest.raises(GoogleProtocolError, match="identity_verification_failed"):
        await GoogleOAuthClient.from_json(config(), cert_request=cert_request).verify_id_token(
            signed_token(key, claims), nonce=NONCE
        )
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
@pytest.mark.asyncio
async def test_loopback_wait_rejects_invalid_timeout(timeout):
    async with OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=30)
    ) as listener:
        with pytest.raises(GoogleProtocolError, match="invalid_input"):
            await listener.wait(timeout=timeout)


@pytest.mark.asyncio
async def test_loopback_wrong_host_oversize_and_partial_connection_close():
    listener = await OAuthLoopback(
        state=STATE, expires_at=datetime.now(UTC) + timedelta(seconds=30)
    ).start()
    address = urlsplit(listener.redirect_uri)
    for host, header in [
        ("wrong.invalid", ""),
        (address.netloc, "X-Padding: " + "a" * 9000 + "\r\n"),
    ]:
        reader, writer = await asyncio.open_connection("127.0.0.1", address.port)
        target = address.path + "?" + urlencode({"state": STATE, "code": "fixture-code"})
        writer.write((f"GET {target} HTTP/1.1\r\nHost: {host}\r\n{header}\r\n").encode())
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 400")
        writer.close()
        await writer.wait_closed()
    reader, writer = await asyncio.open_connection("127.0.0.1", address.port)
    writer.write(b"GET /partial HTTP/1.1\r\n")
    await writer.drain()
    await listener.close()
    closed_response = await asyncio.wait_for(reader.read(), 2)
    assert closed_response == b"" or closed_response.startswith((b"HTTP/1.1 400", b"HTTP/1.1 410"))
    writer.close()
    await writer.wait_closed()


def test_error_types_reject_unstructured_sensitive_messages():
    with pytest.raises(ValueError) as caught:
        GoogleProtocolError("raw secret=response")
    assert "secret" not in str(caught.value)
