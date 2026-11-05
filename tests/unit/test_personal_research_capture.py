"""Public research captures pin transport authority and preserve exact source bytes."""

from __future__ import annotations

import asyncio
import hashlib
import socket
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from naumi_agent.config.settings import SearchConfig
from naumi_agent.personal import research_capture as capture
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.web import SearchItem, SearchOutcome, SearchStatus, WebSearchTool

PUBLIC_IP = "8.8.8.8"
OTHER_IP = "1.1.1.1"
URL = "https://docs.example/research"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *pieces):
        self.pieces, self.consumed, self.closed = pieces, 0, False

    async def __aiter__(self):
        for piece in self.pieces:
            self.consumed += 1
            yield piece

    async def aclose(self):
        self.closed = True


def _network(monkeypatch, handler, *, addresses=(PUBLIC_IP,)):
    requests, clients, resolutions = [], [], []

    async def resolve(host, port):
        resolutions.append((host, port))
        return addresses

    def client(timeout):
        async def dispatch(request):
            requests.append(request)
            return handler(request)

        value = httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
        )
        clients.append(value)
        return value

    monkeypatch.setattr(capture, "_resolve_public", resolve)
    monkeypatch.setattr(capture, "_make_client", client)
    return requests, clients, resolutions


def _plain(raw=b"actual source", *, status=200, headers=None):
    return httpx.Response(
        status,
        headers={"content-type": "text/plain; charset=utf-8", **(headers or {})},
        stream=Chunks(raw),
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        1,
        "file:///etc/passwd",
        "https://user:secret@docs.example/",
        "http://localhost/",
        "http://nested.localhost/",
        "https://internal.local/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "https://docs.example:0/",
        "https://docs.example:65536/",
        "https://docs.example\\@127.0.0.1/",
        "https://docs.example/\r\nsecret",
    ],
)
async def test_invalid_or_nonpublic_input_stops_before_resolution(monkeypatch, url):
    resolve = AsyncMock(side_effect=AssertionError("No DNS for rejected input"))
    monkeypatch.setattr(capture, "_resolve_public", resolve)
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(url)
    assert error.value.code == "invalid_input"
    resolve.assert_not_awaited()


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": True},
        {"max_bytes": 0},
        {"max_bytes": capture.MAX_CAPTURE_BYTES + 1},
        {"max_chars": False},
        {"max_chars": 0},
        {"max_chars": capture.MAX_CAPTURE_CHARS + 1},
        {"timeout": True},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": 61},
        {"max_redirects": True},
        {"max_redirects": -1},
        {"max_redirects": 5},
    ],
)
async def test_invalid_bounds_are_rejected_before_network(monkeypatch, options):
    resolve = AsyncMock(side_effect=AssertionError("No DNS for bad bounds"))
    monkeypatch.setattr(capture, "_resolve_public", resolve)
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL, **options)
    assert error.value.code == "invalid_input"
    resolve.assert_not_awaited()


@pytest.mark.parametrize(
    "bad_ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "fe80::1",
        "224.0.0.1",
        "0.0.0.0",
        "not-an-address",
    ],
)
async def test_all_dns_answers_must_be_public(monkeypatch, bad_ip):
    def answers(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (bad_ip, 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", answers)
    with pytest.raises(PersonalAgentError) as error:
        await capture._resolve_public("docs.example", 443)
    assert error.value.code == "source_unavailable"


async def test_empty_dns_and_resolution_failure_have_fixed_errors(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(PersonalAgentError) as empty:
        await capture._resolve_public("docs.example", 443)
    assert empty.value.code == "source_unavailable"

    def failure(*_args, **_kwargs):
        raise OSError("private DNS diagnostic secret")

    monkeypatch.setattr(socket, "getaddrinfo", failure)
    with pytest.raises(PersonalAgentError) as error:
        await capture._resolve_public("docs.example", 443)
    assert error.value.code == "source_fetch_failed"
    assert "secret" not in error.value.message


async def test_capture_pins_ip_host_and_tls_name_without_second_hostname_resolution(monkeypatch):
    raw = "\ufeffCafe\u0301\r\nactual source\r\n".encode()
    requests, clients, resolutions = _network(monkeypatch, lambda _request: _plain(raw))
    result = await capture.capture_url(URL + "?q=proof#ignored")
    assert resolutions == [("docs.example", 443)]
    assert len(requests) == len(clients) == 1 and clients[0].is_closed
    request = requests[0]
    assert request.url.host == PUBLIC_IP and request.url.path == "/research"
    assert request.url.query == b"q=proof"
    assert request.headers["host"] == "docs.example"
    assert request.headers["accept-encoding"] == "identity"
    assert request.extensions["sni_hostname"] == "docs.example"
    assert result["requested_url"] == result["final_url"] == URL + "?q=proof"
    assert result["body"] == "Café\nactual source"
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["body_sha256"] == hashlib.sha256(result["body"].encode()).hexdigest()
    assert result["http_status"] == 200 and result["redirects"] == []
    assert result["extractor"] == "plain-text-v1" and result["truncated"] is False
    assert datetime.fromisoformat(result["captured_at"]).utcoffset().total_seconds() == 0


async def test_injected_address_is_revalidated_before_transport(monkeypatch):
    requests, _, _ = _network(
        monkeypatch, lambda _request: _plain(), addresses=(PUBLIC_IP, "127.0.0.1")
    )
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_unavailable" and requests == []


async def test_default_client_keeps_certificate_verification_and_disables_env_proxy():
    async with capture._make_client(3) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client._transport._pool._ssl_context.check_hostname is True
        assert client._transport._pool._max_keepalive_connections == 0


async def test_redirects_validate_hosts_and_do_not_reuse_tls_or_cookies(monkeypatch):
    def handler(request):
        if len(requests) == 1:
            return _plain(
                status=302,
                headers={
                    "location": "https://other.example/final#part",
                    "set-cookie": "private_cookie=must-not-cross",
                },
            )
        assert "cookie" not in request.headers
        return _plain(b"final source")

    requests, clients, resolutions = _network(monkeypatch, handler)
    result = await capture.capture_url(URL)
    assert resolutions == [("docs.example", 443), ("other.example", 443)]
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(client.is_closed for client in clients)
    assert [request.extensions["sni_hostname"] for request in requests] == [
        "docs.example",
        "other.example",
    ]
    assert [request.headers["host"] for request in requests] == ["docs.example", "other.example"]
    assert result["redirects"] == ["https://other.example/final"]
    assert result["final_url"] == "https://other.example/final"


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/private",
        "http://[::ffff:10.0.0.1]/",
        "file:///etc/passwd",
        "https://user:secret@other.example/",
        URL,
        "",
    ],
)
async def test_unsafe_or_cyclic_redirect_is_rejected_before_second_request(monkeypatch, location):
    requests, _, resolutions = _network(
        monkeypatch,
        lambda _request: _plain(status=302, headers={"location": location}),
    )
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_unavailable"
    assert len(requests) == len(resolutions) == 1


async def test_redirect_dns_rebinding_target_is_blocked_before_following(monkeypatch):
    requests, _, _ = _network(
        monkeypatch,
        lambda _request: _plain(
            status=302,
            headers={"location": "https://other.example/"},
        ),
    )

    async def resolve(host, _port):
        return (PUBLIC_IP,) if host == "docs.example" else ("::ffff:127.0.0.1",)

    monkeypatch.setattr(capture, "_resolve_public", resolve)
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_unavailable" and len(requests) == 1


async def test_redirect_limit_bounds_total_requests(monkeypatch):
    requests, clients, _ = _network(
        monkeypatch,
        lambda _request: _plain(
            status=302,
            headers={"location": "/next-" + str(len(requests))},
        ),
    )
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL, max_redirects=2)
    assert error.value.code == "source_unavailable"
    assert len(requests) == 3 and all(client.is_closed for client in clients)


async def test_stream_byte_limit_stops_without_reading_whole_document(monkeypatch):
    stream = Chunks(b"1234", b"5678", b"9abc", b"never-read")
    _network(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=stream,
        ),
    )
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL, max_bytes=8)
    assert error.value.code == "source_unavailable"
    assert stream.consumed == 3 and stream.closed


@pytest.mark.parametrize("length", ["999", "-1", "unknown", "9.0"])
async def test_bad_or_excessive_content_length_is_rejected_before_body(monkeypatch, length):
    stream = Chunks(b"not read")
    _network(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-length": length},
            stream=stream,
        ),
    )
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL, max_bytes=8)
    assert error.value.code == "source_unavailable"
    assert stream.consumed == 0 and stream.closed


async def test_exact_byte_limit_and_character_truncation_keep_distinct_hashes(monkeypatch):
    raw = "第一行\n第二行".encode()
    _network(monkeypatch, lambda _request: _plain(raw))
    result = await capture.capture_url(URL, max_bytes=len(raw), max_chars=3)
    assert result["body"] == "第一行" and result["truncated"] is True
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["body_sha256"] == hashlib.sha256("第一行".encode()).hexdigest()


@pytest.mark.parametrize(
    "raw,content_type,expected",
    [
        (b"caf\xe9", "text/plain; charset=windows-1252", "café"),
        ("中文".encode("gb18030"), "text/plain; charset=gb18030", "中文"),
        ("source".encode("utf-16"), "text/plain; charset=utf-16", "source"),
    ],
)
async def test_declared_bounded_text_charset_is_strictly_decoded(
    monkeypatch,
    raw,
    content_type,
    expected,
):
    _network(monkeypatch, lambda _request: _plain(raw, headers={"content-type": content_type}))
    assert (await capture.capture_url(URL))["body"] == expected


@pytest.mark.parametrize(
    "raw,headers",
    [
        (b"\xff", {"content-type": "text/plain; charset=utf-8"}),
        (b"+ADw-script+AD4-", {"content-type": "text/plain; charset=utf-7"}),
        (b"binary", {"content-type": "application/pdf"}),
        (b"gzip", {"content-encoding": "gzip"}),
        (b"bad\0source", {}),
        (b"", {}),
    ],
)
async def test_unsupported_or_invalid_source_does_not_become_evidence(monkeypatch, raw, headers):
    _network(monkeypatch, lambda _request: _plain(raw, headers=headers))
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_unavailable"


async def test_html_extraction_removes_active_content_and_keeps_readable_evidence(monkeypatch):
    raw = b"""<!doctype html><html><head><title>Actual report</title></head><body>
    <article><h1>Actual report</h1><p>The actual supported metric is 25 percent for this period.
    This source paragraph remains available for a precise quotation and a reproducible capture.</p>
    <script>NEVER_EXECUTE_PRIVATE_SCRIPT</script><iframe src='https://evil.example/'></iframe>
    <img src='https://evil.example/private'><a href='javascript:alert(1)'>Unsafe link</a>
    <a href='/details'>Details</a></article></body></html>"""
    _network(monkeypatch, lambda _request: _plain(raw, headers={"content-type": "text/html"}))
    result = await capture.capture_url(URL)
    assert result["extractor"] == "readability-markdown-v1"
    assert "25 percent" in result["body"] and "Actual report" in result["title"]
    for forbidden in ("<script", "<iframe", "<img", "javascript:", "NEVER_EXECUTE_PRIVATE_SCRIPT"):
        assert forbidden not in result["body"]
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("status", [304, 400, 401, 403, 404, 410, 500, 503])
async def test_unsuccessful_http_status_has_no_success_capture(monkeypatch, status):
    _network(monkeypatch, lambda _request: _plain(status=status))
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_unavailable"


async def test_transport_exception_and_whole_operation_timeout_are_sanitized(monkeypatch):
    def failed(_request):
        raise httpx.ConnectError("private diagnostic API_KEY=secret")

    _network(monkeypatch, failed)
    with pytest.raises(PersonalAgentError) as error:
        await capture.capture_url(URL)
    assert error.value.code == "source_fetch_failed" and "secret" not in error.value.message

    async def blocked(_host, _port):
        await asyncio.Event().wait()

    monkeypatch.setattr(capture, "_resolve_public", blocked)
    with pytest.raises(PersonalAgentError) as timeout:
        await capture.capture_url(URL, timeout=0.001)
    assert timeout.value.code == "source_fetch_failed"


async def test_cancelled_capture_propagates_without_fabricating_fetch_failure(monkeypatch):
    started = asyncio.Event()

    async def blocked(_host, _port):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(capture, "_resolve_public", blocked)
    task = asyncio.create_task(capture.capture_url(URL))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _search(monkeypatch, *, browser=True, key=None, order=("brave", "duckduckgo", "browser")):
    tool = WebSearchTool(
        object() if browser else None,
        search_config=SearchConfig(provider_order=order),
    )
    monkeypatch.setattr(type(tool._search_config.brave), "resolve_api_key", lambda _self: key)
    return tool


async def test_discovery_uses_structured_providers_without_pretty_parse(monkeypatch):
    tool = _search(monkeypatch, key="fixture-brave-key")
    order = []

    async def brave(query, count, key):
        order.append(("brave", query, count, key))
        return SearchOutcome(SearchStatus.UNAVAILABLE, "brave", code="http_503")

    async def ddg(query, count):
        order.append(("duckduckgo", query, count))
        return SearchOutcome(
            SearchStatus.SUCCESS,
            "duckduckgo",
            items=(
                SearchItem(
                    "Actual result",
                    "https://docs.example/evidence#paragraph",
                    "Discovery only",
                ),
            ),
        )

    monkeypatch.setattr(tool, "_brave_search", brave)
    monkeypatch.setattr(tool, "_ddg_search", ddg)
    monkeypatch.setattr(
        tool,
        "_browser_search",
        AsyncMock(
            side_effect=AssertionError("No fallback"),
        ),
    )
    monkeypatch.setattr(
        tool,
        "execute",
        AsyncMock(
            side_effect=AssertionError("Never parse Markdown"),
        ),
    )
    result = await capture.discover(tool, "  actual public research  ", max_results=2)
    assert [row[0] for row in order] == ["brave", "duckduckgo"]
    assert result["query"] == "actual public research" and result["provider"] == "duckduckgo"
    assert result["status"] == "success" and result["code"] == ""
    assert result["items"] == [
        {
            "title": "Actual result",
            "url": "https://docs.example/evidence",
            "snippet": "Discovery only",
        }
    ]
    assert [row["provider"] for row in result["attempts"]] == ["brave", "duckduckgo"]
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    tool.execute.assert_not_awaited()


async def test_discovery_falls_back_to_browser_once_with_bounded_diagnostics(monkeypatch):
    tool = _search(monkeypatch)
    ddg = AsyncMock(return_value=SearchOutcome(SearchStatus.EMPTY, "duckduckgo", code="no_results"))
    browser = AsyncMock(
        return_value=SearchOutcome(
            SearchStatus.SUCCESS,
            "browser",
            items=(SearchItem("Browser result", "https://docs.example/", "text"),),
        )
    )
    monkeypatch.setattr(tool, "_ddg_search", ddg)
    monkeypatch.setattr(tool, "_browser_search", browser)
    result = await capture.discover(tool, "source")
    assert result["provider"] == "browser" and result["status"] == "success"
    ddg.assert_awaited_once_with("source", 5)
    browser.assert_awaited_once_with("source", 5)
    assert result["attempts"][0]["code"] == "not_configured"


async def test_discovery_does_not_elevate_invalid_urls_or_failed_provider_items(monkeypatch):
    tool = _search(monkeypatch, browser=False, order=("duckduckgo",))
    monkeypatch.setattr(
        tool,
        "_ddg_search",
        AsyncMock(
            return_value=SearchOutcome(
                SearchStatus.SUCCESS,
                "duckduckgo",
                items=(
                    SearchItem("Private", "http://127.0.0.1/private", "not a source"),
                    SearchItem("Active", "javascript:alert(1)", "not a source"),
                ),
            )
        ),
    )
    result = await capture.discover(tool, "query")
    assert result["status"] == "empty" and result["items"] == []
    assert result["code"] == "no_public_results"
    monkeypatch.setattr(
        tool,
        "_ddg_search",
        AsyncMock(
            side_effect=ValueError("secret API_KEY=value"),
        ),
    )
    result = await capture.discover(tool, "query")
    assert result["status"] == "unavailable" and result["code"] == "provider_failed"
    assert "secret" not in str(result) and "API_KEY" not in str(result)


async def test_discovery_bounds_exported_results_even_if_provider_returns_too_many(monkeypatch):
    tool = _search(monkeypatch, order=("duckduckgo",))
    monkeypatch.setattr(
        tool,
        "_ddg_search",
        AsyncMock(
            return_value=SearchOutcome(
                SearchStatus.SUCCESS,
                "duckduckgo",
                items=tuple(
                    SearchItem("Title" * 200, f"https://docs.example/{index}", "x" * 5000)
                    for index in range(20)
                ),
            )
        ),
    )
    result = await capture.discover(tool, "query", max_results=2)
    assert len(result["items"]) == 2
    assert all(len(row["title"]) <= 500 and len(row["snippet"]) <= 4000 for row in result["items"])


@pytest.mark.parametrize(
    "query,count",
    [
        ("", 5),
        (None, 5),
        ("x" * 401, 5),
        ("query\0secret", 5),
        ("query", True),
        ("query", 0),
        ("query", 11),
    ],
)
async def test_discovery_rejects_invalid_inputs_without_provider(monkeypatch, query, count):
    tool = _search(monkeypatch)
    called = AsyncMock(side_effect=AssertionError("No provider for bad input"))
    monkeypatch.setattr(tool, "_ddg_search", called)
    with pytest.raises(PersonalAgentError) as error:
        await capture.discover(tool, query, count)
    assert error.value.code == "invalid_input"
    called.assert_not_awaited()
