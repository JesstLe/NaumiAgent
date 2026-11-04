"""Real HTTP and command transports with an explicitly controlled memory service port."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

from naumi_agent.api.routes.commands import _CommandEngine, validate_command
from naumi_agent.api.routes.commands import router as commands_router
from naumi_agent.api.routes.memories import router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal.memory_commands import parse_memory_command, run_memory_command
from naumi_agent.personal.memory_tools import (
    HUMAN_OPERATIONS,
    MODEL_OPERATIONS,
    MemoryRecallAlias,
    MemoryStoreAlias,
    PersonalMemoryTool,
    operation_schema,
)
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tools.base import ToolExecutionError

MEMORY = "pm_" + "a" * 32
ACTOR = "pa_" + "b" * 32
GRANT = "pmg_" + "c" * 32
HASH = "d" * 64
REF = {
    "memory_id": MEMORY,
    "revision": 2,
    "content_sha256": HASH,
    "grant_id": "",
    "grant_revision": 0,
}
SENTINEL = "PRIVATE_MEMORY_BODY_NOT_FOR_PERSISTED_TOOL_OR_COMMAND"
CREATE = {
    "request_id": "stable-request",
    "topic": "输出语言",
    "category": "preference",
    "content": "使用中文回答",
    "scope": "personal",
}
CONFIRM = {
    "request_id": "stable-confirm",
    "memory_id": MEMORY,
    "expected_revision": 2,
    "expected_content_sha256": HASH,
    "expected_active_ref": None,
}


def memory():
    return {
        "memory_id": MEMORY,
        "revision": 2,
        "state": "active",
        "topic": "输出语言",
        "content": SENTINEL,
        "ref": dict(REF),
        "content_sha256": HASH,
    }


class Port:
    """Adapters are real; this class only captures the frozen async service contract."""

    def __init__(self, engine):
        self.engine, self.calls, self.error, self.invalid_reply = engine, [], None, None

    async def _call(self, method, *args, **kwargs):
        self.calls.append((method, copy.deepcopy(args), copy.deepcopy(kwargs)))
        if self.error:
            raise self.error
        if self.invalid_reply is not None:
            return copy.deepcopy(self.invalid_reply)
        if method == "dispatch" and kwargs.get("caller_actor") is not None:
            return {
                "type": "personal_memory_refs",
                "refs": [dict(REF)],
                "memory_id": MEMORY,
                "state": "proposed" if args[0] == "propose" else "active",
                "content": SENTINEL,
                "source": {"excerpt": SENTINEL},
                "diagnostics": {"content": SENTINEL},
                "request_id": "stable-request",
            }
        if method == "context":
            return {
                "actor_id": kwargs["actor_id"],
                "workspace_key": "e" * 64,
                "index": {"state": "ready"},
            }
        if method in {"list", "search", "export", "legacy"}:
            return {"memories": [memory()], "limit": 50, "offset": 0, "next_offset": None}
        if method == "history":
            return {"history": [{"memory_id": MEMORY, "revision": 2, "state": "active"}]}
        if method == "grants":
            return {"grants": []}
        if method in {"prepare_request", "request"}:
            return {
                "request_id": "stable-request",
                "actor_id": "main",
                "fingerprint": HASH,
                "state": "prepared",
            }
        return memory()

    def __getattr__(self, method):
        async def call(*args, **kwargs):
            return await self._call(method, *args, **kwargs)

        return call


def engine(tmp_path, *, actor=None):
    result = SimpleNamespace(
        _session=SimpleNamespace(id="source-session", personal_execution_id="")
    )
    if actor:
        db = tmp_path / "personal.db"
        db.touch()
        profile = PersonalAgent(
            id=actor,
            name="Echo",
            description="",
            instructions="整理",
            model_tier="capable",
            tool_names=(),
            kind="specialist",
            member_ids=(),
            status="active",
            version=4,
            created_at="",
            updated_at="",
        )
        result._session.personal_agent_id = actor
        result.personal_agents = SimpleNamespace(
            store=SimpleNamespace(db_path=db),
            current=lambda session_id: profile,
            validate_tools=lambda names: None,
        )
    result.personal_memory = Port(result)
    return result


def app_for(service):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.engine = SimpleNamespace(personal_memory=service)
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["memory-test-key"]))

    @app.exception_handler(PersonalAgentError)
    async def handler(request, exc):
        return JSONResponse(
            status_code={"version_conflict": 409, "storage_error": 503}.get(exc.code, 422),
            content={"code": exc.code, "detail": exc.message},
            headers={"Cache-Control": "no-store"},
        )

    return app


@pytest.fixture
async def api(tmp_path):
    service = engine(tmp_path).personal_memory
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://memory-test/api/v1",
        headers={"X-API-Key": "memory-test-key"},
    ) as client:
        yield client, service


@pytest.mark.parametrize("operation", [name for name in HUMAN_OPERATIONS if name != "propose"])
async def test_model_has_no_human_management_operations(tmp_path, operation):
    runtime = engine(tmp_path)
    tool = PersonalMemoryTool(runtime.personal_memory)
    with pytest.raises(ToolExecutionError) as exc:
        await tool.execute(operation, **CREATE)
    assert exc.value.code == "invalid_input"
    assert runtime.personal_memory.calls == []


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("list", {"scope": "workspace", "limit": 5}),
        ("get", {"memory_id": MEMORY}),
        ("search", {"query": "语言", "category": "preference"}),
        ("propose", {**CREATE, "scope": "workspace"}),
        ("history", {"memory_id": MEMORY}),
    ],
    ids=MODEL_OPERATIONS,
)
async def test_model_uses_current_actor_and_persists_only_exact_refs(tmp_path, operation, payload):
    runtime = engine(tmp_path, actor=ACTOR)
    result = json.loads(
        await PersonalMemoryTool(runtime.personal_memory).execute(operation, **payload)
    )
    method, args, kwargs = runtime.personal_memory.calls[-1]
    assert method == "dispatch" and args == (operation, payload)
    assert kwargs == {"actor_id": ACTOR, "caller_actor": ACTOR}
    assert result["refs"] == [REF] and result["type"] == "personal_memory_refs"
    assert SENTINEL not in json.dumps(result)
    if operation == "propose":
        assert "尚未生效" in result["notice"]


@pytest.mark.parametrize(
    "field",
    [
        "actor_id",
        "owner_key",
        "caller_actor",
        "trusted_channel",
        "source",
        "confirmation",
        "confirmed",
    ],
)
async def test_model_rejects_trusted_fields_before_service(tmp_path, field):
    runtime = engine(tmp_path)
    with pytest.raises(ToolExecutionError):
        await PersonalMemoryTool(runtime.personal_memory).execute(
            "propose", **CREATE, **{field: "x"}
        )
    assert runtime.personal_memory.calls == []


async def test_aliases_are_scoped_proposal_and_reference_search(tmp_path):
    runtime = engine(tmp_path)
    assert MemoryRecallAlias(runtime.personal_memory).metadata.read_only is True
    assert MemoryRecallAlias(runtime.personal_memory).metadata.concurrency_safe is True
    assert MemoryStoreAlias(runtime.personal_memory).metadata.read_only is False
    stored = json.loads(
        await MemoryStoreAlias(runtime.personal_memory).execute(content="以后请用中文")
    )
    method, args, kwargs = runtime.personal_memory.calls[-1]
    assert method == "dispatch" and args[0] == "propose"
    assert args[1]["scope"] == "workspace" and "request_id" not in args[1]
    assert kwargs == {"actor_id": "main", "caller_actor": "main"}
    assert stored["state"] == "proposed" and SENTINEL not in json.dumps(stored)
    result = json.loads(
        await MemoryRecallAlias(runtime.personal_memory).execute(
            query="语言",
            category="preference",
            top_k=7,
        )
    )
    assert runtime.personal_memory.calls[-1][1] == (
        "search",
        {"query": "语言", "category": "preference", "limit": 7},
    )
    assert result["refs"] == [REF] and SENTINEL not in json.dumps(result)


@pytest.mark.parametrize(
    "top_k", [True, 0, 11, 3.0, "3", None], ids=["bool", "zero", "large", "float", "str", "null"]
)
async def test_alias_rejects_invalid_limits(tmp_path, top_k):
    runtime = engine(tmp_path)
    with pytest.raises(ToolExecutionError):
        await MemoryRecallAlias(runtime.personal_memory).execute(query="text", top_k=top_k)
    assert not runtime.personal_memory.calls


@pytest.mark.parametrize(
    "reply", [{"content": SENTINEL}, {"type": "personal_memory_refs", "refs": [{}]}]
)
async def test_model_fails_closed_on_non_reference_service_reply(tmp_path, reply):
    runtime = engine(tmp_path)
    runtime.personal_memory.invalid_reply = reply
    with pytest.raises(ToolExecutionError) as exc:
        await PersonalMemoryTool(runtime.personal_memory).execute("list")
    assert exc.value.code == "memory_result_unverified" and SENTINEL not in str(exc.value)


async def test_model_json_rejects_duplicate_keys(tmp_path):
    tool = PersonalMemoryTool(engine(tmp_path).personal_memory)
    with pytest.raises(ValueError):
        tool.parse_arguments('{"operation":"list","operation":"search"}')
    assert set(tool.parameters_schema["properties"]["operation"]["enum"]) == set(MODEL_OPERATIONS)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/personal/memories/context", "context"),
        ("/personal/memories", "list"),
        ("/personal/memories/search?query=language&category=preference", "search"),
        ("/personal/memories/export", "export"),
        ("/personal/memories/legacy", "legacy"),
        (f"/personal/memories/{MEMORY}", "get"),
        (f"/personal/memories/{MEMORY}/history", "history"),
        (f"/personal/memories/{MEMORY}/grants", "grants"),
        ("/personal/memory-requests/stable-request", "request"),
    ],
)
async def test_authenticated_read_routes_keep_human_actor_query_and_no_store(api, path, method):
    client, service = api
    separator = "&" if "?" in path else "?"
    response = await client.get(path + separator + "actor_id=" + ACTOR)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    called, args, kwargs = service.calls[-1]
    assert called == method and kwargs["actor_id"] == ACTOR and "caller_actor" not in kwargs
    denied = await client.get(path, headers={"X-API-Key": "wrong"})
    assert denied.status_code == 401


async def test_actual_conflict_state_filter_reaches_shared_service(api):
    client, service = api
    response = await client.get("/personal/memories?state=conflict&actor_id=" + ACTOR)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    called, args, kwargs = service.calls[-1]
    assert called == "list" and not args
    assert kwargs["state"] == "conflict" and kwargs["actor_id"] == ACTOR
    count = len(service.calls)
    invalid = await client.get("/personal/memories?state=unrecognized")
    assert invalid.status_code == 422 and len(service.calls) == count


async def test_prepare_apply_are_opaque_and_have_server_human_channel(api):
    client, service = api
    body = {
        "request_id": CREATE["request_id"],
        "actor_id": "main",
        "operation": "create",
        "payload": CREATE,
    }
    prepared = await client.post("/personal/memory-requests", json=body)
    assert prepared.status_code == 201, prepared.text
    assert service.calls[-1] == ("prepare_request", (body,), {"trusted_channel": "management_web"})
    assert "content" not in prepared.json() and "payload" not in prepared.json()
    response = await client.post(
        "/personal/memory-requests/stable-request/apply?actor_id=" + ACTOR,
        json={"expected_fingerprint": HASH},
    )
    assert response.status_code == 200
    assert service.calls[-1] == (
        "apply_request",
        ("stable-request",),
        {"actor_id": ACTOR, "expected_fingerprint": HASH, "trusted_channel": "management_web"},
    )


@pytest.mark.parametrize("state", ["erased", "applied"])
async def test_discard_keeps_original_request_identity_and_server_human_channel(api, state):
    client, service = api
    metadata = {
        "request_id": "stable-request",
        "actor_id": ACTOR,
        "fingerprint": HASH,
        "state": state,
        "result": {"memory_id": MEMORY} if state == "applied" else None,
    }
    service.invalid_reply = metadata
    response = await client.post(
        "/personal/memory-requests/stable-request/discard?actor_id=" + ACTOR,
        json={"expected_fingerprint": HASH},
    )
    assert response.status_code == 200, response.text
    assert response.json() == metadata
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert SENTINEL not in response.text and "payload" not in response.json()
    assert service.calls == [
        (
            "discard_request",
            ("stable-request",),
            {
                "actor_id": ACTOR,
                "expected_fingerprint": HASH,
                "trusted_channel": "management_web",
            },
        )
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_fingerprint": None},
        {"expected_fingerprint": True},
        {"expected_fingerprint": 42},
        {"expected_fingerprint": []},
        {"expected_fingerprint": "d" * 63},
        {"expected_fingerprint": "D" * 64},
        {"expected_fingerprint": HASH, "trusted_channel": "manual_cli"},
        {"expected_fingerprint": HASH, "actor_id": ACTOR},
        {"expected_fingerprint": HASH, "request_id": "different-request"},
        {"expected_fingerprint": HASH, "content": SENTINEL},
        {"expected_fingerprint": HASH, "owner_key": "e" * 64},
    ],
    ids=[
        "missing",
        "null",
        "bool",
        "number",
        "array",
        "short",
        "uppercase",
        "channel",
        "body-actor",
        "request-replacement",
        "second-body",
        "owner",
    ],
)
async def test_discard_strict_fingerprint_body_rejects_unknown_trusted_fields(api, payload):
    client, service = api
    response = await client.post("/personal/memory-requests/stable-request/discard", json=payload)
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    assert not service.calls


@pytest.mark.parametrize(
    "content",
    [
        '{"expected_fingerprint":"' + HASH + '","expected_fingerprint":"' + HASH + '"}',
        '{"expected_fingerprint":NaN}',
        '{"expected_fingerprint":Infinity}',
    ],
    ids=["duplicate", "nan", "infinity"],
)
async def test_discard_rejects_untrusted_json_at_http_edge(api, content):
    client, service = api
    response = await client.post(
        "/personal/memory-requests/stable-request/discard",
        content=content,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and not service.calls


async def test_discard_authenticates_before_service_and_does_not_cancel_committed_facts(api):
    client, service = api
    response = await client.post(
        "/personal/memory-requests/stable-request/discard",
        json={"expected_fingerprint": HASH},
        headers={"X-API-Key": "wrong"},
    )
    assert response.status_code == 401 and not service.calls
    assert response.headers["cache-control"] == "no-store"


async def test_discard_preserves_service_fingerprint_conflict_without_claiming_erased(api):
    client, service = api
    service.error = PersonalAgentError("version_conflict", "原请求摘要不一致，请核对")
    response = await client.post(
        "/personal/memory-requests/stable-request/discard",
        json={"expected_fingerprint": HASH},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "version_conflict"
    assert "state" not in response.json()
    assert response.headers["cache-control"] == "no-store"
    assert len(service.calls) == 1 and service.calls[0][0] == "discard_request"


async def test_discard_rejects_oversized_body_before_service(api):
    client, service = api
    response = await client.post(
        "/personal/memory-requests/stable-request/discard",
        content=b" " * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and not service.calls


@pytest.mark.parametrize("operation", ["discard", "discard_request"])
async def test_model_does_not_get_manual_draft_discard_operation(tmp_path, operation):
    runtime = engine(tmp_path)
    with pytest.raises(ToolExecutionError):
        await PersonalMemoryTool(runtime.personal_memory).execute(
            operation, request_id="stable-request", expected_fingerprint=HASH
        )
    assert not runtime.personal_memory.calls


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("create", CREATE),
        ("propose", {**CREATE, "scope": "workspace"}),
        ("confirm", CONFIRM),
        (
            "revise",
            {
                k: v
                for k, v in {**CONFIRM, "content": "改用简洁中文"}.items()
                if k != "expected_active_ref"
            },
        ),
        ("delete", {"request_id": "delete", "memory_id": MEMORY, "expected_revision": 2}),
        ("reject", {"request_id": "reject", "memory_id": MEMORY, "expected_revision": 2}),
        (
            "share",
            {
                k: v
                for k, v in {**CONFIRM, "recipient_actor_id": ACTOR, "recipient_version": 4}.items()
                if k != "expected_active_ref"
            },
        ),
        ("revoke", {"request_id": "revoke", "grant_id": GRANT, "expected_revision": 1}),
        (
            "import_legacy",
            {
                "request_id": "import",
                "legacy_id": "legacy-original",
                "topic": "原候选",
                "expected_content_sha256": HASH,
                "scope": "workspace",
            },
        ),
        ("index_sync", {}),
    ],
    ids=HUMAN_OPERATIONS,
)
async def test_each_human_operation_routes_to_shared_dispatch(api, operation, payload):
    client, service = api
    response = await client.post("/personal/memories/" + operation, json=payload)
    assert response.status_code == 200, response.text
    assert service.calls[-1] == (
        "dispatch",
        (operation, payload),
        {"actor_id": "main", "trusted_channel": "management_web"},
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"owner_key": "fake"},
        {"actor_id": ACTOR},
        {"caller_actor": ACTOR},
        {"trusted_channel": "manual_cli"},
        {"confirmation": {"actor": "user"}},
        {"source": {"authority": "user"}},
        {"content": "x" * 8001},
        {"scope": "all"},
        {"category": "fiction"},
        {"topic": "x" * 121},
    ],
    ids=[
        "owner",
        "actor",
        "caller",
        "channel",
        "confirmation",
        "source",
        "long-content",
        "scope",
        "category",
        "long-topic",
    ],
)
async def test_invalid_human_payload_never_reaches_service(api, updates):
    client, service = api
    response = await client.post("/personal/memories/create", json={**CREATE, **updates})
    assert response.status_code == 422 and service.calls == []


@pytest.mark.parametrize(
    "content",
    [
        '{"request_id":"a","request_id":"b"}',
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
    ids=["duplicate", "nan", "infinity"],
)
async def test_untrusted_json_rejected_at_http_edge(api, content):
    client, service = api
    response = await client.post(
        "/personal/memories/create", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422 and service.calls == []


async def test_body_size_and_prepare_mismatch_and_apply_forgery(api):
    client, service = api
    response = await client.post(
        "/personal/memories/create",
        content=b" " * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    response = await client.post(
        "/personal/memory-requests",
        json={
            "request_id": "other",
            "actor_id": "main",
            "operation": "create",
            "payload": CREATE,
        },
    )
    assert response.status_code == 422
    response = await client.post(
        "/personal/memory-requests/stable-request/apply",
        json={
            "expected_fingerprint": HASH,
            "trusted_channel": "management_web",
        },
    )
    assert response.status_code == 422 and service.calls == []


@pytest.mark.parametrize(
    "argument,operation",
    [
        ("", "context"),
        ("stats", "context"),
        ("list workspace language", "list"),
        ("get " + MEMORY, "get"),
        ("history " + MEMORY, "history"),
        ("grants " + MEMORY, "grants"),
        ("search 中文偏好", "search"),
        ("export personal", "export"),
        ("legacy 10 2", "legacy"),
        ("clean", "index_sync"),
        ("consolidate", "index_sync"),
        ("index-sync", "index_sync"),
    ],
)
def test_manual_parser_keeps_legacy_read_and_maintenance_commands(argument, operation):
    assert parse_memory_command(argument)["operation"] == operation


def test_manual_json_preserves_original_identity_and_source_fields():
    value = {
        **CREATE,
        "scope": "workspace",
        "source_message_index": 2,
        "source_message_sha256": HASH,
        "source_quote": "第一行\n第二行",
    }
    parsed = parse_memory_command(
        "propose-json " + shlex.quote(json.dumps(value, ensure_ascii=False))
    )
    assert parsed == {"operation": "propose", **value}


@pytest.mark.parametrize(
    "argument",
    [
        "export /outside/path",
        "legacy 101",
        "search",
        "get other",
        "confirm-json '{}'",
        'create-json \'{"operation":"create"}\'',
        "propose-json '{}'",
        "'unclosed",
    ],
)
def test_manual_rejects_unknown_or_partial_writes(argument):
    with pytest.raises(ValueError):
        parse_memory_command(argument)


async def test_both_legacy_manual_entrypoints_forward_to_current_service(tmp_path):
    from naumi_agent import main
    from naumi_agent.cli.commands_meta import handle_memory

    runtime = engine(tmp_path, actor=ACTOR)
    await main._handle_memory(runtime, "stats")
    await handle_memory(runtime, "stats")
    assert [x[0] for x in runtime.personal_memory.calls] == ["context", "context"]
    assert all(x[2] == {"actor_id": ACTOR} for x in runtime.personal_memory.calls)


async def test_manual_mutation_uses_fixed_human_channel_and_keeps_original_payload(tmp_path):
    runtime = engine(tmp_path)
    command = "confirm-json " + shlex.quote(json.dumps(CONFIRM))
    output = await run_memory_command(runtime, command)
    assert "记忆确认结果" in output
    assert runtime.personal_memory.calls[-1] == (
        "dispatch",
        ("confirm", CONFIRM),
        {"actor_id": "main", "trusted_channel": "manual_cli"},
    )
    assert runtime._personal_memory_command_refs == [REF]


async def test_ref_collection_is_per_web_adapter_not_a_shared_engine_field(tmp_path):
    runtime = engine(tmp_path)
    first, second = _CommandEngine(runtime, None), _CommandEngine(runtime, None)
    await run_memory_command(first, "get " + MEMORY, trusted_channel="management_web")
    second._personal_memory_command_refs = []
    assert first._personal_memory_command_refs == [REF]
    assert second._personal_memory_command_refs == []
    assert not hasattr(runtime, "_personal_memory_command_refs")


async def web_context(tmp_path):
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    session = await sessions.create_session(title="记忆命令")
    runtime = SimpleNamespace(session_store=sessions, runtime_mode="default", _session=session)
    runtime.personal_memory = Port(runtime)

    async def load(identifier):
        runtime._session = await sessions.load(identifier)
        return runtime._session is not None

    runtime.load_session = load
    runtime.set_runtime_mode = lambda mode: setattr(runtime, "runtime_mode", mode)
    app = FastAPI()
    app.include_router(commands_router)
    app.state.engine = runtime
    app.state.chat_run_store = ChatRunStore(str(tmp_path / "runs.db"))
    app.state.engine_lock = asyncio.Lock()
    return app, runtime, session, sessions


async def test_actual_web_command_streams_body_but_persists_only_revocable_refs(tmp_path):
    app, runtime, session, sessions = await web_context(tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://web-command"
        ) as client:
            response = await client.post(
                f"/sessions/{session.id}/commands",
                json={
                    "command": "/memory get " + MEMORY,
                },
            )
        assert response.status_code == 200 and SENTINEL in response.text
        saved = await sessions.load(session.id)
        assert SENTINEL not in json.dumps(saved.messages)
        assert saved.messages[-1]["metadata"]["personal_memory_refs"] == [REF]
        envelope = json.loads(saved.messages[-1]["content"])
        assert envelope == {"type": "personal_memory_refs", "refs": [REF]}
        runs = await app.state.chat_run_store.list_runs(session.id)
        assert runs[0].status == "completed" and SENTINEL not in runs[0].steps[0].detail
        assert runtime.personal_memory.calls[-1] == ("get", (MEMORY,), {"actor_id": "main"})
    finally:
        await sessions.close()


async def test_actual_web_manual_error_is_failed_and_execution_session_is_readonly(tmp_path):
    app, runtime, session, sessions = await web_context(tmp_path)
    try:
        runtime.personal_memory.error = PersonalAgentError("version_conflict", "记忆已变更")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://web-command"
        ) as client:
            response = await client.post(
                f"/sessions/{session.id}/commands",
                json={
                    "command": "/memory get " + MEMORY,
                },
            )
            assert response.status_code == 200 and '"failed"' in response.text
            runs = await app.state.chat_run_store.list_runs(session.id)
            assert runs[0].status == "failed"
            original = await sessions.load(session.id)
            original.personal_execution_id = "pio_" + "f" * 32
            await sessions.save(original)
            before = copy.deepcopy(original.messages)
            denied = await client.post(
                f"/sessions/{session.id}/commands",
                json={
                    "command": "/memory stats",
                },
            )
            assert denied.status_code == 409
            assert (await sessions.load(session.id)).messages == before
            assert len(await app.state.chat_run_store.list_runs(session.id)) == 1
    finally:
        await sessions.close()


def test_memory_command_is_in_actual_shared_web_index():
    assert validate_command("/memory stats") == "/memory stats"


async def test_search_limit_matches_actual_service_and_list_keeps_100(api, tmp_path):
    client, service = api
    assert (
        await client.get("/personal/memories/search?query=language&limit=20")
    ).status_code == 200
    count = len(service.calls)
    assert (
        await client.get("/personal/memories/search?query=language&limit=21")
    ).status_code == 422
    assert len(service.calls) == count
    assert (await client.get("/personal/memories?limit=100")).status_code == 200
    runtime = engine(tmp_path)
    with pytest.raises(ToolExecutionError):
        await PersonalMemoryTool(runtime.personal_memory).execute(
            "search", query="language", limit=21
        )
    assert not runtime.personal_memory.calls


async def test_safe_diagnostics_preserve_index_unavailability_without_error_body(tmp_path):
    runtime = engine(tmp_path)
    runtime.personal_memory.invalid_reply = {
        "type": "personal_memory_refs",
        "refs": [],
        "candidate_id": MEMORY,
        "diagnostics": {
            "status": "unavailable",
            "error": SENTINEL,
            "semantic_error": SENTINEL,
            "semantic_used": False,
            "authorized_candidates": 2,
            "lexical_hits": 0,
        },
    }
    value = json.loads(
        await PersonalMemoryTool(runtime.personal_memory).execute("search", query="text")
    )
    assert value["candidate_id"] == MEMORY
    assert value["diagnostics"]["status"] == "unavailable"
    assert value["diagnostics"]["authorized_candidates"] == 2
    assert "暂未就绪" in value["diagnostics"]["notice"]
    assert SENTINEL not in json.dumps(value)


async def test_new_search_query_2000_and_legacy_alias_1000_bounds(tmp_path):
    runtime = engine(tmp_path)
    await PersonalMemoryTool(runtime.personal_memory).execute("search", query="a" * 2000)
    with pytest.raises(ToolExecutionError):
        await PersonalMemoryTool(runtime.personal_memory).execute("search", query="a" * 2001)
    with pytest.raises(ToolExecutionError):
        await MemoryRecallAlias(runtime.personal_memory).execute(query="a" * 1001)
    assert len(runtime.personal_memory.calls) == 1


@pytest.mark.parametrize("state", ["proposed", "active", "deleted", "rejected", "superseded"])
async def test_proposal_replay_reports_actual_current_state_without_resurrection_claim(
    tmp_path, state
):
    runtime = engine(tmp_path)
    runtime.personal_memory.invalid_reply = {
        "type": "personal_memory_refs",
        "refs": [],
        "candidate_id": MEMORY,
        "state": state,
    }
    result = json.loads(
        await PersonalMemoryTool(runtime.personal_memory).execute("propose", **CREATE)
    )
    assert result["state"] == state
    assert ("等待用户确认" in result["notice"]) is (state == "proposed")
    if state != "proposed":
        assert "未重新创建" in result["notice"]


async def test_proposal_missing_mechanical_identity_is_not_reported_saved(tmp_path):
    runtime = engine(tmp_path)
    runtime.personal_memory.invalid_reply = {
        "type": "personal_memory_refs",
        "refs": [],
        "state": "proposed",
    }
    with pytest.raises(ToolExecutionError) as exc:
        await PersonalMemoryTool(runtime.personal_memory).execute("propose", **CREATE)
    assert exc.value.code == "memory_result_unverified"


def _enum_nodes(value):
    if isinstance(value, dict):
        if "enum" in value:
            yield value
        for child in value.values():
            yield from _enum_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _enum_nodes(child)


@pytest.mark.parametrize("tool_type", [PersonalMemoryTool, MemoryStoreAlias, MemoryRecallAlias])
def test_every_model_exposed_enum_has_explicit_provider_compatible_string_type(tmp_path, tool_type):
    schema = tool_type(engine(tmp_path).personal_memory).parameters_schema
    nodes = list(_enum_nodes(schema))
    assert nodes
    assert all(node.get("type") == "string" for node in nodes)
    assert all(isinstance(value, str) for node in nodes for value in node["enum"])
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "tool_type,base",
    [
        (PersonalMemoryTool, {"operation": "list"}),
        (MemoryStoreAlias, {"content": "明确提出候选"}),
        (MemoryRecallAlias, {"query": "偏好"}),
    ],
)
def test_explicit_enum_types_preserve_existing_jsonschema_acceptance(tmp_path, tool_type, base):
    schema = tool_type(engine(tmp_path).personal_memory).parameters_schema
    original_schema = copy.deepcopy(schema)
    for node in _enum_nodes(original_schema):
        node.pop("type")
    current, original = Draft202012Validator(schema), Draft202012Validator(original_schema)
    samples = [base, {}, {**base, "unknown": "not allowed"}]
    for field, definition in schema["properties"].items():
        if "enum" not in definition:
            continue
        values = [*definition["enum"], None, False, True, 0, 1, [], {}, "not-an-enum-member"]
        samples.extend({**base, field: value} for value in values)
    assert all(current.is_valid(sample) == original.is_valid(sample) for sample in samples)


@pytest.mark.parametrize(
    "operation", [*MODEL_OPERATIONS, *HUMAN_OPERATIONS, "grants", "export", "legacy", "context"]
)
def test_shared_operation_schema_enum_nodes_are_typed_without_new_lifecycle_states(operation):
    schema = operation_schema(operation)
    assert all(node.get("type") == "string" for node in _enum_nodes(schema))
    Draft202012Validator.check_schema(schema)
    if operation == "list":
        assert "conflict" in schema["properties"]["state"]["enum"]


@pytest.mark.parametrize("state", [None, False, 0, [], {}, "conflict"])
async def test_reference_reply_state_validation_does_not_expand_to_list_filter_states(
    tmp_path, state
):
    runtime = engine(tmp_path)
    runtime.personal_memory.invalid_reply = {
        "type": "personal_memory_refs",
        "refs": [],
        "candidate_id": MEMORY,
        "state": state,
    }
    with pytest.raises(ToolExecutionError) as exc:
        await PersonalMemoryTool(runtime.personal_memory).execute("propose", **CREATE)
    assert exc.value.code == "memory_result_unverified"
