"""Manual peer commands share the authorized CLI/TUI personal-agent tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.cli.completer import _build_commands
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.personal.commands import parse_assistants_command, run_assistants_command
from naumi_agent.tools.base import ToolResult

SENDER = "pa_" + "a" * 32
RECIPIENT = "pa_" + "b" * 32
MESSAGE = "pm_" + "1" * 32
COLLABORATION = "pc_" + "2" * 32
DIGEST = "3" * 64


def _payload(**updates):
    return {
        "sender_agent_id": SENDER,
        "sender_agent_version": 2,
        "recipient_agent_id": RECIPIENT,
        "recipient_agent_version": 3,
        "content": "请核对这份预算",
        **updates,
    }


def _command(payload):
    return "message-json '" + json.dumps(payload, ensure_ascii=False) + "'"


def _engine(payload):
    return SimpleNamespace(
        execute_tool=AsyncMock(
            return_value=ToolResult("call", "success", json.dumps(payload, ensure_ascii=False))
        ),
        load_session=AsyncMock(),
    )


def test_message_json_preserves_explicit_sender_recipient_and_stable_request():
    fields = _payload(
        request_id="stable-peer-1",
        context="只有这一份资料",
        source_session_id="session_1",
        peer_agents=[{"agent_id": RECIPIENT, "agent_version": 3, "tool_names": ["file_read"]}],
    )
    assert parse_assistants_command(_command(fields)) == {"operation": "message_send", **fields}


def test_message_json_generates_request_identity_but_no_roles_or_tool_grants():
    result = parse_assistants_command(_command(_payload()))
    assert result == {"operation": "message_send", "request_id": result["request_id"], **_payload()}
    assert len(result["request_id"]) == 32
    assert "peer_agents" not in result
    assert "source_session_id" not in result


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("messages " + COLLABORATION, {"operation": "messages", "collaboration_id": COLLABORATION}),
        ("message " + MESSAGE, {"operation": "message", "message_id": MESSAGE}),
        (
            "message-ack " + MESSAGE + " " + DIGEST,
            {"operation": "message_ack", "message_id": MESSAGE, "expected_response_sha256": DIGEST},
        ),
    ],
)
def test_peer_read_and_confirmation_commands(argument, expected):
    assert parse_assistants_command(argument) == expected


@pytest.mark.parametrize(
    "argument",
    [
        "message-json '{}'",
        "message-json '[]'",
        "message-json '{broken}'",
        "message " + COLLABORATION,
        "messages " + MESSAGE,
        "message-ack " + MESSAGE + " invalid",
        "message-ack " + MESSAGE,
        "message " + MESSAGE + " extra",
        "messages ../../private",
    ],
)
def test_invalid_peer_command_shapes_do_not_dispatch(argument):
    with pytest.raises(ValueError):
        parse_assistants_command(argument)


@pytest.mark.parametrize(
    "updates",
    [
        {"sender_agent_id": "../../private"},
        {"recipient_agent_id": "pa_custom"},
        {"sender_agent_version": True},
        {"sender_agent_version": 0},
        {"recipient_agent_version": "3"},
        {"content": ""},
        {"content": "\x1b"},
        {"content": "x" * 16001},
        {"context": "x" * 32001},
        {"request_id": "contains whitespace"},
        {"source_session_id": 2},
        {"source_session_id": "../private"},
        {"operation": "open"},
        {"root_job_id": "forged"},
        {"dispatch_token": "forged"},
        {"peer_agents": [{"agent_id": RECIPIENT, "agent_version": True}]},
        {
            "peer_agents": [
                {
                    "agent_id": RECIPIENT,
                    "agent_version": 3,
                    "tool_names": ["file_read", "file_read"],
                }
            ]
        },
    ],
)
def test_invalid_peer_fields_rejected_before_tool_dispatch(updates):
    with pytest.raises(ValueError):
        parse_assistants_command(_command(_payload(**updates)))


def test_collaboration_json_accepts_explicit_peer_scope():
    fields = {
        "steps": [],
        "request_id": "stable-collaboration",
        "peer_agents": [{"agent_id": RECIPIENT, "agent_version": 3, "tool_names": []}],
    }
    argument = "collaborate-json '" + json.dumps(fields) + "'"
    assert parse_assistants_command(argument) == {"operation": "collaborate", **fields}


@pytest.mark.asyncio
async def test_manual_send_uses_authorized_tool_and_does_not_switch_session():
    engine = _engine({"id": COLLABORATION, "status": "admitted", "steps": []})
    result = await run_assistants_command(engine, _command(_payload(request_id="stable-message")))
    call = engine.execute_tool.await_args.args[0]
    assert call.name == "personal_agents"
    assert engine.execute_tool.await_args.kwargs == {"agent_name": "cli"}
    assert json.loads(call.arguments) == {
        "operation": "message_send",
        **_payload(request_id="stable-message"),
    }
    assert COLLABORATION in result and "等待执行" in result
    engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_terminal_router_supports_message_details_and_full_answers():
    response = "第一行\n" + "完整回答" * 1000 + "\n最后一行"
    engine = _engine(
        {
            "id": MESSAGE,
            "state": "responded",
            "sender_name": "整理专员",
            "recipient_name": "核对专员",
            "content": "原始问题",
            "response": response,
            "response_sha256": DIGEST,
        }
    )
    result = await execute_slash_command(engine, "/assistants message " + MESSAGE)
    assert response in result
    assert "整理专员 → 核对专员" in result and "已回答" in result and DIGEST in result
    assert json.loads(engine.execute_tool.await_args.args[0].arguments) == {
        "operation": "message",
        "message_id": MESSAGE,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "expected"), [("user", "用户已确认收到"), ("agent", "发送Agent已确认收到")]
)
async def test_ack_formats_actual_first_confirmation_actor(actor, expected):
    engine = _engine(
        {
            "id": MESSAGE,
            "state": "acknowledged",
            "sender_agent_id": SENDER,
            "recipient_agent_id": RECIPIENT,
            "response": "825 元",
            "response_sha256": DIGEST,
            "acknowledged_by": actor,
            "acknowledged_at": "2026-11-04T01:00:00Z",
        }
    )
    result = await run_assistants_command(engine, "message-ack " + MESSAGE + " " + DIGEST)
    assert expected in result and "825 元" in result
    assert json.loads(engine.execute_tool.await_args.args[0].arguments) == {
        "operation": "message_ack",
        "message_id": MESSAGE,
        "expected_response_sha256": DIGEST,
    }


@pytest.mark.asyncio
async def test_message_listing_empty_or_summary_is_readable():
    engine = _engine({"messages": []})
    assert await run_assistants_command(engine, "messages " + COLLABORATION) == "暂无Agent消息。"
    engine.execute_tool.return_value = ToolResult(
        "call",
        "success",
        json.dumps(
            {
                "messages": [
                    {
                        "id": MESSAGE,
                        "state": "queued",
                        "sender_name": "专员A",
                        "recipient_name": "专员B",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    result = await run_assistants_command(engine, "messages " + COLLABORATION)
    assert MESSAGE in result and "专员A → 专员B" in result and "等待派送" in result


@pytest.mark.asyncio
async def test_message_display_strips_terminal_control_sequences_but_preserves_lines():
    engine = _engine({"id": MESSAGE, "state": "responded", "response": "正文\n\x1b[31m红色\x07"})
    result = await run_assistants_command(engine, "message " + MESSAGE)
    assert "正文\n" in result and "\x1b" not in result and "\x07" not in result


@pytest.mark.asyncio
async def test_peer_help_and_validation_errors_never_dispatch():
    engine = _engine({})
    help_text = await run_assistants_command(engine, "help")
    for command in ("message-json", "messages", "message", "message-ack"):
        assert command in help_text
    assert "摘要格式" in await run_assistants_command(engine, "message-ack " + MESSAGE + " wrong")
    engine.execute_tool.assert_not_awaited()


def test_completer_describes_all_shared_peer_commands():
    command = next(command for command in _build_commands() if command.name == "/assistants")
    for name in ("message-json", "messages", "message", "message-ack"):
        assert name in command.arg_hint
