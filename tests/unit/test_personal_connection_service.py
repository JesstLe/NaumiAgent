"""Actual connection authority, isolated AES vault and local HTTP lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.request
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from naumi_agent.personal.calendar_actions import CalendarActionMixin
from naumi_agent.personal.calendar_sync import CalendarSyncMixin
from naumi_agent.personal.connection_models import canonical, digest
from naumi_agent.personal.connection_vault import ConnectionVault
from naumi_agent.personal.connections import ConnectionAuthority
from naumi_agent.personal.google_calendar import GoogleCalendarAdapter
from naumi_agent.personal.google_oauth import (
    CERT_URI,
    READ_SCOPE,
    WRITE_SCOPE,
    GoogleOAuthClient,
)
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_google_oauth import (
    CLIENT_ID,
    Forward,
    config,
    signed_token,
)
from tests.unit.test_personal_google_oauth import (
    signing_material as _signing_material,
)
from tests.unit.test_personal_items_service import system as _item_system

signing_material = _signing_material
system = _item_system

CONNECTION = "pc_" + "1" * 32
SECRET = "fixture-refresh-never-public"
MANUAL = "manual_api"


class MemoryBackend:
    """Dedicated fixture credentials; never consult the real OS credential backend."""

    def __init__(self):
        self.values, self.calls = {}, []
        self.lock = threading.RLock()

    def get_password(self, service, account):
        with self.lock:
            self.calls.append(("get", service, account))
            return self.values.get((service, account))

    def set_password(self, service, account, value):
        with self.lock:
            self.calls.append(("set", service, account))
            self.values[(service, account)] = value

    def delete_password(self, service, account):
        with self.lock:
            self.calls.append(("delete", service, account))
            self.values.pop((service, account), None)


class FixtureService(CalendarActionMixin, CalendarSyncMixin, ConnectionAuthority):
    """Compose the actual production mixins, not a replacement authority implementation."""


@pytest.fixture
def calendar_endpoint(signing_material):
    key, certificate = signing_material
    state = SimpleNamespace(
        requests=[],
        nonce=None,
        subject="fixture-original-subject",
        audience=CLIENT_ID,
        email="calendar@example.invalid",
        write=False,
        token_status=200,
        token_reset=False,
        token_body=None,
        token_entered=threading.Event(),
        token_release=threading.Event(),
        revoke_entered=threading.Event(),
        revoke_release=threading.Event(),
        revoke_status=200,
        calendar=lambda _query, _path: (200, {"items": [], "nextSyncToken": "initial-token"}),
    )
    state.token_release.set()
    state.revoke_release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.answer()

        def do_POST(self):
            self.answer()

        def answer(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            state.requests.append((self.command, self.path, dict(self.headers), raw))
            status = 200
            if path.path == "/token":
                state.token_entered.set()
                assert state.token_release.wait(timeout=15)
                if state.token_reset:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status = state.token_status
                body = state.token_body
                if body is None:
                    claims = {
                        "iss": "https://accounts.google.com",
                        "aud": state.audience,
                        "sub": state.subject,
                        "iat": int(time.time()) - 5,
                        "exp": int(time.time()) + 300,
                        "nonce": state.nonce,
                        "email": state.email,
                        "email_verified": True,
                    }
                    body = {
                        "access_token": "fixture-access-renewed",
                        "refresh_token": "fixture-refresh-rotated",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "openid email " + (WRITE_SCOPE if state.write else READ_SCOPE),
                        "id_token": signed_token(key, claims),
                    }
            elif path.path == "/revoke":
                state.revoke_entered.set()
                assert state.revoke_release.wait(timeout=15)
                status, body = state.revoke_status, {}
            elif path.path == "/certs":
                body = {"fixture-key": certificate}
            else:
                status, body = state.calendar(query, path.path)
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    yield state
    state.token_release.set()
    state.revoke_release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
async def connection_system(system, calendar_endpoint):
    items, engine, port, clock = system
    clock[0] = datetime.now(UTC)
    backend = MemoryBackend()
    transport = Forward(calendar_endpoint.base)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)

    def certificates(url, method="GET", **_):
        assert url == CERT_URI and method == "GET"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(calendar_endpoint.base + "/certs", timeout=5) as response:
            return SimpleNamespace(
                status=response.status, data=response.read(), headers=dict(response.headers)
            )

    authority = FixtureService(
        engine,
        vault=ConnectionVault(backend),
        adapter=GoogleCalendarAdapter(http_client=http),
        oauth_factory=lambda raw: GoogleOAuthClient.from_json(
            raw, http_client=http, cert_request=certificates
        ),
        clock=lambda: clock[0],
    )
    engine.personal_connections = authority
    engine.personal_items = items
    yield SimpleNamespace(
        service=authority,
        engine=engine,
        items=items,
        port=port,
        clock=clock,
        backend=backend,
        endpoint=calendar_endpoint,
        http=http,
        transport=transport,
    )
    calendar_endpoint.token_release.set()
    calendar_endpoint.revoke_release.set()
    tasks = [
        *authority._oauth_tasks.values(),
        *getattr(authority, "_sync_tasks", {}).values(),
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await http.aclose()


async def configure(authority, *, request="client-original", value=None):
    return await authority.configure_client(
        {"request_id": request, "client_json": json.dumps(config() if value is None else value)},
        trusted_channel=MANUAL,
    )


async def seed_connection(fixture, *, expires=None, write=False):
    authority = fixture.service
    slot = "conn_" + "2" * 32
    authority.vault.provision(owner_key=authority.owner_key, slot=slot)
    bundle = {
        "access_token": "fixture-access-original",
        "refresh_token": SECRET,
        "expires_at": (expires or authority.now() + timedelta(hours=1)).isoformat(),
        "scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            WRITE_SCOPE if write else READ_SCOPE,
        ],
        "client": config(),
    }
    envelope = authority.vault.seal(
        owner_key=authority.owner_key, slot=slot, purpose="calendar_tokens", value=bundle
    )
    return await authority._write(
        lambda tx: tx.put(
            "connection",
            {
                "id": CONNECTION,
                "provider": "google_calendar",
                "status": "connected",
                "issuer": "https://accounts.google.com",
                "subject": "fixture-original-subject",
                "email": "calendar@example.invalid",
                "email_verified": True,
                "auth_epoch": 1,
                "can_write": write,
                "scopes": bundle["scopes"],
                "credential_envelope": envelope,
                "key_slot": slot,
                "token_generation": 1,
                "access_expires_at": bundle["expires_at"],
                "client_id_digest": digest(CLIENT_ID),
                "grants": {},
                "sync_status": "not_synced",
                "last_sync_at": None,
                "last_error_code": None,
                "revoke_status": None,
                "updated_at": authority.stamp(),
            },
            expected_revision=0,
        )
    )


async def oauth_callback(fixture, intent):
    authority, endpoint = fixture.service, fixture.endpoint
    query = parse_qs(urlsplit(intent["authorization_url"]).query)
    endpoint.nonce = query["nonce"][0]
    endpoint.write = "owned" in query["scope"][0] and "readonly" not in query["scope"][0]
    task = authority._oauth_tasks[intent["intent_id"]]
    async with httpx.AsyncClient(trust_env=False) as local:
        response = await local.get(
            query["redirect_uri"][0],
            params={"state": query["state"][0], "code": "fixture-once-code"},
        )
    assert response.status_code == 200
    return task


def requests(endpoint, path):
    return [entry for entry in endpoint.requests if urlsplit(entry[1]).path == path]


async def assert_error(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        await operation
    assert caught.value.code == code, (caught.value.code, str(caught.value))
    assert SECRET not in str(caught.value)


async def test_fresh_context_is_read_only_and_does_not_touch_native_credentials(connection_system):
    fixture = connection_system
    context = await fixture.service.context()
    assert context["client_configured"] is False
    assert context["provider"] == "google_calendar"
    assert not fixture.service.store.base_dir.exists()
    assert not fixture.service.boundary.exists()
    assert fixture.backend.calls == [] and fixture.endpoint.requests == []


async def test_client_configuration_requires_manual_channel_and_encrypts_secret(connection_system):
    fixture, authority = connection_system, connection_system.service
    await assert_error(
        "scope_denied",
        authority.configure_client(
            {"request_id": "not-human", "client_json": json.dumps(config())},
            trusted_channel="model_tool",
        ),
    )
    assert fixture.backend.calls == [] and not authority.store.base_dir.exists()
    assert (await configure(authority))["client_configured"] is True
    assert await authority._client_data() == config()
    for path in authority.store.base_dir.iterdir():
        if path.is_file():
            assert b"fixture-secret-do-not-echo" not in path.read_bytes()
    assert fixture.endpoint.requests == []


async def test_old_client_request_replay_does_not_resurrect_superseded_configuration(
    connection_system,
):
    authority = connection_system.service
    original = config()
    second = deepcopy(original)
    second["installed"]["client_id"] = "new-client.apps.googleusercontent.com"
    await configure(authority, value=original)
    await configure(authority, request="client-second", value=second)
    before = await authority._record("client", "google_calendar")
    await configure(authority, value=original)
    after = await authority._record("client", "google_calendar")
    assert before == after and await authority._client_data() == second


async def test_real_loopback_signed_identity_and_acl_read_activate_once(connection_system):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    intent = await authority.start_oauth(
        {"request_id": "oauth-original", "write": False}, trusted_channel=MANUAL
    )
    query = parse_qs(urlsplit(intent["authorization_url"]).query)
    task = await oauth_callback(fixture, intent)
    await asyncio.wait_for(task, timeout=10)
    final = await authority.oauth_status(intent["intent_id"])
    assert final["status"] == "connected" and final["connection_id"].startswith("pc_")
    connection = await authority._record("connection", final["connection_id"])
    assert connection["status"] == "connected" and connection["can_write"] is False
    assert await authority.token(connection["id"]) == "fixture-access-renewed"
    sent = parse_qs(requests(fixture.endpoint, "/token")[0][3].decode())
    import base64
    import hashlib

    actual_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"][0].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [actual_challenge]
    assert len(requests(fixture.endpoint, "/token")) == 1
    assert len(requests(fixture.endpoint, "/certs")) == 1
    assert any("/calendars/primary/events" in row[1] for row in fixture.endpoint.requests)
    public = canonical(await authority.list_connections())
    assert "credential_envelope" not in public and "key_slot" not in public
    assert "fixture-access" not in public and "fixture-refresh" not in public
    assert (await authority._record("oauth", intent["intent_id"]))["secret_envelope"] is None


async def test_reauthorization_rejects_a_different_signed_google_subject(connection_system):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    before = await seed_connection(fixture)
    fixture.endpoint.subject = "fixture-different-subject"
    intent = await authority.start_oauth(
        {
            "request_id": "upgrade-wrong-account",
            "write": True,
            "connection_id": CONNECTION,
            "expected_revision": before["revision"],
        },
        trusted_channel=MANUAL,
    )
    await asyncio.wait_for(await oauth_callback(fixture, intent), timeout=10)
    final = await authority.oauth_status(intent["intent_id"])
    assert final["status"] == "denied" and final["error_code"] == "account_mismatch"
    assert await authority._record("connection", CONNECTION) == before


async def test_cancel_before_callback_never_exchanges_code_or_creates_connection(connection_system):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    intent = await authority.start_oauth(
        {"request_id": "cancelled-oauth", "write": False}, trusted_channel=MANUAL
    )
    final = await authority.cancel_oauth(
        intent["intent_id"], {"request_id": "cancel-original"}, trusted_channel=MANUAL
    )
    await asyncio.gather(*authority._oauth_tasks.values(), return_exceptions=True)
    assert final["status"] == "cancelled"
    assert (await authority.list_connections())["connections"] == []
    assert requests(fixture.endpoint, "/token") == []


async def test_cancel_during_real_code_exchange_never_activates_returned_credentials(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    fixture.endpoint.token_release.clear()
    intent = await authority.start_oauth(
        {"request_id": "cancel-during-exchange", "write": False}, trusted_channel=MANUAL
    )
    task = await oauth_callback(fixture, intent)
    assert await asyncio.to_thread(fixture.endpoint.token_entered.wait, 5)
    await authority.cancel_oauth(
        intent["intent_id"], {"request_id": "cancel-exchange-original"}, trusted_channel=MANUAL
    )
    fixture.endpoint.token_release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert (await authority.oauth_status(intent["intent_id"]))["status"] == "cancelled"
    assert (await authority.list_connections())["connections"] == []
    assert len(requests(fixture.endpoint, "/token")) == 1


async def test_public_original_request_lookup_distinguishes_unknown_and_context_without_secret(
    connection_system,
):
    authority = connection_system.service
    unknown = await authority.request_status("not-observed")
    assert unknown["status"] == "unknown" and unknown["result"] is None
    await configure(authority)
    known = await authority.request_status("client-original")
    assert known["status"] == "applied" and known["result_kind"] == "context"
    assert known["result_id"] is None
    assert known["result"]["client_configured"] is True
    assert "fixture-secret" not in canonical(known)


async def test_oauth_start_receipt_is_applied_while_original_intent_waits_for_google(
    connection_system,
):
    authority = connection_system.service
    await configure(authority)
    intent = await authority.start_oauth(
        {"request_id": "oauth-start-response-lost", "write": False}, trusted_channel=MANUAL
    )
    recovered = await authority.request_status("oauth-start-response-lost")
    assert recovered["status"] == "applied" and recovered["result_kind"] == "oauth"
    assert recovered["result_id"] == intent["intent_id"]
    assert recovered["result"]["intent_id"] == intent["intent_id"]
    assert recovered["result"]["status"] == "pending"
    assert recovered["result"]["authorization_url"] == intent["authorization_url"]
    assert (await authority.list_connections())["connections"] == []


async def test_sync_start_receipt_is_applied_while_exact_connection_remains_syncing(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    entered, release = threading.Event(), threading.Event()

    def calendar(_query, _path):
        entered.set()
        assert release.wait(timeout=10)
        return 200, {"items": [], "nextSyncToken": "sync-recovered"}

    fixture.endpoint.calendar = calendar
    await authority.sync(
        CONNECTION,
        {"request_id": "sync-start-response-lost", "expected_revision": original["revision"]},
        trusted_channel=MANUAL,
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        recovered = await authority.request_status("sync-start-response-lost")
        assert recovered["status"] == "applied" and recovered["result_kind"] == "connection"
        assert recovered["result_id"] == CONNECTION
        assert recovered["result"]["id"] == CONNECTION
        assert recovered["result"]["sync_status"] == "syncing"
    finally:
        release.set()
        await asyncio.gather(*list(authority._sync_tasks.values()))


async def test_missing_fixture_key_marks_credentials_invalid_and_fences_old_authority(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    fixture.backend.values.clear()
    await assert_error("credential_missing", authority.token(CONNECTION))
    current = await authority._record("connection", CONNECTION)
    assert current["status"] == "credential_error"
    assert current["auth_epoch"] > original["auth_epoch"]
    await assert_error("connection_revoked", authority.token(CONNECTION))
    assert fixture.endpoint.requests == []


async def test_runtime_boundary_prevents_missing_original_database_from_becoming_fresh(
    connection_system, tmp_path
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    witness = authority.boundary.read_bytes()
    saved = tmp_path / "original-connections.db"
    authority.store.db_path.rename(saved)
    try:
        await assert_error("storage_error", authority.context())
        await assert_error("storage_error", configure(authority, request="must-not-recreate"))
        assert not authority.store.db_path.exists()
        assert authority.boundary.read_bytes() == witness
    finally:
        saved.rename(authority.store.db_path)
    assert await authority.token(CONNECTION) == "fixture-access-original"


async def test_refresh_rotation_and_same_process_contention_have_one_wire_send(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture, expires=authority.now() - timedelta(seconds=1))
    fixture.endpoint.token_release.clear()
    first = asyncio.create_task(authority.token(CONNECTION))
    assert await asyncio.to_thread(fixture.endpoint.token_entered.wait, 5)
    contenders = await asyncio.gather(
        *(authority.token(CONNECTION) for _ in range(5)), return_exceptions=True
    )
    assert all(isinstance(value, PersonalAgentError) for value in contenders)
    fixture.endpoint.token_release.set()
    assert await first == "fixture-access-renewed"
    assert await authority.token(CONNECTION) == "fixture-access-renewed"
    bundle = await authority._credential(await authority._record("connection", CONNECTION))
    assert bundle["refresh_token"] == "fixture-refresh-rotated"
    assert len(requests(fixture.endpoint, "/token")) == 1


@pytest.mark.parametrize(
    "reset,code,status",
    [(True, None, "refresh_unknown"), (False, "invalid_grant", "reauth_required")],
)
async def test_refresh_failure_is_not_replayed_and_invalidates_original_epoch(
    connection_system, reset, code, status
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture, expires=authority.now() - timedelta(seconds=1))
    fixture.endpoint.token_reset = reset
    if code:
        fixture.endpoint.token_status = 400
        fixture.endpoint.token_body = {"error": code, "error_description": SECRET}
    with pytest.raises(PersonalAgentError):
        await authority.token(CONNECTION)
    current = await authority._record("connection", CONNECTION)
    assert current["status"] == status and current["auth_epoch"] > original["auth_epoch"]
    with pytest.raises(PersonalAgentError):
        await authority.token(CONNECTION)
    assert len(requests(fixture.endpoint, "/token")) == 1
    assert SECRET not in canonical(authority.connection_view(current))


@pytest.mark.parametrize("mutation", ["extra-scope", "oversize-access", "excessive-lifetime"])
async def test_unverifiable_refresh_response_becomes_unknown_without_another_wire_send(
    connection_system, mutation
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture, expires=authority.now() - timedelta(seconds=1))
    body = {
        "access_token": "fixture-new-access",
        "refresh_token": "fixture-new-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid email " + READ_SCOPE,
    }
    if mutation == "extra-scope":
        body["scope"] += " https://www.googleapis.com/auth/drive"
    elif mutation == "oversize-access":
        body["access_token"] = "x" * 17000
    else:
        body["expires_in"] = 100000
    fixture.endpoint.token_body = body
    with pytest.raises(PersonalAgentError):
        await authority.token(CONNECTION)
    current = await authority._record("connection", CONNECTION)
    assert current["status"] == "refresh_unknown"
    assert current["auth_epoch"] > original["auth_epoch"]
    await assert_error("connection_revoked", authority.token(CONNECTION))
    assert len(requests(fixture.endpoint, "/token")) == 1


async def test_expired_sent_refresh_lease_is_unknown_and_never_retransmits(connection_system):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture, expires=authority.now() - timedelta(seconds=1))
    past = authority.now().timestamp() - 120

    def sent_before_crash(tx):
        lease = tx.claim("refresh", CONNECTION, "previous-process", now=past, ttl=60, once=True)
        tx.require_lease(
            "refresh", CONNECTION, "previous-process", lease["fence"], now=past, phase="sent"
        )

    await authority._write(sent_before_crash)
    with pytest.raises(PersonalAgentError):
        await authority.token(CONNECTION)
    current = await authority._record("connection", CONNECTION)
    assert current["status"] == "refresh_unknown" and current["auth_epoch"] > original["auth_epoch"]
    assert requests(fixture.endpoint, "/token") == []


async def test_stale_refresh_failure_does_not_invalidate_a_newer_token_generation(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    bundle = await authority._credential(original)
    newer = {**bundle, "access_token": "fixture-current-generation"}
    envelope = authority.vault.seal(
        owner_key=authority.owner_key,
        slot=original["key_slot"],
        purpose="calendar_tokens",
        value=newer,
    )
    current = await authority._write(
        lambda tx: authority._change(
            tx, "connection", CONNECTION, {"token_generation": 2, "credential_envelope": envelope}
        )
    )
    await authority._credential_failure(original, "reauth_required", "invalid_grant")
    assert await authority._record("connection", CONNECTION) == current
    assert await authority.token(CONNECTION) == "fixture-current-generation"
    assert fixture.endpoint.requests == []


@pytest.mark.parametrize("raw", [b"{not-json", b"\xff\xfe", b"[]"])
async def test_corrupt_runtime_marker_is_a_stable_storage_error_without_repair(
    connection_system, raw
):
    authority = connection_system.service
    await seed_connection(connection_system)
    authority.boundary.write_bytes(raw)
    await assert_error("storage_error", authority.context())
    assert authority.boundary.read_bytes() == raw


async def test_runtime_marker_read_oserror_has_stable_storage_identity(
    connection_system, monkeypatch
):
    authority = connection_system.service
    await seed_connection(connection_system)
    actual_read = type(authority.boundary).read_bytes

    def unreadable_original_marker(path):
        if path == authority.boundary:
            raise OSError("fixture marker read failure")
        return actual_read(path)

    monkeypatch.setattr(type(authority.boundary), "read_bytes", unreadable_original_marker)
    await assert_error("storage_error", authority.context())


async def test_local_disconnect_does_not_revoke_remote_and_same_request_is_read_only(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    payload = {
        "request_id": "disconnect-original",
        "expected_revision": original["revision"],
        "remote_revoke": False,
    }
    first = await authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL)
    replay = await authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL)
    assert first == replay and first["status"] == "disconnected"
    assert first["revoke_status"] == "not_requested" and first["grants"] == []
    assert requests(fixture.endpoint, "/revoke") == []
    await assert_error("connection_revoked", authority.token(CONNECTION))


async def test_remote_disconnect_same_request_has_atomic_once_send(connection_system):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    fixture.endpoint.revoke_release.clear()
    payload = {
        "request_id": "remote-original",
        "expected_revision": original["revision"],
        "remote_revoke": True,
    }
    first = asyncio.create_task(authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL))
    assert await asyncio.to_thread(fixture.endpoint.revoke_entered.wait, 5)
    duplicate = await asyncio.wait_for(
        asyncio.gather(
            authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL),
            return_exceptions=True,
        ),
        timeout=5,
    )
    assert len(requests(fixture.endpoint, "/revoke")) == 1
    fixture.endpoint.revoke_release.set()
    result = await first
    assert result["status"] == "disconnected"
    assert isinstance(duplicate[0], (dict, PersonalAgentError))
    await authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL)
    assert len(requests(fixture.endpoint, "/revoke")) == 1


async def test_reconnect_uses_new_key_generation_and_old_request_does_not_delete_it(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    old = await seed_connection(fixture)
    payload = {
        "request_id": "old-disconnect",
        "expected_revision": old["revision"],
        "remote_revoke": False,
    }
    await authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL)
    disconnected = await authority._record("connection", CONNECTION)
    intent = await authority.start_oauth(
        {
            "request_id": "reconnect-original",
            "write": False,
            "connection_id": CONNECTION,
            "expected_revision": disconnected["revision"],
        },
        trusted_channel=MANUAL,
    )
    await asyncio.wait_for(await oauth_callback(fixture, intent), timeout=10)
    current = await authority._record("connection", CONNECTION)
    assert current["status"] == "connected" and current["key_slot"] != old["key_slot"]
    await authority.disconnect(CONNECTION, payload, trusted_channel=MANUAL)
    assert await authority.token(CONNECTION) == "fixture-access-renewed"
    assert await authority._record("connection", CONNECTION) == current


async def test_grants_require_human_and_exact_live_specialist_version(connection_system):
    fixture, authority = connection_system, connection_system.service
    original = await seed_connection(fixture)
    actor = fixture.engine.personal_agents.create(
        name="Mira", instructions="只读核对日程", request_id="agent-original", tool_names=[]
    )
    payload = {
        "request_id": "grant-original",
        "expected_revision": original["revision"],
        "actor_id": actor.id,
        "actor_version": actor.version,
    }
    await assert_error(
        "scope_denied", authority.grant(CONNECTION, payload, trusted_channel="model_tool")
    )
    await authority.grant(CONNECTION, payload, trusted_channel=MANUAL)
    assert await authority.token(CONNECTION, actor.id) == "fixture-access-original"
    fixture.engine.personal_agents.update(actor.id, actor.version, instructions="更新后的只读要求")
    before = len(fixture.backend.calls)
    await assert_error("scope_denied", authority.token(CONNECTION, actor.id))
    assert len(fixture.backend.calls) == before
    assert fixture.endpoint.requests == []


async def upgrade_connection(fixture, *, request="write-upgrade", write=True):
    authority = fixture.service
    before = await authority._record("connection", CONNECTION)
    intent = await authority.start_oauth(
        {
            "request_id": request,
            "connection_id": CONNECTION,
            "expected_revision": before["revision"],
            "write": write,
        },
        trusted_channel=MANUAL,
    )
    await asyncio.wait_for(await oauth_callback(fixture, intent), timeout=10)
    return intent, await authority._record("connection", CONNECTION)


async def share_calendar(fixture, *, request="share-calendar", name="日历协作 Agent"):
    authority = fixture.service
    actor = fixture.engine.personal_agents.create(
        name=name, instructions="核对主日历并提出草稿", request_id=request, tool_names=[]
    )
    current = await authority._record("connection", CONNECTION)
    await authority.grant(
        CONNECTION,
        {
            "request_id": request + ":grant",
            "expected_revision": current["revision"],
            "actor_id": actor.id,
            "actor_version": actor.version,
        },
        trusted_channel=MANUAL,
    )
    return actor


async def test_healthy_write_upgrade_preserves_read_grants_cursor_sources_and_action_history(
    connection_system,
):
    from tests.unit.test_personal_calendar_sync import seed_cursor, seed_event

    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    actor = await share_calendar(fixture)
    revoked = await share_calendar(fixture, request="share-revoked", name="撤销分享 Agent")
    event = await seed_event(fixture)
    cursor = await seed_cursor(fixture)
    await authority._write(
        lambda tx: authority._change(
            tx,
            "connection",
            CONNECTION,
            {
                "sync_status": "ready",
                "last_sync_at": authority.stamp(),
                "sync_retry_count": 0,
                "next_sync_at": None,
                "sync_requested": False,
            },
        )
    )
    source = (await authority.events(CONNECTION, actor.id))["events"][0]["source_ref"]
    revoked_source = (await authority.events(CONNECTION, revoked.id))["events"][0]["source_ref"]
    current = await authority._record("connection", CONNECTION)
    await authority.grant(
        CONNECTION,
        {
            "request_id": "revoke-before-upgrade",
            "expected_revision": current["revision"],
            "actor_id": revoked.id,
        },
        trusted_channel=MANUAL,
        revoke=True,
    )
    draft = await authority.prepare_action(
        {
            "request_id": "ro-update-draft",
            "connection_id": CONNECTION,
            "actor_id": actor.id,
            "source_refs": [source],
            "operation": "update",
            "target_event_id": event["event_id"],
            "base_etag": event["event"]["etag"],
            "body": {
                "summary": "待人工批准的日程修改",
                "description": "写入升级不是这份草稿的批准。",
                "start": event["event"]["start"],
                "end": event["event"]["end"],
            },
            "expires_at": (authority.now() + timedelta(hours=1)).isoformat(),
        }
    )
    original_action = await authority._record("action", draft["id"])
    marker = {
        "action_id": draft["id"],
        "revision": draft["revision"],
        "digest": draft["digest"],
        "connection_id": CONNECTION,
        "auth_epoch": draft["auth_epoch"],
        "actor_id": actor.id,
        "grant_id": original_action["grant"]["id"],
        "grant_revision": original_action["grant"]["revision"],
    }
    await assert_error(
        "scope_denied",
        authority._read(lambda tx: authority._action_current(tx, original_action)),
    )
    revised = await authority.revise_action(
        draft["id"],
        {
            "request_id": "revise-before-upgrade",
            "expected_revision": draft["revision"],
            "body": {**draft["body"], "summary": "用户核对后的草稿"},
            "expires_at": draft["expires_at"],
        },
    )
    before = await authority._record("connection", CONNECTION)
    intent, after = await upgrade_connection(fixture)
    assert after["can_write"] is True
    assert after["auth_epoch"] == before["auth_epoch"]
    assert after["grants"] == before["grants"]
    assert after["token_generation"] == before["token_generation"] + 1
    assert after["key_slot"] != before["key_slot"]
    assert after["sync_status"] == before["sync_status"]
    assert after["last_sync_at"] == before["last_sync_at"]
    assert await authority._record("sync", CONNECTION) == cursor
    assert await authority.capture_source(source, actor.id) == source
    await assert_error("scope_denied", authority.capture_source(revoked_source, revoked.id))
    historical = await authority.authorize_action_reference(marker, actor.id)
    assert historical == original_action
    assert await authority.get_action(draft["id"]) == revised
    current_action = await authority._record("action", draft["id"])
    await authority._read(lambda tx: authority._action_current(tx, current_action))
    assert current_action["status"] == "draft" and current_action["approval"] is None
    assert current_action["execution"] is None
    assert (await authority._record("oauth", intent["intent_id"]))[
        "read_authority_preserved"
    ] is True
    # Replaying the completed OAuth request reads status, never opens another callback or write.
    await authority.start_oauth(
        {
            "request_id": "write-upgrade",
            "connection_id": CONNECTION,
            "expected_revision": before["revision"],
            "write": True,
        },
        trusted_channel=MANUAL,
    )
    assert len(requests(fixture.endpoint, "/token")) == 1
    assert not [r for r in fixture.endpoint.requests if r[0] in {"PATCH", "DELETE"}]
    assert all(r[0] == "GET" for r in fixture.endpoint.requests if "/calendar/" in r[1])


@pytest.mark.parametrize(
    "difference",
    [
        "email",
        "client",
        "disconnected",
        "reauth_required",
        "refresh_unknown",
        "credential_error",
        "same_ro",
        "same_rw",
        "downgrade",
        "mixed_old_scope",
        "known_error",
    ],
)
async def test_noncontinuous_reauthorization_fences_epoch_and_grants(connection_system, difference):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture, write=difference in {"same_rw", "downgrade"})
    await share_calendar(fixture)
    updates = {}
    if difference == "email":
        fixture.endpoint.email = "changed-email@example.invalid"
    elif difference == "client":
        client = config()
        client["installed"]["client_id"] = "new-client.apps.googleusercontent.com"
        fixture.endpoint.audience = client["installed"]["client_id"]
        await configure(authority, request="replacement-client", value=client)
    elif difference in {"disconnected", "reauth_required", "refresh_unknown", "credential_error"}:
        updates["status"] = difference
    elif difference == "mixed_old_scope":
        current = await authority._record("connection", CONNECTION)
        updates["scopes"] = [*current["scopes"], WRITE_SCOPE]
    elif difference == "known_error":
        updates["last_error_code"] = "access_denied"
    if updates:
        await authority._write(lambda tx: authority._change(tx, "connection", CONNECTION, updates))
    before = await authority._record("connection", CONNECTION)
    intent, after = await upgrade_connection(
        fixture, write=difference not in {"same_ro", "downgrade"}
    )
    assert after["status"] == "connected"
    assert after["auth_epoch"] == before["auth_epoch"] + 1
    assert after["grants"] == {}
    assert after["token_generation"] == before["token_generation"] + 1
    assert (await authority._record("oauth", intent["intent_id"]))[
        "read_authority_preserved"
    ] is False


@pytest.mark.parametrize("phase", ["before_callback", "during_exchange"])
async def test_upgrade_cas_never_resurrects_a_concurrently_revoked_grant(connection_system, phase):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    actor = await share_calendar(fixture)
    before = await authority._record("connection", CONNECTION)
    intent = await authority.start_oauth(
        {
            "request_id": "upgrade-revoke-race",
            "connection_id": CONNECTION,
            "expected_revision": before["revision"],
            "write": True,
        },
        trusted_channel=MANUAL,
    )
    if phase == "during_exchange":
        fixture.endpoint.token_release.clear()
        task = await oauth_callback(fixture, intent)
        assert await asyncio.to_thread(fixture.endpoint.token_entered.wait, 5)
    await authority.grant(
        CONNECTION,
        {
            "request_id": "revoke-while-oauth-pending",
            "expected_revision": before["revision"],
            "actor_id": actor.id,
        },
        trusted_channel=MANUAL,
        revoke=True,
    )
    revoked = await authority._record("connection", CONNECTION)
    if phase == "before_callback":
        task = await oauth_callback(fixture, intent)
    fixture.endpoint.token_release.set()
    await asyncio.wait_for(task, timeout=10)
    assert await authority._record("connection", CONNECTION) == revoked
    final = await authority.oauth_status(intent["intent_id"])
    assert final["status"] == "denied" and final["error_code"] == "version_conflict"
    assert len(requests(fixture.endpoint, "/token")) == (phase == "during_exchange")


async def test_second_oauth_from_the_old_revision_cannot_overwrite_continuous_upgrade(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    before = await seed_connection(fixture)
    intents = []
    for index in range(2):
        intents.append(
            await authority.start_oauth(
                {
                    "request_id": f"parallel-upgrade-{index}",
                    "connection_id": CONNECTION,
                    "expected_revision": before["revision"],
                    "write": True,
                },
                trusted_channel=MANUAL,
            )
        )
    await asyncio.wait_for(await oauth_callback(fixture, intents[0]), timeout=10)
    upgraded = await authority._record("connection", CONNECTION)
    await asyncio.wait_for(await oauth_callback(fixture, intents[1]), timeout=10)
    assert await authority._record("connection", CONNECTION) == upgraded
    assert upgraded["auth_epoch"] == before["auth_epoch"]
    assert upgraded["token_generation"] == before["token_generation"] + 1
    second = await authority.oauth_status(intents[1]["intent_id"])
    assert second["error_code"] == "version_conflict"
    assert len(requests(fixture.endpoint, "/token")) == 1


async def test_token_snapshot_refresh_binds_the_saved_generation_without_public_secret_repr(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    before = await seed_connection(fixture, expires=authority.now() - timedelta(seconds=1))
    token = await authority.token_snapshot(CONNECTION)
    after = await authority._record("connection", CONNECTION)
    assert token.connection == after
    assert token.connection["token_generation"] == before["token_generation"] + 1
    assert token.access_token == "fixture-access-renewed"
    assert "fixture-" not in repr(token)
    assert after["auth_epoch"] == before["auth_epoch"]


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_old_sync_http_failure_never_fences_or_poison_new_token_generation(
    connection_system,
    monkeypatch,
    status,
):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    acquired, release = asyncio.Event(), asyncio.Event()
    actual = authority.token_snapshot

    async def held_token(*args, **kwargs):
        token = await actual(*args, **kwargs)
        acquired.set()
        await release.wait()
        return token

    def calendar(_query, _path):
        sent_token = fixture.endpoint.requests[-1][2].get("Authorization")
        if sent_token == "Bearer fixture-access-original":
            return status, {"error": {"code": status, "errors": [{"reason": "forbidden"}]}}
        return 200, {"items": [], "nextSyncToken": "verified-new-generation"}

    monkeypatch.setattr(authority, "token_snapshot", held_token)
    fixture.endpoint.calendar = calendar
    task = asyncio.create_task(authority._run_sync(CONNECTION))
    await asyncio.wait_for(acquired.wait(), timeout=5)
    _, upgraded = await upgrade_connection(fixture)
    release.set()
    await asyncio.wait_for(task, timeout=10)
    assert await authority._record("connection", CONNECTION) == upgraded
    assert upgraded["status"] == "connected" and upgraded["auth_epoch"] == 1
    old_gets = [
        r
        for r in fixture.endpoint.requests
        if r[2].get("Authorization") == "Bearer fixture-access-original"
    ]
    assert len(old_gets) == 1 and old_gets[0][0] == "GET"


async def test_token_decryption_racing_upgrade_never_relabels_old_token_as_new_generation(
    connection_system,
    monkeypatch,
):
    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    opened, release = asyncio.Event(), asyncio.Event()
    actual = authority._credential

    async def held_credential(connection):
        bundle = await actual(connection)
        opened.set()
        await release.wait()
        return bundle

    monkeypatch.setattr(authority, "_credential", held_credential)
    task = asyncio.create_task(authority.token_snapshot(CONNECTION))
    await asyncio.wait_for(opened.wait(), timeout=5)
    _, upgraded = await upgrade_connection(fixture)
    release.set()
    await assert_error("version_conflict", task)
    assert await authority._record("connection", CONNECTION) == upgraded


async def test_replacement_grant_and_new_epoch_never_revive_old_source_or_action_reference(
    connection_system,
):
    from tests.unit.test_personal_calendar_sync import seed_event

    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    actor = await share_calendar(fixture)
    event = await seed_event(fixture)
    source = (await authority.events(CONNECTION, actor.id))["events"][0]["source_ref"]
    draft = await authority.prepare_action(
        {
            "request_id": "original-grant-draft",
            "connection_id": CONNECTION,
            "actor_id": actor.id,
            "source_refs": [],
            "operation": "insert",
            "body": {
                "summary": "原分享下的草稿",
                "description": "",
                "start": event["event"]["start"],
                "end": event["event"]["end"],
            },
            "expires_at": (authority.now() + timedelta(hours=1)).isoformat(),
        }
    )
    action = await authority._record("action", draft["id"])
    marker = {
        "action_id": draft["id"],
        "revision": draft["revision"],
        "digest": draft["digest"],
        "connection_id": CONNECTION,
        "auth_epoch": draft["auth_epoch"],
        "actor_id": actor.id,
        "grant_id": action["grant"]["id"],
        "grant_revision": action["grant"]["revision"],
    }
    for revoke in (True, False):
        current = await authority._record("connection", CONNECTION)
        await authority.grant(
            CONNECTION,
            {
                "request_id": f"replace-original-{revoke}",
                "expected_revision": current["revision"],
                "actor_id": actor.id,
                "actor_version": actor.version,
            },
            trusted_channel=MANUAL,
            revoke=revoke,
        )
    _, upgraded = await upgrade_connection(fixture)
    assert upgraded["auth_epoch"] == source["auth_epoch"]
    await assert_error("connection_source_revoked", authority.capture_source(source, actor.id))
    await assert_error(
        "approval_invalidated", authority.authorize_action_reference(marker, actor.id)
    )
    # A later real reauthorization fences even this continuous generation.
    _, reconnected = await upgrade_connection(fixture, request="actual-reauthorization")
    assert reconnected["auth_epoch"] == upgraded["auth_epoch"] + 1
    assert reconnected["grants"] == {}


@pytest.mark.parametrize("legacy_status", ["approved", "dispatching", "submitted", "unknown"])
async def test_current_ro_epoch_with_legacy_write_fact_never_preserves_authority(
    connection_system,
    legacy_status,
):
    from tests.unit.test_personal_calendar_sync import event

    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    before = await seed_connection(fixture)
    original = event()
    draft = await authority.prepare_action(
        {
            "request_id": "legacy-ro-draft",
            "connection_id": CONNECTION,
            "source_refs": [],
            "operation": "insert",
            "body": {
                "summary": "旧协议写事实",
                "description": "",
                "start": original["start"],
                "end": original["end"],
            },
            "expires_at": (authority.now() + timedelta(hours=1)).isoformat(),
        }
    )
    # Deliberately model a legacy/inconsistent persisted state, not a provider receipt.
    legacy = await authority._write(
        lambda tx: authority._change(tx, "action", draft["id"], {"status": legacy_status})
    )
    _, after = await upgrade_connection(fixture)
    assert after["auth_epoch"] == before["auth_epoch"] + 1
    assert after["grants"] == {}
    assert await authority._record("action", draft["id"]) == legacy
    assert all(r[0] == "GET" for r in fixture.endpoint.requests if "/calendar/" in r[1])


async def test_unknown_from_a_fenced_epoch_is_not_replayed_or_reauthorized_by_healthy_upgrade(
    connection_system,
):
    from tests.unit.test_personal_calendar_sync import event

    fixture, authority = connection_system, connection_system.service
    await configure(authority)
    await seed_connection(fixture)
    original = event()
    draft = await authority.prepare_action(
        {
            "request_id": "old-epoch-draft",
            "connection_id": CONNECTION,
            "source_refs": [],
            "operation": "insert",
            "body": {
                "summary": "保持原状态的行动",
                "description": "",
                "start": original["start"],
                "end": original["end"],
            },
            "expires_at": (authority.now() + timedelta(hours=1)).isoformat(),
        }
    )
    legacy = await authority._write(
        lambda tx: authority._change(tx, "action", draft["id"], {"status": "unknown"})
    )
    # Same-level reauthorization is a real new read epoch, not the continuous branch.
    _, ro = await upgrade_connection(fixture, request="recover-read", write=False)
    assert ro["auth_epoch"] == legacy["auth_epoch"] + 1
    _, rw = await upgrade_connection(fixture, request="recover-write", write=True)
    assert rw["auth_epoch"] == ro["auth_epoch"]
    assert await authority._record("action", draft["id"]) == legacy
    await assert_error(
        "approval_invalidated", authority._authorize_action(legacy, write=False, expiry=False)
    )
    assert len(requests(fixture.endpoint, "/token")) == 2
    assert all(r[0] == "GET" for r in fixture.endpoint.requests if "/calendar/" in r[1])
