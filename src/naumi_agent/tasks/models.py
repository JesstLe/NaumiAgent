"""任务数据模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class Task:
    """单个任务项."""

    id: str
    session_id: str
    subject: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    active_form: str | None = None
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def is_blocked(self, all_tasks: list[Task]) -> bool:
        """Check if any blocking task is still unresolved."""
        unresolved = {t.id for t in all_tasks if t.status != TaskStatus.COMPLETED}
        return bool(set(self.blocked_by) & unresolved)
