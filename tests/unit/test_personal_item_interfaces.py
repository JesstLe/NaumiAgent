"""Real HTTP/Tool/manual-command adapters preserve trusted identities and request bytes."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from naumi_agent.api.routes.commands import (
    _CommandEngine,
    validate_command,
)
from naumi_agent.api.routes.commands import (
    router as command_router,
)
from naumi_agent.api.routes.items import router
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal.item_commands import parse_items_command, run_items_command
from naumi_agent.personal.item_tools import PersonalItemsTool
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tools.base import ToolExecutionError, ToolResult

ITEM = "pi_" + "a" * 32
OCCURRENCE = "pio_" + "b" * 32
ACTOR = "pa_" + "c" * 32
HASH = "d" * 64
REQUEST = "original-request-001"
CREATE = {
    "request_id": REQUEST,
    "title": "核对原资料",
    "instructions": "核对实际版本并保存报告",
    "tags": [],
    "assignee_id": "main",
    "assignee_version": 0,
    "schedule": {
        "mode": "once",
        "local_time": "2026-11-05T09:00:00",
        "timezone": "Asia/Shanghai",
        "fold": None,
        "misfire_policy": "run_once",
        "grace_seconds": 86400,
    },
    "input_refs": [],
    "directories": [],
}
CONTROL = {"request_id": REQUEST, "expected_revision": 3}
READ = {"request_id": REQUEST, "delivery_id": "delivery-original", "expected_delivery_sha256": HASH}


def item():
    return {
        "item_id": ITEM,
        "revision": 3,
        "state": "active",
        "occurrence": None,
        "definition": {
            "title": CREATE["title"],
            "instructions": CREATE["instructions"],
            "assignee": {"id": "main", "version": 0, "name": "主 Agent"},
            "schedule": {**CREATE["schedule"], "due_at": "2026-11-05T01:00:00Z"},
        },
    }


def occurrence():
    return {
        "occurrence_id": OCCURRENCE,
        "status": "unknown",
        "job_id": "job-original",
        "run_id": "run-original",
        "artifacts": [],
        "answer": "",
        "read_receipt": None,
        "delivery_id": READ["delivery_id"],
        "delivery_sha256": HASH,
    }


class Port:
    """Capture the frozen async service port; adapters perform real parsing and dispatch."""

    def __init__(self, engine):
        self.engine, self.calls = engine, []
        self.error = None

    async def _call(self, operation, *args, **kwargs):
        self.calls.append((operation, copy.deepcopy(args), copy.deepcopy(kwargs)))
        if self.error:
            raise self.error
        if operation == "list":
            return {
                "items": [item()],
                "limit": kwargs.get("limit", 50),
                "offset": kwargs.get("offset", 0),
                "next_offset": None,
            }
        if operation == "history":
            return {"history": [item()]}
        if operation == "occurrences":
            return {"occurrences": [occurrence()]}
        if operation == "results":
            return {"results": [occurrence()], "limit": 50, "offset": 0, "next_offset": None}
        if operation == "result":
            return occurrence()
        if operation == "read_result":
            return {"read_receipt": {"acknowledgement_id": "ack-real"}, "occurrence": occurrence()}
        return item()

    def __getattr__(self, operation):
        async def call(*args, **kwargs):
            return await self._call(operation, *args, **kwargs)

        return call


def engine(tmp_path, *, profile=None, marker=""):
    value = SimpleNamespace(
        _session=SimpleNamespace(id="session-original", personal_agent_id=marker)
    )
    if profile:
        db = tmp_path / "catalog.db"
        db.touch()
        value.personal_agents = SimpleNamespace(
            store=SimpleNamespace(db_path=db),
            current=lambda session_id: profile,
            validate_tools=lambda names: None,
        )
    value.personal_items = Port(value)
    tool = PersonalItemsTool(value.personal_items)
    value.tool_calls = []

    async def execute_tool(call, **kwargs):
        value.tool_calls.append((call, kwargs))
        try:
            content = await tool.execute(**tool.parse_arguments(call.arguments))
            return ToolResult(call.id, "success", content)
        except ToolExecutionError as exc:
            return ToolResult(call.id, "error", str(exc), error_code=exc.code)

    value.execute_tool = execute_tool
    return value


def profile(**changes):
    return PersonalAgent(
        **{
            "id": ACTOR,
            "name": "我的 Agent",
            "description": "",
            "instructions": "处理事项",
            "model_tier": "capable",
            "tool_names": (),
            "kind": "specialist",
            "member_ids": (),
            "status": "active",
            "version": 4,
            "created_at": "",
            "updated_at": "",
            **changes,
        }
    )


def app_for(service):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.engine = SimpleNamespace(personal_items=service)
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["human-key"]))

    @app.exception_handler(PersonalAgentError)
    async def personal_error(request, exc):
        return JSONResponse(
            status_code=409 if exc.code == "version_conflict" else 422,
            content={"code": exc.code, "detail": exc.message},
        )

    return app


def json_command(action, body):
    return action + "-json " + shlex.quote(json.dumps(body, ensure_ascii=False))


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", {"operation": "list"}),
        ("list paused 标题 搜索", {"operation": "list", "state": "paused", "query": "标题 搜索"}),
        ("list all", {"operation": "list"}),
        ("get " + ITEM, {"operation": "get", "item_id": ITEM}),
        ("history " + ITEM, {"operation": "history", "item_id": ITEM}),
        ("occurrences " + ITEM, {"operation": "occurrences", "item_id": ITEM}),
        (
            "results unread 30 60",
            {"operation": "results", "unread": True, "limit": 30, "offset": 60},
        ),
        ("result " + OCCURRENCE, {"operation": "result", "occurrence_id": OCCURRENCE}),
    ],
)
def test_manual_parser_keeps_explicit_identity(argument, expected):
    assert parse_items_command(argument) == expected


@pytest.mark.parametrize(
    "action", ["create", "update", "pause", "resume", "cancel", "run-now", "restart", "close"]
)
def test_json_writes_preserve_the_original_request(action):
    value = copy.deepcopy(CREATE) if action in {"create", "update"} else dict(CONTROL)
    if action != "create":
        value.update(item_id=ITEM, expected_revision=3)
    parsed = parse_items_command(json_command(action, value))
    assert parsed == {"operation": action.replace("-", "_"), **value}
    assert parsed["request_id"] == REQUEST


@pytest.mark.parametrize(
    "extra",
    ["caller_actor", "actor_id", "trusted_channel", "origin_session_id", "route", "operation"],
)
def test_manual_json_rejects_trusted_field_spoofing(extra):
    with pytest.raises(ValueError):
        parse_items_command(json_command("create", {**CREATE, extra: "main"}))


@pytest.mark.parametrize(
    "argument",
    [
        "pause " + ITEM,
        "create-json '{}'",
        "results read",
        "results unread 0",
        "results all 5 -1",
        "create-json " + shlex.quote('{"request_id":"a","request_id":"b"}'),
        "create-json " + shlex.quote('{"request_id":NaN}'),
        "get fake",
        "read-json '[]'",
    ],
)
def test_parser_rejects_ambiguous_or_incomplete_commands(argument):
    with pytest.raises(ValueError):
        parse_items_command(argument)


async def test_tool_binds_real_runtime_actor_and_source_session(tmp_path):
    current = engine(tmp_path, profile=profile(), marker=ACTOR)
    value = {**CREATE, "assignee_id": ACTOR, "assignee_version": 4}
    result = await PersonalItemsTool(current.personal_items).execute(operation="create", **value)
    assert json.loads(result)["item_id"] == ITEM
    assert current.personal_items.calls == [
        ("create", ({**value, "origin_session_id": "session-original"},), {"caller_actor": ACTOR})
    ]


@pytest.mark.parametrize(
    "operation", ["list", "get", "history", "occurrences", "results", "result"]
)
async def test_tool_reads_use_actor_scope_without_ack(tmp_path, operation):
    current = engine(tmp_path)
    fields = (
        {"item_id": ITEM}
        if operation in {"get", "history", "occurrences"}
        else {"occurrence_id": OCCURRENCE}
        if operation == "result"
        else {}
    )
    await PersonalItemsTool(current.personal_items).execute(operation=operation, **fields)
    assert current.personal_items.calls[-1][2] == {"caller_actor": "main"}
    assert all(call[0] != "read_result" for call in current.personal_items.calls)


@pytest.mark.parametrize(
    "fields",
    [
        {"operation": "read", **READ, "occurrence_id": OCCURRENCE},
        {"operation": "list", "caller_actor": "main"},
        {"operation": "create", **CREATE, "origin_session_id": "forged"},
        {"operation": "pause", "item_id": ITEM, **CONTROL, "expected_revision": True},
        {
            "operation": "create",
            **CREATE,
            "schedule": {**CREATE["schedule"], "trusted_channel": "web"},
        },
    ],
)
async def test_tool_rejects_read_and_identity_before_service_io(tmp_path, fields):
    current = engine(tmp_path)
    with pytest.raises(ToolExecutionError, match="无效字段"):
        await PersonalItemsTool(current.personal_items).execute(**fields)
    assert not current.personal_items.calls


async def test_tool_revalidates_missing_catalog_paused_agent_and_changed_turn_version(tmp_path):
    missing = engine(tmp_path, marker=ACTOR)
    with pytest.raises(ToolExecutionError) as unavailable:
        await PersonalItemsTool(missing.personal_items).execute(operation="list")
    assert unavailable.value.code == "catalog_unavailable"
    paused = engine(tmp_path, profile=profile(status="paused"), marker=ACTOR)
    with pytest.raises(ToolExecutionError) as inactive:
        await PersonalItemsTool(paused.personal_items).execute(operation="list")
    assert inactive.value.code == "agent_inactive"
    changed = engine(tmp_path, profile=profile(), marker=ACTOR)
    changed._personal_turn_snapshot = ("session-original", ACTOR, 3)
    with pytest.raises(ToolExecutionError) as conflict:
        await PersonalItemsTool(changed.personal_items).execute(operation="list")
    assert conflict.value.code == "version_conflict"
    assert (
        not missing.personal_items.calls
        + paused.personal_items.calls
        + changed.personal_items.calls
    )


async def test_manual_routes_normal_ops_through_execute_tool_and_preserves_unknown(tmp_path):
    current = engine(tmp_path)
    output = await run_items_command(current, json_command("create", CREATE))
    assert ITEM in output and REQUEST in output
    call, kwargs = current.tool_calls[0]
    assert call.name == "personal_items" and kwargs["agent_name"] == "cli"
    assert json.loads(call.arguments) == {"operation": "create", **CREATE}
    current.personal_items.error = PersonalAgentError("storage_error", "执行结果未知，请核对原请求")
    first = await run_items_command(current, json_command("pause", {"item_id": ITEM, **CONTROL}))
    second = await run_items_command(current, json_command("pause", {"item_id": ITEM, **CONTROL}))
    assert "未知" in first and REQUEST in first and "未受理" not in first
    assert second == first
    assert current.personal_items.calls[-1] == current.personal_items.calls[-2]


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
async def test_manual_read_is_explicit_exact_and_absent_from_tool_schema(tmp_path, channel):
    current = engine(tmp_path, profile=profile(), marker=ACTOR)
    output = await run_items_command(
        current,
        json_command("read", {"occurrence_id": OCCURRENCE, **READ}),
        trusted_channel=channel,
    )
    assert "已确认指定结果已读" in output and REQUEST in output
    assert not current.tool_calls
    assert current.personal_items.calls == [
        ("read_result", (OCCURRENCE, READ), {"trusted_channel": channel, "caller_actor": ACTOR})
    ]
    assert (
        "read"
        not in PersonalItemsTool(current.personal_items).parameters_schema["properties"][
            "operation"
        ]["enum"]
    )


async def test_manual_read_receipt_mismatch_is_not_reported_as_read(tmp_path):
    current = engine(tmp_path)

    async def wrong(*args, **kwargs):
        return {
            "read_receipt": {"acknowledgement_id": "ack"},
            "occurrence": {**occurrence(), "delivery_id": "other"},
        }

    current.personal_items.read_result = wrong
    proxy = _CommandEngine(current, lambda *args: None)
    output = await run_items_command(
        proxy, json_command("read", {"occurrence_id": OCCURRENCE, **READ}), trusted_channel="web"
    )
    assert "待核对" in output and REQUEST in output
    assert "已确认指定结果已读" not in output and proxy.failed


@pytest.mark.parametrize(
    ("method", "path", "payload", "operation", "arguments"),
    [
        ("GET", "/personal/items?state=paused&query=核对&limit=30&offset=60", None, "list", ()),
        ("POST", "/personal/items", CREATE, "create", (CREATE,)),
        ("GET", f"/personal/items/{ITEM}", None, "get", (ITEM,)),
        (
            "PATCH",
            f"/personal/items/{ITEM}",
            {**CREATE, "expected_revision": 3},
            "update",
            (ITEM, {**CREATE, "expected_revision": 3}),
        ),
        ("GET", f"/personal/items/{ITEM}/history", None, "history", (ITEM,)),
        ("GET", f"/personal/items/{ITEM}/occurrences", None, "occurrences", (ITEM,)),
        ("POST", f"/personal/items/{ITEM}/pause", CONTROL, "control", (ITEM, "pause", CONTROL)),
        ("POST", f"/personal/items/{ITEM}/resume", CONTROL, "control", (ITEM, "resume", CONTROL)),
        ("POST", f"/personal/items/{ITEM}/cancel", CONTROL, "control", (ITEM, "cancel", CONTROL)),
        ("POST", f"/personal/items/{ITEM}/run-now", CONTROL, "control", (ITEM, "run-now", CONTROL)),
        (
            "POST",
            f"/personal/items/{ITEM}/restart",
            {**CONTROL, "schedule": CREATE["schedule"]},
            "control",
            (ITEM, "restart", {**CONTROL, "schedule": CREATE["schedule"]}),
        ),
        ("POST", f"/personal/items/{ITEM}/close", CONTROL, "control", (ITEM, "close", CONTROL)),
        ("GET", "/personal/item-results?unread=true&limit=30&offset=60", None, "results", ()),
        ("GET", f"/personal/item-results/{OCCURRENCE}", None, "result", (OCCURRENCE,)),
        (
            "POST",
            f"/personal/item-results/{OCCURRENCE}/read",
            READ,
            "read_result",
            (OCCURRENCE, READ),
        ),
    ],
)
async def test_real_http_routes_preserve_payload_and_trusted_channel(
    tmp_path, method, path, payload, operation, arguments
):
    current = engine(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(current.personal_items)),
        base_url="http://test",
        headers={"Authorization": "Bearer human-key"},
    ) as client:
        response = await client.request(method, "/api/v1" + path, json=payload)
    assert response.status_code == (201 if operation == "create" else 200), response.text
    assert response.headers["Cache-Control"] == "no-store"
    observed, args, kwargs = current.personal_items.calls[-1]
    assert observed == operation and args == arguments
    assert "caller_actor" not in kwargs
    if operation == "read_result":
        assert kwargs == {"trusted_channel": "web"}
    elif operation == "list":
        assert kwargs == {"state": "paused", "query": "核对", "limit": 30, "offset": 60}
    elif operation == "results":
        assert kwargs == {"unread": True, "limit": 30, "offset": 60}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "caller_actor": "main"},
        lambda value: {**value, "trusted_channel": "cli"},
        lambda value: {**value, "route": {"workspace_root": "other"}},
        lambda value: {**value, "assignee_version": False},
        lambda value: {**value, "schedule": {**value["schedule"], "fold": True}},
        lambda value: {
            **value,
            "schedule": {**value["schedule"], "due_at": "2026-11-05T01:00:00Z"},
        },
        lambda value: {
            **value,
            "input_refs": [
                {
                    "artifact_id": "art_" + "e" * 32,
                    "version": 1,
                    "sha256": HASH,
                    "grant_id": "",
                    "grant_revision": 0,
                    "actor_id": "main",
                }
            ],
        },
        lambda value: {**value, "tags": ["x"] * 17},
    ],
)
async def test_http_rejects_invalid_or_trusted_nested_fields_before_service(tmp_path, mutate):
    current = engine(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(current.personal_items)),
        base_url="http://test",
        headers={"Authorization": "Bearer human-key"},
    ) as client:
        response = await client.post("/api/v1/personal/items", json=mutate(copy.deepcopy(CREATE)))
    assert response.status_code == 422
    assert not current.personal_items.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"request_id":"one","request_id":"two"}',
        '{"fold":NaN}',
        '{"title":"' + "x" * (1024 * 1024) + '"}',
    ],
    ids=["duplicate", "nonfinite", "oversize"],
)
async def test_http_rejects_duplicate_nonfinite_and_oversize_json(tmp_path, raw):
    current = engine(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(current.personal_items)),
        base_url="http://test",
        headers={"Authorization": "Bearer human-key"},
    ) as client:
        response = await client.post(
            "/api/v1/personal/items", content=raw, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422 and response.json()["code"] == "invalid_input"
    assert not current.personal_items.calls


async def test_http_auth_and_error_use_existing_authorities(tmp_path):
    current = engine(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(current.personal_items)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/personal/items")).status_code == 401
        current.personal_items.error = PersonalAgentError("version_conflict", "事项修订已变化")
        response = await client.post(
            f"/api/v1/personal/items/{ITEM}/pause",
            json=CONTROL,
            headers={"Authorization": "Bearer human-key"},
        )
    assert response.status_code == 409 and response.json()["code"] == "version_conflict"


async def test_actual_shared_cli_and_tui_slash_path(tmp_path, monkeypatch):
    current = engine(tmp_path)
    output = await execute_slash_command(current, "/items list")
    assert ITEM in output and current.tool_calls[-1][1]["agent_name"] == "cli"
    frontend_class = type("_TuiSlashCommandFrontend", (), {"__module__": "naumi_agent.tui.app"})
    output = await execute_slash_command(current, "/items list", frontend=frontend_class())
    assert ITEM in output and current.tool_calls[-1][1]["agent_name"] == "cli"
    assert validate_command("/items results unread") == "/items results unread"


async def test_real_web_command_stream_routes_explicit_read_as_web(tmp_path):
    current = engine(tmp_path)
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="事项命令验收")
    current.session_store = sessions
    current.runtime_mode = "default"
    current.set_runtime_mode = lambda mode: setattr(current, "runtime_mode", mode)

    async def load_session(identifier):
        current._session = await sessions.load(identifier)
        return current._session

    current.load_session = load_session
    app = FastAPI()
    app.include_router(command_router)
    app.state.engine, app.state.engine_lock = current, asyncio.Lock()
    app.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    command = "/items " + json_command("read", {"occurrence_id": OCCURRENCE, **READ})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/sessions/{session.id}/commands", json={"command": command})
    assert response.status_code == 200
    assert "已确认指定结果已读" in response.text
    assert current.personal_items.calls[-1][2] == {"trusted_channel": "web", "caller_actor": "main"}
    assert not current.tool_calls
    saved = await sessions.load(session.id)
    assert saved.messages[-2]["content"].startswith("/items read-json")
    assert "已确认指定结果已读" in saved.messages[-1]["content"]
    assert (await app.state.chat_run_store.list_runs(session.id))[0].status == "completed"


async def test_items_web_commands_leave_execution_session_immutable(tmp_path):
    current = engine(tmp_path)
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="原执行记录", personal_execution_id=OCCURRENCE)
    current.session_store = sessions
    app = FastAPI()
    app.include_router(command_router)
    app.state.engine = current
    app.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/sessions/{session.id}/commands", json={"command": "/items list"}
        )
    assert response.status_code == 409
    assert not current.personal_items.calls
    assert (await sessions.load(session.id)).messages == session.messages
    assert await app.state.chat_run_store.list_runs(session.id) == []


async def test_partial_policy_reaches_service_without_adapter_defaults(tmp_path):
    current = engine(tmp_path)
    payload = {**CREATE, "policy": {"max_turns": 8}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(current.personal_items)),
        base_url="http://test",
        headers={"Authorization": "Bearer human-key"},
    ) as client:
        response = await client.post("/api/v1/personal/items", json=payload)
    assert response.status_code == 201
    assert current.personal_items.calls[-1][1] == (payload,)
    await PersonalItemsTool(current.personal_items).execute(operation="create", **payload)
    assert current.personal_items.calls[-1][1][0]["policy"] == {"max_turns": 8}


@pytest.mark.usefixtures("runtime_payload_key")
async def test_real_empty_tool_agent_manual_cli_tui_and_model_scopes(tmp_path, monkeypatch):
    from naumi_agent.config.settings import AppConfig
    from naumi_agent.model.router import ModelRouter
    from naumi_agent.runtime.composition import create_agent_engine
    from naumi_agent.tools.base import ToolCall

    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "runtime-home"))

    async def no_provider(*args, **kwargs):
        raise AssertionError("Manual item management must not invoke a provider")

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    work = tmp_path / "work"
    work.mkdir()
    state = tmp_path / "state"
    runtime = create_agent_engine(
        AppConfig(
            workspace_root=str(work),
            personal={"data_dir": str(state / "personal")},
            memory={
                "session_db_path": str(state / "sessions.db"),
                "vector_db_path": str(state / "vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "bypass", "allowed_dirs": [str(work)]},
        )
    )
    try:
        await runtime.get_or_create_session()
        agent = runtime.personal_agents.create(
            name="我的 Agent",
            instructions="处理我明确委托的事项",
            request_id="empty-tool-agent",
            tool_names=[],
            model_tier="capable",
        )
        opened = await runtime.personal_agents.open_session(agent.id)
        assert await runtime.load_session(opened["session_id"])
        payload = {
            **CREATE,
            "request_id": "real-manual-item",
            "assignee_id": agent.id,
            "assignee_version": agent.version,
            "schedule": {**CREATE["schedule"], "local_time": "2035-11-05T09:00:00"},
        }
        created = await execute_slash_command(runtime, "/items " + json_command("create", payload))
        assert "pi_" in created and "操作未完成" not in created, created
        cli = await execute_slash_command(runtime, "/items list")
        frontend_class = type("_TuiSlashCommandFrontend", (), {"__module__": "naumi_agent.tui.app"})
        tui = await execute_slash_command(runtime, "/items list", frontend=frontend_class())
        assert cli == tui and CREATE["title"] in tui
        values = await runtime.personal_items.list(caller_actor=agent.id)
        assert len(values["items"]) == 1
        assert values["items"][0]["definition"]["origin"]["session_id"] == opened["session_id"]
        denied = await runtime.execute_tool(
            ToolCall(
                id="model-list-attempt", name="personal_items", arguments='{"operation":"list"}'
            ),
            agent_name=agent.name,
        )
        assert denied.status == "error"
        assert runtime.personal_agents.require_active_profile(agent.id).tool_names == ()
    finally:
        await runtime.shutdown()
