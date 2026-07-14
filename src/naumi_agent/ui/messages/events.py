"""Typed UI message classes — one per semantic event kind.

Each class adds the fields it needs on top of the base UIMessage.
All fields have defaults so that constructing a message from engine event
data is straightforward and never raises due to missing keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.ui.messages.base import MessageType, UIMessage

# ---------------------------------------------------------------------------
# User / Assistant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserMessage(UIMessage):
    """A user-submitted prompt or command."""

    content: str = ""
    is_command: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not hasattr(self, "_type_set") and self.type == MessageType.USER:
            pass  # already set by caller

    def summary(self) -> str:
        preview = self.content[:80].replace("\n", " ")
        return f"[user] {preview}"


@dataclass(frozen=True)
class AssistantStreamMessage(UIMessage):
    """Streaming token or boundary event from the model's final answer.

    The adapter emits separate messages for `response_start`, `token`,
    and `response_end` — they all share this type but differ in `phase`.
    """

    phase: str = ""  # "start" | "token" | "end"
    content: str = ""  # token text (empty for start/end)

    def summary(self) -> str:
        if self.phase == "start":
            return "[assistant] response_start"
        if self.phase == "end":
            return "[assistant] response_end"
        preview = self.content[:40].replace("\n", " ")
        return f"[assistant] {preview}"


# ---------------------------------------------------------------------------
# Thinking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThinkingMessage(UIMessage):
    """Extended thinking events from reasoning models."""

    phase: str = ""  # "start" | "delta" | "end"
    content: str = ""

    def summary(self) -> str:
        if self.phase == "start":
            return "[thinking] start"
        if self.phase == "end":
            return "[thinking] end"
        preview = self.content[:60].replace("\n", " ")
        return f"[thinking] {preview}"


# ---------------------------------------------------------------------------
# Tool lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPrepareMessage(UIMessage):
    """Tool arguments are being streamed from the model (preparation phase)."""

    tool_name: str = ""
    tool_call_id: str = ""
    phase: str = ""  # "start" | "snapshot" | "end"
    path: str = ""
    argument_chars: int = 0
    content_chars: int = 0
    content_lines: int = 0
    elapsed_ms: int = 0
    todo_total: int = 0
    todo_completed: int = 0
    todo_open: int = 0
    todo_items: tuple[Any, ...] = ()

    def summary(self) -> str:
        return f"[tool_prepare] {self.tool_name} {self.phase} {self.elapsed_ms}ms"


@dataclass(frozen=True)
class ToolUseMessage(UIMessage):
    """Tool execution has started."""

    tool_name: str = ""
    tool_call_id: str = ""
    args_summary: str = ""  # condensed parameter preview
    args_raw: str = ""  # raw JSON arguments (may be large; renderers should truncate)
    # Structured fields extracted BEFORE truncation for reliable card display
    primary_arg: str = ""  # e.g. file_path, command, query, url
    file_path: str = ""
    command: str = ""
    query: str = ""
    url: str = ""

    def summary(self) -> str:
        label = self.primary_arg or self.tool_name
        return f"[tool_use] {label}"


@dataclass(frozen=True)
class ToolResultMessage(UIMessage):
    """Tool execution has finished (success, error, skipped, or aborted)."""

    tool_name: str = ""
    tool_call_id: str = ""
    status: str = ""  # "success" | "error" | "skipped" | "aborted" | "failed"
    duration_ms: int = 0
    content_preview: str = ""  # truncated output for display
    content_length: int = 0  # full output length
    preview_format: str = "text"  # "text" | "code" | "diff" | "markdown"
    preview_language: str = ""  # language hint for syntax highlighting
    content_truncated: bool = False

    def summary(self) -> str:
        return f"[tool_result] {self.tool_name} {self.status} {self.duration_ms}ms"


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionBubbleMessage(UIMessage):
    """Permission decision bubbled from engine or subagent."""

    agent_name: str = ""
    tool_name: str = ""
    # "needs_confirmation" | "confirmed" | "denied"
    # | "blocked" | "bypass_enabled" | ...
    status: str = ""
    reason: str = ""
    requires_confirmation: bool = False

    def summary(self) -> str:
        return f"[permission] {self.agent_name} -> {self.tool_name} [{self.status}]"


# ---------------------------------------------------------------------------
# Todo / Task
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TodoStatusMessage(UIMessage):
    """Task list snapshot after a task-mutating tool runs."""

    source: str = ""  # tool name that triggered the snapshot
    total_count: int = 0
    open_count: int = 0
    completed_count: int = 0
    items: tuple[dict[str, Any], ...] = ()  # immutable
    summary_text: str = ""  # formatted text summary

    def summary(self) -> str:
        return f"[todo] {self.completed_count}/{self.total_count} completed"


# ---------------------------------------------------------------------------
# Runtime / system
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeStatusMessage(UIMessage):
    """Lifecycle and performance telemetry (run_started, turn_start, perf_phase)."""

    phase: str = ""  # "run_started" | "turn_start" | "perf_phase" | ...
    label: str = ""  # human-readable phase label
    duration_ms: int = 0
    model: str = ""
    turn: int = 0

    def summary(self) -> str:
        parts = [f"[runtime] {self.phase}"]
        if self.label:
            parts.append(self.label)
        if self.duration_ms:
            parts.append(f"{self.duration_ms}ms")
        return " ".join(parts)


@dataclass(frozen=True)
class CompletionReceiptMessage(UIMessage):
    """Authoritative backend evidence for one finished run."""

    receipt: CompletionReceipt = field(
        default_factory=lambda: CompletionReceipt.from_dict(
            {
                "schema_version": 1,
                "receipt_id": "uninitialized",
                "run_id": "uninitialized",
                "outcome": "failed",
                "git_state": {"available": False, "dirty": False},
            }
        )
    )

    def summary(self) -> str:
        return f"[receipt] {self.receipt.receipt_id} {self.receipt.outcome}"


@dataclass(frozen=True)
class RuntimeNotificationMessage(UIMessage):
    """Background task or scheduler notification injected into context."""

    source: str = ""  # "background" | "schedule"
    title: str = ""
    count: int = 0
    preview: str = ""

    def summary(self) -> str:
        return f"[notification] {self.source} x{self.count}"


@dataclass(frozen=True)
class SubagentEventMessage(UIMessage):
    """Subagent lifecycle event."""

    agent_name: str = ""
    task_id: str = ""
    status: str = ""  # "completed" | "error" | "failed" | ...
    description: str = ""
    message: str = ""
    tokens: int = 0
    cost: float = 0.0
    timestamp: float = 0.0

    def summary(self) -> str:
        return f"[subagent] {self.agent_name} {self.status}"


@dataclass(frozen=True)
class TeamEventMessage(UIMessage):
    """Team protocol event (message bus)."""

    event_type: str = ""
    sender: str = ""
    recipient: str = ""
    priority: str = "normal"  # "normal" | "high" | "critical"
    message: str = ""

    def summary(self) -> str:
        return f"[team] {self.sender} -> {self.recipient} [{self.priority}]"


@dataclass(frozen=True)
class HookTraceMessage(UIMessage):
    """Hook execution trace (triggered, aborted, or errored)."""

    point: str = ""
    callback: str = ""
    duration_ms: int = 0
    error: str = ""
    aborted: bool = False

    def summary(self) -> str:
        status = "aborted" if self.aborted else "error" if self.error else "triggered"
        return f"[hook] {self.point} -> {self.callback} ({status})"


@dataclass(frozen=True)
class ContextCompactMessage(UIMessage):
    """Context window compaction event."""

    before: int = 0
    after: int = 0
    archived_tool_results: int = 0
    preserved_sections: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        return f"[compact] {self.before} -> {self.after} messages"


@dataclass(frozen=True)
class RecoveryMessage(UIMessage):
    """Model recovery event (output continuation, prompt-too-long retry)."""

    reason: str = ""
    action: str = ""
    phase: str = ""  # "started" | "continued" | "completed" | "failed"
    before: int | str = 0
    after: int | str = 0
    unit: str = ""

    def summary(self) -> str:
        return f"[recovery] {self.action} ({self.reason}) {self.phase}"


@dataclass(frozen=True)
class ErrorMessage(UIMessage):
    """Error from engine or model."""

    message: str = ""

    def summary(self) -> str:
        preview = self.message[:80].replace("\n", " ")
        return f"[error] {preview}"


@dataclass(frozen=True)
class SystemNoticeMessage(UIMessage):
    """Generic system notice (catch-all for events not yet mapped)."""

    title: str = ""
    content: str = ""
    level: str = "info"  # "success" | "info" | "warning" | "error" | "debug"

    def summary(self) -> str:
        label = self.title or self.level
        return f"[system:{label}] {self.content[:60]}"
