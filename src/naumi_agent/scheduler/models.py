"""Scheduler data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ScheduleKind(StrEnum):
    """Supported schedule expression types."""

    ONCE = "once"
    CRON = "cron"


class ScheduleStatus(StrEnum):
    """Lifecycle state for a scheduled job."""

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ScheduleTarget(StrEnum):
    """Delivery target for fired schedule events."""

    SESSION_MESSAGE = "session_message"


@dataclass
class ScheduleJob:
    """Persistent metadata for one scheduled reminder."""

    id: str
    kind: ScheduleKind
    expression: str
    prompt: str
    target: ScheduleTarget
    status: ScheduleStatus
    next_fire_at: str
    created_at: str
    last_fired_at: str = ""
    fired_count: int = 0

    @property
    def is_active(self) -> bool:
        return self.status == ScheduleStatus.ACTIVE


@dataclass
class ScheduleEvent:
    """A durable fired schedule event waiting to be delivered."""

    id: str
    schedule_id: str
    fired_at: str
    prompt: str
    target: ScheduleTarget
    delivered: bool = False
