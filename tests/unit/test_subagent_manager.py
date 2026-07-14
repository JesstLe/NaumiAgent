"""Agent 调度器测试."""

import asyncio

import pytest

from naumi_agent.agents.base import AgentCapability, AgentConfig, AgentResult
from naumi_agent.config.settings import AppConfig, SafetyConfig
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
    async def test_execute_parallel_applies_fifo_backpressure(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=2))
        )
        manager = SubAgentManager(engine)
        active = 0
        peak = 0
        started: list[str] = []
        release = asyncio.Event()
        first_wave = asyncio.Event()

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.append(task.id)
            if len(started) == 2:
                first_wave.set()
            try:
                await release.wait()
                return AgentResult(status="completed", response=task.id)
            finally:
                active -= 1

        manager.delegate = fake_delegate  # type: ignore[method-assign]
        running = asyncio.create_task(
            manager.execute_parallel(
                [SubTask(str(index), f"task {index}") for index in range(6)]
            )
        )
        try:
            await asyncio.wait_for(first_wave.wait(), timeout=1)
            await asyncio.sleep(0)
            assert started == ["0", "1"]
            assert peak == 2
            assert manager.queued_parallel_agent_count == 4
            release.set()
            results = await asyncio.wait_for(running, timeout=1)
        finally:
            release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

        assert [result.response for result in results] == [
            "0",
            "1",
            "2",
            "3",
            "4",
            "5",
        ]
        assert started == ["0", "1", "2", "3", "4", "5"]
        assert peak == 2
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_execute_parallel_isolates_failure_and_preserves_order(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=3))
        )
        manager = SubAgentManager(engine)

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            if task.id == "1":
                raise RuntimeError("boom")
            await asyncio.sleep(0)
            return AgentResult(status="completed", response=task.id)

        manager.delegate = fake_delegate  # type: ignore[method-assign]
        results = await manager.execute_parallel(
            [SubTask(str(index), f"task {index}") for index in range(3)]
        )

        assert [result.status for result in results] == [
            "completed",
            "error",
            "completed",
        ]
        assert "RuntimeError: boom" in (results[1].error or "")
        assert results[2].response == "2"

    @pytest.mark.asyncio
    async def test_execute_parallel_parent_cancel_stops_workers_and_queue(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=2))
        )
        manager = SubAgentManager(engine)
        started: list[str] = []
        cancelled: list[str] = []
        first_wave = asyncio.Event()

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.append(task.id)
            if len(started) == 2:
                first_wave.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(task.id)
                raise
            return AgentResult(status="completed")

        manager.delegate = fake_delegate  # type: ignore[method-assign]
        running = asyncio.create_task(
            manager.execute_parallel(
                [SubTask(str(index), f"task {index}") for index in range(20)]
            )
        )
        await asyncio.wait_for(first_wave.wait(), timeout=1)
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running
        assert started == ["0", "1"]
        assert set(cancelled) == {"0", "1"}
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_parallel_limit_is_shared_across_simultaneous_batches(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=2))
        )
        manager = SubAgentManager(engine)
        active = 0
        peak = 0

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return AgentResult(status="completed", response=task.id)
            finally:
                active -= 1

        manager.delegate = fake_delegate  # type: ignore[method-assign]
        left, right = await asyncio.gather(
            manager.execute_parallel(
                [SubTask(f"left-{index}", "left") for index in range(5)]
            ),
            manager.execute_parallel(
                [SubTask(f"right-{index}", "right") for index in range(5)]
            ),
        )

        assert peak == 2
        assert [result.response for result in left] == [
            f"left-{index}" for index in range(5)
        ]
        assert [result.response for result in right] == [
            f"right-{index}" for index in range(5)
        ]

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

    @pytest.mark.asyncio
    async def test_stop_execution_cancels_only_the_selected_running_task(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager.spawn(
            AgentConfig(
                name="blocking_agent",
                description="agent with independently cancellable executions",
                capabilities=[],
                timeout_seconds=60,
            )
        )
        agent = manager.get_agent("blocking_agent")
        assert agent is not None
        both_started = asyncio.Event()
        release_second = asyncio.Event()
        started_count = 0

        async def blocking_execute(*, task: str, **kwargs: object) -> AgentResult:
            nonlocal started_count
            started_count += 1
            if started_count == 2:
                both_started.set()
            if task == "second":
                await release_second.wait()
                return AgentResult(status="completed", response="second done")
            await asyncio.Event().wait()
            return AgentResult(status="completed", response="unexpected")

        monkeypatch.setattr(agent, "execute", blocking_execute)
        first = asyncio.create_task(manager.delegate(SubTask(
            id="agent-task-1",
            description="first",
            agent_name="blocking_agent",
        )))
        second = asyncio.create_task(manager.delegate(SubTask(
            id="agent-task-2",
            description="second",
            agent_name="blocking_agent",
        )))

        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active = manager.list_executions()
            assert {item.task_id for item in active if item.status == "running"} == {
                "agent-task-1",
                "agent-task-2",
            }

            stopped = await manager.stop_execution("agent-task-1", "用户停止。")
            assert stopped.accepted is True
            assert stopped.code == "accepted"
            repeated = await manager.stop_execution("agent-task-1", "再次停止。")
            assert repeated.accepted is False
            assert repeated.code == "already_requested"
            assert (await asyncio.wait_for(first, timeout=1)).status == "cancelled"
            assert not second.done()
            assert manager.get_state("blocking_agent") == AgentState.RUNNING
            lifecycle = manager.get_lifecycle("blocking_agent")
            assert lifecycle is not None
            assert lifecycle.task_count == 2

            release_second.set()
            assert (await asyncio.wait_for(second, timeout=1)).status == "completed"
            terminal = {item.task_id: item for item in manager.list_executions()}
            assert terminal["agent-task-1"].status == "cancelled"
            assert terminal["agent-task-1"].stop_requested is True
            assert terminal["agent-task-2"].status == "completed"
        finally:
            for pending in (first, second):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_stop_execution_returns_stable_missing_and_finished_codes(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert (await manager.stop_execution("")).code == "missing_task_id"
        assert (await manager.stop_execution("unknown")).code == "not_found"
        agent = manager.get_agent("coder")
        assert agent is not None

        async def complete_execute(**kwargs: object) -> AgentResult:
            return AgentResult(status="completed", response="done")

        monkeypatch.setattr(agent, "execute", complete_execute)
        result = await manager.delegate(SubTask("finished", "done", "coder"))

        assert result.status == "completed"
        stopped = await manager.stop_execution("finished")
        assert stopped.accepted is False
        assert stopped.code == "already_finished"

    @pytest.mark.asyncio
    async def test_delegate_rejects_duplicate_active_task_id(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        started = asyncio.Event()

        async def blocking_execute(**kwargs: object) -> AgentResult:
            started.set()
            await asyncio.Event().wait()
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", blocking_execute)
        first = asyncio.create_task(manager.delegate(SubTask("same", "first", "coder")))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            duplicate = await manager.delegate(SubTask("same", "second", "coder"))
            assert duplicate.status == "error"
            assert "Duplicate active" in (duplicate.error or "")
            assert len([
                item for item in manager.list_executions()
                if item.task_id == "same" and item.stop_supported
            ]) == 1
        finally:
            await manager.stop_execution("same")
            await asyncio.gather(first, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_parent_cancellation_propagates_and_cleans_execution(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        started = asyncio.Event()

        async def blocking_execute(**kwargs: object) -> AgentResult:
            started.set()
            await asyncio.Event().wait()
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", blocking_execute)
        parent = asyncio.create_task(manager.delegate(SubTask("parent-cancel", "wait", "coder")))
        await asyncio.wait_for(started.wait(), timeout=1)
        parent.cancel()

        with pytest.raises(asyncio.CancelledError):
            await parent
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "parent-cancel"
        )
        assert record.status == "cancelled"
        assert record.stop_requested is False
        assert manager.get_state("coder") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_execution_observes_tool_progress_without_swallowing_callback(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        tool_seen = asyncio.Event()
        release = asyncio.Event()
        forwarded: list[tuple[str, dict[str, object]]] = []

        async def tool_execute(
            *,
            event_callback: object,
            **kwargs: object,
        ) -> AgentResult:
            assert callable(event_callback)
            await event_callback("tool_start", {"tool_name": "file_read"})
            tool_seen.set()
            await release.wait()
            return AgentResult(status="completed", turns=1)

        async def callback(event: str, data: dict[str, object]) -> None:
            forwarded.append((event, data))

        monkeypatch.setattr(agent, "execute", tool_execute)
        delegated = asyncio.create_task(manager.delegate(
            SubTask("tool-progress", "inspect", "coder"),
            event_callback=callback,
        ))
        await asyncio.wait_for(tool_seen.wait(), timeout=1)
        active = next(
            item for item in manager.list_executions()
            if item.task_id == "tool-progress"
        )
        assert active.phase == "running_tool"
        assert active.current_tool == "file_read"
        assert active.recent_tools == ("file_read",)
        assert [item for item in forwarded if item[0] == "tool_start"] == [
            ("tool_start", {"tool_name": "file_read"})
        ]
        release.set()
        assert (await delegated).status == "completed"

    @pytest.mark.asyncio
    async def test_execution_history_is_bounded_to_one_hundred_records(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None

        async def complete_execute(**kwargs: object) -> AgentResult:
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", complete_execute)
        for index in range(105):
            result = await manager.delegate(SubTask(
                f"history-{index}",
                f"task {index}",
                "coder",
            ))
            assert result.status == "completed"

        records = manager.list_executions(limit=500)
        assert len(records) == 100
        assert records[0].task_id == "history-104"
        assert records[-1].task_id == "history-5"

    @pytest.mark.asyncio
    async def test_started_callback_failure_does_not_leak_active_execution(
        self,
        manager: SubAgentManager,
    ) -> None:
        async def failing_callback(event: str, data: dict[str, object]) -> None:
            if event == "subagent_event":
                raise RuntimeError("broken event consumer")

        with pytest.raises(RuntimeError, match="broken event consumer"):
            await manager.delegate(
                SubTask("callback-failure", "inspect", "coder"),
                event_callback=failing_callback,
            )

        assert not any(
            item.task_id == "callback-failure" and item.stop_supported
            for item in manager.list_executions()
        )
        assert manager.get_state("coder") == AgentState.IDLE


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
