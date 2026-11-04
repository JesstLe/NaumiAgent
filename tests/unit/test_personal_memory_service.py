"""Actual memory/catalog/session/artifact SQLite with an explicit index-only port.

No Engine, model provider or embedding implementation is replaced or exercised here.
The index port records the candidate boundary; semantic quality belongs to integration.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.personal.artifacts import PersonalArtifactService
from naumi_agent.personal.memories import PersonalMemoryService
from naumi_agent.personal.memory_models import content_digest, reference
from naumi_agent.personal.memory_tools import validate_operation_payload
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.tools.base import ToolRegistry


class IndexPort:
    def __init__(self):
        self.keys, self.candidates, self.reconciliations = set(), [], []
        self.unavailable = False

    def status(self):
        return {"status": "unavailable" if self.unavailable else "ready", "error": ""}

    def _guard(self):
        if self.unavailable:
            raise PersonalAgentError("memory_index_unavailable", "语义索引暂未就绪")

    def upsert(self, record):
        self._guard()
        self.delete(record["memory_id"])
        self.keys.add((record["memory_id"], record["revision"], record["content_sha256"]))

    def delete(self, memory_id):
        self._guard()
        self.keys = {key for key in self.keys if key[0] != memory_id}

    def ensure_current(self, records):
        self._guard()
        self.reconciliations.append([record["memory_id"] for record in records])
        repaired = 0
        for record in records:
            key = (record["memory_id"], record["revision"], record["content_sha256"])
            if key not in self.keys:
                self.upsert(record)
                repaired += 1
        return repaired

    def query(self, query, records, *, limit):
        self._guard()
        self.candidates = [record["memory_id"] for record in records]
        return []

    def reconcile(self, records):
        self._guard()
        active = {
            (record["memory_id"], record["revision"], record["content_sha256"])
            for record in records
        }
        stale = self.keys - active
        self.keys -= stale
        return {"repaired": self.ensure_current(records), "removed": len(stale)}


@pytest.fixture
async def memory_context(tmp_path):
    opened = []

    async def make(name="alpha", *, owner=None):
        workspace = tmp_path / name
        workspace.mkdir(exist_ok=True)
        state = tmp_path / (name + "-state")
        state.mkdir(exist_ok=True)
        config = SimpleNamespace(
            memory=MemoryConfig(
                session_db_path=str(state / "sessions.db"),
                vector_db_path=str(state / "legacy-vectors"),
                long_term_enabled=True,
            )
        )
        engine = SimpleNamespace(
            config=config,
            workspace_root=workspace,
            tool_registry=ToolRegistry(),
            router=SimpleNamespace(resolve_model=lambda tier: "controlled-model"),
            _compactor=SimpleNamespace(_long_term_memory=None),
            _personal_artifact_refs=[],
            _personal_memory_artifact_refs=[],
            _personal_memory_runtime_refs=[],
            personal_memory_runtime=SimpleNamespace(enabled=True),
        )
        engine.session_store = SessionStore(config.memory)
        engine.personal_agents = PersonalAgentService(
            engine, PersonalAgentStore((owner or tmp_path / "owner") / "personal")
        )
        profiles = [
            engine.personal_agents.create(
                request_id="memory-agent-" + str(i),
                name=name,
                instructions="按用户委托处理资料。",
            )
            for i, name in enumerate(("Mira", "Echo", "山海"))
        ]
        session = Session(title="记忆", workspace_root=str(workspace))
        await engine.session_store.save(session)
        engine._session, engine._full_history = session, []
        engine.personal_artifacts = PersonalArtifactService(engine)
        items = {}

        async def item(identity):
            if identity not in items:
                raise PersonalAgentError("not_found", "该事项不存在")
            return copy.deepcopy(items[identity])

        engine.personal_items = SimpleNamespace(_item=item)
        index = IndexPort()
        engine.personal_memory = PersonalMemoryService(engine, index=index)
        result = SimpleNamespace(
            engine=engine,
            service=engine.personal_memory,
            index=index,
            profiles=profiles,
            items=items,
            main_session=session,
        )
        opened.append(result)
        return result

    first = await make()
    first.make = make
    try:
        yield first
    finally:
        for context in opened:
            await context.service.stop()
            await context.engine.session_store.close()


async def actor(context, identity="main"):
    if identity == "main":
        session = context.main_session
    else:
        opened = await context.engine.personal_agents.open_session(identity)
        session = await context.engine.session_store.load(opened["session_id"])
    context.engine._session = session
    context.engine._full_history = session.messages
    context.engine._personal_active_turn = None
    return session


async def messages(context, values):
    context.engine._session.messages = copy.deepcopy(values)
    await context.engine.session_store.save(context.engine._session)
    context.engine._full_history = context.engine._session.messages


async def create(
    context,
    request="create-one",
    *,
    owner="main",
    scope="workspace",
    topic="回复语言",
    content="使用中文回答",
    category="preference",
    item_id=None,
):
    return await context.service.dispatch(
        "create",
        {
            "request_id": request,
            "topic": topic,
            "category": category,
            "content": content,
            "scope": scope,
            **({"item_id": item_id} if item_id else {}),
        },
        actor_id=owner,
        trusted_channel="management_web",
    )


async def propose(
    context,
    request="propose-one",
    *,
    owner="main",
    topic="输出方式",
    content="简洁回答",
    scope="workspace",
    **source,
):
    reply = await context.service.dispatch(
        "propose",
        {
            "request_id": request,
            "topic": topic,
            "category": "preference",
            "content": content,
            "scope": scope,
            **source,
        },
        actor_id=owner,
        caller_actor=owner,
    )
    return await context.service.get(reply["candidate_id"], actor_id=owner)


async def confirm(context, candidate, request="confirm-one"):
    return await context.service.dispatch(
        "confirm",
        {
            "request_id": request,
            "memory_id": candidate["memory_id"],
            "expected_revision": candidate["revision"],
            "expected_content_sha256": candidate["content_sha256"],
            "expected_active_ref": candidate.get("active_ref"),
        },
        actor_id=candidate["actor_id"],
        trusted_channel="management_web",
    )


async def share(context, memory, recipient, request="share-one"):
    return await context.service.dispatch(
        "share",
        {
            "request_id": request,
            "memory_id": memory["memory_id"],
            "expected_revision": memory["revision"],
            "expected_content_sha256": memory["content_sha256"],
            "recipient_actor_id": recipient,
            "recipient_version": 0
            if recipient == "main"
            else context.engine.personal_agents.get(recipient).version,
        },
        actor_id=memory["actor_id"],
        trusted_channel="management_web",
    )


async def revoke(context, grant, request="revoke-one"):
    return await context.service.dispatch(
        "revoke",
        {
            "request_id": request,
            "grant_id": grant["grant_id"],
            "expected_revision": grant["revision"],
        },
        actor_id=grant["actor_id"],
        trusted_channel="management_web",
    )


def execute(context, identity, *, origin="main", origins=None, refs=None, item_id=None):
    engine = context.engine
    engine._isolated_child = True
    engine._personal_memory_parent_enabled = True
    engine._personal_memory_execution_context = {
        "actor_id": identity,
        "origin_actor_id": origin,
        "origin_actor_ids": origins or [],
        "workspace_key": context.service.workspace_key,
        "item_id": item_id,
        "artifact_refs": refs or [],
    }


def end_execution(context):
    context.engine._isolated_child = False
    context.engine._personal_memory_execution_context = None


async def artifact(context, owner, request="artifact-one"):
    snapshot = await context.engine.personal_artifacts.publish(
        actor_id=owner,
        request_id=request,
        title="预算",
        kind="material",
        filename="budget.txt",
        content="预算为825元".encode(),
    )
    return snapshot, context.engine.personal_artifacts.reference(snapshot)


async def artifact_share(context, snapshot, recipient, request="artifact-share"):
    grant = await context.engine.personal_artifacts.share(
        actor_id=snapshot["owner_id"],
        artifact_id=snapshot["artifact_id"],
        version=snapshot["version"],
        recipient_id=recipient,
        request_id=request,
    )
    dto = await context.engine.personal_artifacts.get(
        actor_id=recipient,
        artifact_id=snapshot["artifact_id"],
        version=snapshot["version"],
    )
    return grant, context.engine.personal_artifacts.reference(dto)


async def test_personal_cross_workspace_but_not_actor_and_workspace_notes_stay_local(
    memory_context,
):
    c = memory_context
    personal = await create(c, "personal", scope="personal")
    local = await create(c, "local", topic="项目约定")
    other = await c.make("beta")
    assert c.service.owner_key == other.service.owner_key
    assert (await other.service.get(personal["memory_id"]))["content"] == "使用中文回答"
    with pytest.raises(PersonalAgentError, match="工作区"):
        await other.service.get(local["memory_id"])
    with pytest.raises(PersonalAgentError):
        await c.service.get(personal["memory_id"], c.profiles[0].id)
    assert len((await other.service.search("回复语言"))["memories"]) == 1


async def test_conflict_confirm_revise_delete_replays_resolve_current_tombstone(memory_context):
    c = memory_context
    initial = await create(c, "original", content="OLD_CONTENT_123")
    conflict = await create(c, "conflict", content="NEW_CONTENT_456")
    assert conflict["state"] == "proposed" and conflict["active_ref"] == initial["ref"]
    assert (await c.service.list(state="conflict"))["memories"][0]["memory_id"] == conflict[
        "memory_id"
    ]
    adopted = await confirm(c, conflict)
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(initial["ref"], "main")
    revised = await c.service.dispatch(
        "revise",
        {
            "request_id": "revise",
            "memory_id": adopted["memory_id"],
            "expected_revision": adopted["revision"],
            "expected_content_sha256": adopted["content_sha256"],
            "content": "CORRECTED_CONTENT_789",
        },
        trusted_channel="management_web",
    )
    deleted = await c.service.dispatch(
        "delete",
        {
            "request_id": "delete",
            "memory_id": revised["memory_id"],
            "expected_revision": revised["revision"],
        },
        trusted_channel="management_web",
    )
    assert deleted["state"] == "deleted" and not deleted["content"]
    replay = await create(c, "conflict", content="NEW_CONTENT_456")
    assert replay["state"] == "deleted" and not replay["content"]
    audit = json.dumps(await c.service.history(deleted["memory_id"]))
    assert all(
        value not in audit
        for value in ("OLD_CONTENT_123", "NEW_CONTENT_456", "CORRECTED_CONTENT_789")
    )
    assert (await c.service.search("CONTENT"))["memories"] == []
    assert (await c.service.export())["memories"] == []


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("create", {}),
        ("create", {"request_id": "a", "content": "x"}),
        ("list", {"limit": True}),
        ("search", {"query": "x", "limit": 1.5}),
        ("search", {"query": 3}),
        ("list", {"state": []}),
        ("propose", {"topic": "x", "scope": "workspace", "content": "x"}),
        ("confirm", {"request_id": "x", "memory_id": "invalid", "expected_revision": True}),
        ("get", {"memory_id": "invalid"}),
        ("list", {"actor_id": "main"}),
        ("search", {"query": " "}),
        ("unknown", {}),
    ],
)
async def test_service_validates_types_and_required_fields_before_storage(
    memory_context, operation, payload
):
    c = memory_context
    with pytest.raises(PersonalAgentError) as exc:
        await c.service.dispatch(operation, payload, trusted_channel="management_web")
    assert exc.value.code == "invalid_input"
    assert not c.service.store.db_path.exists()


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("list", {"limit": True}),
        ("list", {"state": []}),
        ("search", {"query": "语言", "category": []}),
        ("search", {"query": 1}),
    ],
)
async def test_direct_read_methods_keep_same_strict_contract(memory_context, method, kwargs):
    with pytest.raises(PersonalAgentError) as exc:
        await getattr(memory_context.service, method)(**kwargs)
    assert exc.value.code == "invalid_input"


@pytest.mark.parametrize(
    "role,authority", [("user", "user"), ("assistant", "assistant"), ("tool", "external")]
)
async def test_source_roles_come_from_real_persisted_message(memory_context, role, authority):
    c = memory_context
    raw = "原始文字，不是参数中的伪造角色。"
    await messages(c, [{"role": role, "content": raw}])
    note = await propose(
        c, content=raw, source_message_index=0, source_message_sha256=content_digest(raw)
    )
    assert note["state"] == "proposed"
    assert note["source"]["authority"] == authority
    assert note["locator_live"] == "live" and note["source_mode"] == "captured_snapshot"


@pytest.mark.parametrize(
    "raw",
    [
        "不要把这些跨项目资料记成个人偏好，仅限当前项目。",
        "请记住为个人长期偏好：使用中文。",
        "助手建议保存为个人偏好，但我尚未确认。",
    ],
)
async def test_message_proposal_never_promotes_workspace_to_personal(memory_context, raw):
    c = memory_context
    await messages(c, [{"role": "user", "content": raw}])
    with pytest.raises(PersonalAgentError) as exc:
        await propose(
            c,
            content=raw,
            scope="personal",
            source_message_index=0,
            source_message_sha256=content_digest(raw),
        )
    assert exc.value.code == "scope_denied"
    assert (await c.service.list())["memories"] == []


async def test_captured_snapshot_survives_source_retention_without_claiming_live(memory_context):
    c = memory_context
    raw = "请用简洁中文回答"
    await messages(c, [{"id": "actual-message", "role": "user", "content": raw}])
    note = await confirm(
        c,
        await propose(
            c, content=raw, source_message_index=0, source_message_sha256=content_digest(raw)
        ),
    )
    await messages(c, [{"id": "actual-message", "role": "assistant", "content": "changed"}])
    changed = await c.service.get(note["memory_id"])
    assert changed["locator_live"] == "changed" and changed["source_available"] is True
    assert changed["source"]["excerpt"] == raw and changed["source"]["authority"] == "user"
    await c.engine.session_store.delete(c.engine._session.id)
    missing = await c.service.get(note["memory_id"])
    assert missing["locator_live"] == "missing" and missing["content"] == raw
    assert (await c.service.search("中文"))["memories"][0]["memory_id"] == note["memory_id"]


async def test_source_quote_and_digest_are_checked_at_admission(memory_context):
    c = memory_context
    await messages(c, [{"role": "user", "content": "真实原文"}])
    for request, update in [
        ("hash", {"source_message_sha256": "a" * 64}),
        ("quote", {"source_quote": "并非原文"}),
        ("index", {"source_message_index": 9}),
    ]:
        with pytest.raises(PersonalAgentError):
            await propose(
                c,
                request,
                **{
                    "source_message_index": 0,
                    "source_message_sha256": content_digest("真实原文"),
                    **update,
                },
            )


async def test_compaction_is_stable_assistant_candidate_not_confirmed_user(memory_context):
    c = memory_context
    note = await c.service.propose_compaction("压缩后的经验", "experience", request_id="compact")
    replay = await c.service.propose_compaction("压缩后的经验", "experience", request_id="compact")
    assert note["state"] == "proposed" and note["memory_id"] == replay["memory_id"]
    assert note["source"]["kind"] == "compaction" and note["source"]["authority"] == "assistant"
    assert note["scope"]["kind"] == "workspace" and note["confirmation"] is None
    with pytest.raises(PersonalAgentError):
        await c.service.propose_compaction("x" * 8001, "experience", request_id="large")


async def test_opaque_draft_discard_erases_real_payload_and_does_not_apply(memory_context):
    c = memory_context
    payload = {
        "request_id": "draft",
        "topic": "草稿",
        "category": "fact",
        "content": "PRIVATE_DRAFT_123",
        "scope": "personal",
    }
    prepared = await c.service.prepare_request(
        {"request_id": "draft", "actor_id": "main", "operation": "create", "payload": payload},
        trusted_channel="management_web",
    )
    assert "PRIVATE_DRAFT_123" not in json.dumps(prepared)
    assert "PRIVATE_DRAFT_123" not in json.dumps(await c.service.request("draft"))
    with pytest.raises(PersonalAgentError):
        await c.service.discard_request(
            "draft", expected_fingerprint="a" * 64, trusted_channel="management_web"
        )
    discarded = await c.service.discard_request(
        "draft", expected_fingerprint=prepared["fingerprint"], trusted_channel="management_web"
    )
    assert discarded["state"] == "erased" and "PRIVATE_DRAFT_123" not in json.dumps(discarded)
    stored = c.service.store.get_prepared_request(owner_key=c.service.owner_key, request_id="draft")
    assert stored["payload"] is None
    with pytest.raises(PersonalAgentError) as exc:
        await c.service.apply_request(
            "draft", expected_fingerprint=prepared["fingerprint"], trusted_channel="management_web"
        )
    assert exc.value.code == "memory_request_erased"
    assert (await c.service.list())["memories"] == []


async def test_discard_applied_request_never_cancels_fact_and_replay_is_current(memory_context):
    c = memory_context
    note = await create(c, "applied", content="COMMITTED_FACT_123")
    request = await c.service.request("applied")
    discarded = await c.service.discard_request(
        "applied", expected_fingerprint=request["fingerprint"], trusted_channel="management_web"
    )
    assert discarded["state"] == "applied" and "COMMITTED_FACT_123" not in json.dumps(discarded)
    assert (await c.service.get(note["memory_id"]))["content"] == "COMMITTED_FACT_123"
    assert (
        await c.service.apply_request(
            "applied", expected_fingerprint=request["fingerprint"], trusted_channel="management_web"
        )
    )["memory_id"] == note["memory_id"]


async def test_request_identity_does_not_allow_other_actor_or_workspace_discard(memory_context):
    c = memory_context
    await create(c, "bound")
    request = await c.service.request("bound")
    with pytest.raises(PersonalAgentError):
        await c.service.discard_request(
            "bound",
            actor_id=c.profiles[0].id,
            expected_fingerprint=request["fingerprint"],
            trusted_channel="management_web",
        )
    other = await c.make("beta")
    with pytest.raises(PersonalAgentError):
        await other.service.request("bound")
    assert (await c.service.request("bound"))["state"] == "applied"


async def test_grant_request_returns_original_grant_current_state_not_replacement(memory_context):
    c = memory_context
    note = await create(c)
    recipient = c.profiles[0].id
    old = await share(c, note, recipient, "share-old")
    old_ref = reference(note, old)
    await revoke(c, old)
    new = await share(c, note, recipient, "share-new")
    request = await c.service.request("share-old")
    assert request["result"]["grant_id"] == old["grant_id"]
    assert request["result"]["state"] == "revoked"
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(old_ref, recipient)
    current = await c.service.get(note["memory_id"], recipient)
    assert current["ref"]["grant_id"] == new["grant_id"]


async def test_other_workspace_cannot_revoke_project_memory_grant(memory_context):
    c = memory_context
    note = await create(c)
    grant = await share(c, note, c.profiles[0].id)
    other = await c.make("beta")
    with pytest.raises(PersonalAgentError):
        await revoke(other, grant, "foreign-revoke")
    assert (await c.service.grants(note["memory_id"]))["grants"][0]["state"] == "active"


async def test_search_scope_winners_and_owned_priority_are_selected_before_topk(memory_context):
    c = memory_context
    await create(c, "personal", scope="personal", content="olduniquesentinel")
    local = await create(c, "workspace", content="newcurrentsentinel")
    assert (await c.service.search("olduniquesentinel", limit=1))["memories"] == []
    result = await c.service.search("回复语言", limit=1)
    assert result["memories"][0]["memory_id"] == local["memory_id"]
    assert len(result["memories"][0]["conflicts"]) == 1
    assert c.index.candidates == [local["memory_id"]]
    assert result["diagnostics"]["omitted_count"] == 1
    other = await create(c, "other-owned", owner=c.profiles[0].id, content="shareduniquesentinel")
    await share(c, other, "main")
    assert (await c.service.search("shareduniquesentinel"))["memories"] == []
    assert (await c.service.search("回复语言"))["memories"][0]["memory_id"] == local["memory_id"]


async def test_item_scope_wins_only_for_exact_actual_item(memory_context):
    c = memory_context
    item_id = "pi_" + "a" * 32
    c.items[item_id] = {"definition": {"assignee": {"id": "main"}, "origin": {"actor_id": "main"}}}
    workspace = await create(c)
    scoped = await create(c, "item", scope="item", item_id=item_id, content="事项约定")
    assert (await c.service.search("回复语言"))["memories"][0]["memory_id"] == workspace[
        "memory_id"
    ]
    assert (await c.service.search("回复语言", item_id=item_id))["memories"][0][
        "memory_id"
    ] == scoped["memory_id"]
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(scoped["ref"], "main")


async def test_index_sync_reconciles_current_heads_after_acknowledged_cache_loss(memory_context):
    c = memory_context
    note = await create(c)
    assert c.service.store.pending_index(owner_key=c.service.owner_key) == []
    c.index.keys.clear()
    result = await c.service.sync_index()
    assert result["processed"] == 0 and result["reconciled"] == 1
    assert (note["memory_id"], note["revision"], note["content_sha256"]) in c.index.keys


async def test_index_full_reconcile_removes_stale_without_pruning_other_authorized_actors(
    memory_context,
):
    c = memory_context
    first = await create(c, "first", topic="本人记忆")
    second = await create(c, "second", owner=c.profiles[0].id, topic="独立记忆")
    c.index.keys.add(("pm_" + "f" * 32, 99, "a" * 64))
    result = await c.service.sync_index()
    assert result["removed"] == 1 and len(c.index.keys) == 2
    await c.service.search("本人记忆")
    assert {key[0] for key in c.index.keys} == {first["memory_id"], second["memory_id"]}


async def test_execution_item_fence_ignores_other_items_and_cannot_expand_source_scope(
    memory_context,
):
    c = memory_context
    identities = ["pi_" + char * 32 for char in ("a", "b")]
    notes = []
    for i, identity in enumerate(identities):
        c.items[identity] = {
            "definition": {"assignee": {"id": "main"}, "origin": {"actor_id": "main"}}
        }
        notes.append(await create(c, "item-" + str(i), scope="item", item_id=identity))
    execute(c, "main", item_id=identities[0])
    listed = await c.service.list()
    assert [note["memory_id"] for note in listed["memories"]] == [notes[0]["memory_id"]]
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(notes[1]["ref"], "main")
    with pytest.raises(PersonalAgentError):
        await c.service.search("回复语言", item_id=identities[1])
    with pytest.raises(PersonalAgentError):
        await propose(c, "scope-expand", scope="workspace")


async def test_index_outage_does_not_revert_confirmed_fact_or_claim_no_memory(memory_context):
    c = memory_context
    c.index.unavailable = True
    note = await create(c)
    result = await c.service.search("回复语言")
    assert result["memories"][0]["memory_id"] == note["memory_id"]
    assert result["diagnostics"]["semantic_used"] is False
    assert result["diagnostics"]["semantic_error"]
    assert c.service.store.pending_index(owner_key=c.service.owner_key)


async def test_legacy_presence_enables_boundary_but_import_stays_unconfirmed(memory_context):
    c = memory_context
    Path(c.engine.config.memory.vector_db_path).mkdir()
    assert c.service.enabled is True

    async def export():
        return json.dumps([{"id": "legacy-1", "content": "旧记录", "category": "fact"}])

    c.engine._legacy_long_term_memory = SimpleNamespace(export_memories=export)
    legacy = (await c.service.legacy())["memories"][0]
    assert (await c.service.search("旧记录"))["memories"] == []
    imported = await c.service.dispatch(
        "import_legacy",
        {
            "request_id": "legacy-import",
            "legacy_id": legacy["legacy_id"],
            "expected_content_sha256": legacy["content_sha256"],
            "topic": "旧记录",
            "scope": "workspace",
        },
        trusted_channel="management_web",
    )
    assert imported["state"] == "proposed" and imported["source"]["authority"] == "unknown"


async def test_trusted_execution_requires_origin_and_all_reply_path_readers(memory_context):
    c = memory_context
    a, b = [profile.id for profile in c.profiles[:2]]
    private = await create(c, "private", owner=a)
    await share(c, private, b, "share-b")
    await actor(c, b)
    execute(c, b, origin=a, origins=["main", a])
    assert (await c.service.search("回复语言", actor_id=b))["memories"] == []
    with pytest.raises(PersonalAgentError):
        await c.service.get(private["memory_id"], b)
    end_execution(c)
    await share(c, private, "main", "share-main")
    execute(c, b, origin=a, origins=["main", a])
    assert (await c.service.get(private["memory_id"], b))["content"] == private["content"]
    assert (await c.service.context(b))["stats"]["active"] == 1
    assert len((await c.service.export(b))["memories"]) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "get",
        "list",
        "search",
        "history",
        "grants",
        "context",
        "export",
        "legacy",
        "request",
        "resolve_ref",
    ],
)
async def test_untrusted_execution_cannot_use_any_read_entry(memory_context, operation):
    c = memory_context
    note = await create(c)
    c.engine._isolated_child = True
    call = {
        "get": lambda: c.service.get(note["memory_id"]),
        "list": lambda: c.service.list(),
        "search": lambda: c.service.search("回复语言"),
        "history": lambda: c.service.history(note["memory_id"]),
        "grants": lambda: c.service.grants(note["memory_id"]),
        "context": lambda: c.service.context(),
        "export": lambda: c.service.export(),
        "legacy": lambda: c.service.legacy(),
        "request": lambda: c.service.request("create-one"),
        "resolve_ref": lambda: c.service.resolve_ref(note["ref"], "main"),
    }[operation]
    with pytest.raises(PersonalAgentError) as exc:
        await call()
    assert exc.value.code == "scope_denied"


@pytest.mark.parametrize(
    "change", ["actor", "workspace", "parent", "runtime", "origin-type", "missing-field"]
)
async def test_execution_context_is_fail_closed_and_live_parent_policy_is_rechecked(
    memory_context, change
):
    c = memory_context
    note = await create(c)
    execute(c, "main", origin="main")
    context = c.engine._personal_memory_execution_context
    if change == "actor":
        context["actor_id"] = c.profiles[0].id
    if change == "workspace":
        context["workspace_key"] = "b" * 64
    if change == "parent":
        c.engine._personal_memory_parent_enabled = False
    if change == "runtime":
        c.engine.personal_memory_runtime.enabled = False
    if change == "origin-type":
        context["origin_actor_ids"] = "main"
    if change == "missing-field":
        del context["artifact_refs"]
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(note["ref"], "main")


async def test_child_task_user_role_is_assistant_authority_and_cannot_assert_manual_create(
    memory_context,
):
    c = memory_context
    raw = "这是上游 Agent 发出的执行任务，不是新的用户陈述。"
    await messages(c, [{"role": "user", "content": raw}])
    execute(c, "main")
    reply = await c.service.dispatch(
        "propose",
        {
            "request_id": "child-propose",
            "topic": "任务",
            "category": "fact",
            "content": raw,
            "scope": "workspace",
            "source_message_index": 0,
            "source_message_sha256": content_digest(raw),
        },
        caller_actor="main",
    )
    stored = c.service.store.get(owner_key=c.service.owner_key, memory_id=reply["candidate_id"])
    assert stored["source"]["authority"] == "assistant" and stored["state"] == "proposed"
    with pytest.raises(PersonalAgentError):
        await create(c, "child-manual")


async def test_private_artifact_memory_requires_frozen_exact_execution_input(memory_context):
    c = memory_context
    a, b = [profile.id for profile in c.profiles[:2]]
    await actor(c, a)
    snapshot, source_ref = await artifact(c, a)
    candidate = await propose(c, owner=a, source_artifact_ref=source_ref)
    note = await confirm(c, candidate)
    _, target_ref = await artifact_share(c, snapshot, b)
    grant = await share(c, note, b)
    await actor(c, b)
    execute(c, b, origin=a, refs=[])
    with pytest.raises(PersonalAgentError):
        await c.service.resolve_ref(reference(note, grant), b)
    c.engine._personal_memory_execution_context["artifact_refs"] = [target_ref]
    assert (await c.service.resolve_ref(reference(note, grant), b))[0]["memory_id"] == note[
        "memory_id"
    ]


async def test_memory_derived_from_shared_artifact_keeps_original_exact_grant(memory_context):
    c = memory_context
    a, b = [profile.id for profile in c.profiles[:2]]
    snapshot, _ = await artifact(c, a)
    artifact_grant, ref = await artifact_share(c, snapshot, b)
    await actor(c, b)
    note = await confirm(c, await propose(c, owner=b, source_artifact_ref=ref))
    await c.engine.personal_artifacts.revoke(
        actor_id=a,
        grant_id=artifact_grant["grant_id"],
        expected_revision=artifact_grant["revision"],
        request_id="artifact-revoke",
    )
    await artifact_share(c, snapshot, b, "artifact-replacement")
    stale = await c.service.get(note["memory_id"], b)
    assert stale["source_available"] is False and stale["content"] is None
    assert (await c.service.search("输出方式", actor_id=b))["memories"] == []


async def test_shared_memory_dependency_revoke_blocks_confirm_and_recall_without_substitution(
    memory_context,
):
    c = memory_context
    a, b = [profile.id for profile in c.profiles[:2]]
    upstream = await create(c, "upstream", owner=a)
    grant = await share(c, upstream, b)
    await actor(c, b)
    c.engine._personal_memory_runtime_refs = [reference(upstream, grant)]
    active = await confirm(c, await propose(c, "derived-active", owner=b))
    candidate = await propose(c, "derived-pending", owner=b, topic="另一个结论")
    assert active["source"]["locator"]["memory_refs"] == [reference(upstream, grant)]
    assert "content" not in active["source"]["locator"]
    await revoke(c, grant)
    await share(c, upstream, b, "replacement")
    with pytest.raises(PersonalAgentError):
        await confirm(c, candidate, "confirm-after-revoke")
    invalid = await c.service.get(active["memory_id"], b)
    assert invalid["content"] is None and invalid["source_available"] is False
    assert (await c.service.search("输出方式", actor_id=b))["memories"] == []


async def test_derived_memory_sharing_does_not_implicitly_share_its_upstream(memory_context):
    c = memory_context
    a, b, recipient = [profile.id for profile in c.profiles]
    upstream = await create(c, "upstream", owner=a)
    grant = await share(c, upstream, b)
    await actor(c, b)
    c.engine._personal_memory_runtime_refs = [reference(upstream, grant)]
    derived = await confirm(c, await propose(c, owner=b))
    with pytest.raises(PersonalAgentError):
        await share(c, derived, recipient, "derived-share")
    await share(c, upstream, recipient, "upstream-recipient")
    shared = await share(c, derived, recipient, "derived-share")
    assert shared["recipient_actor_id"] == recipient
    assert (await c.service.get(derived["memory_id"], recipient))["content"] == derived["content"]


async def test_manual_summary_does_not_inherit_model_memory_dependencies(memory_context):
    c = memory_context
    upstream = await create(c, "upstream")
    c.engine._personal_memory_runtime_refs = [upstream["ref"]]
    summary = await create(c, "manual-summary", topic="独立摘要", scope="personal")
    assert "memory_refs" not in summary["source"]["locator"]
    assert summary["source"]["artifact_refs"] == []
    await c.service.dispatch(
        "delete",
        {
            "request_id": "remove-upstream",
            "memory_id": upstream["memory_id"],
            "expected_revision": upstream["revision"],
        },
        trusted_channel="management_web",
    )
    assert (await c.service.get(summary["memory_id"]))["source_available"] is True


async def test_memory_dependencies_are_bounded_and_recursion_has_explicit_guard(memory_context):
    c = memory_context
    upstream = await create(c)
    c.engine._personal_memory_runtime_refs = [upstream["ref"]] * 33
    with pytest.raises(PersonalAgentError) as exc:
        await propose(c)
    assert exc.value.code == "scope_denied"
    visited = frozenset(("memory-" + str(i), 1, "main", "") for i in range(16))
    with pytest.raises(PersonalAgentError) as exc:
        await c.service._source_authorized(upstream, "main", _visited=visited)
    assert exc.value.code == "source_unavailable"


async def test_apply_discard_concurrency_has_no_draft_resurrection(memory_context):
    c = memory_context
    payload = {
        "request_id": "race",
        "topic": "并发",
        "category": "fact",
        "content": "race body",
        "scope": "personal",
    }
    request = await c.service.prepare_request(
        {"request_id": "race", "actor_id": "main", "operation": "create", "payload": payload},
        trusted_channel="management_web",
    )
    results = await asyncio.gather(
        c.service.apply_request(
            "race", expected_fingerprint=request["fingerprint"], trusted_channel="management_web"
        ),
        c.service.discard_request(
            "race", expected_fingerprint=request["fingerprint"], trusted_channel="management_web"
        ),
        return_exceptions=True,
    )
    final = await c.service.request("race")
    stored = c.service.store.get_prepared_request(owner_key=c.service.owner_key, request_id="race")
    assert stored["payload"] is None
    assert final["state"] in {"erased", "applied"}
    memories = (await c.service.list())["memories"]
    assert len(memories) == int(final["state"] == "applied")
    assert all(
        not isinstance(result, Exception) or isinstance(result, PersonalAgentError)
        for result in results
    )


def test_conflict_schema_is_list_only_not_memory_lifecycle():
    validate_operation_payload("list", {"state": "conflict"})
    with pytest.raises(PersonalAgentError):
        validate_operation_payload("confirm", {"state": "conflict"})
