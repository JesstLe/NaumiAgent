"""MCP Client Manager — 连接外部 MCP Server 并注册为工具."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    command: str
    args: list[str]
    env: dict[str, str] | None = None


class MCPClientManager:
    """管理多个 MCP Server 连接，将远程工具桥接为本地 Tool 实例."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._tool_to_server: dict[str, str] = {}

    async def connect(self, name: str, config: MCPServerConfig) -> list[Tool]:
        """连接到一个 MCP Server，返回其工具列表."""
        if name in self._sessions:
            return []

        stack = AsyncExitStack()
        try:
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env,
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            self._sessions[name] = session
            self._exit_stacks[name] = stack

            result = await session.list_tools()
            tools = []
            for t in result.tools:
                tool = MCPToolBridge(
                    server_name=name,
                    tool_name=t.name,
                    description=t.description or "",
                    parameters_schema=t.inputSchema or {"type": "object", "properties": {}},
                    session=session,
                )
                self._tool_to_server[t.name] = name
                tools.append(tool)

            logger.info(
                "MCP server '%s' connected: %d tools (%s)",
                name,
                len(tools),
                ", ".join(t.name for t in tools),
            )
            return tools

        except Exception as e:
            await stack.aclose()
            logger.error("Failed to connect MCP server '%s': %s", name, e)
            return []

    async def disconnect(self, name: str) -> None:
        """断开一个 MCP Server."""
        stack = self._exit_stacks.pop(name, None)
        if stack:
            await stack.aclose()
        self._sessions.pop(name, None)
        to_remove = [k for k, v in self._tool_to_server.items() if v == name]
        for k in to_remove:
            del self._tool_to_server[k]

    async def disconnect_all(self) -> None:
        """断开所有 MCP Server."""
        for name in list(self._exit_stacks.keys()):
            await self.disconnect(name)


class MCPToolBridge(Tool):
    """将 MCP 远程工具桥接为本地 Tool 接口."""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters_schema: dict[str, Any],
        session: ClientSession,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._description = description
        self._schema = parameters_schema
        self._session = session

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._session.call_tool(self._tool_name, arguments=kwargs)
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    parts.append(content.data)
            if result.isError:
                return f"Error: {''.join(parts)}"
            return "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            return (
                f"MCP tool error ({self._server_name}"
                f"/{self._tool_name}): {type(e).__name__}: {e}"
            )


async def setup_mcp_servers(
    server_configs: dict[str, dict[str, Any]],
) -> tuple[MCPClientManager, list[Tool]]:
    """从配置初始化所有 MCP Server，返回 manager 和工具列表."""
    manager = MCPClientManager()
    all_tools: list[Tool] = []

    for name, cfg in server_configs.items():
        if not cfg.get("command"):
            logger.warning("MCP server '%s' missing 'command', skipping", name)
            continue

        config = MCPServerConfig(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
        )
        tools = await manager.connect(name, config)
        all_tools.extend(tools)

    return manager, all_tools
