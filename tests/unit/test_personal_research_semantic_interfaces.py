"""Independent-review adapters preserve strict inputs and trusted entry identities.

Domain execution is intentionally a bounded port here. Real journals, grants,
AgentJobs and ChatRuns are covered by the separate integrated research tests.
"""

from __future__ import annotations

import copy
import json
import shlex
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware
from naumi_agent.api.routes.commands import _CommandEngine
from naumi_agent.api.routes.research import router
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_commands import parse_research_command
from naumi_agent.personal.research_semantic_models import SEMANTIC_OPERATIONS
from naumi_agent.personal.research_tools import (
    PersonalResearchTool,
    execute_research_operation,
    request_schema,
    validate_research_arguments,
)
from naumi_agent.tools.base import ToolExecutionError

PREFIX = "/api/v1/personal/research"
KEY = "semantic-interface-only"
NOTEBOOK = "rq_" + "1" * 32
REVIEW = "rsv_" + "2" * 32
SOURCE = "rs_" + "3" * 32
REVIEWER = "pa_" + "4" * 32
SHA = "5" * 64
SESSION = "original-author-session"
REQUEST = "original-independent-review"
PRIVATE = "PRIVATE_SEMANTIC_INPUT_NEVER_ECHOED_IN_ERRORS"
PREVIEW = {
    "operation": "semantic_preview",
    "id": NOTEBOOK,
    "source_session_id": SESSION,
    "reviewer_agent_id": REVIEWER,
}
START = {
    **PREVIEW,
    "operation": "semantic_start",
    "expected_reviewer_version": 2,
    "expected_revision": 3,
    "expected_input_digest": SHA,
    "request_id": REQUEST,
}
QUOTE = {
    "source_id": SOURCE,
    "source_revision": 1,
    "body_sha256": SHA,
    "quote": "收入 USD 100",
    "start": 1,
}
VERDICT = {
    "claim_id": "income",
    "verdict": "supported",
    "reason": "该原文直接写明收入。",
    "evidence": [QUOTE],
}
SUBMIT = {
    "operation": "semantic_submit",
    "id": REVIEW,
    "request_id": "original-submission",
    "verdicts": [VERDICT],
    "summary": "只复核提供的确切版本来源。",
}
ARGUMENTS = [
    PREVIEW,
    START,
    {"operation": "semantic_get", "id": REVIEW},
    {"operation": "semantic_list", "id": NOTEBOOK},
    {"operation": "semantic_request", "request_id": REQUEST},
    {"operation": "semantic_reconcile", "id": REVIEW, "request_id": "original-reconcile"},
    {"operation": "semantic_cancel", "id": REVIEW, "request_id": "original-cancel"},
    {"operation": "semantic_sources", "id": REVIEW, "offset": 0, "limit": 10000},
    {
        "operation": "semantic_source",
        "id": REVIEW,
        "source_id": SOURCE,
        "source_revision": 1,
        "request_id": "original-source-read",
        "offset": 0,
        "limit": 20000,
    },
    SUBMIT,
]


class AdapterPort:
    def __init__(self):
        self.engine = SimpleNamespace(_session=None)
        self.calls, self.pins = [], []
        self.failure = self.pin_failure = None
        self.result = {"accepted": True}

    async def execute_operation(self, arguments, **authority):
        self.calls.append((copy.deepcopy(arguments), copy.deepcopy(authority)))
        if self.failure is not None:
            raise self.failure
        return copy.deepcopy(self.result)

    async def model_result(self, arguments, result, actor):
        self.pins.append((copy.deepcopy(arguments), copy.deepcopy(result), actor))
        if self.pin_failure is not None:
            raise self.pin_failure
        return {"transport": "exact-review-port", "actor": actor}


@pytest.fixture
def port():
    return AdapterPort()


@pytest.fixture
def app(port):
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.state.engine = SimpleNamespace(personal_research=port)
    application.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=[KEY]))
    application.add_middleware(AuthMiddleware)
    return application


def client(app, *, authorized=True):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://semantic-local",
        headers={"Authorization": "Bearer " + KEY} if authorized else {},
    )


def no_store(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("arguments", ARGUMENTS)
def test_all_semantic_operations_share_the_strict_flat_schema(arguments):
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(arguments)
    assert set(SEMANTIC_OPERATIONS) == {row["operation"] for row in ARGUMENTS}
    assert validate_research_arguments(arguments, human=True) == arguments
    assert validate_research_arguments(arguments) == arguments
    for field in (
        "actor_id",
        "caller_actor",
        "trusted_channel",
        "human",
        "approved",
        "peer_read_refs",
        "packet_ref",
        "skip_acl",
        "run_id",
        "result",
    ):
        forged = {**arguments, field: PRIVATE}
        assert not validator.is_valid(forged)
        with pytest.raises(PersonalAgentError) as error:
            validate_research_arguments(forged)
        assert error.value.code == "invalid_input" and PRIVATE not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", REVIEW),
        ("source_session_id", ""),
        ("source_session_id", "wrong session id"),
        ("reviewer_agent_id", "main"),
        ("reviewer_agent_id", "pa_" + "F" * 32),
        ("expected_reviewer_version", True),
        ("expected_reviewer_version", "2"),
        ("expected_reviewer_version", 0),
        ("expected_revision", False),
        ("expected_revision", 1.0),
        ("expected_revision", -1),
        ("expected_revision", 2**53),
        ("expected_input_digest", "5" * 63),
        ("expected_input_digest", "G" * 64),
        ("request_id", ""),
        ("request_id", "a" * 129),
    ],
)
def test_start_rejects_wrong_identity_types_and_cas_bounds(field, value):
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments({**START, field: value}, human=True)
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", True),
        ("source_revision", "1"),
        ("offset", True),
        ("offset", -1),
        ("offset", 2**31),
        ("limit", 0),
        ("limit", 20001),
        ("limit", "20000"),
        ("id", NOTEBOOK),
        ("source_id", REVIEW),
    ],
)
def test_source_pagination_preserves_strict_codepoint_bounds(field, value):
    with pytest.raises(PersonalAgentError):
        validate_research_arguments({**ARGUMENTS[-2], field: value})


def test_source_pagination_defaults_are_explicit_and_stable():
    arguments = {
        key: value for key, value in ARGUMENTS[-2].items() if key not in {"offset", "limit"}
    }
    assert validate_research_arguments(arguments) == ARGUMENTS[-2]


@pytest.mark.parametrize("verdict", ["supported", "contradicted"])
def test_supported_and_contradicted_verdicts_require_real_quote_inputs(verdict):
    body = {**SUBMIT, "verdicts": [{**VERDICT, "verdict": verdict, "evidence": []}]}
    with pytest.raises(PersonalAgentError):
        validate_research_arguments(body)
    body["verdicts"][0]["evidence"] = [QUOTE]
    assert validate_research_arguments(body) == body


def test_insufficient_is_a_real_result_and_does_not_invent_evidence():
    body = {**SUBMIT, "verdicts": [{**VERDICT, "verdict": "insufficient", "evidence": []}]}
    assert validate_research_arguments(body) == body


@pytest.mark.parametrize(
    "change",
    [
        {"summary": " \n\t"},
        {"verdicts": []},
        {"verdicts": [VERDICT, VERDICT]},
        {"verdicts": [{**VERDICT, "reason": "\t "}]},
        {"verdicts": [{**VERDICT, "verified": True}]},
        {"verdicts": [{**VERDICT, "verdict": "independently_verified"}]},
        {"verdicts": [{**VERDICT, "evidence": [QUOTE, QUOTE]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "quote": "\n "}]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "start": True}]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "start": -1}]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "end_byte": 20}]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "body_sha256": "0" * 63}]}]},
        {"verdicts": [{**VERDICT, "evidence": [{**QUOTE, "source_revision": "1"}]}]},
    ],
)
def test_submission_rejects_duplicate_blank_and_forged_derived_fields(change):
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments({**SUBMIT, **change})
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("arguments", ARGUMENTS)
@pytest.mark.parametrize("actor", ["main", REVIEWER])
async def test_tool_uses_actual_actor_and_unchanged_shared_service_contract(
    port, monkeypatch, arguments, actor
):
    from naumi_agent.personal import runtime

    monkeypatch.setattr(
        runtime,
        "require_current_agent",
        lambda _engine: None if actor == "main" else SimpleNamespace(id=actor),
    )
    output = json.loads(await PersonalResearchTool(port).execute(**arguments))
    assert output == {"transport": "exact-review-port", "actor": actor}
    assert port.calls == [(arguments, {"caller_actor": actor, "trusted_channel": None})]
    assert port.pins == [(arguments, port.result, actor)]


@pytest.mark.parametrize("code", ["scope_denied", "source_stale", "storage_error"])
async def test_tool_domain_or_pin_failure_never_returns_raw_body(port, code):
    port.result = {"summary": PRIVATE}
    port.pin_failure = PersonalAgentError(code, PRIVATE)
    with pytest.raises(ToolExecutionError) as error:
        await PersonalResearchTool(port).execute(operation="semantic_get", id=REVIEW)
    assert error.value.code == code and PRIVATE not in str(error.value)
    assert len(port.calls) == len(port.pins) == 1


@pytest.mark.parametrize(
    "actor,channel",
    [(None, None), (None, "agent"), (None, True), ("main", "web"), ("forged", None)],
)
async def test_shared_executor_rejects_untrusted_or_mixed_authority(port, actor, channel):
    with pytest.raises(PersonalAgentError) as error:
        await execute_research_operation(port, START, caller_actor=actor, trusted_channel=channel)
    assert error.value.code == "scope_denied" and not port.calls


CLI_CASES = [
    ("semantic-get " + REVIEW, ARGUMENTS[2]),
    ("semantic-list " + NOTEBOOK, ARGUMENTS[3]),
    ("semantic-request " + REQUEST, ARGUMENTS[4]),
    *[
        (
            value["operation"].replace("_", "-")
            + "-json "
            + shlex.quote(json.dumps({k: v for k, v in value.items() if k != "operation"})),
            value,
        )
        for value in (PREVIEW, START, ARGUMENTS[5], ARGUMENTS[6])
    ],
]


@pytest.mark.parametrize("command,expected", CLI_CASES)
def test_cli_and_tui_parse_the_original_exact_operation(command, expected):
    assert parse_research_command(command) == expected


@pytest.mark.parametrize("channel", ["cli", "tui", "web"])
@pytest.mark.parametrize("command,expected", CLI_CASES)
async def test_actual_slash_router_supplies_each_real_manual_channel(
    port, channel, command, expected
):
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
    output = await execute_slash_command(engine, "/research " + command, frontend=frontend)
    assert json.loads(output) == port.result
    assert port.calls == [(expected, {"caller_actor": None, "trusted_channel": channel})]


@pytest.mark.parametrize(
    "command",
    [
        "semantic-get " + NOTEBOOK,
        "semantic-list " + REVIEW,
        "semantic-request",
        "semantic-get " + REVIEW + " --skip-acl true",
        'semantic-start-json \'{"request_id":"a","request_id":"b"}\'',
        "semantic-submit-json {}",
    ],
)
def test_manual_parser_does_not_invent_identity_or_accept_execution_only_commands(command):
    with pytest.raises(ValueError):
        parse_research_command(command)


HTTP_CASES = [
    (
        "GET",
        "/"
        + NOTEBOOK
        + "/semantic-context?source_session_id="
        + SESSION
        + "&reviewer_agent_id="
        + REVIEWER,
        None,
        PREVIEW,
    ),
    ("GET", "/" + NOTEBOOK + "/semantic-reviews", None, ARGUMENTS[3]),
    ("GET", "/semantic-reviews/" + REVIEW, None, ARGUMENTS[2]),
    ("GET", "/semantic-requests/" + REQUEST, None, ARGUMENTS[4]),
    *[
        (
            "POST",
            "/operations/" + arguments["operation"],
            {key: value for key, value in arguments.items() if key != "operation"},
            arguments,
        )
        for arguments in (START, ARGUMENTS[5], ARGUMENTS[6])
    ],
]


@pytest.mark.parametrize("method,path,body,expected", HTTP_CASES)
async def test_four_get_and_three_post_routes_are_authenticated_no_store_and_manual(
    app, port, method, path, body, expected
):
    async with client(app, authorized=False) as http:
        denied = await http.request(method, PREFIX + path, json=body)
    assert denied.status_code == 401
    assert denied.headers["cache-control"] == "no-store" and not port.calls
    async with client(app) as http:
        response = await http.request(method, PREFIX + path, json=body)
    assert response.status_code == 200 and response.json() == port.result
    no_store(response)
    assert port.calls == [(expected, {"caller_actor": None, "trusted_channel": "web"})]


@pytest.mark.parametrize(
    "path",
    [
        "/"
        + NOTEBOOK
        + "/semantic-context?source_session_id="
        + SESSION
        + "&source_session_id="
        + SESSION
        + "&reviewer_agent_id="
        + REVIEWER,
        "/"
        + NOTEBOOK
        + "/semantic-context?source_session_id="
        + SESSION
        + "&reviewer_agent_id="
        + REVIEWER
        + "&approved=true",
        "/" + NOTEBOOK + "/semantic-context?source_session_id=" + SESSION,
        "/" + NOTEBOOK + "/semantic-reviews?actor_id=main",
        "/semantic-reviews/" + REVIEW + "?skip_acl=true",
        "/semantic-requests/" + REQUEST + "?request_id=changed",
    ],
)
async def test_get_rejects_unknown_duplicate_and_missing_query_before_dispatch(app, port, path):
    async with client(app) as http:
        response = await http.get(PREFIX + path)
    assert response.status_code == 422
    no_store(response)
    assert not port.calls


@pytest.mark.parametrize(
    "path,body,content_type,status",
    [
        ("/operations/semantic_start", {**START}, "application/json", 422),
        ("/operations/semantic_start", {"id": NOTEBOOK}, "application/json", 422),
        ("/operations/semantic_approve", {}, "application/json", 422),
        ("/operations/semantic_start", {}, "text/plain", 415),
        (
            "/operations/semantic_cancel",
            {"id": REVIEW, "request_id": REQUEST, "approved": True},
            "application/json",
            422,
        ),
    ],
)
async def test_post_rejects_unknown_operation_authority_and_wrong_media(
    app, port, path, body, content_type, status
):
    async with client(app) as http:
        response = await http.post(
            PREFIX + path, content=json.dumps(body), headers={"content-type": content_type}
        )
    assert response.status_code == status
    no_store(response)
    assert not port.calls


@pytest.mark.parametrize(
    "code,status",
    [("scope_denied", 403), ("not_found", 404), ("source_stale", 409), ("storage_error", 503)],
)
async def test_http_errors_preserve_domain_status_but_not_private_details(app, port, code, status):
    port.failure = PersonalAgentError(code, PRIVATE)
    async with client(app) as http:
        response = await http.get(PREFIX + "/semantic-reviews/" + REVIEW)
    assert response.status_code == status and response.json()["code"] == code
    assert PRIVATE not in response.text
    no_store(response)


async def test_semantic_http_exception_returns_bounded_unknown_not_exception_body(app, port):
    port.failure = RuntimeError(PRIVATE)
    async with client(app) as http:
        response = await http.get(PREFIX + "/semantic-reviews/" + REVIEW)
    assert response.status_code == 503 and response.json()["code"] == "research_unknown"
    assert PRIVATE not in response.text
    no_store(response)
