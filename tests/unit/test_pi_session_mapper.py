"""Unit tests for the pi event stream -> UIMessage mapper.

Event shapes mirror a live capture against pi 0.85.1 driving GLM-4.7.
"""

from __future__ import annotations

import json

from naumi_agent.pi_engine.rpc import PiEventType
from naumi_agent.pi_engine.session import PiSessionMapper
from naumi_agent.ui.messages.events import (
    AssistantStreamMessage,
    ErrorMessage,
    SystemNoticeMessage,
    ToolPrepareMessage,
    ToolResultMessage,
    ToolUseMessage,
)


def _msg(event_type: str, **fields: object) -> dict:
    return {"type": event_type, **fields}


def _feed_all(mapper: PiSessionMapper, events: list[dict]) -> list[object]:
    out: list[object] = []
    for event in events:
        out.extend(mapper.feed(event))
    return out


def test_text_only_reply_maps_to_assistant_stream() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.MESSAGE_START,
                message={"role": "assistant", "content": []},
            ),
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={"type": "text_start", "contentIndex": 0},
            ),
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "你"},
            ),
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "好"},
            ),
            _msg(
                PiEventType.MESSAGE_END,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "你好"}],
                    "stopReason": "stop",
                },
            ),
            _msg(PiEventType.AGENT_SETTLED),
        ],
    )
    assert [type(m) for m in messages] == [
        AssistantStreamMessage,
        AssistantStreamMessage,
        AssistantStreamMessage,
        AssistantStreamMessage,
    ]
    assert messages[0].phase == "start"
    assert messages[1].content == "你"
    assert messages[3].phase == "end"
    assert messages[3].content == "你好"
    assert mapper.settled is True
    assert mapper.final_text == "你好"
    assert mapper.error == ""


def test_user_echo_is_skipped() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.MESSAGE_START,
                message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ),
            _msg(
                PiEventType.MESSAGE_END,
                message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ),
        ],
    )
    assert messages == []


def test_tool_roundtrip_maps_prepare_use_and_result() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={"type": "toolcall_start", "contentIndex": 0},
            ),
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={
                    "type": "toolcall_delta",
                    "contentIndex": 0,
                    "delta": '{"command":"echo',
                },
            ),
            _msg(
                PiEventType.TOOL_EXECUTION_START,
                toolCallId="call_1",
                toolName="bash",
                args={"command": "echo naumi-ok"},
            ),
            _msg(
                PiEventType.TOOL_EXECUTION_UPDATE,
                toolCallId="call_1",
                toolName="bash",
                partialResult={"content": []},
            ),
            _msg(
                PiEventType.TOOL_EXECUTION_END,
                toolCallId="call_1",
                toolName="bash",
                result={"content": [{"type": "text", "text": "naumi-ok\n"}]},
                isError=False,
            ),
            _msg(PiEventType.AGENT_SETTLED),
        ],
    )
    assert isinstance(messages[0], ToolPrepareMessage)
    assert messages[0].phase == "start"
    assert isinstance(messages[1], ToolPrepareMessage)
    assert messages[1].argument_chars == len('{"command":"echo')
    tool_use = messages[2]
    assert isinstance(tool_use, ToolUseMessage)
    assert tool_use.tool_name == "bash"
    assert tool_use.tool_call_id == "call_1"
    assert tool_use.command == "echo naumi-ok"
    assert json.loads(tool_use.args_raw) == {"command": "echo naumi-ok"}
    tool_result = messages[3]
    assert isinstance(tool_result, ToolResultMessage)
    assert tool_result.status == "success"
    assert tool_result.content_preview == "naumi-ok\n"
    assert tool_result.content_length == len("naumi-ok\n")
    assert mapper.settled is True


def test_error_stop_reason_yields_error_message() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.MESSAGE_END,
                message={
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "error": "401 unauthorized",
                },
            ),
            _msg(PiEventType.AGENT_SETTLED),
        ],
    )
    assert isinstance(messages[0], ErrorMessage)
    assert "401" in messages[0].message
    assert mapper.error == "401 unauthorized"
    assert mapper.final_text == ""


def test_thinking_delta_maps_to_thinking_message() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.MESSAGE_UPDATE,
                assistantMessageEvent={
                    "type": "thinking_delta",
                    "contentIndex": 0,
                    "delta": "推理",
                },
            ),
        ],
    )
    assert len(messages) == 1
    assert messages[0].phase == "delta"
    assert messages[0].content == "推理"


def test_long_tool_result_is_truncated() -> None:
    mapper = PiSessionMapper()
    big = "x" * 5000
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.TOOL_EXECUTION_END,
                toolCallId="t",
                toolName="bash",
                result={"content": [{"type": "text", "text": big}]},
                isError=False,
            ),
        ],
    )
    result = messages[0]
    assert result.content_truncated is True
    assert result.content_length == 5000
    assert len(result.content_preview) < 5000


def test_compaction_and_retry_map_to_system_notices() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(PiEventType.COMPACTION_START),
            _msg(PiEventType.COMPACTION_END, savedTokens=1234),
            _msg(PiEventType.AUTO_RETRY_START, attempt=1, error="rate limited"),
        ],
    )
    assert all(isinstance(m, SystemNoticeMessage) for m in messages)
    assert "压缩" in messages[0].content
    assert "1234" in messages[1].content
    assert "重试" in messages[2].content


def test_extension_error_maps_to_error_message() -> None:
    mapper = PiSessionMapper()
    messages = _feed_all(
        mapper,
        [
            _msg(
                PiEventType.EXTENSION_ERROR,
                error="boom",
                extensionPath="/tmp/ext.js",
            ),
        ],
    )
    assert isinstance(messages[0], ErrorMessage)
    assert "boom" in messages[0].message
    assert "/tmp/ext.js" in messages[0].message


def test_reset_clears_run_state() -> None:
    mapper = PiSessionMapper()
    _feed_all(mapper, [_msg(PiEventType.AGENT_SETTLED)])
    assert mapper.settled is True
    mapper.reset()
    assert mapper.settled is False
    assert mapper.final_text == ""
