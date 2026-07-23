from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.agents.base import AgentConfig, AgentResult
from naumi_agent.config.settings import AppConfig
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubAgentManager, SubTask
from naumi_agent.runtime.agent_heartbeat import (
    AgentExecutionHeartbeatFactory,
    AgentExecutionHeartbeatState,
)

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 23, tzinfo=UTC)

    def now(self) -> str:
        current = self._value
        self._value += timedelta(seconds=1)
        return current.isoformat()


def _factory(
    tmp_path,
    *,
    store: HarnessStore | None = None,
) -> AgentExecutionHeartbeatFactory:
    return AgentExecutionHeartbeatFactory(
        store=store or HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        interval_seconds=1,
        timeout_seconds=3,
        now_provider=_Clock().now,
        auto_pulse=False,
    )


def _engine(tmp_path) -> AgentEngine:
    return AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory={
            "session_db_path": str(tmp_path / ".naumi" / "sessions.db"),
            "vector_db_path": str(tmp_path / ".naumi" / "chroma"),
            "long_term_enabled": False,
        },
    ))


def test_agent_heartbeat_factory_rejects_invalid_cadence(tmp_path) -> None:
    store = HarnessStore(tmp_path / "invalid.db")
    with pytest.raises(ValueError, match="interval"):
        AgentExecutionHeartbeatFactory(
            store=store,
            workspace_root=tmp_path,
            interval_seconds=3,
            timeout_seconds=3,
        )
    with pytest.raises(ValueError, match="timeout"):
        AgentExecutionHeartbeatFactory(
            store=store,
            workspace_root=tmp_path,
            timeout_seconds=True,
        )
    with pytest.raises(TypeError, match="auto_pulse"):
        AgentExecutionHeartbeatFactory(
            store=store,
            workspace_root=tmp_path,
            auto_pulse=1,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_agent_lifecycle_persists_terminal_status_and_advances_epoch(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    first = await factory.create(
        session_id="session-1",
        task_id="task-1",
        agent_name="coder",
    )

    assert await first.start() is True
    assert await first.finish("completed") is True
    assert await first.finish("completed") is False
    first_snapshot = first.snapshot()
    assert first_snapshot.state is AgentExecutionHeartbeatState.STOPPED
    assert first_snapshot.phase == "stopped"

    persisted = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.AGENT,
        subject_id=first_snapshot.subject_id,
    )
    assert persisted is not None
    assert persisted.epoch == 1
    assert persisted.sequence == 4
    assert persisted.phase is HarnessHeartbeatPhase.STOPPED
    assert persisted.detail_code == "agent_completed"

    second = await factory.create(
        session_id="session-1",
        task_id="task-1",
        agent_name="coder",
    )
    assert second.snapshot().subject_id == first_snapshot.subject_id
    assert second.snapshot().instance_id != first_snapshot.instance_id
    assert second.snapshot().epoch == 2
    await second.start()
    await second.finish("cancelled")
    replay = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="agent",
        subject_id=second.snapshot().subject_id,
    )
    assert replay is not None
    assert replay.epoch == 2
    assert replay.detail_code == "agent_cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_status", "detail_code"),
    [
        ("error", "agent_failed"),
        ("failed", "agent_failed"),
        ("timeout", "agent_timeout"),
        ("max_turns", "agent_max_turns"),
    ],
)
async def test_agent_lifecycle_maps_unsuccessful_results_to_failed(
    tmp_path,
    result_status: str,
    detail_code: str,
) -> None:
    factory = _factory(tmp_path)
    lifecycle = await factory.create(
        session_id="session-failed",
        task_id=f"task-{result_status}",
        agent_name="coder",
    )
    await lifecycle.start()

    assert await lifecycle.finish(result_status) is True
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="agent",
        subject_id=lifecycle.snapshot().subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED
    assert heartbeat.detail_code == detail_code


@pytest.mark.asyncio
async def test_manager_delegation_exposes_and_persists_agent_heartbeat(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(tmp_path)
    manager = SubAgentManager(engine, heartbeat_factory=factory)
    agent = manager.get_agent("coder")
    assert agent is not None

    async def complete_execute(**_: object) -> AgentResult:
        return AgentResult(status="completed", response="done", turns=2)

    monkeypatch.setattr(agent, "execute", complete_execute)
    result = await manager.delegate(SubTask("durable-agent", "work", "coder"))

    assert result.status == "completed"
    record = next(
        item for item in manager.list_executions()
        if item.task_id == "durable-agent"
    )
    assert record.heartbeat_subject_id.startswith("agent-execution-")
    assert record.heartbeat_phase == "stopped"
    assert record.heartbeat_failure_code == ""
    heartbeat = await HarnessStore(factory.store.db_path).get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="agent",
        subject_id=record.heartbeat_subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.detail_code == "agent_completed"
    await engine.shutdown()


@pytest.mark.asyncio
async def test_manager_user_stop_persists_cancelled_without_touching_peer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(tmp_path)
    manager = SubAgentManager(engine, heartbeat_factory=factory)
    manager.spawn(AgentConfig(
        name="blocking",
        description="blocking agent",
        capabilities=[],
        timeout_seconds=60,
    ))
    agent = manager.get_agent("blocking")
    assert agent is not None
    started = asyncio.Event()

    async def blocking_execute(**_: object) -> AgentResult:
        started.set()
        await asyncio.Event().wait()
        return AgentResult(status="completed")

    monkeypatch.setattr(agent, "execute", blocking_execute)
    delegated = asyncio.create_task(manager.delegate(
        SubTask("cancel-heartbeat", "wait", "blocking")
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    active = next(
        item for item in manager.list_executions()
        if item.task_id == "cancel-heartbeat"
    )
    assert active.heartbeat_phase == "running"
    assert (await manager.stop_execution("cancel-heartbeat")).accepted
    assert (await asyncio.wait_for(delegated, timeout=1)).status == "cancelled"

    terminal = next(
        item for item in manager.list_executions()
        if item.task_id == "cancel-heartbeat"
    )
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="agent",
        subject_id=terminal.heartbeat_subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.STOPPED
    assert heartbeat.detail_code == "agent_cancelled"
    await engine.shutdown()


@pytest.mark.asyncio
async def test_manager_timeout_persists_failed_heartbeat(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(tmp_path)
    manager = SubAgentManager(engine, heartbeat_factory=factory)
    manager.spawn(AgentConfig(
        name="timed",
        description="timed agent",
        capabilities=[],
        timeout_seconds=0.01,
    ))
    agent = manager.get_agent("timed")
    assert agent is not None

    async def blocked_execute(**_: object) -> AgentResult:
        await asyncio.Event().wait()
        return AgentResult(status="completed")

    monkeypatch.setattr(agent, "execute", blocked_execute)
    result = await manager.delegate(SubTask("timeout-heartbeat", "wait", "timed"))

    assert result.status == "timeout"
    record = next(
        item for item in manager.list_executions()
        if item.task_id == "timeout-heartbeat"
    )
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="agent",
        subject_id=record.heartbeat_subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED
    assert heartbeat.detail_code == "agent_timeout"
    await engine.shutdown()


@pytest.mark.asyncio
async def test_parallel_delegations_keep_distinct_durable_subjects(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(tmp_path)
    manager = SubAgentManager(engine, heartbeat_factory=factory)
    agent = manager.get_agent("coder")
    assert agent is not None

    async def complete_execute(*, task: str, **_: object) -> AgentResult:
        await asyncio.sleep(0)
        return AgentResult(status="completed", response=task)

    monkeypatch.setattr(agent, "execute", complete_execute)
    tasks = [SubTask(f"parallel-{index}", f"task {index}", "coder") for index in range(12)]
    results = await manager.execute_parallel(tasks)

    assert all(result.status == "completed" for result in results)
    records = {
        item.task_id: item
        for item in manager.list_executions()
        if item.task_id.startswith("parallel-")
    }
    assert len(records) == 12
    assert len({item.heartbeat_subject_id for item in records.values()}) == 12
    for record in records.values():
        heartbeat = await factory.store.get_heartbeat(
            workspace_root=tmp_path,
            subject_kind="agent",
            subject_id=record.heartbeat_subject_id,
        )
        assert heartbeat is not None
        assert heartbeat.phase is HarnessHeartbeatPhase.STOPPED
    await engine.shutdown()


@pytest.mark.asyncio
async def test_heartbeat_start_failure_degrades_without_changing_agent_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingStore(HarnessStore):
        async def record_heartbeat(self, **kwargs):
            raise OSError("private database path")

    engine = _engine(tmp_path)
    manager = SubAgentManager(
        engine,
        heartbeat_factory=_factory(
            tmp_path,
            store=FailingStore(tmp_path / "failing.db"),
        ),
    )
    agent = manager.get_agent("coder")
    assert agent is not None

    async def complete_execute(**_: object) -> AgentResult:
        return AgentResult(status="completed", response="still done")

    monkeypatch.setattr(agent, "execute", complete_execute)
    result = await manager.delegate(SubTask("degraded-heartbeat", "work", "coder"))

    assert result.status == "completed"
    record = next(
        item for item in manager.list_executions()
        if item.task_id == "degraded-heartbeat"
    )
    assert record.heartbeat_phase == ""
    assert record.heartbeat_failure_code == "agent_heartbeat_start_failed"
    assert "private" not in record.heartbeat_failure_code
    await engine.shutdown()
