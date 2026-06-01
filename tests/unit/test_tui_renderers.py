"""Tests for the TUI renderer registry."""

from __future__ import annotations

from typing import Any

from naumi_agent.tui.renderers.registry import TUIRenderer
from naumi_agent.ui.messages import EngineEventAdapter


class FakeChat:
    def __init__(self) -> None:
        self.started_tool = ""
        self.mounted: list[Any] = []

    def start_tool(self, name: str) -> None:
        self.started_tool = name

    def mount(self, widget: Any) -> None:
        self.mounted.append(widget)


class FakeStatus:
    def __init__(self) -> None:
        self.status_text = ""


class FakeTodo:
    def __init__(self) -> None:
        self.status_text = ""


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
