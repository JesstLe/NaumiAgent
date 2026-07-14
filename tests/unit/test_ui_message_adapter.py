"""Tests for the UI message adapter — engine events → typed UIMessages."""

from __future__ import annotations

import pytest

from naumi_agent.ui.messages import (
    AssistantStreamMessage,
    CompletionReceiptMessage,
    ContextCompactMessage,
    EngineEventAdapter,
    ErrorMessage,
    HookTraceMessage,
    MessageType,
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
)
from naumi_agent.ui.protocol import ui_message_payload


@pytest.fixture
def adapter() -> EngineEventAdapter:
    return EngineEventAdapter()


# ---------------------------------------------------------------------------
# Core contract tests
# ---------------------------------------------------------------------------


class TestAdapterBasics:
    """Verify the adapter converts every known engine event correctly."""

    def test_unknown_event_returns_none(self, adapter: EngineEventAdapter) -> None:
        result = adapter.adapt("unknown_event_xyz", {})
        assert result is None

    def test_all_message_types_are_frozen(self) -> None:
        """Every UIMessage subclass must be frozen (immutable)."""
        from naumi_agent.ui.messages.events import (
            ToolResultMessage,
        )

        msg = ToolResultMessage(
            type=MessageType.TOOL_RESULT,
            tool_name="test",
            status="success",
        )
        with pytest.raises(AttributeError):
            msg.tool_name = "changed"  # type: ignore[mut]

    def test_message_id_auto_generated(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("error", {"message": "test"})
        assert msg is not None
        assert msg.message_id
        assert len(msg.message_id) == 12

    def test_raw_event_preserved(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("error", {"message": "test"})
        assert msg is not None
        assert msg.raw_event == "error"

    def test_raw_data_not_stored_by_default(self, adapter: EngineEventAdapter) -> None:
        """The adapter must not store large raw_data dicts by default."""
        msg = adapter.adapt("tool_end", {
            "name": "file_write",
            "status": "success",
            "content": "x" * 10000,
        })
        assert msg is not None
        assert msg.raw_data is None


class TestHarnessCompletionMessages:
    def test_correction_is_visible_warning(
        self,
        adapter: EngineEventAdapter,
    ) -> None:
        msg = adapter.adapt(
            "harness_completion_correction",
            {"message": "缺少必需检查 unit。", "run_id": "run-1"},
        )

        assert isinstance(msg, SystemNoticeMessage)
        assert msg.level == "warning"
        assert msg.title == "Harness 完成门禁"
        assert "unit" in msg.content

    def test_verified_receipt_exposes_checks_changes_and_fingerprint(
        self,
        adapter: EngineEventAdapter,
    ) -> None:
        msg = adapter.adapt(
            "harness_completion_receipt",
            {
                "run_id": "run-1",
                "status": "completed_verified",
                "checks": [{"id": "unit", "status": "passed"}],
                "changed_files": ["source.py"],
                "warnings": [],
                "tree_fingerprint": "sha256:abc",
            },
        )

        assert isinstance(msg, SystemNoticeMessage)
        assert msg.level == "success"
        assert "已验证完成" in msg.content
        assert "unit=passed" in msg.content
        assert "source.py" in msg.content
        assert "sha256:abc" in msg.content

    def test_blocked_receipt_is_error(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt(
            "harness_completion_receipt",
            {
                "run_id": "run-2",
                "status": "blocked",
                "warnings": ["工作树不可读取。"],
            },
        )

        assert isinstance(msg, SystemNoticeMessage)
        assert msg.level == "error"
        assert "工作树不可读取" in msg.content


# ---------------------------------------------------------------------------
# Thinking messages
# ---------------------------------------------------------------------------


class TestThinkingMessages:

    def test_thinking_start(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("thinking_start", {})
        assert isinstance(msg, ThinkingMessage)
        assert msg.type == MessageType.THINKING
        assert msg.phase == "start"

    def test_thinking_delta(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("thinking_delta", {"content": "I should check..."})
        assert isinstance(msg, ThinkingMessage)
        assert msg.phase == "delta"
        assert msg.content == "I should check..."

    def test_thinking_end(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("thinking_end", {"content": "full thinking text"})
        assert isinstance(msg, ThinkingMessage)
        assert msg.phase == "end"


# ---------------------------------------------------------------------------
# Assistant stream
# ---------------------------------------------------------------------------


class TestAssistantStream:

    def test_response_start(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("response_start", {})
        assert isinstance(msg, AssistantStreamMessage)
        assert msg.phase == "start"

    def test_token(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("token", {"content": "Hello"})
        assert isinstance(msg, AssistantStreamMessage)
        assert msg.phase == "token"
        assert msg.content == "Hello"

    def test_response_end(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("response_end", {})
        assert isinstance(msg, AssistantStreamMessage)
        assert msg.phase == "end"


# ---------------------------------------------------------------------------
# Tool lifecycle
# ---------------------------------------------------------------------------


class TestToolLifecycle:

    def test_tool_prepare_start(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_prepare_start", {
            "name": "file_write",
            "tool_call_id": "call-file",
            "path": "/tmp/test.py",
            "argument_chars": 500,
            "content_lines": 30,
            "elapsed_ms": 120,
        })
        assert isinstance(msg, ToolPrepareMessage)
        assert msg.tool_name == "file_write"
        assert msg.tool_call_id == "call-file"
        assert msg.phase == "start"
        assert msg.path == "/tmp/test.py"
        assert msg.content_lines == 30

    def test_tool_prepare_snapshot(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_prepare_snapshot", {
            "name": "file_write",
            "tool_call_id": "call-file",
            "argument_chars": 5000,
            "elapsed_ms": 2000,
        })
        assert isinstance(msg, ToolPrepareMessage)
        assert msg.phase == "snapshot"
        assert msg.tool_call_id == "call-file"
        assert msg.argument_chars == 5000

    def test_tool_prepare_end(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_prepare_end", {
            "name": "todo_write",
            "tool_call_id": "call-todo",
            "argument_chars": 512,
            "elapsed_ms": 3000,
            "todo_total": 2,
            "todo_completed": 1,
            "todo_open": 1,
            "todo_items": [{"id": "2", "status": "pending", "subject": "验证"}],
        })
        assert isinstance(msg, ToolPrepareMessage)
        assert msg.phase == "end"
        assert msg.tool_name == "todo_write"
        assert msg.tool_call_id == "call-todo"
        assert msg.argument_chars == 512
        assert msg.todo_total == 2
        assert msg.todo_completed == 1
        assert msg.todo_open == 1
        assert msg.todo_items == ({"id": "2", "status": "pending", "subject": "验证"},)

    def test_tool_start(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_start", {
            "name": "bash_run",
            "args": "ls -la",
        })
        assert isinstance(msg, ToolUseMessage)
        assert msg.tool_name == "bash_run"
        assert msg.args_summary  # should contain "ls -la"
        assert msg.args_raw == ""  # raw NOT stored

    def test_tool_start_large_args(self, adapter: EngineEventAdapter) -> None:
        """Large arguments must be summarized, not stored in full."""
        huge_args = "x" * 5000
        msg = adapter.adapt("tool_start", {"name": "file_write", "args": huge_args})
        assert isinstance(msg, ToolUseMessage)
        assert len(msg.args_summary) < 300  # truncated

    def test_tool_end_success(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_read",
            "status": "success",
            "duration_ms": 150,
            "content": "file contents here",
        })
        assert isinstance(msg, ToolResultMessage)
        assert msg.tool_name == "file_read"
        assert msg.status == "success"
        assert msg.duration_ms == 150
        assert msg.content_preview == "file contents here"
        assert msg.content_length == 18
        assert msg.preview_format == "text"
        assert msg.preview_language == ""

    def test_tool_end_large_content_truncated(self, adapter: EngineEventAdapter) -> None:
        """Large tool output must be truncated in preview."""
        big_content = "line\n" * 200
        msg = adapter.adapt("tool_end", {
            "name": "bash_run",
            "status": "success",
            "content": big_content,
        })
        assert isinstance(msg, ToolResultMessage)
        assert len(msg.content_preview) <= 600  # truncated
        assert msg.content_length == len(big_content)
        assert msg.content_truncated

    def test_tool_end_detects_fenced_code_preview(
        self,
        adapter: EngineEventAdapter,
    ) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_write",
            "status": "success",
            "content": "✅ 已创建 demo.py\n\n```python\nprint('ok')\n```",
        })

        assert isinstance(msg, ToolResultMessage)
        assert msg.preview_format == "code"
        assert msg.preview_language == "python"

    def test_tool_end_detects_fenced_diff_preview(
        self,
        adapter: EngineEventAdapter,
    ) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_edit",
            "status": "success",
            "content": "```diff\n--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old\n+new\n```",
        })

        assert isinstance(msg, ToolResultMessage)
        assert msg.preview_format == "diff"
        assert msg.preview_language == "diff"

    def test_tool_end_error(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "bash_run",
            "status": "error",
            "duration_ms": 50,
            "content": "command failed",
        })
        assert isinstance(msg, ToolResultMessage)
        assert msg.status == "error"

    def test_tool_end_skipped(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_write",
            "status": "skipped",
            "duration_ms": 0,
        })
        assert isinstance(msg, ToolResultMessage)
        assert msg.status == "skipped"

    def test_tool_end_aborted(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_write",
            "status": "aborted",
            "duration_ms": 0,
            "content": "被 Hook 中止",
        })
        assert isinstance(msg, ToolResultMessage)
        assert msg.status == "aborted"


# ---------------------------------------------------------------------------
# Permission bubbles
# ---------------------------------------------------------------------------


class TestPermissionBubbles:

    def test_needs_confirmation(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("permission_bubble", {
            "agent_name": "main",
            "tool_name": "bash_run",
            "status": "needs_confirmation",
            "reason": "该工具需要用户确认。",
            "requires_confirmation": True,
        })
        assert isinstance(msg, PermissionBubbleMessage)
        assert msg.tool_name == "bash_run"
        assert msg.requires_confirmation is True

    def test_blocked(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("permission_bubble", {
            "tool_name": "file_write",
            "status": "blocked",
            "reason": "Plan mode only allows read-only tools.",
        })
        assert isinstance(msg, PermissionBubbleMessage)
        assert msg.status == "blocked"

    def test_confirmed(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("permission_bubble", {
            "tool_name": "bash_run",
            "status": "confirmed",
        })
        assert isinstance(msg, PermissionBubbleMessage)
        assert msg.status == "confirmed"


# ---------------------------------------------------------------------------
# Task / todo
# ---------------------------------------------------------------------------


class TestTodoStatus:

    def test_task_snapshot(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("task_snapshot", {
            "source": "todo_write",
            "count": 5,
            "open_count": 3,
            "completed_count": 2,
            "items": [
                {"id": 1, "status": "in_progress", "subject": "Write tests"},
                {"id": 2, "status": "pending", "subject": "Review code"},
            ],
            "summary": "2/5 completed",
        })
        assert isinstance(msg, TodoStatusMessage)
        assert msg.total_count == 5
        assert msg.open_count == 3
        assert msg.completed_count == 2
        assert len(msg.items) == 2
        # items are immutable tuple
        assert isinstance(msg.items, tuple)

    def test_task_snapshot_empty(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("task_snapshot", {"source": "todo_write"})
        assert isinstance(msg, TodoStatusMessage)
        assert msg.total_count == 0
        assert msg.items == ()


# ---------------------------------------------------------------------------
# Subagent / team / hook
# ---------------------------------------------------------------------------


class TestSubagentEvents:

    def test_completed(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("subagent_event", {
            "agent_name": "security-analyst",
            "task_id": "task-123",
            "status": "completed",
            "description": "扫描安全边界",
            "message": "扫描完成",
            "tokens": 321,
            "cost": 0.0123,
            "timestamp": 1784000000.5,
        })
        assert isinstance(msg, SubagentEventMessage)
        assert msg.agent_name == "security-analyst"
        assert msg.status == "completed"
        assert msg.description == "扫描安全边界"
        assert msg.message == "扫描完成"
        assert msg.tokens == 321
        assert msg.cost == 0.0123
        assert msg.timestamp == 1784000000.5
        payload = ui_message_payload(msg)
        assert payload["description"] == "扫描安全边界"
        assert payload["tokens"] == 321
        assert payload["cost"] == 0.0123
        assert payload["timestamp"] == 1784000000.5

    def test_failed(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("subagent_event", {
            "status": "error",
            "agent_name": "reviewer",
            "tokens": "invalid",
            "cost": "nan",
            "timestamp": "inf",
        })
        assert isinstance(msg, SubagentEventMessage)
        assert msg.status == "error"
        assert msg.tokens == 0
        assert msg.cost == 0.0
        assert msg.timestamp == 0.0


class TestTeamEvents:

    def test_team_message(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("team_event", {
            "event_type": "handoff",
            "sender": "agent-1",
            "recipient": "agent-2",
            "priority": "high",
            "message": "需要你的审查",
        })
        assert isinstance(msg, TeamEventMessage)
        assert msg.sender == "agent-1"
        assert msg.priority == "high"

    def test_team_broadcast(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("team_event", {
            "event_type": "blocker",
            "sender": "agent-1",
            "priority": "critical",
        })
        assert isinstance(msg, TeamEventMessage)
        assert msg.recipient == "广播"
        assert msg.priority == "critical"


class TestHookTrace:

    def test_triggered(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("hook_trace", {
            "point": "tool_execute_start",
            "callback": "format_python",
            "duration_ms": 15,
        })
        assert isinstance(msg, HookTraceMessage)
        assert msg.aborted is False
        assert msg.error == ""

    def test_aborted(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("hook_trace", {
            "point": "tool_execute_start",
            "callback": "security_check",
            "aborted": True,
        })
        assert isinstance(msg, HookTraceMessage)
        assert msg.aborted is True

    def test_error(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("hook_trace", {
            "point": "tool_execute_start",
            "callback": "bad_hook",
            "error": "ImportError: no module",
        })
        assert isinstance(msg, HookTraceMessage)
        assert msg.error == "ImportError: no module"


# ---------------------------------------------------------------------------
# Runtime / system events
# ---------------------------------------------------------------------------


class TestRuntimeEvents:

    def test_completion_receipt(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt(
            "completion_receipt",
            {
                "schema_version": 1,
                "receipt_id": "receipt-adapter",
                "run_id": "run-adapter",
                "outcome": "partial",
                "summary": "验证未通过。",
                "git_state": {"available": True, "dirty": True},
            },
        )
        assert isinstance(msg, CompletionReceiptMessage)
        assert msg.receipt.receipt_id == "receipt-adapter"
        assert msg.receipt.outcome == "partial"

    def test_run_started(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("run_started", {"task": "test"})
        assert isinstance(msg, RuntimeStatusMessage)
        assert msg.phase == "run_started"

    def test_turn_start(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("turn_start", {"turn": 3, "model": "gpt-4o"})
        assert isinstance(msg, RuntimeStatusMessage)
        assert msg.turn == 3
        assert msg.model == "gpt-4o"

    def test_perf_phase(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("perf_phase", {
            "label": "模型首包",
            "duration_ms": 350,
            "turn": 1,
        })
        assert isinstance(msg, RuntimeStatusMessage)
        assert msg.phase == "perf_phase"
        assert msg.label == "模型首包"
        assert msg.duration_ms == 350

    def test_latency_metric(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("latency_metric", {
            "metric": "first_token",
            "label": "端到端首字",
            "duration_ms": 1200,
            "turn": 1,
        })
        assert isinstance(msg, RuntimeStatusMessage)
        assert msg.phase == "latency_metric"
        assert msg.label == "端到端首字"
        assert msg.duration_ms == 1200
        assert msg.turn == 1


class TestRuntimeNotification:

    def test_background_notification(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("runtime_notification", {
            "source": "background",
            "title": "后台任务通知",
            "count": 2,
            "preview": "npm build completed",
        })
        assert isinstance(msg, RuntimeNotificationMessage)
        assert msg.source == "background"
        assert msg.count == 2


class TestContextCompaction:

    def test_compaction(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("context_compacted", {
            "before": 50,
            "after": 20,
            "archived_tool_results": 3,
            "preserved_sections": ["todo", "team_protocol"],
            "warnings": ["有 2 个未完成 todo"],
        })
        assert isinstance(msg, ContextCompactMessage)
        assert msg.before == 50
        assert msg.after == 20
        assert msg.archived_tool_results == 3
        assert msg.preserved_sections == ("todo", "team_protocol")
        assert msg.warnings == ("有 2 个未完成 todo",)

    def test_compaction_minimal(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("context_compacted", {
            "before": 10,
            "after": 5,
        })
        assert isinstance(msg, ContextCompactMessage)
        assert msg.preserved_sections == ()
        assert msg.warnings == ()


class TestRecoveryEvents:

    def test_recovery_started(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("recovery_event", {
            "reason": "output_truncated",
            "action": "continue_output",
            "phase": "started",
            "attempt": 1,
            "before": 5000,
            "unit": "chars",
        })
        assert isinstance(msg, RecoveryMessage)
        assert msg.reason == "output_truncated"
        assert msg.phase == "started"

    def test_recovery_completed(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("recovery_event", {
            "reason": "prompt_too_long",
            "action": "reactive_compact_retry",
            "phase": "completed",
            "before": 100,
            "after": 30,
        })
        assert isinstance(msg, RecoveryMessage)
        assert msg.phase == "completed"
        assert msg.after == 30


class TestErrorMessages:

    def test_error(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("error", {"message": "API key invalid"})
        assert isinstance(msg, ErrorMessage)
        assert msg.message == "API key invalid"

    def test_error_missing_message(self, adapter: EngineEventAdapter) -> None:
        msg = adapter.adapt("error", {})
        assert isinstance(msg, ErrorMessage)
        assert msg.message == ""


# ---------------------------------------------------------------------------
# Summary / display
# ---------------------------------------------------------------------------


class TestSummary:

    def test_all_message_types_have_summary(self, adapter: EngineEventAdapter) -> None:
        """Every produced message should have a non-empty summary()."""
        events = [
            ("thinking_start", {}),
            ("thinking_delta", {"content": "test"}),
            ("thinking_end", {}),
            ("response_start", {}),
            ("token", {"content": "hi"}),
            ("response_end", {}),
            ("tool_prepare_start", {"name": "file_write"}),
            ("tool_start", {"name": "bash_run"}),
            ("tool_end", {"name": "bash_run", "status": "success"}),
            ("hook_trace", {"point": "start", "callback": "test"}),
            ("task_snapshot", {"source": "todo_write"}),
            ("subagent_event", {"status": "completed"}),
            ("permission_bubble", {"status": "confirmed"}),
            ("team_event", {"sender": "a"}),
            ("runtime_notification", {}),
            ("context_compacted", {"before": 10, "after": 5}),
            ("recovery_event", {"reason": "test", "action": "test", "phase": "started"}),
            ("error", {"message": "test"}),
            ("run_started", {}),
            ("turn_start", {"turn": 1}),
        ]
        for event_name, data in events:
            msg = adapter.adapt(event_name, data)
            assert msg is not None, f"Event {event_name} returned None"
            assert msg.summary(), f"Event {event_name} has empty summary"


# ---------------------------------------------------------------------------
# Roundtrip: every engine event is handled
# ---------------------------------------------------------------------------


class TestDispatchCoverage:
    """Ensure every engine event emitted by engine.py is in the dispatch table."""

    # These are all events emitted by the engine (from engine.py analysis).
    ENGINE_EVENTS = [
        "completion_receipt",
        "harness_completion_correction",
        "harness_completion_receipt",
        "run_started",
        "turn_start",
        "perf_phase",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "response_start",
        "token",
        "response_end",
        "tool_prepare_start",
        "tool_prepare_snapshot",
        "tool_prepare_end",
        "tool_start",
        "tool_end",
        "hook_trace",
        "task_snapshot",
        "subagent_event",
        "permission_bubble",
        "team_event",
        "runtime_notification",
        "context_compacted",
        "recovery_event",
        "error",
    ]

    def test_all_engine_events_adapted(self, adapter: EngineEventAdapter) -> None:
        """Every known engine event must produce a non-None UIMessage."""
        for event in self.ENGINE_EVENTS:
            msg = adapter.adapt(event, {})
            assert msg is not None, f"Engine event '{event}' not handled"

    def test_dispatch_table_matches_engine_events(self) -> None:
        """Dispatch table must contain all engine events."""
        from naumi_agent.ui.messages.adapter import EngineEventAdapter

        dispatch = EngineEventAdapter._DISPATCH
        for event in self.ENGINE_EVENTS:
            assert event in dispatch, f"Missing dispatch for '{event}'"
