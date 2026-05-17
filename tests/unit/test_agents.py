"""子 Agent 系统测试."""

import pytest

from naumi_agent.agents.base import AgentCapability, BaseAgent
from naumi_agent.agents.presets import (
    ALL_AGENT_CONFIGS,
    BROWSER_CONFIG,
    CODER_CONFIG,
    RESEARCHER_CONFIG,
)
from naumi_agent.config.settings import AppConfig
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
        assert CODER_CONFIG.max_turns == 15

    def test_researcher_config(self) -> None:
        assert RESEARCHER_CONFIG.name == "researcher"
        assert AgentCapability.WEB_SEARCH in RESEARCHER_CONFIG.capabilities

    def test_browser_config(self) -> None:
        assert BROWSER_CONFIG.name == "browser"
        assert AgentCapability.WEB_BROWSE in BROWSER_CONFIG.capabilities


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
