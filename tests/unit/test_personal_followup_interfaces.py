"""Real ASGI/command boundaries and source-pinned followup notification adapters.

The explicit ports verify adapter authority, not original provider execution.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware
from naumi_agent.api.routes.followups import router
from naumi_agent.personal.followup_commands import parse_followup_command, run_followup_command
from naumi_agent.personal.followup_tools import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PersonalFollowupsTool,
    execute_followup_operation,
    public_error,
    request_schema,
    validate_followup_arguments,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_authority import NotificationAuthorityMixin
from naumi_agent.personal.notification_models import record_digest
from naumi_agent.personal.notifications import PersonalNotificationService
from naumi_agent.tools.base import ToolExecutionError

PREFIX = "/api/v1/personal/followups"
KEY = "followup-interface-key"
ACTOR = "pa_" + "1" * 32
RULE = "fr_" + "2" * 32
PROPOSAL = "fp_" + "3" * 32
EFFECT = "fe_" + "4" * 32
CURSOR = "fc_" + "5" * 32
CONNECTION = "pc_" + "6" * 32
ITEM = "pi_" + "7" * 32
READ = "fl_" + "8" * 32
SHA = "9" * 64
PRIVATE = "PRIVATE_FOLLOWUP_SOURCE_BODY_MUST_NOT_ENTER_ERRORS_OR_MODEL_HISTORY"
PROPOSAL_REF = {"proposal_id": PROPOSAL, "revision": 1, "digest": SHA, "workspace_key": SHA}
RULE_BODY = {
    "name": "截止跟进",
    "actor_id": ACTOR,
    "actor_version": 1,
    "status": "active",
    "timezone": "Asia/Shanghai",
    "include_existing": False,
    "max_lateness_minutes": 60,
    "freshness_seconds": 3600,
    "misfire_policy": "latest",
    "trigger": {"kind": "deadline", "item_ids": [ITEM], "offsets_minutes": [60, 0, -60]},
}
CALENDAR_RULE = {
    **RULE_BODY,
    "trigger": {
        "kind": "calendar_change",
        "connection_id": CONNECTION,
        "event_id": None,
        "change_kinds": ["created", "updated", "cancelled", "unavailable", "restored"],
        "fields": ["summary", "description", "start", "end", "status", "available"],
    },
}
TODAY_TARGET = {
    "kind": "today_place",
    "date": "2026-11-05",
    "timezone": "Asia/Shanghai",
    "expected_day_revision": 0,
    "expected_metadata_revision": 1,
}
CALENDAR_TARGET = {
    "kind": "calendar_draft",
    "expires_at": "2026-11-06T00:00:00Z",
    "body": {
        "summary": "跟进安排",
        "description": "核对原建议",
        "start": {"dateTime": "2026-11-05T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-11-05T09:30:00+08:00", "timeZone": "Asia/Shanghai"},
    },
}
DECISION = {
    "request_id": "stable-request",
    "proposal_ref": PROPOSAL_REF,
    "expected_decision_revision": 0,
}
PAYLOADS = {
    "create_rule": {"request_id": "stable-request", "rule": RULE_BODY},
    "update_rule": {
        "request_id": "stable-request",
        "rule_id": RULE,
        "expected_revision": 1,
        "rule": CALENDAR_RULE,
    },
    "set_status": {
        "request_id": "stable-request",
        "rule_id": RULE,
        "expected_revision": 1,
        "status": "archived",
    },
    "dismiss": DECISION,
    "snooze": {**DECISION, "until": "2026-11-06T00:00:00Z"},
    "accept": {**DECISION, "target": TODAY_TARGET},
    "reconcile": {"request_id": "stable-request", "effect_id": EFFECT},
}
READS = [
    {"operation": "context"},
    {"operation": "rules"},
    {"operation": "list", "status": "snoozed", "limit": 2, "after": CURSOR},
    {"operation": "get", "proposal_id": PROPOSAL},
]
SOURCE = {
    "proposal_ref": PROPOSAL_REF,
    "decision_revision": 0,
    "decision_digest": SHA,
    "presentation_epoch": 0,
    "actor_id": ACTOR,
    "actor_version": 1,
    "occurred_at": "2026-11-05T00:00:00Z",
    "store_instance": "a" * 32,
}


class AdapterPort:
    def __init__(self):
        self.engine = SimpleNamespace(_session=None)
        self.calls, self.pins = [], []
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
                "operation": arguments["operation"],
                "projection_available": True,
                "identity": {
                    "rule_id": RULE,
                    "rule_revision": 1,
                    "proposal_id": PROPOSAL,
                    "decision_revision": 1,
                    "effect_id": EFFECT,
                },
                "view": PRIVATE,
            }
        return {"operation": arguments["operation"], "body": PRIVATE}

    async def model_result(self, operation, result, actor, arguments):
        self.pins.append((operation, copy.deepcopy(result), actor, copy.deepcopy(arguments)))
        if self.pin_failure:
            raise self.pin_failure
        return {"content_transport": "explicit_test_pin", "actor": actor, "operation": operation}


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
    application.state.engine = SimpleNamespace(personal_followups=port)
    return application


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://followup-local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize(
    "arguments",
    [
        *READS,
        {"operation": "requests", "request_id": "original/req:1"},
        {"operation": "list", "actor_id": ACTOR},
    ],
)
async def test_human_reads_exact_envelope_web_authority(app, port, arguments):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200 and not port.pins
    assert port.calls == [(arguments, {"caller_actor": None, "trusted_channel": "web"})]
    no_store(response)


@pytest.mark.parametrize("operation", PAYLOADS)
async def test_human_mutations_one_shared_service(app, port, operation):
    arguments = {"operation": operation, "payload": copy.deepcopy(PAYLOADS[operation])}
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert port.calls == [(arguments, {"caller_actor": None, "trusted_channel": "web"})]
    no_store(response)


async def test_calendar_target_is_original_action_draft_not_item_or_approve(app, port):
    arguments = {"operation": "accept", "payload": {**DECISION, "target": CALENDAR_TARGET}}
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200
    assert port.calls[0][0] == arguments and not port.pins


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"X-API-Key": "wrong"}, 401),
        ({"Authorization": "Bearer " + KEY}, 200),
        ({"Authorization": "Basic " + KEY}, 401),
        ([("X-API-Key", KEY), ("X-API-Key", KEY)], 401),
        ([("Authorization", "Bearer " + KEY), ("Authorization", "Bearer " + KEY)], 401),
        ({"X-API-Key": KEY, "Authorization": "Bearer " + KEY}, 401),
    ],
)
async def test_human_header_only_authentication(app, port, headers, status):
    async with client(app, headers) as http:
        response = await http.get(PREFIX + "/context")
    assert response.status_code == status and bool(port.calls) is (status == 200)
    no_store(response)


async def test_no_configuration_or_query_key_authority(app, port):
    async with client(app) as http:
        query = await http.get(PREFIX + "/context?api_key=" + KEY)
        app.state.config.api.api_keys = []
        missing = await http.get(PREFIX + "/context")
    assert query.status_code == 401 and missing.status_code == 503 and not port.calls
    no_store(query)
    no_store(missing)


@pytest.mark.parametrize(
    "body",
    [
        '{"operation":"context","operation":"rules"}',
        '{"operation":"dismiss","payload":{"request_id":"a","request_id":"b"}}',
        '{"operation":"context","value":NaN}',
        '{"operation":"context","value":Infinity}',
        "[]",
        "null",
        '"context"',
        '{"operation":"context","args":{}}',
        '{"operation":"context","caller_actor":"main"}',
        '{"operation":"context","trusted_channel":"web"}',
        '{"operation":"execute"}',
        '{"operation":"approve"}',
        '{"operation":"list","limit":true}',
        '{"operation":"list","limit":1.0}',
        '{"operation":"list","limit":"1"}',
        '{"operation":"list","limit":0}',
        '{"operation":"list","limit":101}',
        '{"operation":"list","after":"raw-cursor"}',
        '{"operation":"get","proposal_id":"other"}',
        '{"operation":"context"',
    ],
)
async def test_strict_json_schema_before_dispatch(app, port, body):
    async with client(app) as http:
        response = await http.post(
            PREFIX + "/operations", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422 and not port.calls and PRIVATE not in response.text
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
    ids=["text", "wrong-charset", "encoded", "invalid-utf8", "utf16", "oversize"],
)
async def test_media_encoding_and_capacity_bounds(app, port, body, headers, status):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", content=body, headers=headers)
    assert response.status_code == status and not port.calls
    no_store(response)


@pytest.mark.parametrize("query", ["?limit=2", "?actor_id=main", "?x=1&x=2"])
async def test_no_query_overrides(app, port, query):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations" + query, json={"operation": "context"})
    assert response.status_code == 422 and not port.calls
    no_store(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_version", True),
        ("actor_version", 1.0),
        ("actor_version", 0),
        ("include_existing", 1),
        ("max_lateness_minutes", True),
        ("max_lateness_minutes", 0),
        ("max_lateness_minutes", 10081),
        ("freshness_seconds", 59),
        ("freshness_seconds", 604801),
        ("freshness_seconds", 60.0),
        ("status", "archived"),
        ("timezone", "Not/AZone"),
        ("name", " "),
        ("misfire_policy", "run_all"),
    ],
)
def test_rule_strict_bounded_fields(field, value):
    with pytest.raises(PersonalAgentError) as caught:
        validate_followup_arguments(
            {
                "operation": "create_rule",
                "payload": {"request_id": "r", "rule": {**RULE_BODY, field: value}},
            },
            human=True,
        )
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize(
    "trigger",
    [
        {"kind": "deadline", "item_ids": [ITEM, ITEM], "offsets_minutes": [0]},
        {"kind": "deadline", "item_ids": [], "offsets_minutes": []},
        {"kind": "deadline", "item_ids": [], "offsets_minutes": [0, 0]},
        {"kind": "deadline", "item_ids": [], "offsets_minutes": [True]},
        {"kind": "deadline", "item_ids": [], "offsets_minutes": [0.0]},
        {"kind": "deadline", "item_ids": [], "offsets_minutes": [10081]},
        {**CALENDAR_RULE["trigger"], "change_kinds": ["updated", "updated"]},
        {**CALENDAR_RULE["trigger"], "fields": []},
        {**CALENDAR_RULE["trigger"], "fields": ["summary", "summary"]},
        {**CALENDAR_RULE["trigger"], "fields": ["instructions"]},
        {**CALENDAR_RULE["trigger"], "event_id": ""},
        {**CALENDAR_RULE["trigger"], "connection_id": "http://localhost"},
    ],
)
def test_trigger_exact_unique_conditions(trigger):
    with pytest.raises(PersonalAgentError):
        validate_followup_arguments(
            {
                "operation": "create_rule",
                "payload": {"request_id": "r", "rule": {**RULE_BODY, "trigger": trigger}},
            },
            human=True,
        )


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_proposal_literal_revision_never_coerces(value):
    with pytest.raises(PersonalAgentError):
        validate_followup_arguments(
            {
                "operation": "dismiss",
                "payload": {**DECISION, "proposal_ref": {**PROPOSAL_REF, "revision": value}},
            },
            human=True,
        )


@pytest.mark.parametrize(
    "target",
    [
        {**TODAY_TARGET, "date": "2026-02-30"},
        {**TODAY_TARGET, "expected_day_revision": True},
        {**TODAY_TARGET, "planner_id": "main"},
        {**TODAY_TARGET, "source_ref": {}},
        {**TODAY_TARGET, "kind": "item_draft"},
        {**TODAY_TARGET, "kind": "run_now"},
        {**CALENDAR_TARGET, "expires_at": "2026-11-06T00:00:00"},
        {**CALENDAR_TARGET, "expires_at": "2026-11-06T00:00:00+08:00"},
        {**CALENDAR_TARGET, "operation": "approve"},
        {**CALENDAR_TARGET, "body": {**CALENDAR_TARGET["body"], "attendees": [PRIVATE]}},
    ],
)
def test_accept_targets_never_inject_executor_approval_or_source(target):
    with pytest.raises(PersonalAgentError):
        validate_followup_arguments(
            {"operation": "accept", "payload": {**DECISION, "target": target}}, human=True
        )


@pytest.mark.parametrize("operation", [*PAYLOADS, "requests", "propose", "approve", "execute"])
async def test_model_schema_has_no_decision_or_human_entrypoint(port, operation):
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalFollowupsTool(port).execute(operation=operation)
    assert caught.value.code == "invalid_input" and not port.calls


def test_model_schema_exact_four_reads(port):
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    assert {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]} == {
        "context",
        "rules",
        "list",
        "get",
    }
    for name in (
        "actor_id",
        "caller_actor",
        "trusted_channel",
        "accept",
        "snooze",
        "target",
        "payload",
        "proposal_ref",
        "create_rule",
        "reconcile",
    ):
        assert '"' + name + '"' not in json.dumps(schema)
    tool = PersonalFollowupsTool(port)
    assert tool.metadata.read_only and tool.metadata.concurrency_safe
    assert tool.metadata.requires_confirmation is False


def test_model_parser_rejects_duplicates_nonfinite_capacity_and_scope(port):
    tool = PersonalFollowupsTool(port)
    for value in (
        '{"operation":"context","operation":"rules"}',
        '{"operation":"context","value":NaN}',
        " " * (MAX_REQUEST_BYTES + 1),
        {"operation": "list", "actor_id": "main"},
        {"operation": "get", "proposal_id": PROPOSAL, "trusted_channel": PRIVATE},
    ):
        with pytest.raises(ValueError) as caught:
            tool.parse_arguments(value)
        assert PRIVATE not in str(caught.value)
    assert tool.parse_arguments('{"operation":"context"}') == {"operation": "context"}


@pytest.mark.parametrize("arguments", READS)
@pytest.mark.parametrize("actor", ["main", ACTOR])
async def test_model_actual_actor_and_mandatory_pin(port, monkeypatch, arguments, actor):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda engine: SimpleNamespace(id=actor) if actor != "main" else None,
    )
    output = json.loads(await PersonalFollowupsTool(port).execute(**arguments))
    assert port.calls == [(arguments, {"caller_actor": actor, "trusted_channel": None})]
    assert port.pins[0][0] == arguments["operation"] and port.pins[0][2:] == (actor, arguments)
    assert output == {
        "content_transport": "explicit_test_pin",
        "actor": actor,
        "operation": arguments["operation"],
    }
    assert PRIVATE not in json.dumps(output)


@pytest.mark.parametrize(
    "code", ["scope_denied", "source_stale", "source_unavailable", "storage_error"]
)
async def test_pin_failure_no_raw_body_fallback(port, code):
    port.pin_failure = PersonalAgentError(code, PRIVATE)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalFollowupsTool(port).execute(**READS[3])
    assert caught.value.code == code and PRIVATE not in str(caught.value)
    assert len(port.calls) == len(port.pins) == 1


@pytest.mark.parametrize("channel", [None, "agent", "management_web", []])
async def test_dispatcher_never_invents_human_channel(port, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_followup_operation(
            port, {"operation": "accept", "payload": PAYLOADS["accept"]}, trusted_channel=channel
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize("caller,channel", [(ACTOR, "web"), ("foreign", None), ([], None)])
async def test_model_dispatcher_no_actor_or_channel_coercion(port, caller, channel):
    with pytest.raises(PersonalAgentError) as caught:
        await execute_followup_operation(
            port, {"operation": "context"}, caller_actor=caller, trusted_channel=channel
        )
    assert caught.value.code == "scope_denied" and not port.calls


@pytest.mark.parametrize(
    "code,status",
    [
        ("source_stale", 409),
        ("scope_denied", 403),
        ("not_found", 404),
        ("storage_error", 503),
        ("idempotency_conflict", 409),
        ("followup_busy", 409),
        ("proposal_expired", 409),
        ("private_unknown_code", 503),
    ],
)
async def test_declared_errors_sanitized(app, port, code, status):
    port.failure = PersonalAgentError(code, PRIVATE)
    async with client(app) as http:
        response = await http.get(PREFIX + "/context")
    assert response.status_code == status and response.json()["detail"] == public_error(code)
    assert PRIVATE not in response.text
    no_store(response)


@pytest.mark.parametrize(
    "malformed",
    [object(), float("nan"), "x" * MAX_RESPONSE_BYTES],
    ids=["object", "nonfinite", "oversize"],
)
async def test_accepted_mutation_keeps_opaque_receipt_on_projection_failure(app, port, malformed):
    port.body = {
        "accepted": True,
        "request_id": "stable-request",
        "view": malformed,
        "identity": {"proposal_id": PROPOSAL, "effect_id": EFFECT, "decision_revision": 1},
    }
    async with client(app) as http:
        response = await http.post(
            PREFIX + "/operations", json={"operation": "accept", "payload": PAYLOADS["accept"]}
        )
    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "request_id": "stable-request",
        "operation": "accept",
        "identity": port.body["identity"],
        "projection_available": False,
    }
    assert "view" not in response.json() and len(port.calls) == 1


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("POST", PREFIX + "/context", 405),
        ("GET", PREFIX + "/context/", 307),
        ("GET", PREFIX + "/unknown", 404),
    ],
)
async def test_framework_errors_never_cache(app, method, path, status):
    app.add_middleware(AuthMiddleware)
    async with client(app) as http:
        response = await http.request(method, path)
    assert response.status_code == status
    no_store(response)


@pytest.mark.parametrize("status", [401, 429])
async def test_early_middleware_errors_never_cache(app, status):
    app.state.config.api.rate_limit_rpm = 1
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)
    async with client(app) as http:
        if status == 429:
            assert (await http.get(PREFIX + "/context")).status_code == 200
        response = await http.get(PREFIX + "/context", headers={"X-API-Key": "wrong"})
    assert response.status_code == status
    no_store(response)


@pytest.mark.parametrize("operation", PAYLOADS)
@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
async def test_shared_human_command_exact_request_channel(port, operation, channel):
    argument = operation.replace("_", "-") + "-json '" + json.dumps(PAYLOADS[operation]) + "'"
    output = await run_followup_command(
        SimpleNamespace(personal_followups=port), argument, trusted_channel=channel
    )
    assert json.loads(output)["accepted"] is True
    assert port.calls == [
        (
            {"operation": operation, "payload": PAYLOADS[operation]},
            {"caller_actor": None, "trusted_channel": channel},
        )
    ]


def test_cli_reads_and_strict_options():
    assert parse_followup_command("") == {"operation": "context"}
    assert parse_followup_command("rules") == {"operation": "rules"}
    assert parse_followup_command("get " + PROPOSAL) == READS[3]
    assert parse_followup_command("list --status snoozed --limit 2 --after " + CURSOR) == READS[2]
    for argument in (
        "list --limit true",
        "list --limit 1 --limit 2",
        "rules " + PRIVATE,
        "get",
        "approve",
        "context " + PRIVATE,
        'accept-json \'{"request_id":"a","request_id":"b"}\'',
    ):
        with pytest.raises(ValueError) as caught:
            parse_followup_command(argument)
        assert PRIVATE not in str(caught.value)


async def test_cli_exception_marks_failure_without_echo_or_retry(port):
    port.failure = RuntimeError(PRIVATE)
    failed = []
    output = await run_followup_command(
        SimpleNamespace(
            personal_followups=port, mark_manual_command_failed=lambda: failed.append(True)
        ),
        "accept-json '" + json.dumps(PAYLOADS["accept"]) + "'",
        trusted_channel="cli",
    )
    assert output == public_error("followup_unknown") and PRIVATE not in output
    assert failed == [True] and len(port.calls) == 1


@pytest.mark.parametrize("invalid", [False, True])
async def test_actual_web_gateway_stores_opaque_receipt_not_source(tmp_path, port, invalid):
    from naumi_agent.api.routes.commands import router as commands_router
    from naumi_agent.config.settings import MemoryConfig
    from naumi_agent.memory.session import SessionStore
    from naumi_agent.runs.store import ChatRunStore

    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="跟进命令")
    engine = SimpleNamespace(
        session_store=sessions, personal_followups=port, runtime_mode="default"
    )

    async def load_session(identifier):
        return await sessions.load(identifier)

    engine.load_session = load_session
    engine.set_runtime_mode = lambda mode: setattr(engine, "runtime_mode", mode)
    application = FastAPI()
    application.include_router(commands_router)
    application.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    application.state.engine = engine
    application.state.engine_lock = asyncio.Lock()
    application.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    command = (
        "/followups " + PRIVATE
        if invalid
        else ("/followups accept-json '" + json.dumps(PAYLOADS["accept"]) + "'")
    )
    async with client(application) as http:
        response = await http.post(f"/sessions/{session.id}/commands", json={"command": command})
    assert response.status_code == 200
    no_store(response)
    saved = await sessions.load(session.id)
    assert saved.messages[-2]["content"] == "/followups"
    receipt = json.loads(saved.messages[-1]["content"])
    assert set(receipt) == {"type", "operation", "run_id", "status"}
    assert receipt["type"] == "personal_followups_command"
    assert receipt["operation"] == ("invalid" if invalid else "accept")
    assert receipt["status"] == ("failed" if invalid else "completed")
    runs = await application.state.chat_run_store.list_runs(session.id)
    assert runs[0].steps[0].detail == saved.messages[-1]["content"]
    assert PRIVATE not in json.dumps(saved.messages) + str(runs)


class NoticeSource:
    def __init__(self):
        self.projections, self.evidence_calls, self.pages = [], [], []
        self.failure = self.pin_failure = None
        self.resolved = False
        self.page = {"events": [SOURCE], "next_after": None, "partial": False}

    async def notification_page(self, **kwargs):
        self.pages.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.page)

    async def notification_projection(self, ref, reader):
        self.projections.append((copy.deepcopy(ref), reader))
        if self.failure:
            raise self.failure
        return {
            "title": None if self.resolved else PRIVATE,
            "actor_name": "Mira",
            "availability": "resolved" if self.resolved else "available",
            "jump": None if self.resolved else {"kind": "followup", "id": PROPOSAL},
        }

    async def notification_read_evidence(self, body, actor, refs):
        self.evidence_calls.append((copy.deepcopy(body), actor, copy.deepcopy(refs)))
        if self.pin_failure:
            raise self.pin_failure
        return {"read_id": READ, "revision": 1, "digest": SHA, "workspace_key": SHA}


class NoticePort(NotificationAuthorityMixin):
    def __init__(self, source):
        self.workspace_key = SHA
        self.engine = SimpleNamespace(personal_followups=source)
        self.published, self.bound = [], []
        self.session = SimpleNamespace(id="original-session")
        self.event = {**self._followup_event(SOURCE), "revision": 1}
        self.records = {}

        async def read_session(actor, expected=None):
            assert actor == ACTOR and (expected is None or expected is self.session)
            return self.session, 1

        async def publish(**kwargs):
            self.published.append(copy.deepcopy(kwargs))
            return {
                "artifact_id": "art_" + "b" * 32,
                "version": 1,
                "sha256": hashlib.sha256(kwargs["content"]).hexdigest(),
            }

        async def bind(session_id, actor, refs):
            self.bound.append((session_id, actor, copy.deepcopy(refs)))

        artifacts = SimpleNamespace(publish=publish, bind_refs=bind, reference=lambda value: value)
        self.engine.personal_today = SimpleNamespace(
            _read_session=read_session, _artifacts=lambda: artifacts
        )
        self.items = SimpleNamespace(_call=self._items_call)

    @staticmethod
    def _broken():
        return PersonalAgentError("storage_error", "通知资料校验失败")

    def _actor(self, actor):
        if actor not in {None, "main", ACTOR}:
            raise PersonalAgentError("scope_denied", "通知归属不一致")

    async def _items_call(self, *args, **kwargs):
        return {"intents": [], "next_after": None}

    async def _page(self, *args, **kwargs):
        return {"records": [], "next_after": None}

    async def _event(self, ref):
        assert ref == {"event_id": self.event["id"], "event_digest": record_digest(self.event)}
        return self.event

    async def list(self, **kwargs):
        return {
            "entries": [
                {
                    "id": self.event["id"],
                    "event_digest": record_digest(self.event),
                    "availability": "available",
                    "kind": "followup_proposal",
                    "title": PRIVATE,
                    "notice_read": {"human": True},
                }
            ]
        }

    async def _write(self, callback):
        records = self.records

        class Tx:
            def get(self, kind, key, **kwargs):
                return records.get(key)

            def put(self, kind, value, **kwargs):
                records[value["id"]] = {**value, "revision": 1}
                return records[value["id"]]

        return callback(Tx())


@pytest.fixture
def notice():
    source = NoticeSource()
    return NoticePort(source), source


async def test_notification_scan_new_kind_exact_store_identity_and_legacy_cursor(notice):
    service, source = notice
    scan = {"intent_after": None, "watch_after": None, "action_after": None}
    events, watches, progress = await service._scan_sources(scan, {})
    assert len(events) == 1 and not watches
    assert events[0]["kind"] == "followup_proposal" and events[0]["source_ref"] == SOURCE
    assert source.pages == [{"after": None, "limit": 50}]
    assert progress["followup_after"] is None and not progress["partial"]
    assert PRIVATE not in json.dumps(events)
    changed = {**SOURCE, "store_instance": "c" * 32}
    wake = {**SOURCE, "presentation_epoch": 1}
    assert len({service._followup_event(value)["id"] for value in (SOURCE, changed, wake)}) == 3


async def test_empty_filtered_notification_page_retains_raw_continuation(notice):
    service, source = notice
    source.page = {"events": [], "next_after": PROPOSAL, "partial": True}
    events, _, progress = await service._scan_sources(
        {
            "intent_after": None,
            "watch_after": None,
            "action_after": None,
            "followup_after": PROPOSAL,
        },
        {},
    )
    assert events == [] and progress["followup_after"] == PROPOSAL and progress["partial"]
    assert source.pages[0]["after"] == PROPOSAL


async def test_notification_foreign_actor_denied_before_source_call(notice):
    service, source = notice
    with pytest.raises(PersonalAgentError):
        await service._authorize_event(service.event, "main")
    assert not source.projections


async def test_notification_source_loss_during_scan_keeps_partial_without_body(notice):
    service, source = notice
    source.failure = PersonalAgentError("source_unavailable", PRIVATE)
    events, _, progress = await service._scan_sources(
        {"intent_after": None, "watch_after": None, "action_after": None}, {}
    )
    assert events == [] and progress["partial"]
    assert PRIVATE not in json.dumps(progress)


async def test_missing_followup_component_preserves_scan_cursor_and_blocks_projection(notice):
    service, _ = notice
    service.engine.personal_followups = None
    events, _, progress = await service._scan_sources(
        {
            "intent_after": None,
            "watch_after": None,
            "action_after": None,
            "followup_after": PROPOSAL,
        },
        {},
    )
    assert events == [] and progress["partial"] and progress["followup_after"] == PROPOSAL
    with pytest.raises(PersonalAgentError) as caught:
        await service._authorize_event(service.event, ACTOR)
    assert caught.value.code == "source_unavailable"


async def test_normal_followup_not_in_attention_filter_or_dnd_bypass(notice):
    from naumi_agent.personal.notification_models import normalize_preferences

    service, source = notice

    class Tx:
        def page(self, *args, **kwargs):
            return {"records": [service.event], "next_after": service.event["id"]}

    async def read(callback):
        return callback(Tx())

    async def summary(actor):
        return {"unread_count": 1, "attention_count": 0, "partial": False}

    async def scan():
        return {"partial": False}

    async def refresh(events):
        assert events == []
        return []

    service._read, service._summary, service._scan = read, summary, scan
    service._refresh_entry_metadata = refresh
    service._entry_metadata = lambda tx, event, **kwargs: {
        "event": event,
        "notice_read": None,
        "outbox": {},
    }
    result = await PersonalNotificationService.list(service, filter="attention", caller_actor=ACTOR)
    assert result["entries"] == [] and result["next_after"] == service.event["id"]
    assert result["unread_count"] == 1 and result["attention_count"] == 0
    assert not source.projections
    with pytest.raises(PersonalAgentError):
        normalize_preferences(
            {
                "enabled": True,
                "timezone": "UTC",
                "quiet_hours": [],
                "muted_actor_ids": [],
                "debounce_seconds": 0,
                "max_delay_seconds": 60,
                "bypass_kinds": ["followup_proposal"],
                "include_existing": False,
            }
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("store_instance", "path"),
        ("actor_version", 0),
        ("presentation_epoch", True),
        ("decision_digest", PRIVATE),
        ("unknown", PRIVATE),
    ],
)
def test_notification_ref_exact_bounded_no_body_metadata(notice, field, value):
    service, _ = notice
    with pytest.raises(PersonalAgentError) as caught:
        service._followup_event({**SOURCE, field: value})
    assert caught.value.code == "storage_error" and PRIVATE not in str(caught.value)


async def test_notification_source_loss_clears_title_jump_normal_priority(notice):
    service, source = notice
    metadata = {"event": service.event, "notice_read": None, "outbox": {}}
    view = await PersonalNotificationService._project_entry(service, service.event, ACTOR, metadata)
    assert view["priority"] == "normal" and view["title"] == PRIVATE
    source.failure = PersonalAgentError("source_unavailable", PRIVATE)
    view = await PersonalNotificationService._project_entry(service, service.event, ACTOR, metadata)
    assert view["availability"] == "source_unavailable"
    assert view["title"] is None and view["actor_name"] is None and view["jump"] is None
    source.failure = None
    source.resolved = True
    resolved = await service._authorize_event(service.event, ACTOR)
    assert resolved["availability"] == "resolved" and resolved["title"] is None


@pytest.mark.parametrize(
    "projection",
    [
        {"title": PRIVATE, "actor_name": "Mira", "availability": "resolved", "jump": None},
        {
            "title": None,
            "actor_name": "Mira",
            "availability": "resolved",
            "jump": {"kind": "followup", "id": PROPOSAL},
        },
        {
            "title": PRIVATE,
            "actor_name": "Mira",
            "availability": "available",
            "jump": {"kind": "calendar_action", "id": EFFECT},
        },
    ],
)
async def test_notification_rejects_detached_resolved_body_or_wrong_target(notice, projection):
    service, source = notice

    async def project(*args):
        return projection

    source.notification_projection = project
    with pytest.raises(PersonalAgentError) as caught:
        await service._authorize_event(service.event, ACTOR)
    assert caught.value.code == "storage_error" and PRIVATE not in str(caught.value)


async def test_notification_model_pin_includes_strict_followup_evidence_for_v5(notice):
    service, source = notice
    body = await service.model_result("list", {"forged": PRIVATE}, ACTOR, {"operation": "list"})
    assert len(service.published) == len(source.evidence_calls) == 1
    published = service.published[0]
    assert json.loads(published["content"]) == body
    assert "notice_read" not in body["entries"][0]
    assert published["evidence"]["followup_read_refs"] == [
        {"read_id": READ, "revision": 1, "digest": SHA, "workspace_key": SHA}
    ]
    assert source.evidence_calls == [(body, ACTOR, [SOURCE])]
    assert service.bound[0][:2] == ("original-session", ACTOR)


async def test_notification_followup_pin_failure_never_publishes_raw_material(notice):
    service, source = notice
    source.pin_failure = PersonalAgentError("source_unavailable", "失权")
    with pytest.raises(PersonalAgentError):
        await service.model_result("list", {}, ACTOR, {"operation": "list"})
    assert not service.published and not service.bound and len(source.evidence_calls) == 1


@pytest.mark.parametrize(
    "proof",
    [
        {"read_id": READ, "revision": True, "digest": SHA, "workspace_key": SHA},
        {"read_id": READ, "revision": 1, "digest": PRIVATE, "workspace_key": SHA},
        {"read_id": READ, "revision": 1, "digest": SHA, "workspace_key": "a" * 64},
    ],
)
async def test_notification_malformed_read_proof_never_published(notice, proof):
    service, source = notice

    async def evidence(*args):
        return proof

    source.notification_read_evidence = evidence
    with pytest.raises(PersonalAgentError) as caught:
        await service.model_result("list", {}, ACTOR, {"operation": "list"})
    assert caught.value.code == "storage_error" and not service.published and not service.bound


def test_followup_tool_registration_permissions_private_roots(port, tmp_path):
    from naumi_agent.api.routes.commands import command_index, validate_command
    from naumi_agent.personal.artifact_boundary import is_protected
    from naumi_agent.personal.service import ASSIGNABLE_TOOL_NAMES
    from naumi_agent.safety.permissions import PermissionChecker, PermissionMode

    assert "personal_followups" in ASSIGNABLE_TOOL_NAMES
    assert any(entry.command == "/followups" for entry in command_index())
    assert validate_command("/followups context") == "/followups context"
    for mode in (PermissionMode.PERMISSIVE, PermissionMode.MODERATE, PermissionMode.STRICT):
        for name in (
            "personal_followups",
            "default.personal_followups",
            "default__personal_followups",
        ):
            decision = PermissionChecker(mode).check(
                name, {"operation": "context"}, PersonalFollowupsTool(port)
            )
            assert (
                decision.allowed
                and not decision.requires_confirmation
                and not decision.allow_session_grant
            )
    workspace = tmp_path / "workspace"
    engine = SimpleNamespace(
        workspace_root=workspace,
        personal_agents=SimpleNamespace(store=SimpleNamespace(base_dir=workspace / ".naumi")),
        personal_followups=SimpleNamespace(
            boundary=tmp_path / "external-pin",
            store=SimpleNamespace(base_dir=tmp_path / "followups"),
        ),
        config=SimpleNamespace(
            memory=SimpleNamespace(
                session_db_path=str(workspace / "sessions.db"), vector_db_path=""
            )
        ),
    )
    assert is_protected(engine, tmp_path / "external-pin")
    assert is_protected(engine, tmp_path / "followups" / "personal_followups.db")
    assert not is_protected(engine, workspace / "public.txt")
