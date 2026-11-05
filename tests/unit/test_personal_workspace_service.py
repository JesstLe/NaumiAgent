"""Two real runtime stores with authenticated local transport, no external provider."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.personal import runtime
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_models import endpoint_url
from naumi_agent.personal.workspace_transport import request_signature, response_signature
from naumi_agent.personal.workspace_vault import WorkspaceVault
from naumi_agent.personal.workspaces import PersonalWorkspaceService
from naumi_agent.runtime.composition import create_agent_engine
from tests.unit.test_personal_today_authority import authority_system as _authority_system
from tests.unit.test_personal_today_authority import make_item, revoke, shared_item

authority_system = _authority_system
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class NativeStoreFixture:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class LocalAuthenticatedTransport:
    def __init__(self, source):
        self.source, self.calls, self.offline = source, [], False

    async def request(self, endpoint, secret, arguments, trace):
        endpoint_url(endpoint)
        self.calls.append(copy.deepcopy(arguments))
        if self.offline:
            raise PersonalAgentError("source_unavailable", "offline fixture")
        nonce, stamp = secrets.token_hex(16), str(int(time.time()))
        signature = request_signature(secret, nonce, stamp, arguments, trace)
        result = await self.source.federation(
            arguments, token=f"NaumiWorkspace {nonce}:{stamp}:{signature}", trace=trace
        )
        proof = result.pop("proof")
        assert proof["nonce"] == nonce
        assert hmac.compare_digest(response_signature(secret, nonce, result), proof["signature"])
        return result


@pytest.fixture
async def federation_system(authority_system):
    source = authority_system
    hub_root = source.root / "hub-workspace"
    hub_root.mkdir()
    hub = create_agent_engine(
        AppConfig(
            workspace_root=str(hub_root),
            personal={"data_dir": str(source.root / "hub-personal")},
            memory={
                "session_db_path": str(source.root / "hub-runtime/sessions.db"),
                "vector_db_path": str(source.root / "hub-vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "bypass", "max_turns": 3, "max_parallel_agents": 1},
        )
    )
    backend = NativeStoreFixture()
    source.engine.personal_workspaces = source_service = PersonalWorkspaceService(
        source.engine, vault=WorkspaceVault(backend), clock=lambda: source.clock[0]
    )
    transport = LocalAuthenticatedTransport(source_service)
    hub.personal_workspaces = hub_service = PersonalWorkspaceService(
        hub, vault=WorkspaceVault(backend), transport=transport, clock=lambda: source.clock[0]
    )
    hub._session = await hub.create_session(title="Workspace hub fixture", model="no-provider")
    env = SimpleNamespace(
        source=source,
        hub=hub,
        source_service=source_service,
        service=hub_service,
        transport=transport,
        backend=backend,
    )
    try:
        yield env
    finally:
        await hub.shutdown()


async def human(service, operation, **fields):
    return await service.execute_operation(
        {"operation": operation, **fields}, trusted_channel="web"
    )


async def pair(env, *, actor="main", source_actor="main", scopes=None, key="pair"):
    recipient = (await human(env.service, "recipient", actor_id=actor))["recipient"]
    version = env.source_service._actor_profile(source_actor)["version"]
    issue = {
        "request_id": key + "-issue",
        "recipient": recipient,
        "source_actor_id": source_actor,
        "source_actor_version": version,
        "scopes": scopes or ["items", "results"],
        "expires_at": (env.source.clock[0] + timedelta(days=1)).isoformat(),
    }
    granted = await human(env.source_service, "issue", payload=issue)
    connect = {
        "request_id": key + "-connect",
        "endpoint": "http://127.0.0.1:54321",
        "label": "工作资料",
        "ticket": granted["ticket"],
    }
    connected = await human(env.service, "connect", payload=connect)
    return granted, connected, issue, connect


def business_rows(engine):
    result = {}
    for path in engine.personal_agents.store.base_dir.rglob("*.db"):
        if path.name in {"personal_workspaces.db", "personal_artifacts.db"}:
            continue
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ):
                if name.startswith("sqlite_"):
                    continue
                rows = db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                result[str(path) + ":" + name] = hashlib.sha256(
                    repr(sorted(rows, key=repr)).encode()
                ).hexdigest()
    return result


async def test_explicit_pair_reads_real_source_without_execution_or_implicit_owner_access(
    federation_system,
):
    env = federation_system
    item = await make_item(env.source, "calendar-preparation")
    assert (await env.service.overview())["sources"] == []
    env.source.engine.personal_agents.list()
    before = business_rows(env.source.engine)
    granted, connected, issue, connect = await pair(env)
    assert granted["ticket"]["source"] != granted["ticket"]["recipient"]
    assert (await human(env.source_service, "issue", payload=issue))["ticket"] == granted["ticket"]
    assert (await human(env.service, "connect", payload=connect))["connection"] == connected[
        "connection"
    ]
    view = await env.service.overview()
    assert view["partial"] is False and len(view["sources"]) == 1
    entry = view["sources"][0]["entries"][0]
    assert entry["title"] == item["definition"]["title"]
    assert entry["execution"] is None and entry["state"] == "active"
    assert before == business_rows(env.source.engine)
    assert env.source.items._worker is env.hub.personal_items._worker is None
    raw = env.source_service.store.db_path.read_bytes() + env.service.store.db_path.read_bytes()
    assert granted["ticket"]["secret"].encode() not in raw
    assert granted["ticket"]["secret"] not in canonical(await env.service.list())


async def test_lazy_context_and_direct_model_management_denied(federation_system):
    env = federation_system
    assert not env.service.store.db_path.exists()
    assert (await env.service.context("main"))["configured"] is False
    assert not env.service.store.db_path.exists() and not env.service.boundary.exists()
    assert (await env.service.list(caller_actor="main")) == {"connections": []}
    for method in (
        env.service.issue,
        env.service.revoke,
        env.service.connect,
        env.service.disconnect,
    ):
        with pytest.raises(PersonalAgentError) as exc:
            await method({})
        assert exc.value.code == "scope_denied"
    for operation in (
        "recipient",
        "grants",
        "requests",
        "issue",
        "revoke",
        "connect",
        "disconnect",
    ):
        with pytest.raises(PersonalAgentError):
            await env.service.execute_operation({"operation": operation}, caller_actor="main")
    assert not env.service.store.db_path.exists()


async def test_exact_recipient_agent_source_agent_and_versions_are_distinct(federation_system):
    env = federation_system
    target = env.hub.personal_agents.create(
        name="Mira", instructions="自己的事项", request_id="target"
    )
    source_actor = env.source.engine.personal_agents.create(
        name="Echo", instructions="原资料", request_id="source"
    )
    private = await make_item(
        env.source, "private-echo", actor=source_actor.id, version=source_actor.version
    )
    await make_item(env.source, "main-only")
    _, connected, _, _ = await pair(env, actor=target.id, source_actor=source_actor.id)
    assert (await env.service.list(caller_actor="main"))["connections"] == []
    assert (await env.service.overview(caller_actor="main"))["sources"] == []
    with pytest.raises(PersonalAgentError):
        await env.service.overview(connection_id=connected["connection"]["id"], caller_actor="main")
    values = await env.service.overview(caller_actor=target.id)
    assert [v["item_id"] for v in values["sources"][0]["entries"]] == [private["item_id"]]
    env.hub.personal_agents.update(target.id, target.version, instructions="修改职责")
    values = await env.service.overview(caller_actor=target.id)
    assert values["partial"] and values["sources"][0]["entries"] == []


async def test_revoked_source_material_hides_title_and_revokes_already_pinned_model_bytes(
    federation_system,
):
    env = federation_system
    owner, _, material_grant, item = await shared_item(env.source)
    await pair(env)
    result = await env.service.model_result(
        "overview", {"tampered": True}, "main", {"operation": "overview"}
    )
    assert result["sources"][0]["entries"][0]["title"] == item["definition"]["title"]
    refs = await env.hub.personal_artifacts.session_refs(env.hub._session.id, "main")
    assert len(refs) == 1
    snapshot, content = await env.hub.personal_artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
    )
    assert (
        json.loads(content) == result and snapshot["sha256"] == hashlib.sha256(content).hexdigest()
    )
    await runtime.authorize_execution(env.hub)
    await revoke(env.source, owner, material_grant)
    assert (await env.service.overview())["sources"][0]["entries"] == []
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.hub)
    with pytest.raises(PersonalAgentError):
        await env.hub.personal_artifacts.read(
            actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
        )


@pytest.mark.parametrize(
    "change", ["revoke", "disconnect", "expire", "offline", "source_version", "missing_pin"]
)
async def test_loss_of_source_clears_body_and_blocks_next_model_send(federation_system, change):
    env = federation_system
    actor = env.source.engine.personal_agents.create(
        name="Source", instructions="明确资料", request_id="source-agent"
    )
    await make_item(env.source, actor=actor.id, version=actor.version)
    granted, connected, _, _ = await pair(env, source_actor=actor.id)
    await env.service.model_result("overview", {}, "main", {"operation": "overview"})
    if change == "revoke":
        await human(
            env.source_service,
            "revoke",
            payload={
                "request_id": "revoke",
                "grant_id": granted["grant"]["id"],
                "expected_revision": 1,
            },
        )
    elif change == "disconnect":
        await human(
            env.service,
            "disconnect",
            payload={
                "request_id": "disconnect",
                "connection_id": connected["connection"]["id"],
                "expected_revision": 1,
            },
        )
    elif change == "expire":
        env.source.clock[0] += timedelta(days=2)
    elif change == "offline":
        env.transport.offline = True
    elif change == "source_version":
        env.source.engine.personal_agents.update(
            actor.id, actor.version, instructions="新的授权范围"
        )
    else:
        env.source_service.boundary.unlink()
    view = await env.service.overview()
    assert not any(v["entries"] for v in view["sources"])
    if view["sources"]:
        assert view["partial"] and view["sources"][0]["availability"] == "unavailable"
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.hub)


async def test_missing_workspace_component_fails_closed_for_derived_material(
    federation_system, monkeypatch
):
    env = federation_system
    await make_item(env.source)
    await pair(env)
    await env.service.model_result("overview", {}, "main", {"operation": "overview"})
    ref = (await env.hub.personal_artifacts.session_refs(env.hub._session.id, "main"))[0]
    derived = await env.hub.personal_artifacts.publish(
        actor_id="main",
        request_id="derive-federation",
        title="整理结果",
        kind="material",
        filename="derived.txt",
        content=b"Derived material",
        source_refs=[ref],
    )
    monkeypatch.setattr(env.hub, "personal_workspaces", None)
    for value in (ref, derived):
        with pytest.raises(PersonalAgentError):
            await env.hub.personal_artifacts.get(
                actor_id="main", artifact_id=value["artifact_id"], version=1
            )


async def test_untrusted_dto_is_not_provenance_and_session_generation_is_rechecked(
    federation_system, monkeypatch
):
    env = federation_system
    await make_item(env.source)
    await pair(env)
    result = await env.service.model_result(
        "overview", {"sources": [{"title": "FORGED"}]}, "main", {"operation": "overview"}
    )
    assert "FORGED" not in canonical(result)
    original = env.service._read_authority

    async def replace_session(record, actor):
        await original(record, actor)
        env.hub._session = await env.hub.create_session(
            title="Replacement session", model="no-provider"
        )

    monkeypatch.setattr(env.service, "_read_authority", replace_session)
    with pytest.raises(PersonalAgentError):
        await env.service.model_result("overview", {}, "main", {"operation": "overview"})


async def test_reopen_keeps_enrollment_and_original_connection_without_starting_workers(
    federation_system,
):
    env = federation_system
    item = await make_item(env.source)
    granted, connected, _, _ = await pair(env)
    env.source.engine.personal_workspaces = env.source_service = PersonalWorkspaceService(
        env.source.engine, vault=WorkspaceVault(env.backend), clock=lambda: env.source.clock[0]
    )
    env.transport.source = env.source_service
    env.hub.personal_workspaces = env.service = PersonalWorkspaceService(
        env.hub,
        vault=WorkspaceVault(env.backend),
        transport=env.transport,
        clock=lambda: env.source.clock[0],
    )
    view = await env.service.overview()
    assert view["sources"][0]["entries"][0]["item_id"] == item["item_id"]
    assert len((await env.service.list())["connections"]) == 1
    assert (await env.service.request_status("pair-connect"))["identity"]["id"] == connected[
        "connection"
    ]["id"]
    assert (await env.source_service.request_status("pair-issue"))["identity"]["id"] == granted[
        "grant"
    ]["id"]
    assert env.source.items._worker is env.hub.personal_items._worker is None


async def test_concurrent_duplicate_connect_has_one_connection_and_one_receipt(federation_system):
    env = federation_system
    await make_item(env.source)
    _, connected, _, connect = await pair(env)
    results = await asyncio.gather(
        *(human(env.service, "connect", payload=connect) for _ in range(6))
    )
    assert all(v["identity"] == connected["identity"] for v in results)
    assert len((await env.service.list())["connections"]) == 1
    changed = copy.deepcopy(connect)
    changed["label"] = "其他内容"
    with pytest.raises(PersonalAgentError) as exc:
        await human(env.service, "connect", payload=changed)
    assert exc.value.code == "idempotency_conflict"
    changed["request_id"] = "duplicate-new-request"
    with pytest.raises(PersonalAgentError):
        await human(env.service, "connect", payload=changed)


async def test_source_hmac_wrong_nonce_replay_tamper_trace_and_recipient_fail(federation_system):
    env = federation_system
    granted, _, _, _ = await pair(env)
    ticket = granted["ticket"]
    args = {
        "operation": "inspect",
        "grant_id": ticket["grant_id"],
        "recipient": ticket["recipient"],
    }
    nonce, stamp = secrets.token_hex(16), str(int(time.time()))
    sig = request_signature(ticket["secret"], nonce, stamp, args, [])
    header = f"NaumiWorkspace {nonce}:{stamp}:{sig}"
    response = await env.source_service.federation(args, token=header)
    assert "secret" not in canonical(response)
    for arguments, proof, trace in (
        (args, header, []),
        ({**args, "operation": "page"}, header, []),
        (args, header, [env.source_service._trace_marker()]),
        (args, "Bearer " + ticket["secret"], []),
        (args, f"NaumiWorkspace {nonce}:{int(stamp) - 120}:{sig}", []),
    ):
        with pytest.raises(PersonalAgentError):
            await env.source_service.federation(arguments, token=proof, trace=trace)
    assert (await env.source_service.request_status("unknown-request"))["status"] == "missing"


async def test_empty_filtered_keyset_page_keeps_raw_continuation(federation_system):
    env = federation_system
    owner, _, material_grant, _ = await shared_item(env.source)
    await make_item(env.source, "visible-source")
    await pair(env)
    await revoke(env.source, owner, material_grant)
    view = await env.service.overview(limit=1)
    seen = {v["item_id"] for v in view["sources"][0]["entries"]}
    after = view["sources"][0]["next_after"]
    if after:
        second = await env.service.overview(
            connection_id=view["sources"][0]["connection_id"], after=after, limit=1
        )
        seen.update(v["item_id"] for v in second["sources"][0]["entries"])
    assert len(seen) == 1


async def test_items_only_grant_does_not_read_results(federation_system):
    env = federation_system
    item = await make_item(env.source)
    _, connected, _, _ = await pair(env, scopes=["items"])
    result = await env.service.result(
        connection_id=connected["connection"]["id"], item_id=item["item_id"]
    )
    assert result["availability"] == "unavailable" and result["result"] is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:80",
        "http://localhost:80",
        "http://127.1:80",
        "http://2130706433:80",
        "http://127.0.0.1:80/path",
        "http://127.0.0.1:80?token=x",
        "http://127.0.0.1:80#x",
        "http://user@127.0.0.1:80",
        "http://[::ffff:127.0.0.1]:80",
        " http://127.0.0.1:80",
    ],
)
def test_endpoint_is_literal_loopback_without_any_proxy_authority(endpoint):
    with pytest.raises(PersonalAgentError):
        endpoint_url(endpoint)
