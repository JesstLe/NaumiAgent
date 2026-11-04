"""Shared authorized CLI/TUI contracts for persistent personal-team tasks."""

from __future__ import annotations

import copy
import json
import re
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.cli.completer import _build_commands
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.personal.commands import parse_assistants_command, run_assistants_command
from naumi_agent.tools.base import ToolResult

TEAM = "pa_" + "a" * 32
MEMBER = "pa_" + "b" * 32
TURN = "pt_" + "1" * 32
PHASE = "pc_" + "2" * 32
DIGEST = "3" * 64


def payload(**updates):
    return {
        "team_id": TEAM,
        "team_version": 4,
        "request_id": "user-team-turn-1",
        "content": "请团队核对明确共享的资料。",
        **updates,
    }


def command(value, action="team-json"):
    return action + " " + shlex.quote(json.dumps(value, ensure_ascii=False))


def snapshot(**updates):
    return {
        "id": TURN,
        "team_id": TEAM,
        "team_name": "用户创建的团队",
        "team_version": 4,
        "source_session_id": "session-main",
        "team_session_id": "session-team",
        "request_id": "user-team-turn-1",
        "status": "queued",
        "cancel_requested": False,
        "result_message_id": "",
        "response": "",
        "error": "",
        "phases": [],
        "roster": {"teams": [], "agents": []},
        **updates,
    }


def engine(value, status="success"):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return SimpleNamespace(
        execute_tool=AsyncMock(return_value=ToolResult("call", status, content)),
        load_session=AsyncMock(),
    )


def test_team_json_preserves_exact_identity_and_normalized_explicit_scope():
    value = payload(
        source_session_id="session_1",
        context="只读取这一份资料。",
        source_ids=["src-z", "src-a"],
        member_grants=[{"agent_id": MEMBER, "agent_version": 5, "tool_names": []}],
    )
    before = copy.deepcopy(value)
    result = parse_assistants_command(command(value))
    assert result == {"operation": "team_send", **value, "source_ids": ["src-a", "src-z"]}
    assert value == before
    result["member_grants"][0]["tool_names"].append("file_write")
    assert value["member_grants"][0]["tool_names"] == []


@pytest.mark.parametrize("action", ["team-json", "team-send-json"])
def test_team_json_aliases_default_only_request_context_and_sources_without_minting_roles(action):
    value = payload()
    value.pop("request_id")
    result = parse_assistants_command(command(value, action))
    identity = result.pop("request_id")
    assert re.fullmatch(r"[0-9a-f]{32}", identity)
    assert result == {"operation": "team_send", **value, "context": "", "source_ids": []}
    assert "member_grants" not in result and "source_session_id" not in result
    assert "member_ids" not in result and "tool_names" not in result


def test_team_json_preserves_no_member_authority_instead_of_inventing_default_grants():
    empty = parse_assistants_command(command(payload(member_grants=[])))
    absent = parse_assistants_command(command(payload(member_grants=None)))
    assert empty["member_grants"] == []
    assert "member_grants" not in absent


def test_team_json_accepts_quoted_apostrophes_multiline_body_and_unicode():
    body = "用户说：it's ready\n来源为‘月度预算’。"
    result = parse_assistants_command(command(payload(content=body)))
    assert result["content"] == body


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("team-turns", {"operation": "team_turns"}),
        (
            "team-turns session_team-1",
            {"operation": "team_turns", "team_session_id": "session_team-1"},
        ),
        ("team-turn " + TURN, {"operation": "team_turn", "turn_id": TURN}),
        ("cancel-team-turn " + TURN, {"operation": "cancel_team_turn", "turn_id": TURN}),
    ],
)
def test_team_query_and_cancel_commands_bind_the_exact_task_or_session(argument, expected):
    assert parse_assistants_command(argument) == expected


@pytest.mark.parametrize(
    "argument",
    [
        "team-json",
        "team-send-json",
        "team-json '{}'",
        "team-json '[]'",
        "team-json '{broken}'",
        "team-json '{}' extra",
        "team-json 'null'",
        "team-json 'true'",
        "team-turn",
        "cancel-team-turn",
        "team-turn " + TURN + " extra",
        "team-turn " + PHASE,
        "team-turn pt_custom",
        "team-turn pt_" + "A" * 32,
        "cancel-team-turn ../../private",
        "team-turns ../session",
        "team-turns 'with spaces'",
        "team-turns one two",
        "team-turns " + "x" * 129,
    ],
)
def test_invalid_team_command_shapes_are_rejected_without_dispatch(argument):
    with pytest.raises(ValueError):
        parse_assistants_command(argument)


@pytest.mark.parametrize("missing", ["team_id", "team_version", "content"])
def test_team_json_requires_explicit_team_identity_and_task(missing):
    value = payload()
    value.pop(missing)
    with pytest.raises(ValueError):
        parse_assistants_command(command(value))


@pytest.mark.parametrize(
    "fields",
    [
        {"team_id": "pa_custom"},
        {"team_version": True},
        {"team_version": "4"},
        {"team_version": 0},
        {"team_version": 2_147_483_648},
        {"request_id": "contains whitespace"},
        {"request_id": None},
        {"content": ""},
        {"content": " \n\t"},
        {"content": "\x1b"},
        {"content": "\ud800"},
        {"content": "x" * 16001},
        {"context": "x" * 32001},
        {"context": "\udfff"},
        {"source_ids": "src-one"},
        {"source_ids": ["src-one", "src-one"]},
        {"source_ids": [f"src-{n}" for n in range(33)]},
        {"source_ids": ["src\nforged"]},
        {"source_session_id": None},
        {"source_session_id": True},
        {"source_session_id": "../private"},
        {"source_session_id": "source\nforged"},
        {"source_session_id": "x" * 129},
        {"operation": "create"},
        {"member_ids": []},
        {"root_owner_id": "forged"},
        {"member_grants": [{"agent_id": MEMBER, "agent_version": True}]},
        {
            "member_grants": [
                {"agent_id": MEMBER, "agent_version": 1, "tool_names": ["file_read", "file_read"]}
            ]
        },
    ],
)
def test_team_json_strict_fields_fail_before_tool_execution(fields):
    with pytest.raises(ValueError):
        parse_assistants_command(command(payload(**fields)))


@pytest.mark.parametrize(
    "raw",
    [
        '{"team_id":"' + TEAM + '","team_version":4,"content":"a","content":"b"}',
        '{"team_id":"'
        + TEAM
        + '","team_version":4,"content":"a","source_session_id":"one","source_session_id":"two"}',
        '{"team_id":"'
        + TEAM
        + '","team_version":4,"content":"a","member_grants":[{"agent_id":"'
        + MEMBER
        + '","agent_version":1,"agent_version":2}]}',
        '{"team_id":"' + TEAM + '","team_version":NaN,"content":"a"}',
        '{"team_id":"' + TEAM + '","team_version":Infinity,"content":"a"}',
    ],
)
def test_team_json_rejects_duplicate_keys_and_nonstandard_constants(raw):
    with pytest.raises(ValueError, match="JSON"):
        parse_assistants_command("team-json " + shlex.quote(raw))


@pytest.mark.asyncio
async def test_manual_team_send_calls_the_authorized_shared_tool_without_switching_sessions():
    fixture_engine = engine(snapshot())
    value = payload(member_grants=[], source_session_id="source_original")
    result = await run_assistants_command(fixture_engine, command(value))
    call = fixture_engine.execute_tool.await_args.args[0]
    assert call.name == "personal_agents"
    assert fixture_engine.execute_tool.await_args.kwargs == {"agent_name": "cli"}
    assert json.loads(call.arguments) == {
        "operation": "team_send",
        **value,
        "context": "",
        "source_ids": [],
    }
    fixture_engine.load_session.assert_not_awaited()
    assert TURN in result and "已受理，等待规划" in result and "已完成" not in result


@pytest.mark.asyncio
async def test_team_source_binding_is_passed_to_the_same_authority_and_not_silently_replaced():
    fixture_engine = engine("请从原始对话发起团队任务", status="error")
    result = await run_assistants_command(
        fixture_engine, command(payload(source_session_id="other_session"))
    )
    assert result == "请从原始对话发起团队任务"
    assert (
        json.loads(fixture_engine.execute_tool.await_args.args[0].arguments)["source_session_id"]
        == "other_session"
    )
    fixture_engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_unstructured_transport_error_is_not_presented_as_a_team_execution_snapshot():
    result = await run_assistants_command(
        engine({"error": "团队服务暂不可用", "id": TURN}), "team-turn " + TURN
    )
    assert result == "操作未完成：团队服务暂不可用"
    assert "团队任务：" not in result


@pytest.mark.asyncio
async def test_shared_slash_router_dispatches_team_query_and_keeps_the_complete_result():
    body = "第一行\n" + "完整团队结果" * 1000 + "\n最后一行"
    fixture_engine = engine(snapshot(status="completed", response=body, result_sha256=DIGEST))
    result = await execute_slash_command(fixture_engine, "/assistants team-turn " + TURN)
    assert body in result and DIGEST in result and "已完成" in result
    assert json.loads(fixture_engine.execute_tool.await_args.args[0].arguments) == {
        "operation": "team_turn",
        "turn_id": TURN,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", "已受理，等待规划"),
        ("planning", "任务规划中"),
        ("working", "成员执行中"),
        ("summarizing", "汇总中"),
        ("delivery_pending", "结果待写回"),
        ("completed", "已完成"),
        ("cancelled", "已停止"),
        ("error", "执行未完成"),
        ("unknown", "结果待核实"),
        ("blocked", "执行受阻"),
        ("new_server_state", "状态待核对"),
    ],
)
async def test_task_status_format_does_not_turn_acceptance_or_delivery_into_completion(
    status, expected
):
    result = await run_assistants_command(engine(snapshot(status=status)), "team-turn " + TURN)
    assert expected in result.splitlines()[0]
    if status != "completed":
        assert "已完成" not in result


@pytest.mark.asyncio
async def test_phase_records_show_exact_member_version_job_run_and_full_multiline_output():
    phase_body = "阶段首行\n" + "阶段原始结果" * 300 + "\n阶段末行"
    phases = [
        {
            "phase": name,
            "collaboration_id": PHASE + str(index),
            "collaboration": {
                "status": "completed",
                "steps": [
                    {
                        "id": f"step{index}",
                        "agent_id": TEAM if name != "members" else MEMBER,
                        "agent_name": "历史协调器" if name != "members" else "历史成员名称",
                        "agent_version": 4 if name != "members" else 5,
                        "status": "completed",
                        "job_id": "job-real-" + str(index),
                        "run_id": "run-real-" + str(index),
                        "session_id": "session-execution-" + str(index),
                        "response": phase_body,
                    }
                ],
            },
        }
        for index, name in enumerate(("planning", "members", "summary"))
    ]
    result = await run_assistants_command(
        engine(
            snapshot(
                status="completed",
                phases=phases,
                usage={"total_tokens": 90, "total_cost_usd": 0.01, "job_count": 3},
                roster={
                    "teams": [],
                    "agents": [{"agent_id": MEMBER, "agent_version": 5, "tool_names": []}],
                },
            )
        ),
        "team-turn " + TURN,
    )
    for name in ("任务规划", "成员分工", "结果汇总", "历史成员名称", MEMBER, "v5"):
        assert name in result
    for index in range(3):
        assert "Job：job-real-" + str(index) in result
        assert "Run：run-real-" + str(index) in result
        assert "执行会话：session-execution-" + str(index) in result
    assert result.count(phase_body) == 3
    assert "90 tokens" in result and "3 个 Job" in result and "0.01 美元" in result


@pytest.mark.asyncio
async def test_no_execution_identity_is_fabricated_for_an_unstarted_phase():
    fixture_engine = engine(
        snapshot(
            phases=[
                {
                    "phase": "planning",
                    "collaboration_id": PHASE,
                    "collaboration": {
                        "status": "waiting",
                        "steps": [
                            {
                                "id": "plan",
                                "agent_id": TEAM,
                                "agent_version": 4,
                                "status": "waiting",
                                "job_id": "",
                                "run_id": "",
                                "session_id": "",
                            }
                        ],
                    },
                }
            ]
        )
    )
    result = await run_assistants_command(fixture_engine, "team-turn " + TURN)
    assert "Job：" not in result and "Run：" not in result and "执行会话：" not in result
    assert "已完成" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["queued", "planning", "working", "summarizing", "delivery_pending"]
)
async def test_cancel_intent_is_not_reported_as_a_terminal_cancel(status):
    fixture_engine = engine(snapshot(status=status, cancel_requested=True))
    result = await run_assistants_command(fixture_engine, "cancel-team-turn " + TURN)
    assert "停止请求已记录" in result and "已停止" not in result and "已完成" not in result
    assert json.loads(fixture_engine.execute_tool.await_args.args[0].arguments) == {
        "operation": "cancel_team_turn",
        "turn_id": TURN,
    }


@pytest.mark.asyncio
async def test_team_task_list_has_empty_and_state_accurate_summary_forms():
    fixture_engine = engine({"turns": []})
    assert await run_assistants_command(fixture_engine, "team-turns") == "暂无团队任务。"
    fixture_engine.execute_tool.return_value = ToolResult(
        "call",
        "success",
        json.dumps(
            {"turns": [snapshot(), snapshot(id="pt_" + "4" * 32, status="unknown")]},
            ensure_ascii=False,
        ),
    )
    result = await run_assistants_command(fixture_engine, "team-turns session_team-1")
    assert "团队任务记录" in result and TURN in result
    assert "已受理，等待规划" in result and "结果待核实" in result
    assert "已完成" not in result
    assert json.loads(fixture_engine.execute_tool.await_args.args[0].arguments) == {
        "operation": "team_turns",
        "team_session_id": "session_team-1",
    }


@pytest.mark.asyncio
async def test_team_task_output_keeps_newlines_and_strips_controls_from_all_visible_sources():
    fixture_engine = engine(
        snapshot(
            team_name="团队\x1b[31m\x07\n伪造行",
            source_session_id="source\x9b",
            status="error",
            response="第一行\r\n第二行\x1b[31m\x07\x9b",
            error="异常\n末尾\x00",
            phases=[
                {
                    "phase": "members",
                    "collaboration_id": PHASE + "\x1b",
                    "collaboration": {
                        "status": "error",
                        "steps": [
                            {
                                "id": "read\x07",
                                "agent_id": MEMBER,
                                "agent_name": "成员\x1b",
                                "agent_version": 5,
                                "status": "error",
                                "job_id": "job\x07",
                                "run_id": "run\x9b",
                                "error": "阶段首行\n阶段末行\x07",
                            }
                        ],
                    },
                }
            ],
        )
    )
    result = await run_assistants_command(fixture_engine, "team-turn " + TURN)
    assert "第一行\n第二行" in result and "异常\n末尾" in result
    assert "阶段首行\n阶段末行" in result
    assert all(control not in result for control in ("\x1b", "\x07", "\x9b", "\x00", "\r"))
    assert "团队 [31m 伪造行" in result.splitlines()[0]


@pytest.mark.asyncio
async def test_team_help_and_invalid_inputs_never_execute_or_switch_sessions():
    fixture_engine = engine({})
    text = await run_assistants_command(fixture_engine, "help")
    for name in ("team-json", "team-send-json", "team-turns", "team-turn", "cancel-team-turn"):
        assert name in text
    assert "团队任务 ID" in await run_assistants_command(fixture_engine, "team-turn wrong")
    assert "来源会话" in await run_assistants_command(
        fixture_engine, command(payload(source_session_id="../forged"))
    )
    fixture_engine.execute_tool.assert_not_awaited()
    fixture_engine.load_session.assert_not_awaited()


def test_shared_completer_advertises_all_new_team_commands():
    metadata = next(item for item in _build_commands() if item.name == "/assistants")
    assert metadata.takes_arg and not metadata.readonly
    for name in ("team-json", "team-send-json", "team-turns", "team-turn", "cancel-team-turn"):
        assert name in metadata.arg_hint
