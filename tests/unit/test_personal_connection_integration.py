"""PA-03 artifact ancestry, PA-02 memory and actual production assembly boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic_settings import BaseSettings

from naumi_agent.api.app import create_app
from naumi_agent.api.routes.commands import validate_command
from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal import connection_vault
from naumi_agent.personal.artifact_boundary import ArtifactToolProxy, is_protected
from naumi_agent.personal.artifact_models import canonical, digest
from naumi_agent.personal.artifact_store import ArtifactStore
from naumi_agent.personal.connection_tools import PersonalActionsTool
from naumi_agent.personal.connection_vault import ConnectionVault
from naumi_agent.personal.connections import PersonalConnectionService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import ASSIGNABLE_TOOL_NAMES
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import Tool, ToolExecutionError
from tests.unit.test_personal_calendar_sync import seed_event
from tests.unit.test_personal_connection_service import CONNECTION, MemoryBackend, seed_connection
from tests.unit.test_personal_memory_service import actor, confirm, propose
from tests.unit.test_personal_memory_service import memory_context as _memory_context

memory_context = _memory_context
ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / ".naumi/transactions/pa03-calendar-20261105/original"


@pytest.fixture(autouse=True)
def fixture_only_configuration_and_credentials(monkeypatch, tmp_path):
    def sources(cls, settings_cls, init_settings, **_):
        return (init_settings,)

    monkeypatch.setattr(BaseSettings, "settings_customise_sources", classmethod(sources))
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "isolated-runtime-home"))

    def native_trap():
        raise AssertionError("Integration fixtures must not access native credentials")

    monkeypatch.setattr(connection_vault, "_native_backend", native_trap)

    async def no_provider(self, **_):
        raise AssertionError("Integration fixtures must not contact a model provider")

    async def no_stream(self, **_):
        raise AssertionError("Integration fixtures must not stream from a model provider")
        yield

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)


class NoNetwork:
    def __getattr__(self, _name):
        raise AssertionError("Source-authority integration must not contact a provider")


@pytest.fixture
async def integrated(memory_context):
    context = memory_context
    backend = MemoryBackend()
    service = PersonalConnectionService(
        context.engine,
        vault=ConnectionVault(backend),
        adapter=NoNetwork(),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    context.engine.personal_connections = service
    fixture = SimpleNamespace(service=service, engine=context.engine, backend=backend)
    await seed_connection(fixture, write=True)
    await seed_event(fixture)
    context.connections, context.backend = service, backend
    yield context
    await service.stop()


async def graph(context, *, agent=False, with_memory=False, derived=True):
    service, engine = context.connections, context.engine
    identity = context.profiles[0].id if agent else "main"
    if agent:
        current = await service._record("connection", CONNECTION)
        await service.grant(
            CONNECTION,
            {
                "request_id": "grant-original",
                "expected_revision": current["revision"],
                "actor_id": identity,
                "actor_version": context.profiles[0].version,
            },
            trusted_channel="manual_api",
        )
    await actor(context, identity)
    source = (await service.events(CONNECTION, identity))["events"][0]["source_ref"]
    direct = await service._source_artifact(source, identity)
    if not derived:
        return SimpleNamespace(
            actor=identity, direct=direct, derived=direct, note=None, source=source
        )
    snapshot = await engine.personal_artifacts.publish(
        actor_id=identity,
        request_id="derived-one",
        title="派生资料",
        kind="material",
        filename="derived.txt",
        content="仅供当前授权上下文使用的派生内容。".encode(),
        source_refs=[direct],
    )
    derived = engine.personal_artifacts.reference(snapshot)
    snapshot = await engine.personal_artifacts.publish(
        actor_id=identity,
        request_id="derived-two",
        title="二级派生资料",
        kind="material",
        filename="derived-two.txt",
        content="二级派生内容。".encode(),
        source_refs=[derived],
    )
    derived = engine.personal_artifacts.reference(snapshot)
    await engine.personal_artifacts.bind_refs(engine._session.id, identity, [derived])
    note = None
    if with_memory:
        note = await confirm(
            context,
            await propose(
                context,
                owner=identity,
                topic="日历来源约束",
                content="派生记录保留原日历来源。",
                source_artifact_ref=derived,
            ),
        )
        assert (await context.service.get(note["memory_id"], identity))["source_available"] is True
    return SimpleNamespace(actor=identity, direct=direct, derived=derived, note=note, source=source)


async def invalidate(context, mode, references):
    service = context.connections
    current = await service._record("connection", CONNECTION)
    if mode == "disconnect":
        await service.disconnect(
            CONNECTION,
            {
                "request_id": "disconnect-original",
                "expected_revision": current["revision"],
                "remote_revoke": False,
            },
            trusted_channel="manual_api",
        )
    elif mode == "grant":
        await service.grant(
            CONNECTION,
            {
                "request_id": "revoke-original",
                "expected_revision": current["revision"],
                "actor_id": references.actor,
            },
            trusted_channel="manual_api",
            revoke=True,
        )
        current = await service._record("connection", CONNECTION)
        await service.grant(
            CONNECTION,
            {
                "request_id": "replacement-grant",
                "expected_revision": current["revision"],
                "actor_id": references.actor,
                "actor_version": context.engine.personal_agents.get(references.actor).version,
            },
            trusted_channel="manual_api",
        )
    elif mode == "epoch":
        await service._write(
            lambda tx: tx.put(
                "connection",
                {**current, "auth_epoch": current["auth_epoch"] + 1},
                expected_revision=current["revision"],
            )
        )
    elif mode == "key":
        assert service.vault.delete(owner_key=service.owner_key, slot=current["key_slot"]) == {
            "status": "confirmed"
        }
    elif mode == "component":
        del context.engine.personal_connections


@pytest.mark.parametrize("mode", ["disconnect", "grant", "epoch", "key", "component"])
@pytest.mark.parametrize("entrypoint", ["direct", "derived", "session", "memory"])
async def test_current_authority_invalidates_direct_ancestry_session_and_pa02_memory(
    integrated, mode, entrypoint
):
    context = integrated
    refs = await graph(
        context,
        agent=mode == "grant",
        with_memory=entrypoint == "memory",
        derived=entrypoint != "direct",
    )
    await invalidate(context, mode, refs)
    artifacts = context.engine.personal_artifacts
    if entrypoint == "memory":
        view = await context.service.get(refs.note["memory_id"], refs.actor)
        assert (
            view["source_available"] is False and view["content"] is None and view["source"] is None
        )
        assert (await context.service.search("日历来源约束", actor_id=refs.actor))["memories"] == []
    else:
        with pytest.raises(PersonalAgentError):
            if entrypoint == "session":
                await artifacts.session_refs(context.engine._session.id, refs.actor)
            else:
                ref = getattr(refs, entrypoint)
                await artifacts.read(
                    actor_id=refs.actor, artifact_id=ref["artifact_id"], version=ref["version"]
                )


def baseline_store_type():
    path = ORIGINAL / "src/naumi_agent/personal/artifact_store.py"
    if not path.is_file():
        pytest.skip("本地冻结 baseline ArtifactStore 源码未附在此 checkout")
    name = "naumi_agent.personal._pa03_original_artifact_store_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.ArtifactStore


@pytest.mark.parametrize("kind", ["direct", "derived"])
@pytest.mark.parametrize("operation", ["get", "read_content"])
async def test_actual_frozen_old_reader_rejects_new_source_on_independent_copy(
    integrated, tmp_path, kind, operation
):
    context, artifacts = integrated, integrated.engine.personal_artifacts
    refs = await graph(context, derived=kind == "derived")
    plain = await artifacts.publish(
        actor_id="main",
        request_id="plain-control",
        title="普通资料",
        kind="material",
        filename="plain.txt",
        content=b"ordinary non-connection control",
    )
    original_hash = hashlib.sha256(artifacts.store.db_path.read_bytes()).hexdigest()
    target = tmp_path / "baseline-read-copy"
    shutil.copytree(artifacts.store.base_dir, target)
    old = baseline_store_type()(target)
    control = old.read_content(
        workspace_key=artifacts.workspace_key,
        actor_id="main",
        artifact_id=plain["artifact_id"],
        version=1,
    )
    assert control == b"ordinary non-connection control"
    ref = getattr(refs, kind)
    with pytest.raises(PersonalAgentError) as caught:
        getattr(old, operation)(
            workspace_key=artifacts.workspace_key,
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
    assert caught.value.code == "storage_error"
    assert hashlib.sha256(artifacts.store.db_path.read_bytes()).hexdigest() == original_hash
    with sqlite3.connect(old.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)


def raw_marker_update(store, artifact_id, transform):
    with sqlite3.connect(store.db_path) as db:
        row = db.execute(
            "SELECT record_json FROM artifact_versions WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        value = json.loads(row[0])
        transform(value)
        db.execute("DROP TRIGGER artifact_version_no_update")
        db.execute(
            "UPDATE artifact_versions SET record_json=?,record_sha256=? WHERE artifact_id=?",
            (canonical(value), digest(value), artifact_id),
        )
        db.execute(
            "UPDATE artifacts SET head_sha256=? WHERE artifact_id=?", (digest(value), artifact_id)
        )
        db.execute(
            "CREATE TRIGGER artifact_version_no_update BEFORE UPDATE ON artifact_versions "
            "BEGIN SELECT RAISE(ABORT,'immutable version'); END"
        )


@pytest.mark.parametrize("marker", [None, True, "2", 3])
async def test_new_reader_requires_exact_authority_marker_for_connection_evidence(
    integrated, tmp_path, marker
):
    context, artifacts = integrated, integrated.engine.personal_artifacts
    refs = await graph(context, derived=False)
    target = tmp_path / "marker-copy"
    shutil.copytree(artifacts.store.base_dir, target)
    store = ArtifactStore(target)

    def transform(value):
        if marker is None:
            value.pop("authority_version", None)
        else:
            value["authority_version"] = marker

    raw_marker_update(store, refs.direct["artifact_id"], transform)
    with pytest.raises(PersonalAgentError) as caught:
        store.get(
            workspace_key=artifacts.workspace_key,
            actor_id="main",
            artifact_id=refs.direct["artifact_id"],
            version=1,
        )
    assert caught.value.code == "storage_error"


@pytest.mark.parametrize("sources", [{"not": "a list"}, "invalid", [None]])
def test_authority_marker_requires_valid_connection_source_evidence_not_only_truthiness(
    tmp_path, sources
):
    store = ArtifactStore(tmp_path / "malformed-marker")
    try:
        value = store.publish(
            workspace_key="a" * 64,
            actor_id="main",
            request_id="malformed",
            title="非法来源",
            kind="material",
            content=b"source bytes",
            suffix=".txt",
            evidence={"connection_sources": sources},
        )
    except PersonalAgentError:
        return
    with pytest.raises(PersonalAgentError):
        store.get(
            workspace_key="a" * 64, actor_id="main", artifact_id=value["artifact_id"], version=1
        )


@pytest.mark.parametrize("derived", [False, True])
async def test_legacy_plain_artifact_is_readable_without_connection_component(integrated, derived):
    context = integrated
    artifacts = context.engine.personal_artifacts
    plain = await artifacts.publish(
        actor_id="main",
        request_id="plain",
        title="普通资料",
        kind="material",
        filename="plain.txt",
        content=b"ordinary",
    )
    if derived:
        plain = await artifacts.publish(
            actor_id="main",
            request_id="ordinary-derived",
            title="普通派生资料",
            kind="material",
            filename="ordinary-derived.txt",
            content=b"ordinary",
            source_refs=[artifacts.reference(plain)],
        )
    del context.engine.personal_connections
    snapshot, raw = await artifacts.read(
        actor_id="main", artifact_id=plain["artifact_id"], version=1
    )
    assert raw == b"ordinary" and snapshot["artifact_id"] == plain["artifact_id"]


class ProposalProbe(Tool):
    description = "隔离边界测试"
    parameters_schema = {"type": "object"}

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    async def execute(self, **_):
        return "PROPOSAL_PORT_REACHED"


async def test_private_boundary_protects_connection_files_and_allows_only_proposal_ports(
    integrated,
):
    context = integrated
    await graph(context)
    engine, service = context.engine, context.connections

    async def authorized():
        return None

    engine.personal_memory_runtime = SimpleNamespace(enabled=True, authorize=authorized)
    assert is_protected(engine, service.store.db_path)
    assert is_protected(engine, service.boundary)
    for name in ["personal_connections", "personal_actions"]:
        assert (
            await ArtifactToolProxy(engine, ProposalProbe(name), "boundary-probe").execute()
            == "PROPOSAL_PORT_REACHED"
        )


async def test_main_model_tool_has_no_human_approval_operation(integrated):
    tool = PersonalActionsTool(integrated.connections)
    with pytest.raises(ToolExecutionError) as caught:
        await tool.execute(
            operation="approve_action",
            action_id="ca_" + "1" * 32,
            payload={"request_id": "forged", "expected_revision": 1, "expected_digest": "a" * 64},
        )
    assert caught.value.code == "invalid_input"


async def test_actual_engine_composition_router_leaf_tools_and_shared_cli_tui(tmp_path):
    workspace = tmp_path / "engine-workspace"
    workspace.mkdir()
    config = AppConfig(
        _env_file=None,
        workspace_root=str(workspace),
        models={"api_key": None, "catalog_path": None},
        api={"api_keys": ["integration-fixture-key"]},
        personal={"data_dir": str(tmp_path / "engine-personal")},
        memory={
            "session_db_path": str(tmp_path / "engine-state/sessions.db"),
            "vector_db_path": str(tmp_path / "engine-state/vectors"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass"},
        browser_daemon={"enabled": False},
    )
    engine = create_agent_engine(config)
    try:
        assert isinstance(engine.personal_connections, PersonalConnectionService)
        assert isinstance(engine.tool_registry.get("personal_actions"), PersonalActionsTool)
        assert {"personal_connections", "personal_actions"} <= ASSIGNABLE_TOOL_NAMES
        await engine.get_or_create_session()
        app = create_app()
        assert len(app.routes) == 203
        assert len([route for route in app.routes if "/personal/routines" in route.path]) == 10
        assert (
            len(
                [
                    route
                    for route in app.routes
                    if "/personal/connections" in route.path or "/personal/actions" in route.path
                ]
            )
            == 20
        )
        cli = await execute_slash_command(engine, "/connections context")
        frontend_type = type("FixtureTui", (), {"__module__": "naumi_agent.tui.app"})
        tui = await execute_slash_command(engine, "/connections context", frontend=frontend_type())
        assert cli == tui and "google_calendar" in cli and '"client_configured": false' in cli
        assert "actions" in await execute_slash_command(engine, "/actions list")
        names = {item.name for item in COMMANDS_META}
        assert {"/connections", "/actions"} <= names
        for command in ["/connections context", "/actions list"]:
            with pytest.raises(HTTPException):
                validate_command(command)
        engine.personal_connections.start()
        assert engine.personal_connections._worker is not None
        await engine.personal_connections.stop()
        assert engine.personal_connections._closing is True
        assert (
            engine.personal_connections._worker is None
            or engine.personal_connections._worker.done()
        )
    finally:
        await engine.shutdown()


async def action_read_draft(
    context, *, request="manual-action-read", actor_id="main", caller_actor=None
):
    return await context.connections.prepare_action(
        {
            "request_id": request,
            "connection_id": CONNECTION,
            "actor_id": actor_id,
            "source_refs": [],
            "operation": "insert",
            "body": {
                "summary": "ACTION_BODY_SENTINEL_manual_calendar_draft",
                "description": "Only the authorized original action may provide this body.",
                "start": {"dateTime": "2030-01-02T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
                "end": {"dateTime": "2030-01-02T11:00:00+08:00", "timeZone": "Asia/Shanghai"},
            },
            "expires_at": "2030-01-01T12:00:00Z",
        },
        caller_actor=caller_actor,
    )


async def action_read_new_session(context):
    from naumi_agent.memory.session import Session
    from naumi_agent.personal.memory_runtime import PersonalMemoryRuntime

    session = Session(
        title="Action read pin regression", workspace_root=str(context.engine.workspace_root)
    )
    await context.engine.session_store.save(session)
    context.engine._session = session
    context.engine._full_history = session.messages
    context.engine._personal_artifact_refs = []
    context.engine._personal_memory_artifact_refs = []
    context.engine._personal_memory_runtime_refs = []
    context.engine._personal_active_turn = None
    context.engine.personal_memory_runtime = PersonalMemoryRuntime(context.service)
    return session


async def action_read_tool_get(context, action_id):
    raw = await PersonalActionsTool(context.connections).execute(
        operation="get_action", action_id=action_id
    )
    context.engine._session.messages.append(
        {"role": "tool", "tool_call_id": "action-read", "content": raw}
    )
    await context.engine.session_store.save(context.engine._session)
    return json.loads(raw)


async def test_model_action_get_pins_manual_empty_source_draft_and_fences_future_send(integrated):
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    draft = await action_read_draft(context)
    assert draft["source_refs"] == []
    session = await action_read_new_session(context)
    assert await context.engine.personal_artifacts.session_refs(session.id, "main") == []
    shown = await action_read_tool_get(context, draft["id"])
    assert shown["body"]["summary"] == draft["body"]["summary"]
    pinned = await context.engine.personal_artifacts.session_refs(session.id, "main")
    await authorize_execution(context.engine)
    await invalidate(context, "disconnect", SimpleNamespace(actor="main"))
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)
    assert pinned, "Reading an action body must persist a real artifact/session authority pin"


async def test_model_action_list_returns_only_opaque_index_without_unpinned_body(integrated):
    context = integrated
    draft = await action_read_draft(context)
    session = await action_read_new_session(context)
    raw = await PersonalActionsTool(context.connections).execute(operation="list_actions")
    row = next(value for value in json.loads(raw)["actions"] if value["id"] == draft["id"])
    assert "body" not in row, "A model listing must not leak an unpinned action body"
    assert set(row) <= {
        "id",
        "revision",
        "status",
        "digest",
        "operation",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert draft["body"]["summary"] not in raw
    assert draft["body"]["description"] not in raw
    assert await context.engine.personal_artifacts.session_refs(session.id, "main") == []


async def action_read_marker(context, draft, *, actor_id="main"):
    artifacts = context.engine.personal_artifacts
    refs = await artifacts.session_refs(context.engine._session.id, actor_id)
    found = []
    for ref in refs:
        snapshot = await artifacts.get(
            actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
        )
        markers = snapshot["evidence"].get("connection_action_refs", [])
        for marker in markers:
            if marker["action_id"] == draft["id"]:
                found.append((ref, snapshot, marker))
    assert len(found) == 1, "The model-visible action must have exactly one original read marker"
    ref, snapshot, marker = found[0]
    expected = {
        "connection_id": draft["connection_id"],
        "auth_epoch": draft["auth_epoch"],
        "action_id": draft["id"],
        "revision": draft["revision"],
        "digest": draft["digest"],
        "actor_id": draft["actor_id"],
    }
    assert {key: marker[key] for key in expected} == expected
    assert set(marker) == set(expected) | {"grant_id", "grant_revision"}
    assert snapshot["kind"] == "material" and snapshot["suffix"] == ".txt"
    with sqlite3.connect(artifacts.store.db_path) as db:
        row = db.execute(
            "SELECT record_json FROM artifact_versions WHERE artifact_id=? AND version=?",
            (ref["artifact_id"], ref["version"]),
        ).fetchone()
    assert json.loads(row[0])["authority_version"] == 2
    return ref, snapshot, marker


async def test_manual_action_management_dtos_do_not_pin_the_active_model_session(integrated):
    context = integrated
    session = await action_read_new_session(context)
    draft = await action_read_draft(context)
    assert (await context.connections.get_action(draft["id"]))["body"] == draft["body"]
    assert (await context.connections.list_actions())["actions"][0]["body"] == draft["body"]
    assert await context.engine.personal_artifacts.session_refs(session.id, "main") == []
    assert context.engine._personal_artifact_refs == []


async def test_model_action_prepare_pins_its_returned_body_before_the_next_send(integrated):
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    original = await action_read_draft(context)
    await action_read_new_session(context)
    raw = await PersonalActionsTool(context.connections).execute(
        operation="prepare_action",
        payload={
            "request_id": "model-action-prepare",
            "connection_id": CONNECTION,
            "source_refs": [],
            "operation": "insert",
            "body": original["body"],
            "expires_at": original["expires_at"],
        },
    )
    draft = json.loads(raw)
    await action_read_marker(context, draft)
    await authorize_execution(context.engine)
    await invalidate(context, "disconnect", SimpleNamespace(actor="main"))
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)


async def test_model_action_reconcile_terminal_shortcut_pins_the_original_returned_body(integrated):
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    draft = await action_read_draft(context)
    # This fixture injects only an already-recorded terminal fact; no execution or
    # provider-verification acceptance is claimed by this shortcut branch test.
    terminal = await context.connections._write(
        lambda tx: context.connections._change(tx, "action", draft["id"], {"status": "verified"})
    )
    await action_read_new_session(context)
    raw = await PersonalActionsTool(context.connections).execute(
        operation="reconcile",
        action_id=draft["id"],
        payload={
            "request_id": "model-terminal-reconcile",
            "expected_revision": terminal["revision"],
        },
    )
    shown = json.loads(raw)
    assert shown["status"] == "verified"
    await action_read_marker(context, shown)
    await authorize_execution(context.engine)
    await invalidate(context, "disconnect", SimpleNamespace(actor="main"))
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)


async def test_model_action_get_rejects_a_different_actor_without_pin_or_body(integrated):
    context = integrated
    draft = await action_read_draft(context)
    session = await actor(context, context.profiles[0].id)
    with pytest.raises(ToolExecutionError) as caught:
        await PersonalActionsTool(context.connections).execute(
            operation="get_action", action_id=draft["id"]
        )
    assert caught.value.code == "not_found"
    assert (
        await context.engine.personal_artifacts.session_refs(session.id, context.profiles[0].id)
        == []
    )
    assert draft["body"]["summary"] not in json.dumps(session.messages)


@pytest.mark.parametrize("dependency", ["artifact_grant", "memory_grant", "memory_delete"])
async def test_action_read_new_session_retains_original_dependency_authority(
    integrated, dependency
):
    from naumi_agent.personal.runtime import authorize_execution
    from tests.unit.test_personal_memory_service import artifact_share, create, revoke, share

    context = integrated
    artifacts = context.engine.personal_artifacts
    await action_read_new_session(context)
    original_grant = note = None
    if dependency == "artifact_grant":
        owner = context.profiles[0].id
        source = await artifacts.publish(
            actor_id=owner,
            request_id="action-private-source",
            title="ACTION_SOURCE_TITLE_SENTINEL",
            kind="material",
            filename="private-source.txt",
            content=b"Original privately shared action source.",
        )
        original_grant, ref = await artifact_share(context, source, "main")
        await artifacts.bind_refs(context.engine._session.id, "main", [ref])
    else:
        owner = context.profiles[0].id if dependency == "memory_grant" else "main"
        note = await create(
            context,
            owner=owner,
            request="action-memory-source",
            topic="Action source rule",
            content="ACTION_MEMORY_SENTINEL",
        )
        if dependency == "memory_grant":
            original_grant = await share(context, note, "main")
        await context.engine.personal_memory_runtime.prepare_messages([])
        assert any(
            ref["memory_id"] == note["memory_id"]
            for ref in context.engine._personal_memory_runtime_refs
        )
    draft = await action_read_draft(context, caller_actor="main")
    assert draft["artifact_refs"] if dependency == "artifact_grant" else draft["memory_refs"]
    reader = await action_read_new_session(context)
    shown = await action_read_tool_get(context, draft["id"])
    assert shown["body"] == draft["body"]
    await action_read_marker(context, shown)
    # A fresh reader has no memory-recall turn state. Its persisted action artifact
    # must independently enforce the original action's dependency grants.
    assert context.engine._personal_memory_runtime_refs == []
    await authorize_execution(context.engine)
    if dependency == "artifact_grant":
        await artifacts.revoke(
            actor_id=original_grant["owner_id"],
            grant_id=original_grant["grant_id"],
            expected_revision=original_grant["revision"],
            request_id="revoke-action-artifact-source",
        )
    elif dependency == "memory_grant":
        await revoke(context, original_grant, "revoke-action-memory-source")
    else:
        await context.service.dispatch(
            "delete",
            {
                "request_id": "delete-action-memory-source",
                "memory_id": note["memory_id"],
                "expected_revision": note["revision"],
            },
            actor_id="main",
            trusted_channel="management_web",
        )
    context.engine._personal_artifact_refs = []
    context.engine._session = await context.engine.session_store.load(reader.id)
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)


@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("operation", ["get", "read"])
async def test_missing_connection_component_rejects_action_artifact_and_ancestry(
    integrated, derived, operation
):
    context = integrated
    draft = await action_read_draft(context)
    await action_read_new_session(context)
    await action_read_tool_get(context, draft["id"])
    ref, _, _ = await action_read_marker(context, draft)
    artifacts = context.engine.personal_artifacts
    if derived:
        result = await artifacts.publish(
            actor_id="main",
            request_id="action-marker-derived",
            title="Action derived source",
            kind="material",
            filename="derived-action.txt",
            content=b"Action-derived text.",
            source_refs=[ref],
        )
        ref = artifacts.reference(result)
    del context.engine.personal_connections
    with pytest.raises(PersonalAgentError) as caught:
        await getattr(artifacts, operation)(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
    assert caught.value.code == "connection_source_revoked"


@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("operation", ["get", "read_content"])
async def test_real_old_reader_rejects_action_marker_on_independent_copy(
    integrated, tmp_path, derived, operation
):
    context = integrated
    draft = await action_read_draft(context)
    await action_read_new_session(context)
    await action_read_tool_get(context, draft["id"])
    ref, _, _ = await action_read_marker(context, draft)
    artifacts = context.engine.personal_artifacts
    if derived:
        value = await artifacts.publish(
            actor_id="main",
            request_id="old-reader-action-derived",
            title="Action ancestry",
            kind="material",
            filename="action-ancestry.txt",
            content=b"Derived action ancestry.",
            source_refs=[ref],
        )
        ref = artifacts.reference(value)
    plain = await artifacts.publish(
        actor_id="main",
        request_id="action-old-reader-control",
        title="Ordinary control",
        kind="material",
        filename="plain-control.txt",
        content=b"ordinary action-reader control",
    )
    original_hash = hashlib.sha256(artifacts.store.db_path.read_bytes()).hexdigest()
    target = tmp_path / "old-action-reader-copy"
    shutil.copytree(artifacts.store.base_dir, target)
    old = baseline_store_type()(target)
    assert (
        old.read_content(
            workspace_key=artifacts.workspace_key,
            actor_id="main",
            artifact_id=plain["artifact_id"],
            version=1,
        )
        == b"ordinary action-reader control"
    )
    with pytest.raises(PersonalAgentError) as caught:
        getattr(old, operation)(
            workspace_key=artifacts.workspace_key,
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
    assert caught.value.code == "storage_error"
    assert hashlib.sha256(artifacts.store.db_path.read_bytes()).hexdigest() == original_hash
    with sqlite3.connect(old.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_marker",
        "old_marker",
        "bool_marker",
        "refs_object",
        "missing_digest",
        "bool_revision",
        "grant_revision_mismatch",
    ],
)
async def test_new_reader_rejects_action_marker_downgrade_and_malformed_refs(
    integrated, tmp_path, corruption
):
    context = integrated
    draft = await action_read_draft(context)
    await action_read_new_session(context)
    await action_read_tool_get(context, draft["id"])
    ref, _, _ = await action_read_marker(context, draft)
    artifacts = context.engine.personal_artifacts
    target = tmp_path / "corrupt-action-marker-copy"
    shutil.copytree(artifacts.store.base_dir, target)
    store = ArtifactStore(target)

    def transform(value):
        if corruption == "missing_marker":
            value.pop("authority_version")
        elif corruption == "old_marker":
            value["authority_version"] = 1
        elif corruption == "bool_marker":
            value["authority_version"] = True
        elif corruption == "refs_object":
            value["evidence"]["connection_action_refs"] = {"not": "a list"}
        else:
            marker = value["evidence"]["connection_action_refs"][0]
            if corruption == "missing_digest":
                marker.pop("digest")
            elif corruption == "bool_revision":
                marker["revision"] = True
            else:
                marker["grant_revision"] = 1

    raw_marker_update(store, ref["artifact_id"], transform)
    with pytest.raises(PersonalAgentError) as caught:
        store.get(
            workspace_key=artifacts.workspace_key,
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
    assert caught.value.code == "storage_error"


@pytest.mark.parametrize("markers", [None, "invalid", [None], [{"action_id": "ca_" + "1" * 32}]])
def test_artifact_writer_rejects_malformed_action_reference_evidence(tmp_path, markers):
    store = ArtifactStore(tmp_path / "malformed-action-reference")
    with pytest.raises(PersonalAgentError):
        store.publish(
            workspace_key="a" * 64,
            actor_id="main",
            request_id="malformed-action-pin",
            title="Malformed action source",
            kind="material",
            content=b"Malformed action reference body",
            suffix=".txt",
            evidence={"connection_action_refs": markers},
        )


async def test_action_read_old_revision_allows_revise_and_approval_without_self_cycle(integrated):
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    draft = await action_read_draft(context)
    reader = await action_read_new_session(context)
    await action_read_tool_get(context, draft["id"])
    _, _, original_marker = await action_read_marker(context, draft)
    changed = await context.connections.revise_action(
        draft["id"],
        {
            "request_id": "revise-after-original-model-read",
            "expected_revision": draft["revision"],
            "body": {**draft["body"], "summary": "Revised approved action body"},
            "expires_at": draft["expires_at"],
        },
        caller_actor="main",
    )
    assert changed["digest"] != original_marker["digest"]
    assert changed["revision"] > original_marker["revision"]
    await authorize_execution(context.engine)
    context.engine.config.safety = SimpleNamespace(permission_mode="strict")
    # Stop the actual service lifecycle before approval: this case validates the
    # persisted approval/intent boundary, not provider submission or Job execution.
    await context.connections.stop()
    approved = await context.connections.approve_action(
        changed["id"],
        {
            "request_id": "approve-revised-after-model-read",
            "expected_revision": changed["revision"],
            "expected_digest": changed["digest"],
        },
        trusted_channel="manual_api",
    )
    assert approved["status"] == "approved"
    assert approved["approval"]["digest"] == changed["digest"]
    record = await context.connections._record("action", approved["id"])
    assert record["execution"]["payload"]["task_id"] == approved["id"]
    await authorize_execution(context.engine)
    context.engine._session = await context.engine.session_store.load(reader.id)
    context.engine._personal_artifact_refs = []
    await invalidate(context, "disconnect", SimpleNamespace(actor="main"))
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)


async def test_historical_action_pin_is_read_authority_not_expiry_or_write_authority(integrated):
    from naumi_agent.personal.google_oauth import READ_SCOPE
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    draft = await action_read_draft(context)
    await action_read_new_session(context)
    await action_read_tool_get(context, draft["id"])
    await action_read_marker(context, draft)
    original = await context.connections._record("connection", CONNECTION)
    await context.connections._write(
        lambda tx: tx.put(
            "connection",
            {
                **original,
                "can_write": False,
                "scopes": ["openid", "https://www.googleapis.com/auth/userinfo.email", READ_SCOPE],
            },
            expected_revision=original["revision"],
        )
    )
    context.connections.clock = lambda: datetime(2030, 1, 3, tzinfo=UTC)
    await authorize_execution(context.engine)
    await invalidate(context, "disconnect", SimpleNamespace(actor="main"))
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)


async def test_action_read_calendar_grant_pin_never_substitutes_a_replacement_grant(integrated):
    from naumi_agent.personal.memory_runtime import PersonalMemoryRuntime
    from naumi_agent.personal.runtime import authorize_execution

    context = integrated
    identity = context.profiles[0].id
    current = await context.connections._record("connection", CONNECTION)
    await context.connections.grant(
        CONNECTION,
        {
            "request_id": "action-original-calendar-grant",
            "expected_revision": current["revision"],
            "actor_id": identity,
            "actor_version": context.profiles[0].version,
        },
        trusted_channel="manual_api",
    )
    draft = await action_read_draft(context, actor_id=identity)
    await actor(context, identity)
    context.engine.personal_memory_runtime = PersonalMemoryRuntime(context.service)
    await action_read_tool_get(context, draft["id"])
    _, _, marker = await action_read_marker(context, draft, actor_id=identity)
    assert marker["grant_id"] and marker["grant_revision"] >= 1
    await authorize_execution(context.engine)
    await invalidate(context, "grant", SimpleNamespace(actor=identity))
    current = await context.connections._record("connection", CONNECTION)
    replacement = current["grants"][context.connections.workspace_key + ":" + identity]
    assert replacement["id"] != marker["grant_id"]
    with pytest.raises(PersonalAgentError):
        await authorize_execution(context.engine)
