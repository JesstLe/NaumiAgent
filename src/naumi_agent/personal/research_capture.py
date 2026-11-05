"""Structured, bounded public-web captures with pinned address/TLS authority."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress
import posixpath
import re
import socket
import unicodedata
from datetime import UTC, datetime
from email.message import Message
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from lxml import html
from markdownify import markdownify
from readability import Document

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.web import MAX_SEARCH_RESULTS, SearchOutcome, SearchStatus, WebSearchTool

MAX_CAPTURE_BYTES = 1024 * 1024
MAX_CAPTURE_CHARS = 120000
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CHARSETS = frozenset(
    {
        "utf-8",
        "utf-8-sig",
        "ascii",
        "iso8859-1",
        "cp1252",
        "gb18030",
        "gbk",
        "big5",
        "shift_jis",
        "euc_jp",
        "euc_kr",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
    }
)
_MESSAGES = {
    "invalid_input": "研究来源地址或抓取参数格式不正确",
    "source_unavailable": "研究来源当前不可读取，请核对原地址或选择其他来源",
    "source_fetch_failed": "研究来源读取未完成，请稍后核对原请求",
}


def _error(code):
    return PersonalAgentError(code, _MESSAGES[code])


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _error("source_unavailable") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise _error("source_unavailable")
    return address


def _public_url(value):
    if type(value) is not str or not value.strip() or len(value) > 8192:
        raise _error("invalid_input")
    value = value.strip()
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise _error("invalid_input")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if (
            not host
            or "%" in host
            or host == "localhost"
            or host.endswith((".localhost", ".local"))
        ):
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if any(
                not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9_-]+", label)
                for label in host.split(".")
            ):
                raise ValueError() from None
        else:
            _public_address(str(address))
            host = address.compressed
        authority = f"[{host}]" if ":" in host else host
        default_port = 443 if parsed.scheme == "https" else 80
        if port is not None and port != default_port:
            authority += f":{port}"
        return str(
            httpx.URL(
                urlunsplit(
                    (parsed.scheme, authority, parsed.path or "/", parsed.query, ""),
                )
            )
        )
    except PersonalAgentError:
        raise _error("invalid_input") from None
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise _error("invalid_input") from None


async def _resolve_public(host, port):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, UnicodeError):
            raise _error("source_fetch_failed") from None
        addresses = [str(_public_address(record[4][0])) for record in records]
    else:
        addresses = [str(_public_address(str(literal)))]
    if not addresses:
        raise _error("source_unavailable")
    return tuple(dict.fromkeys(addresses))


def _make_client(timeout):
    # A new client per hop prevents cookies and same-IP TLS pools crossing origins.
    return httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
        verify=True,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


def _canonical_text(value):
    value = unicodedata.normalize("NFC", value.removeprefix("\ufeff"))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 32 and char not in "\t\n" or ord(char) == 127 for char in value):
        raise _error("source_unavailable")
    return value.strip()


def _decode(raw, content_type):
    header = Message()
    header["content-type"] = content_type
    charset = header.get_content_charset() or "utf-8"
    try:
        charset = codecs.lookup(charset).name
        if charset not in _CHARSETS:
            raise ValueError()
        return raw.decode(charset, errors="strict")
    except (LookupError, UnicodeError, ValueError):
        raise _error("source_unavailable") from None


def _extract(raw, content_type, final_url):
    decoded = _decode(raw, content_type)
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "text/plain":
        title = posixpath.basename(urlsplit(final_url).path) or urlsplit(final_url).hostname
        return _canonical_text(title)[:500], _canonical_text(decoded), "plain-text-v1"
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise _error("source_unavailable")
    try:
        document = html.document_fromstring(decoded, parser=html.HTMLParser(no_network=True))
        for element in document.xpath(
            "//script|//style|//iframe|//object|//embed|//form|//input|//button|//meta|//link"
            "|//svg|//math|//video|//audio|//img"
        ):
            element.drop_tree()
        for element in document.iter():
            for name in tuple(element.attrib):
                if name.lower().startswith("on") or name.lower() in {"src", "srcdoc", "action"}:
                    element.attrib.pop(name, None)
            if "href" in element.attrib:
                target = urljoin(final_url, element.attrib["href"])
                try:
                    target = _public_url(target)
                except PersonalAgentError:
                    element.attrib.pop("href", None)
                else:
                    element.attrib["href"] = target
        safe_html = html.tostring(document, encoding="unicode")
        readable = Document(safe_html)
        title = _canonical_text(readable.short_title())[:500]
        body = markdownify(readable.summary(html_partial=True), heading_style="ATX")
        # Markdown must not carry executable HTML into a later report renderer.
        body = body.replace("<", "&lt;").replace(">", "&gt;")
        return title, _canonical_text(body), "readability-markdown-v1"
    except PersonalAgentError:
        raise
    except Exception:
        raise _error("source_unavailable") from None


async def capture_url(
    url,
    *,
    max_bytes=MAX_CAPTURE_BYTES,
    max_chars=MAX_CAPTURE_CHARS,
    timeout=20,
    max_redirects=4,
):
    """Capture exact returned bytes and canonical text, never trust supplied body metadata."""
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_CAPTURE_BYTES
        or type(max_chars) is not int
        or not 1 <= max_chars <= MAX_CAPTURE_CHARS
        or type(timeout) not in {int, float}
        or not 0 < timeout <= 60
        or type(max_redirects) is not int
        or not 0 <= max_redirects <= 4
    ):
        raise _error("invalid_input")
    requested_url = current = _public_url(url)
    redirects, visited = [], {current}
    try:
        async with asyncio.timeout(timeout):
            while True:
                parsed = urlsplit(current)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                addresses = await _resolve_public(parsed.hostname, port)
                if not addresses:
                    raise _error("source_unavailable")
                # Revalidate injected results; transport never resolves this hostname again.
                pinned = str(_public_address(addresses[0]))
                for address in addresses:
                    _public_address(address)
                authority = f"[{pinned}]" if ":" in pinned else pinned
                authority += f":{port}"
                pinned_url = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))
                async with _make_client(timeout) as client:
                    async with client.stream(
                        "GET",
                        pinned_url,
                        headers={
                            "Host": parsed.netloc,
                            "Accept-Encoding": "identity",
                            "User-Agent": "NaumiAgent/ResearchCapture",
                        },
                        extensions={"sni_hostname": parsed.hostname},
                    ) as response:
                        if response.status_code in _REDIRECTS:
                            location = response.headers.get("location")
                            if not location or len(redirects) >= max_redirects:
                                raise _error("source_unavailable")
                            try:
                                following = _public_url(urljoin(current, location))
                            except PersonalAgentError:
                                raise _error("source_unavailable") from None
                            if following in visited:
                                raise _error("source_unavailable")
                            visited.add(following)
                            redirects.append(following)
                            current = following
                            continue
                        if not 200 <= response.status_code < 300:
                            raise _error("source_unavailable")
                        content_type = response.headers.get("content-type", "")
                        if len(content_type) > 512:
                            raise _error("source_unavailable")
                        if content_type.split(";", 1)[0].strip().lower() not in {
                            "text/plain",
                            "text/html",
                            "application/xhtml+xml",
                        }:
                            raise _error("source_unavailable")
                        encoding = response.headers.get("content-encoding", "identity").lower()
                        if encoding != "identity":
                            raise _error("source_unavailable")
                        length = response.headers.get("content-length")
                        if length is not None and (
                            len(length) > 20
                            or not length.isascii()
                            or not length.isdecimal()
                            or int(length) > max_bytes
                        ):
                            raise _error("source_unavailable")
                        chunks, size = [], 0
                        byte_stream = response.aiter_bytes(chunk_size=min(max_bytes + 1, 65536))
                        async for chunk in byte_stream:
                            size += len(chunk)
                            if size > max_bytes:
                                raise _error("source_unavailable")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        status = response.status_code
                if not raw:
                    raise _error("source_unavailable")
                title, complete_body, extractor = await asyncio.to_thread(
                    _extract,
                    raw,
                    content_type,
                    current,
                )
                if not complete_body:
                    raise _error("source_unavailable")
                body = complete_body[:max_chars]
                return {
                    "requested_url": requested_url,
                    "final_url": current,
                    "title": title,
                    "body": body,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "http_status": status,
                    "captured_at": datetime.now(UTC).isoformat(timespec="microseconds"),
                    "truncated": len(body) < len(complete_body),
                    "content_type": content_type,
                    "extractor": extractor,
                    "redirects": redirects,
                }
    except PersonalAgentError:
        raise
    except (httpx.HTTPError, OSError, TimeoutError):
        raise _error("source_fetch_failed") from None


def _discovery_result(query, outcome, attempts):
    items = []
    candidates = (
        outcome.items[:MAX_SEARCH_RESULTS] if outcome.status is SearchStatus.SUCCESS else ()
    )
    for item in candidates:
        try:
            if type(item.title) is not str or type(item.snippet) is not str:
                continue
            title = _canonical_text(item.title)[:500]
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "url": _public_url(item.url),
                    "snippet": _canonical_text(item.snippet)[:4000],
                }
            )
        except PersonalAgentError:
            continue
    status, code = outcome.status.value, outcome.code
    if status == "success" and not items:
        status, code = "empty", "no_public_results"
    if type(code) is not str or not re.fullmatch(r"[a-zA-Z0-9_.:-]{0,80}", code):
        code = "provider_failed"
    return {
        "query": query,
        "provider": outcome.provider,
        "status": status,
        "code": code,
        "items": items,
        "observed_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "attempts": attempts,
    }


async def discover(search_tool, query, max_results=5):
    """Reuse configured structured search providers; snippets never become captured evidence."""
    if (
        not isinstance(search_tool, WebSearchTool)
        or type(query) is not str
        or not query.strip()
        or len(query) > 400
        or len(query.split()) > 50
        or any(ord(char) < 32 or ord(char) == 127 for char in query)
        or type(max_results) is not int
        or not 1 <= max_results <= MAX_SEARCH_RESULTS
    ):
        raise _error("invalid_input")
    query = query.strip()
    attempts = []
    last = SearchOutcome(SearchStatus.UNAVAILABLE, "none", code="search_unavailable")
    for provider in search_tool._search_config.provider_order:
        outcome = None
        try:
            if provider == "brave":
                key = search_tool._search_config.brave.resolve_api_key()
                if key:
                    outcome = await search_tool._brave_search(query, max_results, key)
            elif provider == "duckduckgo":
                outcome = await search_tool._ddg_search(query, max_results)
            elif provider == "browser" and search_tool._browser_runtime is not None:
                outcome = await search_tool._browser_search(query, max_results)
        except Exception:
            outcome = SearchOutcome(SearchStatus.UNAVAILABLE, provider, code="provider_failed")
        if outcome is None:
            outcome = SearchOutcome(SearchStatus.UNAVAILABLE, provider, code="not_configured")
        if (
            not isinstance(outcome, SearchOutcome)
            or outcome.provider != provider
            or not isinstance(outcome.status, SearchStatus)
        ):
            outcome = SearchOutcome(SearchStatus.UNAVAILABLE, provider, code="provider_failed")
        result = _discovery_result(query, outcome, attempts)
        result["items"] = result["items"][:max_results]
        attempts.append({"provider": provider, "status": result["status"], "code": result["code"]})
        if result["status"] == "success":
            return result
        last = SearchOutcome(SearchStatus(result["status"]), provider, code=result["code"])
    return _discovery_result(query, last, attempts)
