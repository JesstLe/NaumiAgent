"""Base types for the unified UI message model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4


class MessageType(StrEnum):
    """Discriminator for UI message types — one per semantic event kind."""

    USER = "user"
    ASSISTANT_STREAM = "assistant_stream"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TOOL_PREPARE = "tool_prepare"
    PERMISSION_BUBBLE = "permission_bubble"
    TODO_STATUS = "todo_status"
    RUNTIME_STATUS = "runtime_status"
    RUNTIME_NOTIFICATION = "runtime_notification"
    SUBAGENT_EVENT = "subagent_event"
    TEAM_EVENT = "team_event"
    HOOK_TRACE = "hook_trace"
    CONTEXT_COMPACT = "context_compact"
    RECOVERY = "recovery"
    ERROR = "error"
    SYSTEM_NOTICE = "system_notice"


@dataclass(frozen=True)
class UIMessage:
    """Base class for all UI messages.

    Every message is immutable (frozen dataclass) so it can be safely stored,
    replayed, and shared across threads or async tasks.

    Attributes:
        type: Discriminator for dispatching to the correct renderer.
        message_id: Unique identifier for this message (auto-generated).
        raw_event: Original engine event name, preserved for debugging.
        raw_data: Original engine event payload, preserved for debugging.
          Does NOT store large tool arguments — only a reference/summary.
    """

    type: MessageType
    message_id: str = ""
    raw_event: str = ""
    raw_data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # frozen dataclass: use object.__setattr__ to set defaults
        if not self.message_id:
            object.__setattr__(self, "message_id", uuid4().hex[:12])

    def summary(self) -> str:
        """Return a one-line human-readable summary for debug/status display."""
        return f"[{self.type.value}] {self.message_id}"
