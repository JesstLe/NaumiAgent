from __future__ import annotations

import sys

import aiosqlite
import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    FailureKind,
    LeaseState,
    ParallelMode,
    RiskLevel,
)
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationRunner


@pytest.mark.asyncio
async def test_dashboard_snapshot_contains_core_cards(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="可视化治理多 Agent 研发",
    )
    task = await task_store.create_task("实现任务市场")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["认领冲突必须被拒绝"],
    )

    snapshot = await service.dashboard_snapshot("s")

    assert snapshot["missions"][0]["title"] == "Mac 工作台"
    assert snapshot["issues"][0]["task_id"] == task.id
    assert snapshot["tasks"][0]["subject"] == "实现任务市场"


@pytest.mark.asyncio
async def test_dashboard_snapshot_includes_only_active_leases(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="可视化治理多 Agent 研发",
    )
    active_task = await task_store.create_task("active lease task")
    released_task = await task_store.create_task("released lease task")
    expired_task = await task_store.create_task("expired lease task")
    for t in (active_task, released_task, expired_task):
        await service.attach_issue(
            session_id="s",
            mission_id=mission.id,
            task_id=t.id,
            acceptance_criteria=["AC"],
        )

    active_lease = await workbench_store.create_lease(
        session_id="s",
        task_id=active_task.id,
        agent_id="agent-a",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )
    released_lease = await workbench_store.create_lease(
        session_id="s",
        task_id=released_task.id,
        agent_id="agent-b",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-b",
    )
    await workbench_store.update_lease_state(released_lease.id, LeaseState.RELEASED)
    expired_lease = await workbench_store.create_lease(
        session_id="s",
        task_id=expired_task.id,
        agent_id="agent-c",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-c",
    )
    await workbench_store.update_lease_state(expired_lease.id, LeaseState.EXPIRED)

    snapshot = await service.dashboard_snapshot("s")

    assert "leases" in snapshot
    assert len(snapshot["leases"]) == 1
    lease_data = snapshot["leases"][0]
    assert lease_data["id"] == active_lease.id
    assert lease_data["task_id"] == active_task.id
    assert lease_data["agent_id"] == "agent-a"
    assert lease_data["state"] == "active"
    assert lease_data["worktree_name"] == "wt-a"


@pytest.mark.asyncio
async def test_dashboard_snapshot_includes_validation_runs_context_snapshots_and_waiting_approvals(
    tmp_path,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    run = await workbench_store.record_validation_run(
        session_id="s",
        task_id="task-1",
        actor="ValidationRunner",
        command=["pytest", "test_a.py"],
        cwd="/workspace",
        status="passed",
        exit_code=0,
        output="ok",
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T00:00:01",
    )
    snapshot = await workbench_store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-1",
        health=ContextHealth.GOOD,
        reasons=["上下文健康"],
    )
    waiting = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="等待审批",
        detail="详情",
        requester="Agent-A",
    )
    approved = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-2",
        title="已批准",
        detail="详情",
        requester="Agent-B",
        state=ApprovalState.APPROVED,
    )
    rejected = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-3",
        title="已拒绝",
        detail="详情",
        requester="Agent-C",
        state=ApprovalState.REJECTED,
    )
    other_session = await workbench_store.add_approval(
        session_id="other",
        mission_id="mission-1",
        task_id="task-1",
        title="其他会话审批",
        detail="详情",
        requester="Agent-D",
    )

    result = await service.dashboard_snapshot("s")

    assert "validation_runs" in result
    assert [r["id"] for r in result["validation_runs"]] == [run["id"]]
    assert result["validation_runs"][0]["status"] == "passed"

    assert "context_snapshots" in result
    assert [s["id"] for s in result["context_snapshots"]] == [snapshot["id"]]
    assert result["context_snapshots"][0]["health"] == "good"

    assert "approvals" in result
    assert {a["id"] for a in result["approvals"]} == {waiting.id}
    assert all(isinstance(a["state"], str) for a in result["approvals"])
    assert result["approvals"][0]["state"] == "waiting"
    assert approved.id not in {a["id"] for a in result["approvals"]}
    assert rejected.id not in {a["id"] for a in result["approvals"]}
    assert other_session.id not in {a["id"] for a in result["approvals"]}


@pytest.mark.asyncio
async def test_list_events_returns_store_events_and_respects_limit(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    event_a = await workbench_store.append_event(
        session_id="s",
        type="mission.created",
        actor="Human",
        subject_id="mission-1",
        payload={"title": "Mission A"},
    )
    event_b = await workbench_store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-1",
        payload={"detail": "issue B"},
    )

    all_events = await service.list_events("s", limit=50)

    assert {event["id"] for event in all_events["events"]} == {event_a.id, event_b.id}
    assert all(
        event in [event_a.to_dict(), event_b.to_dict()]
        for event in all_events["events"]
    )
    assert all_events["event_type"] is None
    assert all_events["subject_id"] is None
    assert all_events["actor"] is None
    assert all_events["limit"] == 50

    limited = await service.list_events("s", limit=1)

    assert len(limited["events"]) == 1
    assert limited["events"][0] in [event_a.to_dict(), event_b.to_dict()]
    assert limited["limit"] == 1


@pytest.mark.asyncio
async def test_list_events_forwards_filters_and_reflected_in_response(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    _event_a = await workbench_store.append_event(
        session_id="s",
        type="mission.created",
        actor="Human",
        subject_id="mission-1",
        payload={"title": "Mission A"},
    )
    event_b = await workbench_store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-1",
        payload={"detail": "issue B"},
    )
    _event_c = await workbench_store.append_event(
        session_id="s",
        type="issue.created",
        actor="Planner-Agent",
        subject_id="task-2",
        payload={"detail": "issue C"},
    )

    filtered = await service.list_events(
        "s",
        event_type="issue.created",
        subject_id="task-1",
        actor="Planner-Agent",
        limit=50,
    )

    assert [event["id"] for event in filtered["events"]] == [event_b.id]
    assert filtered["event_type"] == "issue.created"
    assert filtered["subject_id"] == "task-1"
    assert filtered["actor"] == "Planner-Agent"
    assert filtered["limit"] == 50


@pytest.mark.asyncio
async def test_list_validation_runs_returns_runs_and_respects_limit(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    run_a = await workbench_store.record_validation_run(
        session_id="s",
        task_id="task-a",
        actor="ValidationRunner",
        command=["pytest", "test_a.py"],
        cwd="/workspace",
        status="passed",
        exit_code=0,
        output="ok",
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T00:00:01",
    )
    run_b = await workbench_store.record_validation_run(
        session_id="s",
        task_id="task-b",
        actor="ValidationRunner",
        command=["pytest", "test_b.py"],
        cwd="/workspace",
        status="failed",
        exit_code=1,
        output="error",
        started_at="2024-01-01T00:01:00",
        completed_at="2024-01-01T00:01:01",
    )

    all_runs = await service.list_validation_runs("s", limit=50)
    assert [run["id"] for run in all_runs] == [run_a["id"], run_b["id"]]

    filtered = await service.list_validation_runs("s", task_id="task-b", limit=50)
    assert [run["id"] for run in filtered] == [run_b["id"]]

    limited = await service.list_validation_runs("s", limit=1)
    assert [run["id"] for run in limited] == [run_b["id"]]


@pytest.mark.asyncio
async def test_list_context_snapshots_returns_store_snapshots_and_respects_limit(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    snap_a = await workbench_store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-a",
        health=ContextHealth.GOOD,
        reasons=["上下文健康"],
    )
    snap_b = await workbench_store.record_context_snapshot(
        session_id="s",
        agent_id="agent-2",
        task_id="task-b",
        health=ContextHealth.STALE,
        reasons=["超过 60 分钟未同步上下文"],
    )

    all_snaps = await service.list_context_snapshots("s", limit=50)
    assert {snap["id"] for snap in all_snaps} == {snap_a["id"], snap_b["id"]}

    filtered = await service.list_context_snapshots("s", agent_id="agent-2", limit=50)
    assert [snap["id"] for snap in filtered] == [snap_b["id"]]
    assert filtered[0]["reasons"] == ["超过 60 分钟未同步上下文"]

    limited = await service.list_context_snapshots("s", limit=1)
    assert len(limited) == 1
    assert limited[0]["id"] in {snap_a["id"], snap_b["id"]}


@pytest.mark.asyncio
async def test_record_context_health_evaluates_persists_and_records_event(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="可视化治理多 Agent 研发",
    )
    task = await task_store.create_task("同步上下文")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["必须记录健康度"],
    )

    snapshot = await service.record_context_health(
        session_id="s",
        task_id=task.id,
        agent_id=" Agent-A ",
        minutes_since_sync=75,
        token_load_ratio=0.2,
        actor="Human",
    )

    assert snapshot["agent_id"] == "Agent-A"
    assert snapshot["task_id"] == task.id
    assert snapshot["health"] == "stale"
    assert snapshot["reasons"] == ["超过 60 分钟未同步上下文"]

    stored = await service.list_context_snapshots(
        "s", task_id=task.id, agent_id="Agent-A"
    )
    assert [item["id"] for item in stored] == [snapshot["id"]]

    events = await service.list_events("s", event_type="context_health.recorded")
    event = events["events"][0]
    assert event["actor"] == "Human"
    assert event["subject_id"] == task.id
    assert event["payload"] == {
        "agent_id": "Agent-A",
        "health": "stale",
        "reasons": ["超过 60 分钟未同步上下文"],
        "mission_id": mission.id,
    }


@pytest.mark.asyncio
async def test_record_context_health_reports_missing_inputs(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    with pytest.raises(ValueError, match="issue 不存在"):
        await service.record_context_health(
            session_id="s",
            task_id="missing",
            agent_id="Agent-A",
            minutes_since_sync=0,
            token_load_ratio=0.1,
        )

    mission = await service.create_mission(session_id="s", title="M", goal="G")
    task = await task_store.create_task("同步上下文")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=[],
    )

    with pytest.raises(ValueError, match="agent_id 不能为空"):
        await service.record_context_health(
            session_id="s",
            task_id=task.id,
            agent_id=" ",
            minutes_since_sync=0,
            token_load_ratio=0.1,
        )

    with pytest.raises(ValueError, match="minutes_since_sync 不能为负数"):
        await service.record_context_health(
            session_id="s",
            task_id=task.id,
            agent_id="Agent-A",
            minutes_since_sync=-1,
            token_load_ratio=0.1,
        )

    with pytest.raises(ValueError, match="token_load_ratio 不能为负数"):
        await service.record_context_health(
            session_id="s",
            task_id=task.id,
            agent_id="Agent-A",
            minutes_since_sync=0,
            token_load_ratio=-0.1,
        )


@pytest.mark.asyncio
async def test_list_approvals_returns_json_friendly_state_strings(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    waiting = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="等待审批",
        detail="详情",
        requester="Agent-A",
    )
    approved = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-2",
        title="已批准",
        detail="详情",
        requester="Agent-B",
        state=ApprovalState.APPROVED,
    )

    all_approvals = await service.list_approvals("s", limit=50)
    assert {a["id"] for a in all_approvals} == {waiting.id, approved.id}
    assert all(isinstance(a["state"], str) for a in all_approvals)
    approvals_by_id = {a["id"]: a for a in all_approvals}
    assert approvals_by_id[waiting.id]["state"] == "waiting"
    assert approvals_by_id[approved.id]["state"] == "approved"

    waiting_only = await service.list_approvals("s", state=ApprovalState.WAITING, limit=50)
    assert [a["id"] for a in waiting_only] == [waiting.id]
    assert waiting_only[0]["state"] == "waiting"


@pytest.mark.asyncio
async def test_list_failures_returns_store_rows_and_respects_filters(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    failure_open = await workbench_store.create_failure(
        session_id="s",
        task_id="task-a",
        kind=FailureKind.TEST_FAILED,
        title="测试失败",
        detail="detail",
        source_id="run-a",
    )
    failure_resolved = await workbench_store.create_failure(
        session_id="s",
        task_id="task-b",
        kind=FailureKind.AGENT_TIMEOUT,
        title="Agent 超时",
        detail="detail",
        source_id="run-b",
    )
    async with aiosqlite.connect(workbench_store._db_path) as db:
        await db.execute(
            "UPDATE workbench_failures SET status = ? WHERE id = ?",
            ("resolved", failure_resolved["id"]),
        )
        await db.commit()
    await workbench_store.create_failure(
        session_id="s2",
        task_id="task-a",
        kind=FailureKind.LEASE_EXPIRED,
        title="其他会话失败",
        detail="detail",
        source_id="run-c",
    )

    all_failures = await service.list_failures("s", limit=50)
    assert {f["id"] for f in all_failures} == {failure_open["id"], failure_resolved["id"]}
    assert all(isinstance(f["status"], str) for f in all_failures)

    filtered_task = await service.list_failures("s", task_id="task-a", limit=50)
    assert [f["id"] for f in filtered_task] == [failure_open["id"]]

    filtered_status = await service.list_failures("s", status="open", limit=50)
    assert [f["id"] for f in filtered_status] == [failure_open["id"]]

    limited = await service.list_failures("s", limit=1)
    assert len(limited) == 1


@pytest.mark.asyncio
async def test_run_validation_records_run_and_event(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(
        store=workbench_store,
        allowed_commands=[[sys.executable, "-c"]],
    )
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=workbench_store,
        validation_runner=runner,
        workspace_root=str(tmp_path),
    )

    argv = [sys.executable, "-c", "print('hello from validation')"]
    result = await service.run_validation(
        session_id="s",
        task_id="task-1",
        actor="Human",
        argv=argv,
    )

    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert "hello from validation" in result["output"]

    runs = await service.list_validation_runs("s", task_id="task-1")
    assert any(run["id"] == result["id"] for run in runs)
    assert next(run for run in runs if run["id"] == result["id"])["cwd"] == str(
        tmp_path.resolve()
    )

    events = await service.list_events("s")
    event = next((e for e in events["events"] if e["type"] == "validation.completed"), None)
    assert event is not None
    assert event["actor"] == "Human"
    assert event["subject_id"] == "task-1"
    assert event["payload"]["run_id"] == result["id"]
    assert event["payload"]["status"] == "passed"
    assert event["payload"]["exit_code"] == 0
    assert event["payload"]["command"] == argv


@pytest.mark.asyncio
async def test_run_validation_rejects_cwd_outside_workspace(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(
        store=workbench_store,
        allowed_commands=[[sys.executable, "-c"]],
    )
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=workbench_store,
        validation_runner=runner,
        workspace_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="工作目录必须在 workspace_root 内"):
        await service.run_validation(
            session_id="s",
            task_id="task-1",
            actor="Human",
            argv=[sys.executable, "-c", "print('ok')"],
            cwd=str(tmp_path.parent),
        )


@pytest.mark.asyncio
async def test_create_intent_lock_persists_lock_and_records_event(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    lock = await service.create_intent_lock(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        rule="禁止修改 src/naumi_agent/api/routes 下除 workbench.py 外的文件",
        blocked_paths=[" src/secret  ", "", "  "],
        allowed_paths=["src/naumi_agent/api/routes/workbench.py", "  ", ""],
        require_proposal_for_risk=RiskLevel.CRITICAL,
    )

    assert lock["session_id"] == "s"
    assert lock["mission_id"] == "mission-1"
    assert lock["rule"] == "禁止修改 src/naumi_agent/api/routes 下除 workbench.py 外的文件"
    assert lock["blocked_paths"] == ["src/secret"]
    assert lock["allowed_paths"] == ["src/naumi_agent/api/routes/workbench.py"]
    assert lock["require_proposal_for_risk"] == "critical"
    assert lock["active"] is True

    locks = await workbench_store.list_intent_locks("s", "mission-1")
    assert any(stored.id == lock["id"] for stored in locks)

    events = await service.list_events("s")
    event = next((e for e in events["events"] if e["type"] == "intent_lock.created"), None)
    assert event is not None
    assert event["actor"] == "Planner-Agent"
    assert event["subject_id"] == lock["id"]
    assert event["payload"]["mission_id"] == "mission-1"
    assert event["payload"]["rule"] == lock["rule"]
    assert event["payload"]["require_proposal_for_risk"] == "critical"


@pytest.mark.asyncio
async def test_create_intent_lock_rejects_empty_rule(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    with pytest.raises(ValueError, match="意图锁规则不能为空"):
        await service.create_intent_lock(
            session_id="s",
            mission_id="mission-1",
            actor="Human",
            rule="   ",
        )

    assert await workbench_store.list_intent_locks("s", "mission-1") == []


@pytest.mark.asyncio
async def test_create_decision_persists_decision_and_records_event(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    decision = await service.create_decision(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.ARCHITECTURE,
        title=" 采用 FastAPI ",
        content=" 使用 FastAPI 承载 Workbench API ",
    )

    assert decision["session_id"] == "s"
    assert decision["mission_id"] == "mission-1"
    assert decision["title"] == "采用 FastAPI"
    assert decision["content"] == "使用 FastAPI 承载 Workbench API"
    assert decision["actor"] == "Planner-Agent"
    assert decision["kind"] == "architecture"

    decisions = await workbench_store.list_decisions("s", "mission-1")
    assert any(stored.id == decision["id"] for stored in decisions)

    events = await service.list_events("s")
    event = next((e for e in events["events"] if e["type"] == "decision.created"), None)
    assert event is not None
    assert event["actor"] == "Planner-Agent"
    assert event["subject_id"] == decision["id"]
    assert event["payload"]["mission_id"] == "mission-1"
    assert event["payload"]["kind"] == "architecture"
    assert event["payload"]["title"] == "采用 FastAPI"


@pytest.mark.asyncio
async def test_create_decision_rejects_empty_title_or_content(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    with pytest.raises(ValueError, match="决策标题不能为空"):
        await service.create_decision(
            session_id="s",
            mission_id="mission-1",
            actor="Human",
            kind=DecisionKind.POLICY,
            title="   ",
            content="有效内容",
        )

    with pytest.raises(ValueError, match="决策内容不能为空"):
        await service.create_decision(
            session_id="s",
            mission_id="mission-1",
            actor="Human",
            kind=DecisionKind.POLICY,
            title="有效标题",
            content="",
        )

    assert await workbench_store.list_decisions("s", "mission-1") == []


@pytest.mark.asyncio
async def test_resolve_approval_persists_record_and_emits_event(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    approval = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="允许重构 core 模块",
        detail="保持测试通过",
        requester="Agent-A",
    )

    result = await service.resolve_approval(
        session_id="s",
        approval_id=approval.id,
        actor="  Human  ",
        state=ApprovalState.APPROVED,
        decision_note="  同意  ",
    )

    assert result is not None
    assert result["id"] == approval.id
    assert result["session_id"] == "s"
    assert result["mission_id"] == "mission-1"
    assert result["task_id"] == "task-1"
    assert result["state"] == "approved"
    assert result["title"] == "允许重构 core 模块"
    assert result["reviewer"] == "Human"
    assert result["decision_note"] == "同意"

    events = await service.list_events("s")
    event = next((e for e in events["events"] if e["type"] == "approval.resolved"), None)
    assert event is not None
    assert event["actor"] == "Human"
    assert event["subject_id"] == approval.id
    assert event["payload"]["state"] == "approved"
    assert event["payload"]["mission_id"] == "mission-1"
    assert event["payload"]["task_id"] == "task-1"
    assert event["payload"]["title"] == "允许重构 core 模块"


@pytest.mark.asyncio
async def test_resolve_approval_rejects_invalid_state(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    approval = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="请求审批",
        detail="详情",
        requester="Agent-A",
    )

    with pytest.raises(ValueError, match="审批结果只能是 approved 或 rejected"):
        await service.resolve_approval(
            session_id="s",
            approval_id=approval.id,
            actor="Human",
            state=ApprovalState.WAITING,
            decision_note="",
        )


@pytest.mark.asyncio
async def test_resolve_approval_returns_none_when_missing(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    result = await service.resolve_approval(
        session_id="s",
        approval_id="no-such-id",
        actor="Human",
        state=ApprovalState.REJECTED,
        decision_note="",
    )
    assert result is None


@pytest.mark.asyncio
async def test_list_issues_returns_json_friendly_strings_and_respects_filters(
    tmp_path,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    await workbench_store.upsert_issue(
        session_id="s",
        task_id="task-a",
        mission_id="mission-1",
        parallel_mode=ParallelMode.EXCLUSIVE,
        risk_level=RiskLevel.HIGH,
        acceptance_criteria=["AC1"],
    )
    await workbench_store.upsert_issue(
        session_id="s",
        task_id="task-b",
        mission_id="mission-1",
        parallel_mode=ParallelMode.COOPERATIVE,
        risk_level=RiskLevel.MEDIUM,
        acceptance_criteria=["AC2"],
    )
    await workbench_store.upsert_issue(
        session_id="s",
        task_id="task-c",
        mission_id="mission-2",
        parallel_mode=ParallelMode.COMPETITIVE,
        risk_level=RiskLevel.HIGH,
        acceptance_criteria=["AC3"],
    )

    all_issues = await service.list_issues("s", limit=50)
    assert all(isinstance(i["risk_level"], str) for i in all_issues["issues"])
    assert all(isinstance(i["parallel_mode"], str) for i in all_issues["issues"])
    assert {i["task_id"] for i in all_issues["issues"]} == {"task-a", "task-b", "task-c"}
    assert all_issues["mission_id"] is None
    assert all_issues["risk_level"] is None
    assert all_issues["limit"] == 50

    mission_1_high = await service.list_issues(
        "s", mission_id="mission-1", risk_level="high", limit=50
    )
    assert [i["task_id"] for i in mission_1_high["issues"]] == ["task-a"]
    assert mission_1_high["risk_level"] == "high"
    assert mission_1_high["mission_id"] == "mission-1"
    assert mission_1_high["issues"][0]["risk_level"] == "high"
    assert mission_1_high["issues"][0]["parallel_mode"] == "exclusive"

    limited = await service.list_issues("s", limit=2)
    assert len(limited["issues"]) == 2
    assert limited["limit"] == 2



@pytest.mark.asyncio
async def test_list_missions_returns_wrapper_and_json_friendly_fields(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await workbench_store.create_mission("s", "Mac 工作台", "可视化治理")

    all_missions = await service.list_missions("s", limit=50)
    assert all_missions["missions"][0]["id"] == mission.id
    assert all_missions["missions"][0]["title"] == "Mac 工作台"
    assert all_missions["status"] is None
    assert all_missions["limit"] == 50
    assert all(isinstance(m["status"], str) for m in all_missions["missions"])

    filtered = await service.list_missions("s", status="planning", limit=10)
    assert len(filtered["missions"]) == 1
    assert filtered["status"] == "planning"
    assert filtered["limit"] == 10


@pytest.mark.asyncio
async def test_list_missions_filters_by_status(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    await workbench_store.create_mission("s", "Planning", "Goal")
    active = await workbench_store.create_mission("s", "Active", "Goal")
    async with aiosqlite.connect(workbench_store._db_path) as db:
        await db.execute(
            "UPDATE workbench_missions SET status = ? WHERE id = ?",
            ("active", active.id),
        )
        await db.commit()

    result = await service.list_missions("s", status="active", limit=50)
    assert [m["id"] for m in result["missions"]] == [active.id]
    assert result["status"] == "active"


@pytest.mark.asyncio
async def test_dashboard_snapshot_mission_order_unchanged_by_list_missions_params(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission_a = await service.create_mission(session_id="s", title="A", goal="G")
    mission_b = await service.create_mission(session_id="s", title="B", goal="G")

    async with aiosqlite.connect(workbench_store._db_path) as db:
        await db.execute(
            "UPDATE workbench_missions SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-06-27T08:00:00", "2026-06-27T08:00:00", mission_a.id),
        )
        await db.execute(
            "UPDATE workbench_missions SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-06-27T08:01:00", "2026-06-27T08:01:00", mission_b.id),
        )
        await db.commit()

    snapshot = await service.dashboard_snapshot("s")
    assert [m["id"] for m in snapshot["missions"]] == [mission_a.id, mission_b.id]

    newest = await workbench_store.list_missions("s", newest_first=True, limit=1)
    assert [m.id for m in newest] == [mission_b.id]

    snapshot_after = await service.dashboard_snapshot("s")
    assert [m["id"] for m in snapshot_after["missions"]] == [mission_a.id, mission_b.id]


@pytest.mark.asyncio
async def test_list_leases_returns_wrapper_and_json_friendly_state_strings(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    active = await workbench_store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )
    released = await workbench_store.create_lease(
        session_id="s",
        task_id="task-b",
        agent_id="agent-2",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-b",
    )
    await workbench_store.update_lease_state(released.id, LeaseState.RELEASED)

    all_leases = await service.list_leases("s", limit=50)
    assert all(isinstance(lease["state"], str) for lease in all_leases["leases"])
    assert {lease["id"] for lease in all_leases["leases"]} == {active.id, released.id}
    assert all_leases["state"] is None
    assert all_leases["task_id"] is None
    assert all_leases["agent_id"] is None
    assert all_leases["limit"] == 50

    active_only = await service.list_leases("s", state=LeaseState.ACTIVE, limit=50)
    assert [lease["id"] for lease in active_only["leases"]] == [active.id]
    assert active_only["state"] == "active"
    assert active_only["task_id"] is None
    assert active_only["agent_id"] is None

    filtered = await service.list_leases(
        "s", state="released", task_id="task-b", agent_id="agent-2", limit=10
    )
    assert [lease["id"] for lease in filtered["leases"]] == [released.id]
    assert filtered["state"] == "released"
    assert filtered["task_id"] == "task-b"
    assert filtered["agent_id"] == "agent-2"
    assert filtered["limit"] == 10
    assert filtered["leases"][0]["state"] == "released"
