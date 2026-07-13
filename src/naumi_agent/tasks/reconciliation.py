"""Terminal reconciliation for agent-managed Todo state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.tasks.store import TaskStore

logger = logging.getLogger(__name__)

_UNRECONCILED_REASON = "Agent 结束前未完成状态对账"


class TodoReconciliationAction(StrEnum):
    NONE = "none"
    RETRY = "retry"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TodoReconciliationResult:
    action: TodoReconciliationAction
    instruction: str = ""
    warning: str = ""
    changed_tasks: tuple[Task, ...] = ()


async def reconcile_todos(
    store: TaskStore,
    *,
    attempted: bool,
) -> TodoReconciliationResult:
    """Require one explicit agent correction before blocking stale active tasks."""
    try:
        tasks = await store.list_tasks()
        active = [task for task in tasks if task.status == TaskStatus.IN_PROGRESS]
        if not active:
            return TodoReconciliationResult(TodoReconciliationAction.NONE)

        if not attempted:
            task_ids = "、".join(f"#{task.id}" for task in active)
            return TodoReconciliationResult(
                TodoReconciliationAction.RETRY,
                instruction=(
                    f"最终回答前必须对账 Todo。当前仍在进行：{task_ids}。"
                    "请调用 task_update 或 todo_write，将其明确更新为 completed、"
                    "blocked 或 pending；不要直接输出最终回答。"
                ),
            )

        changed = await store.block_unreconciled_tasks(_UNRECONCILED_REASON)
        return TodoReconciliationResult(
            TodoReconciliationAction.BLOCKED,
            changed_tasks=tuple(changed),
        )
    except Exception as exc:
        logger.warning("Todo reconciliation failed: %s", type(exc).__name__)
        return TodoReconciliationResult(
            TodoReconciliationAction.NONE,
            warning="Todo 状态读取失败，无法完成终态对账。",
        )
