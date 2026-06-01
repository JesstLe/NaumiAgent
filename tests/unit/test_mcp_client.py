"""MCP Client 单元测试."""

from __future__ import annotations

import pytest

from naumi_agent.mcp.client import (
    MCPClientManager,
    MCPServerConfig,
    MCPToolBridge,
    _mcp_public_tool_name,
)


class TestMCPServerConfig:
    def test_defaults(self):
        cfg = MCPServerConfig(command="echo", args=["hello"])
        assert cfg.command == "echo"
        assert cfg.args == ["hello"]
        assert cfg.env is None

    def test_with_env(self):
        cfg = MCPServerConfig(command="node", args=["server.js"], env={"KEY": "val"})
        assert cfg.env == {"KEY": "val"}


class TestMCPClientManager:
    def test_init(self):
        manager = MCPClientManager()
        assert len(manager._sessions) == 0
        assert len(manager._exit_stacks) == 0
        assert len(manager._tool_to_server) == 0
        assert manager.connected_servers == []

    @pytest.mark.asyncio
    async def test_connect_nonexistent_command(self):
        """连接不存在的命令应返回空列表，不抛异常."""
        manager = MCPClientManager()
        config = MCPServerConfig(command="nonexistent_command_xyz", args=[])
        tools = await manager.connect("test", config)
        assert tools == []
        assert "test" not in manager._sessions

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(self):
        """断开未连接的 server 不应报错."""
        manager = MCPClientManager()
        await manager.disconnect("nonexistent")  # no error

    @pytest.mark.asyncio
    async def test_disconnect_all_empty(self):
        """空 manager 断开全部不应报错."""
        manager = MCPClientManager()
        await manager.disconnect_all()


class TestMCPToolBridge:
    def test_properties(self):
        tool = MCPToolBridge(
            server_name="test_server",
            tool_name="search",
            description="Search the web",
            parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            session=None,  # type: ignore[arg-type]
        )
        assert tool.name == "search"
        assert tool.description == "Search the web"
        assert tool.parameters_schema == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }

    def test_public_name_can_be_namespaced(self):
        tool = MCPToolBridge(
            server_name="docs",
            tool_name="search",
            public_name="mcp__docs__search",
            description="Search docs",
            parameters_schema={"type": "object", "properties": {}},
            session=None,  # type: ignore[arg-type]
        )

        assert tool.name == "mcp__docs__search"

    def test_public_tool_name_sanitizes_and_truncates(self):
        name = _mcp_public_tool_name(
            "local docs/server",
            "search everything with a very very very very long name",
        )

        assert name.startswith("mcp__local_docs_")
        assert len(name) <= 64
        assert "/" not in name
        assert " " not in name
        assert name.rsplit("_", 1)[-1].isalnum()

    @pytest.mark.asyncio
    async def test_execute_without_session(self):
        """没有活跃 session 时执行应返回错误信息."""
        tool = MCPToolBridge(
            server_name="test",
            tool_name="echo",
            description="",
            parameters_schema={"type": "object", "properties": {}},
            session=None,  # type: ignore[arg-type]
        )
        result = await tool.execute()
        assert "MCP tool error" in result


class TestSetupMCPServers:
    @pytest.mark.asyncio
    async def test_empty_config(self):
        from naumi_agent.mcp.client import setup_mcp_servers

        manager, tools = await setup_mcp_servers({})
        assert len(tools) == 0

    @pytest.mark.asyncio
    async def test_skip_missing_command(self):
        from naumi_agent.mcp.client import setup_mcp_servers

        manager, tools = await setup_mcp_servers({"bad": {"args": []}})
        assert len(tools) == 0

    @pytest.mark.asyncio
    async def test_skip_empty_command(self):
        from naumi_agent.mcp.client import setup_mcp_servers

        manager, tools = await setup_mcp_servers({"bad": {"command": ""}})
        assert len(tools) == 0
