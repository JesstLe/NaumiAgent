"""Tests for the CLI renderer registry — UIMessage → ANSI text."""

from __future__ import annotations

import pytest

from naumi_agent.cli.renderers.registry import CLIRenderer
from naumi_agent.ui.messages import EngineEventAdapter
from naumi_agent.ui.messages.base import MessageType


@pytest.fixture
def adapter() -> EngineEventAdapter:
    return EngineEventAdapter()


@pytest.fixture
def renderer() -> CLIRenderer:
    return CLIRenderer()


class TestRendererDispatch:
    """Every registered message type can be rendered without error."""

    EVENTS_WITH_OUTPUT = [
        ("thinking_start", {}),
        ("thinking_delta", {"content": "hmm"}),
        ("thinking_end", {}),
        ("response_start", {}),
        ("token", {"content": "hello"}),
        ("response_end", {}),
        ("tool_prepare_start", {"name": "file_write", "argument_chars": 100}),
        ("tool_start", {"name": "bash_run", "args": "ls"}),
        ("tool_end", {"name": "bash_run", "status": "success", "duration_ms": 50}),
        ("tool_end", {"name": "file_write", "status": "error", "duration_ms": 10}),
        ("hook_trace", {"point": "start", "callback": "fmt"}),
        ("subagent_event", {"status": "completed", "agent_name": "a"}),
        ("permission_bubble", {"status": "confirmed", "tool_name": "bash"}),
        ("team_event", {"sender": "a", "recipient": "b"}),
        ("runtime_notification", {"title": "通知", "source": "bg", "count": 1}),
        ("run_started", {}),
        ("turn_start", {"model": "gpt-4o"}),
        ("context_compacted", {"before": 10, "after": 5}),
        ("recovery_event", {"reason": "trunc", "action": "continue", "phase": "started"}),
        ("error", {"message": "API error"}),
    ]

    @pytest.mark.parametrize("event,data", EVENTS_WITH_OUTPUT)
    def test_renders_without_error(
        self,
        adapter: EngineEventAdapter,
        renderer: CLIRenderer,
        event: str,
        data: dict,
    ) -> None:
        msg = adapter.adapt(event, data)
        assert msg is not None
        result = renderer.render(msg)
        # Some events produce output, some return None — just must not raise.
        if result is not None:
            assert isinstance(result, str)


class TestThinkingRendering:

    def test_thinking_start_has_emoji(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("thinking_start", {})
        text = renderer.render(msg)
        assert text is not None
        assert "思考中" in text

    def test_thinking_delta_preserves_content(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("thinking_delta", {"content": "I should verify"})
        text = renderer.render(msg)
        assert text is not None
        assert "I should verify" in text


class TestToolCardRendering:

    def test_tool_use_shows_running(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("tool_start", {"name": "bash_run", "args": "ls"})
        text = renderer.render(msg)
        assert text is not None
        assert "running" in text
        assert "bash_run" in text

    def test_tool_result_success(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "file_read",
            "status": "success",
            "duration_ms": 100,
        })
        text = renderer.render(msg)
        assert text is not None
        assert "success" in text
        assert "100ms" in text

    def test_tool_result_error(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("tool_end", {
            "name": "bash_run",
            "status": "error",
            "duration_ms": 5,
        })
        text = renderer.render(msg)
        assert text is not None
        assert "error" in text


class TestPermissionRendering:

    def test_blocked_is_red(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("permission_bubble", {
            "status": "blocked",
            "tool_name": "file_write",
            "agent_name": "main",
        })
        text = renderer.render(msg)
        assert text is not None
        assert "31" in text  # red ANSI code
        assert "blocked" in text

    def test_confirmed_is_green(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("permission_bubble", {
            "status": "confirmed",
            "tool_name": "bash_run",
        })
        text = renderer.render(msg)
        assert text is not None
        assert "32" in text  # green ANSI code


class TestRuntimeRendering:

    def test_run_started_shows_progress(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("run_started", {})
        text = renderer.render(msg)
        assert text is not None
        assert "准备执行" in text

    def test_turn_start_shows_model(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("turn_start", {"model": "gpt-4o"})
        text = renderer.render(msg)
        assert text is not None
        assert "gpt-4o" in text

    def test_perf_phase_shows_timing(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("perf_phase", {
            "label": "模型首包",
            "duration_ms": 250,
        })
        text = renderer.render(msg)
        assert text is not None
        assert "250ms" in text


class TestErrorRendering:

    def test_error_message(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        msg = adapter.adapt("error", {"message": "API key invalid"})
        text = renderer.render(msg)
        assert text is not None
        assert "API key invalid" in text
        assert "31" in text  # red


class TestTodoRendering:

    def test_todo_returns_none(
        self, adapter: EngineEventAdapter, renderer: CLIRenderer
    ) -> None:
        """Todo is rendered in the bottom bar, not in the output area."""
        msg = adapter.adapt("task_snapshot", {
            "source": "todo_write",
            "count": 3,
            "open_count": 1,
        })
        text = renderer.render(msg)
        # Todo goes to the bottom bar, not output
        assert text is None


class TestRegistryOverride:

    def test_override_renderer(self, renderer: CLIRenderer) -> None:
        """Custom renderer overrides the default."""
        from naumi_agent.ui.messages.events import ErrorMessage

        custom_msg = ErrorMessage(type=MessageType.ERROR, message="test")
        renderer.register(MessageType.ERROR, lambda _: "CUSTOM")
        result = renderer.render(custom_msg)
        assert result == "CUSTOM"

    def test_unregistered_type_returns_none(self, renderer: CLIRenderer) -> None:
        """Unregistered message types return None."""
        from naumi_agent.ui.messages.events import SystemNoticeMessage

        msg = SystemNoticeMessage(type=MessageType.SYSTEM_NOTICE)
        result = renderer.render(msg)
        assert result is None


class TestAllMessageTypesRegistered:
    """Ensure every MessageType has a renderer registered."""

    RENDERED_TYPES = {
        MessageType.THINKING,
        MessageType.ASSISTANT_STREAM,
        MessageType.TOOL_PREPARE,
        MessageType.TOOL_USE,
        MessageType.TOOL_RESULT,
        MessageType.HOOK_TRACE,
        MessageType.TODO_STATUS,
        MessageType.SUBAGENT_EVENT,
        MessageType.PERMISSION_BUBBLE,
        MessageType.TEAM_EVENT,
        MessageType.RUNTIME_NOTIFICATION,
        MessageType.RUNTIME_STATUS,
        MessageType.CONTEXT_COMPACT,
        MessageType.RECOVERY,
        MessageType.ERROR,
    }

    def test_all_adapted_types_have_renderers(self, renderer: CLIRenderer) -> None:
        for msg_type in self.RENDERED_TYPES:
            assert msg_type in renderer._registry, (
                f"No renderer registered for {msg_type.value}"
            )
