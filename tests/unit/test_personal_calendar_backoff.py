"""Actual Retry-After transport and durable read-only synchronization backoff."""

from __future__ import annotations

import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from naumi_agent.personal.google_calendar import GoogleCalendarAdapter
from naumi_agent.personal.google_oauth import GoogleProtocolError, _http_error
from tests.unit.test_personal_connection_service import CONNECTION, MANUAL, seed_connection
from tests.unit.test_personal_connection_service import calendar_endpoint as _calendar_endpoint
from tests.unit.test_personal_connection_service import connection_system as _connection_system
from tests.unit.test_personal_connection_service import signing_material as _signing_material
from tests.unit.test_personal_connection_service import system as _system
from tests.unit.test_personal_google_oauth import Forward

system = _system
signing_material = _signing_material
calendar_endpoint = _calendar_endpoint
connection_system = _connection_system


@pytest.mark.asyncio
async def test_real_http_retry_after_is_carried_without_repeating_request():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'
            self.send_response(429)
            self.send_header("Retry-After", "120")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        async with httpx.AsyncClient(transport=Forward(base), trust_env=False) as http:
            with pytest.raises(GoogleProtocolError) as caught:
                await GoogleCalendarAdapter(http_client=http).list_events("fixture-access")
        assert caught.value.code == "rate_limited"
        assert caught.value.retry_after_seconds == 120
        assert caught.value.retryable and not caught.value.unknown
        assert len(requests) == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("header,expected", [("invalid", None), ("99999999", 86400), ("0", 0)])
def test_retry_after_is_bounded_and_never_echoed(header, expected):
    error = _http_error(429, {}, False, {"Retry-After": header})
    assert error.retry_after_seconds == expected
    assert str(error) == "rate_limited"


@pytest.mark.asyncio
async def test_retry_deadline_survives_store_reopen_and_manual_retry(connection_system):
    f = connection_system
    await seed_connection(f)
    await f.service._sync_failure(CONNECTION, "rate_limited", 1, retryable=True, delay=600)
    current = await f.service._record("connection", CONNECTION)
    deadline = f.service._parse_sync_time(current["next_sync_at"])
    assert deadline >= f.service.now() + timedelta(seconds=600)
    count = len(f.endpoint.requests)
    await f.service.sync(
        CONNECTION,
        {"request_id": "manual-read-retry", "expected_revision": current["revision"]},
        trusted_channel=MANUAL,
    )
    await f.service._run_sync(CONNECTION)
    assert len(f.endpoint.requests) == count
    # A fresh authority reads the committed deadline; it is not an in-memory sleep.
    reopened = type(f.service)(
        f.engine,
        store=f.service.store,
        vault=f.service.vault,
        adapter=f.service.adapter,
        oauth_factory=f.service.oauth_factory,
        clock=f.service.clock,
    )
    await reopened._run_sync(CONNECTION)
    assert len(f.endpoint.requests) == count
    f.clock[0] = deadline + timedelta(seconds=1)
    await reopened._run_sync(CONNECTION)
    final = await reopened._record("connection", CONNECTION)
    assert final["sync_status"] == "ready"
    assert final["next_sync_at"] is None and final["sync_retry_count"] == 0
    assert len(f.endpoint.requests) > count
