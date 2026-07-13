"""网络工具 — 搜索与网页抓取."""

from __future__ import annotations

import asyncio
import html as html_mod
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse, urlunparse

import httpx
from markdownify import markdownify as md
from readability import Document

from naumi_agent.tools.base import Tool, ToolMetadata

if TYPE_CHECKING:
    from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10
MAX_FETCH_CHARS = 120_000
DEFAULT_FETCH_CHARS = 30_000
BLOCKED_HOSTNAMES = {"localhost"}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local")
SEARCH_PROVIDER_NAMES = {
    "brave": "Brave",
    "duckduckgo": "DuckDuckGo",
    "browser": "浏览器搜索",
}


class SearchStatus(StrEnum):
    """搜索提供方返回的标准状态。"""

    SUCCESS = "success"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SearchItem:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    status: SearchStatus
    provider: str
    items: tuple[SearchItem, ...] = ()
    code: str = ""
    message: str = ""


class WebToolInputError(ValueError):
    """Raised when user-provided web tool input is unsafe or invalid."""


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _normalize_public_http_url(raw_url: str) -> str:
    """Validate and normalize a user-provided public HTTP(S) URL."""
    url = raw_url.strip()
    if not url:
        raise WebToolInputError("URL 不能为空。")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise WebToolInputError("仅允许抓取 http 或 https URL。")
    if not parsed.hostname:
        raise WebToolInputError("URL 缺少有效主机名。")
    if parsed.username or parsed.password:
        raise WebToolInputError("URL 不允许包含用户名或密码。")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise WebToolInputError("已阻止本机或本地域名，避免 SSRF 风险。")

    try:
        ip = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise WebToolInputError("已阻止内网、本机或保留 IP，避免 SSRF 风险。")

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


class WebSearchTool(Tool):
    """固定路由搜索，并在直连搜索不可用时至多回退一次浏览器。"""

    def __init__(self, browser_runtime: BrowserRuntime | None = None) -> None:
        self._browser_runtime = browser_runtime

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索网络信息；无需 API Key，必要时会自动回退到浏览器搜索。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=self._browser_runtime is None,
            user_facing_name="网络搜索",
            search_hint="web search internet brave duckduckgo current information",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, *, query: str, max_results: int = 5, **kwargs: Any) -> str:
        query = query.strip()
        if not query:
            return "搜索失败：query 不能为空。"
        safe_max_results = _clamp_int(
            max_results,
            default=5,
            minimum=1,
            maximum=MAX_SEARCH_RESULTS,
        )
        attempts: list[SearchOutcome] = []
        brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if brave_api_key:
            attempts.append(await self._brave_search(query, safe_max_results, brave_api_key))
            if attempts[-1].status is SearchStatus.SUCCESS:
                return self._format_success(attempts[-1], attempts)

        attempts.append(await self._ddg_search(query, safe_max_results))
        if attempts[-1].status is SearchStatus.SUCCESS:
            return self._format_success(attempts[-1], attempts)

        if self._browser_runtime is not None:
            attempts.append(await self._browser_search(query, safe_max_results))
            if attempts[-1].status is SearchStatus.SUCCESS:
                return self._format_success(attempts[-1], attempts)

        return self._format_failure(attempts)

    async def _brave_search(self, query: str, max_results: int, api_key: str) -> SearchOutcome:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": max_results},
                    headers={
                        "X-Subscription-Token": api_key,
                        "Accept": "application/json",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()

            items = tuple(
                SearchItem(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("url", "")).strip(),
                    snippet=str(item.get("description", "")).strip(),
                )
                for item in data.get("web", {}).get("results", [])[:max_results]
                if item.get("title") and item.get("url")
            )
            return SearchOutcome(
                status=SearchStatus.SUCCESS if items else SearchStatus.EMPTY,
                provider="brave",
                items=items,
                code="no_results" if not items else "",
            )
        except httpx.HTTPStatusError as exc:
            code = (
                "authentication"
                if exc.response.status_code in {401, 403}
                else f"http_{exc.response.status_code}"
            )
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="brave",
                code=code,
                message="Brave Search API unavailable",
            )
        except Exception as exc:
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="brave",
                code=type(exc).__name__,
                message=str(exc),
            )

    async def _ddg_search(self, query: str, max_results: int) -> SearchOutcome:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://lite.duckduckgo.com/lite/",
                    data={"q": query, "kl": "wt-wt"},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; NaumiAgent/1.0)"},
                    timeout=15,
                )
                resp.raise_for_status()

            link_pattern = re.compile(
                r'<a[^>]*href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>',
                re.DOTALL,
            )
            snippet_pattern = re.compile(r"class=['\"]result-snippet['\"]>(.*?)</td>", re.DOTALL)

            links = link_pattern.findall(resp.text)
            snippets = snippet_pattern.findall(resp.text)

            items: list[SearchItem] = []
            for index, (url, raw_title) in enumerate(links[:max_results]):
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", raw_title).strip())
                snippet = ""
                if index < len(snippets):
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[index]).strip())
                normalized_url = self._normalize_ddg_url(url)
                if title and normalized_url:
                    items.append(SearchItem(title, normalized_url, snippet))

            return SearchOutcome(
                status=SearchStatus.SUCCESS if items else SearchStatus.EMPTY,
                provider="duckduckgo",
                items=tuple(items),
                code="no_results" if not items else "",
            )
        except httpx.HTTPStatusError as exc:
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="duckduckgo",
                code=f"http_{exc.response.status_code}",
                message="DuckDuckGo Lite unavailable",
            )
        except Exception as exc:
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="duckduckgo",
                code=type(exc).__name__,
                message=str(exc),
            )

    @staticmethod
    def _format_success(outcome: SearchOutcome, attempts: list[SearchOutcome]) -> str:
        provider_name = SEARCH_PROVIDER_NAMES.get(outcome.provider, outcome.provider)
        lines = [f"搜索来源：{provider_name}"]
        if len(attempts) > 1:
            previous = "、".join(
                SEARCH_PROVIDER_NAMES.get(item.provider, item.provider) for item in attempts[:-1]
            )
            lines.append(f"已自动回退：{previous} 不可用。")
        rendered = [
            f"### [{item.title}]({item.url})\n{item.snippet}".rstrip() for item in outcome.items
        ]
        return "\n\n".join(lines) + "\n\n" + "\n\n---\n\n".join(rendered)

    @staticmethod
    def _format_failure(attempts: list[SearchOutcome]) -> str:
        names = "、".join(
            SEARCH_PROVIDER_NAMES.get(item.provider, item.provider) for item in attempts
        )
        details = "；".join(
            f"{SEARCH_PROVIDER_NAMES.get(item.provider, item.provider)}: "
            f"{item.code or item.status.value}"
            for item in attempts
        )
        return (
            f"搜索失败：已尝试 {names or '可用搜索方式'}，当前均不可用。"
            f"\n诊断：{details or '未配置可用搜索方式'}。"
            "\n请勿在本轮重复调用 web_search；可以稍后重试，或配置 "
            "BRAVE_SEARCH_API_KEY 提升搜索稳定性。"
        )

    @staticmethod
    def _normalize_ddg_url(raw_url: str) -> str:
        url = html_mod.unescape(raw_url).strip()
        if url.startswith("//"):
            url = "https:" + url
        parsed = urlparse(url)
        redirected = parse_qs(parsed.query).get("uddg")
        if redirected:
            url = unquote(redirected[0])
            parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return url

    async def _browser_search(self, query: str, max_results: int) -> SearchOutcome:
        runtime = self._browser_runtime
        if runtime is None:
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="browser",
                code="not_configured",
            )

        started_here = not runtime.is_running()

        async def run() -> SearchOutcome:
            if started_here:
                await runtime.start({"source": "managed", "headless": True})
            search_url = f"https://www.bing.com/search?q={quote_plus(query)}"
            await runtime.goto(search_url)
            expression = f"""
                return Array.from(document.querySelectorAll('li.b_algo'))
                    .slice(0, {max_results})
                    .map((item) => {{
                        const link = item.querySelector('h2 a');
                        const snippet = item.querySelector('.b_caption p');
                        return {{
                            title: link?.textContent?.trim() || '',
                            url: link?.href || '',
                            snippet: snippet?.textContent?.trim() || ''
                        }};
                    }});
            """
            evaluated = await runtime.evaluate(expression)
            if evaluated.get("isError"):
                return SearchOutcome(
                    status=SearchStatus.FAILED,
                    provider="browser",
                    code="evaluate",
                    message=str(evaluated.get("result", "")),
                )
            raw_items = json.loads(str(evaluated.get("result", "[]")))
            seen_urls: set[str] = set()
            items: list[SearchItem] = []
            for item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                url = str(item.get("url", "")).strip()
                snippet = str(item.get("snippet", "")).strip()
                parsed = urlparse(url)
                if (
                    title
                    and parsed.scheme in {"http", "https"}
                    and parsed.netloc
                    and url not in seen_urls
                ):
                    seen_urls.add(url)
                    items.append(SearchItem(title, url, snippet))
                if len(items) >= max_results:
                    break
            return SearchOutcome(
                status=SearchStatus.SUCCESS if items else SearchStatus.EMPTY,
                provider="browser",
                items=tuple(items),
                code="no_results" if not items else "",
            )

        try:
            timeout = float(os.environ.get("NAUMI_BROWSER_SEARCH_TIMEOUT", "30"))
            return await asyncio.wait_for(run(), timeout=max(5.0, min(timeout, 60.0)))
        except Exception as exc:
            return SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="browser",
                code=type(exc).__name__,
                message=str(exc),
            )
        finally:
            if started_here:
                try:
                    await runtime.stop()
                except Exception:
                    logger.warning("browser search cleanup failed", exc_info=True)


class WebFetchTool(Tool):
    """抓取网页内容并转为 Markdown."""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "抓取指定 URL 的网页内容，提取正文并转为 Markdown 格式。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="网页抓取",
            search_hint="web fetch url html markdown readability public http https",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 URL"},
                "max_length": {
                    "type": "integer",
                    "description": "最大返回字符数，默认 30000，上限 120000",
                    "default": DEFAULT_FETCH_CHARS,
                },
            },
            "required": ["url"],
        }

    async def execute(
        self, *, url: str, max_length: int = DEFAULT_FETCH_CHARS, **kwargs: Any
    ) -> str:
        try:
            safe_url = _normalize_public_http_url(url)
            safe_max_length = _clamp_int(
                max_length,
                default=DEFAULT_FETCH_CHARS,
                minimum=1_000,
                maximum=MAX_FETCH_CHARS,
            )
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; NaumiAgent/0.1)"},
            ) as client:
                resp = await client.get(safe_url, timeout=15)
                resp.raise_for_status()

            final_url = _normalize_public_http_url(str(resp.url))
            content_type = resp.headers.get("content-type", "").lower()
            if content_type and not (
                "text/html" in content_type
                or "text/plain" in content_type
                or "application/xhtml+xml" in content_type
            ):
                return f"抓取失败：不支持的内容类型 `{content_type}`。"

            # 提取正文
            doc = Document(resp.text)
            title = doc.title()
            content = md(doc.summary())

            result = f"# {title}\n\n来源: {final_url}\n\n{content}"

            if len(result) > safe_max_length:
                result = (
                    result[:safe_max_length] + f"\n\n...（已截断，原始长度 {len(result)} 字符）"
                )

            return result
        except WebToolInputError as e:
            return f"URL 校验失败: {e}"
        except httpx.HTTPStatusError as e:
            return f"抓取失败：HTTP {e.response.status_code}，URL: {url}"
        except Exception as e:
            return f"抓取失败: {type(e).__name__}: {e}"


def create_web_tools(browser_runtime: BrowserRuntime | None = None) -> list[Tool]:
    return [WebSearchTool(browser_runtime), WebFetchTool()]
