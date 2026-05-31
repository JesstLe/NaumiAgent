"""Agent 调度器测试."""

import pytest

from naumi_agent.agents.base import AgentCapability, AgentConfig
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubAgentManager, SubTask


@pytest.fixture
def manager() -> SubAgentManager:
    engine = AgentEngine(AppConfig())
    return SubAgentManager(engine)


class TestSubAgentManager:
    def test_get_agent(self, manager: SubAgentManager) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        assert agent.config.name == "coder"

    def test_get_nonexistent_agent(self, manager: SubAgentManager) -> None:
        assert manager.get_agent("nonexistent") is None

    def test_select_agent_coder(self, manager: SubAgentManager) -> None:
        assert manager.select_agent("debug the error in main.py") == "coder"
        assert manager.select_agent("refactor the code") == "coder"

    def test_select_agent_researcher(self, manager: SubAgentManager) -> None:
        assert manager.select_agent("research about quantum computing") == "researcher"
        assert manager.select_agent("search for best practices") == "researcher"

    def test_select_agent_browser(self, manager: SubAgentManager) -> None:
        assert manager.select_agent("browse to example.com and scrape data") == "browser"

    def test_select_agent_no_match(self, manager: SubAgentManager) -> None:
        result = manager.select_agent("just a general question")
        assert result is None

    def test_list_agents(self, manager: SubAgentManager) -> None:
        agents = manager.list_agents()
        assert len(agents) == 3
        names = {a["name"] for a in agents}
        assert names == {"coder", "researcher", "browser"}

    @pytest.mark.asyncio
    async def test_dynamic_spawn_starts_reaper_lazily(self, manager: SubAgentManager) -> None:
        assert manager._reaper_task is None
        manager.spawn(
            AgentConfig(
                name="temp_agent",
                description="temporary",
                capabilities=[AgentCapability.FILE_OPS],
            )
        )
        assert manager._reaper_task is not None
        await manager.stop_reaper()


class TestSubTask:
    def test_subtask_defaults(self) -> None:
        task = SubTask(id="t1", description="test task")
        assert task.depends_on == []
        assert task.agent_name is None
        assert task.context == ""

    def test_subtask_with_deps(self) -> None:
        task = SubTask(
            id="t2",
            description="step 2",
            depends_on=["t1"],
            agent_name="coder",
        )
        assert task.depends_on == ["t1"]
        assert task.agent_name == "coder"
