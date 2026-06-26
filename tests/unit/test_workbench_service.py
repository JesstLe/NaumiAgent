from __future__ import annotations

import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


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

    assert {event["id"] for event in all_events} == {event_a.id, event_b.id}
    assert all(event in [event_a.to_dict(), event_b.to_dict()] for event in all_events)

    limited = await service.list_events("s", limit=1)

    assert len(limited) == 1
    assert limited[0] in [event_a.to_dict(), event_b.to_dict()]


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
