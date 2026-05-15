"""网络工具测试."""

from naumi_agent.tools.web import WebFetchTool, WebSearchTool, create_web_tools


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

    def test_fetch_tool_schema(self) -> None:
        tool = WebFetchTool()
        schema = tool.schema
        assert schema.name == "web_fetch"
        assert "url" in schema.parameters["properties"]
