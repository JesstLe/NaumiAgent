"""Harness context assembly tests."""

from __future__ import annotations

import time

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.context_assembly import (
    HARNESS_CONTEXT_MARKER,
    is_harness_context_message,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.pursuit import PursuitRun, PursuitRunStatus


@pytest.fixture
async def engine(tmp_path) -> AgentEngine:
    config = AppConfig(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "chroma"),
        ),
        workspace_root=str(tmp_path),
    )
    agent = AgentEngine(config)
    try:
        session = await agent.get_or_create_session()
        agent.task_store.set_session(session.id)
        yield agent
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_harness_context_snapshot_includes_live_state(engine: AgentEngine) -> None:
    await engine.task_store.create_task("整理 hooks 优化方案")
    engine.scheduler_runner.create(
        kind="once",
        expression="2999-01-01T00:00:00+00:00",
        prompt="复查长期任务",
    )
    now = time.time()
    engine.pursuit_store.save_run(PursuitRun(
        id="pursuit_ctx",
        goal="完成上下文快照",
        status=PursuitRunStatus.RUNNING,
        phase="assess",
        started_at=now,
        updated_at=now,
        criteria_total=2,
        criteria_verified=1,
    ))

    await engine._inject_harness_context_snapshot()
    snapshot = engine._messages[-1]

    assert is_harness_context_message(snapshot)
    content = snapshot["content"]
    assert HARNESS_CONTEXT_MARKER in content
    assert "## Harness 状态快照" in content
    assert "### 工具池" in content
    assert "整理 hooks 优化方案" in content
    assert "复查长期任务" in content
    assert "pursuit_ctx" in content
    assert "完成上下文快照" in content


@pytest.mark.asyncio
async def test_harness_context_snapshot_replaces_previous_without_persisting(
    engine: AgentEngine,
) -> None:
    engine._messages = [
        {"role": "system", "content": "base"},
        {"role": "system", "content": f"{HARNESS_CONTEXT_MARKER}\nold"},
        {"role": "user", "content": "hello"},
    ]
    engine._full_history = list(engine._messages)

    await engine._inject_harness_context_snapshot()
    await engine._inject_harness_context_snapshot()

    active_snapshots = [
        item for item in engine._messages
        if is_harness_context_message(item)
    ]
    persisted_snapshots = [
        item for item in engine._full_history
        if is_harness_context_message(item)
    ]

    assert len(active_snapshots) == 1
    assert active_snapshots[0]["content"] != f"{HARNESS_CONTEXT_MARKER}\nold"
    assert len(persisted_snapshots) == 1
    assert persisted_snapshots[0]["content"] == f"{HARNESS_CONTEXT_MARKER}\nold"
