"""Fresh model-send memory over real Service, catalog, session and SQLite authority.

The semantic-index and artifact/item boundary ports are explicitly controlled;
these tests make no provider or embedding-quality acceptance claim.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.long_term import MemoryEntry
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.personal import runtime as personal_runtime
from naumi_agent.personal.memories import PersonalMemoryService
from naumi_agent.personal.memory_adapter import ScopedMemoryAdapter
from naumi_agent.personal.memory_models import canonical, content_digest, reference
from naumi_agent.personal.memory_runtime import PersonalMemoryRuntime, strip_legacy_memory
from naumi_agent.personal.memory_tools import PersonalMemoryTool
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore

ITEM = "pi_" + "c" * 32
ARTIFACT_REF = {
    "artifact_id": "art_" + "d" * 32,
    "version": 1,
    "sha256": "e" * 64,
    "grant_id": "",
    "grant_revision": 0,
}
PRIVATE = "PRIVATE_FRESH_SEND_BODY_20261104"


class UnavailableSemanticIndex:
    """Exercise the actual lexical path without loading or faking embeddings."""

    @staticmethod
    def _fail(*args, **kwargs):
        raise PersonalAgentError("memory_index_unavailable", "测试未启用语义索引")

    upsert = delete = query = ensure_current = _fail

    def reconcile(self, records):
        self.ensure_current(records)
        return {"repaired": 0, "removed": 0}

    @staticmethod
    def status():
        return {"status": "unavailable", "error": "测试未启用语义索引", "automatic_download": False}


class ArtifactBoundary:
    enabled = False

    def __init__(self):
        self.revoked = False
        self.session_pins = {}

    async def authorize_refs(self, *, actor_id, refs):
        if refs and self.revoked:
            raise PersonalAgentError("source_revoked", "原资料授权已撤销")
        return copy.deepcopy(refs)

    async def bind_refs(self, session_id, actor_id, refs):
        authorized = await self.authorize_refs(actor_id=actor_id, refs=refs)
        key = (session_id, actor_id)
        combined = {canonical(ref): ref for ref in self.session_pins.get(key, []) + authorized}
        self.session_pins[key] = copy.deepcopy(list(combined.values()))
        return copy.deepcopy(self.session_pins[key])


class ItemBoundary:
    async def _item(self, item_id):
        if item_id != ITEM:
            raise PersonalAgentError("not_found", "该事项不存在")
        return {"definition": {"assignee": {"id": "main"}, "origin": {"actor_id": "main"}}}


@pytest.fixture
async def memory_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SimpleNamespace(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "session.db"),
            vector_db_path=str(tmp_path / "legacy"),
            long_term_enabled=True,
        )
    )
    engine = SimpleNamespace(
        config=config,
        workspace_root=str(workspace),
        session_store=SessionStore(config.memory),
        tool_registry=SimpleNamespace(all=lambda: []),
        router=SimpleNamespace(resolve_model=lambda tier: "no-provider-test-model"),
        personal_artifacts=ArtifactBoundary(),
        personal_items=ItemBoundary(),
        _compactor=SimpleNamespace(_long_term_memory=None),
        _messages=[],
        _full_history=[],
        _personal_artifact_refs=[],
        _personal_active_turn=None,
    )
    engine.personal_agents = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "catalog"))
    profile = engine.personal_agents.create(
        name="Echo", instructions="按任务处理", request_id="create-echo"
    )
    engine._session = Session(workspace_root=str(workspace))
    await engine.session_store.save(engine._session)
    service = PersonalMemoryService(engine, index=UnavailableSemanticIndex())
    runtime = PersonalMemoryRuntime(service)
    engine.personal_memory = service
    engine.personal_memory_runtime = runtime
    engine.long_term_memory = SimpleNamespace(kind="scoped-memory-adapter")
    try:
        yield SimpleNamespace(engine=engine, service=service, runtime=runtime, profile=profile)
    finally:
        await service.stop()
        await engine.session_store.close()


async def create_note(
    fixture,
    request="create-note",
    *,
    content=PRIVATE,
    topic="输出习惯",
    scope="personal",
    category="preference",
    actor="main",
    **extra,
):
    return await fixture.service.dispatch(
        "create",
        {
            "request_id": request,
            "topic": topic,
            "content": content,
            "category": category,
            "scope": scope,
            **extra,
        },
        actor_id=actor,
        trusted_channel="management_web",
    )


def tool_messages(refs, *, metadata=True):
    result = [
        {"role": "system", "content": "原系统设置"},
        {"role": "user", "content": "处理当前任务"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "memory-call",
                    "type": "function",
                    "function": {"name": "personal_memories", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "memory-call",
            "content": canonical({"type": "personal_memory_refs", "refs": refs}),
        },
    ]
    if metadata:
        result[-1]["metadata"] = {"personal_memory_refs": copy.deepcopy(refs)}
    return result


def memory_block(messages):
    block = next(
        m["content"]
        for m in messages
        if m.get("role") == "system" and str(m.get("content", "")).startswith("## 已确认记忆与来源")
    )
    return json.loads(block[block.index("{") :])


async def set_messages(fixture, messages):
    engine = fixture.engine
    engine._messages = copy.deepcopy(messages)
    engine._full_history = copy.deepcopy(messages)
    engine._session.messages = copy.deepcopy(messages)
    await engine.session_store.save(engine._session)


async def become_echo(fixture):
    opened = await fixture.engine.personal_agents.open_session(fixture.profile.id)
    fixture.engine._session = await fixture.engine.session_store.load(opened["session_id"])
    fixture.engine._messages = []
    fixture.engine._full_history = []
    fixture.runtime.reset()


async def test_fresh_send_resolves_real_tool_refs_without_persisting_recalled_body(memory_runtime):
    f = memory_runtime
    note = await create_note(f)
    raw = json.loads(
        await PersonalMemoryTool(f.service).execute("get", memory_id=note["memory_id"])
    )
    assert raw["refs"] == [note["ref"]] and PRIVATE not in canonical(raw)
    messages = tool_messages(raw["refs"])
    await set_messages(f, messages)
    before = copy.deepcopy(messages)
    prepared = await f.runtime.prepare_messages(f.engine._messages)
    assert PRIVATE in canonical(prepared)
    assert f.engine._messages == before and f.engine._full_history == before
    assert PRIVATE not in canonical(
        (await f.engine.session_store.load(f.engine._session.id)).messages
    )
    assert PRIVATE not in canonical(f.engine._personal_memory_last_selection)
    assert f.engine._personal_memory_runtime_refs == [note["ref"]]
    await f.runtime.authorize()


@pytest.mark.parametrize("operation", ["delete", "revise"])
async def test_deleted_or_corrected_refs_never_restore_old_tool_body(memory_runtime, operation):
    f = memory_runtime
    note = await create_note(f)
    await set_messages(f, tool_messages([note["ref"]]))
    assert PRIVATE in canonical(await f.runtime.prepare_messages(f.engine._messages))
    payload = {
        "request_id": operation + "-note",
        "memory_id": note["memory_id"],
        "expected_revision": note["revision"],
    }
    if operation == "revise":
        payload.update(
            content="CORRECTED_FRESH_BODY", expected_content_sha256=note["content_sha256"]
        )
    current = await f.service.dispatch(operation, payload, trusted_channel="management_web")
    with pytest.raises(PersonalAgentError, match="本轮记忆") as changed:
        await f.runtime.authorize()
    assert changed.value.code == "memory_context_changed"
    prepared = await f.runtime.prepare_messages(f.engine._messages)
    assert PRIVATE not in canonical(prepared)
    assert PRIVATE not in canonical(f.engine._messages)
    assert PRIVATE not in canonical(
        (await f.engine.session_store.load(f.engine._session.id)).messages
    )
    if operation == "revise":
        assert "CORRECTED_FRESH_BODY" in canonical(prepared)
        assert f.engine._personal_memory_runtime_refs == [current["ref"]]
    else:
        assert current["state"] == "deleted" and not current["content"]
        assert f.engine._personal_memory_runtime_refs == []


async def test_revoked_memory_grant_removes_shared_context_and_old_refs(memory_runtime):
    f = memory_runtime
    note = await create_note(f)
    grant = await f.service.dispatch(
        "share",
        {
            "request_id": "share-note",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
            "expected_content_sha256": note["content_sha256"],
            "recipient_actor_id": f.profile.id,
            "recipient_version": f.profile.version,
        },
        trusted_channel="management_web",
    )
    await become_echo(f)
    shared = await f.service.get(note["memory_id"], actor_id=f.profile.id)
    assert shared["access"] == "shared" and shared["ref"]["grant_id"] == grant["grant_id"]
    await set_messages(f, tool_messages([shared["ref"]]))
    assert PRIVATE in canonical(await f.runtime.prepare_messages(f.engine._messages))
    await f.service.dispatch(
        "revoke",
        {
            "request_id": "revoke-note",
            "grant_id": grant["grant_id"],
            "expected_revision": grant["revision"],
        },
        trusted_channel="management_web",
    )
    with pytest.raises(PersonalAgentError) as changed:
        await f.runtime.authorize()
    assert changed.value.code == "memory_context_changed"
    assert PRIVATE not in canonical(await f.runtime.prepare_messages(f.engine._messages))
    assert f.engine._personal_memory_runtime_refs == []


async def test_original_artifact_source_revocation_fails_fresh_resolution(memory_runtime):
    f = memory_runtime
    note = await create_note(f, scope="workspace")
    source = copy.deepcopy(note["source"])
    source.update(
        kind="artifact",
        authority="external",
        artifact_refs=[ARTIFACT_REF],
        scope_ceiling={
            "kind": "workspace",
            "workspace_key": f.service.workspace_key,
            "item_id": "",
        },
    )
    original = await f.service._call(
        "create",
        actor_id="main",
        request_id="create-private-source",
        scope=note["scope"],
        topic="资料结论",
        category="fact",
        content="ARTIFACT_PRIVATE_BODY",
        source=source,
        human={"channel": "management_web", "action": "create", "actor_kind": "human"},
    )
    ref = reference(original)
    await set_messages(f, tool_messages([ref]))
    prepared = await f.runtime.prepare_messages(f.engine._messages)
    assert "ARTIFACT_PRIVATE_BODY" in canonical(prepared)
    assert f.engine._personal_memory_artifact_refs == [ARTIFACT_REF]
    f.engine.personal_artifacts.revoked = True
    with pytest.raises(PersonalAgentError) as changed:
        await f.runtime.authorize()
    assert changed.value.code == "memory_context_changed"
    assert "ARTIFACT_PRIVATE_BODY" not in canonical(
        await f.runtime.prepare_messages(f.engine._messages)
    )
    assert f.engine._personal_memory_artifact_refs == []


@pytest.mark.parametrize("metadata", [False, True])
async def test_disabled_memory_clears_old_system_and_tool_refs_without_altering_user_text(
    memory_runtime, metadata
):
    f = memory_runtime
    note = await create_note(f)
    messages = tool_messages([note["ref"]], metadata=metadata)
    messages[1]["content"] = "## 相关记忆\n这是用户实际输入，必须保留"
    messages.insert(1, {"role": "system", "content": "## 相关记忆\nLEGACY_GLOBAL_BODY"})
    messages.insert(1, {"role": "system", "content": "## 已确认记忆与来源\nOLD_DERIVED_BODY"})
    await set_messages(f, messages)
    await f.runtime.prepare_messages(f.engine._messages)
    f.engine.config.memory.long_term_enabled = False
    with pytest.raises(PersonalAgentError) as changed:
        await f.runtime.authorize()
    assert changed.value.code == "memory_context_changed"
    prepared = await f.runtime.prepare_messages(f.engine._messages)
    text = canonical(prepared)
    assert (
        "LEGACY_GLOBAL_BODY" not in text and "OLD_DERIVED_BODY" not in text and PRIVATE not in text
    )
    assert "这是用户实际输入，必须保留" in text
    assert json.loads(next(m["content"] for m in prepared if m["role"] == "tool"))["refs"] == []
    assert not any(m.get("metadata", {}).get("personal_memory_refs") for m in prepared)


async def test_scope_priority_is_item_then_workspace_then_personal_and_own_over_shared(
    memory_runtime,
):
    f = memory_runtime
    await create_note(f, "personal-priority", content="PERSONAL_SCOPE_VALUE", topic="输出语言")
    await create_note(
        f,
        "workspace-priority",
        content="WORKSPACE_SCOPE_VALUE",
        topic="输出语言",
        scope="workspace",
    )
    item = await create_note(
        f, "item-priority", content="ITEM_SCOPE_VALUE", topic="输出语言", scope="item", item_id=ITEM
    )
    await create_note(
        f,
        "echo-priority",
        content="SHARED_COMPETING_VALUE",
        topic="输出语言",
        scope="workspace",
        actor=f.profile.id,
    )
    shared = (await f.service.list(actor_id=f.profile.id))["memories"][0]
    await f.service.dispatch(
        "share",
        {
            "request_id": "share-echo-priority",
            "memory_id": shared["memory_id"],
            "expected_revision": shared["revision"],
            "expected_content_sha256": shared["content_sha256"],
            "recipient_actor_id": "main",
            "recipient_version": 0,
        },
        actor_id=f.profile.id,
        trusted_channel="management_web",
    )
    await set_messages(f, [{"role": "user", "content": "开始当前任务"}])
    without_item = memory_block(await f.runtime.prepare_messages(f.engine._messages))
    assert [m["content"] for m in without_item["memories"]] == ["WORKSPACE_SCOPE_VALUE"]
    f.engine._personal_item_context = SimpleNamespace(intent={"item_id": ITEM})
    with_item = memory_block(await f.runtime.prepare_messages(f.engine._messages))
    assert [m["ref"] for m in with_item["memories"]] == [item["ref"]]
    assert "SHARED_COMPETING_VALUE" not in canonical(with_item)


async def test_personal_scope_crosses_workspaces_but_never_implicitly_crosses_agents(
    memory_runtime,
    tmp_path,
):
    f = memory_runtime
    await create_note(f, content="CROSS_WORKSPACE_PERSONAL_VALUE")
    await create_note(
        f,
        "local-workspace-note",
        scope="workspace",
        topic="项目环境",
        content="WORKSPACE_A_ONLY_VALUE",
    )
    other_workspace = tmp_path / "workspace-b"
    other_workspace.mkdir()
    other = copy.copy(f.engine)
    other.workspace_root = str(other_workspace)
    other._session = Session(workspace_root=str(other_workspace))
    other._messages, other._full_history = [], []
    await other.session_store.save(other._session)
    other.personal_agents = PersonalAgentService(other, f.engine.personal_agents.store)
    other_service = PersonalMemoryService(other, index=UnavailableSemanticIndex())
    other_runtime = PersonalMemoryRuntime(other_service)
    assert other_service.owner_key == f.service.owner_key
    assert other_service.workspace_key != f.service.workspace_key
    other_text = canonical(await other_runtime.prepare_messages([]))
    assert (
        "CROSS_WORKSPACE_PERSONAL_VALUE" in other_text
        and "WORKSPACE_A_ONLY_VALUE" not in other_text
    )
    await become_echo(f)
    echo_text = canonical(await f.runtime.prepare_messages([]))
    assert (
        "CROSS_WORKSPACE_PERSONAL_VALUE" not in echo_text
        and "WORKSPACE_A_ONLY_VALUE" not in echo_text
    )


async def test_auto_capture_preserves_assistant_source_and_all_outputs_remain_candidates(
    memory_runtime,
):
    f = memory_runtime
    task = "个人偏好：我喜欢黑白配色。"
    answer = "项目使用PostgreSQL作为数据库。"
    await set_messages(
        f, [{"role": "user", "content": task}, {"role": "assistant", "content": answer}]
    )
    with personal_runtime.run_context(f.engine, f.engine._session.id, "real-source-run", None):
        await f.runtime.auto_capture(task, answer)
    records = await f.service._records(actor_id="main")
    assert len(records) == 2
    assert {r["state"] for r in records} == {"proposed"}
    assert all(r["confirmation"] is None for r in records)
    assistant = next(r for r in records if "PostgreSQL" in r["content"])
    user = next(r for r in records if "黑白" in r["content"])
    assert assistant["source"]["authority"] == "assistant"
    assert assistant["source"]["locator"]["message_index"] == 1
    assert assistant["source"]["locator"]["message_sha256"] == content_digest(answer)
    assert assistant["source"]["excerpt"] in answer
    assert user["source"]["authority"] == "user" and user["source"]["locator"]["message_index"] == 0
    assert all(r["scope"]["kind"] == "workspace" for r in records)
    assert memory_block(await f.runtime.prepare_messages(f.engine._messages))["memories"] == []
    # Same execution identity is an idempotent proposal, not a second memory authority.
    with personal_runtime.run_context(f.engine, f.engine._session.id, "real-source-run", None):
        await f.runtime.auto_capture(task, answer)
    assert len(await f.service._records(actor_id="main")) == 2


@pytest.mark.parametrize(
    "guard", ["isolated", "disabled", "artifact", "missing-run", "missing-source"]
)
async def test_auto_capture_does_not_invent_a_human_source_or_cross_private_execution_boundary(
    memory_runtime, guard
):
    f = memory_runtime
    task, answer = "我喜欢中文回答。", "项目使用SQLite。"
    await set_messages(
        f, [{"role": "user", "content": task}, {"role": "assistant", "content": answer}]
    )
    if guard == "isolated":
        f.engine._isolated_child = True
    elif guard == "disabled":
        f.engine.config.memory.long_term_enabled = False
    elif guard == "artifact":
        f.engine._personal_artifact_refs = [ARTIFACT_REF]
    elif guard == "missing-source":
        f.engine._full_history = []
    with personal_runtime.run_context(
        f.engine, f.engine._session.id, "" if guard == "missing-run" else "guarded-run", None
    ):
        await f.runtime.auto_capture(task, answer)
    assert await f.service._records(actor_id="main") == []


async def test_begin_turn_reset_and_prime_clear_derived_state_and_keep_real_source_positions(
    memory_runtime,
):
    f = memory_runtime
    note = await create_note(f)
    messages = tool_messages([note["ref"]])
    messages.insert(1, {"role": "system", "content": "## 相关记忆\nOLD_DERIVED_BODY"})
    await set_messages(f, messages)
    await f.runtime.prepare_messages(f.engine._messages)
    personal_runtime.begin_turn(f.engine)
    assert f.engine._personal_memory_runtime_refs == []
    assert f.engine._personal_memory_artifact_refs == []
    assert f.engine._personal_memory_query == ""
    assert "OLD_DERIVED_BODY" not in canonical(f.engine._messages)
    assert "OLD_DERIVED_BODY" not in canonical(f.engine._full_history)
    await f.runtime.prime("新的检索问题")
    assert f.engine._personal_memory_query == "新的检索问题"
    prepared = memory_block(await f.runtime.prepare_messages(f.engine._messages))
    hint = prepared["current_source"]
    original = f.engine._full_history[hint["source_message_index"]]
    assert original["role"] == "user" and hint["source_message_sha256"] == content_digest(
        original["content"]
    )
    assert f.engine._compactor._long_term_memory is f.engine.long_term_memory
    f.runtime.reset()
    assert f.engine._personal_memory_runtime_refs == [] and f.engine._personal_memory_query == ""


async def test_context_budget_omits_whole_notes_without_truncating_meaning(memory_runtime):
    f = memory_runtime
    values = [str(index) * 5000 for index in range(3)]
    for index, value in enumerate(values):
        await create_note(f, f"budget-{index}", content=value, topic=f"偏好{index}")
    prepared = memory_block(await f.runtime.prepare_messages([]))
    assert len(prepared["memories"]) == 2 and prepared["omitted_count"] == 1
    assert all(m["content"] in values and len(m["content"]) == 5000 for m in prepared["memories"])
    assert len(f.engine._personal_memory_runtime_refs) == 2


async def test_scoped_compatibility_compaction_only_proposes_and_never_accesses_raw_legacy(
    memory_runtime,
):
    f = memory_runtime

    class LegacyTrap:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            self.calls.append(name)
            raise AssertionError("Raw global memory must not participate in scoped runtime")

    legacy = LegacyTrap()
    f.engine._legacy_long_term_memory = legacy
    adapter = ScopedMemoryAdapter(f.service, f.runtime)
    f.engine.long_term_memory = adapter
    await create_note(f)
    await set_messages(f, [{"role": "user", "content": "继续处理任务"}])
    personal_runtime.begin_turn(f.engine)
    assert f.engine._compactor._long_term_memory is adapter
    assert f.engine._compactor._long_term_memory is not legacy
    entry = MemoryEntry(
        id="",
        content="ASSISTANT_COMPACTED_PROJECT_FACT",
        category="fact",
        metadata={"source": "compaction"},
    )
    with personal_runtime.run_context(
        f.engine, f.engine._session.id, "compaction-source-run", None
    ):
        identity = await f.engine._compactor._long_term_memory.store(entry)
    candidate = await f.service.get(identity)
    assert candidate["state"] == "proposed" and candidate["confirmation"] is None
    assert candidate["source"]["kind"] == "compaction"
    assert candidate["source"]["authority"] in {"assistant", "unknown"}
    assert candidate["scope"]["kind"] == "workspace"
    assert "ASSISTANT_COMPACTED_PROJECT_FACT" not in canonical(
        await f.runtime.prepare_messages(f.engine._messages)
    )
    assert len(await adapter.search()) == 1 and await adapter.count() == 1
    assert (await adapter.stats()).active == 1
    with pytest.raises(PersonalAgentError) as deletion:
        await adapter.delete(candidate["memory_id"])
    assert deletion.value.code == "human_confirmation_required"
    assert await adapter.forget_old() == 0 and legacy.calls == []


@pytest.mark.parametrize("guard", ["missing-run", "disabled"])
async def test_scoped_compaction_rejects_unbound_or_disabled_sources(memory_runtime, guard):
    f = memory_runtime
    adapter = ScopedMemoryAdapter(f.service, f.runtime)
    if guard == "disabled":
        f.engine.config.memory.long_term_enabled = False
    entry = MemoryEntry(id="", content="UNBOUND_COMPACTION_FACT", category="fact")
    with pytest.raises(PersonalAgentError) as denied:
        await adapter.store(entry)
    assert denied.value.code == ("memory_disabled" if guard == "disabled" else "scope_denied")
    assert await f.service._records(actor_id="main") == []


async def test_disabled_compatibility_search_does_not_leak_a_blank_query_listing(memory_runtime):
    f = memory_runtime
    await create_note(f)
    adapter = ScopedMemoryAdapter(f.service, f.runtime)
    f.engine.config.memory.long_term_enabled = False
    assert await adapter.search() == []
    assert await adapter.recall("输出习惯") == []


async def test_missing_authority_database_is_storage_error_not_empty_memory_success(memory_runtime):
    f = memory_runtime
    note = await create_note(f)
    await f.runtime.prepare_messages(tool_messages([note["ref"]]))
    original_path = f.service.store.db_path
    assert original_path.is_relative_to(f.service.store.base_dir)
    original_path.unlink()
    with pytest.raises(PersonalAgentError) as unavailable:
        await f.runtime.authorize()
    assert unavailable.value.code == "storage_error"
    with pytest.raises(PersonalAgentError) as unavailable:
        await f.runtime.prepare_messages([])
    assert unavailable.value.code == "storage_error" and not original_path.exists()


def test_legacy_scrubbing_preserves_user_statements_and_sanitizes_untrusted_memory_tool_text():
    messages = tool_messages([])
    messages[-1]["content"] = "OLD_PRIVATE_MEMORY_TEXT"
    messages[-1].pop("metadata")
    messages[1]["content"] = "## 已确认记忆与来源\n用户原文不删除"
    cleaned = strip_legacy_memory(messages)
    assert "OLD_PRIVATE_MEMORY_TEXT" not in canonical(cleaned)
    assert "用户原文不删除" in canonical(cleaned)
    assert json.loads(cleaned[-1]["content"])["refs"] == []
