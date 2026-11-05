"""Real ASGI and strict notification adapters with an explicit service/pin spy.

The spy verifies adapter authority; these are not notification persistence tests.
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
from naumi_agent.api.routes.notifications import router
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_commands import (
    parse_notification_command,
    run_notification_command,
)
from naumi_agent.personal.notification_tools import (
    MAX_REQUEST_BYTES,
    PersonalNotificationsTool,
    execute_notification_operation,
    request_schema,
    validate_notification_arguments,
)
from naumi_agent.tools.base import ToolExecutionError

BATCH = "nb_" + "1" * 32
EVENT = "ne_" + "2" * 32
CLIENT = "nc_" + "3" * 32
ACTOR = "pa_" + "4" * 32
CLAIM = "no_" + "5" * 32
PREFS = "np_" + "6" * 32
PRESENT = "ns_" + "7" * 32
READ = "nr_" + "8" * 32
DIGEST = "9" * 64
KEY = "notification-interface-test-key"
PRIVATE = "PRIVATE_NOTIFICATION_BODY_NOT_IN_ERROR_OR_MODEL_UNPINNED_OUTPUT"
REF = {"event_id": EVENT, "event_digest": DIGEST}
PAYLOADS = {
    "set_preferences": {
        "request_id": "stable-request",
        "expected_revision": 0,
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "quiet_hours": [{"weekdays": [0, 1], "start": "22:00", "end": "08:00", "full_day": False}],
        "muted_actor_ids": [ACTOR],
        "debounce_seconds": 60,
        "max_delay_seconds": 300,
        "bypass_kinds": [],
        "include_existing": False,
    },
    "claim": {
        "request_id": "stable-request",
        "batch_id": BATCH,
        "expected_digest": DIGEST,
        "client_id": CLIENT,
    },
    "present": {
        "request_id": "stable-request",
        "batch_id": BATCH,
        "batch_digest": DIGEST,
        "client_id": CLIENT,
        "claim_id": CLAIM,
        "event_refs": [REF],
    },
    "mark_read": {"request_id": "stable-request", "event_refs": [REF]},
}
ROUTE_WRITES = {
    "set_preferences": "preferences",
    "claim": "claim",
    "present": "presentation",
    "mark_read": "notice-read",
}
CLI_WRITES = {
    "set_preferences": "preferences-json",
    "claim": "claim-json",
    "present": "present-json",
    "mark_read": "read-json",
}


class AdapterPort:
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
                        "preferences_id": PREFS,
                        "preferences_revision": 1,
                        "batch_id": BATCH,
                        "batch_digest": DIGEST,
                        "client_id": CLIENT,
                        "claim_id": CLAIM,
                        "presentation_id": PRESENT,
                        "event_refs": [REF],
                        "notice_read_ids": [READ],
                    },
                    "view": {"body": object() if self.malformed_projection else PRIVATE},
                    "projection_available": True,
                }
            return {"operation": operation, "private_body": PRIVATE}

        return call

    async def model_result(self, operation, result, actor, arguments):
        self.pins.append((operation, result, actor, copy.deepcopy(arguments)))
        if self.pin_error:
            raise self.pin_error
        return {"content_transport": "explicit_test_pin", "operation": operation, "actor": actor}


@pytest.fixture
def port():
    return AdapterPort()


@pytest.fixture
def app(port):
    application = FastAPI()
    application.include_router(router)
    application.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    application.state.engine = SimpleNamespace(personal_notifications=port)
    return application


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://notification-local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("status", [401, 429])
async def test_early_auth_rate_limit_errors_never_cache(status):
    application = FastAPI()
    application.state.config = SimpleNamespace(
        api=SimpleNamespace(api_keys=[KEY], rate_limit_rpm=1)
    )
    application.add_middleware(AuthMiddleware)
    application.add_middleware(RateLimitMiddleware)

    @application.get("/api/v1/personal/notifications/context")
    async def context():
        return {"ok": True}

    async with client(application) as http:
        if status == 429:
            first = await http.get("/api/v1/personal/notifications/context")
            assert first.status_code == 200
        response = await http.get(
            "/api/v1/personal/notifications/context", headers={"X-API-Key": "wrong"}
        )
    assert response.status_code == status
    no_store(response)


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
async def test_header_only_identity_precedes_any_service(app, port, headers, status):
    async with client(app, headers) as http:
        response = await http.get("/personal/notifications/context")
    assert response.status_code == status
    assert bool(port.calls) is (status == 200)
    no_store(response)


async def test_missing_configuration_and_query_key_never_confer_authority(app, port):
    async with client(app) as http:
        query = await http.get("/personal/notifications/context?api_key=" + KEY)
        app.state.config.api.api_keys = []
        missing = await http.get("/personal/notifications/context")
    assert query.status_code == 401 and missing.status_code == 503
    assert not port.calls
    no_store(query)
    no_store(missing)


@pytest.mark.parametrize("operation", PAYLOADS)
async def test_human_writes_exact_shared_service_with_injected_web_channel(app, port, operation):
    payload = copy.deepcopy(PAYLOADS[operation])
    async with client(app) as http:
        response = await http.post(
            "/personal/notifications/" + ROUTE_WRITES[operation], json=payload
        )
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert port.calls == [(operation, (payload,), {"caller_actor": None, "trusted_channel": "web"})]
    assert not port.pins
    no_store(response)


@pytest.mark.parametrize(
    "path,operation,args,kwargs",
    [
        ("/personal/notifications/context", "context", (), {}),
        ("/personal/notifications", "list", (), {"filter": "unread", "limit": 50}),
        (
            "/personal/notifications?filter=all&actor_id="
            + ACTOR
            + "&limit=2&after=original-cursor",
            "list",
            (),
            {"filter": "all", "actor_id": ACTOR, "limit": 2, "after": "original-cursor"},
        ),
        ("/personal/notifications/preferences", "preferences", (), {}),
        ("/personal/notifications/batches/" + BATCH, "get_batch", (BATCH,), {}),
        ("/personal/notifications/prompt", "prompt", (), {}),
        (
            "/personal/notifications/requests/original%2Frequest:1",
            "request_status",
            ("original/request:1",),
            {},
        ),
    ],
)
async def test_human_reads_only_read_exact_queries(app, port, path, operation, args, kwargs):
    async with client(app) as http:
        response = await http.get(path)
    assert response.status_code == 200
    assert port.calls == [(operation, args, {**kwargs, "caller_actor": None})]
    assert not port.pins
    no_store(response)


@pytest.mark.parametrize(
    "query",
    [
        "limit=1&limit=2",
        "filter=all&filter=unread",
        "limit=true",
        "limit=1.0",
        "limit=-1",
        "limit=%EF%BC%91",
        "limit=0",
        "limit=101",
        "filter=read",
        "caller_actor=main",
        "trusted_channel=web",
        "actor_id=invalid",
        "after=",
    ],
)
async def test_query_duplicates_types_unknown_authority_and_bounds(app, port, query):
    async with client(app) as http:
        response = await http.get("/personal/notifications?" + query)
    assert response.status_code == 422 and not port.calls
    no_store(response)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"request_id":"a","request_id":"b"}',
        b'{"request_id":NaN}',
        b"\xff",
        b'{"event_refs":[{"event_id":"a","event_id":"b"}]}',
        b'{"request_id":"a","n":Infinity}',
    ],
)
async def test_strict_json_duplicate_nested_nonfinite_and_encoding(app, port, raw):
    async with client(app) as http:
        response = await http.post(
            "/personal/notifications/notice-read",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422 and not port.calls
    no_store(response)


async def test_capacity_media_and_validation_never_echo_private_input(app, port):
    async with client(app) as http:
        oversized = await http.post(
            "/personal/notifications/claim",
            content=b" " * (MAX_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        wrong_media = await http.post("/personal/notifications/claim", content=PRIVATE)
        invalid = await http.post("/personal/notifications/claim", json={"batch_id": PRIVATE})
        duplicate_media = await http.post(
            "/personal/notifications/claim",
            content=json.dumps(PAYLOADS["claim"]),
            headers=[("Content-Type", "application/json"), ("Content-Type", "application/json")],
        )
    assert [r.status_code for r in (oversized, wrong_media, invalid, duplicate_media)] == [
        413,
        415,
        422,
        415,
    ]
    for response in (oversized, wrong_media, invalid, duplicate_media):
        no_store(response)
        assert PRIVATE not in response.text
    assert not port.calls


@pytest.mark.parametrize(
    "field", ["caller_actor", "trusted_channel", "owner_key", "human", "admin"]
)
async def test_no_body_supplied_identity_or_trust(app, port, field):
    async with client(app) as http:
        response = await http.post(
            "/personal/notifications/claim", json={**PAYLOADS["claim"], field: PRIVATE}
        )
    assert response.status_code == 422 and not port.calls and PRIVATE not in response.text


@pytest.mark.parametrize(
    "update",
    [
        {"enabled": 1},
        {"expected_revision": True},
        {"debounce_seconds": "60"},
        {"debounce_seconds": -1},
        {"max_delay_seconds": 30},
        {"include_existing": 0},
        {"muted_actor_ids": [ACTOR, ACTOR]},
        {"bypass_kinds": ["item_failure", "item_failure"]},
        {"bypass_kinds": ["item_result"]},
        {"timezone_sha256": PRIVATE},
        {
            "quiet_hours": [
                {"weekdays": [True], "start": "22:00", "end": "08:00", "full_day": False}
            ]
        },
        {
            "quiet_hours": [
                {"weekdays": [0, 0], "start": "22:00", "end": "08:00", "full_day": False}
            ]
        },
        {"quiet_hours": [{"weekdays": [], "start": "22:00", "end": "08:00", "full_day": False}]},
        {"quiet_hours": [{"weekdays": [0], "start": "24:00", "end": "08:00", "full_day": False}]},
        {"quiet_hours": [{"weekdays": [0], "start": "08:00", "end": "08:00", "full_day": False}]},
        {"quiet_hours": [{"weekdays": [0], "start": "22:00", "end": "08:00", "full_day": True}]},
    ],
)
def test_preferences_strict_complete_raw_input(update):
    with pytest.raises(PersonalAgentError):
        validate_notification_arguments(
            {"operation": "set_preferences", "payload": {**PAYLOADS["set_preferences"], **update}},
            human=True,
        )


@pytest.mark.parametrize("operation", ["present", "mark_read"])
@pytest.mark.parametrize(
    "refs", [[], [REF, REF], [{"event_id": EVENT}], [{**REF, "body": PRIVATE}]]
)
def test_exact_members_nonempty_unique_bound_and_body_free(operation, refs):
    with pytest.raises(PersonalAgentError):
        validate_notification_arguments(
            {"operation": operation, "payload": {**PAYLOADS[operation], "event_refs": refs}},
            human=True,
        )


async def test_unknown_error_keeps_original_request_and_sanitizes(app, port):
    port.error = RuntimeError(PRIVATE)
    async with client(app) as http:
        response = await http.post("/personal/notifications/claim", json=PAYLOADS["claim"])
    assert response.status_code == 503 and response.json()["request_id"] == "stable-request"
    assert response.json()["code"] == "notification_unknown" and PRIVATE not in response.text
    assert len(port.calls) == 1
    no_store(response)


@pytest.mark.parametrize(
    "code,status",
    [
        ("scope_denied", 403),
        ("not_found", 404),
        ("version_conflict", 409),
        ("storage_error", 503),
        ("notification_claimed", 409),
        ("notification_unavailable", 409),
        ("notification_busy", 409),
    ],
)
async def test_declared_errors_have_sanitized_codes(app, port, code, status):
    port.error = PersonalAgentError(code, PRIVATE)
    async with client(app) as http:
        response = await http.get("/personal/notifications/context")
    assert response.status_code == status and response.json()["code"] == code
    assert PRIVATE not in response.text
    no_store(response)


async def test_committed_mutation_survives_projection_serialization_failure(app, port):
    port.malformed_projection = True
    async with client(app) as http:
        response = await http.post("/personal/notifications/presentation", json=PAYLOADS["present"])
    result = response.json()
    assert response.status_code == 200 and result["accepted"] is True
    assert result["request_id"] == "stable-request" and result["projection_available"] is False
    assert result["identity"]["claim_id"] == CLAIM and result["identity"]["event_refs"] == [REF]
    assert "view" not in result and len(port.calls) == 1


def test_model_schema_is_read_only_no_human_or_scope_overrides():
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    operations = {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]}
    assert operations == {"context", "list", "preferences", "get-batch", "requests"}
    rendered = json.dumps(schema)
    for forbidden in (
        "actor_id",
        "caller_actor",
        "trusted_channel",
        "payload",
        "prompt",
        "mark_read",
        "claim",
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize("operation", [*PAYLOADS, "prompt", "read", "approve", "execute"])
async def test_model_never_mutates_claims_presents_reads_or_approves(port, operation):
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalNotificationsTool(port).execute(
            operation=operation, payload=PAYLOADS.get(operation, {})
        )
    assert caught.value.code == "invalid_input" and not port.calls


@pytest.mark.parametrize(
    "operation,arguments,service_operation",
    [
        ("context", {}, "context"),
        ("list", {"filter": "attention", "limit": 2}, "list"),
        ("preferences", {}, "preferences"),
        ("get-batch", {"batch_id": BATCH}, "get_batch"),
        ("requests", {"request_id": "original/request"}, "request_status"),
    ],
)
async def test_every_model_read_actual_actor_and_mandatory_source_projection(
    port, monkeypatch, operation, arguments, service_operation
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(runtime, "require_current_agent", lambda engine: SimpleNamespace(id=ACTOR))
    result = json.loads(
        await PersonalNotificationsTool(port).execute(operation=operation, **arguments)
    )
    assert port.calls[0][0] == service_operation and port.calls[0][2]["caller_actor"] == ACTOR
    assert port.pins[0][0] == operation and port.pins[0][2] == ACTOR
    assert port.pins[0][3] == {"operation": operation, **arguments}
    assert result["content_transport"] == "explicit_test_pin" and PRIVATE not in json.dumps(result)


async def test_pin_failure_never_falls_back_to_raw_read(port):
    port.pin_error = PersonalAgentError("scope_denied", PRIVATE)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalNotificationsTool(port).execute(operation="get-batch", batch_id=BATCH)
    assert caught.value.code == "scope_denied" and PRIVATE not in str(caught.value)
    assert len(port.calls) == len(port.pins) == 1


def test_model_parser_rejects_duplicates_nonfinite_capacity_authority(port):
    tool = PersonalNotificationsTool(port)
    for raw in (
        '{"operation":"context","operation":"preferences"}',
        '{"operation":"context","value":NaN}',
        " " * (MAX_REQUEST_BYTES + 1),
        {"operation": "context", "caller_actor": PRIVATE},
        {"operation": "list", "actor_id": ACTOR},
    ):
        with pytest.raises(ValueError) as caught:
            tool.parse_arguments(raw)
        assert PRIVATE not in str(caught.value)
    assert tool.metadata.requires_confirmation is False and tool.metadata.read_only is True


@pytest.mark.parametrize("channel", [None, "agent", "manual_api", []])
async def test_dispatcher_never_invents_human_trust(port, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_notification_operation(
            port,
            {"operation": "mark_read", "payload": PAYLOADS["mark_read"]},
            trusted_channel=channel,
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize("channel", ["web", "cli", "tui"])
@pytest.mark.parametrize("operation", PAYLOADS)
async def test_shared_human_command_exact_channel_and_request(port, channel, operation):
    argument = CLI_WRITES[operation] + " '" + json.dumps(PAYLOADS[operation]) + "'"
    result = json.loads(
        await run_notification_command(
            SimpleNamespace(personal_notifications=port), argument, trusted_channel=channel
        )
    )
    assert result["request_id"] == "stable-request"
    assert port.calls == [
        (operation, (PAYLOADS[operation],), {"caller_actor": None, "trusted_channel": channel})
    ]


def test_cli_read_exact_ids_options_and_rejects_ambiguous_arguments():
    assert parse_notification_command("") == {"operation": "context"}
    assert parse_notification_command("get-batch " + BATCH) == {
        "operation": "get-batch",
        "batch_id": BATCH,
    }
    assert parse_notification_command("requests original/request") == {
        "operation": "requests",
        "request_id": "original/request",
    }
    assert parse_notification_command(
        "list --filter attention --actor main --limit 2 --after original"
    ) == {
        "operation": "list",
        "filter": "attention",
        "actor_id": "main",
        "limit": 2,
        "after": "original",
    }
    for argument in (
        "list --limit 1 --limit 2",
        "list --limit true",
        "get-batch",
        "context " + PRIVATE,
        "prompt",
        'read-json \'{"request_id":"a","request_id":"b"}\'',
    ):
        with pytest.raises(ValueError) as caught:
            parse_notification_command(argument)
        assert PRIVATE not in str(caught.value)


async def test_cli_unknown_keeps_original_request_no_retry(port):
    port.error = RuntimeError(PRIVATE)
    failed = []
    output = await run_notification_command(
        SimpleNamespace(
            personal_notifications=port, mark_manual_command_failed=lambda: failed.append(True)
        ),
        "claim-json '" + json.dumps(PAYLOADS["claim"]) + "'",
        trusted_channel="cli",
    )
    assert "stable-request" in output and PRIVATE not in output and len(port.calls) == 1
    assert failed == [True]
