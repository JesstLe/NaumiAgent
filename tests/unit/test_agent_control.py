"""Authoritative Agent Control Center model and service tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from naumi_agent.agent_control import (
    AGENT_CONTROL_SCHEMA_VERSION,
    AgentControlService,
    AgentControlSnapshot,
)
from naumi_agent.agents.base import AgentCapability, AgentConfig, AgentResult
from naumi_agent.agents.message_bus import AgentMessage, MessagePriority
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubTask


def _engine(tmp_path: Path) -> AgentEngine:
    root = tmp_path / "workspace"
    root.mkdir()
    data = tmp_path / "data"
    return AgentEngine(AppConfig(
        workspace_root=str(root),
        memory=MemoryConfig(
            session_db_path=str(data / "sessions.db"),
            vector_db_path=str(data / "vectors"),
            long_term_enabled=False,
        ),
    ))


def test_snapshot_round_trip_is_strict_and_bounded() -> None:
    snapshot = AgentControlSnapshot.empty(session_id="session-1")

    restored = AgentControlSnapshot.from_dict(snapshot.to_dict())

    assert restored == snapshot
    assert restored.schema_version == AGENT_CONTROL_SCHEMA_VERSION
    assert set(restored.to_dict()) == {
        "schema_version",
        "session_id",
        "revision",
        "generated_at",
        "summary",
        "agents",
        "executions",
        "team_messages",
        "blackboard",
        "warnings",
    }

    invalid = snapshot.to_dict()
    invalid["summary"]["total_agents"] = True
    with pytest.raises(ValueError, match="total_agents"):
        AgentControlSnapshot.from_dict(invalid)

    oversized = snapshot.to_dict()
    oversized["agents"] = [
        {
            "name": f"agent-{index}",
            "description": "agent",
            "kind": "dynamic",
            "state": "idle",
        }
        for index in range(101)
    ]
    with pytest.raises(ValueError, match="agents.*100"):
        AgentControlSnapshot.from_dict(oversized)

    unknown = snapshot.to_dict()
    unknown["invented_section"] = {}
    with pytest.raises(ValueError, match="unknown fields"):
        AgentControlSnapshot.from_dict(unknown)


@pytest.mark.asyncio
async def test_service_builds_authoritative_snapshot_and_stable_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    release = asyncio.Event()
    started = asyncio.Event()
    delegated: asyncio.Task[AgentResult] | None = None
    try:
        session = await engine.get_or_create_session(title="Agent Control")
        manager = engine.subagent_manager
        manager.spawn(AgentConfig(
            name="observer",
            description="observes a real execution",
            capabilities=[AgentCapability.FILE_OPS],
            model_tier="fast",
            tools=["file_read"],
            permission_level="strict",
        ))
        agent = manager.get_agent("observer")
        assert agent is not None

        async def blocking_execute(**kwargs: object) -> AgentResult:
            started.set()
            await release.wait()
            return AgentResult(
                status="completed",
                total_tokens=23,
                total_cost_usd=0.01,
                turns=2,
            )

        monkeypatch.setattr(agent, "execute", blocking_execute)
        delegated = asyncio.create_task(manager.delegate(SubTask(
            "execution-1",
            "检查真实执行",
            "observer",
        )))
        await asyncio.wait_for(started.wait(), timeout=1)
        await manager.message_bus.send(AgentMessage(
            sender="coder",
            recipient="observer",
            topic="team.review",
            content="请复核执行证据。",
            priority=MessagePriority.HIGH,
            metadata={"session_id": session.id},
        ))
        await manager.message_bus.blackboard_set(
            "team/decision",
            {"status": "approved", "secret": "x" * 2500},
            "coder",
        )

        first = await engine.agent_control.snapshot()
        second = await engine.agent_control.snapshot()

        assert first.session_id == session.id
        assert first.revision == second.revision
        assert first.summary.total_agents == 4
        assert first.summary.active_agents == 1
        assert first.summary.stoppable_executions == 1
        assert first.summary.pending_messages == 1
        observer = next(item for item in first.agents if item.name == "observer")
        assert observer.kind == "dynamic"
        assert observer.state == "running"
        assert observer.model_tier == "fast"
        assert observer.capabilities == ("file_operations",)
        assert "file_read" in observer.tools
        assert "file_edit" in observer.tools
        coder = next(item for item in first.agents if item.name == "coder")
        assert "file_read" in coder.tools
        assert "bash_run" in coder.tools
        execution = first.executions[0]
        assert execution.task_id == "execution-1"
        assert execution.session_id == session.id
        assert execution.stop_supported is True
        assert first.team_messages[0].topic == "team.review"
        assert first.team_messages[0].priority == "high"
        assert first.blackboard[0].key == "team/decision"
        assert len(first.blackboard[0].value_summary) <= 2000
        assert "x" * 100 not in first.blackboard[0].value_summary

        await manager.message_bus.blackboard_set(
            "team/decision",
            {"status": "changes_requested"},
            "reviewer",
        )
        changed = await engine.agent_control.snapshot()
        assert changed.revision == first.revision + 1
        assert AgentControlService.changed_sections(first, changed) == ("blackboard",)
    finally:
        release.set()
        if delegated is not None:
            await asyncio.gather(delegated, return_exceptions=True)
        await engine.shutdown()


@pytest.mark.asyncio
async def test_service_isolates_execution_and_team_evidence_after_session_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    try:
        first_session = await engine.get_or_create_session(title="first")
        manager = engine.subagent_manager
        agent = manager.get_agent("coder")
        assert agent is not None

        async def complete_execute(**kwargs: object) -> AgentResult:
            return AgentResult(status="completed")

        monkeypatch.setattr(agent, "execute", complete_execute)
        await manager.delegate(SubTask("old-execution", "old", "coder"))
        await manager.message_bus.publish(AgentMessage(
            sender="coder",
            topic="team.old",
            content="old message",
            metadata={"session_id": first_session.id},
        ))
        await manager.message_bus.blackboard_set("team/old", "old", "coder")
        old_snapshot = await engine.agent_control.snapshot()
        assert old_snapshot.executions
        assert old_snapshot.team_messages
        assert old_snapshot.blackboard

        engine._session = await engine.session_store.create_session(title="second")
        new_snapshot = await engine.agent_control.snapshot()

        assert new_snapshot.session_id != first_session.id
        assert new_snapshot.executions == ()
        assert new_snapshot.team_messages == ()
        assert new_snapshot.blackboard == ()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_service_keeps_team_data_when_agent_source_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    try:
        session = await engine.get_or_create_session(title="partial failure")
        await engine.subagent_manager.message_bus.publish(AgentMessage(
            sender="coder",
            topic="team.notice",
            content="still visible",
            metadata={"session_id": session.id},
        ))
        monkeypatch.setattr(
            engine.subagent_manager,
            "list_agents",
            lambda: (_ for _ in ()).throw(RuntimeError("agent source down")),
        )

        snapshot = await engine.agent_control.snapshot()

        assert snapshot.agents == ()
        assert snapshot.team_messages[0].content == "still visible"
        assert any("Agent 数据读取失败" in item for item in snapshot.warnings)
    finally:
        await engine.shutdown()
