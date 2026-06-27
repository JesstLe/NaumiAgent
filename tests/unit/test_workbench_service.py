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
async def test_create_issue_creates_backing_task_and_issue_metadata(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    blocker = await task_store.create_task("先完成 API client")
    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="让用户能直接从 Mac App 创建可认领 Issue",
    )

    issue = await service.create_issue(
        session_id="s",
        mission_id=mission.id,
        title="实现 Issue 创建 API",
        description="创建 backing task 并绑定 workbench metadata",
        blocked_by=[blocker.id],
        acceptance_criteria=["dashboard 刷新后可见", "可被 Agent claim"],
        parallel_mode=ParallelMode.COOPERATIVE,
        risk_level=RiskLevel.HIGH,
    )

    tasks = await task_store.list_tasks()
    created_task = next(task for task in tasks if task.id == issue["task_id"])

    assert created_task.subject == "实现 Issue 创建 API"
    assert created_task.description == "创建 backing task 并绑定 workbench metadata"
    assert created_task.blocked_by == [blocker.id]
    assert issue["session_id"] == "s"
    assert issue["mission_id"] == mission.id
    assert issue["acceptance_criteria"] == ["dashboard 刷新后可见", "可被 Agent claim"]
    assert issue["parallel_mode"] == "cooperative"
    assert issue["risk_level"] == "high"

    snapshot = await service.dashboard_snapshot("s")
    assert [task["subject"] for task in snapshot["tasks"]] == [
        "先完成 API client",
        "实现 Issue 创建 API",
    ]
    assert snapshot["issues"][0]["task_id"] == created_task.id

    events = await service.list_events("s", event_type="issue.created")
    assert events["events"][0]["subject_id"] == created_task.id
    assert events["events"][0]["payload"]["mission_id"] == mission.id


@pytest.mark.asyncio
async def test_get_mission_returns_json_friendly_mission(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="让治理页可以直接读取 Mission 详情",
    )

    detail = await service.get_mission("s", mission.id)

    assert detail is not None
    assert detail["id"] == mission.id
    assert detail["session_id"] == "s"
    assert detail["title"] == "Mac 工作台"
    assert detail["goal"] == "让治理页可以直接读取 Mission 详情"
    assert detail["status"] == "planning"


@pytest.mark.asyncio
async def test_get_mission_returns_none_for_missing_mission(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    assert await service.get_mission("s", "missing-mission") is None


@pytest.mark.asyncio
async def test_get_issue_returns_json_friendly_issue_metadata(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="让检查器可以直接读取 Issue 详情",
    )
    task = await task_store.create_task("实现 Issue 详情 API")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["详情页不依赖全量 snapshot"],
        parallel_mode=ParallelMode.COOPERATIVE,
        risk_level=RiskLevel.HIGH,
    )

    issue = await service.get_issue("s", task.id)

    assert issue is not None
    assert issue["session_id"] == "s"
    assert issue["task_id"] == task.id
    assert issue["mission_id"] == mission.id
    assert issue["parallel_mode"] == "cooperative"
    assert issue["risk_level"] == "high"
    assert issue["acceptance_criteria"] == ["详情页不依赖全量 snapshot"]


@pytest.mark.asyncio
async def test_get_issue_returns_none_for_missing_issue(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    assert await service.get_issue("s", "missing-task") is None


@pytest.mark.asyncio
async def test_attach_issue_rejects_missing_mission(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)
    task = await task_store.create_task("不要绑定到孤儿 mission")

    with pytest.raises(ValueError, match="mission 不存在"):
        await service.attach_issue(
            session_id="s",
            mission_id="missing-mission",
            task_id=task.id,
            acceptance_criteria=["必须拒绝孤儿 issue"],
        )

    assert await workbench_store.get_issue("s", task.id) is None


@pytest.mark.asyncio
async def test_create_issue_rejects_missing_mission_without_creating_task(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    with pytest.raises(ValueError, match="mission 不存在"):
        await service.create_issue(
            session_id="s",
            mission_id="missing-mission",
            title="不要先创建孤儿 backing task",
        )

    assert await task_store.list_tasks() == []


@pytest.mark.asyncio
async def test_register_agent_profile_records_event_and_snapshot_card(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    profile = await service.register_agent_profile(
        session_id="s",
        agent_id="agent-a",
        name="Backend Agent",
        role="coder",
        capabilities=["code", "test"],
        permissions=["read", "write"],
        max_parallel_tasks=2,
        status="busy",
        actor="Human",
    )

    assert profile["id"] == "agent-a"
    assert profile["capabilities"] == ["code", "test"]
    assert profile["permissions"] == ["read", "write"]
    assert profile["status"] == "busy"

    listed = await service.list_agent_profiles("s", status="busy")
    assert [item["id"] for item in listed["agent_profiles"]] == ["agent-a"]
    assert listed["status"] == "busy"

    snapshot = await service.dashboard_snapshot("s")
    assert [item["id"] for item in snapshot["agent_profiles"]] == ["agent-a"]

    events = await service.list_events("s", event_type="agent_profile.upserted")
    event = events["events"][0]
    assert event["actor"] == "Human"
    assert event["subject_id"] == "agent-a"
    assert event["payload"] == {
        "name": "Backend Agent",
        "role": "coder",
        "status": "busy",
        "capabilities": ["code", "test"],
        "permissions": ["read", "write"],
    }


@pytest.mark.asyncio
async def test_get_agent_profile_returns_single_profile(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    profile = await workbench_store.upsert_agent_profile(
        session_id="s",
        agent_id="agent-a",
        name="Backend Agent",
        role="coder",
        capabilities=["code", "test"],
        permissions=["read", "write"],
        max_parallel_tasks=2,
        status="busy",
    )

    result = await service.get_agent_profile("s", profile.id)

    assert result == {
        "id": "agent-a",
        "session_id": "s",
        "name": "Backend Agent",
        "role": "coder",
        "capabilities": ["code", "test"],
        "permissions": ["read", "write"],
        "max_parallel_tasks": 2,
        "status": "busy",
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@pytest.mark.asyncio
async def test_get_agent_profile_returns_none_for_missing_or_other_session(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    await workbench_store.upsert_agent_profile(
        session_id="s",
        agent_id="agent-a",
        name="Backend Agent",
        role="coder",
    )

    assert await service.get_agent_profile("s", "missing-agent") is None
    assert await service.get_agent_profile("other", "agent-a") is None


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
async def test_get_event_returns_single_event_payload(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    event = await workbench_store.append_event(
        session_id="s",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="task-1",
        payload={"lease_id": "lease-1"},
    )

    result = await service.get_event("s", event.id)

    assert result == event.to_dict()


@pytest.mark.asyncio
async def test_get_event_returns_none_for_missing_or_other_session(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    event = await workbench_store.append_event(
        session_id="s",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="task-1",
        payload={"lease_id": "lease-1"},
    )

    assert await service.get_event("s", "missing-event") is None
    assert await service.get_event("other", event.id) is None


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
async def test_get_validation_run_returns_single_run(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    run = await workbench_store.record_validation_run(
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

    result = await service.get_validation_run("s", run["id"])

    assert result == run


@pytest.mark.asyncio
async def test_get_validation_run_returns_none_for_missing_or_other_session(
    tmp_path,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    run = await workbench_store.record_validation_run(
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

    assert await service.get_validation_run("s", "missing-run") is None
    assert await service.get_validation_run("other-session", run["id"]) is None


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
async def test_get_context_snapshot_returns_single_snapshot(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    snapshot = await workbench_store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-a",
        health=ContextHealth.STALE,
        reasons=["超过 60 分钟未同步上下文"],
    )

    result = await service.get_context_snapshot("s", snapshot["id"])

    assert result == snapshot


@pytest.mark.asyncio
async def test_get_context_snapshot_returns_none_for_missing_or_other_session(
    tmp_path,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    snapshot = await workbench_store.record_context_snapshot(
        session_id="s",
        agent_id="agent-1",
        task_id="task-a",
        health=ContextHealth.GOOD,
        reasons=["上下文健康"],
    )

    assert await service.get_context_snapshot("s", "missing-snapshot") is None
    assert await service.get_context_snapshot("other-session", snapshot["id"]) is None


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
async def test_get_approval_returns_json_friendly_approval_detail(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    approval = await workbench_store.add_approval(
        session_id="s",
        mission_id="mission-1",
        task_id="task-1",
        title="请求审批",
        detail="需要人工确认高风险变更",
        requester="Agent-A",
    )

    result = await service.get_approval("s", approval.id)

    assert result is not None
    assert result["id"] == approval.id
    assert result["session_id"] == "s"
    assert result["mission_id"] == "mission-1"
    assert result["task_id"] == "task-1"
    assert result["state"] == "waiting"
    assert result["title"] == "请求审批"
    assert result["detail"] == "需要人工确认高风险变更"
    assert result["requester"] == "Agent-A"
    assert result["reviewer"] == ""
    assert result["decision_note"] == ""


@pytest.mark.asyncio
async def test_get_approval_returns_none_for_missing_or_other_session(tmp_path) -> None:
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

    assert await service.get_approval("s", "missing-approval") is None
    assert await service.get_approval("other", approval.id) is None


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
async def test_get_failure_returns_single_failure(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    failure = await workbench_store.create_failure(
        session_id="s",
        task_id="task-a",
        kind=FailureKind.TEST_FAILED,
        title="测试失败",
        detail="pytest failed",
        source_id="run-a",
    )

    result = await service.get_failure("s", failure["id"])

    assert result == failure


@pytest.mark.asyncio
async def test_get_failure_returns_none_for_missing_or_other_session(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    failure = await workbench_store.create_failure(
        session_id="s",
        task_id="task-a",
        kind=FailureKind.TEST_FAILED,
        title="测试失败",
        detail="pytest failed",
        source_id="run-a",
    )

    assert await service.get_failure("s", "missing-failure") is None
    assert await service.get_failure("other-session", failure["id"]) is None


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
    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="验证运行必须绑定 issue",
    )
    task = await task_store.create_task("运行验证")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["验证记录必须可追溯"],
    )

    argv = [sys.executable, "-c", "print('hello from validation')"]
    result = await service.run_validation(
        session_id="s",
        task_id=task.id,
        actor="Human",
        argv=argv,
    )

    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert "hello from validation" in result["output"]

    runs = await service.list_validation_runs("s", task_id=task.id)
    assert any(run["id"] == result["id"] for run in runs)
    assert next(run for run in runs if run["id"] == result["id"])["cwd"] == str(
        tmp_path.resolve()
    )

    events = await service.list_events("s")
    event = next((e for e in events["events"] if e["type"] == "validation.completed"), None)
    assert event is not None
    assert event["actor"] == "Human"
    assert event["subject_id"] == task.id
    assert event["payload"]["run_id"] == result["id"]
    assert event["payload"]["status"] == "passed"
    assert event["payload"]["exit_code"] == 0
    assert event["payload"]["command"] == argv


@pytest.mark.asyncio
async def test_run_validation_rejects_missing_issue_without_recording_run(tmp_path) -> None:
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

    with pytest.raises(ValueError, match="issue 不存在"):
        await service.run_validation(
            session_id="s",
            task_id="missing-task",
            actor="Human",
            argv=[sys.executable, "-c", "print('should not run')"],
        )

    assert await service.list_validation_runs("s", task_id="missing-task") == []


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
    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="验证 cwd 必须被限制",
    )
    task = await task_store.create_task("验证 cwd")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["cwd 不能越界"],
    )

    with pytest.raises(ValueError, match="工作目录必须在 workspace_root 内"):
        await service.run_validation(
            session_id="s",
            task_id=task.id,
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


@pytest.mark.asyncio
async def test_get_lease_returns_json_friendly_lease_detail(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    lease = await workbench_store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )

    result = await service.get_lease("s", lease.id)

    assert result is not None
    assert result["id"] == lease.id
    assert result["session_id"] == "s"
    assert result["task_id"] == "task-a"
    assert result["agent_id"] == "agent-1"
    assert result["state"] == "active"
    assert result["worktree_name"] == "wt-a"


@pytest.mark.asyncio
async def test_get_lease_returns_none_for_missing_or_other_session(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    lease = await workbench_store.create_lease(
        session_id="s",
        task_id="task-a",
        agent_id="agent-1",
        expires_at="2099-01-01T00:00:00",
        worktree_name="wt-a",
    )

    assert await service.get_lease("s", "missing-lease") is None
    assert await service.get_lease("other", lease.id) is None


@pytest.mark.asyncio
async def test_list_intent_locks_returns_json_friendly_strings(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    lock = await service.create_intent_lock(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        rule="禁止修改 src/secret 下文件",
        blocked_paths=["src/secret"],
        allowed_paths=["src/secret/README.md"],
        require_proposal_for_risk=RiskLevel.HIGH,
    )

    locks = await service.list_intent_locks("s", "mission-1")
    assert [item["id"] for item in locks] == [lock["id"]]
    assert all(isinstance(item["require_proposal_for_risk"], str) for item in locks)
    assert locks[0]["require_proposal_for_risk"] == "high"
    assert locks[0]["rule"] == "禁止修改 src/secret 下文件"
    assert locks[0]["blocked_paths"] == ["src/secret"]
    assert locks[0]["allowed_paths"] == ["src/secret/README.md"]
    assert locks[0]["active"] is True


@pytest.mark.asyncio
async def test_list_intent_locks_is_scoped_to_session_and_mission(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    await service.create_intent_lock(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        rule="规则 A",
    )
    await service.create_intent_lock(
        session_id="s",
        mission_id="mission-2",
        actor="Planner-Agent",
        rule="规则 B",
    )
    await service.create_intent_lock(
        session_id="other",
        mission_id="mission-1",
        actor="Planner-Agent",
        rule="规则 C",
    )

    locks = await service.list_intent_locks("s", "mission-1")
    assert [item["rule"] for item in locks] == ["规则 A"]


@pytest.mark.asyncio
async def test_list_decisions_returns_json_friendly_strings(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    decision = await service.create_decision(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.POLICY,
        title="采用策略 A",
        content="内容 A",
    )

    decisions = await service.list_decisions("s", "mission-1")
    assert [item["id"] for item in decisions] == [decision["id"]]
    assert all(isinstance(item["kind"], str) for item in decisions)
    assert decisions[0]["kind"] == "policy"
    assert decisions[0]["title"] == "采用策略 A"
    assert decisions[0]["content"] == "内容 A"
    assert decisions[0]["actor"] == "Planner-Agent"


@pytest.mark.asyncio
async def test_get_decision_returns_json_friendly_decision_detail(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    decision = await service.create_decision(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.POLICY,
        title="采用策略 A",
        content="内容 A",
    )

    result = await service.get_decision("s", "mission-1", decision["id"])

    assert result == decision
    assert result["kind"] == "policy"


@pytest.mark.asyncio
async def test_get_decision_returns_none_for_missing_or_other_scope(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    decision = await service.create_decision(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.POLICY,
        title="采用策略 A",
        content="内容 A",
    )

    assert await service.get_decision("s", "mission-1", "missing-decision") is None
    assert await service.get_decision("s", "mission-2", decision["id"]) is None
    assert await service.get_decision("other", "mission-1", decision["id"]) is None


@pytest.mark.asyncio
async def test_list_decisions_is_scoped_to_session_and_mission(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    await service.create_decision(
        session_id="s",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.ARCHITECTURE,
        title="决策 A",
        content="内容 A",
    )
    await service.create_decision(
        session_id="s",
        mission_id="mission-2",
        actor="Planner-Agent",
        kind=DecisionKind.POLICY,
        title="决策 B",
        content="内容 B",
    )
    await service.create_decision(
        session_id="other",
        mission_id="mission-1",
        actor="Planner-Agent",
        kind=DecisionKind.EXPERIMENT,
        title="决策 C",
        content="内容 C",
    )

    decisions = await service.list_decisions("s", "mission-1")
    assert [item["title"] for item in decisions] == ["决策 A"]
