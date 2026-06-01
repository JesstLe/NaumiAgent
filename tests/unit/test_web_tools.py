"""网络工具测试."""

import pytest

from naumi_agent.tools.web import (
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
