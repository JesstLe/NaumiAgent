"""Agent 调度器测试."""

import asyncio

import pytest

from naumi_agent.agents.base import AgentCapability, AgentConfig, AgentResult
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import (
    AgentState,
    SubAgentManager,
    SubTask,
)


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

    @pytest.mark.asyncio
    async def test_execute_dag_blocks_downstream_when_dependency_fails(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executed: list[str] = []

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            executed.append(task.id)
            if task.id == "a":
                return AgentResult(status="error", error="boom")
            return AgentResult(status="completed", response="unexpected")

        monkeypatch.setattr(manager, "delegate", fake_delegate)

        results = await manager.execute_dag(
            [
                SubTask(id="a", description="upstream"),
                SubTask(id="b", description="downstream", depends_on=["a"]),
            ]
        )

        assert executed == ["a"]
        assert results["a"].status == "error"
        assert results["b"].status == "error"
        assert "Failed dependencies" in (results["b"].error or "")

    @pytest.mark.asyncio
    async def test_delegate_times_out_stuck_agent_and_restores_idle_state(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager.spawn(
            AgentConfig(
                name="stuck_agent",
                description="agent that never returns",
                capabilities=[],
                timeout_seconds=0.01,
            )
        )
        agent = manager.get_agent("stuck_agent")
        assert agent is not None
        started = asyncio.Event()

        async def stuck_execute(**kwargs: object) -> AgentResult:
            started.set()
            await asyncio.sleep(3600)
            return AgentResult(status="completed", response="unexpected")

        monkeypatch.setattr(agent, "execute", stuck_execute)

        result = await manager.delegate(
            SubTask(
                id="hang",
                description="hang forever",
                agent_name="stuck_agent",
            )
        )

        assert started.is_set()
        assert result.status == "timeout"
        assert "超时" in (result.error or "")
        assert manager.get_state("stuck_agent") == AgentState.IDLE


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
