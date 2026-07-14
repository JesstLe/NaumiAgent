"""网络工具测试."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from naumi_agent.config.settings import SearchConfig
from naumi_agent.tools.web import (
    SearchItem,
    SearchOutcome,
    SearchStatus,
    WebFetchTool,
    WebSearchTool,
    _clamp_int,
    _normalize_public_http_url,
    create_web_tools,
)


class TestWebTools:
    def test_create_web_tools(self) -> None:
        tools = create_web_tools()
        assert len(tools) == 2
        assert tools[0].name == "web_search"
        assert tools[1].name == "web_fetch"

    def test_search_tool_schema(self) -> None:
        tool = WebSearchTool()
        schema = tool.schema
        assert schema.name == "web_search"
        assert "query" in schema.parameters["properties"]
        assert tool.metadata.read_only is True
        assert tool.metadata.concurrency_safe is True

    def test_fetch_tool_schema(self) -> None:
        tool = WebFetchTool()
        schema = tool.schema
        assert schema.name == "web_fetch"
        assert "url" in schema.parameters["properties"]
        assert tool.metadata.read_only is True
        assert tool.metadata.concurrency_safe is True

    def test_clamp_int_bounds_tool_limits(self) -> None:
        assert _clamp_int("9", default=5, minimum=1, maximum=10) == 9
        assert _clamp_int("999", default=5, minimum=1, maximum=10) == 10
        assert _clamp_int("bad", default=5, minimum=1, maximum=10) == 5

    def test_normalize_public_http_url_accepts_public_https(self) -> None:
        assert _normalize_public_http_url("https://example.com") == "https://example.com/"
        assert (
            _normalize_public_http_url("http://example.com/path?q=1#frag")
            == "http://example.com/path?q=1"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://localhost/admin",
            "http://127.0.0.1:8000",
            "http://10.0.0.5",
            "http://169.254.169.254/latest/meta-data",
            "http://example.local/status",
            "https://user:pass@example.com/",
        ],
    )
    def test_normalize_public_http_url_blocks_unsafe_targets(self, url: str) -> None:
        with pytest.raises(ValueError):
            _normalize_public_http_url(url)

    @pytest.mark.asyncio
    async def test_fetch_rejects_unsafe_url_before_network(self) -> None:
        output = await WebFetchTool().execute(url="http://127.0.0.1:8000")

        assert "URL 校验失败" in output
        assert "SSRF" in output

    @pytest.mark.asyncio
    async def test_search_rejects_empty_query(self) -> None:
        output = await WebSearchTool().execute(query="  ")

        assert "query 不能为空" in output

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["x" * 401, " ".join(["word"] * 51)])
    async def test_search_rejects_queries_beyond_brave_contract(self, query: str) -> None:
        output = await WebSearchTool().execute(query=query)

        assert "最多 400 个字符和 50 个词" in output

    @pytest.mark.asyncio
    async def test_search_without_key_uses_keyless_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        tool = WebSearchTool()
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.SUCCESS,
                provider="duckduckgo",
                items=(SearchItem("结果", "https://example.com", "摘要"),),
            )
        )

        output = await tool.execute(query="naumi agent")

        assert "搜索来源：DuckDuckGo" in output
        assert "[结果](https://example.com)" in output

    @pytest.mark.asyncio
    async def test_invalid_brave_key_falls_back_to_keyless_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "invalid")
        tool = WebSearchTool()
        tool._brave_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="brave",
                code="authentication",
                message="Brave credentials rejected",
            )
        )
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.SUCCESS,
                provider="duckduckgo",
                items=(SearchItem("结果", "https://example.com", ""),),
            )
        )

        output = await tool.execute(query="naumi agent")

        assert "搜索来源：DuckDuckGo" in output
        assert "已自动回退" in output
        tool._brave_search.assert_awaited_once()
        tool._ddg_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_uses_custom_brave_environment_reference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NAUMI_CUSTOM_BRAVE_KEY", "custom-secret")
        config = SearchConfig(
            provider_order=("brave",),
            brave={"api_key_ref": "{env:NAUMI_CUSTOM_BRAVE_KEY}"},
        )
        tool = WebSearchTool(search_config=config)
        tool._brave_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.SUCCESS,
                provider="brave",
                items=(SearchItem("Brave 结果", "https://example.com", ""),),
            )
        )

        output = await tool.execute(query="naumi")

        assert "搜索来源：Brave" in output
        tool._brave_search.assert_awaited_once_with("naumi", 5, "custom-secret")

    @pytest.mark.asyncio
    async def test_search_skips_disabled_brave_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "configured-secret")
        config = SearchConfig(brave={"enabled": False})
        tool = WebSearchTool(search_config=config)
        tool._brave_search = AsyncMock()
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.SUCCESS,
                provider="duckduckgo",
                items=(SearchItem("结果", "https://example.com", ""),),
            )
        )

        output = await tool.execute(query="naumi")

        assert "搜索来源：DuckDuckGo" in output
        tool._brave_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_brave_request_uses_validated_advanced_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "web": {
                        "results": [{
                            "title": "结果",
                            "url": "https://example.com",
                            "description": "摘要",
                        }]
                    }
                }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **kwargs):
                captured.update({"url": url, **kwargs})
                return _Response()

        monkeypatch.setattr("naumi_agent.tools.web.httpx.AsyncClient", _Client)
        config = SearchConfig(brave={
            "country": "CN",
            "search_lang": "zh-hans",
            "ui_lang": "zh-CN",
            "safesearch": "strict",
            "spellcheck": False,
            "freshness": "pw",
            "timeout_seconds": 12,
        })
        tool = WebSearchTool(search_config=config)

        outcome = await tool._brave_search("naumi", 5, "secret")

        assert outcome.status is SearchStatus.SUCCESS
        assert captured["params"] == {
            "q": "naumi",
            "count": 5,
            "country": "CN",
            "search_lang": "zh-hans",
            "ui_lang": "zh-CN",
            "safesearch": "strict",
            "spellcheck": False,
            "freshness": "pw",
        }
        assert captured["timeout"] == 12
        assert captured["headers"] == {
            "X-Subscription-Token": "secret",
            "Accept": "application/json",
        }

    @pytest.mark.asyncio
    async def test_direct_search_failure_uses_browser_once_and_closes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        runtime = AsyncMock()
        runtime.is_running = Mock(return_value=False)
        runtime.evaluate.return_value = {
            "isError": False,
            "result": json.dumps(
                [
                    {
                        "title": "Browser result",
                        "url": "https://example.com/browser",
                        "snippet": "Browser snippet",
                    }
                ]
            ),
        }
        tool = WebSearchTool(runtime)
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="duckduckgo",
                code="timeout",
                message="timed out",
            )
        )

        output = await tool.execute(query="naumi agent")

        assert "搜索来源：浏览器搜索" in output
        runtime.start.assert_awaited_once_with({"source": "managed", "headless": True})
        runtime.goto.assert_awaited_once()
        runtime.evaluate.assert_awaited_once()
        runtime.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_route_exhaustion_does_not_retry_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        runtime = AsyncMock()
        runtime.is_running = Mock(return_value=False)
        runtime.goto.side_effect = TimeoutError("browser timeout")
        tool = WebSearchTool(runtime)
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.FAILED,
                provider="duckduckgo",
                code="parser",
                message="parser failed",
            )
        )

        output = await tool.execute(query="naumi agent")

        assert "搜索失败" in output
        assert "DuckDuckGo、浏览器搜索" in output
        assert "请勿在本轮重复调用 web_search" in output
        runtime.goto.assert_awaited_once()
        runtime.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_browser_fallback_preserves_existing_browser_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        runtime = AsyncMock()
        runtime.is_running = Mock(return_value=True)
        runtime.evaluate.return_value = {
            "isError": False,
            "result": json.dumps(
                [{"title": "结果", "url": "https://example.com", "snippet": ""}]
            ),
        }
        tool = WebSearchTool(runtime)
        tool._ddg_search = AsyncMock(
            return_value=SearchOutcome(
                status=SearchStatus.UNAVAILABLE,
                provider="duckduckgo",
                code="timeout",
            )
        )

        output = await tool.execute(query="naumi agent")

        assert "搜索来源：浏览器搜索" in output
        assert tool.metadata.concurrency_safe is False
        runtime.start.assert_not_awaited()
        runtime.stop.assert_not_awaited()
