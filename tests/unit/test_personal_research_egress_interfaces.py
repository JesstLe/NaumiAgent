"""Exact egress has distinct human power and frozen bounded provider transport."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware
from naumi_agent.api.routes.research import router
from naumi_agent.config.settings import SearchConfig
from naumi_agent.personal import research_capture as capture
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_commands import parse_research_command
from naumi_agent.personal.research_tools import request_schema, validate_research_arguments
from naumi_agent.tools.web import WebSearchTool

NOTEBOOK = "rq_" + "1" * 32
EGRESS = "rx_" + "2" * 32
SHA = "3" * 64
PROPOSE = {
    "operation": "egress_propose",
    "request_id": "original-egress-propose",
    "notebook_id": NOTEBOOK,
    "expected_notebook_revision": 1,
    "action": "search",
    "query": "原始 查询 + private",
    "provider": "duckduckgo",
    "reason": "核对来源",
}
DECIDE = {
    "operation": "egress_decide",
    "id": EGRESS,
    "request_id": "original-egress-decision",
    "expected_revision": 1,
    "expected_digest": SHA,
    "decision": "approve",
}


def engine_for(config=None):
    tool = WebSearchTool(
        search_config=config or SearchConfig(provider_order=("duckduckgo", "brave"))
    )
    return SimpleNamespace(
        tool_registry=SimpleNamespace(get=lambda name: tool if name == "web_search" else None)
    ), tool


@pytest.mark.parametrize(
    "operation", ["egress_propose", "egress_get", "egress_list", "egress_request", "egress_execute"]
)
def test_model_schema_explicitly_supports_only_nonhuman_egress_operations(operation):
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    names = {row["properties"]["operation"]["const"] for row in schema["oneOf"]}
    assert operation in names and "egress_decide" not in names
    for branch in schema["oneOf"]:
        assert branch["properties"]["operation"]["type"] == "string"


def test_model_cannot_approve_even_by_raw_arguments_but_real_human_adapter_can():
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments(DECIDE)
    assert error.value.code == "scope_denied"
    assert validate_research_arguments(DECIDE, human=True)["decision"] == "approve"
    assert not list(Draft202012Validator(request_schema()).iter_errors(PROPOSE))
    assert list(Draft202012Validator(request_schema()).iter_errors(DECIDE))


@pytest.mark.parametrize(
    "field,value",
    [
        ("caller_actor", "main"),
        ("trusted_channel", "web"),
        ("approved", True),
        ("artifact_refs", []),
    ],
)
def test_proposal_rejects_authority_and_dependency_forgery(field, value):
    with pytest.raises(PersonalAgentError):
        validate_research_arguments({**PROPOSE, field: value})


def test_all_egress_command_surfaces_use_the_original_operation_contract():
    raw = {key: value for key, value in PROPOSE.items() if key != "operation"}
    assert (
        parse_research_command("egress-propose-json " + json.dumps(raw, ensure_ascii=False))[
            "operation"
        ]
        == "egress_propose"
    )
    body = {key: value for key, value in DECIDE.items() if key != "operation"}
    assert parse_research_command("egress-decide-json " + json.dumps(body))["decision"] == "approve"
    assert parse_research_command("egress-get " + EGRESS) == {
        "operation": "egress_get",
        "id": EGRESS,
    }
    assert parse_research_command("egress-list --limit 2")["limit"] == 2
    assert (
        parse_research_command("egress-request original-request")["operation"] == "egress_request"
    )


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/egress?limit=2", None),
        ("GET", "/egress/" + EGRESS, None),
        ("GET", "/egress/requests/original-request", None),
        (
            "POST",
            "/operations/egress_decide",
            {key: value for key, value in DECIDE.items() if key != "operation"},
        ),
    ],
)
async def test_real_route_preserves_authenticated_human_channel_and_no_store(method, path, body):
    calls = []

    async def operation(arguments, **authority):
        calls.append((arguments, authority))
        return {"accepted": True}

    app = FastAPI()
    app.state.engine = SimpleNamespace(
        personal_research=SimpleNamespace(execute_operation=operation)
    )
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["egress-interface-only"]))
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.request(method, "/api/v1/personal/research" + path, json=body)
        assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
        response = await client.request(
            method,
            "/api/v1/personal/research" + path,
            json=body,
            headers={"Authorization": "Bearer egress-interface-only"},
        )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(calls) == 1 and calls[0][1] == {"caller_actor": None, "trusted_channel": "web"}


def test_transport_plan_is_local_exact_and_invalidated_by_provider_config_change(monkeypatch):
    engine, tool = engine_for()
    payload = {"query": "原始 查询 + private", "provider": "duckduckgo", "max_results": 3}
    plan = capture.build_egress_plan("search", payload, engine)
    assert plan["endpoint"] == "https://lite.duckduckgo.com/lite/"
    assert plan["parameters"] == {"q": payload["query"], "kl": "wt-wt"}
    assert plan["max_redirects"] == 0 and plan["redirect_policy"] == "none"
    assert plan["trust_env"] is False and plan["tls_verify"] is True
    assert capture.validate_egress_plan("search", payload, engine, plan) == plan
    tool._search_config = SearchConfig(provider_order=("brave", "duckduckgo"))
    with pytest.raises(PersonalAgentError) as error:
        capture.validate_egress_plan("search", payload, engine, plan)
    assert error.value.code == "source_stale"


async def test_approved_search_sends_exact_form_to_pinned_recipient_without_fallback(monkeypatch):
    engine, tool = engine_for()
    payload = {"query": "原始 查询 + private", "provider": "duckduckgo", "max_results": 2}
    plan = capture.build_egress_plan("search", payload, engine)
    requests, clients = [], []

    async def resolve(host, port):
        assert (host, port) == ("lite.duckduckgo.com", 443)
        return ("8.8.8.8",)

    def client(timeout):
        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text='<a href="https://docs.example/source" class="result-link">原始来源</a>',
            )

        value = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        clients.append(value)
        return value

    monkeypatch.setattr(capture, "_resolve_public", resolve)
    monkeypatch.setattr(capture, "_make_client", client)
    monkeypatch.setattr(
        tool, "_brave_search", AsyncMock(side_effect=AssertionError("Unapproved fallback"))
    )
    monkeypatch.setattr(
        tool, "_browser_search", AsyncMock(side_effect=AssertionError("Unapproved browser"))
    )
    result = await capture.discover_approved(tool, payload["query"], 2, plan=plan)
    assert result["status"] == "success" and len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and request.url.host == "8.8.8.8"
    assert request.headers["host"] == "lite.duckduckgo.com"
    assert request.extensions["sni_hostname"] == "lite.duckduckgo.com"
    assert parse_qs(request.content.decode()) == {"q": [payload["query"]], "kl": ["wt-wt"]}
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://unapproved.example/private"}),
        httpx.Response(503),
        httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b"valid compressed bytes"),
        ),
        httpx.Response(
            200, headers={"content-length": str(capture.MAX_CAPTURE_BYTES + 1)}, content=b"x"
        ),
    ],
    ids=["redirect", "503", "compression", "oversize"],
)
async def test_provider_failure_never_falls_back_or_follows_redirect(monkeypatch, response):
    engine, tool = engine_for()
    payload = {"query": "private-canary", "provider": "duckduckgo", "max_results": 2}
    plan = capture.build_egress_plan("search", payload, engine)
    requests = []

    async def resolve(*_args):
        return ("8.8.8.8",)

    def client(timeout):
        def handler(request):
            requests.append(request)
            return response

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(capture, "_resolve_public", resolve)
    monkeypatch.setattr(capture, "_make_client", client)
    result = await capture.discover_approved(tool, payload["query"], 2, plan=plan)
    assert result["status"] == "unavailable" and len(requests) == 1
    assert len(result["attempts"]) == 1 and result["attempts"][0]["provider"] == "duckduckgo"
