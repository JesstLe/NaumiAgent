"""Real ASGI boundaries, human command dispatch and mandatory model pin adapters.

The explicit service spy proves adapter authority, not remote-source acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware
from naumi_agent.api.routes.workspaces import router
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_commands import parse_workspace_command, run_workspace_command
from naumi_agent.personal.workspace_models import error
from naumi_agent.personal.workspace_tools import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PersonalWorkspacesTool,
    execute_workspace_operation,
    request_schema,
    validate_workspace_arguments,
)
from naumi_agent.tools.base import ToolExecutionError

PREFIX = "/api/v1/personal/workspaces"
KEY = "workspace-interface-key"
ACTOR = "pa_" + "1" * 32
CONNECTION = "wc_" + "2" * 32
GRANT = "wg_" + "3" * 32
ITEM = "pi_" + "4" * 32
READ = "ws_" + "5" * 32
SHA = "6" * 64
SECRET = "7" * 64
PRIVATE = "PRIVATE_WORKSPACE_CONTENT_NOT_IN_ERRORS_OR_UNPINNED_MODEL_OUTPUT"
AUTH = "NaumiWorkspace " + "8" * 32 + ":1793844000:" + "9" * 64
RECIPIENT = {
    "version": 1,
    "owner_key": SHA,
    "workspace_key": SHA,
    "session_db_key": SHA,
    "directory_instance_id": "a" * 32,
    "actor_id": ACTOR,
    "actor_version": 1,
}
SOURCE = {
    key: value for key, value in RECIPIENT.items() if key not in {"actor_id", "actor_version"}
} | {"item_instance_id": "b" * 32}
TICKET = {
    "version": 1,
    "grant_id": GRANT,
    "grant_revision": 1,
    "source": SOURCE,
    "recipient": RECIPIENT,
    "source_actor_id": "main",
    "source_actor_version": 1,
    "scopes": ["items", "results"],
    "expires_at": "2027-01-01T00:00:00Z",
    "secret": SECRET,
}
PAYLOADS = {
    "issue": {
        "request_id": "original/request:1",
        "recipient": RECIPIENT,
        "source_actor_id": "main",
        "source_actor_version": 1,
        "scopes": ["items", "results"],
        "expires_at": "2027-01-01T00:00:00Z",
    },
    "revoke": {"request_id": "original/request:1", "grant_id": GRANT, "expected_revision": 1},
    "connect": {
        "request_id": "original/request:1",
        "endpoint": "http://127.0.0.1:8123",
        "label": "另一个工作区",
        "ticket": TICKET,
    },
    "disconnect": {
        "request_id": "original/request:1",
        "connection_id": CONNECTION,
        "expected_revision": 1,
    },
}
READS = [
    {"operation": "context"},
    {"operation": "list"},
    {"operation": "overview", "connection_id": CONNECTION, "after": ITEM, "limit": 2},
    {"operation": "result", "connection_id": CONNECTION, "item_id": ITEM},
]
HUMAN_READS = [
    *READS,
    {"operation": "recipient", "actor_id": ACTOR},
    {"operation": "grants"},
    {"operation": "requests", "request_id": "original/request:1"},
]
FEDERATION = {"operation": "inspect", "grant_id": GRANT, "recipient": RECIPIENT}


class AdapterPort:
    def __init__(self):
        self.engine = SimpleNamespace(_session=None)
        self.calls, self.federated, self.pins = [], [], []
        self.failure = self.pin_failure = None
        self.body = None

    async def execute_operation(self, arguments, **kwargs):
        self.calls.append((copy.deepcopy(arguments), copy.deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        if self.body is not None:
            return self.body
        if arguments["operation"] in PAYLOADS:
            return {
                "accepted": True,
                "request_id": arguments["payload"]["request_id"],
                "identity": {"id": CONNECTION, "revision": 1, "status": "active"},
            }
        return {"operation": arguments["operation"], "body": PRIVATE}

    async def model_result(self, operation, result, actor, arguments):
        self.pins.append((operation, copy.deepcopy(result), actor, copy.deepcopy(arguments)))
        if self.pin_failure:
            raise self.pin_failure
        return {"content_transport": "explicit_test_pin", "actor": actor, "operation": operation}

    async def federation(self, arguments, **kwargs):
        self.federated.append((copy.deepcopy(arguments), copy.deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        return (
            self.body
            if self.body is not None
            else {
                "source": SOURCE,
                "grant": {"id": GRANT},
                "body": {"entries": []},
                "read_ref": None,
                "proof": {"nonce": "8" * 32, "signature": "9" * 64},
            }
        )


@pytest.fixture
def port():
    return AdapterPort()


@pytest.fixture
def app(port):
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.state.config = SimpleNamespace(
        api=SimpleNamespace(api_keys=[KEY], rate_limit_rpm=60)
    )
    application.state.engine = SimpleNamespace(personal_workspaces=port)
    return application


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://workspace-local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("arguments", HUMAN_READS)
async def test_management_exact_envelope_outside_web_authority(app, port, arguments):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200
    assert port.calls == [(arguments, {"caller_actor": None, "trusted_channel": "web"})]
    assert not port.pins and not port.federated
    no_store(response)


@pytest.mark.parametrize("operation", PAYLOADS)
async def test_management_writes_one_shared_service_no_model_pin(app, port, operation):
    arguments = {"operation": operation, "payload": copy.deepcopy(PAYLOADS[operation])}
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert port.calls == [(arguments, {"caller_actor": None, "trusted_channel": "web"})]
    assert not port.pins
    no_store(response)


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"X-API-Key": "wrong"}, 401),
        ({"Authorization": "Bearer " + KEY}, 200),
        ({"Authorization": AUTH}, 401),
        ({"Authorization": "Basic " + KEY}, 401),
        ([("X-API-Key", KEY), ("X-API-Key", KEY)], 401),
        ([("Authorization", "Bearer " + KEY), ("Authorization", "Bearer " + KEY)], 401),
        ({"X-API-Key": KEY, "Authorization": "Bearer " + KEY}, 401),
    ],
)
async def test_management_header_only_authentication(app, port, headers, status):
    async with client(app, headers) as http:
        response = await http.get(PREFIX + "/context")
    assert response.status_code == status
    assert bool(port.calls) is (status == 200)
    no_store(response)


async def test_management_requires_configured_keys_and_rejects_query_key(app, port):
    async with client(app) as http:
        query = await http.get(PREFIX + "/context?api_key=" + KEY)
        app.state.config.api.api_keys = []
        missing = await http.get(PREFIX + "/context")
    assert query.status_code == 401 and missing.status_code == 503
    assert not port.calls
    no_store(query)
    no_store(missing)


@pytest.mark.parametrize("query", ["?actor_id=main", "?limit=2", "?x=1&x=2", "?operation=context"])
async def test_management_no_query_arguments(app, port, query):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations" + query, json={"operation": "context"})
    assert response.status_code == 422 and not port.calls
    no_store(response)


@pytest.mark.parametrize(
    "body",
    [
        '{"operation":"context","operation":"list"}',
        '{"operation":"disconnect","payload":{"request_id":"a","request_id":"b"}}',
        '{"operation":"context","value":NaN}',
        '{"operation":"context","value":Infinity}',
        '{"operation":"context","value":-Infinity}',
        "[]",
        "null",
        '"context"',
        '{"operation":"context","caller_actor":"main"}',
        '{"operation":"context","trusted_channel":"web"}',
        '{"operation":"context","args":{}}',
        '{"operation":"federation"}',
        '{"operation":"approve"}',
        '{"operation":"execute"}',
        '{"operation":"overview","limit":true}',
        '{"operation":"overview","limit":1.0}',
        '{"operation":"overview","limit":"2"}',
        '{"operation":"overview","limit":0}',
        '{"operation":"overview","limit":101}',
        '{"operation":"overview","after":""}',
        '{"operation":"result","connection_id":"' + CONNECTION + '"}',
        '{"operation":"context"',
    ],
)
async def test_strict_json_and_operation_fields_before_dispatch(app, port, body):
    async with client(app) as http:
        response = await http.post(
            PREFIX + "/operations", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422 and not port.calls
    assert SECRET not in response.text
    no_store(response)


@pytest.mark.parametrize(
    "body,headers,status",
    [
        (b"{}", {"Content-Type": "text/plain"}, 415),
        (b"{}", {"Content-Type": "application/json; charset=utf-16"}, 415),
        (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, 415),
        (b"\xff", {"Content-Type": "application/json"}, 422),
        ('{"operation":"context"}'.encode("utf-16"), {"Content-Type": "application/json"}, 422),
        (b" " * (MAX_REQUEST_BYTES + 1), {"Content-Type": "application/json"}, 413),
    ],
    ids=["text", "wrong-charset", "content-encoding", "invalid-utf8", "utf16", "oversize"],
)
async def test_media_encoding_capacity_bounds(app, port, body, headers, status):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", content=body, headers=headers)
    assert response.status_code == status and not port.calls
    no_store(response)


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, 2**53])
@pytest.mark.parametrize(
    "operation,field", [("revoke", "expected_revision"), ("issue", "source_actor_version")]
)
def test_human_revision_is_strict_bounded_integer(value, operation, field):
    payload = copy.deepcopy(PAYLOADS[operation])
    payload[field] = value
    with pytest.raises(PersonalAgentError) as caught:
        validate_workspace_arguments({"operation": operation, "payload": payload}, human=True)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize(
    "path", ["version", "grant_revision", "recipient.version", "source.version"]
)
@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_ticket_literal_versions_never_coerce(path, value):
    payload = copy.deepcopy(PAYLOADS["connect"])
    target = payload["ticket"]
    for part in path.split(".")[:-1]:
        target = target[part]
    target[path.split(".")[-1]] = value
    with pytest.raises(PersonalAgentError):
        validate_workspace_arguments({"operation": "connect", "payload": payload}, human=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8123",
        "http://example.com:8123",
        "https://127.0.0.1:8123",
        "http://127.0.0.1:8123/path",
        "http://127.0.0.1:8123?x=1",
        "http://127.0.0.1:8123#x",
        "http://user:pass@127.0.0.1:8123",
        "http://127.0.0.2:8123",
        "http://127.0.0.1:0",
        "http://2130706433:8123",
        "http://127.1:8123",
        "http://[::ffff:127.0.0.1]:8123",
    ],
)
def test_human_connect_literal_loopback_only(endpoint):
    with pytest.raises(PersonalAgentError):
        validate_workspace_arguments(
            {"operation": "connect", "payload": {**PAYLOADS["connect"], "endpoint": endpoint}},
            human=True,
        )


@pytest.mark.parametrize("scopes", [[], ["results"], ["items", "items"], ["items", "write"]])
def test_issue_scope_exact_unique_and_includes_items(scopes):
    with pytest.raises(PersonalAgentError):
        validate_workspace_arguments(
            {"operation": "issue", "payload": {**PAYLOADS["issue"], "scopes": scopes}}, human=True
        )


def test_model_schema_four_reads_without_management_or_authority():
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    assert {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]} == {
        "context",
        "list",
        "overview",
        "result",
    }
    for forbidden in (
        "actor_id",
        "caller_actor",
        "trusted_channel",
        "payload",
        "ticket",
        "secret",
        "issue",
        "revoke",
        'connect"',
        "disconnect",
        "federation",
        "grant_id",
    ):
        assert forbidden not in json.dumps(schema)


@pytest.mark.parametrize("arguments", READS)
@pytest.mark.parametrize("actor", ["main", ACTOR])
async def test_model_actual_actor_and_exact_mandatory_projection(
    port, monkeypatch, arguments, actor
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda engine: SimpleNamespace(id=actor) if actor != "main" else None,
    )
    result = json.loads(await PersonalWorkspacesTool(port).execute(**arguments))
    assert port.calls == [(arguments, {"caller_actor": actor, "trusted_channel": None})]
    assert port.pins == [
        (
            arguments["operation"],
            {"operation": arguments["operation"], "body": PRIVATE},
            actor,
            arguments,
        )
    ]
    assert result == {
        "content_transport": "explicit_test_pin",
        "actor": actor,
        "operation": arguments["operation"],
    }


@pytest.mark.parametrize(
    "operation",
    [
        *PAYLOADS,
        "recipient",
        "grants",
        "requests",
        "federation",
        "approve",
        "check",
        "execute",
        "mark_read",
    ],
)
async def test_model_cannot_call_management_or_source_operations(port, operation):
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalWorkspacesTool(port).execute(operation=operation)
    assert caught.value.code == "invalid_input" and not port.calls and not port.pins


@pytest.mark.parametrize("code", ["scope_denied", "source_stale", "grant_revoked", "storage_error"])
async def test_model_pin_failure_never_falls_back_to_private_body(port, code):
    port.pin_failure = PersonalAgentError(code, PRIVATE + SECRET)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalWorkspacesTool(port).execute(**READS[3])
    assert caught.value.code == code
    assert PRIVATE not in str(caught.value) and SECRET not in str(caught.value)
    assert len(port.calls) == len(port.pins) == 1


def test_tool_parser_metadata_and_no_scope_overrides(port):
    tool = PersonalWorkspacesTool(port)
    for raw in (
        '{"operation":"context","operation":"list"}',
        '{"operation":"context","value":NaN}',
        " " * (MAX_REQUEST_BYTES + 1),
        {"operation": "overview", "actor_id": "main"},
        {"operation": "context", "caller_actor": PRIVATE},
    ):
        with pytest.raises(ValueError) as caught:
            tool.parse_arguments(raw)
        assert PRIVATE not in str(caught.value)
    assert tool.parse_arguments('{"operation":"context"}') == {"operation": "context"}
    assert tool.metadata.read_only and tool.metadata.concurrency_safe
    assert tool.metadata.requires_confirmation is False


@pytest.mark.parametrize("channel", [None, "agent", "management_web", []])
async def test_mutation_dispatch_never_invents_trusted_human_channel(port, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_workspace_operation(
            port, {"operation": "revoke", "payload": PAYLOADS["revoke"]}, trusted_channel=channel
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize("caller,channel", [(ACTOR, "web"), ("impostor", None), ([], None)])
async def test_model_dispatch_no_channel_or_actor_coercion(port, caller, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_workspace_operation(
            port, {"operation": "context"}, caller_actor=caller, trusted_channel=channel
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 403),
        ({"X-API-Key": KEY}, 403),
        ({"Authorization": "Bearer " + SECRET}, 403),
        ({"Authorization": "Bearer " + KEY}, 403),
        ({"Authorization": AUTH}, 200),
        ({"Authorization": AUTH, "X-API-Key": KEY}, 403),
        ({"Authorization": AUTH, "Origin": "http://localhost"}, 403),
        ({"Authorization": AUTH, "Cookie": "key=" + KEY}, 403),
        ([("Authorization", AUTH), ("Authorization", AUTH)], 403),
        ({"Authorization": AUTH.replace(":1793844000:", ":01793844000:")}, 403),
        ({"Authorization": AUTH.replace(":1793844000:", ":0:")}, 403),
        ({"Authorization": AUTH + " "}, 403),
    ],
)
async def test_federation_separate_mutual_proof_auth(app, port, headers, status):
    async with client(app, headers) as http:
        response = await http.post(PREFIX + "/federation", json=FEDERATION)
    assert response.status_code == status
    assert bool(port.federated) is (status == 200) and not port.calls
    if status == 200:
        assert port.federated == [(FEDERATION, {"token": AUTH, "trace": []})]
        assert response.json()["proof"] == {"nonce": "8" * 32, "signature": "9" * 64}
    no_store(response)


@pytest.mark.parametrize("trace", ["", SHA, ",".join(f"{index:064x}" for index in range(8))])
async def test_federation_bounded_unique_trace_forwarded_exactly(app, port, trace):
    async with client(app, {"Authorization": AUTH, "X-Naumi-Workspace-Trace": trace}) as http:
        response = await http.post(PREFIX + "/federation", json=FEDERATION)
    assert response.status_code == 200
    assert port.federated[0][1] == {"token": AUTH, "trace": trace.split(",") if trace else []}


@pytest.mark.parametrize(
    "trace",
    [
        SHA + "," + SHA,
        "A" * 64,
        '["' + SHA + '"]',
        SHA + ",",
        SHA + ", " + "1" * 64,
        ",".join(f"{index:064x}" for index in range(9)),
    ],
)
async def test_federation_invalid_trace_before_service(app, port, trace):
    async with client(app, {"Authorization": AUTH, "X-Naumi-Workspace-Trace": trace}) as http:
        response = await http.post(PREFIX + "/federation", json=FEDERATION)
    assert response.status_code == 422 and not port.federated
    no_store(response)


async def test_federation_duplicate_trace_and_any_query_rejected(app, port):
    async with client(
        app,
        [
            ("Authorization", AUTH),
            ("X-Naumi-Workspace-Trace", SHA),
            ("X-Naumi-Workspace-Trace", "1" * 64),
        ],
    ) as http:
        duplicate = await http.post(PREFIX + "/federation", json=FEDERATION)
    async with client(app, {"Authorization": AUTH}) as http:
        query = await http.post(PREFIX + "/federation?unrecognized=value", json=FEDERATION)
    assert duplicate.status_code == 403 and query.status_code == 403 and not port.federated
    no_store(duplicate)
    no_store(query)


@pytest.mark.parametrize(
    "fields",
    [
        {"operation": "page", "limit": 1, "after": ITEM},
        {"operation": "result", "item_id": ITEM},
        {
            "operation": "validate",
            "read_ref": {"read_id": READ, "revision": 1, "digest": SHA, "body_sha256": SHA},
        },
    ],
)
async def test_federation_operation_exact_fields(app, port, fields):
    arguments = {**FEDERATION, **fields}
    async with client(app, {"Authorization": AUTH}) as http:
        response = await http.post(PREFIX + "/federation", json=arguments)
    assert response.status_code == 200
    assert port.federated == [(arguments, {"token": AUTH, "trace": []})]


@pytest.mark.parametrize(
    "fields",
    [
        {"operation": "inspect", "limit": 1},
        {"operation": "inspect", "item_id": ITEM},
        {"operation": "page", "limit": True},
        {"operation": "page", "limit": 1.0},
        {"operation": "page", "after": "http://localhost"},
        {"operation": "result"},
        {"operation": "validate"},
        {"operation": "execute"},
        {"secret": SECRET},
        {"caller_actor": "main"},
        {"trusted_channel": "web"},
    ],
)
async def test_federation_unknown_or_wrong_operation_members_rejected(app, port, fields):
    async with client(app, {"Authorization": AUTH}) as http:
        response = await http.post(PREFIX + "/federation", json={**FEDERATION, **fields})
    assert response.status_code == 422 and not port.federated
    assert SECRET not in response.text
    no_store(response)


@pytest.mark.parametrize(
    "code,status",
    [
        ("scope_denied", 403),
        ("source_stale", 409),
        ("grant_revoked", 403),
        ("not_found", 404),
        ("storage_error", 503),
        ("input_limit", 413),
        ("workspace_busy", 409),
        ("idempotency_conflict", 409),
        ("credential_missing", 503),
        ("unexpected_private_code", 503),
    ],
)
async def test_errors_sanitized_fixed_public_messages(app, port, code, status):
    port.failure = PersonalAgentError(code, PRIVATE + SECRET)
    async with client(app) as http:
        response = await http.get(PREFIX + "/context")
    assert response.status_code == status
    assert response.json()["code"] == error(code).code
    assert response.json()["detail"] == error(code).message
    assert PRIVATE not in response.text and SECRET not in response.text
    no_store(response)


@pytest.mark.parametrize(
    "body,status",
    [
        ({"bad": object()}, 503),
        ({"bad": float("nan")}, 503),
        ({"large": "x" * MAX_RESPONSE_BYTES}, 413),
    ],
)
async def test_response_bound_and_serialization_sanitized(app, port, body, status):
    port.body = body
    async with client(app, {"Authorization": AUTH}) as http:
        response = await http.post(PREFIX + "/federation", json=FEDERATION)
    assert response.status_code == status and len(response.content) < 1024
    no_store(response)


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("POST", PREFIX + "/federation", 200),
        ("GET", PREFIX + "/federation", 401),
        ("POST", PREFIX + "/operations", 401),
        ("POST", PREFIX + "/federation/extra", 401),
        ("POST", PREFIX + "/federation/", 401),
        ("GET", PREFIX + "/context", 401),
    ],
)
async def test_middleware_exemption_only_one_exact_post(app, port, method, path, status):
    app.add_middleware(AuthMiddleware)
    async with client(app, {"Authorization": AUTH}) as http:
        response = await http.request(method, path, json=FEDERATION)
    assert response.status_code == status
    assert bool(port.federated) is (status == 200)
    no_store(response)


async def test_federation_still_rate_limited_and_errors_no_store(app, port):
    app.state.config.api.rate_limit_rpm = 1
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)
    async with client(app, {"Authorization": AUTH}) as http:
        first = await http.post(PREFIX + "/federation", json=FEDERATION)
        second = await http.post(PREFIX + "/federation", json=FEDERATION)
    assert first.status_code == 200 and second.status_code == 429
    assert len(port.federated) == 1
    no_store(first)
    no_store(second)


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", PREFIX + "/federation", 405),
        ("POST", PREFIX + "/federation/", 307),
        ("GET", PREFIX + "/unknown", 404),
    ],
)
async def test_workspace_framework_errors_and_redirect_never_cache(app, method, path, status):
    app.add_middleware(AuthMiddleware)
    async with client(app) as http:
        response = await http.request(method, path)
    assert response.status_code == status
    no_store(response)


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
@pytest.mark.parametrize("operation", PAYLOADS)
async def test_human_commands_same_exact_request_injected_channel(port, channel, operation):
    payload = copy.deepcopy(PAYLOADS[operation])
    arguments = {"operation": operation, "payload": payload}
    output = await run_workspace_command(
        SimpleNamespace(personal_workspaces=port),
        operation + "-json '" + json.dumps(payload) + "'",
        trusted_channel=channel,
    )
    assert json.loads(output)["accepted"] is True
    assert port.calls == [(arguments, {"caller_actor": None, "trusted_channel": channel})]
    assert not port.pins


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("", {"operation": "context"}),
        ("help", {"operation": "help"}),
        ("list", {"operation": "list"}),
        ("grants", {"operation": "grants"}),
        ("recipient " + ACTOR, {"operation": "recipient", "actor_id": ACTOR}),
        (
            "requests original/request:1",
            {"operation": "requests", "request_id": "original/request:1"},
        ),
        ("overview --connection " + CONNECTION + " --after " + ITEM + " --limit 2", READS[2]),
        ("result " + CONNECTION + " " + ITEM, READS[3]),
    ],
)
def test_human_command_exact_read_options(argument, expected):
    assert parse_workspace_command(argument) == expected


@pytest.mark.parametrize(
    "argument",
    [
        "overview --limit 1 --limit 2",
        "overview --limit true",
        "overview --limit 1.0",
        "overview --actor main",
        "overview --limit",
        "result " + CONNECTION,
        "list " + PRIVATE,
        "context " + PRIVATE,
        "recipient",
        "requests",
        "execute",
        'issue-json \'{"request_id":"a","request_id":"b"}\'',
    ],
)
def test_human_parser_rejects_ambiguous_arguments_no_echo(argument):
    with pytest.raises(ValueError) as caught:
        parse_workspace_command(argument)
    assert PRIVATE not in str(caught.value)


async def test_human_command_unknown_result_never_retries_or_echoes_secret(port):
    port.failure = RuntimeError(PRIVATE + SECRET)
    failed = []
    output = await run_workspace_command(
        SimpleNamespace(
            personal_workspaces=port, mark_manual_command_failed=lambda: failed.append(True)
        ),
        "connect-json '" + json.dumps(PAYLOADS["connect"]) + "'",
        trusted_channel="cli",
    )
    assert output == error("workspace_unknown").message
    assert len(port.calls) == 1 and failed == [True]
    assert PRIVATE not in output and SECRET not in output


async def test_shared_runtime_registry_and_main_command_dispatch(port):
    from naumi_agent.api.routes.commands import command_index, validate_command
    from naumi_agent.cli.slash_router import execute_slash_command

    assert any(entry.command == "/workspaces" for entry in command_index())
    assert validate_command("/workspaces context") == "/workspaces context"
    output = await execute_slash_command(
        SimpleNamespace(personal_workspaces=port), "/workspaces context"
    )
    assert json.loads(output)["operation"] == "context"
    assert port.calls == [
        ({"operation": "context"}, {"caller_actor": None, "trusted_channel": "cli"})
    ]


async def test_artifact_missing_workspace_component_denies_transitive_read():
    from naumi_agent.personal.artifacts import PersonalArtifactService

    broker = object.__new__(PersonalArtifactService)
    broker.engine = SimpleNamespace()
    root = {
        "artifact_id": "root",
        "version": 1,
        "owner_id": "main",
        "source_refs": [{"artifact_id": "parent", "version": 1}],
    }
    parent = {
        "artifact_id": "parent",
        "version": 1,
        "owner_id": "main",
        "source_refs": [],
        "evidence": {"workspace_read_refs": [{"read_id": READ}]},
    }

    async def call(method, **kwargs):
        return [] if method == "access_grants" else parent

    broker._call = call
    with pytest.raises(PersonalAgentError) as caught:
        await broker._checked("main", root)
    assert caught.value.code == "source_unavailable"


async def test_artifact_existing_workspace_component_receives_original_snapshot():
    from naumi_agent.personal.artifacts import PersonalArtifactService

    broker = object.__new__(PersonalArtifactService)
    calls = []

    async def authorize(snapshot, actor):
        calls.append((copy.deepcopy(snapshot), actor))
        raise error("grant_revoked")

    broker.engine = SimpleNamespace(
        personal_workspaces=SimpleNamespace(authorize_artifact=authorize)
    )

    async def call(method, **kwargs):
        return []

    broker._call = call
    snapshot = {"artifact_id": "original", "version": 1, "owner_id": "main", "source_refs": []}
    with pytest.raises(PersonalAgentError) as caught:
        await broker._checked("main", snapshot)
    assert caught.value.code == "grant_revoked" and calls == [(snapshot, "main")]


@pytest.mark.parametrize("operation", ["issue", "connect", "invalid"])
async def test_actual_web_command_stores_only_opaque_receipt_not_ticket_or_body(
    tmp_path, port, operation
):
    from naumi_agent.api.routes.commands import router as commands_router
    from naumi_agent.config.settings import MemoryConfig
    from naumi_agent.memory.session import SessionStore
    from naumi_agent.runs.store import ChatRunStore

    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="工作区命令")
    engine = SimpleNamespace(
        session_store=sessions, runtime_mode="default", personal_workspaces=port
    )

    async def load_session(identity):
        return await sessions.load(identity)

    engine.load_session = load_session
    engine.set_runtime_mode = lambda mode: setattr(engine, "runtime_mode", mode)
    application = FastAPI()
    application.include_router(commands_router)
    application.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    application.state.engine = engine
    application.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    application.state.engine_lock = asyncio.Lock()
    if operation == "invalid":
        command = "/workspaces " + SECRET
    else:
        command = "/workspaces " + operation + "-json '" + json.dumps(PAYLOADS[operation]) + "'"
        port.body = {"accepted": True, "ticket": TICKET, "body": PRIVATE}
    async with client(application) as http:
        response = await http.post(f"/sessions/{session.id}/commands", json={"command": command})
    assert response.status_code == 200
    no_store(response)
    frames = [json.loads(frame[6:]) for frame in response.text.strip().split("\n\n")]
    assert frames[-1]["data"]["status"] == ("failed" if operation == "invalid" else "completed")
    if operation != "invalid":
        assert SECRET in response.text and PRIVATE in response.text
        assert port.calls[0][1] == {"caller_actor": None, "trusted_channel": "web"}
    saved = await sessions.load(session.id)
    assert saved.messages[-2]["content"] == "/workspaces"
    receipt = json.loads(saved.messages[-1]["content"])
    assert set(receipt) == {"type", "operation", "run_id", "status"}
    assert receipt["type"] == "personal_workspaces_command"
    assert receipt["operation"] == operation
    runs = await application.state.chat_run_store.list_runs(session.id)
    assert len(runs) == 1 and runs[0].steps[0].detail == saved.messages[-1]["content"]
    history = json.dumps(saved.messages, ensure_ascii=False) + str(runs)
    assert SECRET not in history and PRIVATE not in history and "ticket" not in history
    assert engine.runtime_mode == "default"


def test_workspace_private_roots_cover_external_witness_and_store(tmp_path):
    from naumi_agent.personal.artifact_boundary import is_protected

    workspace = tmp_path / "project"
    witness = tmp_path / "private-witness"
    store = tmp_path / "isolated-store"
    engine = SimpleNamespace(
        workspace_root=workspace,
        personal_agents=SimpleNamespace(store=SimpleNamespace(base_dir=workspace / ".naumi")),
        personal_workspaces=SimpleNamespace(
            boundary=witness, store=SimpleNamespace(base_dir=store)
        ),
        config=SimpleNamespace(
            memory=SimpleNamespace(
                session_db_path=str(workspace / "sessions.db"), vector_db_path=""
            )
        ),
    )
    assert is_protected(engine, witness)
    assert is_protected(engine, store / "personal_workspaces.db")
    assert not is_protected(engine, workspace / "public.txt")


@pytest.mark.parametrize("mode", ["permissive", "moderate", "strict"])
@pytest.mark.parametrize(
    "name", ["personal_workspaces", "default.personal_workspaces", "default__personal_workspaces"]
)
def test_workspace_permissions_and_assignable_leaf_tool(port, mode, name):
    from naumi_agent.personal.service import ASSIGNABLE_TOOL_NAMES
    from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionChecker, PermissionMode

    assert "personal_workspaces" in ASSIGNABLE_TOOL_NAMES
    rule = TOOL_PERMISSIONS["personal_workspaces"]
    assert rule.max_calls_per_session == 200 and not rule.allow_session_grant
    checker = PermissionChecker(PermissionMode(mode))
    decision = checker.check(name, {"operation": "context"}, PersonalWorkspacesTool(port))
    assert decision.allowed and not decision.requires_confirmation
    assert decision.tool_family == "personal_workspaces"
