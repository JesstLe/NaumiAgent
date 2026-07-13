from __future__ import annotations

import pytest

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.reconciliation import (
    TodoReconciliationAction,
    reconcile_todos,
)
from naumi_agent.tasks.store import TaskStore


@pytest.fixture
def store(tmp_path) -> TaskStore:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("session-1")
    return task_store


@pytest.mark.asyncio
async def test_reconciliation_does_nothing_without_active_todo(store: TaskStore) -> None:
    await store.create_task("等待执行")

    result = await reconcile_todos(store, attempted=False)

    assert result.action == TodoReconciliationAction.NONE
    assert result.changed_tasks == ()


@pytest.mark.asyncio
async def test_first_reconciliation_requests_agent_status_update(store: TaskStore) -> None:
    task = await store.create_task("实现后端")
    await store.update_task(task.id, status=TaskStatus.IN_PROGRESS)

    result = await reconcile_todos(store, attempted=False)

    assert result.action == TodoReconciliationAction.RETRY
    assert "task_update" in result.instruction
    assert f"#{task.id}" in result.instruction
    assert (await store.get_task(task.id)).status == TaskStatus.IN_PROGRESS  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_second_reconciliation_blocks_stale_active_todo(store: TaskStore) -> None:
    task = await store.create_task("实现后端")
    await store.update_task(task.id, status=TaskStatus.IN_PROGRESS)

    result = await reconcile_todos(store, attempted=True)
    stored = await store.get_task(task.id)

    assert result.action == TodoReconciliationAction.BLOCKED
    assert [item.id for item in result.changed_tasks] == [task.id]
    assert stored is not None
    assert stored.status == TaskStatus.BLOCKED
    assert stored.active_form == "Agent 结束前未完成状态对账"


@pytest.mark.asyncio
async def test_reconciliation_returns_warning_when_store_fails() -> None:
    class FailingStore:
        async def list_tasks(self):
            raise OSError("database unavailable")

    result = await reconcile_todos(FailingStore(), attempted=False)  # type: ignore[arg-type]

    assert result.action == TodoReconciliationAction.NONE
    assert "Todo 状态读取失败" in result.warning
    assert "database unavailable" not in result.warning
