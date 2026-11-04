"""Real ASGI, strict model-tool and shared human command adapter contracts."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.routes.connections import router
from naumi_agent.personal import connection_commands as commands
from naumi_agent.personal.connection_commands import (
    execute_action_command,
    execute_connection_command,
    parse_action_command,
    parse_connection_command,
)
from naumi_agent.personal.connection_store import ConnectionStore
from naumi_agent.personal.connection_tools import (
    MAX_REQUEST_BYTES,
    PersonalActionsTool,
    PersonalConnectionsTool,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolExecutionError

CID = "pc_" + "1" * 32
OID = "co_" + "2" * 32
AID = "ca_" + "3" * 32
ACTOR = "pa_" + "4" * 32
DIGEST = "5" * 64
KEY = "fixture-runtime-key"
SECRET = "NOT_FOR_ERRORS_CLIENT_SECRET_1f0d"
ACTION_PRIVATE_BODY = "fixture-action-body-not-for-listing"
MUTATION = {"request_id": "original-request", "expected_revision": 7}
BODY = {
    "summary": "本地草稿",
    "description": "保留来源",
    "start": {"dateTime": "2026-11-06T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
    "end": {"dateTime": "2026-11-06T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
}
PROPOSAL = {
    "request_id": "original-request",
    "connection_id": CID,
    "source_refs": [],
    "operation": "insert",
    "body": BODY,
    "expires_at": "2026-11-06T08:00:00+08:00",
}
ITEM = {
    "request_id": "original-request",
    "event_revision": 2,
    "actor_id": ACTOR,
    "title": "处理日程",
    "instructions": "仅创建事项",
    "schedule": {
        "mode": "now",
        "timezone": "Asia/Shanghai",
        "misfire_policy": "run_once",
        "grace_seconds": 3600,
    },
}


class Port:
    def __init__(self):
        self.engine = SimpleNamespace(_session=SimpleNamespace(id="original-session"))
        self.calls, self.error = [], None
        self.action = {
            "id": AID,
            "revision": 7,
            "status": "proposed",
            "digest": DIGEST,
            "operation": "insert",
            "created_at": "2026-11-05T09:00:00+08:00",
            "updated_at": "2026-11-05T09:00:00+08:00",
            "connection_id": CID,
            "actor_id": ACTOR,
            "body": BODY | {"description": ACTION_PRIVATE_BODY},
            "source_refs": [],
            "artifact_refs": [],
        }
        self.actions = [self.action]
        self.last_action_result = None
        self.pin_inputs, self.pin_error = [], None

    async def list_actions(self, **kwargs):
        self.calls.append(("list_actions", (), copy.deepcopy(kwargs)))
        if self.error:
            raise self.error
        return {"actions": copy.deepcopy(self.actions)}

    async def pin_action_read(self, result, actor_id):
        self.calls.append(("pin_action_read", copy.deepcopy((result, actor_id)), {}))
        self.pin_inputs.append((result, actor_id))
        assert result["actor_id"] == actor_id
        if self.pin_error:
            raise self.pin_error
        return result

    def __getattr__(self, operation):
        async def call(*args, **kwargs):
            self.calls.append((operation, copy.deepcopy(args), copy.deepcopy(kwargs)))
            if self.error:
                raise self.error
            if operation == "list_connections":
                return {"connections": [{"id": CID, "revision": 7}]}
            if operation == "tool_events":
                return {
                    "content_transport": "exact_private_artifact",
                    "events": [
                        {
                            "event_id": "event-1",
                            "source_ref": {"revision": 1},
                            "artifact_ref": {"artifact_id": "art_pinned"},
                        }
                    ],
                }
            if operation == "events":
                return {
                    "events": [
                        {
                            "event_id": "event-1",
                            "source_ref": {"revision": 1},
                            "description": "fixture-readable-body",
                        }
                    ],
                    "limit": kwargs.get("limit", 50),
                    "offset": kwargs.get("offset", 0),
                    "next_offset": None,
                }
            if operation in {"get_action", "prepare_action", "reconcile_action"}:
                result = copy.deepcopy(self.action)
                result["actor_id"] = kwargs.get("caller_actor", result["actor_id"])
                self.last_action_result = result
                return result
            return {"operation": operation, "status": "fixture"}

        return call


@pytest.fixture
def port():
    return Port()


@pytest.fixture
def app(port):
    result = FastAPI()
    result.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    result.state.engine = port.engine
    result.state.engine.personal_connections = port
    result.include_router(router)
    return result


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local", headers={"X-API-Key": KEY}
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "headers,query,configured,status",
    [
        ({}, "", True, 401),
        ({"X-API-Key": "wrong"}, "", True, 401),
        ({"Authorization": f"Bearer {KEY}"}, "", True, 200),
        ({"Authorization": f"Basic {KEY}"}, "", True, 401),
        ({"X-API-Key": KEY}, f"?api_key={KEY}", True, 401),
        ({}, f"?api_key={KEY}", True, 401),
        ({"X-API-Key": KEY}, "", False, 503),
    ],
)
async def test_runtime_auth_requires_configured_header_key(
    app, port, headers, query, configured, status
):
    if not configured:
        app.state.config.api.api_keys = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as http:
        response = await http.get("/personal/connections/context" + query, headers=headers)
    assert response.status_code == status
    no_store(response)
    assert bool(port.calls) is (status == 200)


@pytest.mark.parametrize(
    "path,body,operation",
    [
        (
            "/personal/connections/client",
            {"request_id": "original-request", "client_json": SECRET},
            "configure_client",
        ),
        (
            "/personal/connections/oauth",
            {"request_id": "original-request", "write": False},
            "start_oauth",
        ),
        (
            f"/personal/connections/oauth/{OID}/cancel",
            {"request_id": "original-request"},
            "cancel_oauth",
        ),
        (f"/personal/connections/{CID}/sync", MUTATION, "sync"),
        (
            f"/personal/connections/{CID}/disconnect",
            MUTATION | {"remote_revoke": False},
            "disconnect",
        ),
        (
            f"/personal/connections/{CID}/grant",
            MUTATION | {"actor_id": ACTOR, "actor_version": 2},
            "grant",
        ),
        (f"/personal/connections/{CID}/revoke-grant", MUTATION | {"actor_id": ACTOR}, "grant"),
        (f"/personal/connections/{CID}/events/event-1/items", ITEM, "event_to_item"),
        (
            f"/personal/actions/{AID}/approve",
            MUTATION | {"expected_digest": DIGEST},
            "approve_action",
        ),
        (f"/personal/actions/{AID}/reject", MUTATION, "reject_action"),
    ],
)
async def test_manual_api_freezes_payload_and_internal_channel(app, port, path, body, operation):
    async with client(app) as http:
        response = await http.post(path, json=body)
    assert response.status_code == 200, response.text
    no_store(response)
    actual, args, kwargs = port.calls[-1]
    assert actual == operation and args[-1] == body
    assert kwargs["trusted_channel"] == "manual_api" and "caller_actor" not in kwargs
    if path.endswith("revoke-grant"):
        assert kwargs["revoke"] is True
    assert port.engine._session.id == "original-session"


@pytest.mark.parametrize(
    "method,path,body,operation",
    [
        ("GET", "/personal/connections", None, "list_connections"),
        ("GET", "/personal/connections/context", None, "context"),
        ("GET", f"/personal/connections/oauth/{OID}", None, "oauth_status"),
        ("GET", "/personal/connections/requests/original-request", None, "request_status"),
        ("GET", f"/personal/connections/{CID}/events?limit=12&offset=3", None, "events"),
        ("GET", "/personal/actions", None, "list_actions"),
        ("GET", f"/personal/actions/{AID}", None, "get_action"),
        ("POST", "/personal/actions", PROPOSAL | {"actor_id": ACTOR}, "prepare_action"),
        (
            "POST",
            f"/personal/actions/{AID}/revise",
            MUTATION | {"body": BODY, "expires_at": PROPOSAL["expires_at"]},
            "revise_action",
        ),
        ("POST", f"/personal/actions/{AID}/reconcile", MUTATION, "reconcile_action"),
    ],
)
async def test_exact_read_and_proposal_routes_preserve_human_caller_none(
    app, port, method, path, body, operation
):
    async with client(app) as http:
        response = (
            await http.request(method, path, json=body)
            if body is not None
            else await http.get(path)
        )
    assert response.status_code == 200, response.text
    no_store(response)
    assert port.calls[-1][0] == operation
    assert "caller_actor" not in port.calls[-1][2]
    if body is not None:
        assert port.calls[-1][1][-1] == body


@pytest.mark.parametrize(
    "body",
    [
        MUTATION | {"human": True},
        MUTATION | {"trusted_channel": "manual_api"},
        MUTATION | {"caller_actor": ACTOR},
        MUTATION | {"owner_key": "a" * 64},
        MUTATION | {"actor_credential": SECRET},
        MUTATION | {"expected_revision": True},
        MUTATION | {"expected_revision": "7"},
    ],
)
async def test_api_rejects_forged_authority_extra_fields_and_coercion(app, port, body):
    async with client(app) as http:
        response = await http.post(f"/personal/connections/{CID}/sync", json=body)
    assert response.status_code == 422
    no_store(response)
    assert port.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"request_id":"a","request_id":"b","write":false}',
        '{"request_id":"a","write":NaN}',
        "[]",
        "{",
        SECRET,
    ],
)
async def test_api_strict_json_rejects_duplicates_nonfinite_and_invalid_body(app, port, raw):
    async with client(app) as http:
        response = await http.post(
            "/personal/connections/oauth", content=raw, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    no_store(response)
    assert not port.calls


@pytest.mark.parametrize(
    "query",
    ["limit=1.0", "offset=-1", "limit=true", "limit=1&limit=2", "actor_id=main", "token=secret"],
)
async def test_query_contract_is_bounded_and_does_not_accept_identity_credentials(app, port, query):
    async with client(app) as http:
        response = await http.get(f"/personal/connections/{CID}/events?{query}")
    assert response.status_code == 422
    no_store(response)
    assert not port.calls


async def test_body_capacity_missing_digest_and_unexpected_errors_are_redacted_no_store(app, port):
    async with client(app) as http:
        oversized = await http.post(
            "/personal/connections/client",
            content="x" * (MAX_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        approval = await http.post(f"/personal/actions/{AID}/approve", json=MUTATION)
        port.error = RuntimeError(SECRET)
        failed = await http.get("/personal/connections/context")
    assert (
        oversized.status_code == 413 and approval.status_code == 422 and failed.status_code == 503
    )
    for response in [oversized, approval, failed]:
        no_store(response)


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "one", "write": "false"},
        {"request_id": "one", "write": False, "connection_id": CID},
        {"request_id": "one", "write": False, "expected_revision": 7},
    ],
)
async def test_oauth_reconnect_is_exact_and_write_cannot_be_coerced(app, port, body):
    async with client(app) as http:
        response = await http.post("/personal/connections/oauth", json=body)
    assert response.status_code == 422
    no_store(response)
    assert not port.calls


async def test_nested_extra_fields_and_noninteger_fixed_schedule_are_rejected(app, port):
    async with client(app) as http:
        forged = await http.post(
            "/personal/actions", json=PROPOSAL | {"body": BODY | {"attendees": [SECRET]}}
        )
        coerced = await http.post(
            f"/personal/connections/{CID}/events/event-1/items",
            json=ITEM | {"schedule": ITEM["schedule"] | {"grace_seconds": 3600.0}},
        )
    assert forged.status_code == coerced.status_code == 422
    no_store(forged)
    no_store(coerced)
    assert not port.calls


async def test_request_identity_with_encoded_slash_remains_one_original_id(app, port):
    async with client(app) as http:
        response = await http.get("/personal/connections/requests/original%2Frequest")
    assert response.status_code == 200
    assert port.calls[-1][1] == ("original/request",)
    no_store(response)


async def test_api_to_real_sqlite_request_replay_and_conflict(app, port, tmp_path):
    store = ConnectionStore(tmp_path / "interface-sqlite", owner_key="a" * 64)

    async def prepare(value):
        return store.mutate(
            value["request_id"],
            "prepare_action",
            value,
            lambda tx: tx.put(
                "action", {"id": AID, "status": "draft", "body": value["body"]}, expected_revision=0
            ),
        )

    port.prepare_action = prepare
    async with client(app) as http:
        first = await http.post("/personal/actions", json=PROPOSAL)
        replay = await http.post("/personal/actions", json=PROPOSAL)
        conflict = await http.post(
            "/personal/actions", json=PROPOSAL | {"body": BODY | {"summary": "changed"}}
        )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() and first.json()["revision"] == 1
    assert conflict.status_code == 409
    assert store.read(lambda tx: tx.get("action", AID))["revision"] == 1


@pytest.mark.parametrize("tool_type", [PersonalConnectionsTool, PersonalActionsTool])
@pytest.mark.parametrize(
    "forged",
    ["owner_key", "caller_actor", "trusted_channel", "actor_id", "actor_credential", "human"],
)
async def test_tools_reject_identity_fields_and_administrative_operations(port, tool_type, forged):
    tool = tool_type(port)
    Draft202012Validator.check_schema(tool.parameters_schema)
    operation = "context" if tool_type is PersonalConnectionsTool else "list_actions"
    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute(operation=operation, **{forged: "forged"})
    assert caught.value.code == "invalid_input" and not port.calls
    with pytest.raises(ToolExecutionError):
        await tool.execute(operation="approve_action", payload=MUTATION)


@pytest.mark.parametrize("actor", [None, ACTOR])
async def test_tools_bind_current_actor_and_events_only_use_pinning_port(port, monkeypatch, actor):
    from naumi_agent.personal import runtime

    calls = []

    def current(engine):
        assert engine is port.engine
        calls.append(engine)
        return None if actor is None else SimpleNamespace(id=actor)

    monkeypatch.setattr(runtime, "require_current_agent", current)
    connection, action = PersonalConnectionsTool(port), PersonalActionsTool(port)
    await connection.execute(operation="list")
    result = json.loads(await connection.execute(operation="events", connection_id=CID))
    await action.execute(operation="list_actions")
    await action.execute(operation="prepare_action", payload=PROPOSAL)
    expected = "main" if actor is None else actor
    assert len(calls) == 4
    assert [call[0] for call in port.calls] == [
        "list_connections",
        "tool_events",
        "list_actions",
        "prepare_action",
        "pin_action_read",
    ]
    assert port.calls[0][2] == {"actor_id": expected}
    assert port.calls[1][0] == "tool_events" and port.calls[1][1] == (CID, expected)
    assert "artifact_ref" in result["events"][0] and not any(
        call[0] == "events" for call in port.calls
    )
    assert port.calls[2][2] == port.calls[3][2] == {"caller_actor": expected}
    assert "actor_id" not in port.calls[3][1][0]
    assert port.pin_inputs == [(port.last_action_result, expected)]
    assert port.pin_inputs[0][0] is port.last_action_result
    assert port.calls[4][1] == (port.last_action_result, expected) and not port.calls[4][2]


async def test_tool_payload_cannot_forge_actor_and_revoked_identity_stops_before_service(
    port, monkeypatch
):
    from naumi_agent.personal import runtime

    for tool, raw in [
        (
            PersonalActionsTool(port),
            {"operation": "prepare_action", "payload": PROPOSAL | {"actor_id": "main"}},
        ),
        (
            PersonalConnectionsTool(port),
            {
                "operation": "event_to_item",
                "connection_id": CID,
                "event_id": "event-1",
                "payload": ITEM,
            },
        ),
    ]:
        with pytest.raises(ToolExecutionError) as caught:
            await tool.execute(**raw)
        assert caught.value.code == "invalid_input"

    def revoked(_engine):
        raise PersonalAgentError("binding_conflict", "执行身份已变化")

    monkeypatch.setattr(runtime, "require_current_agent", revoked)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalActionsTool(port).execute(operation="list_actions")
    assert caught.value.code == "binding_conflict" and not port.calls


def test_tool_json_parser_rejects_duplicate_fields_without_secret_echo(port):
    for tool in [PersonalConnectionsTool(port), PersonalActionsTool(port)]:
        with pytest.raises(ValueError) as caught:
            tool.parse_arguments('{"operation":"context","operation":"' + SECRET + '"}')
        assert SECRET not in str(caught.value)


async def test_tool_pinning_failure_never_falls_back_to_manual_event_body(port, monkeypatch):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda _engine: None)
    port.error = PersonalAgentError("artifact_access_revoked", "来源授权已变化")
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalConnectionsTool(port).execute(operation="events", connection_id=CID)
    assert caught.value.code == "artifact_access_revoked"
    assert len(port.calls) == 1 and port.calls[0][0] == "tool_events"
    port.error = None
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalConnectionsTool(port).execute(
            operation="events", connection_id=CID, limit=2.0
        )
    assert caught.value.code == "invalid_input" and len(port.calls) == 1


@pytest.mark.parametrize("actor", [None, ACTOR])
@pytest.mark.parametrize("empty", [False, True])
async def test_action_list_returns_only_summaries_without_reading_or_pinning_body(
    port, monkeypatch, actor, empty
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda _engine: None if actor is None else SimpleNamespace(id=actor),
    )
    if empty:
        port.actions = []
    expected_actor = "main" if actor is None else actor
    raw = await PersonalActionsTool(port).execute(operation="list_actions")
    result = json.loads(raw)
    fields = {"id", "revision", "status", "digest", "operation", "created_at", "updated_at"}
    assert result == {
        "actions": [{key: value[key] for key in fields} for value in port.actions],
        "content_transport": "get_action",
    }
    assert ACTION_PRIVATE_BODY not in raw
    assert port.calls == [("list_actions", (), {"caller_actor": expected_actor})]
    assert not port.pin_inputs and port.last_action_result is None


@pytest.mark.parametrize("actor", [None, ACTOR])
@pytest.mark.parametrize(
    "raw,operation",
    [
        ({"operation": "get_action", "action_id": AID}, "get_action"),
        ({"operation": "prepare_action", "payload": PROPOSAL}, "prepare_action"),
        ({"operation": "reconcile", "action_id": AID, "payload": MUTATION}, "reconcile_action"),
    ],
)
async def test_action_body_routes_pin_exact_result_under_actual_actor(
    port, monkeypatch, actor, raw, operation
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda _engine: None if actor is None else SimpleNamespace(id=actor),
    )
    result = json.loads(await PersonalActionsTool(port).execute(**raw))
    expected_actor = "main" if actor is None else actor
    assert [call[0] for call in port.calls] == [operation, "pin_action_read"]
    assert port.calls[0][2] == {"caller_actor": expected_actor}
    assert port.calls[1][1] == (port.last_action_result, expected_actor)
    assert not port.calls[1][2]
    assert port.pin_inputs == [(port.last_action_result, expected_actor)]
    assert port.pin_inputs[0][0] is port.last_action_result
    assert result == port.last_action_result
    assert result["actor_id"] == expected_actor
    assert result["body"]["description"] == ACTION_PRIVATE_BODY


@pytest.mark.parametrize("actor", [None, ACTOR])
@pytest.mark.parametrize(
    "raw,operation",
    [
        ({"operation": "get_action", "action_id": AID}, "get_action"),
        ({"operation": "prepare_action", "payload": PROPOSAL}, "prepare_action"),
        ({"operation": "reconcile", "action_id": AID, "payload": MUTATION}, "reconcile_action"),
    ],
)
@pytest.mark.parametrize(
    "failure,code",
    [
        ("revoked", "connection_source_revoked"),
        ("session_changed", "binding_conflict"),
        ("unexpected", "connection_unknown"),
    ],
)
async def test_action_pinning_failure_never_returns_unpinned_body_or_replays_operation(
    port, monkeypatch, actor, raw, operation, failure, code
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda _engine: None if actor is None else SimpleNamespace(id=actor),
    )
    port.pin_error = (
        RuntimeError(SECRET)
        if failure == "unexpected"
        else PersonalAgentError(
            code, "来源授权已变化" if failure == "revoked" else "读取期间对话已切换"
        )
    )
    returned = []
    with pytest.raises(ToolExecutionError) as caught:
        returned.append(await PersonalActionsTool(port).execute(**raw))
    expected_actor = "main" if actor is None else actor
    assert caught.value.code == code and returned == []
    assert ACTION_PRIVATE_BODY not in str(caught.value) and SECRET not in str(caught.value)
    assert [call[0] for call in port.calls] == [operation, "pin_action_read"]
    assert port.calls[0][2] == {"caller_actor": expected_actor}
    assert port.pin_inputs == [(port.last_action_result, expected_actor)]
    assert port.pin_inputs[0][0] is port.last_action_result
    assert port.last_action_result["body"]["description"] == ACTION_PRIVATE_BODY


@pytest.mark.parametrize(
    "operation,expected", [("sync", "sync"), ("reconcile", "reconcile_action")]
)
async def test_model_mutation_routes_never_receive_manual_authority(
    port, monkeypatch, operation, expected
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda _engine: SimpleNamespace(id=ACTOR))
    if operation == "sync":
        await PersonalConnectionsTool(port).execute(
            operation=operation, connection_id=CID, payload=MUTATION
        )
    else:
        await PersonalActionsTool(port).execute(
            operation=operation, action_id=AID, payload=MUTATION
        )
    assert [call[0] for call in port.calls] == (
        [expected] if operation == "sync" else [expected, "pin_action_read"]
    )
    assert port.calls[0][2] == {"caller_actor": ACTOR}
    if operation == "reconcile":
        assert port.calls[1][1] == (port.last_action_result, ACTOR) and not port.calls[1][2]


@pytest.mark.parametrize("parser", [parse_connection_command, parse_action_command])
def test_command_invalid_unicode_and_capacity_are_sanitized(parser):
    for value in ["\ud800", "x" * (MAX_REQUEST_BYTES + 1), None]:
        with pytest.raises(PersonalAgentError) as caught:
            parser(value)
        assert caught.value.code == "invalid_input"


@pytest.mark.parametrize(
    "arguments",
    ["connect --bad", "sync bad 1", f"sync {CID} true", f"sync {CID} -1", "grant", "list extra"],
)
def test_connection_command_parser_rejects_invalid_forms(arguments):
    with pytest.raises(PersonalAgentError):
        parse_connection_command(arguments)


@pytest.mark.parametrize(
    "arguments",
    ["approve bad 1 nope", f"approve {AID} 1", f"reject {AID} true", "propose []", "list extra"],
)
def test_action_command_parser_rejects_invalid_forms(arguments):
    with pytest.raises(PersonalAgentError):
        parse_action_command(arguments)


@pytest.mark.parametrize("channel", ["manual_cli", "manual_tui"])
async def test_shared_human_commands_preserve_channel_revision_digest_and_generated_request(
    port, channel, tmp_path
):
    path = tmp_path / "desktop client.json"
    path.write_text("{}", encoding="utf-8")
    client_result = await execute_connection_command(
        port, f'client "{path}"', trusted_channel=channel
    )
    assert port.calls[-1][0] == "configure_client" and port.calls[-1][2] == {
        "trusted_channel": channel
    }
    assert port.calls[-1][1][0]["client_json"] == "{}"
    connect_result = await execute_connection_command(
        port, f"connect {CID} --write", trusted_channel=channel
    )
    assert port.calls[-1][1][0]["expected_revision"] == 7
    approved = await execute_action_command(
        port, f"approve {AID} 7 {DIGEST}", trusted_channel=channel
    )
    assert port.calls[-1][1][1] == {
        "request_id": approved["request_id"],
        "expected_revision": 7,
        "expected_digest": DIGEST,
    }
    assert port.calls[-1][2] == {"trusted_channel": channel}
    assert (
        len({client_result["request_id"], connect_result["request_id"], approved["request_id"]})
        == 3
    )


async def test_command_unknown_preserves_original_request_without_automatic_retry(
    port, monkeypatch
):
    generated = []

    def identity():
        generated.append("one-human-action")
        return SimpleNamespace(hex="6" * 32)

    monkeypatch.setattr(commands.uuid, "uuid4", identity)
    port.error = TimeoutError(SECRET)
    with pytest.raises(PersonalAgentError) as caught:
        await execute_connection_command(port, f"sync {CID} 7", trusted_channel="manual_cli")
    assert len(generated) == len(port.calls) == 1
    assert caught.value.code == "connection_unknown" and caught.value.request_id == "6" * 32
    assert "6" * 32 in str(caught.value) and SECRET not in str(caught.value)
    port.error = None
    await execute_connection_command(
        port, "request " + caught.value.request_id, trusted_channel="manual_cli"
    )
    assert len(generated) == 1 and port.calls[-1][1] == ("6" * 32,)


async def test_propose_command_keeps_supplied_request_and_rejects_forged_trusted_fields(port):
    raw = json.dumps(PROPOSAL | {"actor_id": ACTOR}, ensure_ascii=False)
    result = await execute_action_command(port, "propose " + raw, trusted_channel="manual_tui")
    assert result["request_id"] == PROPOSAL["request_id"]
    assert port.calls[-1][1][0]["actor_id"] == ACTOR and port.calls[-1][2] == {}
    with pytest.raises(PersonalAgentError):
        await execute_action_command(
            port,
            "propose " + json.dumps(PROPOSAL | {"trusted_channel": "manual_api"}),
            trusted_channel="manual_tui",
        )


async def test_commands_require_manual_entry_before_file_or_service_access(port, tmp_path):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_connection_command(port, "client missing-file.json", trusted_channel=None)
    assert caught.value.code == "scope_denied" and not port.calls
    path = tmp_path / "oversized.json"
    path.write_text("x" * 16385, encoding="utf-8")
    with pytest.raises(PersonalAgentError) as caught:
        await execute_connection_command(port, f'client "{path}"', trusted_channel="manual_cli")
    assert caught.value.code == "invalid_input" and not port.calls


@pytest.mark.parametrize(
    "arguments,operation",
    [
        (f"events {CID}", "events"),
        (f"disconnect {CID} 7", "disconnect"),
        (f"grant {CID} 7 {ACTOR} 2", "grant"),
        (f"revoke {CID} 7 {ACTOR}", "grant"),
    ],
)
async def test_connection_command_remainder_uses_same_port(port, arguments, operation):
    await execute_connection_command(port, arguments, trusted_channel="manual_tui")
    assert port.calls[-1][0] == operation
    if operation == "events":
        assert port.calls[-1][2] == {"actor_id": "main", "limit": 50, "offset": 0}
        assert port.calls[-2][0] == "tool_events"
    else:
        assert port.calls[-1][2]["trusted_channel"] == "manual_tui"


async def test_action_command_remainder_and_explicit_request_never_mints_unused_identity(
    port, monkeypatch
):
    generated = []

    def identity():
        generated.append(True)
        return SimpleNamespace(hex="7" * 32)

    monkeypatch.setattr(commands.uuid, "uuid4", identity)
    await execute_action_command(
        port, "propose " + json.dumps(PROPOSAL), trusted_channel="manual_cli"
    )
    assert not generated
    await execute_action_command(port, f"get {AID}", trusted_channel="manual_cli")
    assert port.calls[-1][0] == "get_action" and not generated
    await execute_action_command(port, f"reject {AID} 7", trusted_channel="manual_cli")
    assert port.calls[-1][0] == "reject_action" and generated == [True]


@pytest.mark.parametrize("existing_session", [True, False])
async def test_manual_events_creates_session_when_needed_and_pins_exact_bound_actor(
    port, monkeypatch, existing_session
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda _engine: SimpleNamespace(id=ACTOR))
    created = []

    async def create_session():
        created.append(True)
        port.engine._session = SimpleNamespace(id="created-session")
        return port.engine._session

    port.engine.get_or_create_session = create_session
    if not existing_session:
        port.engine._session = None
    page = await execute_connection_command(port, f"events {CID}", trusted_channel="manual_tui")
    assert created == ([] if existing_session else [True])
    assert [call[0] for call in port.calls] == ["tool_events", "events"]
    assert port.calls[0][1] == (CID, ACTOR)
    assert port.calls[1][2] == {"actor_id": ACTOR, "limit": 50, "offset": 0}
    assert page["events"][0]["description"] == "fixture-readable-body"


async def test_manual_events_pin_revocation_does_not_return_unpinned_body(port):
    port.error = PersonalAgentError("connection_source_revoked", "账户分享已撤销")
    with pytest.raises(PersonalAgentError) as caught:
        await execute_connection_command(port, f"events {CID}", trusted_channel="manual_cli")
    assert caught.value.code == "connection_source_revoked"
    assert [call[0] for call in port.calls] == ["tool_events"]


@pytest.mark.parametrize("changed", ["revision", "session"])
async def test_manual_events_does_not_display_body_after_source_or_session_changes(
    port, monkeypatch, changed
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda _engine: None)

    async def read_page(_identity, **_kwargs):
        if changed == "session":
            port.engine._session = SimpleNamespace(id="changed-session")
        return {"events": [{"event_id": "event-1", "source_ref": {"revision": 2}}]}

    port.events = read_page
    with pytest.raises(PersonalAgentError) as caught:
        await execute_connection_command(port, f"events {CID}", trusted_channel="manual_cli")
    assert caught.value.code == (
        "version_conflict" if changed == "revision" else "binding_conflict"
    )
