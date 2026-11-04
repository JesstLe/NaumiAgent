from __future__ import annotations

import copy
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from naumi_agent.personal.google_calendar import (
    BASE_URI,
    GoogleCalendarAdapter,
    approved_event_projection,
    event_matches_approval,
    normalize_event,
    occurrence_key,
)
from naumi_agent.personal.google_oauth import OPERATION_MARKER, GoogleProtocolError


def event():
    return {
        "id": "a" * 32,
        "status": "confirmed",
        "etag": '"fixture-etag"',
        "summary": "准备材料",
        "description": "外部材料，不是执行授权",
        "location": "会议室",
        "start": {"dateTime": "2026-11-06T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-11-06T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "htmlLink": "https://calendar.google.com/calendar/event?eid=fixture",
        "extendedProperties": {"private": {OPERATION_MARKER: "fixture-action-1"}},
    }


def approved():
    raw = event()
    return {
        key: raw[key]
        for key in ("summary", "description", "location", "start", "end", "extendedProperties")
    }


@pytest.fixture
def endpoint():
    state = SimpleNamespace(
        requests=[],
        status=200,
        body={"items": [], "nextSyncToken": "fixture-sync"},
        reset=False,
        headers={},
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.command, self.path, dict(self.headers), raw))
            if state.reset:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            body = state.body if isinstance(state.body, bytes) else json.dumps(state.body).encode()
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_PATCH(self):
            self.handle_request()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


class Forward(httpx.AsyncBaseTransport):
    def __init__(self, base):
        self.base = base
        self.urls = []
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        return await self.inner.handle_async_request(
            httpx.Request(
                request.method,
                httpx.URL(self.base).copy_with(raw_path=request.url.raw_path),
                headers=request.headers,
                content=await request.aread(),
            )
        )

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.asyncio
async def test_real_http_list_sync_instances_get_insert_and_etag_update(endpoint):
    transport = Forward(endpoint.base)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as http:
        adapter = GoogleCalendarAdapter(http_client=http)
        assert (
            await adapter.list_events("fixture-access", page_token="page-2", sync_token="sync-1")
        )["nextSyncToken"] == "fixture-sync"
        query = parse_qs(urlsplit(endpoint.requests[-1][1]).query)
        assert query == {
            "singleEvents": ["false"],
            "showDeleted": ["true"],
            "maxResults": ["250"],
            "pageToken": ["page-2"],
            "syncToken": ["sync-1"],
        }
        await adapter.instances(
            "fixture-access",
            "series_id",
            page_token="page-3",
            time_min="2026-11-01T00:00:00Z",
            time_max="2026-12-01T00:00:00Z",
        )
        assert urlsplit(endpoint.requests[-1][1]).path.endswith("/series_id/instances")
        assert parse_qs(urlsplit(endpoint.requests[-1][1]).query)["timeMin"] == [
            "2026-11-01T00:00:00.000000Z"
        ]
        endpoint.body = event()
        assert (await adapter.get_event("fixture-access", event()["id"]))["id"] == event()["id"]
        await adapter.insert_event("fixture-access", event()["id"], approved())
        method, path, headers, body = endpoint.requests[-1]
        assert method == "POST" and parse_qs(urlsplit(path).query) == {"sendUpdates": ["none"]}
        assert (
            json.loads(body)["id"] == event()["id"]
            and headers["Authorization"] == "Bearer fixture-access"
        )
        await adapter.update_event(
            "fixture-access", event()["id"], approved(), etag='"fixture-etag"'
        )
        assert (
            endpoint.requests[-1][0] == "PATCH"
            and endpoint.requests[-1][2]["If-Match"] == '"fixture-etag"'
        )
        assert len(endpoint.requests) == 5
        assert all(url.startswith(BASE_URI) for url in transport.urls)


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (401, None, "authentication_required"),
        (403, "insufficientPermissions", "scope_denied"),
        (403, "forbidden", "access_denied"),
        (403, "rateLimitExceeded", "quota_exceeded"),
        (404, None, "not_found"),
        (409, None, "conflict"),
        (410, None, "sync_token_expired"),
        (412, None, "precondition_failed"),
        (429, None, "rate_limited"),
        (503, None, "service_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_real_http_error_classification_sanitized(endpoint, status, reason, expected):
    endpoint.status = status
    endpoint.body = {
        "error": {"message": "fixture-secret-not-for-errors", "errors": [{"reason": reason}]}
    }
    async with httpx.AsyncClient(transport=Forward(endpoint.base)) as http:
        with pytest.raises(GoogleProtocolError) as caught:
            await GoogleCalendarAdapter(http_client=http).list_events("fixture-access")
    assert caught.value.code == expected and caught.value.status == status
    assert not caught.value.unknown and "secret" not in str(caught.value)
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("case", ["reset", "redirect", "503", "oversize", "invalid_json"])
@pytest.mark.asyncio
async def test_write_uncertainty_is_unknown_and_single_dispatch(endpoint, case):
    if case == "reset":
        endpoint.reset = True
    elif case == "redirect":
        endpoint.status = 307
        endpoint.headers = {"Location": endpoint.base + "/do-not-follow"}
    elif case == "503":
        endpoint.status = 503
    elif case == "oversize":
        endpoint.body = b"x" * (2 * 1024 * 1024 + 1)
    else:
        endpoint.body = b'{"id":1,"id":2}'
    async with httpx.AsyncClient(transport=Forward(endpoint.base), follow_redirects=True) as http:
        with pytest.raises(GoogleProtocolError) as caught:
            await GoogleCalendarAdapter(http_client=http).insert_event(
                "fixture-access", event()["id"], approved()
            )
    assert caught.value.unknown
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("invalid", ["../escape", "event%2felsewhere", "", "a b", "x" * 1025])
@pytest.mark.asyncio
async def test_invalid_target_is_rejected_before_http(endpoint, invalid):
    async with httpx.AsyncClient(transport=Forward(endpoint.base)) as http:
        with pytest.raises(GoogleProtocolError):
            await GoogleCalendarAdapter(http_client=http).get_event("fixture-access", invalid)
    assert endpoint.requests == []


def test_normalization_idempotence_occurrence_not_title_or_icaluid():
    raw = event()
    raw.update(
        recurringEventId="series-1",
        originalStartTime={"dateTime": "2026-11-06T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        iCalUID="shared@fixture",
    )
    first = occurrence_key(raw)
    moved = copy.deepcopy(raw)
    moved["start"]["dateTime"] = "2026-11-06T11:00:00+08:00"
    moved["summary"] = "新标题"
    assert occurrence_key(moved) == first
    other = copy.deepcopy(raw)
    other["originalStartTime"]["dateTime"] = "2026-11-07T09:00:00+08:00"
    assert occurrence_key(other) != first
    normalized = normalize_event(raw)
    assert normalized["start"]["dateTime"] == "2026-11-06T01:00:00.000000Z"
    assert normalize_event(normalized) == normalized
    assert occurrence_key(normalized) == first
    assert normalize_event({"id": "gone", "status": "cancelled"})["start"] is None


def test_all_day_dates_keep_type_and_original_date():
    raw = event()
    raw["start"] = {"date": "2026-11-06"}
    raw["end"] = {"date": "2026-11-07"}
    assert normalize_event(raw)["start"] == {"date": "2026-11-06"}
    assert event_matches_approval(
        raw,
        {"start": raw["start"], "end": raw["end"], "extendedProperties": raw["extendedProperties"]},
        operation_marker="fixture-action-1",
    )
    raw["start"] = {"dateTime": "2026-11-06T00:00:00Z"}
    assert not event_matches_approval(
        raw,
        {"start": {"date": "2026-11-06"}, "extendedProperties": raw["extendedProperties"]},
        operation_marker="fixture-action-1",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", "changed"),
        ("description", "changed"),
        ("start", {"dateTime": "2026-11-06T02:00:00Z"}),
        ("status", "cancelled"),
        ("attendees", [{"email": "extra@example.invalid"}]),
        ("extendedProperties", {"private": {OPERATION_MARKER: "other-action"}}),
    ],
)
def test_readback_exact_approved_projection_and_marker(field, value):
    raw = event()
    expected = approved()
    assert event_matches_approval(raw, expected, operation_marker="fixture-action-1")
    raw[field] = value
    assert not event_matches_approval(raw, expected, operation_marker="fixture-action-1")


def test_readback_accepts_provider_defaults_offsets_and_timezone_canonicalization():
    raw = event()
    raw["start"] = {"dateTime": "2026-11-06T01:00:00Z", "timeZone": "Etc/UTC"}
    raw["end"] = {"dateTime": "2026-11-06T02:00:00Z", "timeZone": "UTC"}
    raw.update(
        created="2026-11-05T00:00:00Z", organizer={"self": True}, reminders={"useDefault": True}
    )
    assert event_matches_approval(raw, approved(), operation_marker="fixture-action-1")
    assert approved_event_projection(approved())["start"]["timeZone"] == "Asia/Shanghai"


@pytest.mark.parametrize(
    "bad",
    [
        {"start": {"dateTime": "2026-11-06T09:00:00"}},
        {"start": {"date": "2026-02-30"}},
        {"start": {"date": "2026-11-06", "dateTime": "2026-11-06T00:00:00Z"}},
        {"start": {"dateTime": "2026-11-06T00:00:00Z", "timeZone": "No/Such_Zone"}},
        {"attendees": [{"email": "extra@example.invalid"}]},
    ],
)
def test_approved_projection_rejects_ambiguous_or_unapproved_fields(bad):
    with pytest.raises(GoogleProtocolError):
        approved_event_projection(bad)


@pytest.mark.parametrize(
    "case", ["marker", "zero_interval", "mixed_dates", "unicode", "visibility"]
)
@pytest.mark.asyncio
async def test_invalid_write_is_rejected_before_dispatch(endpoint, case):
    body = approved()
    if case == "marker":
        del body["extendedProperties"]
    elif case == "zero_interval":
        body["end"] = copy.deepcopy(body["start"])
    elif case == "mixed_dates":
        body["end"] = {"date": "2026-11-07"}
    elif case == "unicode":
        body["summary"] = "bad\ud800"
    else:
        body["visibility"] = "unrecognized"
    async with httpx.AsyncClient(transport=Forward(endpoint.base)) as http:
        with pytest.raises(GoogleProtocolError):
            await GoogleCalendarAdapter(http_client=http).insert_event(
                "fixture-access", event()["id"], body
            )
    assert endpoint.requests == []


def test_provider_omitted_default_fields_match_approved_defaults():
    body = {**approved(), "transparency": "opaque", "visibility": "default"}
    assert event_matches_approval(event(), body, operation_marker="fixture-action-1")


def test_all_day_optional_timezone_is_preserved_without_utc_day_conversion():
    raw = event()
    raw["start"] = {"date": "2026-11-06", "timeZone": "Pacific/Kiritimati"}
    raw["end"] = {"date": "2026-11-07", "timeZone": "Pacific/Kiritimati"}
    normalized = normalize_event(raw)
    assert normalized["start"] == raw["start"]
    assert normalize_event(normalized) == normalized
    assert (
        approved_event_projection({"start": raw["start"], "end": raw["end"]})["start"]
        == raw["start"]
    )


@pytest.mark.parametrize("bad_status", [None, {}, [], 4])
def test_malformed_provider_status_is_typed(bad_status):
    raw = event()
    raw["status"] = bad_status
    with pytest.raises(GoogleProtocolError, match="invalid_event"):
        normalize_event(raw)
