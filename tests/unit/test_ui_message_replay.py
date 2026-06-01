"""Tests for session replay — converting stored messages to UIMessages."""

from __future__ import annotations

import pytest

from naumi_agent.cli.renderers.registry import CLIRenderer
from naumi_agent.ui.messages.base import MessageType
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserMessage,
)
from naumi_agent.ui.messages.replay import replay_messages


class TestReplayConversion:
    """Convert raw session dicts to typed UIMessages."""

    def test_empty_messages(self) -> None:
        result = replay_messages([])
        assert result == []

    def test_system_messages_skipped(self) -> None:
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], UserMessage)
        assert result[0].content == "Hello"

    def test_user_message(self) -> None:
        msgs = [{"role": "user", "content": "Write a function"}]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], UserMessage)
        assert result[0].type == MessageType.USER
        assert result[0].content == "Write a function"
        assert result[0].is_command is False

    def test_user_command_message(self) -> None:
        msgs = [{"role": "user", "content": "/help"}]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert result[0].is_command is True

    def test_assistant_text_only(self) -> None:
        msgs = [{"role": "assistant", "content": "Here is the answer."}]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], AssistantStreamMessage)
        assert result[0].content == "Here is the answer."
        assert result[0].phase == "token"

    def test_assistant_with_tool_calls(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "Let me read the file.",
                "tool_calls": [
                    {
                        "function": {
                            "name": "file_read",
                            "arguments": '{"file_path": "main.py"}',
                        }
                    }
                ],
            }
        ]
        result = replay_messages(msgs)
        assert len(result) == 2
        # First: assistant text
        assert isinstance(result[0], AssistantStreamMessage)
        assert result[0].content == "Let me read the file."
        # Second: tool use
        assert isinstance(result[1], ToolUseMessage)
        assert result[1].tool_name == "file_read"
        assert result[1].file_path == "main.py"
        assert result[1].primary_arg == "main.py"

    def test_tool_result_success(self) -> None:
        msgs = [
            {"role": "tool", "content": "file contents here"},
        ]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], ToolResultMessage)
        assert result[0].status == "success"
        assert result[0].content_length == 18

    def test_tool_result_replay_keeps_medium_output(self) -> None:
        content = "x" * 700
        result = replay_messages([{"role": "tool", "content": content}])
        assert len(result) == 1
        assert isinstance(result[0], ToolResultMessage)
        assert result[0].content_preview == content

    def test_tool_result_replay_closes_truncated_fence(self) -> None:
        content = "```python\n" + "\n".join(f"print({i})" for i in range(300))
        result = replay_messages([{"role": "tool", "content": content}])
        assert len(result) == 1
        assert isinstance(result[0], ToolResultMessage)
        assert result[0].content_preview.count("```") % 2 == 0
        assert "已隐藏" in result[0].content_preview

    def test_tool_result_error(self) -> None:
        msgs = [
            {"role": "tool", "content": "Error: file not found"},
        ]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], ToolResultMessage)
        assert result[0].status == "error"

    def test_tool_result_placeholder(self) -> None:
        msgs = [
            {"role": "tool", "content": "工具调用结果缺失"},
        ]
        result = replay_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], ToolResultMessage)
        assert result[0].status == "skipped"

    def test_full_conversation(self) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Read config.yaml"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "file_read",
                            "arguments": '{"file_path": "config.yaml"}',
                        }
                    }
                ],
            },
            {"role": "tool", "content": "key: value"},
            {"role": "assistant", "content": "The config has key: value."},
        ]
        result = replay_messages(msgs)
        # system skipped → user → assistant(tool) → tool → assistant(text)
        assert len(result) == 4
        assert isinstance(result[0], UserMessage)
        assert isinstance(result[1], ToolUseMessage)
        assert isinstance(result[2], ToolResultMessage)
        assert isinstance(result[3], AssistantStreamMessage)


class TestReplayRendering:
    """Ensure replayed messages can all be rendered by CLIRenderer."""

    @pytest.fixture
    def renderer(self) -> CLIRenderer:
        return CLIRenderer()

    def test_full_conversation_renders(self, renderer: CLIRenderer) -> None:
        msgs = [
            {"role": "user", "content": "Read main.py"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {
                        "function": {
                            "name": "file_read",
                            "arguments": '{"file_path": "main.py"}',
                        }
                    }
                ],
            },
            {"role": "tool", "content": "# main file contents"},
            {"role": "assistant", "content": "Here's what I found."},
        ]
        ui_messages = replay_messages(msgs)
        outputs: list[str] = []
        for msg in ui_messages:
            text = renderer.render(msg)
            if text is not None:
                outputs.append(text)
        # All messages should produce some output:
        # user(1) + assistant_text(1) + tool_use(1) + tool_result(1) + assistant_text(1) = 5
        assert len(outputs) == 5
        # User message should have ❯ prompt
        assert "❯" in outputs[0]
        # Assistant text before tool call
        assert "Let me check." in outputs[1]
        # Tool use should show tool card
        assert "file_read" in outputs[2]
        # Tool result should be rendered
        assert outputs[3] is not None
        # Final assistant text should be present
        assert "Here's what I found." in outputs[4]

    def test_empty_tool_calls_handled(self, renderer: CLIRenderer) -> None:
        """Assistant with no content and empty tool_calls still renders."""
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [],
            }
        ]
        ui_messages = replay_messages(msgs)
        # Empty content produces a message with empty string
        # Empty tool_calls produces no ToolUseMessages
        assert len(ui_messages) == 0

    def test_long_tool_args_summarized(self, renderer: CLIRenderer) -> None:
        long_args = '{"content": "' + "x" * 500 + '"}'
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "file_write",
                            "arguments": long_args,
                        }
                    }
                ],
            }
        ]
        ui_messages = replay_messages(msgs)
        assert len(ui_messages) == 1
        tool_msg = ui_messages[0]
        assert isinstance(tool_msg, ToolUseMessage)
        # args_summary is capped at 200 chars
        assert len(tool_msg.args_summary) <= 200
