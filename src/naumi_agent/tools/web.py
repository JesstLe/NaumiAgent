"""网络工具 — 搜索与网页抓取."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from markdownify import markdownify as md
from readability import Document

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    """使用搜索引擎搜索信息（Brave Search API 或 DuckDuckGo fallback）."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索网络信息，返回相关结果列表。"

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

        brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")

        if brave_api_key:
            return await self._brave_search(query, max_results, brave_api_key)
        return await self._ddg_search(query, max_results)

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
            return "No results found."
        return "\n---\n".join(results)

    async def _ddg_search(self, query: str, max_results: int) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()

        # 简单的 HTML 结果解析
        text = resp.text
        results = []
        import re

        # 提取搜索结果块
        blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)".*?>(.*?)</a>.*?'
            r'<a class="result__snippet".*?>(.*?)</a>',
            text,
            re.DOTALL,
        )
        for url, title, snippet in blocks[:max_results]:
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            clean_snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            results.append(f"### [{clean_title}]({url})\n{clean_snippet}\n")

        if not results:
            return "No results found. Consider setting BRAVE_SEARCH_API_KEY for better results."
        return "\n---\n".join(results)


class WebFetchTool(Tool):
    """抓取网页内容并转为 Markdown."""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "抓取指定 URL 的网页内容，提取正文并转为 Markdown 格式。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 URL"},
                "max_length": {
                    "type": "integer",
                    "description": "最大返回字符数，默认 30000",
                    "default": 30000,
                },
            },
            "required": ["url"],
        }

    async def execute(self, *, url: str, max_length: int = 30000, **kwargs: Any) -> str:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; NaumiAgent/0.1)"},
            ) as client:
                resp = await client.get(url, timeout=15)
                resp.raise_for_status()

            # 提取正文
            doc = Document(resp.text)
            title = doc.title()
            content = md(doc.summary())

            result = f"# {title}\n\n{content}"

            if len(result) > max_length:
                result = result[:max_length] + f"\n\n... (truncated, {len(result)} total chars)"

            return result
        except httpx.HTTPStatusError as e:
            return f"HTTP Error {e.response.status_code} fetching {url}"
        except Exception as e:
            return f"Error fetching {url}: {type(e).__name__}: {e}"


def create_web_tools() -> list[Tool]:
    return [WebSearchTool(), WebFetchTool()]
