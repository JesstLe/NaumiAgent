"""Research context recovery shares strict authenticated human/manual adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from naumi_agent.api.middleware import AuthMiddleware
from naumi_agent.api.routes.research import router
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_commands import parse_research_command
from naumi_agent.personal.research_context import normalize_context_ref
from naumi_agent.personal.research_tools import (
    execute_research_operation,
    request_schema,
    validate_research_arguments,
)

SESSION = "123456abcdef"
SHA = "3" * 64
PREVIEW = {"operation": "context_preview", "session_id": SESSION, "actor_id": "main"}
ROLLFORWARD = {
    "operation": "context_rollforward",
    "session_id": SESSION,
    "actor_id": "main",
    "request_id": "original-context-update",
    "expected_context_revision": 0,
    "expected_plan_digest": SHA,
}
REQUEST = {"operation": "context_request", "request_id": "original-context-update"}
CONTEXT_REF = {
    "id": "rcu_" + "6" * 32,
    "revision": 1,
    "workspace_key": "7" * 64,
    "record_digest": "8" * 64,
    "original_ref": {
        "artifact_id": "art_" + "4" * 32,
        "version": 1,
        "sha256": "5" * 64,
        "grant_id": "",
        "grant_revision": 0,
    },
}


def test_context_model_schema_allows_own_read_operations_but_never_rollforward():
    schema = request_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    names = {branch["properties"]["operation"]["const"] for branch in schema["oneOf"]}
    assert {"context_preview", "context_request"} <= names
    assert "context_rollforward" not in names
    assert not list(validator.iter_errors(PREVIEW))
    assert not list(validator.iter_errors(REQUEST))
    assert list(validator.iter_errors(ROLLFORWARD))


def test_zero_revision_is_valid_only_with_a_real_human_channel():
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments(ROLLFORWARD)
    assert error.value.code == "scope_denied"
    normalized = validate_research_arguments(ROLLFORWARD, human=True)
    assert normalized == ROLLFORWARD and type(normalized["expected_context_revision"]) is int


def test_context_authority_ref_preserves_the_exact_original_artifact_identity():
    assert normalize_context_ref(CONTEXT_REF) == CONTEXT_REF


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "rread_" + "6" * 32),
        ("revision", True),
        ("revision", 2),
        ("workspace_key", "7" * 63),
        ("record_digest", "8" * 63),
        ("original_ref", {"artifact_id": "art_" + "4" * 32, "version": 1}),
        ("allowed_read_ids", []),
        ("skip_acl", True),
    ],
)
def test_context_authority_ref_rejects_tampered_identity_and_scope_fields(field, value):
    with pytest.raises(PersonalAgentError):
        normalize_context_ref({**CONTEXT_REF, field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_context_revision", True),
        ("expected_context_revision", -1),
        ("expected_context_revision", "0"),
        ("expected_plan_digest", "3" * 63),
        ("expected_plan_digest", "G" * 64),
        ("actor_id", "invented-agent"),
        ("session_id", ""),
        ("request_id", ""),
    ],
)
def test_context_mutation_rejects_malformed_original_identity_and_cas(field, value):
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments({**ROLLFORWARD, field: value}, human=True)
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "field,value",
    [
        ("caller_actor", "main"),
        ("trusted_channel", "web"),
        ("approved", True),
        ("old_refs", []),
        ("new_refs", []),
        ("allowed_read_ids", []),
        ("record_digest", SHA),
        ("body", "forged historical body"),
    ],
)
def test_context_client_cannot_supply_authority_receipts_or_replacement_bytes(field, value):
    with pytest.raises(PersonalAgentError) as error:
        validate_research_arguments({**ROLLFORWARD, field: value}, human=True)
    assert error.value.code == "invalid_input"


async def test_shared_adapter_passes_real_human_channel_without_model_identity_coercion():
    calls = []

    async def execute(arguments, **authority):
        calls.append((arguments, authority))
        return {"status": "completed"}

    service = SimpleNamespace(execute_operation=execute)
    assert await execute_research_operation(service, ROLLFORWARD, trusted_channel="cli") == {
        "status": "completed"
    }
    assert calls == [(ROLLFORWARD, {"caller_actor": None, "trusted_channel": "cli"})]
    with pytest.raises(PersonalAgentError) as error:
        await execute_research_operation(service, ROLLFORWARD, caller_actor="main")
    assert error.value.code == "scope_denied" and len(calls) == 1


def test_context_cli_tui_commands_preserve_the_exact_shared_arguments():
    assert parse_research_command("context-preview " + SESSION + " main") == PREVIEW
    assert parse_research_command("context-request original-context-update") == REQUEST
    fields = {key: value for key, value in ROLLFORWARD.items() if key != "operation"}
    assert parse_research_command("context-rollforward-json " + json.dumps(fields)) == ROLLFORWARD


@pytest.mark.parametrize(
    "method,path,body,expected",
    [
        ("GET", "/session-context/" + SESSION + "?actor_id=main", None, PREVIEW),
        ("GET", "/context-requests/original-context-update", None, REQUEST),
        (
            "POST",
            "/operations/context_rollforward",
            {key: value for key, value in ROLLFORWARD.items() if key != "operation"},
            ROLLFORWARD,
        ),
    ],
)
async def test_context_http_is_authenticated_no_store_and_uses_original_manual_contract(
    method, path, body, expected
):
    calls = []

    async def operation(arguments, **authority):
        calls.append((arguments, authority))
        return {"accepted": True}

    app = FastAPI()
    app.state.engine = SimpleNamespace(
        personal_research=SimpleNamespace(execute_operation=operation)
    )
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["context-interface-only"]))
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.request(method, "/api/v1/personal/research" + path, json=body)
        assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
        result = await client.request(
            method,
            "/api/v1/personal/research" + path,
            json=body,
            headers={"Authorization": "Bearer context-interface-only"},
        )
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert calls == [(expected, {"caller_actor": None, "trusted_channel": "web"})]


@pytest.mark.parametrize("query", ["?actor_id=main&actor_id=main", "?actor_id=main&override=true"])
async def test_context_get_rejects_duplicate_or_unknown_query_before_dispatch(query):
    calls = []

    async def operation(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    app = FastAPI()
    app.state.engine = SimpleNamespace(
        personal_research=SimpleNamespace(execute_operation=operation)
    )
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["context-interface-only"]))
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/personal/research/session-context/" + SESSION + query,
            headers={"Authorization": "Bearer context-interface-only"},
        )
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert calls == []
