from __future__ import annotations

import aiosqlite
import pytest

from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    ParallelMode,
    RiskLevel,
)
from naumi_agent.workbench.store import WorkbenchStore


@pytest.fixture
def store(tmp_path) -> WorkbenchStore:
    return WorkbenchStore(str(tmp_path / "workbench.db"))


async def _set_approval_timestamps(
    store: WorkbenchStore,
    approval_id: str,
    *,
    created_at: str,
    updated_at: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            """UPDATE workbench_approvals
               SET created_at = ?, updated_at = ?
               WHERE id = ?""",
            (created_at, updated_at, approval_id),
        )
        await db.commit()


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
async def test_add_and_resolve_approval_round_trip(store: WorkbenchStore) -> None:
    approval = await store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="  允许重构 core 模块  ",
        detail="  重构后保持现有测试通过  ",
        requester="  Agent-A  ",
    )

    assert approval.session_id == "s"
    assert approval.mission_id == "mission-1"
    assert approval.task_id == "task-1"
    assert approval.title == "允许重构 core 模块"
    assert approval.detail == "重构后保持现有测试通过"
    assert approval.requester == "Agent-A"
    assert approval.state == ApprovalState.WAITING
    assert approval.reviewer == ""
    assert approval.decision_note == ""

    resolved = await store.resolve_approval(
        session_id="s",
        approval_id=approval.id,
        state=ApprovalState.APPROVED,
        reviewer="  Human  ",
        decision_note="  同意，但需补充回归测试  ",
    )
    assert resolved is not None
    assert resolved.id == approval.id
    assert resolved.state == ApprovalState.APPROVED
    assert resolved.reviewer == "Human"
    assert resolved.decision_note == "同意，但需补充回归测试"
    assert resolved.updated_at >= approval.updated_at


@pytest.mark.asyncio
async def test_resolve_approval_only_matches_same_session(store: WorkbenchStore) -> None:
    approval = await store.add_approval(
        session_id="s1",
        mission_id="mission-1",
        task_id="task-1",
        title="请求审批",
        detail="详情",
        requester="Agent-A",
    )

    result = await store.resolve_approval(
        session_id="s2",
        approval_id=approval.id,
        state=ApprovalState.REJECTED,
        reviewer="Human",
        decision_note="",
    )
    assert result is None

    unchanged = await store.resolve_approval(
        session_id="s1",
        approval_id=approval.id,
        state=ApprovalState.APPROVED,
        reviewer="Human",
        decision_note="",
    )
    assert unchanged is not None
    assert unchanged.state == ApprovalState.APPROVED


@pytest.mark.asyncio
async def test_list_approvals_filters_by_session_state_and_orders_newest_first(
    store: WorkbenchStore,
) -> None:
    waiting_s = await store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="等待审批 A",
        detail="详情 A",
        requester="Agent-A",
    )
    other_s = await store.add_approval(
        session_id="s2",
        mission_id="mission-1",
        task_id="task-1",
        title="其他会话",
        detail="详情",
        requester="Agent-A",
    )
    approved_s = await store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-2",
        title="已批准",
        detail="详情",
        requester="Agent-B",
        state=ApprovalState.APPROVED,
    )
    waiting_s2 = await store.add_approval(
        session_id="s",
        mission_id="mission-2",
        task_id="task-3",
        title="等待审批 B",
        detail="详情 B",
        requester="Agent-C",
    )
    await _set_approval_timestamps(
        store,
        waiting_s.id,
        created_at="2026-06-27T08:00:00",
        updated_at="2026-06-27T08:00:00",
    )
    await _set_approval_timestamps(
        store,
        other_s.id,
        created_at="2026-06-27T08:01:00",
        updated_at="2026-06-27T08:01:00",
    )
    await _set_approval_timestamps(
        store,
        approved_s.id,
        created_at="2026-06-27T08:02:00",
        updated_at="2026-06-27T08:02:00",
    )
    await _set_approval_timestamps(
        store,
        waiting_s2.id,
        created_at="2026-06-27T08:03:00",
        updated_at="2026-06-27T08:03:00",
    )

    all_s = await store.list_approvals("s", limit=50)
    assert [a.id for a in all_s] == [waiting_s2.id, approved_s.id, waiting_s.id]

    waiting_only = await store.list_approvals("s", state=ApprovalState.WAITING, limit=50)
    assert [a.id for a in waiting_only] == [waiting_s2.id, waiting_s.id]
    assert all(a.state == ApprovalState.WAITING for a in waiting_only)

    approved_only = await store.list_approvals("s", state=ApprovalState.APPROVED, limit=50)
    assert [a.id for a in approved_only] == [approved_s.id]

    other_session = await store.list_approvals("s2", state=ApprovalState.WAITING, limit=50)
    assert len(other_session) == 1
    assert other_session[0].session_id == "s2"

    limited = await store.list_approvals("s", state=ApprovalState.WAITING, limit=1)
    assert [a.id for a in limited] == [waiting_s2.id]


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
