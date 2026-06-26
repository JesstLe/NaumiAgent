from __future__ import annotations

import pytest

from naumi_agent.workbench.models import ContextHealth, DecisionKind, ParallelMode, RiskLevel
from naumi_agent.workbench.store import WorkbenchStore


@pytest.fixture
def store(tmp_path) -> WorkbenchStore:
    return WorkbenchStore(str(tmp_path / "workbench.db"))


@pytest.mark.asyncio
async def test_create_mission_and_issue_metadata(store: WorkbenchStore) -> None:
    mission = await store.create_mission(
        session_id="s",
        title="构建 Mac 工作台",
        goal="让用户治理多 Agent 研发流程",
    )
    issue = await store.upsert_issue(
        session_id="s",
        task_id="1",
        mission_id=mission.id,
        parallel_mode=ParallelMode.EXCLUSIVE,
        risk_level=RiskLevel.HIGH,
        acceptance_criteria=["必须通过 claim 冲突测试"],
        expected_artifacts=["实现文档", "测试报告"],
    )

    loaded = await store.get_issue("s", "1")
    assert loaded == issue
    assert loaded.risk_level == RiskLevel.HIGH
    assert loaded.acceptance_criteria == ["必须通过 claim 冲突测试"]


@pytest.mark.asyncio
async def test_decision_and_audit_event_are_persisted(store: WorkbenchStore) -> None:
    mission = await store.create_mission("s", "M", "G")
    decision = await store.add_decision(
        session_id="s",
        mission_id=mission.id,
        kind=DecisionKind.ARCHITECTURE,
        title="任务认领必须使用租约",
        content="避免 agent 崩溃后任务永久占用。",
        actor="Human",
    )
    event = await store.append_event(
        session_id="s",
        type="decision.created",
        actor="Human",
        subject_id=decision.id,
        payload={"kind": decision.kind.value},
    )

    assert [d.id for d in await store.list_decisions("s", mission.id)] == [decision.id]
    assert [e.id for e in await store.list_events("s")] == [event.id]


@pytest.mark.asyncio
async def test_intent_locks_round_trip(store: WorkbenchStore) -> None:
    mission = await store.create_mission("s", "M", "G")
    lock = await store.add_intent_lock(
        session_id="s",
        mission_id=mission.id,
        rule="本轮不动 UI",
        blocked_paths=["frontend/"],
        require_proposal_for_risk=RiskLevel.MEDIUM,
    )

    locks = await store.list_intent_locks("s", mission.id)
    assert locks == [lock]


@pytest.mark.asyncio
async def test_list_validation_runs_filters_and_orders(store: WorkbenchStore) -> None:
    run_a = await store.record_validation_run(
        session_id="s",
        task_id="task-a",
        actor="ValidationRunner",
        command=["pytest", "tests/unit/test_a.py"],
        cwd="/workspace/a",
        status="passed",
        exit_code=0,
        output="ok",
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T00:00:01",
    )
    run_b = await store.record_validation_run(
        session_id="s",
        task_id="task-b",
        actor="ValidationRunner",
        command=["pytest", "tests/unit/test_b.py"],
        cwd="/workspace/b",
        status="failed",
        exit_code=1,
        output="error",
        started_at="2024-01-01T00:01:00",
        completed_at="2024-01-01T00:01:01",
    )
    run_c = await store.record_validation_run(
        session_id="s",
        task_id="task-a",
        actor="ValidationRunner",
        command=["pytest", "tests/unit/test_a2.py"],
        cwd="/workspace/a",
        status="passed",
        exit_code=0,
        output="ok",
        started_at="2024-01-01T00:02:00",
        completed_at="2024-01-01T00:02:01",
    )

    all_runs = await store.list_validation_runs("s", limit=50)
    assert [run["id"] for run in all_runs] == [run_a["id"], run_b["id"], run_c["id"]]
    assert all(isinstance(run["command"], list) for run in all_runs)
    for run, stored in zip(all_runs, [run_a, run_b, run_c]):
        assert run["command"] == stored["command"]

    task_a_runs = await store.list_validation_runs("s", task_id="task-a", limit=50)
    assert [run["id"] for run in task_a_runs] == [run_a["id"], run_c["id"]]

    task_b_runs = await store.list_validation_runs("s", task_id="task-b", limit=50)
    assert [run["id"] for run in task_b_runs] == [run_b["id"]]

    limited = await store.list_validation_runs("s", limit=1)
    assert [run["id"] for run in limited] == [run_c["id"]]


@pytest.mark.asyncio
async def test_list_context_snapshots_filters_and_returns_reasons(store: WorkbenchStore) -> None:
    snap_a = await store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-a",
        health=ContextHealth.GOOD,
        reasons=["上下文健康"],
    )
    snap_b = await store.record_context_snapshot(
        session_id="s",
        agent_id="agent-2",
        task_id="task-b",
        health=ContextHealth.STALE,
        reasons=["超过 60 分钟未同步上下文"],
    )
    snap_c = await store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-b",
        health=ContextHealth.MISSING,
        reasons=["缺少 mission 目标", "缺少验收标准"],
    )

    all_snapshots = await store.list_context_snapshots("s", limit=50)
    assert {snap["id"] for snap in all_snapshots} == {snap_a["id"], snap_b["id"], snap_c["id"]}
    for snap in all_snapshots:
        assert isinstance(snap["reasons"], list)

    task_a_snaps = await store.list_context_snapshots("s", task_id="task-a", limit=50)
    assert {snap["id"] for snap in task_a_snaps} == {snap_a["id"]}
    assert task_a_snaps[0]["reasons"] == ["上下文健康"]

    agent_2_snaps = await store.list_context_snapshots("s", agent_id="agent-2", limit=50)
    assert {snap["id"] for snap in agent_2_snaps} == {snap_b["id"]}

    combined_snaps = await store.list_context_snapshots(
        "s", task_id="task-b", agent_id="agent-1", limit=50
    )
    assert {snap["id"] for snap in combined_snaps} == {snap_c["id"]}
    assert combined_snaps[0]["reasons"] == ["缺少 mission 目标", "缺少验收标准"]

    limited = await store.list_context_snapshots("s", limit=1)
    assert len(limited) == 1
    assert limited[0]["id"] in {snap_a["id"], snap_b["id"], snap_c["id"]}
