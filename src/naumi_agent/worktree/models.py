"""Worktree data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorktreeStatus(StrEnum):
    """Lifecycle/status labels for an isolated worktree."""

    CLEAN = "clean"
    DIRTY = "dirty"
    MISSING = "missing"
    KEPT = "kept"


@dataclass
class WorktreeRecord:
    """Persistent metadata for one isolated git worktree."""

    name: str
    path: str
    branch: str
    base_ref: str
    status: WorktreeStatus = WorktreeStatus.CLEAN
    task_id: str = ""
    dirty_files: int = 0
    commits_ahead: int = 0
    created_at: str = ""
    updated_at: str = ""
    kept_reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def removable(self) -> bool:
        """Whether the worktree can be removed without discarding work."""
        return (
            self.status == WorktreeStatus.CLEAN
            and self.dirty_files == 0
            and self.commits_ahead == 0
        )
