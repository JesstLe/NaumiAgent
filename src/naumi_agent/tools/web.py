"""网络工具 — 搜索与网页抓取."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from markdownify import markdownify as md
from readability import Document

from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10
MAX_FETCH_CHARS = 120_000
DEFAULT_FETCH_CHARS = 30_000
BLOCKED_HOSTNAMES = {"localhost"}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local")


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
    """使用搜索引擎搜索信息（Brave Search API 或 DuckDuckGo fallback）."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索网络信息，返回相关结果列表。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
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
        import os

        query = query.strip()
        if not query:
            return "搜索失败：query 不能为空。"
        safe_max_results = _clamp_int(
            max_results,
            default=5,
            minimum=1,
            maximum=MAX_SEARCH_RESULTS,
        )
        brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")

        if brave_api_key:
            return await self._brave_search(query, safe_max_results, brave_api_key)
        return await self._ddg_search(query, safe_max_results)

    async def _brave_search(self, query: str, max_results: int, api_key: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            results.append(
                f"### [{item.get('title', '')}]({item.get('url', '')})\n"
                f"{item.get('description', '')}\n"
            )

        if not results:
            return "未找到搜索结果。"
        return "\n---\n".join(results)

    async def _ddg_search(self, query: str, max_results: int) -> str:
        import html as html_mod
        import re

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://lite.duckduckgo.com/lite/",
                    data={"q": query, "kl": "wt-wt"},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; NaumiAgent/1.0)"},
                    timeout=15,
                )
                resp.raise_for_status()

            page = resp.text

            link_pattern = re.compile(
                r'<a[^>]*href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>',
                re.DOTALL,
            )
            snippet_pattern = re.compile(
                r"class=['\"]result-snippet['\"]>(.*?)</td>", re.DOTALL
            )

            links = link_pattern.findall(page)
            snippets = snippet_pattern.findall(page)

            results = []
            for i, (url, raw_title) in enumerate(links[:max_results]):
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", raw_title).strip())
                snippet = ""
                if i < len(snippets):
                    snippet = html_mod.unescape(
                        re.sub(r"<[^>]+>", "", snippets[i]).strip()
                    )
                results.append(f"### [{title}]({url})\n{snippet}\n")

            if not results:
                return "未找到搜索结果。可设置 BRAVE_SEARCH_API_KEY 提升搜索质量。"
            return "\n---\n".join(results)
        except httpx.HTTPStatusError as e:
            return (
                f"搜索失败（HTTP {e.response.status_code}）。"
                "可设置 BRAVE_SEARCH_API_KEY 提升稳定性。"
            )
        except Exception as e:
            return f"搜索失败: {type(e).__name__}: {e}"


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
                    result[:safe_max_length]
                    + f"\n\n...（已截断，原始长度 {len(result)} 字符）"
                )

            return result
        except WebToolInputError as e:
            return f"URL 校验失败: {e}"
        except httpx.HTTPStatusError as e:
            return f"抓取失败：HTTP {e.response.status_code}，URL: {url}"
        except Exception as e:
            return f"抓取失败: {type(e).__name__}: {e}"


def create_web_tools() -> list[Tool]:
    return [WebSearchTool(), WebFetchTool()]
