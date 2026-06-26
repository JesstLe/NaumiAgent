from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.market import TaskMarket
from naumi_agent.workbench.models import LeaseState
from naumi_agent.workbench.store import WorkbenchStore


@pytest.fixture
async def stores(tmp_path):
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    mission = await workbench_store.create_mission("s", "M", "G")
    task = await task_store.create_task("实现任务认领")
    await workbench_store.upsert_issue(session_id="s", task_id=task.id, mission_id=mission.id)
    return task_store, workbench_store, task


@pytest.mark.asyncio
async def test_claim_marks_task_in_progress_and_creates_lease(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)

    lease = await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=45)

    assert lease.agent_id == "Backend-Agent"
    assert lease.state == LeaseState.ACTIVE
    updated = await task_store.get_task(task.id)
    assert updated is not None
    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.owner == "agent:Backend-Agent"


@pytest.mark.asyncio
async def test_exclusive_issue_rejects_second_active_claim(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=45)

    with pytest.raises(ValueError, match="已经被 Backend-Agent 认领"):
        await market.claim(task_id=task.id, agent_id="Frontend-Agent", duration_minutes=45)


@pytest.mark.asyncio
async def test_expire_leases_returns_task_to_pending(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    lease = await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=1)
    expired_at = (datetime.fromisoformat(lease.expires_at) - timedelta(minutes=2)).isoformat()
    await workbench_store.force_lease_expiry_for_test(lease.id, expired_at)

    expired = await market.expire_overdue_leases(now=datetime.fromisoformat(lease.expires_at))

    assert [item.id for item in expired] == [lease.id]
    refreshed = await task_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.owner is None
