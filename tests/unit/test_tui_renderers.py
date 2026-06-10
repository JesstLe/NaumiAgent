"""Tests for the TUI renderer registry."""

from __future__ import annotations

from typing import Any

from naumi_agent.tui.renderers.registry import TUIRenderer, _highlightable_tool_preview
from naumi_agent.ui.messages import EngineEventAdapter, ToolResultMessage
from naumi_agent.ui.messages.base import MessageType


class FakeChat:
    def __init__(self) -> None:
        self.started_tool = ""
        self.mounted: list[Any] = []

    def start_tool(self, name: str) -> None:
        self.started_tool = name

    def end_tool(
        self,
        label: str,
        status: str,
        duration_ms: int,
        content_preview: str,
    ) -> None:
        self.mounted.append((label, status, duration_ms, content_preview))

    def mount(self, widget: Any) -> None:
        self.mounted.append(widget)


class FakeStatus:
    def __init__(self) -> None:
        self.status_text = ""


class FakeTodo:
    def __init__(self) -> None:
        self.status_text = ""


def _mounted_plain_text(chat: FakeChat) -> str:
    return "\n".join(
        getattr(widget.content, "plain", str(widget.content))
        for widget in chat.mounted
    )


def test_tool_use_shows_structured_path() -> None:
    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = FakeChat()
    status = FakeStatus()
    args = '{"file_path": "/tmp/showcase/index.html", "content": "' + "x" * 1000 + '"}'

    msg = adapter.adapt("tool_start", {"name": "file_write", "args": args})
    assert msg is not None
    renderer.render(msg, chat, status, FakeTodo())

    assert "file_write" in chat.started_tool
    assert "/tmp/showcase/index.html" in chat.started_tool
    assert "/tmp/showcase/index.html" in status.status_text


def test_permission_renderer_keeps_literal_status_text() -> None:
    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = FakeChat()

    msg = adapter.adapt(
        "permission_bubble",
        {
            "agent_name": "main",
            "tool_name": "bash_run",
            "status": "needs_confirmation",
            "reason": "需要确认。",
        },
    )
    assert msg is not None

    renderer.render(msg, chat, FakeStatus(), FakeTodo())

    mounted = _mounted_plain_text(chat)
    assert "[needs_confirmation]" in mounted
    assert "bash_run" in mounted


def test_tool_result_wraps_code_preview_with_language_fence() -> None:
    msg = ToolResultMessage(
        type=MessageType.TOOL_RESULT,
        tool_name="code_execute",
        status="success",
        content_preview="print('ok')",
        preview_format="code",
        preview_language="python",
    )

    assert _highlightable_tool_preview(msg) == "```python\nprint('ok')\n```"


def test_recovery_renderer_escapes_markup_sensitive_text() -> None:
    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = FakeChat()

    msg = adapter.adapt(
        "recovery_event",
        {
            "phase": "started",
            "action": "compact[unsafe]",
            "reason": "[red]context[/red]",
            "before": "10[bad]",
            "after": "5",
            "unit": "messages",
        },
    )
    assert msg is not None

    renderer.render(msg, chat, FakeStatus(), FakeTodo())

    assert len(chat.mounted) == 1


def test_tui_renderer_skips_duplicate_message_id() -> None:
    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = FakeChat()
    msg = adapter.adapt(
        "recovery_event",
        {
            "phase": "started",
            "action": "compact",
            "reason": "context",
            "before": "10",
            "after": "5",
            "unit": "messages",
        },
    )
    assert msg is not None

    renderer.render(msg, chat, FakeStatus(), FakeTodo())
    renderer.render(msg, chat, FakeStatus(), FakeTodo())

    assert len(chat.mounted) == 1
    assert renderer.cache_stats().hits == 1
