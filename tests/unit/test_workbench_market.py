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

    expired = await market.expire_overdue_leases(
        session_id="s",
        now=datetime.fromisoformat(lease.expires_at),
    )

    assert [item.id for item in expired] == [lease.id]
    refreshed = await task_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.owner is None


@pytest.mark.asyncio
async def test_claim_records_related_worktree_on_issue(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)

    await market.claim(
        task_id=task.id,
        agent_id="Backend-Agent",
        duration_minutes=45,
        worktree_name="issue-1-backend",
    )

    issue = await workbench_store.get_issue("s", task.id)
    assert issue is not None
    assert issue.related_worktree == "issue-1-backend"


@pytest.mark.asyncio
async def test_release_ignores_lease_from_other_session(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    own_lease = await market.claim(
        task_id=task.id, agent_id="Backend-Agent", duration_minutes=45
    )
    other_lease = await workbench_store.create_lease(
        session_id="other-session",
        task_id="other-task",
        agent_id="Other-Agent",
        expires_at="2099-01-01T00:00:00",
    )

    result = await market.release(session_id="s", lease_id=other_lease.id)

    assert result is None
    refreshed_other = await workbench_store.list_leases(
        "other-session", state=LeaseState.ACTIVE
    )
    assert [lease.id for lease in refreshed_other] == [other_lease.id]
    refreshed_own = await workbench_store.list_leases("s", state=LeaseState.ACTIVE)
    assert [lease.id for lease in refreshed_own] == [own_lease.id]


@pytest.mark.asyncio
async def test_expire_rejects_session_argument_that_differs_from_active_session(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    own_lease = await market.claim(
        task_id=task.id, agent_id="Backend-Agent", duration_minutes=1
    )
    expired_at = (datetime.fromisoformat(own_lease.expires_at) - timedelta(minutes=2)).isoformat()
    await workbench_store.force_lease_expiry_for_test(own_lease.id, expired_at)

    expired = await market.expire_overdue_leases(
        session_id="other-session",
        now=datetime.fromisoformat(own_lease.expires_at),
    )

    assert expired == []
    refreshed_own = await workbench_store.list_leases("s", state=LeaseState.ACTIVE)
    assert [lease.id for lease in refreshed_own] == [own_lease.id]


@pytest.mark.asyncio
async def test_release_rejects_session_argument_that_differs_from_active_session(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    own_lease = await market.claim(
        task_id=task.id, agent_id="Backend-Agent", duration_minutes=45
    )
    other_lease = await workbench_store.create_lease(
        session_id="other-session",
        task_id=task.id,
        agent_id="Other-Agent",
        expires_at="2099-01-01T00:00:00",
    )

    result = await market.release(session_id="other-session", lease_id=other_lease.id)

    assert result is None
    refreshed_other = await workbench_store.list_leases(
        "other-session", state=LeaseState.ACTIVE
    )
    assert [lease.id for lease in refreshed_other] == [other_lease.id]
    active_task = await task_store.get_task(task.id)
    assert active_task is not None
    assert active_task.status == TaskStatus.IN_PROGRESS
    refreshed_own = await workbench_store.list_leases("s", state=LeaseState.ACTIVE)
    assert [lease.id for lease in refreshed_own] == [own_lease.id]
