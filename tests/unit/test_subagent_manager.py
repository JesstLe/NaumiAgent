"""Agent 调度器测试."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.agents.base import AgentCapability, AgentConfig, AgentResult
from naumi_agent.config.settings import AppConfig, SafetyConfig
from naumi_agent.daemons.agent_jobs import (
    AgentJobError,
    AgentJobLifecycleConflictError,
    AgentJobPublicationState,
    AgentJobState,
    AgentJobStore,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import (
    AgentState,
    SubAgentManager,
    SubTask,
)
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.publisher import RuntimeEventPublisher

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture(autouse=True)
def _isolated_agent_job_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(
        "NAUMI_MEMORY__SESSION_DB_PATH",
        str(tmp_path / ".naumi" / "sessions.db"),
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
    async def test_exact_recovery_action_hashes_session_and_maps_conflicts(
        self,
        manager: SubAgentManager,
    ) -> None:
        job = SimpleNamespace(
            job_id="agent-job-1",
            state=AgentJobState.UNKNOWN,
            claim_epoch=4,
            latest_receipt=SimpleNamespace(receipt_sha256="c" * 64),
        )
        manager._agent_job_store.mark_recovery_unknown = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(job=job, applied=True)
        )

        result = await manager.resolve_recovery_unknown(
            session_id="session-1",
            job_id="agent-job-1",
            expected_request_sha256="a" * 64,
            expected_claim_epoch=4,
            expected_latest_receipt_sha256="b" * 64,
        )

        assert result.accepted is True
        assert result.applied is True
        assert result.job_state == "unknown"
        manager._agent_job_store.mark_recovery_unknown.assert_awaited_once_with(
            "agent-job-1",
            expected_request_sha256="a" * 64,
            expected_session_id_sha256=hashlib.sha256(
                b"session-1"
            ).hexdigest(),
            expected_claim_epoch=4,
            expected_latest_receipt_sha256="b" * 64,
        )

        manager._agent_job_store.mark_recovery_unknown = AsyncMock(  # type: ignore[method-assign]
            side_effect=AgentJobLifecycleConflictError("stale")
        )
        rejected = await manager.resolve_recovery_unknown(
            session_id="session-1",
            job_id="agent-job-1",
            expected_request_sha256="a" * 64,
            expected_claim_epoch=4,
            expected_latest_receipt_sha256="b" * 64,
        )
        assert rejected.accepted is False
        assert rejected.code == "recovery_fence_changed"
        assert rejected.receipt_sha256 == ""

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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
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
    async def test_direct_delegations_share_the_parallel_admission_limit(self) -> None:
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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        running = [
            asyncio.create_task(manager.delegate(SubTask(str(index), "direct")))
            for index in range(5)
        ]
        try:
            await asyncio.wait_for(first_wave.wait(), timeout=1)
            await asyncio.sleep(0)
            assert started == ["0", "1"]
            assert peak == 2
            assert manager.queued_parallel_agent_count == 3
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*running), timeout=1)
        finally:
            release.set()
            for task in running:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

        assert [result.response for result in results] == [
            "0",
            "1",
            "2",
            "3",
            "4",
        ]
        assert peak == 2
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_direct_delegation_rejects_when_waiting_queue_is_full(self) -> None:
        engine = AgentEngine(AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=1,
        )))
        manager = SubAgentManager(engine)
        release = asyncio.Event()
        started = asyncio.Event()
        events: list[tuple[str, dict[str, object]]] = []

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.set()
            await release.wait()
            return AgentResult(status="completed", response=task.id)

        async def callback(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        active = asyncio.create_task(manager.delegate(SubTask("active", "direct")))
        queued: asyncio.Task[AgentResult] | None = None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            queued = asyncio.create_task(
                manager.delegate(SubTask("queued", "direct"))
            )
            async with asyncio.timeout(1):
                while manager.queued_parallel_agent_count != 1:
                    await asyncio.sleep(0)

            rejected = await manager.delegate(
                SubTask("rejected", "direct"),
                event_callback=callback,
            )
            assert rejected.status == "error"
            assert "等待队列已满（上限 1）" in (rejected.error or "")
            assert manager.queued_parallel_agent_count == 1
            assert any(
                event == "subagent_event"
                and data.get("status") == "failed"
                and data.get("task_id") == "rejected"
                for event, data in events
            )

            release.set()
            assert (await asyncio.wait_for(active, timeout=1)).response == "active"
            assert (await asyncio.wait_for(queued, timeout=1)).response == "queued"
        finally:
            release.set()
            pending = [task for task in (active, queued) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_zero_waiting_capacity_rejects_second_direct_delegation(self) -> None:
        engine = AgentEngine(AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=0,
        )))
        manager = SubAgentManager(engine)
        release = asyncio.Event()
        started = asyncio.Event()

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.set()
            await release.wait()
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        active = asyncio.create_task(manager.delegate(SubTask("active", "direct")))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            rejected = await manager.delegate(SubTask("rejected", "direct"))
            assert rejected.status == "error"
            assert "等待队列已满（上限 0）" in (rejected.error or "")
            assert manager.queued_parallel_agent_count == 0
        finally:
            release.set()
            await asyncio.gather(active, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_parallel_batch_rejects_items_beyond_shared_queue_budget(self) -> None:
        engine = AgentEngine(AppConfig(safety=SafetyConfig(
            max_parallel_agents=2,
            max_queued_agents=2,
        )))
        manager = SubAgentManager(engine)
        release = asyncio.Event()
        first_wave = asyncio.Event()
        started: list[str] = []
        active = 0
        peak = 0

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

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        running = asyncio.create_task(manager.execute_parallel([
            SubTask(str(index), "batch") for index in range(6)
        ]))
        try:
            await asyncio.wait_for(first_wave.wait(), timeout=1)
            await asyncio.sleep(0)
            assert manager.queued_parallel_agent_count == 2
            assert started == ["0", "1"]
            release.set()
            results = await asyncio.wait_for(running, timeout=1)
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

        assert [result.status for result in results] == [
            "completed",
            "completed",
            "completed",
            "completed",
            "error",
            "error",
        ]
        assert all(
            "等待队列已满（上限 2）" in (result.error or "")
            for result in results[4:]
        )
        assert started == ["0", "1", "2", "3"]
        assert peak == 2
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_simultaneous_batches_share_active_and_waiting_budget(self) -> None:
        engine = AgentEngine(AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=1,
        )))
        manager = SubAgentManager(engine)
        release = asyncio.Event()
        started: list[str] = []

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.append(task.id)
            await release.wait()
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        combined = asyncio.gather(
            manager.execute_parallel([
                SubTask(f"left-{index}", "left") for index in range(3)
            ]),
            manager.execute_parallel([
                SubTask(f"right-{index}", "right") for index in range(3)
            ]),
        )
        try:
            async with asyncio.timeout(1):
                while len(started) != 1 or manager.queued_parallel_agent_count != 1:
                    await asyncio.sleep(0)
            release.set()
            left, right = await asyncio.wait_for(combined, timeout=1)
        finally:
            release.set()
            if not combined.done():
                combined.cancel()
            await asyncio.gather(combined, return_exceptions=True)

        results = [*left, *right]
        assert sum(result.status == "completed" for result in results) == 2
        assert sum(result.status == "error" for result in results) == 4
        assert len(started) == 2
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_direct_and_batch_delegations_share_one_fifo_capacity_gate(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=1))
        )
        manager = SubAgentManager(engine)
        started: list[str] = []
        gates = {
            name: asyncio.Event()
            for name in ("batch-1", "batch-2", "direct")
        }
        began = {name: asyncio.Event() for name in gates}

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.append(task.id)
            began[task.id].set()
            await gates[task.id].wait()
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        batch = asyncio.create_task(manager.execute_parallel([
            SubTask("batch-1", "batch"),
            SubTask("batch-2", "batch"),
        ]))
        direct: asyncio.Task[AgentResult] | None = None
        try:
            await asyncio.wait_for(began["batch-1"].wait(), timeout=1)
            direct = asyncio.create_task(
                manager.delegate(SubTask("direct", "direct"))
            )
            async with asyncio.timeout(1):
                while manager.queued_parallel_agent_count != 2:
                    await asyncio.sleep(0)
            assert manager.queued_parallel_agent_count == 2

            gates["batch-1"].set()
            await asyncio.wait_for(began["batch-2"].wait(), timeout=1)
            gates["batch-2"].set()
            await asyncio.wait_for(began["direct"].wait(), timeout=1)
            gates["direct"].set()

            assert (await asyncio.wait_for(direct, timeout=1)).response == "direct"
            batch_results = await asyncio.wait_for(batch, timeout=1)
        finally:
            for gate in gates.values():
                gate.set()
            pending = [task for task in (batch, direct) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        assert [result.response for result in batch_results] == [
            "batch-1",
            "batch-2",
        ]
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_direct_waiter_does_not_leak_queue_or_capacity(self) -> None:
        engine = AgentEngine(AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=1,
        )))
        manager = SubAgentManager(engine)
        started: list[str] = []
        release = asyncio.Event()
        first_started = asyncio.Event()

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            started.append(task.id)
            if task.id == "first":
                first_started.set()
            await release.wait()
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        first = asyncio.create_task(manager.delegate(SubTask("first", "direct")))
        second: asyncio.Task[AgentResult] | None = None
        replacement_task: asyncio.Task[AgentResult] | None = None
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second = asyncio.create_task(
                manager.delegate(SubTask("cancelled", "direct"))
            )
            async with asyncio.timeout(1):
                while manager.queued_parallel_agent_count != 1:
                    await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert manager.queued_parallel_agent_count == 0
            assert started == ["first"]

            replacement_task = asyncio.create_task(
                manager.delegate(SubTask("replacement", "direct"))
            )
            async with asyncio.timeout(1):
                while manager.queued_parallel_agent_count != 1:
                    await asyncio.sleep(0)
            release.set()
            assert (await asyncio.wait_for(first, timeout=1)).response == "first"
            replacement = await asyncio.wait_for(replacement_task, timeout=1)
        finally:
            release.set()
            pending = [
                task for task in (first, second, replacement_task)
                if task is not None
            ]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        assert replacement.response == "replacement"
        assert started == ["first", "replacement"]
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_active_direct_delegation_releases_capacity(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=1))
        )
        manager = SubAgentManager(engine)
        started = asyncio.Event()

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            if task.id == "active":
                started.set()
                await asyncio.Event().wait()
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]
        active = asyncio.create_task(
            manager.delegate(SubTask("active", "direct"))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        active.cancel()

        with pytest.raises(asyncio.CancelledError):
            await active
        replacement = await asyncio.wait_for(
            manager.delegate(SubTask("replacement", "direct")),
            timeout=1,
        )

        assert replacement.response == "replacement"
        assert manager.queued_parallel_agent_count == 0

    @pytest.mark.asyncio
    async def test_saturated_nested_delegation_fails_instead_of_deadlocking(self) -> None:
        engine = AgentEngine(
            AppConfig(safety=SafetyConfig(max_parallel_agents=1))
        )
        manager = SubAgentManager(engine)
        executed: list[str] = []

        async def fake_delegate(task: SubTask, **kwargs: object) -> AgentResult:
            executed.append(task.id)
            if task.id == "parent":
                return await manager.delegate(SubTask("child", "nested"))
            return AgentResult(status="completed", response=task.id)

        manager._delegate_admitted = fake_delegate  # type: ignore[method-assign]

        result = await asyncio.wait_for(
            manager.delegate(SubTask("parent", "direct")),
            timeout=1,
        )

        assert result.status == "error"
        assert "嵌套委派" in (result.error or "")
        assert executed == ["parent"]
        assert manager.queued_parallel_agent_count == 0

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

        monkeypatch.setattr(manager, "_delegate_admitted", fake_delegate)

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
            return AgentResult(
                status="completed",
                response="durable-private-result-6bfa",
            )

        monkeypatch.setattr(agent, "execute", complete_execute)
        result = await manager.delegate(
            SubTask(
                "finished",
                "durable-private-task-8cd1",
                "coder",
            )
        )

        assert result.status == "completed"
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "finished"
        )
        assert len(record.worker_request_sha256) == 64
        assert len(record.worker_result_sha256) == 64
        assert record.worker_tool_scope == tuple(sorted(agent.tool_names))
        assert record.worker_contract_failure_code == ""
        assert record.worker_job_id.startswith("agent-job-")
        assert record.worker_job_state == AgentJobState.COMPLETED.value
        assert record.worker_claim_epoch == 1
        assert record.worker_job_failure_code == ""
        stored = await manager._agent_job_store.get(record.worker_job_id)
        assert stored is not None
        assert stored.state is AgentJobState.COMPLETED
        assert stored.result is not None
        assert stored.result.result_sha256 == record.worker_result_sha256
        terminal_payload = (
            await manager._agent_job_store.recover_terminal_payload(
                record.worker_job_id,
                expected_result_sha256=record.worker_result_sha256,
            )
        )
        assert terminal_payload.response == "durable-private-result-6bfa"
        assert terminal_payload.error == ""
        publication = await manager._agent_job_store.get_job_publication(
            record.worker_job_id,
        )
        assert publication is not None
        assert publication.state is AgentJobPublicationState.PUBLISHED
        inbox = await manager._agent_job_store.list_result_inbox("")
        delivery = next(
            item for item in inbox
            if item.publication_id == publication.publication_id
        )
        recovered = await manager._agent_job_store.recover_delivered_result(
            delivery.delivery_id,
            expected_delivery_sha256=delivery.delivery_sha256,
        )
        assert recovered.terminal_payload.response == (
            "durable-private-result-6bfa"
        )
        raw_store = manager._agent_job_store.db_path.read_bytes()
        assert b"durable-private-task-8cd1" not in raw_store
        assert b"durable-private-result-6bfa" not in raw_store
        stopped = await manager.stop_execution("finished")
        assert stopped.accepted is False
        assert stopped.code == "already_finished"

    @pytest.mark.asyncio
    async def test_restart_recovers_publication_after_live_delivery_gap(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "restart-publication.db"
        first = SubAgentManager(
            AgentEngine(AppConfig()),
            agent_job_store=AgentJobStore(path),
        )
        agent = first.get_agent("coder")
        assert agent is not None

        async def execute(**_: object) -> AgentResult:
            return AgentResult(
                status="completed",
                response="restart-safe-result",
                total_tokens=12,
                total_cost_usd=0.002,
                turns=2,
            )

        async def fail_live_delivery(_: object) -> object:
            raise AgentJobError("injected post-commit gap")

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(
            first,
            "_deliver_execution_publication",
            fail_live_delivery,
        )
        result = await first.delegate(
            SubTask("restart-publication", "work", "coder")
        )
        assert result.status == "completed"
        first_record = next(
            item for item in first.list_executions()
            if item.task_id == "restart-publication"
        )
        assert first_record.worker_job_failure_code == (
            "agent_job_publication_delivery_failed"
        )
        pending = await first._agent_job_store.get_job_publication(
            first_record.worker_job_id,
        )
        assert pending is not None
        assert pending.state is AgentJobPublicationState.PENDING

        restarted = SubAgentManager(
            AgentEngine(AppConfig()),
            agent_job_store=AgentJobStore(path),
        )
        summary = await restarted.recover_pending_publications()

        assert summary.scanned == 1
        assert summary.delivered == 1
        assert summary.failed == 0
        assert summary.notification_failures == 0
        assert restarted.publication_recovery_status() == {
            "scanned": 1,
            "delivered": 1,
            "notification_failures": 0,
            "failed": 0,
            "failure_codes": [],
        }
        delivered = await restarted._agent_job_store.get_publication(
            pending.publication_id,
        )
        assert delivered is not None
        assert delivered.state is AgentJobPublicationState.PUBLISHED
        inbox = await restarted._agent_job_store.list_result_inbox("")
        assert len(inbox) == 1
        content = await restarted._agent_job_store.recover_delivered_result(
            inbox[0].delivery_id,
            expected_delivery_sha256=inbox[0].delivery_sha256,
        )
        assert content.terminal_payload.response == "restart-safe-result"
        history = restarted.message_bus.get_history(
            topic="task.restart-publication.completed",
        )
        assert len(history) == 1
        assert history[0].metadata["delivery_id"] == inbox[0].delivery_id

    @pytest.mark.asyncio
    async def test_two_runtime_managers_share_durable_capacity_fifo(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=1,
        ))
        path = tmp_path / "shared-agent-jobs.db"
        first = SubAgentManager(
            AgentEngine(config),
            agent_job_store=AgentJobStore(path),
        )
        second = SubAgentManager(
            AgentEngine(config),
            agent_job_store=AgentJobStore(path),
        )
        first_agent = first.get_agent("coder")
        second_agent = second.get_agent("coder")
        assert first_agent is not None
        assert second_agent is not None
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def execute_first(**_: object) -> AgentResult:
            first_started.set()
            await release_first.wait()
            return AgentResult(status="completed", response="first")

        async def execute_second(**_: object) -> AgentResult:
            second_started.set()
            return AgentResult(status="completed", response="second")

        monkeypatch.setattr(first_agent, "execute", execute_first)
        monkeypatch.setattr(second_agent, "execute", execute_second)
        first_task = asyncio.create_task(first.delegate(
            SubTask("shared-first", "work", "coder")
        ))
        second_task: asyncio.Task[AgentResult] | None = None
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second_task = asyncio.create_task(second.delegate(
                SubTask("shared-second", "work", "coder")
            ))
            for _ in range(100):
                active = [
                    item for item in second.list_executions()
                    if item.task_id == "shared-second"
                ]
                if active and active[0].phase == "waiting_capacity":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("第二个 Runtime 未进入持久 capacity 等待。")
            assert not second_started.is_set()
            snapshot = await second.capacity_snapshot()
            assert snapshot is not None
            assert (snapshot.active_jobs, snapshot.waiting_jobs) == (1, 1)

            release_first.set()
            assert (await asyncio.wait_for(first_task, timeout=2)).response == "first"
            assert (await asyncio.wait_for(second_started.wait(), timeout=2)) is True
            assert second_task is not None
            assert (await asyncio.wait_for(second_task, timeout=2)).response == "second"
            snapshot = await first.capacity_snapshot()
            assert snapshot is not None
            assert (snapshot.active_jobs, snapshot.waiting_jobs) == (0, 0)
        finally:
            release_first.set()
            for pending in (first_task, second_task):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *[
                    pending
                    for pending in (first_task, second_task)
                    if pending is not None
                ],
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    async def test_stop_waiting_capacity_cancels_durable_job_without_model_call(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = AppConfig(safety=SafetyConfig(
            max_parallel_agents=1,
            max_queued_agents=1,
        ))
        path = tmp_path / "shared-agent-jobs.db"
        running_manager = SubAgentManager(
            AgentEngine(config),
            agent_job_store=AgentJobStore(path),
        )
        waiting_manager = SubAgentManager(
            AgentEngine(config),
            agent_job_store=AgentJobStore(path),
        )
        running_agent = running_manager.get_agent("coder")
        waiting_agent = waiting_manager.get_agent("coder")
        assert running_agent is not None
        assert waiting_agent is not None
        running_started = asyncio.Event()
        release_running = asyncio.Event()
        waiting_called = False

        async def running_execute(**_: object) -> AgentResult:
            running_started.set()
            await release_running.wait()
            return AgentResult(status="completed")

        async def waiting_execute(**_: object) -> AgentResult:
            nonlocal waiting_called
            waiting_called = True
            return AgentResult(status="completed")

        monkeypatch.setattr(running_agent, "execute", running_execute)
        monkeypatch.setattr(waiting_agent, "execute", waiting_execute)
        running = asyncio.create_task(running_manager.delegate(
            SubTask("capacity-running", "work", "coder")
        ))
        waiting: asyncio.Task[AgentResult] | None = None
        try:
            await asyncio.wait_for(running_started.wait(), timeout=1)
            waiting = asyncio.create_task(waiting_manager.delegate(
                SubTask("capacity-waiting", "work", "coder")
            ))
            for _ in range(100):
                records = [
                    item for item in waiting_manager.list_executions()
                    if item.task_id == "capacity-waiting"
                ]
                if records and records[0].phase == "waiting_capacity":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("等待任务未进入 durable capacity queue。")
            stopped = await waiting_manager.stop_execution(
                "capacity-waiting",
                "用户取消持久等待。",
            )
            assert stopped.accepted
            assert waiting is not None
            result = await asyncio.wait_for(waiting, timeout=1)
            assert result.status == "cancelled"
            assert waiting_called is False
            record = next(
                item for item in waiting_manager.list_executions()
                if item.task_id == "capacity-waiting"
            )
            assert record.worker_job_state == AgentJobState.CANCELLED.value
            stored = await waiting_manager._agent_job_store.get(
                record.worker_job_id
            )
            assert stored is not None
            assert stored.state is AgentJobState.CANCELLED
            snapshot = await waiting_manager.capacity_snapshot()
            assert snapshot is not None
            assert snapshot.waiting_jobs == 0
        finally:
            release_running.set()
            for pending in (running, waiting):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *[
                    pending
                    for pending in (running, waiting)
                    if pending is not None
                ],
                return_exceptions=True,
            )

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
        stored = await manager._agent_job_store.get(record.worker_job_id)
        assert stored is not None
        assert stored.state is AgentJobState.CANCELLED

    @pytest.mark.asyncio
    async def test_missing_runtime_key_fails_closed_before_model_call(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        called = False

        async def execute(**_: object) -> AgentResult:
            nonlocal called
            called = True
            return AgentResult(status="completed", response="must-not-run")

        monkeypatch.setattr(agent, "execute", execute)
        manager._agent_job_store = AgentJobStore(
            tmp_path / "missing-key.db",
            key_provider=lambda: b"invalid",
        )

        result = await manager.delegate(
            SubTask("missing-runtime-key", "work", "coder")
        )
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "missing-runtime-key"
        )

        assert result.status == "error"
        assert "naumi runtime-key init" in (result.error or "")
        assert called is False
        assert record.worker_job_id == ""
        assert record.worker_job_failure_code == "agent_job_key_unavailable"

    @pytest.mark.asyncio
    async def test_start_fence_failure_blocks_model_call(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        called = False

        async def execute(**_: object) -> AgentResult:
            nonlocal called
            called = True
            return AgentResult(status="completed", response="must-not-run")

        async def fail_mark_running(*args: object, **kwargs: object) -> object:
            raise AgentJobError("fenced")

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(
            manager._agent_job_store,
            "mark_running",
            fail_mark_running,
        )

        result = await manager.delegate(
            SubTask("start-fenced", "work", "coder")
        )
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "start-fenced"
        )

        assert result.status == "error"
        assert "start fence" in (result.error or "")
        assert called is False
        assert record.worker_job_state == AgentJobState.CLAIMED.value
        assert record.worker_claim_epoch == 1
        assert record.worker_job_failure_code == "agent_job_start_fenced"

    @pytest.mark.asyncio
    async def test_claim_renewal_failure_cancels_and_commits_terminal_state(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        hook_entered = asyncio.Event()
        release_hook = asyncio.Event()
        called = False

        async def execute(**_: object) -> AgentResult:
            nonlocal called
            called = True
            return AgentResult(status="completed", response="must-not-return")

        async def fail_renew(*args: object, **kwargs: object) -> object:
            raise AgentJobError("lease lost")

        async def slow_hook(context: object) -> None:
            point = getattr(context, "point", None)
            if str(point) == "delegate_start":
                hook_entered.set()
                await release_hook.wait()

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(manager._hooks, "fire", slow_hook)
        monkeypatch.setattr(
            manager._agent_job_store,
            "renew_claim",
            fail_renew,
        )
        monkeypatch.setattr(
            "naumi_agent.orchestrator.subagent_manager."
            "_AGENT_JOB_RENEW_INTERVAL_SECONDS",
            0.01,
        )

        delegated = asyncio.create_task(
            manager.delegate(SubTask("renewal-failed", "work", "coder"))
        )
        await asyncio.wait_for(hook_entered.wait(), timeout=1)
        for _ in range(100):
            active = next(
                item for item in manager.list_executions()
                if item.task_id == "renewal-failed"
            )
            if active.worker_job_failure_code:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("AgentJob 续租失败未在预期时间内进入停止状态。")
        release_hook.set()
        result = await asyncio.wait_for(delegated, timeout=1)
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "renewal-failed"
        )

        assert result.status == "cancelled"
        assert "claim 续期失败" in (result.error or "")
        assert called is False
        assert record.worker_job_state == AgentJobState.CANCELLED.value
        assert (
            record.worker_job_failure_code
            == "agent_job_claim_renewal_failed"
        )
        stored = await manager._agent_job_store.get(record.worker_job_id)
        assert stored is not None
        assert stored.state is AgentJobState.CANCELLED

    @pytest.mark.asyncio
    async def test_terminal_commit_failure_isolates_model_output(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        attempts = 0

        async def execute(**_: object) -> AgentResult:
            return AgentResult(
                status="completed",
                response="sensitive uncommitted result",
                total_tokens=7,
                turns=1,
            )

        async def fail_finish(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            raise AgentJobError("disk unavailable")

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(manager._agent_job_store, "finish", fail_finish)

        result = await manager.delegate(
            SubTask("terminal-commit-failed", "work", "coder")
        )
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "terminal-commit-failed"
        )

        assert attempts == 2
        assert result.status == "error"
        assert result.response == ""
        assert "持久终态提交失败" in (result.error or "")
        assert record.worker_result_sha256
        assert record.worker_job_state == AgentJobState.RUNNING.value
        assert (
            record.worker_job_failure_code
            == "agent_job_terminal_commit_failed"
        )

    @pytest.mark.asyncio
    async def test_terminal_payload_recovery_failure_isolates_committed_output(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        attempts = 0

        async def execute(**_: object) -> AgentResult:
            return AgentResult(
                status="completed",
                response="committed but unavailable result",
                total_tokens=7,
                turns=1,
            )

        async def fail_recovery(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            raise AgentJobError("key backend unavailable")

        monkeypatch.setattr(agent, "execute", execute)
        monkeypatch.setattr(
            manager._agent_job_store,
            "recover_terminal_payload",
            fail_recovery,
        )

        result = await manager.delegate(
            SubTask("terminal-recovery-failed", "work", "coder")
        )
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "terminal-recovery-failed"
        )

        assert attempts == 2
        assert result.status == "error"
        assert result.response == ""
        assert "安全隔离" in (result.error or "")
        assert record.worker_result_sha256
        assert record.worker_job_state == AgentJobState.COMPLETED.value
        assert (
            record.worker_job_failure_code
            == "agent_job_terminal_payload_recovery_failed"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("terminal_event", "terminal_payload"),
        [
            ("tool_end", {"tool_name": "file_read"}),
            ("tool_error", {}),
        ],
    )
    async def test_execution_observes_tool_progress_without_swallowing_callback(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
        terminal_event: str,
        terminal_payload: dict[str, object],
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        tool_seen = asyncio.Event()
        tool_finished = asyncio.Event()
        release = asyncio.Event()
        finish = asyncio.Event()
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
            await event_callback(terminal_event, terminal_payload)
            tool_finished.set()
            await finish.wait()
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
        assert len(active.worker_request_sha256) == 64
        assert active.worker_result_sha256 == ""
        assert "file_read" in active.worker_tool_scope
        assert [item for item in forwarded if item[0] == "tool_start"] == [
            ("tool_start", {"tool_name": "file_read"})
        ]
        release.set()
        await asyncio.wait_for(tool_finished.wait(), timeout=1)
        active = next(
            item for item in manager.list_executions()
            if item.task_id == "tool-progress"
        )
        assert active.phase == "running"
        assert active.current_tool == ""
        assert active.recent_tools == ("file_read",)
        assert [item for item in forwarded if item[0] == terminal_event] == [
            (terminal_event, terminal_payload)
        ]
        finish.set()
        assert (await delegated).status == "completed"
        terminal = next(
            item for item in manager.list_executions()
            if item.task_id == "tool-progress"
        )
        assert len(terminal.worker_result_sha256) == 64

    @pytest.mark.asyncio
    async def test_invalid_worker_request_contract_blocks_before_model_call(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager.spawn(AgentConfig(
            name="invalid-contract",
            description="invalid",
            capabilities=[],
            model_tier="unsupported",
        ))
        agent = manager.get_agent("invalid-contract")
        assert agent is not None
        called = False

        async def execute(**_: object) -> AgentResult:
            nonlocal called
            called = True
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", execute)
        result = await manager.delegate(
            SubTask("invalid-worker-contract", "work", "invalid-contract")
        )

        assert result.status == "error"
        assert "模型调用前安全拒绝" in (result.error or "")
        assert called is False
        assert all(
            item.task_id != "invalid-worker-contract"
            for item in manager.list_executions()
        )

    @pytest.mark.asyncio
    async def test_invalid_terminal_metrics_are_visible_contract_degradation(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None

        async def execute(**_: object) -> AgentResult:
            return AgentResult(
                status="completed",
                response="done",
                total_tokens=-1,
            )

        monkeypatch.setattr(agent, "execute", execute)
        result = await manager.delegate(
            SubTask("invalid-worker-result", "work", "coder")
        )
        record = next(
            item for item in manager.list_executions()
            if item.task_id == "invalid-worker-result"
        )

        assert result.status == "error"
        assert result.response == ""
        assert "持久终态提交失败" in (result.error or "")
        assert record.worker_result_sha256 == ""
        assert (
            record.worker_contract_failure_code
            == "agent_worker_result_invalid"
        )
        assert (
            record.worker_job_failure_code
            == "agent_job_terminal_receipt_invalid"
        )

    @pytest.mark.asyncio
    async def test_delegate_rejects_unknown_child_runtime_event_before_forwarding(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        forwarded: list[str] = []

        async def invalid_execute(*, event_callback: object, **_: object) -> AgentResult:
            assert callable(event_callback)
            await event_callback("invented_child_event", {"value": 1})
            return AgentResult(status="completed")

        async def callback(event: str, _: dict[str, object]) -> None:
            forwarded.append(event)

        monkeypatch.setattr(agent, "execute", invalid_execute)
        result = await manager.delegate(
            SubTask("invalid-event", "inspect", "coder"),
            event_callback=callback,
        )

        assert result.status == "error"
        assert "未知 Runtime 事件" in str(result.error)
        assert "invented_child_event" not in forwarded

    @pytest.mark.asyncio
    async def test_delegate_events_share_parent_publisher_sequence(
        self,
        manager: SubAgentManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = manager.get_agent("coder")
        assert agent is not None
        events: list[RuntimeEvent] = []

        class RecordingSink:
            async def emit(self, event: RuntimeEvent) -> None:
                events.append(event)

        async def tool_execute(*, event_callback: object, **_: object) -> AgentResult:
            assert callable(event_callback)
            await event_callback("tool_start", {"tool_name": "file_read"})
            return AgentResult(status="completed", turns=1)

        monkeypatch.setattr(agent, "execute", tool_execute)
        publisher = RuntimeEventPublisher(
            RecordingSink(),
            session_id="session-parent",
            run_id="run-parent",
        )

        result = await manager.delegate(
            SubTask("typed-events", "inspect", "coder"),
            event_callback=publisher.legacy_callback(),
        )

        assert result.status == "completed"
        assert [event.type for event in events] == [
            RuntimeEventType.SUBAGENT_EVENT,
            RuntimeEventType.TOOL_START,
            RuntimeEventType.SUBAGENT_EVENT,
        ]
        assert [event.sequence for event in events] == [1, 2, 3]
        assert {event.session_id for event in events} == {"session-parent"}
        assert {event.run_id for event in events} == {"run-parent"}

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
