"""Harness context assembly tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseManager,
    EvolutionExperimentLeaseStore,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContractIssuer
from naumi_agent.orchestrator.context_assembly import (
    HARNESS_CONTEXT_MARKER,
    HarnessContextAssembler,
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
    engine.goal_store.create("持续完善持久目标能力", session_id="session-context")

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
    assert "### 当前目标" in content
    assert "持续完善持久目标能力" in content
    assert "session-context" in content
    assert "预算：不限 · 已用 $0.0000" in content


def test_engine_composes_experiment_contract_and_worktree_lease_services(
    engine: AgentEngine,
) -> None:
    assert isinstance(
        engine.evolution_experiment_contract_issuer,
        EvolutionExperimentContractIssuer,
    )
    assert isinstance(
        engine.evolution_experiment_lease_store,
        EvolutionExperimentLeaseStore,
    )
    assert isinstance(
        engine.evolution_experiment_lease_manager,
        EvolutionExperimentLeaseManager,
    )
    assert (
        engine.evolution_experiment_lease_manager._worktree_manager
        is engine.worktree_manager
    )


@pytest.mark.asyncio
async def test_harness_context_snapshot_reports_no_unfinished_goal(
    engine: AgentEngine,
) -> None:
    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "### 当前目标" in content
    assert "当前没有未完成目标" in content


@pytest.mark.asyncio
async def test_engine_registers_goal_tools(engine: AgentEngine) -> None:
    assert {
        "goal_create",
        "goal_status",
        "goal_list",
        "goal_update",
        "goal_pursue",
    }.issubset(engine.tool_registry.names)


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


@pytest.mark.asyncio
async def test_harness_context_snapshot_includes_trusted_local_time(
    engine: AgentEngine,
) -> None:
    fixed = datetime(
        2026, 7, 12, 3, 22, 36,
        tzinfo=timezone(timedelta(hours=8), name="Asia/Shanghai"),
    )
    engine._harness_context = HarnessContextAssembler(clock=lambda: fixed)

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "### 当前环境" in content
    assert "当前本地时间：2026-07-12T03:22:36+08:00" in content
    assert "时区：Asia/Shanghai (UTC+08:00)" in content
    assert "可直接回答，无需调用工具或公网 API" in content


@pytest.mark.asyncio
async def test_harness_context_clock_refreshes_each_snapshot(
    engine: AgentEngine,
) -> None:
    times = iter((
        datetime(2026, 7, 12, 3, 22, tzinfo=UTC),
        datetime(2026, 7, 12, 3, 23, tzinfo=UTC),
    ))
    engine._harness_context = HarnessContextAssembler(clock=lambda: next(times))

    await engine._inject_harness_context_snapshot()
    first = engine._messages[-1]["content"]
    await engine._inject_harness_context_snapshot()
    second = engine._messages[-1]["content"]

    assert "2026-07-12T03:22:00+00:00" in first
    assert "2026-07-12T03:23:00+00:00" in second
    assert "2026-07-12T03:22:00+00:00" not in second


@pytest.mark.asyncio
async def test_harness_context_normalizes_naive_clock_to_local_timezone(
    engine: AgentEngine,
) -> None:
    engine._harness_context = HarnessContextAssembler(
        clock=lambda: datetime(2026, 7, 12, 3, 22, 36),
    )

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    time_line = next(
        line for line in content.splitlines()
        if line.startswith("- 当前本地时间：")
    )
    parsed = datetime.fromisoformat(time_line.split("：", 1)[1])

    assert parsed.utcoffset() is not None
    assert "- 时区：" in content
    assert "(UTC+" in content or "(UTC-" in content


@pytest.mark.asyncio
async def test_harness_context_default_clock_tracks_local_time(
    engine: AgentEngine,
) -> None:
    before = datetime.now().astimezone()

    await engine._inject_harness_context_snapshot()

    after = datetime.now().astimezone()
    content = engine._messages[-1]["content"]
    time_line = next(
        line for line in content.splitlines()
        if line.startswith("- 当前本地时间：")
    )
    parsed = datetime.fromisoformat(time_line.split("：", 1)[1])

    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)
    assert parsed.utcoffset() == parsed.astimezone().utcoffset()
