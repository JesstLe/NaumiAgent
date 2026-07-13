"""Unified UI message model — shared between CLI and TUI renderers.

Every engine event is converted into a typed UIMessage via the adapter.
CLI and TUI consume the same message types but render them independently.
"""

from naumi_agent.ui.messages.adapter import EngineEventAdapter
from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    CompletionReceiptMessage,
    ContextCompactMessage,
    ErrorMessage,
    HookTraceMessage,
    PermissionBubbleMessage,
    RecoveryMessage,
    RuntimeNotificationMessage,
    RuntimeStatusMessage,
    SubagentEventMessage,
    SystemNoticeMessage,
    TeamEventMessage,
    ThinkingMessage,
    TodoStatusMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserMessage,
)

__all__ = [
    "EngineEventAdapter",
    "MessageType",
    "UIMessage",
    "AssistantStreamMessage",
    "CompletionReceiptMessage",
    "ContextCompactMessage",
    "ErrorMessage",
    "HookTraceMessage",
    "PermissionBubbleMessage",
    "RecoveryMessage",
    "RuntimeNotificationMessage",
    "RuntimeStatusMessage",
    "SubagentEventMessage",
    "SystemNoticeMessage",
    "TeamEventMessage",
    "ThinkingMessage",
    "TodoStatusMessage",
    "ToolPrepareMessage",
    "ToolResultMessage",
    "ToolUseMessage",
    "UserMessage",
]
