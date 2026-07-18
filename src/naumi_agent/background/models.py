"""Background task data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BackgroundStatus(StrEnum):
    """Lifecycle states for a background command."""

    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass
class BackgroundTask:
    """Persistent metadata for one background command."""

    id: str
    command: str
    cwd: str
    status: BackgroundStatus
    output_path: str
    pid: int | None = None
    process_group_id: int | None = None
    port_hints: list[int] = field(default_factory=list)
    exit_code: int | None = None
    started_at: str = ""
    completed_at: str = ""
    output_preview: str = ""
    error: str = ""
    notified: bool = False
    idempotency_key: str = ""
    timeout_seconds: int = 1800

    @property
    def is_finished(self) -> bool:
        return self.status in {
            BackgroundStatus.COMPLETED,
            BackgroundStatus.FAILED,
            BackgroundStatus.CANCELLED,
            BackgroundStatus.TIMED_OUT,
        }
