"""Real ASGI and identity-bound Tool/CLI contracts over explicit routine ports."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.routes.routines import router
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError
from naumi_agent.personal.routine_commands import parse_routines_command, run_routines_command
from naumi_agent.personal.routine_tools import (
    MAX_REQUEST_BYTES,
    PersonalRoutinesTool,
    execute_routine_operation,
    request_schema,
    validate_routine_arguments,
)
from naumi_agent.tools.base import ToolExecutionError

RID = "pr_" + "1" * 32
OID = "pro_" + "2" * 32
PID = "prp_" + "3" * 32
ACTOR = "pa_" + "4" * 32
SHA = "5" * 64
KEY = "fixture-runtime-key"
PRIVATE = "SOURCE_PRIVATE_BODY_DO_NOT_LEAK_BY_LIST"
CONTROL = {"request_id": "stable-request", "expected_revision": 7}
CREATE = {
    "request_id": "stable-request",
    "preview_id": PID,
    "expected_digest": SHA,
    "enabled": True,
}
RAW = {
    "title": "我的周期计划",
    "instructions": "核对指定材料并保存报告",
    "tags": [],
    "assignee_id": ACTOR,
    "assignee_version": 3,
    "schedule": {
        "kind": "cron",
        "timezone": "Asia/Shanghai",
        "expression": "0 9 * * 1-5",
        "starts_at": None,
        "ends_at": None,
        "dst_gap": "skip",
        "dst_fold": "first",
    },
    "policy": {
        "tool_names": ["personal_artifacts"],
        "artifact_operations": ["get", "read", "publish"],
        "model_tier": "capable",
        "max_turns": 12,
        "max_cost_microusd": 2000000,
        "timeout_seconds": 180,
    },
    "input_refs": [],
    "directories": [],
    "limits": {
        "max_executions": 10,
        "total_cost_microusd": 20000000,
        "max_concurrent": 1,
        "max_pending": 4,
    },
    "recovery": {
        "misfire_policy": "skip",
        "grace_seconds": 3600,
        "max_catch_up": 1,
        "overlap_policy": "queue",
    },
}
PREVIEW_INPUT = {"request_id": "stable-request", "definition": RAW}


def plan():
    return {
        "id": RID,
        "revision": 7,
        "status": "active",
        "epoch": "a" * 32,
        "definition_sha256": SHA,
        "definition": {"title": PRIVATE, "instructions": PRIVATE},
        "created_at": "2026-11-05T00:00:00Z",
        "updated_at": "2026-11-05T00:00:00Z",
        "next_due_at": "2026-11-06T01:00:00Z",
    }


def preview():
    return {
        "preview_id": PID,
        "definition_digest": SHA,
        "expires_at": "2026-11-05T00:30:00Z",
        "definition": {"instructions": PRIVATE},
        "base_routine_id": None,
        "base_revision": None,
        "schedule_preview": [
            {
                "due_at": "2026-11-06T01:00:00Z",
                "local_time": "2026-11-06T09:00:00",
                "fold": 0,
                "calendar_evidence": None,
                "body": PRIVATE,
            }
        ],
    }


def occurrence():
    return {
        "id": OID,
        "routine_id": RID,
        "epoch": "a" * 32,
        "definition_revision": 7,
        "nominal_due_at": "2026-11-06T01:00:00Z",
        "local_time": "2026-11-06T09:00:00",
        "fold": 0,
        "status": "unknown",
        "item_id": "pi_" + "b" * 32,
        "reserved_cost_microusd": 2000000,
        "settled_cost_microusd": None,
        "created_at": "2026-11-05T00:00:00Z",
        "child": {"answer": PRIVATE},
        "decision": PRIVATE,
        "body": PRIVATE,
    }


class Port:
    """An explicit pin barrier port; missing pin methods are never replaced by permissive fakes."""

    def __init__(self):
        self.engine = SimpleNamespace(_session=SimpleNamespace(id="session-main"))
        self.calls, self.pins, self.error, self.pin_error = [], [], None, None
        self.request_kind = "routine"

    def record(self, operation, args, kwargs):
        self.calls.append((operation, copy.deepcopy(args), copy.deepcopy(kwargs)))
        if self.error:
            raise self.error

    async def context(self):
        self.record("context", (), {})
        return {
            "calendars": [],
            "default_policy": RAW["policy"],
            "owner_key": SHA,
            "workspace_key": SHA,
        }

    async def list(self, **kwargs):
        self.record("list", (), kwargs)
        return {"routines": [plan()], "next_after": RID}

    async def get(self, identity, **kwargs):
        self.record("get", (identity,), kwargs)
        return plan()

    async def preview(self, payload, **kwargs):
        self.record("preview", (payload,), kwargs)
        return preview()

    async def create(self, payload, **kwargs):
        self.record("create", (payload,), kwargs)
        return plan()

    async def revise(self, identity, payload, **kwargs):
        self.record("revise", (identity, payload), kwargs)
        return plan()

    async def control(self, identity, action, payload, **kwargs):
        self.record("control", (identity, action, payload), kwargs)
        return occurrence() if action == "extra-run" else plan()

    async def history(self, identity, **kwargs):
        self.record("history", (identity,), kwargs)
        return {"history": [plan()], "next_after": 7}

    async def occurrences(self, identity, **kwargs):
        self.record("occurrences", (identity,), kwargs)
        return {"occurrences": [occurrence()], "next_after": OID}

    async def request_status(self, identity, **kwargs):
        self.record("request_status", (identity,), kwargs)
        result = {"routine": plan, "preview": preview, "occurrence": occurrence}[
            self.request_kind
        ]()
        result_id = PID if self.request_kind == "preview" else result["id"]
        return {
            "request_id": identity,
            "operation": "create",
            "status": "applied",
            "result_kind": self.request_kind,
            "result_id": result_id,
            "result": result,
        }

    async def pin_plan_read(self, result, actor):
        self.record("pin_plan_read", (result, actor), {})
        assert result == plan()
        assert actor in {"main", ACTOR}
        if self.pin_error:
            raise self.pin_error
        self.pins.append(copy.deepcopy((result, actor)))
        return result


@pytest.fixture
def port():
    return Port()


@pytest.fixture
def app(port):
    app = FastAPI()
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    app.state.engine = port.engine
    app.state.engine.personal_routines = port
    app.include_router(router)
    return app


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize(
    "headers,query,keys,expected",
    [
        ({}, "", [KEY], 401),
        ({"X-API-Key": "wrong"}, "", [KEY], 401),
        ({"Authorization": f"Bearer {KEY}"}, "", [KEY], 200),
        ({"Authorization": f"Basic {KEY}"}, "", [KEY], 401),
        ({"X-API-Key": KEY}, f"?api_key={KEY}", [KEY], 401),
        ({}, f"?api_key={KEY}", [KEY], 401),
        ({"X-API-Key": KEY}, "", [], 503),
        ({"X-API-Key": KEY}, "", [KEY, 7], 503),
        ({"X-API-Key": KEY, "Authorization": f"Bearer {KEY}"}, "", [KEY], 401),
        ([("X-API-Key", KEY), ("X-API-Key", KEY)], "", [KEY], 401),
        ([("Authorization", f"Bearer {KEY}"), ("Authorization", f"Bearer {KEY}")], "", [KEY], 401),
    ],
)
async def test_header_auth_is_mandatory_even_without_a_configured_key(
    app, port, headers, query, keys, expected
):
    app.state.config.api.api_keys = keys
    async with client(app, headers) as http:
        response = await http.get("/personal/routines/context" + query)
    assert response.status_code == expected
    no_store(response)
    assert bool(port.calls) is (expected == 200)


@pytest.mark.parametrize(
    "path,body,operation",
    [
        ("/personal/routines/previews", PREVIEW_INPUT, "preview"),
        ("/personal/routines", CREATE, "create"),
        (f"/personal/routines/{RID}/revise", {**CREATE, "expected_revision": 7}, "revise"),
        *[
            (f"/personal/routines/{RID}/{action}", CONTROL, "control")
            for action in ("pause", "resume", "cancel", "extra-run")
        ],
    ],
)
async def test_real_asgi_writes_preserve_nulls_identity_digest_and_manual_actor(
    app, port, path, body, operation
):
    body = copy.deepcopy(body)
    if operation == "revise":
        body.pop("enabled")
    async with client(app) as http:
        response = await http.post(path, json=body)
    assert response.status_code in {200, 201}, response.text
    no_store(response)
    call = port.calls[-1]
    assert call[0] == operation
    assert call[1][-1] == body
    assert call[2] == {"caller_actor": None}
    assert port.pins == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/personal/routines?limit=5&status=paused&query=%E8%B5%84%E6%96%99"),
        ("get", f"/personal/routines/{RID}"),
        ("get", f"/personal/routines/{RID}/history?limit=5&after=0"),
        ("get", f"/personal/routines/{RID}/occurrences?limit=5&after={OID}"),
        ("get", "/personal/routines/requests/request%2Fstable"),
    ],
)
async def test_manual_reads_keep_original_resource_and_pagination(app, port, method, path):
    async with client(app) as http:
        response = await getattr(http, method)(path)
    assert response.status_code == 200, response.text
    no_store(response)
    assert port.calls[-1][2]["caller_actor"] is None
    if "/history" in path:
        assert port.calls[-1][2]["after"] == 0
    if "/occurrences" in path:
        assert port.calls[-1][2]["after"] == OID
    if "/requests/" in path:
        assert port.calls[-1][1] == ("request/stable",)


@pytest.mark.parametrize(
    "suffix",
    [
        "?limit=true",
        "?limit=1.0",
        "?limit=+1",
        "?limit=-1",
        "?limit=0",
        "?limit=101",
        "?limit=１",
        "?limit=1&limit=2",
        "?owner=main",
        "?caller_actor=main",
        "?offset=1",
        "?after=pro_" + "1" * 32,
        "?status=running",
        "?query=" + "x" * 201,
    ],
)
async def test_list_query_rejects_unknown_duplicate_and_coerced_fields(app, port, suffix):
    async with client(app) as http:
        response = await http.get("/personal/routines" + suffix)
    assert response.status_code == 422
    no_store(response)
    assert port.calls == []


@pytest.mark.parametrize(
    "tail",
    [
        "history?after=true",
        "history?after=1.0",
        "history?after=-1",
        "history?after=1&after=2",
        "history?status=active",
        "occurrences?query=x",
        "occurrences?after=7",
        "occurrences?limit=1000",
        "pause?limit=1",
    ],
)
async def test_each_route_has_its_own_query_key_set(app, port, tail):
    async with client(app) as http:
        path = f"/personal/routines/{RID}/{tail}"
        response = await (
            http.post(path, json=CONTROL) if tail.startswith("pause") else http.get(path)
        )
    assert response.status_code == 422
    no_store(response)
    assert not port.calls


@pytest.mark.parametrize(
    "body",
    [
        {**CREATE, "enabled": 1},
        {**CREATE, "enabled": "true"},
        {**CREATE, "expected_digest": "g" * 64},
        {**CREATE, "caller_actor": "main"},
        {**CREATE, "owner_key": SHA},
        {**CREATE, "trusted_channel": "web"},
    ],
)
async def test_create_body_is_strict_and_has_no_identity_escape(app, port, body):
    async with client(app) as http:
        response = await http.post("/personal/routines", json=body)
    assert response.status_code == 422
    assert not port.calls
    no_store(response)


@pytest.mark.parametrize(
    "change",
    [
        {"assignee_version": True},
        {"assignee_version": "3"},
        {"assignee_version": 3.0},
        {"owner_key": SHA},
        {"route": {}},
        {"schema_version": 1},
        {"schedule": {**RAW["schedule"], "version": 1}},
        {"schedule": {**RAW["schedule"], "calendar_sessions": []}},
        {"schedule": {**RAW["schedule"], "kind": "trading_day"}},
        {
            "schedule": {
                "kind": "once",
                "timezone": "UTC",
                "local_time": "2026-12-01T09:00:00",
                "fold": True,
            }
        },
        {"limits": {**RAW["limits"], "max_executions": 1.0}},
        {"limits": {**RAW["limits"], "max_pending": 0}},
        {"limits": {**RAW["limits"], "max_concurrent": 5}},
        {"recovery": {**RAW["recovery"], "max_catch_up": 0}},
        {"policy": {**RAW["policy"], "max_cost_microusd": None}},
    ],
)
async def test_preview_reuses_raw_definition_and_rejects_coercion_at_every_level(app, port, change):
    async with client(app) as http:
        response = await http.post(
            "/personal/routines/previews",
            json={
                **PREVIEW_INPUT,
                "definition": {**copy.deepcopy(RAW), **change},
            },
        )
    assert response.status_code == 422, response.text
    no_store(response)
    assert port.calls == []


@pytest.mark.parametrize(
    "raw,content_type,expected",
    [
        ('{"request_id":"one","request_id":"two"}', "application/json", 422),
        ('{"payload":{"x":1,"x":2}}', "application/json", 422),
        ('{"enabled":NaN}', "application/json", 422),
        ("[]", "application/json", 422),
        ("{broken", "application/json", 422),
        ("{}", "text/plain", 415),
        pytest.param("x" * (MAX_REQUEST_BYTES + 1), "application/json", 413, id="oversized-json"),
    ],
)
async def test_json_is_duplicate_safe_bounded_and_not_echoed(
    app, port, raw, content_type, expected
):
    async with client(app) as http:
        response = await http.post(
            "/personal/routines/previews", content=raw, headers={"Content-Type": content_type}
        )
    assert response.status_code == expected
    no_store(response)
    assert not port.calls


@pytest.mark.parametrize(
    "error,status",
    [
        (PersonalAgentError("scope_denied", PRIVATE), 403),
        (PersonalAgentError("version_conflict", PRIVATE), 409),
        (PersonalAgentError("storage_error", PRIVATE), 503),
        (RuntimeError(PRIVATE), 503),
    ],
)
async def test_service_errors_do_not_echo_private_body_and_are_not_cached(app, port, error, status):
    port.error = error
    async with client(app) as http:
        response = await http.get(f"/personal/routines/{RID}")
    assert response.status_code == status
    assert PRIVATE not in response.text
    no_store(response)


def _profile(status="active", version=3):
    return PersonalAgent(
        ACTOR,
        "我的 Agent",
        "",
        "我的职责",
        "capable",
        ("personal_routines",),
        "specialist",
        (),
        status,
        version,
        "",
        "",
    )


def bind_actor(port, tmp_path, profile=None):
    database = tmp_path / "catalog.db"
    database.touch()
    profile = profile or _profile()
    port.engine._session = SimpleNamespace(id="session-agent", personal_agent_id=ACTOR)
    port.engine.personal_agents = SimpleNamespace(
        store=SimpleNamespace(db_path=database),
        current=lambda _: profile,
        validate_tools=lambda _: None,
    )
    return port.engine


@pytest.mark.parametrize(
    "operation,args",
    [
        ("get", {"routine_id": RID}),
        ("create", {"payload": CREATE}),
        (
            "revise",
            {"routine_id": RID, "payload": {**CONTROL, "preview_id": PID, "expected_digest": SHA}},
        ),
        *[
            (action, {"routine_id": RID, "payload": CONTROL})
            for action in ("pause", "resume", "cancel")
        ],
    ],
)
async def test_model_body_operations_use_actual_runtime_actor_and_pin_before_return(
    port, tmp_path, operation, args
):
    bind_actor(port, tmp_path)
    tool = PersonalRoutinesTool(port)
    result = json.loads(await tool.execute(operation=operation, **args))
    assert result == plan()
    assert port.pins == [(plan(), ACTOR)]
    assert port.calls[0][2]["caller_actor"] == ACTOR
    assert tool.metadata.requires_confirmation is True


async def test_pin_failure_or_missing_pin_never_returns_a_plan_body(port, tmp_path):
    bind_actor(port, tmp_path)
    tool = PersonalRoutinesTool(port)
    port.pin_error = PersonalAgentError("scope_denied", PRIVATE)
    with pytest.raises(ToolExecutionError) as error:
        await tool.execute(operation="get", routine_id=RID)
    assert error.value.code == "scope_denied"
    assert PRIVATE not in str(error.value)
    assert not port.pins
    port.pin_plan_read = None
    with pytest.raises(ToolExecutionError) as error:
        await tool.execute(operation="get", routine_id=RID)
    assert error.value.code == "configuration_error"


@pytest.mark.parametrize(
    "operation,arguments,key",
    [
        ("list", {}, "routines"),
        ("history", {"routine_id": RID}, "history"),
        ("occurrences", {"routine_id": RID}, "occurrences"),
        ("preview", {"payload": PREVIEW_INPUT}, "schedule_preview"),
        ("extra-run", {"routine_id": RID, "payload": CONTROL}, None),
    ],
)
async def test_model_summaries_do_not_transport_unpinned_definition_child_or_decision(
    port, tmp_path, operation, arguments, key
):
    bind_actor(port, tmp_path)
    result = await PersonalRoutinesTool(port).execute(operation=operation, **arguments)
    assert PRIVATE not in result
    public = json.loads(result)
    if key:
        assert public[key]
    assert port.pins == []
    assert port.calls[0][2].get("caller_actor") == ACTOR


@pytest.mark.parametrize("kind", ["routine", "preview", "occurrence"])
async def test_request_lookup_uses_the_same_content_pin_rules(port, tmp_path, kind):
    bind_actor(port, tmp_path)
    port.request_kind = kind
    value = await PersonalRoutinesTool(port).execute(operation="requests", request_id="original/id")
    assert bool(port.pins) is (kind == "routine")
    assert (PRIVATE in value) is (kind == "routine")
    assert port.calls[0][1] == ("original/id",)


async def test_real_runtime_rejects_stale_or_inactive_agent_before_service_access(port, tmp_path):
    bind_actor(port, tmp_path, _profile(status="paused"))
    tool = PersonalRoutinesTool(port)
    with pytest.raises(ToolExecutionError) as error:
        await tool.execute(operation="get", routine_id=RID)
    assert error.value.code == "agent_inactive"
    assert not port.calls
    port.engine.personal_agents.current = lambda _: _profile()
    port.engine._personal_active_turn = ("session-agent", ACTOR, 2)
    with pytest.raises(ToolExecutionError) as error:
        await tool.execute(operation="get", routine_id=RID)
    assert error.value.code == "version_conflict"
    assert not port.calls


async def test_main_tool_actor_is_not_promoted_to_the_manual_caller(port):
    await PersonalRoutinesTool(port).execute(operation="get", routine_id=RID)
    assert port.calls[0][2] == {"caller_actor": "main"}
    assert port.pins[-1][1] == "main"


@pytest.mark.parametrize(
    "bad",
    [
        {"operation": "get", "routine_id": RID, "caller_actor": "main"},
        {"operation": "get", "routine_id": RID, "owner_key": SHA},
        {"operation": "get", "routine_id": RID, "trusted_channel": "cli"},
        {"operation": "read", "routine_id": RID},
        {"operation": "create", "payload": {**CREATE, "enabled": 1}},
        {"operation": "pause", "routine_id": RID, "payload": {**CONTROL, "expected_revision": 7.0}},
        {"operation": "preview", "payload": {**PREVIEW_INPUT, "caller_actor": "main"}},
    ],
)
async def test_tool_strict_validation_has_no_identity_or_read_ack_escape(port, bad):
    with pytest.raises(ToolExecutionError):
        await PersonalRoutinesTool(port).execute(**bad)
    assert not port.calls


def test_tool_parser_preserves_json_and_rejects_duplicates_size_and_strict_float():
    tool = PersonalRoutinesTool(Port())
    value = {"operation": "preview", "payload": PREVIEW_INPUT}
    assert tool.parse_arguments(json.dumps(value, ensure_ascii=False)) == value
    assert Draft202012Validator(request_schema()).is_valid(value)
    for raw in ('{"operation":"get","operation":"list"}', "x" * (MAX_REQUEST_BYTES + 1)):
        with pytest.raises(ValueError):
            tool.parse_arguments(raw)
    with pytest.raises(PersonalAgentError):
        validate_routine_arguments({"operation": "history", "routine_id": RID, "after": 1.0})


@pytest.mark.parametrize(
    "command,operation",
    [
        ("", "list"),
        ("context", "context"),
        (f"get {RID}", "get"),
        (f"history {RID} 5 0", "history"),
        (f"occurrences {RID} 5 {OID}", "occurrences"),
        ("requests original/id", "requests"),
        (f"pause {RID} 7 original/id", "pause"),
        ("list --status paused --query '资料 文档' --limit 5 --after " + RID, "list"),
        ("preview-json '" + json.dumps(PREVIEW_INPUT, ensure_ascii=False) + "'", "preview"),
        ("create-json '" + json.dumps(CREATE) + "'", "create"),
        (
            "revise-json '"
            + json.dumps({**CONTROL, "routine_id": RID, "preview_id": PID, "expected_digest": SHA})
            + "'",
            "revise",
        ),
    ],
)
def test_shared_manual_parser_preserves_stable_identity_and_json(command, operation):
    value = parse_routines_command(command)
    assert value["operation"] == operation
    if operation == "preview":
        assert value["payload"] == PREVIEW_INPUT
    if operation == "create":
        assert value["payload"] == CREATE


@pytest.mark.parametrize(
    "command",
    [
        "get wrong",
        "list --limit 1.0",
        "list --limit 1 --limit 2",
        "list --owner main",
        f"pause {RID} true request",
        f"pause {RID} 7",
        "requests ''",
        "read any",
        'create-json \'{"enabled":true,"enabled":false}\'',
        "preview-json '{}'",
        "create-json '[]'",
        "'unclosed",
        pytest.param("x" * (MAX_REQUEST_BYTES + 1), id="oversized-command"),
    ],
)
def test_manual_parser_rejects_ambiguous_or_coerced_input(command):
    with pytest.raises(ValueError):
        parse_routines_command(command)


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
async def test_human_cli_tui_web_share_dispatcher_but_not_model_body_redaction(port, channel):
    port.engine.personal_routines = port
    result = await run_routines_command(port.engine, f"get {RID}", trusted_channel=channel)
    assert json.loads(result) == plan()
    assert port.calls[0][2] == {"caller_actor": None}
    assert not port.pins


async def test_unknown_manual_control_returns_original_request_without_hidden_replay(port):
    port.engine.personal_routines = port
    port.error = RuntimeError(PRIVATE)
    result = await run_routines_command(port.engine, f"pause {RID} 7 original/id")
    assert PRIVATE not in result
    assert "original/id" in result
    assert len(port.calls) == 1


async def test_model_dispatch_never_accepts_an_unspecified_actor(port):
    with pytest.raises(PersonalAgentError):
        await execute_routine_operation(
            port, {"operation": "get", "routine_id": RID}, model_output=True
        )
    assert not port.calls


def test_schema_exposes_no_owner_channel_or_human_read_operation():
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    for operation in ("context", "list", "get", "preview", "create", "revise", "requests"):
        assert operation in json.dumps(schema)
    for key in ("caller_actor", "trusted_channel", "owner_key", "read_receipt"):
        assert key not in json.dumps(schema)


def test_agent_fixture_is_a_real_runtime_profile_not_a_prompt_role():
    assert replace(_profile(), name="用户自定义名字").name == "用户自定义名字"
