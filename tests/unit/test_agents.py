"""子 Agent 系统测试."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.agents.base import AgentCapability, AgentConfig, BaseAgent
from naumi_agent.agents.presets import (
    ALL_AGENT_CONFIGS,
    BROWSER_CONFIG,
    CODER_CONFIG,
    RESEARCHER_CONFIG,
)
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine


@pytest.fixture
def engine() -> AgentEngine:
    return AgentEngine(AppConfig())


class TestAgentConfigs:
    def test_all_configs_present(self) -> None:
        assert "coder" in ALL_AGENT_CONFIGS
        assert "researcher" in ALL_AGENT_CONFIGS
        assert "browser" in ALL_AGENT_CONFIGS

    def test_coder_config(self) -> None:
        assert CODER_CONFIG.name == "coder"
        assert AgentCapability.FILE_OPS in CODER_CONFIG.capabilities
        assert AgentCapability.CODE_EXEC in CODER_CONFIG.capabilities
        assert CODER_CONFIG.max_turns == 50

    def test_researcher_config(self) -> None:
        assert RESEARCHER_CONFIG.name == "researcher"
        assert AgentCapability.WEB_SEARCH in RESEARCHER_CONFIG.capabilities

    def test_browser_config(self) -> None:
        assert BROWSER_CONFIG.name == "browser"
        assert AgentCapability.WEB_BROWSE in BROWSER_CONFIG.capabilities

    @pytest.mark.parametrize("config", ALL_AGENT_CONFIGS.values())
    def test_presets_use_shared_unlimited_budget_and_fifty_turns(
        self,
        config: AgentConfig,
    ) -> None:
        assert config.max_budget_usd is None
        assert config.max_turns == 50


class TestBaseAgent:
    def test_resolve_tools(self, engine: AgentEngine) -> None:
        agent = BaseAgent(CODER_CONFIG, engine)
        tool_names = agent._tool_names

        assert "file_read" in tool_names
        assert "file_write" in tool_names
        assert "file_edit" in tool_names
        assert "code_execute" in tool_names
        assert "bash_run" in tool_names

    def test_browser_agent_tools(self, engine: AgentEngine) -> None:
        agent = BaseAgent(BROWSER_CONFIG, engine)
        tool_names = agent._tool_names

        assert "browser_goto" in tool_names
        assert "browser_click" in tool_names
        assert "browser_click" in tool_names
        assert "web_search" in tool_names

    def test_get_tool_schemas(self, engine: AgentEngine) -> None:
        agent = BaseAgent(CODER_CONFIG, engine)
        schemas = agent._get_tool_schemas()

        assert len(schemas) > 0
        for s in schemas:
            assert s["type"] == "function"
            assert "function" in s

    @pytest.mark.asyncio
    async def test_subagent_tool_call_bubbles_parent_permission(
        self,
        tmp_path,
    ) -> None:
        engine = AgentEngine(AppConfig(
            memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
        ))
        agent = BaseAgent(CODER_CONFIG, engine)
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        tool_call = {
            "id": "call_bash",
            "function": {
                "name": "bash_run",
                "arguments": json.dumps({"command": "echo hi"}),
            },
        }
        responses = [
            ModelResponse(
                content="",
                tool_calls=[tool_call],
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            ),
            ModelResponse(
                content="已处理",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            ),
        ]

        try:
            with patch.object(
                engine.router,
                "call",
                new_callable=AsyncMock,
                side_effect=responses,
            ):
                result = await agent.execute("运行命令", event_callback=on_event)

            assert result.status == "completed"
            bubbles = [data for event, data in events if event == "permission_bubble"]
            assert bubbles
            assert bubbles[-1]["agent_name"] == "coder"
            assert bubbles[-1]["tool_name"] == "bash_run"
            assert bubbles[-1]["status"] == "needs_confirmation"
            assert engine.get_recent_permission_bubbles()
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_subagent_stops_before_next_turn_when_budget_is_exhausted(
        self,
        engine: AgentEngine,
    ) -> None:
        agent = BaseAgent(
            AgentConfig(
                name="budgeted",
                description="Budgeted test agent",
                capabilities=[],
                max_turns=2,
                max_budget_usd=0.01,
            ),
            engine,
        )
        responses = [
            ModelResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_unknown",
                        "function": {"name": "unknown_tool", "arguments": "{}"},
                    }
                ],
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=10,
                    total_tokens=20,
                    cost_usd=0.02,
                ),
                model="test",
            ),
            ModelResponse(
                content="should not be called",
                usage=TokenUsage(total_tokens=1, cost_usd=0.01),
                model="test",
            ),
        ]

        with patch.object(
            engine.router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as call:
            result = await agent.execute("需要多轮")

        assert call.await_count == 1
        assert result.status == "error"
        assert result.total_cost_usd == 0.02
        assert "预算" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unlimited_budget_continues_after_high_cost(
        self,
        engine: AgentEngine,
    ) -> None:
        agent = BaseAgent(
            AgentConfig(
                name="unlimited",
                description="Unlimited test agent",
                capabilities=[],
                max_turns=2,
            ),
            engine,
        )
        responses = [
            ModelResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_unknown",
                        "function": {"name": "unknown_tool", "arguments": "{}"},
                    }
                ],
                usage=TokenUsage(total_tokens=20, cost_usd=100.0),
                model="test",
            ),
            ModelResponse(
                content="完成",
                usage=TokenUsage(total_tokens=1, cost_usd=100.0),
                model="test",
            ),
        ]

        with patch.object(
            engine.router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as call:
            result = await agent.execute("需要两轮")

        assert call.await_count == 2
        assert result.status == "completed"
        assert result.total_cost_usd == 200.0

    @pytest.mark.asyncio
    async def test_zero_budget_stops_before_first_router_call(
        self,
        engine: AgentEngine,
    ) -> None:
        agent = BaseAgent(
            AgentConfig(
                name="zero-budget",
                description="Zero budget test agent",
                capabilities=[],
                max_budget_usd=0,
            ),
            engine,
        )

        with patch.object(
            engine.router,
            "call",
            new_callable=AsyncMock,
        ) as call:
            result = await agent.execute("不应调用模型")

        call.assert_not_awaited()
        assert result.status == "error"
        assert "预算已耗尽" in (result.error or "")

    @pytest.mark.asyncio
    async def test_subagent_permission_level_blocks_disallowed_tools_before_parent_execution(
        self,
        engine: AgentEngine,
    ) -> None:
        agent = BaseAgent(
            AgentConfig(
                name="strict_worker",
                description="Strict test agent",
                capabilities=[AgentCapability.SHELL_EXEC],
                max_turns=2,
                permission_level="strict",
            ),
            engine,
        )
        tool_call = {
            "id": "call_bash",
            "function": {
                "name": "bash_run",
                "arguments": json.dumps({"command": "echo hi"}),
            },
        }
        responses = [
            ModelResponse(
                content="",
                tool_calls=[tool_call],
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            ),
            ModelResponse(
                content="已收到权限拒绝",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test",
            ),
        ]

        with (
            patch.object(
                engine.router,
                "call",
                new_callable=AsyncMock,
                side_effect=responses,
            ) as call,
            patch.object(engine, "_execute_tool", new_callable=AsyncMock) as execute_tool,
        ):
            result = await agent.execute("运行命令")

        assert execute_tool.await_count == 0
        assert call.await_count == 2
        followup_messages = call.await_args_list[1].kwargs["messages"]
        tool_messages = [m for m in followup_messages if m.get("role") == "tool"]
        assert result.status == "completed"
        assert "权限拒绝" in tool_messages[-1]["content"]
