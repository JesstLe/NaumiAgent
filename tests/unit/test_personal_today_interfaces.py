"""Real ASGI and strict adapter boundaries using an explicit service/pin spy.

These tests do not claim Today service persistence or model-provider acceptance.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware
from naumi_agent.api.routes.today import router
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_commands import parse_today_command, run_today_command
from naumi_agent.personal.today_tools import (
    MAX_REQUEST_BYTES,
    PersonalTodayTool,
    execute_today_operation,
    request_schema,
    validate_today_arguments,
)
from naumi_agent.tools.base import ToolExecutionError

DAY = "td_" + "1" * 32
META = "tm_" + "2" * 32
PLACE = "tp_" + "3" * 32
ITEM = "pi_" + "4" * 32
ACTOR = "pa_" + "5" * 32
CHANGE = "tc_" + "6" * 32
KEY = "today-adapter-test-key"
PRIVATE = "PRIVATE_TODAY_BODY_DO_NOT_LEAK_IN_ERROR_OR_OPAQUE_OUTPUT"


@pytest.mark.parametrize("status", [401, 429])
async def test_early_middleware_errors_are_not_cached(status):
    app = FastAPI()
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY], rate_limit_rpm=1))
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/v1/personal/today/context")
    async def context():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://today-local"
    ) as client:
        if status == 429:
            assert (
                await client.get("/api/v1/personal/today/context", headers={"X-API-Key": KEY})
            ).status_code == 200
        response = await client.get("/api/v1/personal/today/context")
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


REF = {
    "kind": "item",
    "workspace_key": "a" * 64,
    "item_id": ITEM,
    "item_revision": 1,
    "once_key": "b" * 32,
    "definition_sha256": "c" * 64,
    "assignee_id": ACTOR,
    "assignee_version": 3,
}
PAYLOADS = {
    "set_metadata": {
        "request_id": "stable-request",
        "expected_revision": 0,
        "source_ref": REF,
        "deadline": None,
        "priority": "high",
        "status": "active",
    },
    "place": {
        "request_id": "stable-request",
        "date": "2026-11-05",
        "timezone": "Asia/Shanghai",
        "expected_day_revision": 0,
        "expected_metadata_revision": 0,
        "source_ref": REF,
    },
    "reorder": {
        "request_id": "stable-request",
        "day_id": DAY,
        "expected_revision": 2,
        "placement_ids": [PLACE],
    },
    "remove": {
        "request_id": "stable-request",
        "day_id": DAY,
        "expected_revision": 2,
        "placement_id": PLACE,
    },
    "rebase": {
        "request_id": "stable-request",
        "day_id": DAY,
        "expected_day_revision": 2,
        "metadata_id": META,
        "expected_metadata_revision": 3,
        "old_source_ref": REF,
        "new_source_ref": REF,
        "preserve_deadline": True,
        "preserve_priority": False,
    },
    "check": {
        "request_id": "stable-request",
        "metadata_id": META,
        "expected_revision": 3,
        "source_ref": REF,
        "checked": True,
    },
    "move": {
        "request_id": "stable-request",
        "day_id": DAY,
        "expected_revision": 2,
        "placement_id": PLACE,
        "target_date": "2026-11-06",
        "target_timezone": "Asia/Shanghai",
        "target_planner_id": ACTOR,
        "expected_target_revision": 0,
    },
}


class AdapterPort:
    """Observe dispatcher/pin contracts; deliberately not a storage implementation."""

    def __init__(self):
        self.engine = SimpleNamespace(_session=None)
        self.calls, self.pins = [], []
        self.error = self.pin_error = None
        self.malformed_projection = False

    def __getattr__(self, operation):
        async def call(*args, **kwargs):
            self.calls.append((operation, copy.deepcopy(args), copy.deepcopy(kwargs)))
            if self.error:
                raise self.error
            if operation in PAYLOADS:
                return {
                    "accepted": True,
                    "request_id": args[0]["request_id"],
                    "operation": operation,
                    "identity": {
                        "day_id": DAY,
                        "day_revision": 3,
                        "metadata_ids": [META],
                        "change_id": CHANGE,
                    },
                    "view": {"private_body": object() if self.malformed_projection else PRIVATE},
                    "projection_available": True,
                }
            return {"operation": operation, "private_body": PRIVATE, "revision": 3}

        return call

    async def model_result(self, operation, result, actor, arguments):
        self.pins.append((operation, result, actor, copy.deepcopy(arguments)))
        if self.pin_error:
            raise self.pin_error
        if operation in {"get", "metadata"}:
            return {
                "content_transport": "explicit_test_pin",
                "pinned_operation": operation,
                "actor": actor,
            }
        if result.get("accepted"):
            return {key: result[key] for key in ("accepted", "request_id", "operation", "identity")}
        return {"operation": operation, "content_transport": "get", "opaque": True}


@pytest.fixture
def port():
    return AdapterPort()


@pytest.fixture
def app(port):
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    app.state.engine = SimpleNamespace(personal_today=port)
    return app


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://today-local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"X-API-Key": "wrong"}, 401),
        ({"Authorization": "Bearer " + KEY}, 200),
        ({"Authorization": "Basic " + KEY}, 401),
        ([("X-API-Key", KEY), ("X-API-Key", KEY)], 401),
        ({"X-API-Key": KEY, "Authorization": "Bearer " + KEY}, 401),
    ],
)
async def test_today_header_only_identity_before_service(app, port, headers, status):
    async with client(app, headers) as http:
        response = await http.get("/personal/today/context")
    assert response.status_code == status
    no_store(response)
    assert bool(port.calls) is (status == 200)


async def test_today_missing_configuration_and_query_key_are_not_authority(app, port):
    async with client(app) as http:
        query = await http.get("/personal/today/context?api_key=" + KEY)
        app.state.config.api.api_keys = []
        unconfigured = await http.get("/personal/today/context")
    assert query.status_code == 401 and unconfigured.status_code == 503
    assert not port.calls
    no_store(query)
    no_store(unconfigured)


@pytest.mark.parametrize("operation", list(PAYLOADS))
async def test_every_human_mutation_uses_exact_shared_service_and_injected_web_channel(
    app, port, operation
):
    body = copy.deepcopy(PAYLOADS[operation])
    if operation == "place":
        body["planner_id"] = ACTOR
    async with client(app) as http:
        response = await http.post("/personal/today/" + operation.replace("_", "-"), json=body)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert port.calls == [(operation, (body,), {"caller_actor": None, "trusted_channel": "web"})]
    assert not port.pins
    no_store(response)


@pytest.mark.parametrize(
    "path,operation,args,kwargs",
    [
        ("/personal/today/context", "context", (), {}),
        (
            "/personal/today?date=2026-11-05&timezone=Asia/Shanghai&planner_id=" + ACTOR,
            "day",
            (),
            {"date": "2026-11-05", "timezone": "Asia/Shanghai", "planner_id": ACTOR, "limit": 50},
        ),
        (
            "/personal/today/sources?limit=2&after=" + ITEM,
            "sources",
            (),
            {"limit": 2, "after": ITEM},
        ),
        ("/personal/today/metadata/" + ITEM, "metadata", (ITEM,), {}),
        ("/personal/today/days/" + DAY + "?revision=2", "get", (DAY,), {"revision": 2}),
        (
            "/personal/today/history/day/" + DAY + "?limit=2&after_revision=1",
            "history",
            (),
            {"kind": "day", "identity": DAY, "limit": 2, "after_revision": 1},
        ),
        (
            "/personal/today/requests/original%2Frequest:1",
            "request_status",
            ("original/request:1",),
            {},
        ),
    ],
)
async def test_human_reads_keep_exact_original_queries_without_model_pinning(
    app, port, path, operation, args, kwargs
):
    async with client(app) as http:
        response = await http.get(path)
    assert response.status_code == 200
    assert port.calls == [(operation, args, {**kwargs, "caller_actor": None})]
    assert not port.pins
    no_store(response)


@pytest.mark.parametrize(
    "query",
    [
        "date=2026-11-05&timezone=Asia/Shanghai&limit=1&limit=2",
        "date=2026-11-05&timezone=Asia/Shanghai&limit=true",
        "date=2026-11-05&timezone=Asia/Shanghai&limit=%EF%BC%91",
        "date=2026-11-05&timezone=Asia/Shanghai&caller_actor=main",
        "date=2026-11-05&timezone=Asia/Shanghai&limit=0",
    ],
)
async def test_today_query_duplicates_coercion_and_authority_fields_are_rejected(app, port, query):
    async with client(app) as http:
        response = await http.get("/personal/today?" + query)
    assert response.status_code == 422 and not port.calls
    no_store(response)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"request_id":"a","request_id":"b"}',
        b'{"request_id":NaN}',
        b"\xff",
        b'{"deadline":{"kind":"date","kind":"instant"}}',
    ],
)
async def test_today_strict_json_rejects_duplicate_nested_nonfinite_and_encoding(app, port, raw):
    async with client(app) as http:
        response = await http.post(
            "/personal/today/set-metadata",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422 and not port.calls
    no_store(response)


async def test_today_capacity_media_and_validation_never_echo_private_input(app, port):
    async with client(app) as http:
        oversized = await http.post(
            "/personal/today/place",
            content=b" " * (MAX_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        wrong_media = await http.post("/personal/today/place", content=PRIVATE)
        invalid = await http.post("/personal/today/place", json={"source_ref": PRIVATE})
    assert (
        oversized.status_code == 413
        and wrong_media.status_code == 415
        and invalid.status_code == 422
    )
    for response in (oversized, wrong_media, invalid):
        no_store(response)
        assert PRIVATE not in response.text
    assert not port.calls


@pytest.mark.parametrize(
    "field", ["caller_actor", "trusted_channel", "owner_key", "human", "checked", "admin"]
)
async def test_manual_body_cannot_supply_identity_or_trust(app, port, field):
    async with client(app) as http:
        response = await http.post(
            "/personal/today/place", json={**PAYLOADS["place"], field: PRIVATE}
        )
    assert response.status_code == 422 and not port.calls and PRIVATE not in response.text


@pytest.mark.parametrize("field", ["resolved_at", "boundary_at", "timezone_sha256"])
async def test_deadline_only_accepts_input_not_client_normalized_authority(app, port, field):
    body = copy.deepcopy(PAYLOADS["set_metadata"])
    body["deadline"] = {
        "kind": "date",
        "local_date": "2026-11-05",
        "timezone": "Asia/Shanghai",
        field: PRIVATE,
    }
    async with client(app) as http:
        response = await http.post("/personal/today/set-metadata", json=body)
    assert response.status_code == 422 and not port.calls and PRIVATE not in response.text


async def test_http_errors_are_sanitized_and_unknown_writes_keep_request(app, port):
    port.error = RuntimeError(PRIVATE)
    async with client(app) as http:
        response = await http.post("/personal/today/place", json=PAYLOADS["place"])
    assert response.status_code == 503
    assert response.json()["request_id"] == "stable-request"
    assert response.json()["code"] == "today_unknown" and PRIVATE not in response.text
    no_store(response)


async def test_http_committed_mutation_survives_projection_serialization_failure(app, port):
    port.malformed_projection = True
    async with client(app) as http:
        response = await http.post("/personal/today/place", json=PAYLOADS["place"])
    value = response.json()
    assert response.status_code == 200 and value["accepted"] is True
    assert value["request_id"] == "stable-request" and value["projection_available"] is False
    assert value["identity"]["change_id"] == CHANGE
    assert "view" not in value and len(port.calls) == 1


def test_model_schema_excludes_human_operations_and_all_planner_authority():
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    rendered = json.dumps(schema)
    for forbidden in (
        "planner_id",
        "target_planner_id",
        "trusted_channel",
        "caller_actor",
        "human_check",
        '"checked"',
    ):
        assert forbidden not in rendered
    operations = {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]}
    assert operations == {
        "context",
        "list",
        "get",
        "metadata",
        "sources",
        "set_metadata",
        "place",
        "reorder",
        "remove",
        "rebase",
        "history",
        "requests",
    }


@pytest.mark.parametrize("operation", ["check", "move", "grant", "execute"])
async def test_model_cannot_invoke_human_or_execution_operations(port, operation):
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalTodayTool(port).execute(
            operation=operation, payload=PAYLOADS.get(operation, {})
        )
    assert caught.value.code == "invalid_input" and not port.calls


@pytest.mark.parametrize(
    "raw",
    [
        {
            "operation": "list",
            "date": "2026-11-05",
            "timezone": "Asia/Shanghai",
            "planner_id": ACTOR,
        },
        {"operation": "place", "payload": {**PAYLOADS["place"], "planner_id": ACTOR}},
        {"operation": "place", "payload": {**PAYLOADS["place"], "caller_actor": "main"}},
        {"operation": "get", "day_id": DAY, "date": "2026-11-05", "timezone": "Asia/Shanghai"},
        {
            "operation": "reorder",
            "payload": {**PAYLOADS["reorder"], "placement_ids": [PLACE, PLACE]},
        },
    ],
)
def test_model_payload_cannot_smuggle_planner_or_ambiguous_identity(raw):
    with pytest.raises(PersonalAgentError):
        validate_today_arguments(raw)


@pytest.mark.parametrize(
    "operation,arguments,service_operation",
    [
        ("get", {"day_id": DAY, "revision": 2}, "get"),
        ("get", {"date": "2026-11-05", "timezone": "Asia/Shanghai"}, "day"),
        ("metadata", {"item_id": ITEM}, "metadata"),
        ("sources", {}, "sources"),
        ("list", {"date": "2026-11-05", "timezone": "Asia/Shanghai"}, "day"),
        ("history", {"kind": "metadata", "identity": META}, "history"),
        ("requests", {"request_id": "original/request"}, "request_status"),
        ("context", {}, "context"),
    ],
)
async def test_model_uses_actual_actor_and_mandatory_service_projection(
    port, monkeypatch, operation, arguments, service_operation
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda engine: SimpleNamespace(id=ACTOR))
    value = json.loads(await PersonalTodayTool(port).execute(operation=operation, **arguments))
    assert port.calls[0][0] == service_operation
    assert port.calls[0][2]["caller_actor"] == ACTOR
    assert port.pins[0][0] == operation and port.pins[0][2] == ACTOR
    assert port.pins[0][3] == {"operation": operation, **arguments}
    assert PRIVATE not in json.dumps(value)


async def test_model_write_has_no_manual_channel_and_projection_failure_keeps_exact_receipt(port):
    port.pin_error = RuntimeError(PRIVATE)
    result = json.loads(
        await PersonalTodayTool(port).execute(operation="place", payload=PAYLOADS["place"])
    )
    assert port.calls == [("place", (PAYLOADS["place"],), {"caller_actor": "main"})]
    assert result["accepted"] is True and result["request_id"] == "stable-request"
    assert result["projection_available"] is False and result["identity"]["change_id"] == CHANGE
    assert "view" not in result and PRIVATE not in json.dumps(result)


async def test_failed_model_pin_never_returns_raw_read_body(port):
    port.pin_error = PersonalAgentError("scope_denied", PRIVATE)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalTodayTool(port).execute(operation="metadata", item_id=ITEM)
    assert caught.value.code == "scope_denied" and PRIVATE not in str(caught.value)


def test_tool_parser_rejects_duplicates_nan_capacity_and_private_messages(port):
    tool = PersonalTodayTool(port)
    for raw in (
        '{"operation":"context","operation":"sources"}',
        '{"operation":"context","x":NaN}',
        " " * (MAX_REQUEST_BYTES + 1),
        {"operation": "context", "caller_actor": PRIVATE},
    ):
        with pytest.raises(ValueError) as caught:
            tool.parse_arguments(raw)
        assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("channel", [None, "agent", "manual_api"])
async def test_dispatcher_never_invents_a_human_channel(port, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_today_operation(
            port, {"operation": "check", "payload": PAYLOADS["check"]}, trusted_channel=channel
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize("channel", ["web", "cli", "tui"])
async def test_shared_human_command_exact_request_and_manual_check_channel(port, channel):
    argument = "check-json '" + json.dumps(PAYLOADS["check"]) + "'"
    result = json.loads(
        await run_today_command(
            SimpleNamespace(personal_today=port), argument, trusted_channel=channel
        )
    )
    assert result["request_id"] == "stable-request"
    assert port.calls == [
        ("check", (PAYLOADS["check"],), {"caller_actor": None, "trusted_channel": channel})
    ]


def test_cli_read_queries_exact_saved_versions_and_reject_duplicate_options():
    assert parse_today_command("") == {"operation": "context"}
    assert parse_today_command(f"get {DAY} 2") == {"operation": "get", "day_id": DAY, "revision": 2}
    assert parse_today_command("list 2026-11-05 Asia/Shanghai --planner main --limit 2") == {
        "operation": "list",
        "date": "2026-11-05",
        "timezone": "Asia/Shanghai",
        "planner_id": "main",
        "limit": 2,
    }
    for argument in (
        "sources --limit 1 --limit 2",
        "sources --limit true",
        "list",
        "context " + PRIVATE,
        'place-json \'{"request_id":"a","request_id":"b"}\'',
    ):
        with pytest.raises(ValueError) as caught:
            parse_today_command(argument)
        assert PRIVATE not in str(caught.value)


async def test_cli_unknown_after_service_preserves_original_request_without_retry(port):
    port.error = RuntimeError(PRIVATE)
    output = await run_today_command(
        SimpleNamespace(personal_today=port),
        "place-json '" + json.dumps(PAYLOADS["place"]) + "'",
        trusted_channel="cli",
    )
    assert "stable-request" in output and PRIVATE not in output and len(port.calls) == 1
