from __future__ import annotations

import aiosqlite
import pytest

from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    FailureKind,
    LeaseState,
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


async def _set_failure_timestamps(
    store: WorkbenchStore,
    failure_id: str,
    *,
    created_at: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE workbench_failures SET created_at = ? WHERE id = ?",
            (created_at, failure_id),
        )
        await db.commit()


async def _set_failure_status(
    store: WorkbenchStore,
    failure_id: str,
    *,
    status: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE workbench_failures SET status = ? WHERE id = ?",
            (status, failure_id),
        )
        await db.commit()


async def _set_issue_timestamps(
    store: WorkbenchStore,
    session_id: str,
    task_id: str,
    *,
    created_at: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            """UPDATE workbench_issues
               SET created_at = ?, updated_at = ?
               WHERE session_id = ? AND task_id = ?""",
            (created_at, created_at, session_id, task_id),
        )
        await db.commit()


async def _set_lease_timestamps(
    store: WorkbenchStore,
    lease_id: str,
    *,
    created_at: str,
    updated_at: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            """UPDATE workbench_leases
               SET created_at = ?, updated_at = ?
               WHERE id = ?""",
            (created_at, updated_at, lease_id),
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
async def test_agent_profiles_round_trip_filter_and_update(store: WorkbenchStore) -> None:
    profile = await store.upsert_agent_profile(
        session_id="s",
        agent_id=" Agent-A ",
        name=" Backend Agent ",
        role=" coder ",
        capabilities=[" code ", "", "test"],
        permissions=["read", " write "],
        max_parallel_tasks=2,
        status="idle",
    )
    await store.upsert_agent_profile(
        session_id="s",
        agent_id="agent-b",
        name="Reviewer",
        role="reviewer",
        status="busy",
    )
    await store.upsert_agent_profile(
        session_id="other",
        agent_id="Agent-A",
        name="Other",
        role="coder",
    )

    loaded = await store.get_agent_profile("s", "Agent-A")
    assert loaded == profile
    assert loaded.name == "Backend Agent"
    assert loaded.role == "coder"
    assert loaded.capabilities == ["code", "test"]
    assert loaded.permissions == ["read", "write"]
    assert loaded.max_parallel_tasks == 2

    busy = await store.list_agent_profiles("s", status="busy", limit=50)
    assert [item.id for item in busy] == ["agent-b"]

    updated = await store.upsert_agent_profile(
        session_id="s",
        agent_id="Agent-A",
        name="Backend Agent",
        role="coder",
        status="busy",
    )
    assert updated.created_at == profile.created_at
    assert updated.status == "busy"

    session_profiles = await store.list_agent_profiles("s", limit=50)
    assert {item.id for item in session_profiles} == {"Agent-A", "agent-b"}


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
    policy_decision = await store.add_decision(
        session_id="s",
        mission_id=mission.id,
        kind=DecisionKind.POLICY,
        title="高风险变更需要审批",
        content="风险等级 high 以上必须走人工审批。",
        actor="Human",
    )

    assert [d.id for d in await store.list_decisions("s", mission.id)] == [
        decision.id,
        policy_decision.id,
    ]
    policy_only = await store.list_decisions("s", mission.id, kind=DecisionKind.POLICY)
    assert [d.id for d in policy_only] == [policy_decision.id]
    assert [e.id for e in await store.list_events("s")] == [event.id]


@pytest.mark.asyncio
async def test_get_decision_returns_matching_session_and_mission_decision(
    store: WorkbenchStore,
) -> None:
    decision = await store.add_decision(
        session_id="s",
        mission_id="mission-1",
        kind=DecisionKind.POLICY,
        title="采用策略 A",
        content="内容 A",
        actor="Planner-Agent",
    )
    _other_mission_decision = await store.add_decision(
        session_id="s",
        mission_id="mission-2",
        kind=DecisionKind.POLICY,
        title="其他 mission",
        content="内容 B",
        actor="Planner-Agent",
    )
    _other_session_decision = await store.add_decision(
        session_id="other",
        mission_id="mission-1",
        kind=DecisionKind.POLICY,
        title="其他 session",
        content="内容 C",
        actor="Planner-Agent",
    )

    found = await store.get_decision("s", "mission-1", decision.id)

    assert found == decision
    assert await store.get_decision("s", "mission-1", "missing-decision") is None
    assert await store.get_decision("s", "mission-2", decision.id) is None
    assert await store.get_decision("other", "mission-1", decision.id) is None


@pytest.mark.asyncio
async def test_get_event_returns_matching_session_event(store: WorkbenchStore) -> None:
    event = await store.append_event(
        session_id="s",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="task-1",
        payload={"lease_id": "lease-1"},
    )
    _other_session_event = await store.append_event(
        session_id="other",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="task-1",
        payload={"lease_id": "lease-other"},
    )

    found = await store.get_event("s", event.id)

    assert found == event
    assert await store.get_event("s", "missing-event") is None
    assert await store.get_event("other", event.id) is None


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
async def test_get_intent_lock_returns_matching_session_and_mission_lock(
    store: WorkbenchStore,
) -> None:
    mission = await store.create_mission("s", "M1", "G")
    other_mission = await store.create_mission("s", "M2", "G")
    lock = await store.add_intent_lock(
        session_id="s",
        mission_id=mission.id,
        rule="高风险变更必须先提交 proposal",
        blocked_paths=["src/core"],
        allowed_paths=["src/core/README.md"],
        require_proposal_for_risk=RiskLevel.HIGH,
    )
    _other_mission_lock = await store.add_intent_lock(
        session_id="s",
        mission_id=other_mission.id,
        rule="其他 mission 的规则",
    )
    _other_session_lock = await store.add_intent_lock(
        session_id="other",
        mission_id=mission.id,
        rule="其他 session 的规则",
    )

    found = await store.get_intent_lock("s", mission.id, lock.id)

    assert found == lock
    assert await store.get_intent_lock("s", mission.id, "missing-lock") is None
    assert await store.get_intent_lock("s", other_mission.id, lock.id) is None
    assert await store.get_intent_lock("other", mission.id, lock.id) is None


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

    failed_runs = await store.list_validation_runs("s", status="failed", limit=50)
    assert [run["id"] for run in failed_runs] == [run_b["id"]]

    task_a_passed_runs = await store.list_validation_runs(
        "s", task_id="task-a", status="passed", limit=50
    )
    assert [run["id"] for run in task_a_passed_runs] == [run_a["id"], run_c["id"]]

    limited = await store.list_validation_runs("s", limit=1)
    assert [run["id"] for run in limited] == [run_c["id"]]


@pytest.mark.asyncio
async def test_list_failures_filters_by_session_task_status_and_orders_newest_first(
    store: WorkbenchStore,
) -> None:
    # Session s, task-a: open failure (oldest).
    f_open_a = await store.create_failure(
        session_id="s",
        task_id="task-a",
        kind=FailureKind.TEST_FAILED,
        title="测试失败 A",
        detail="detail-a",
        source_id="run-a",
    )
    # Session s, task-b: open failure (middle).
    f_open_b = await store.create_failure(
        session_id="s",
        task_id="task-b",
        kind=FailureKind.AGENT_TIMEOUT,
        title="Agent 超时 B",
        detail="detail-b",
        source_id="run-b",
    )
    # Session s, task-a: resolved failure (newest).
    f_resolved_a = await store.create_failure(
        session_id="s",
        task_id="task-a",
        kind=FailureKind.LEASE_EXPIRED,
        title="租约过期 A",
        detail="detail-resolved",
        source_id="run-c",
    )
    # Different session.
    await store.create_failure(
        session_id="s2",
        task_id="task-a",
        kind=FailureKind.TEST_FAILED,
        title="其他会话失败",
        detail="detail-other",
        source_id="run-other",
    )

    # Pin timestamps deterministically; store orders by created_at DESC.
    await _set_failure_timestamps(
        store, f_open_a["id"], created_at="2026-06-27T06:00:00"
    )
    await _set_failure_timestamps(
        store, f_open_b["id"], created_at="2026-06-27T06:01:00"
    )
    await _set_failure_timestamps(
        store, f_resolved_a["id"], created_at="2026-06-27T06:02:00"
    )
    await _set_failure_status(
        store, f_resolved_a["id"], status="resolved"
    )

    all_failures = await store.list_failures("s", limit=50)
    assert [f["id"] for f in all_failures] == [
        f_resolved_a["id"],
        f_open_b["id"],
        f_open_a["id"],
    ]
    assert all(f["session_id"] == "s" for f in all_failures)

    task_a_failures = await store.list_failures("s", task_id="task-a", limit=50)
    assert [f["id"] for f in task_a_failures] == [
        f_resolved_a["id"],
        f_open_a["id"],
    ]

    open_failures = await store.list_failures("s", status="open", limit=50)
    assert [f["id"] for f in open_failures] == [f_open_b["id"], f_open_a["id"]]

    timeout_failures = await store.list_failures("s", kind="agent_timeout", limit=50)
    assert [f["id"] for f in timeout_failures] == [f_open_b["id"]]

    task_a_open_failures = await store.list_failures(
        "s", task_id="task-a", status="open", kind="test_failed", limit=50
    )
    assert [f["id"] for f in task_a_open_failures] == [f_open_a["id"]]

    limited = await store.list_failures("s", limit=1)
    assert [f["id"] for f in limited] == [f_resolved_a["id"]]

    other_session = await store.list_failures("s2", limit=50)
    assert len(other_session) == 1
    assert other_session[0]["session_id"] == "s2"


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
async def test_get_approval_returns_matching_session_approval(
    store: WorkbenchStore,
) -> None:
    approval = await store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="请求审批",
        detail="详情",
        requester="Agent-A",
    )
    _other_session_approval = await store.add_approval(
        session_id="other",
        mission_id="mission-1",
        task_id="task-1",
        title="其他会话",
        detail="详情",
        requester="Agent-A",
    )

    found = await store.get_approval("s", approval.id)

    assert found == approval
    assert await store.get_approval("s", "missing-approval") is None
    assert await store.get_approval("other", approval.id) is None


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

    mission_1 = await store.list_approvals("s", mission_id="mission-1", limit=50)
    assert [a.id for a in mission_1] == [approved_s.id, waiting_s.id]

    task_1 = await store.list_approvals("s", task_id="task-1", limit=50)
    assert [a.id for a in task_1] == [waiting_s.id]

    mission_1_waiting = await store.list_approvals(
        "s", state=ApprovalState.WAITING, mission_id="mission-1", limit=50
    )
    assert [a.id for a in mission_1_waiting] == [waiting_s.id]

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

    stale_snaps = await store.list_context_snapshots("s", health="stale", limit=50)
    assert {snap["id"] for snap in stale_snaps} == {snap_b["id"]}

    combined_snaps = await store.list_context_snapshots(
        "s", task_id="task-b", agent_id="agent-1", health="missing", limit=50
    )
    assert {snap["id"] for snap in combined_snaps} == {snap_c["id"]}
    assert combined_snaps[0]["reasons"] == ["缺少 mission 目标", "缺少验收标准"]

    limited = await store.list_context_snapshots("s", limit=1)
    assert len(limited) == 1
    assert limited[0]["id"] in {snap_a["id"], snap_b["id"], snap_c["id"]}


@pytest.mark.asyncio
async def test_list_issues_filters_by_session_mission_risk_and_orders_newest_first(
    store: WorkbenchStore,
) -> None:
    mission_s1 = await store.create_mission("s", "Mission S1", "Goal")
    mission_s2 = await store.create_mission("s", "Mission S2", "Goal")
    await store.create_mission("s2", "Mission Other", "Goal")

    issue_high_m1 = await store.upsert_issue(
        session_id="s",
        task_id="task-high-m1",
        mission_id=mission_s1.id,
        risk_level=RiskLevel.HIGH,
    )
    issue_medium_m1 = await store.upsert_issue(
        session_id="s",
        task_id="task-medium-m1",
        mission_id=mission_s1.id,
        risk_level=RiskLevel.MEDIUM,
    )
    issue_high_m2 = await store.upsert_issue(
        session_id="s",
        task_id="task-high-m2",
        mission_id=mission_s2.id,
        risk_level=RiskLevel.HIGH,
    )
    issue_low_m2 = await store.upsert_issue(
        session_id="s",
        task_id="task-low-m2",
        mission_id=mission_s2.id,
        risk_level=RiskLevel.LOW,
    )
    issue_other_session = await store.upsert_issue(
        session_id="s2",
        task_id="task-other",
        mission_id="mission-other",
        risk_level=RiskLevel.HIGH,
    )

    # Pin timestamps so ordering/limit assertions are deterministic.
    await _set_issue_timestamps(
        store, "s", issue_high_m1.task_id, created_at="2026-06-27T08:02:00"
    )
    await _set_issue_timestamps(
        store, "s", issue_medium_m1.task_id, created_at="2026-06-27T08:01:00"
    )
    await _set_issue_timestamps(
        store, "s", issue_high_m2.task_id, created_at="2026-06-27T08:00:00"
    )
    await _set_issue_timestamps(
        store, "s", issue_low_m2.task_id, created_at="2026-06-27T07:59:00"
    )
    await _set_issue_timestamps(
        store, "s2", issue_other_session.task_id, created_at="2026-06-27T08:03:00"
    )

    all_issues = await store.list_issues("s", limit=50)
    assert [i.task_id for i in all_issues] == [
        issue_high_m1.task_id,
        issue_medium_m1.task_id,
        issue_high_m2.task_id,
        issue_low_m2.task_id,
    ]
    assert all(i.session_id == "s" for i in all_issues)

    mission_s1_issues = await store.list_issues(
        "s", mission_id=mission_s1.id, limit=50
    )
    assert [i.task_id for i in mission_s1_issues] == [
        issue_high_m1.task_id,
        issue_medium_m1.task_id,
    ]

    high_risk_issues = await store.list_issues("s", risk_level="high", limit=50)
    assert [i.task_id for i in high_risk_issues] == [
        issue_high_m1.task_id,
        issue_high_m2.task_id,
    ]
    assert all(i.risk_level == RiskLevel.HIGH for i in high_risk_issues)

    filtered = await store.list_issues(
        "s", mission_id=mission_s1.id, risk_level="high", limit=50
    )
    assert [i.task_id for i in filtered] == [issue_high_m1.task_id]

    limited = await store.list_issues("s", limit=2)
    assert [i.task_id for i in limited] == [
        issue_high_m1.task_id,
        issue_medium_m1.task_id,
    ]

    other_session_issues = await store.list_issues("s2", limit=50)
    assert [i.task_id for i in other_session_issues] == [issue_other_session.task_id]



async def _set_mission_status(
    store: WorkbenchStore,
    mission_id: str,
    *,
    status: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE workbench_missions SET status = ? WHERE id = ?",
            (status, mission_id),
        )
        await db.commit()


async def _set_mission_timestamps(
    store: WorkbenchStore,
    mission_id: str,
    *,
    created_at: str,
    updated_at: str,
) -> None:
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE workbench_missions SET created_at = ?, updated_at = ? WHERE id = ?",
            (created_at, updated_at, mission_id),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_list_missions_defaults_match_snapshot_behavior(store: WorkbenchStore) -> None:
    mission_a = await store.create_mission("s", "Mission A", "Goal A")
    mission_b = await store.create_mission("s", "Mission B", "Goal B")
    await store.create_mission("s2", "Mission Other", "Goal Other")

    await _set_mission_timestamps(
        store, mission_a.id, created_at="2026-06-27T08:00:00", updated_at="2026-06-27T08:00:00"
    )
    await _set_mission_timestamps(
        store, mission_b.id, created_at="2026-06-27T08:01:00", updated_at="2026-06-27T08:01:00"
    )

    default = await store.list_missions("s")
    assert [m.id for m in default] == [mission_a.id, mission_b.id]
    assert all(m.session_id == "s" for m in default)


@pytest.mark.asyncio
async def test_list_missions_filters_by_status_and_session(store: WorkbenchStore) -> None:
    planning = await store.create_mission("s", "Planning Mission", "Goal")
    active = await store.create_mission("s", "Active Mission", "Goal")
    await store.create_mission("s2", "Other Planning", "Goal")

    await _set_mission_status(store, active.id, status="active")

    planning_only = await store.list_missions("s", status="planning")
    assert [m.id for m in planning_only] == [planning.id]
    assert all(m.status == "planning" for m in planning_only)

    active_only = await store.list_missions("s", status="active")
    assert [m.id for m in active_only] == [active.id]

    other_session = await store.list_missions("s2", status="planning")
    assert len(other_session) == 1
    assert other_session[0].session_id == "s2"


@pytest.mark.asyncio
async def test_list_missions_respects_limit_and_newest_first(store: WorkbenchStore) -> None:
    mission_a = await store.create_mission("s", "Mission A", "Goal")
    mission_b = await store.create_mission("s", "Mission B", "Goal")
    mission_c = await store.create_mission("s", "Mission C", "Goal")

    await _set_mission_timestamps(
        store, mission_a.id, created_at="2026-06-27T08:00:00", updated_at="2026-06-27T08:00:00"
    )
    await _set_mission_timestamps(
        store, mission_b.id, created_at="2026-06-27T08:01:00", updated_at="2026-06-27T08:01:00"
    )
    await _set_mission_timestamps(
        store, mission_c.id, created_at="2026-06-27T08:02:00", updated_at="2026-06-27T08:02:00"
    )

    limited = await store.list_missions("s", limit=2)
    assert [m.id for m in limited] == [mission_a.id, mission_b.id]

    newest_first = await store.list_missions("s", newest_first=True)
    assert [m.id for m in newest_first] == [mission_c.id, mission_b.id, mission_a.id]

    newest_limited = await store.list_missions("s", newest_first=True, limit=2)
    assert [m.id for m in newest_limited] == [mission_c.id, mission_b.id]


@pytest.mark.asyncio
async def test_list_missions_status_filter_with_limit_and_order(store: WorkbenchStore) -> None:
    active_a = await store.create_mission("s", "Active A", "Goal")
    active_b = await store.create_mission("s", "Active B", "Goal")
    planning = await store.create_mission("s", "Planning", "Goal")

    await _set_mission_status(store, active_a.id, status="active")
    await _set_mission_status(store, active_b.id, status="active")

    await _set_mission_timestamps(
        store, active_a.id, created_at="2026-06-27T08:00:00", updated_at="2026-06-27T08:00:00"
    )
    await _set_mission_timestamps(
        store, active_b.id, created_at="2026-06-27T08:01:00", updated_at="2026-06-27T08:01:00"
    )
    await _set_mission_timestamps(
        store, planning.id, created_at="2026-06-27T08:02:00", updated_at="2026-06-27T08:02:00"
    )

    active_newest = await store.list_missions("s", status="active", newest_first=True, limit=1)
    assert [m.id for m in active_newest] == [active_b.id]


@pytest.mark.asyncio
async def test_list_leases_filters_by_session_state_task_agent_and_orders_newest_first(
    store: WorkbenchStore,
) -> None:
    active_a = await store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )
    released_b = await store.create_lease(
        session_id="s",
        task_id="task-b",
        agent_id="agent-2",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-b",
    )
    await store.update_lease_state(released_b.id, LeaseState.RELEASED)
    expired_a = await store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a2",
    )
    await store.update_lease_state(expired_a.id, LeaseState.EXPIRED)
    other_session = await store.create_lease(
        session_id="s2",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-other",
    )

    # Pin timestamps so ordering/limit assertions are deterministic.
    # active_a is newest, released_b middle, expired_a oldest.
    await _set_lease_timestamps(
        store,
        active_a.id,
        created_at="2026-06-27T08:02:00",
        updated_at="2026-06-27T08:02:00",
    )
    await _set_lease_timestamps(
        store,
        released_b.id,
        created_at="2026-06-27T08:01:00",
        updated_at="2026-06-27T08:01:00",
    )
    await _set_lease_timestamps(
        store,
        expired_a.id,
        created_at="2026-06-27T08:00:00",
        updated_at="2026-06-27T08:00:00",
    )
    await _set_lease_timestamps(
        store,
        other_session.id,
        created_at="2026-06-27T08:03:00",
        updated_at="2026-06-27T08:03:00",
    )

    all_leases = await store.list_leases("s", limit=50)
    assert [lease.id for lease in all_leases] == [active_a.id, released_b.id, expired_a.id]
    assert all(isinstance(lease.state, LeaseState) for lease in all_leases)
    assert all(lease.session_id == "s" for lease in all_leases)

    active_only = await store.list_leases("s", state=LeaseState.ACTIVE, limit=50)
    assert [lease.id for lease in active_only] == [active_a.id]
    assert all(lease.state == LeaseState.ACTIVE for lease in active_only)

    active_by_string = await store.list_leases("s", state="active", limit=50)
    assert [lease.id for lease in active_by_string] == [active_a.id]

    released_only = await store.list_leases("s", state="released", limit=50)
    assert [lease.id for lease in released_only] == [released_b.id]

    task_a_leases = await store.list_leases("s", task_id="task-a", limit=50)
    assert [lease.id for lease in task_a_leases] == [active_a.id, expired_a.id]

    agent_2_leases = await store.list_leases("s", agent_id="agent-2", limit=50)
    assert [lease.id for lease in agent_2_leases] == [released_b.id]

    combined = await store.list_leases(
        "s", state="released", task_id="task-b", agent_id="agent-2", limit=50
    )
    assert [lease.id for lease in combined] == [released_b.id]

    limited = await store.list_leases("s", limit=2)
    assert [lease.id for lease in limited] == [active_a.id, released_b.id]

    other_session_leases = await store.list_leases("s2", limit=50)
    assert [lease.id for lease in other_session_leases] == [other_session.id]


@pytest.mark.asyncio
async def test_get_lease_returns_matching_session_lease(store: WorkbenchStore) -> None:
    lease = await store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )
    _other_session_lease = await store.create_lease(
        session_id="other",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-other",
    )

    found = await store.get_lease("s", lease.id)

    assert found == lease
    assert await store.get_lease("s", "missing-lease") is None
    assert await store.get_lease("other", lease.id) is None


@pytest.mark.asyncio
async def test_list_events_filters_by_type_subject_id_actor_and_returns_newest_first(
    store: WorkbenchStore,
) -> None:
    event_a = await store.append_event(
        session_id="s",
        type="mission.created",
        actor="Human",
        subject_id="mission-1",
        payload={"title": "A"},
    )
    event_b = await store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-1",
        payload={"detail": "B"},
    )
    event_c = await store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-2",
        payload={"detail": "C"},
    )
    event_d = await store.append_event(
        session_id="s",
        type="approval.resolved",
        actor="Human",
        subject_id="approval-1",
        payload={"state": "approved"},
    )
    event_other = await store.append_event(
        session_id="s2",
        type="mission.created",
        actor="Human",
        subject_id="mission-1",
        payload={"title": "Other"},
    )

    all_events = await store.list_events("s", limit=50)
    assert [e.id for e in all_events] == [event_d.id, event_c.id, event_b.id, event_a.id]

    by_type = await store.list_events("s", event_type="issue.created", limit=50)
    assert [e.id for e in by_type] == [event_c.id, event_b.id]

    by_subject = await store.list_events("s", subject_id="task-1", limit=50)
    assert [e.id for e in by_subject] == [event_b.id]

    by_actor = await store.list_events("s", actor="Human", limit=50)
    assert [e.id for e in by_actor] == [event_d.id, event_a.id]

    combined = await store.list_events(
        "s", event_type="issue.created", actor="Planner-Agent", limit=50
    )
    assert [e.id for e in combined] == [event_c.id, event_b.id]

    limited = await store.list_events("s", event_type="issue.created", limit=1)
    assert [e.id for e in limited] == [event_c.id]

    other_session = await store.list_events(
        "s2", event_type="mission.created", limit=50
    )
    assert [e.id for e in other_session] == [event_other.id]


@pytest.mark.asyncio
async def test_list_events_filters_by_since_timestamp(store: WorkbenchStore) -> None:
    event_a = await store.append_event(
        session_id="s",
        type="mission.created",
        actor="Human",
        subject_id="mission-1",
        payload={"title": "A"},
    )
    event_b = await store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-1",
        payload={"detail": "B"},
    )
    event_c = await store.append_event(
        session_id="s",
        type="validation.passed",
        actor="Test-Agent",
        subject_id="task-1",
        payload={"detail": "C"},
    )
    async with aiosqlite.connect(store._db_path) as db:
        await db.executemany(
            "UPDATE workbench_audit_events SET timestamp = ? WHERE id = ?",
            [
                ("2026-06-27T10:00:00+00:00", event_a.id),
                ("2026-06-27T10:01:00+00:00", event_b.id),
                ("2026-06-27T10:02:00+00:00", event_c.id),
            ],
        )
        await db.commit()

    newer = await store.list_events(
        "s",
        event_type="issue.created",
        since="2026-06-27T10:00:00+00:00",
        limit=50,
    )

    assert [event.id for event in newer] == [event_b.id]
