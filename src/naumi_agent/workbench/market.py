"""Task market claim and lease logic."""

from __future__ import annotations

from datetime import datetime, timedelta

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore, TaskWriteItem
from naumi_agent.workbench.models import Lease, LeaseState
from naumi_agent.workbench.store import WorkbenchStore


class TaskMarket:
    """Coordinate issue claims without bypassing the existing TaskStore."""

    def __init__(self, *, task_store: TaskStore, workbench_store: WorkbenchStore) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store

    async def claim(
        self,
        *,
        task_id: str,
        agent_id: str,
        duration_minutes: int = 45,
        worktree_name: str = "",
    ) -> Lease:
        if not self._task_store.session_id:
            raise ValueError("当前没有活动会话，不能认领任务")
        task = await self._task_store.get_task(task_id)
        if task is None:
            raise ValueError(f"任务 #{task_id} 不存在")
        if task.status == TaskStatus.COMPLETED:
            raise ValueError(f"任务 #{task_id} 已完成，不能认领")

        active = await self._workbench_store.get_active_lease(self._task_store.session_id, task_id)
        if active is not None:
            raise ValueError(f"任务 #{task_id} 已经被 {active.agent_id} 认领")

        expires_at = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(
            timespec="seconds"
        )
        lease = await self._workbench_store.create_lease(
            session_id=self._task_store.session_id,
            task_id=task_id,
            agent_id=agent_id,
            expires_at=expires_at,
            worktree_name=worktree_name,
        )
        if worktree_name:
            await self._workbench_store.set_issue_worktree(
                session_id=self._task_store.session_id,
                task_id=task_id,
                worktree_name=worktree_name,
            )
        await self._task_store.update_task(
            task_id,
            status=TaskStatus.IN_PROGRESS,
            active_form=f"{agent_id} 已认领，租约到期：{lease.expires_at}",
            owner=f"agent:{agent_id}",
        )
        await self._workbench_store.append_event(
            session_id=self._task_store.session_id,
            type="issue.claimed",
            actor=agent_id,
            subject_id=task_id,
            payload={"lease_id": lease.id, "expires_at": lease.expires_at},
        )
        return lease

    async def release(self, lease_id: str) -> Lease | None:
        lease = await self._workbench_store.update_lease_state(lease_id, LeaseState.RELEASED)
        if lease is None:
            return None
        await self._reset_task_to_pending(lease.task_id)
        await self._workbench_store.append_event(
            session_id=lease.session_id,
            type="issue.released",
            actor=lease.agent_id,
            subject_id=lease.task_id,
            payload={"lease_id": lease.id},
        )
        return lease

    async def expire_overdue_leases(self, *, now: datetime | None = None) -> list[Lease]:
        if not self._task_store.session_id:
            return []
        now_text = (now or datetime.now()).isoformat(timespec="seconds")
        overdue = await self._workbench_store.list_overdue_leases(
            self._task_store.session_id,
            now_text,
        )
        expired: list[Lease] = []
        for lease in overdue:
            updated = await self._workbench_store.update_lease_state(lease.id, LeaseState.EXPIRED)
            if updated is None:
                continue
            await self._reset_task_to_pending(lease.task_id)
            await self._workbench_store.append_event(
                session_id=lease.session_id,
                type="lease.expired",
                actor="system",
                subject_id=lease.task_id,
                payload={"lease_id": lease.id, "agent_id": lease.agent_id},
            )
            expired.append(updated)
        return expired

    async def _reset_task_to_pending(self, task_id: str) -> None:
        task = await self._task_store.get_task(task_id)
        if task is None:
            return
        await self._task_store.write_tasks(
            [
                TaskWriteItem(
                    id=task.id,
                    subject=task.subject,
                    description=task.description,
                    status=TaskStatus.PENDING,
                    active_form=None,
                    owner=None,
                    blocked_by=task.blocked_by,
                ),
            ],
        )
