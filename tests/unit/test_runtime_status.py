"""Runtime status tool tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.agents.team_protocol import execute_team_signal
from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import ToolCall
from naumi_agent.tools.runtime import (
    RuntimeMCPConnectTool,
    RuntimeStatusTool,
    build_runtime_status,
    connect_runtime_mcp,
    run_runtime_command,
)


@pytest.fixture
def engine(tmp_path, request) -> AgentEngine:
    instance = AgentEngine(AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
    ))

    def cleanup() -> None:
        asyncio.run(instance.shutdown())

    request.addfinalizer(cleanup)
    return instance


class TestRuntimeStatus:
    def test_tool_is_registered(self, engine: AgentEngine) -> None:
        assert "runtime_status" in engine.tool_registry.names
        assert "runtime_mcp_connect" in engine.tool_registry.names

    def test_runtime_tools_expose_permission_metadata(self, engine: AgentEngine) -> None:
        status_metadata = RuntimeStatusTool(engine).metadata
        connect_metadata = RuntimeMCPConnectTool(engine).metadata

        assert status_metadata.read_only is True
        assert status_metadata.concurrency_safe is True
        assert connect_metadata.destructive is True
        assert connect_metadata.requires_confirmation is True
        assert connect_metadata.command_argument_names == ("command",)

    @pytest.mark.asyncio
    async def test_snapshot_includes_runtime_state(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        task = await engine.task_store.create_task(subject="处理 runtime connect")
        await engine.task_store.update_task(
            task.id,
            status=TaskStatus.BLOCKED,
            active_form="阻塞：等待用户确认范围",
        )
        await execute_team_signal(
            engine.subagent_manager,
            event_type="handoff",
            sender="main_agent",
            recipient="coder",
            content="接手 runtime_status 验证。",
            priority="high",
        )
        await engine.subagent_manager.delegate(
            SubTask(id="runtime-sub", description="没有关键词的验证任务")
        )
        await engine._emit_permission_bubble(
            None,
            agent_name="coder",
            tool_name="bash_run",
            status="needs_confirmation",
            reason="该工具需要用户确认",
            requires_confirmation=True,
        )

        output = await build_runtime_status(engine, sections="todo,team,subagent,recommendations")

        assert "处理 runtime connect" in output
        assert "等待用户确认范围" in output
        assert "接手 runtime_status 验证" in output
        assert "没有找到合适的子 Agent" in output
        assert "集群并发：0/4 活跃 · 0 排队" in output
        assert "权限冒泡" in output
        assert "bash_run" in output
        assert "blocked todo" in output

    @pytest.mark.asyncio
    async def test_tool_execution_uses_same_snapshot(self, engine: AgentEngine) -> None:
        session = await engine.get_or_create_session()
        engine.task_store.set_session(session.id)
        await engine.task_store.create_task(subject="读取 runtime tool")

        result = await engine._execute_tool(ToolCall(
            id="runtime-1",
            name="runtime_status",
            arguments=json.dumps({"sections": "todo", "limit": 3}, ensure_ascii=False),
        ))

        assert result.status == "success"
        assert "读取 runtime tool" in result.content

    @pytest.mark.asyncio
    async def test_runtime_command_uses_same_builder(self, engine: AgentEngine) -> None:
        output = await run_runtime_command(engine, "context 2")

        assert "## Runtime 状态" in output
        assert "### 上下文与预算" in output
        assert "预算：不限 · 已用 $0.0000" in output
        assert "工作区" in output

    @pytest.mark.asyncio
    async def test_resources_reports_browser_backpressure_without_starting_runner(
        self,
        engine: AgentEngine,
    ) -> None:
        assert engine._task_runner is None

        output = await build_runtime_status(engine, sections="resources")

        assert "浏览器队列：0/2 活跃 · 0 排队" in output
        assert engine._task_runner is None

    @pytest.mark.asyncio
    async def test_resources_reports_preparing_background_reservation(
        self,
        engine: AgentEngine,
    ) -> None:
        engine.background_runner.store.save(BackgroundTask(
            id="bg_preparing",
            command="echo preparing",
            cwd=str(engine.workspace_root),
            status=BackgroundStatus.PREPARING,
            output_path=str(
                engine.background_runner.store.artifacts_dir / "bg_preparing.log"
            ),
            idempotency_key="pact_preparing-1",
        ))

        output = await build_runtime_status(engine, sections="resources")

        assert "1 准备中" in output
        assert "bg bg_preparing [preparing]" in output

    @pytest.mark.asyncio
    async def test_runtime_mcp_connect_registers_discovered_tools(
        self,
        engine: AgentEngine,
    ) -> None:
        with patch.object(
            engine,
            "connect_mcp_server",
            new_callable=AsyncMock,
            return_value=["mcp__demo__echo"],
        ) as connect:
            output = await run_runtime_command(engine, "connect demo python server.py")

        connect.assert_awaited_once_with(
            name="demo",
            command="python",
            args=["server.py"],
            env=None,
        )
        assert "已连接 MCP server `demo`" in output
        assert "mcp__demo__echo" in output

    @pytest.mark.asyncio
    async def test_runtime_mcp_connect_reports_empty_discovery(
        self,
        engine: AgentEngine,
    ) -> None:
        with patch.object(
            engine,
            "connect_mcp_server",
            new_callable=AsyncMock,
            return_value=[],
        ):
            output = await run_runtime_command(engine, "connect demo missing-command")

        assert "未注册新工具" in output
        assert "请检查命令是否可执行" in output

    @pytest.mark.asyncio
    async def test_runtime_mcp_connect_rejects_invalid_server_name(
        self,
        engine: AgentEngine,
    ) -> None:
        with patch.object(
            engine,
            "connect_mcp_server",
            new_callable=AsyncMock,
        ) as connect:
            output = await connect_runtime_mcp(
                engine,
                name="bad name",
                command="python",
                args=["server.py"],
            )

        connect.assert_not_awaited()
        assert "名称只能包含" in output

    @pytest.mark.asyncio
    async def test_runtime_mcp_connect_rejects_invalid_args_and_env(
        self,
        engine: AgentEngine,
    ) -> None:
        output = await connect_runtime_mcp(
            engine,
            name="demo",
            command="python",
            args=["server.py", 1],  # type: ignore[list-item]
        )
        assert "args 必须是字符串数组" in output

        output = await connect_runtime_mcp(
            engine,
            name="demo",
            command="python",
            env={"TOKEN": object()},  # type: ignore[dict-item]
        )
        assert "env 必须是字符串到字符串的映射" in output
