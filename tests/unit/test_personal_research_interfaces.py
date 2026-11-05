"""Actual strict adapters preserve one research operation and trusted caller/channel."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware
from naumi_agent.api.routes.commands import _CommandEngine, validate_command
from naumi_agent.api.routes.research import router
from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_commands import parse_research_command, run_research_command
from naumi_agent.personal.research_tools import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PersonalResearchTool,
    execute_research_operation,
    request_schema,
    serializable_result,
    validate_research_arguments,
)
from naumi_agent.tools.base import ToolExecutionError
from naumi_agent.ui.command_index import build_terminal_command_index

PREFIX = "/api/v1/personal/research"
KEY = "research-interface-only-key"
NOTEBOOK = "rq_" + "1" * 32
SOURCE = "rs_" + "2" * 32
ACTOR = "pa_" + "3" * 32
DIGEST = "4" * 64
PRIVATE = "PRIVATE_RESEARCH_SOURCE_NOT_FOR_HISTORY_OR_ERRORS"
CREATE = {
    "request_id": "original-create",
    "title": "研究原问题",
    "question": "核对原资料",
    "domain": "general",
}
ARTIFACT = {
    "artifact_id": "art_" + "5" * 32,
    "version": 1,
    "sha256": DIGEST,
    "grant_id": "",
    "grant_revision": 0,
}
ARGUMENTS = [
    {"operation": "context"},
    {"operation": "list", "limit": 2, "after": NOTEBOOK},
    {"operation": "get", "id": NOTEBOOK, "revision": 1},
    {"operation": "source", "id": SOURCE, "revision": 1},
    {"operation": "history", "id": NOTEBOOK, "limit": 2, "after_revision": 3},
    {"operation": "request", "request_id": "original-create"},
    {"operation": "create", **CREATE},
    {
        "operation": "revise",
        "id": NOTEBOOK,
        **CREATE,
        "expected_revision": 1,
        "claims": [{"id": "unknown", "text": "原结论尚未核实", "kind": "unknown"}],
        "conclusion": {"stance": "undecided"},
    },
    {
        "operation": "search",
        "id": NOTEBOOK,
        "request_id": "original-search",
        "query": "公开资料",
        "max_results": 2,
    },
    {
        "operation": "capture",
        "id": NOTEBOOK,
        "request_id": "original-capture",
        "url": "https://docs.example/source",
        "max_age_days": 30,
    },
    {
        "operation": "import",
        "id": NOTEBOOK,
        "request_id": "original-import",
        "artifact_ref": ARTIFACT,
        "max_age_days": 30,
    },
    {
        "operation": "recheck",
        "source_id": SOURCE,
        "request_id": "original-recheck",
        "expected_revision": 1,
    },
    {
        "operation": "review",
        "id": NOTEBOOK,
        "request_id": "original-review",
        "expected_revision": 1,
    },
    {
        "operation": "publish",
        "id": NOTEBOOK,
        "request_id": "original-publish",
        "expected_revision": 1,
        "expected_review_digest": DIGEST,
    },
]


class AdapterPort:
    def __init__(self):
        self.engine = SimpleNamespace(_session=None)
        self.calls, self.pins = [], []
        self.failure = self.pin_failure = None
        self.body = {"body": PRIVATE}

    async def execute_operation(self, arguments, **kwargs):
        self.calls.append((copy.deepcopy(arguments), copy.deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.body)

    async def model_result(self, arguments, result, actor):
        self.pins.append((copy.deepcopy(arguments), copy.deepcopy(result), actor))
        if self.pin_failure:
            raise self.pin_failure
        return {
            "content_transport": "test_exact_pin",
            "actor": actor,
            "operation": arguments["operation"],
        }


@pytest.fixture
def port():
    return AdapterPort()


@pytest.fixture
def app(port):
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.state.engine = SimpleNamespace(personal_research=port)
    application.state.config = SimpleNamespace(
        api=SimpleNamespace(api_keys=[KEY], rate_limit_rpm=60)
    )
    return application


def client(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://research-local",
        headers={"X-API-Key": KEY} if headers is None else headers,
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("arguments", ARGUMENTS)
def test_all_operations_share_strict_flat_schema_and_domain_models(arguments):
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(arguments)
    normalized = validate_research_arguments(arguments)
    assert normalized["operation"] == arguments["operation"]
    for field in ("actor_id", "caller_actor", "trusted_channel", "human", "verified", "evidence"):
        forged = {**arguments, field: PRIVATE}
        assert not Draft202012Validator(schema).is_valid(forged)
        with pytest.raises(PersonalAgentError) as error:
            validate_research_arguments(forged)
        assert error.value.code == "invalid_input" and PRIVATE not in str(error.value)


@pytest.mark.parametrize("arguments", ARGUMENTS)
@pytest.mark.parametrize("actor", ["main", ACTOR])
async def test_model_uses_bound_actor_and_requires_exact_result_pin(
    port, monkeypatch, arguments, actor
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda _engine: None if actor == "main" else SimpleNamespace(id=actor),
    )
    normalized = validate_research_arguments(arguments)
    result = json.loads(await PersonalResearchTool(port).execute(**arguments))
    assert port.calls == [(normalized, {"caller_actor": actor, "trusted_channel": None})]
    assert port.pins == [(normalized, port.body, actor)]
    assert result == {
        "content_transport": "test_exact_pin",
        "actor": actor,
        "operation": arguments["operation"],
    }
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize(
    "code", ["scope_denied", "source_stale", "source_unavailable", "storage_error"]
)
async def test_pin_failure_does_not_return_raw_service_body(port, code):
    port.pin_failure = PersonalAgentError(code, PRIVATE)
    with pytest.raises(ToolExecutionError) as error:
        await PersonalResearchTool(port).execute(operation="get", id=NOTEBOOK)
    assert error.value.code == code and PRIVATE not in str(error.value)
    assert len(port.calls) == len(port.pins) == 1


@pytest.mark.parametrize(
    "actor,channel", [(None, None), (None, "agent"), (None, []), ("main", "web"), ("forged", None)]
)
async def test_shared_executor_does_not_invent_actor_or_manual_channel(port, actor, channel):
    with pytest.raises(PersonalAgentError) as error:
        await execute_research_operation(
            port, {"operation": "context"}, caller_actor=actor, trusted_channel=channel
        )
    assert error.value.code == "scope_denied" and port.calls == []


def test_tool_rejects_duplicate_keys_unknown_branches_and_invalid_nested_authority(port):
    tool = PersonalResearchTool(port)
    for raw in (
        '{"operation":"context","operation":"list"}',
        '{"operation":"approve","actor_id":"main"}',
        {
            "operation": "import",
            "id": NOTEBOOK,
            "request_id": "x",
            "artifact_ref": {**ARTIFACT, "grant_revision": 1},
        },
        {"operation": "list", "limit": True},
        {
            "operation": "revise",
            **CREATE,
            "id": NOTEBOOK,
            "expected_revision": 1,
            "claims": [{"id": "a", "kind": "fact", "text": "x", "verified": True}],
            "conclusion": {},
        },
    ):
        with pytest.raises(ValueError):
            tool.parse_arguments(raw)
    assert tool.parse_arguments('{"operation":"context"}') == {"operation": "context"}
    assert tool.metadata.requires_confirmation is False


def test_response_capacity_and_nonfinite_values_are_not_serialized():
    with pytest.raises(PersonalAgentError) as oversized:
        serializable_result({"body": "x" * MAX_RESPONSE_BYTES})
    assert oversized.value.code == "input_limit"
    with pytest.raises(PersonalAgentError):
        serializable_result({"value": float("nan")})


@pytest.mark.parametrize("arguments", ARGUMENTS)
async def test_authenticated_flat_post_uses_shared_operation_once(app, port, arguments):
    body = {key: value for key, value in arguments.items() if key != "operation"}
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations/" + arguments["operation"], json=body)
    assert response.status_code == 200
    no_store(response)
    assert port.calls == [
        (validate_research_arguments(arguments), {"caller_actor": None, "trusted_channel": "web"})
    ]
    assert port.pins == []


@pytest.mark.parametrize("arguments", ARGUMENTS)
async def test_authenticated_envelope_post_uses_shared_operation_once(app, port, arguments):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", json=arguments)
    assert response.status_code == 200
    no_store(response)
    assert port.calls == [
        (validate_research_arguments(arguments), {"caller_actor": None, "trusted_channel": "web"})
    ]
    assert port.pins == []


@pytest.mark.parametrize(
    "path,arguments",
    [
        ("/context", {"operation": "context"}),
        ("?limit=2&after=" + NOTEBOOK, ARGUMENTS[1]),
        ("/" + NOTEBOOK + "?revision=1", ARGUMENTS[2]),
        ("/sources/" + SOURCE + "?revision=1", ARGUMENTS[3]),
        ("/" + NOTEBOOK + "/history?limit=2&after_revision=3", ARGUMENTS[4]),
        ("/requests/original-create", ARGUMENTS[5]),
    ],
)
async def test_get_routes_do_not_accept_operation_or_identity_queries(app, port, path, arguments):
    async with client(app) as http:
        response = await http.get(PREFIX + path)
    assert response.status_code == 200
    no_store(response)
    assert port.calls[0][0] == validate_research_arguments(arguments)


@pytest.mark.parametrize(
    "headers,suffix,status",
    [
        ({}, "/context", 401),
        ({"X-API-Key": "wrong"}, "/context", 401),
        ({"X-API-Key": KEY}, "/context?api_key=" + KEY, 401),
        ({"X-API-Key": KEY}, "?limit=1&limit=2", 422),
        ({"X-API-Key": KEY}, "/context?actor_id=main", 422),
        ({"X-API-Key": KEY}, "?limit=true", 422),
        ({"X-API-Key": KEY}, "/" + NOTEBOOK + "?revision=0", 422),
    ],
)
async def test_authentication_and_query_rejection_precede_domain_dispatch(
    app, port, headers, suffix, status
):
    async with client(app, headers) as http:
        response = await http.get(PREFIX + suffix)
    assert response.status_code == status
    no_store(response)
    assert port.calls == [] and PRIVATE not in response.text


@pytest.mark.parametrize(
    "body,headers,status",
    [
        ('{"operation":"context","operation":"list"}', {"content-type": "application/json"}, 422),
        ('{"operation":"context"}', {"content-type": "text/plain"}, 415),
        (
            '{"operation":"context"}',
            {"content-type": "application/json", "content-encoding": "gzip"},
            415,
        ),
        ("x" * (MAX_REQUEST_BYTES + 1), {"content-type": "application/json"}, 413),
    ],
    ids=["duplicate-json", "wrong-media", "content-encoding", "oversized-body"],
)
async def test_post_rejects_ambiguous_encoded_or_oversized_requests(
    app, port, body, headers, status
):
    async with client(app) as http:
        response = await http.post(PREFIX + "/operations", content=body, headers=headers)
    assert response.status_code == status
    no_store(response)
    assert port.calls == []


async def test_path_operation_cannot_be_overridden_in_body(app, port):
    async with client(app) as http:
        response = await http.post(
            PREFIX + "/operations/create", json={"operation": "capture", **CREATE}
        )
    assert response.status_code == 422 and port.calls == []
    no_store(response)


@pytest.mark.parametrize("status", [401, 429])
async def test_research_middleware_errors_are_never_cacheable(app, status):
    app.state.config.api.rate_limit_rpm = 1
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)
    async with client(app) as http:
        if status == 429:
            assert (await http.get(PREFIX + "/context")).status_code == 200
        response = await http.get(PREFIX + "/context", headers={"X-API-Key": "wrong"})
    assert response.status_code == status
    no_store(response)


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
async def test_json_command_reuses_flat_arguments_and_preserves_apostrophes(port, channel):
    body = {**CREATE, "title": "O'Reilly 原资料"}
    command = "create-json " + shlex.quote(json.dumps(body, ensure_ascii=False))
    expected = validate_research_arguments({"operation": "create", **body})
    assert parse_research_command(command) == expected
    output = await run_research_command(
        SimpleNamespace(personal_research=port), command, trusted_channel=channel
    )
    assert json.loads(output) == port.body
    assert port.calls == [(expected, {"caller_actor": None, "trusted_channel": channel})]


def test_cli_reads_strict_options_and_all_terminal_indexes():
    assert parse_research_command("") == {"operation": "context"}
    assert parse_research_command("get " + NOTEBOOK + " --revision 2") == {
        "operation": "get",
        "id": NOTEBOOK,
        "revision": 2,
    }
    assert parse_research_command("list --limit 2 --after " + NOTEBOOK) == ARGUMENTS[1]
    assert validate_command("/research context") == "/research context"
    assert "/research" in {entry.name for entry in COMMANDS_META}
    for surface in ("tui", "new_ui"):
        assert "/research" in {entry.command for entry in build_terminal_command_index(surface)}
    for command in (
        "list --limit true",
        "context " + PRIVATE,
        "approve",
        "source " + NOTEBOOK,
        "list --limit 1 --limit 2",
        'create-json \'{"request_id":"a","request_id":"b"}\'',
    ):
        with pytest.raises(ValueError) as error:
            parse_research_command(command)
        assert PRIVATE not in str(error.value)


async def test_actual_cli_help_includes_research():
    output = await execute_slash_command(SimpleNamespace(), "/help")
    assert "/research" in output


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
async def test_actual_shared_slash_router_selects_real_manual_channel(port, channel):
    engine = SimpleNamespace(personal_research=port)
    if channel == "web":

        async def callback(*_args):
            return None

        engine = _CommandEngine(engine, callback)
    frontend = (
        type("TuiFrontend", (), {"__module__": "naumi_agent.tui.app"})()
        if channel == "tui"
        else None
    )
    result = await execute_slash_command(engine, "/research context", frontend=frontend)
    assert json.loads(result) == port.body
    assert port.calls == [
        ({"operation": "context"}, {"caller_actor": None, "trusted_channel": channel})
    ]


@pytest.mark.parametrize("invalid", [False, True])
async def test_actual_web_command_gateway_keeps_only_opaque_receipt(tmp_path, port, invalid):
    from naumi_agent.api.routes.commands import router as commands_router
    from naumi_agent.config.settings import MemoryConfig
    from naumi_agent.memory.session import SessionStore
    from naumi_agent.runs.store import ChatRunStore

    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="研究命令")
    engine = SimpleNamespace(session_store=sessions, personal_research=port, runtime_mode="default")

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
        "/research " + PRIVATE
        if invalid
        else "/research create-json "
        + shlex.quote(
            json.dumps({**CREATE, "question": PRIVATE}),
        )
    )
    async with client(application) as http:
        response = await http.post(f"/sessions/{session.id}/commands", json={"command": command})
    assert response.status_code == 200
    no_store(response)
    saved = await sessions.load(session.id)
    assert saved.messages[-2]["content"] == "/research"
    receipt = json.loads(saved.messages[-1]["content"])
    assert set(receipt) == {"type", "operation", "run_id", "status"}
    assert receipt["type"] == "personal_research_command"
    assert receipt["operation"] == ("invalid" if invalid else "create")
    assert receipt["status"] == ("failed" if invalid else "completed")
    runs = await application.state.chat_run_store.list_runs(session.id)
    assert runs[0].steps[0].detail == saved.messages[-1]["content"]
    assert PRIVATE not in json.dumps(saved.messages) + str(runs)
