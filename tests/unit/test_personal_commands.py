"""Shared command routing, parsing, authorization, and Web session semantics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from naumi_agent.api.routes.commands import command_index, validate_command
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.personal.commands import parse_assistants_command, run_assistants_command
from naumi_agent.tools.base import ToolResult
from naumi_agent.ui.command_index import build_terminal_command_index


def _engine(payload: object, *, status: str = "success") -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(
        execute_tool=AsyncMock(return_value=ToolResult("call", status, content)),
        load_session=AsyncMock(return_value=True),
    )


def _profile() -> dict[str, object]:
    return {
        "id": "pa_custom",
        "name": "旅行规划",
        "instructions": "整理用户指定的旅行资料",
        "status": "active",
        "version": 1,
        "model_tier": "capable",
        "tool_names": [],
        "updated_at": "2026-11-03T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", {"operation": "list"}),
        ("list", {"operation": "list", "include_archived": False}),
        ("list all", {"operation": "list", "include_archived": True}),
        ("tools", {"operation": "tools"}),
        ("get pa_custom", {"operation": "get", "agent_id": "pa_custom"}),
        ("history pa_custom", {"operation": "history", "agent_id": "pa_custom"}),
        ("open pa_custom", {"operation": "open", "agent_id": "pa_custom"}),
        (
            "pause pa_custom 2",
            {
                "operation": "update",
                "agent_id": "pa_custom",
                "expected_version": 2,
                "status": "paused",
            },
        ),
        (
            "resume pa_custom 3",
            {
                "operation": "update",
                "agent_id": "pa_custom",
                "expected_version": 3,
                "status": "active",
            },
        ),
        (
            "archive pa_custom 4",
            {
                "operation": "update",
                "agent_id": "pa_custom",
                "expected_version": 4,
                "status": "archived",
            },
        ),
    ],
)
def test_parse_standard_commands(argument, expected):
    assert parse_assistants_command(argument) == expected


def test_create_preserves_user_defined_role_and_grants_no_default_tools():
    result = parse_assistants_command('create "旅行 & 学习" "仅整理我指定的资料，不发布"')
    assert result["name"] == "旅行 & 学习"
    assert result["instructions"] == "仅整理我指定的资料，不发布"
    assert result["tool_names"] == []
    assert len(result["request_id"]) == 32
    assert result["operation"] == "create"


def test_create_json_supports_custom_team_and_bound_request_identity():
    fields = {
        "name": "我的团队",
        "instructions": "按用户要求分工",
        "kind": "team",
        "member_ids": ["pa_custom"],
        "tool_names": ["file_read"],
        "request_id": "one",
    }
    result = parse_assistants_command(f"create-json '{json.dumps(fields, ensure_ascii=False)}'")
    assert result == {"operation": "create", **fields}


def test_update_json_preserves_tool_membership_and_revision():
    result = parse_assistants_command("update-json pa_custom 8 '{\"tool_names\":[]}'")
    assert result == {
        "operation": "update",
        "agent_id": "pa_custom",
        "expected_version": 8,
        "tool_names": [],
    }


@pytest.mark.parametrize(
    "argument",
    [
        'create "未闭合',
        'create "" "职责"',
        "create only_name",
        "list unexpected",
        "get ../../private",
        "open pa_custom extra",
        "pause pa_custom",
        "pause pa_custom 0",
        "archive pa_custom -1",
        "archive pa_custom 999999999999999",
        "resume pa_custom ２",
        "unknown",
        "create-json '[]'",
        "create-json '{}'",
        "create-json '{bad}'",
        'create-json \'{"name":"x"}\'',
        'create-json \'{"operation":"open"}\'',
        "update-json pa_custom 1 '{\"expected_version\":99}'",
        'update-json pa_custom 1 \'{"kind":"team"}\'',
        "x" * 16001,
    ],
)
def test_invalid_input_rejected_before_any_execution(argument):
    with pytest.raises(ValueError):
        parse_assistants_command(argument)


@pytest.mark.asyncio
async def test_every_manual_mutation_uses_authorized_engine_tool():
    engine = _engine(_profile())
    text = await run_assistants_command(engine, 'create "旅行规划" "整理用户指定的旅行资料"')
    call = engine.execute_tool.await_args.args[0]
    assert call.name == "personal_agents"
    assert engine.execute_tool.await_args.kwargs == {"agent_name": "cli"}
    arguments = json.loads(call.arguments)
    assert arguments["operation"] == "create" and arguments["tool_names"] == []
    assert "已创建" in text and "旅行规划" in text
    engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_catalog_is_not_populated_by_listing():
    engine = _engine({"agents": []})
    text = await run_assistants_command(engine, "")
    assert "尚未创建专员" in text
    assert json.loads(engine.execute_tool.await_args.args[0].arguments) == {"operation": "list"}


@pytest.mark.asyncio
async def test_denied_tool_never_opens_session():
    engine = _engine("本次操作未获批准", status="error")
    assert await run_assistants_command(engine, "open pa_custom") == "本次操作未获批准"
    engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not json", {"session_id": "../invalid"}, {}])
async def test_invalid_open_receipt_keeps_current_session(payload):
    engine = _engine(payload)
    assert "保持不变" in await run_assistants_command(engine, "open pa_custom")
    engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_manual_open_loads_bound_session():
    engine = _engine({"session_id": "session_1", "agent_id": "pa_custom", "version": 1})
    assert "已打开" in await run_assistants_command(engine, "open pa_custom")
    engine.load_session.assert_awaited_once_with("session_1")


@pytest.mark.asyncio
async def test_missing_bound_session_does_not_claim_success():
    engine = _engine({"session_id": "session_1"})
    engine.load_session.return_value = False
    assert "暂未就绪" in await run_assistants_command(engine, "open pa_custom")


@pytest.mark.asyncio
async def test_error_payload_never_claims_success_or_opens_session():
    engine = _engine({"error": "团队执行尚未接入", "code": "not_ready"})
    assert "团队执行尚未接入" in await run_assistants_command(engine, "open pa_custom")
    engine.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_and_version_conflicts_are_visible():
    engine = _engine({"revisions": [_profile()]})
    assert "v1" in await run_assistants_command(engine, "history pa_custom")
    engine.execute_tool.return_value = ToolResult("call", "error", "配置已更新，请刷新后重试")
    assert "请刷新" in await run_assistants_command(engine, "pause pa_custom 1")


@pytest.mark.asyncio
async def test_help_and_parse_errors_do_not_invoke_tool():
    engine = _engine({})
    assert "create-json" in await run_assistants_command(engine, "help")
    assert "版本必须" in await run_assistants_command(engine, "pause pa_custom 0")
    engine.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_cli_and_tui_router_dispatches_command():
    engine = _engine({"agents": [_profile()]})
    text = await execute_slash_command(engine, "/assistants")
    assert "旅行规划" in text
    assert engine.execute_tool.await_args.args[0].name == "personal_agents"


def test_command_is_indexed_for_both_terminal_surfaces_and_web():
    for surface in ("new_ui", "tui"):
        entry = next(e for e in build_terminal_command_index(surface) if e.command == "/assistants")
        assert entry.source == "shared_runtime" and not entry.readonly
    assert any(entry.command == "/assistants" for entry in command_index())


def test_web_allows_management_but_not_implicit_session_switch():
    assert validate_command('/assistants create "旅行规划" "整理资料"').startswith(
        "/assistants create"
    )
    assert validate_command("/assistants help") == "/assistants help"
    with pytest.raises(HTTPException, match="侧栏") as error:
        validate_command("/assistants open pa_custom")
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        validate_command("/assistants pause pa_custom 0")


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_node_bridge_replays_only_authoritatively_changed_open_session(success):
    from naumi_agent.ui.bridge import JsonlEngineBridge

    engine = _engine({"session_id": "new_session"}, status="success" if success else "error")
    engine._session = SimpleNamespace(id="old_session")

    async def load(session_id):
        engine._session = SimpleNamespace(id=session_id)
        return True

    engine.load_session.side_effect = load
    bridge = SimpleNamespace(
        engine=engine,
        resume_session=AsyncMock(),
        emit=AsyncMock(),
        _emit_system_notice=AsyncMock(),
        emit_error=AsyncMock(),
        debug_trace=None,
        status_payload=lambda: {},
    )
    await JsonlEngineBridge._run_cli_slash_command(
        bridge,
        "/assistants open pa_custom",
        request_id="manual-open",
    )
    if success:
        bridge.resume_session.assert_awaited_once_with(
            {"session_id": "new_session"},
            request_id="manual-open",
        )
    else:
        bridge.resume_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_tui_replays_only_authoritatively_changed_open_session(success):
    from naumi_agent.tui.app import NaumiApp, StatusBar

    engine = _engine({"session_id": "new_session"}, status="success" if success else "error")
    engine._session = SimpleNamespace(id="old_session")

    async def load(session_id):
        engine._session = SimpleNamespace(id=session_id)
        return True

    engine.load_session.side_effect = load
    chat = SimpleNamespace(mount=Mock())
    status = SimpleNamespace(status_text="")
    app = SimpleNamespace(
        engine=engine,
        debug_trace=None,
        _slash_frontend=None,
        query_one=lambda kind: status if kind is StatusBar else chat,
        _show_current_session=Mock(),
        _sync_model_control_status=Mock(),
    )
    await NaumiApp._run_cli_slash_command.__wrapped__(app, "/assistants open pa_custom")
    assert app._show_current_session.call_count == int(success)
