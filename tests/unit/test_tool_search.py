"""Tool search tests."""

from __future__ import annotations

import asyncio

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.search import ToolSearchTool, search_registered_tools


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in create_builtin_tools():
        reg.register(tool)
    search_tool = ToolSearchTool(reg)
    reg.register(search_tool)
    return reg


class TestToolSearch:
    def test_keyword_search_scores_name_description_and_metadata(
        self,
        registry: ToolRegistry,
    ) -> None:
        matches, missing = search_registered_tools(
            query="yaml syntax",
            tools=registry.all(),
            max_results=5,
        )

        names = [match.name for match in matches]
        assert missing == []
        assert "yaml_validate" in names
        assert matches[0].score > 0

    def test_select_query_returns_requested_tools(self, registry: ToolRegistry) -> None:
        matches, missing = search_registered_tools(
            query="select:file_read,bash_run,missing_tool",
            tools=registry.all(),
            max_results=5,
        )

        assert [match.name for match in matches] == ["file_read", "bash_run"]
        assert missing == ["missing_tool"]

    async def test_tool_execution_formats_results(self, registry: ToolRegistry) -> None:
        tool = registry.get("tool_search")
        assert tool is not None

        output = await tool.execute(query="file read", max_results=3)

        assert "工具搜索" in output
        assert "`file_read`" in output
        assert "只读" in output

    async def test_tool_reports_empty_query(self, registry: ToolRegistry) -> None:
        tool = registry.get("tool_search")
        assert tool is not None

        output = await tool.execute(query="   ")

        assert "query 不能为空" in output


class TestToolSearchIntegration:
    @pytest.fixture
    def engine(self, tmp_path, request) -> AgentEngine:
        instance = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )

        def cleanup() -> None:
            asyncio.run(instance.shutdown())

        request.addfinalizer(cleanup)
        return instance

    def test_engine_registers_tool_search(self, engine: AgentEngine) -> None:
        assert "tool_search" in engine.tool_registry.names

    async def test_engine_executes_tool_search(self, engine: AgentEngine) -> None:
        result = await engine._execute_tool(
            ToolCall(
                id="search-1",
                name="tool_search",
                arguments='{"query": "memory recall", "max_results": 5}',
            )
        )

        assert result.status == "success"
        assert "工具搜索" in result.content
        assert "`memory_recall`" in result.content

    def test_permission_allows_tool_search_in_lockdown(self) -> None:
        decision = PermissionChecker(PermissionMode.LOCKDOWN).check("tool_search", {})

        assert decision.allowed
        assert not decision.requires_confirmation
