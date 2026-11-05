"""Research authoring retains real Session, Artifact and confirmed Memory inputs."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from naumi_agent.memory.session import Session
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import digest
from naumi_agent.personal.research_provenance import (
    normalize_provenance_ref,
    provenance_ref,
)
from tests.integration import test_personal_research_service as base

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class UnavailableSemanticIndex:
    """Disable optional embeddings while retaining actual SQLite/Memory selection."""

    @staticmethod
    def _fail(*_args, **_kwargs):
        raise PersonalAgentError("memory_index_unavailable", "来源核验测试未启用语义索引")

    upsert = delete = query = ensure_current = _fail

    def reconcile(self, records):
        self.ensure_current(records)

    @staticmethod
    def status():
        return {"status": "unavailable", "error": "来源核验测试未启用语义索引"}


@pytest.fixture
async def system(tmp_path, monkeypatch):
    async for value in base.system.__wrapped__(tmp_path, monkeypatch):
        value.engine.personal_memory.index = UnavailableSemanticIndex()
        yield value


async def memory(system, *, actor="main", request="authoring-memory", scope="personal", **extra):
    return await system.engine.personal_memory.dispatch(
        "create",
        {
            "request_id": request,
            "topic": "原成本分析口径",
            "category": "preference",
            "content": "PRIVATE_RAW_MEMORY_4681：成本与收入统一使用2026年第三季度美元口径。",
            "scope": scope,
            **extra,
        },
        actor_id=actor,
        trusted_channel="management_web",
    )


async def select_memories(system):
    # Exercise actual production selection, not a test-only provenance setter.
    system.engine.config.memory.long_term_enabled = True
    system.engine._personal_memory_query = ""
    await system.engine.personal_memory_runtime.prepare_messages(
        [{"role": "user", "content": "检查成本比例"}]
    )


async def provenance(system, view, actor="main"):
    return await system.service.notebook_provenance(view["notebook"], actor)


def empty_fields(view):
    value = view["notebook"]
    return {
        "id": value["id"],
        "expected_revision": value["revision"],
        **{key: value[key] for key in ("title", "question", "domain", "claims", "conclusion")},
    }


async def model_report(system):
    captured = await base.capture(system, await base.create(system))
    revised = await base.model(
        system, "revise", request_id="memory-model-revise", **base.fields(captured)
    )
    checked = await base.checked(system, revised)
    published = await base.published(system, checked)
    report = published["publication"]
    await base.report_body(system, report)
    derived = await system.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="memory-derived",
        title="比例派生结论",
        kind="output",
        filename="derived.txt",
        content=b"Derived exact research result",
        source_refs=[report],
    )
    return published, report, system.engine.personal_artifacts.reference(derived)


async def test_manual_authoring_is_explicit_human_and_idempotent(system):
    first = await base.create(system)
    proof = await provenance(system, first)
    assert proof["status"] == "complete" and len(proof["records"]) == 1
    record = proof["records"][0]
    assert record["origin_kind"] == "human"
    assert record["origin_session_id"] is None and record["memory_item_id"] is None
    assert record["memory_refs"] == record["session_artifact_refs"] == []
    assert first["notebook"]["authoring_provenance_refs"] == [provenance_ref(record)]
    replay = await base.create(system)
    assert (await provenance(system, replay)) == proof
    records = await system.service._read(lambda tx: tx.list("provenance", limit=100))
    assert len(records) == 1


async def test_real_model_selection_records_exact_session_memory_and_item_scope(system):
    item_view = await system.engine.personal_items.create(
        {"request_id": "provenance-item", "title": "原成本事项", "instructions": "核对成本"}
    )
    item_id = item_view["item_id"]
    note = await memory(system, scope="item", item_id=item_id)
    system.engine._personal_item_context = SimpleNamespace(intent={"item_id": item_id})
    await select_memories(system)
    assert system.engine._personal_memory_runtime_refs == [note["ref"]]
    created = await base.model(
        system,
        "create",
        request_id="model-item-notebook",
        title="成本原口径",
        question="原成本比例是否一致？",
        domain="general",
    )
    record = (await provenance(system, created))["records"][0]
    assert record["origin_kind"] == "model"
    assert record["origin_session_id"] == system.engine._session.id
    assert record["memory_refs"] == [note["ref"]] and record["memory_item_id"] == item_id
    assert record["session_artifact_refs"] == await system.engine.personal_artifacts.session_refs(
        system.engine._session.id, "main"
    )
    # Later browsing outside the original item retains original-scope evaluation.
    system.engine._personal_item_context = None
    view = await base.manual(system, "get", id=created["notebook"]["id"])
    assert view["availability"] == "available"


@pytest.mark.parametrize("operation", ["delete", "revise"])
async def test_memory_change_fences_notebook_report_and_derived_artifact(system, operation):
    note = await memory(system)
    await select_memories(system)
    published, report, derived = await model_report(system)
    proof = await provenance(system, published)
    assert proof["records"][-1]["memory_refs"] == [note["ref"]]
    arguments = {
        "request_id": "invalidate-original-memory",
        "memory_id": note["memory_id"],
        "expected_revision": note["revision"],
    }
    if operation == "revise":
        arguments.update(
            expected_content_sha256=note["content_sha256"], content="改用2026年第四季度口径"
        )
    await system.engine.personal_memory.dispatch(
        operation, arguments, trusted_channel="management_web"
    )
    current = await base.manual(system, "get", id=published["notebook"]["id"])
    assert current["availability"] == "source_unavailable"
    assert current["notebook"]["claims"] == []
    for reference in (report, derived):
        with pytest.raises(PersonalAgentError):
            await base.report_body(system, reference)


async def test_original_shared_memory_grant_revocation_fences_published_results(system):
    owner = system.engine.personal_agents.create(
        request_id="memory-owner", name="Atlas", instructions="保留原记忆授权", tool_names=[]
    )
    note = await memory(system, actor=owner.id)
    grant = await system.engine.personal_memory.dispatch(
        "share",
        {
            "request_id": "share-authoring-memory",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
            "expected_content_sha256": note["content_sha256"],
            "recipient_actor_id": "main",
            "recipient_version": 0,
        },
        actor_id=owner.id,
        trusted_channel="management_web",
    )
    await select_memories(system)
    selected = system.engine._personal_memory_runtime_refs
    assert len(selected) == 1 and selected[0]["grant_id"] == grant["grant_id"]
    published, report, derived = await model_report(system)
    await system.engine.personal_memory.dispatch(
        "revoke",
        {
            "request_id": "revoke-authoring-memory",
            "grant_id": grant["grant_id"],
            "expected_revision": grant["revision"],
        },
        actor_id=owner.id,
        trusted_channel="management_web",
    )
    assert (await base.manual(system, "get", id=published["notebook"]["id"]))[
        "availability"
    ] == "source_unavailable"
    for reference in (report, derived):
        with pytest.raises(PersonalAgentError):
            await base.report_body(system, reference)


async def test_manual_revision_keeps_all_model_dependencies_and_unknown_legacy_status(system):
    note = await memory(system)
    await select_memories(system)
    created = await base.model(
        system,
        "create",
        request_id="model-before-human",
        title="原模型研究",
        question="检查原口径",
        domain="general",
    )
    first_proof = await provenance(system, created)
    system.engine.personal_memory_runtime.reset()
    manual = await base.manual(
        system, "revise", request_id="human-after-model", **empty_fields(created)
    )
    second_proof = await provenance(system, manual)
    assert second_proof["records"][:-1] == first_proof["records"]
    assert second_proof["records"][-1]["origin_kind"] == "human"
    assert second_proof["records"][0]["memory_refs"] == [note["ref"]]
    await system.engine.personal_memory.dispatch(
        "delete",
        {
            "request_id": "delete-after-human",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
        },
        trusted_channel="management_web",
    )
    assert (await base.manual(system, "get", id=manual["notebook"]["id"]))[
        "availability"
    ] == "source_unavailable"


async def test_legacy_missing_provenance_stays_unknown_after_later_manual_revision(system):
    original = await base.create(system)
    legacy = copy.deepcopy(original["notebook"])
    legacy["id"] = legacy["notebook_id"] = "rq_" + "a" * 32
    legacy.pop("authoring_provenance_refs")
    legacy.pop("authoring_provenance_legacy_unknown")
    await system.service._write(lambda tx: tx.put("notebook", legacy, expected_revision=0))
    view = await base.manual(system, "get", id=legacy["id"])
    assert view["availability"] == "available"
    assert await provenance(system, view) == {"status": "legacy_unknown", "records": []}
    revised = await base.manual(
        system, "revise", request_id="legacy-manual-revise", **empty_fields(view)
    )
    proof = await provenance(system, revised)
    assert revised["availability"] == "available" and proof["status"] == "legacy_unknown"
    assert len(proof["records"]) == 1 and proof["records"][0]["origin_kind"] == "human"


async def test_author_session_exact_artifact_grant_is_retained_and_live(system):
    owner = system.engine.personal_agents.create(
        request_id="artifact-author", name="Mira", instructions="管理原资料", tool_names=[]
    )
    original = await system.engine.personal_artifacts.publish(
        actor_id=owner.id,
        request_id="authoring-original",
        title="原私有资料",
        kind="material",
        filename="original.txt",
        content=base.BODY.encode(),
    )
    grant = await system.engine.personal_artifacts.share(
        actor_id=owner.id,
        artifact_id=original["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="authoring-share",
    )
    snapshot = await system.engine.personal_artifacts.get(
        actor_id="main", artifact_id=original["artifact_id"], version=1
    )
    ref = system.engine.personal_artifacts.reference(snapshot)
    await system.engine.personal_artifacts.bind_refs(system.engine._session.id, "main", [ref])
    created = await base.model(
        system,
        "create",
        request_id="artifact-model-create",
        title="保留原授权",
        question="核验来源",
        domain="general",
    )
    record = (await provenance(system, created))["records"][0]
    assert record["session_artifact_refs"] == record["artifact_refs"] == [ref]
    await system.engine.personal_artifacts.revoke(
        actor_id=owner.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="authoring-revoke",
    )
    assert (await base.manual(system, "get", id=created["notebook"]["id"]))[
        "availability"
    ] == "source_unavailable"


@pytest.mark.parametrize("change", ["session", "memory_selection", "item_scope", "actor_version"])
async def test_changed_model_inputs_before_commit_do_not_mutate_notebook(
    system, monkeypatch, change
):
    actor, profile = "main", None
    if change == "actor_version":
        profile = system.engine.personal_agents.create(
            request_id="write-race-agent", name="Echo", instructions="核验研究", tool_names=[]
        )
        actor = profile.id
        opened = await system.engine.personal_agents.open_session(actor)
        system.engine._session = await system.engine.session_store.load(opened["session_id"])
    await memory(system, actor=actor)
    await select_memories(system)
    created = await base.model(
        system,
        "create",
        actor=actor,
        request_id="before-input-race",
        title="原研究",
        question="核验口径",
        domain="general",
    )
    before = await system.service._record("notebook", created["notebook"]["id"])
    check = system.service._recheck_authoring_provenance

    async def race(notebook, record, target, *, from_model):
        if change == "session":
            session = Session(workspace_root=str(system.engine.workspace_root))
            await system.engine.session_store.save(session)
            system.engine._session = session
        elif change == "memory_selection":
            system.engine._personal_memory_runtime_refs = []
        elif change == "item_scope":
            system.engine._personal_item_context = SimpleNamespace(
                intent={"item_id": "pi_" + "b" * 32}
            )
        else:
            system.engine.personal_agents.update(
                profile.id, profile.version, instructions="保存前原身份已更新"
            )
        return await check(notebook, record, target, from_model=from_model)

    monkeypatch.setattr(system.service, "_recheck_authoring_provenance", race)
    with pytest.raises(PersonalAgentError) as caught:
        await base.model(
            system, "revise", actor=actor, request_id="changed-input-race", **empty_fields(created)
        )
    assert caught.value.code in {"binding_conflict", "source_stale"}
    assert await system.service._record("notebook", before["id"]) == before
    records = await system.service._read(lambda tx: tx.list("provenance", limit=100))
    assert len(records) == 1


async def test_historical_actor_version_is_not_a_permanent_owner_read_lock(system):
    profile = system.engine.personal_agents.create(
        request_id="historical-author", name="Atlas", instructions="原研究约定", tool_names=[]
    )
    opened = await system.engine.personal_agents.open_session(profile.id)
    system.engine._session = await system.engine.session_store.load(opened["session_id"])
    created = await base.model(
        system,
        "create",
        actor=profile.id,
        request_id="versioned-model-create",
        title="原研究",
        question="核验原输入",
        domain="general",
    )
    system.engine.personal_agents.update(
        profile.id, profile.version, instructions="继续核验，不改变历史证据"
    )
    reopened = await base.model(system, "get", actor=profile.id, id=created["notebook"]["id"])
    assert reopened["availability"] == "available"
    record = (await provenance(system, reopened, profile.id))["records"][0]
    assert record["actor_version"] == profile.version


@pytest.mark.parametrize("change", ["session", "memory_selection"])
async def test_interrupted_stage_never_rebinds_live_inputs_on_retry(system, monkeypatch, change):
    note = await memory(system)
    await select_memories(system)
    original_session = system.engine._session
    arguments = {
        "request_id": "interrupted-authoring",
        "title": "原中断研究",
        "question": "必须保留原会话与记忆依据",
        "domain": "general",
    }
    complete = system.service._complete

    async def interrupt(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(system.service, "_complete", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await base.model(system, "create", **arguments)
    identity = system.service._request_identity("main", arguments["request_id"])
    frozen = await system.service._record("intent", identity)
    assert frozen["phase"] == "authoring_prepared"
    assert frozen["stage"]["provenance"]["memory_refs"] == [note["ref"]]
    monkeypatch.setattr(system.service, "_complete", complete)
    if change == "session":
        replacement = Session(workspace_root=str(system.engine.workspace_root))
        await system.engine.session_store.save(replacement)
        system.engine._session = replacement
    else:
        system.engine.personal_memory_runtime.reset()
    with pytest.raises(PersonalAgentError) as stale:
        await base.model(system, "create", **arguments)
    assert stale.value.code in {"binding_conflict", "source_stale"}
    assert await system.service._record("intent", identity) == frozen
    assert await system.service._read(lambda tx: tx.list("notebook", limit=100)) == []
    assert await system.service._read(lambda tx: tx.list("provenance", limit=100)) == []
    # Returning to the same real Session and confirmed selection can finish the
    # original request; its persisted authoring record is byte-for-byte frozen.
    system.engine._session = original_session
    await select_memories(system)
    completed = await base.model(system, "create", **arguments)
    assert (await provenance(system, completed))["records"] == [frozen["stage"]["provenance"]]


@pytest.mark.parametrize("model_first", [False, True])
async def test_interrupted_request_cannot_switch_human_and_model_origin(
    system, monkeypatch, model_first
):
    arguments = {
        "request_id": "origin-frozen-request",
        "title": "原入口研究",
        "question": "原写作入口不能改变",
        "domain": "general",
    }
    complete = system.service._complete

    async def interrupt(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(system.service, "_complete", interrupt)
    first, retry = (base.model, base.manual) if model_first else (base.manual, base.model)
    with pytest.raises(asyncio.CancelledError):
        await first(system, "create", **arguments)
    monkeypatch.setattr(system.service, "_complete", complete)
    with pytest.raises(PersonalAgentError) as mismatch:
        await retry(system, "create", **arguments)
    assert mismatch.value.code == "binding_conflict"
    assert await system.service._read(lambda tx: tx.list("notebook", limit=100)) == []
    completed = await first(system, "create", **arguments)
    record = (await provenance(system, completed))["records"][0]
    assert record["origin_kind"] == ("model" if model_first else "human")


async def test_upstream_authoring_provenance_is_in_exact_basis_and_read_record(system):
    await memory(system)
    await select_memories(system)
    captured = await base.capture(system, await base.create(system, "upstream-create"))
    upstream = await base.model(
        system, "revise", request_id="upstream-model-write", **base.fields(captured)
    )
    system.engine.personal_memory_runtime.reset()
    child = await base.create(system, "downstream-create")
    fields = empty_fields(child)
    fields["claims"] = [
        {
            "id": "transferred",
            "kind": "unknown",
            "text": "转述上游收入",
            "upstream_claims": [
                {
                    "notebook_id": upstream["notebook"]["id"],
                    "revision": upstream["notebook"]["revision"],
                    "claim_id": "income",
                }
            ],
        }
    ]
    downstream = await base.manual(system, "revise", request_id="downstream-authoring", **fields)
    basis, _ = await system.service._basis(downstream["notebook"], require_current=False)
    expected = (await provenance(system, upstream))["records"]
    for record in expected:
        assert provenance_ref(record) in basis["authoring_provenance"]["refs"]
    read, _ = await system.service._new_read(
        downstream, downstream["notebook"], "main", require_current=False
    )
    for record in expected:
        assert record in read["authoring_provenance_records"]


async def test_self_mirror_keeps_protocol_nine_memory_provenance_marker(system):
    await memory(system)
    await select_memories(system)
    published, _, _ = await model_report(system)
    original = (await provenance(system, published))["records"][-1]
    await system.service.model_result(
        {"operation": "get", "id": published["notebook"]["id"]}, published, "main"
    )
    revised = await base.model(
        system, "revise", request_id="mirror-memory-revision", **base.fields(published)
    )
    assert revised["availability"] == "available"
    mirrors = []
    for ref in revised["notebook"]["context_refs"]:
        snapshot = await system.engine.personal_artifacts.get(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        if snapshot.get("evidence", {}).get("source_kind") == "research_revision_context":
            mirrors.append(snapshot)
    assert mirrors
    assert any(
        provenance_ref(original)
        in snapshot["evidence"].get("research_authoring_provenance_refs", [])
        for snapshot in mirrors
    )


async def test_provenance_ref_digest_binding_and_raw_shape_are_strict(system):
    view = await base.create(system)
    ref = view["notebook"]["authoring_provenance_refs"][0]
    assert normalize_provenance_ref(ref) == ref
    with pytest.raises(PersonalAgentError) as mismatch:
        await system.service.authorize_authoring_ref({**ref, "record_digest": "f" * 64}, "main")
    assert mismatch.value.code == "storage_error"
    for malformed in (
        {**ref, "extra": "ignored?"},
        {**ref, "revision": True},
        {**ref, "revision": 2},
        {**ref, "workspace_key": "wrong"},
    ):
        with pytest.raises(PersonalAgentError) as invalid:
            normalize_provenance_ref(malformed)
        assert invalid.value.code == "invalid_input"
    broken = copy.deepcopy(view["notebook"])
    broken["id"] = broken["notebook_id"] = "rq_" + "c" * 32
    with pytest.raises(PersonalAgentError) as binding:
        await system.service.notebook_provenance(broken, "main")
    assert binding.value.code == "storage_error"
    record = await system.service._resolve_authoring_ref(ref)
    assert ref["record_digest"] == digest(record)


async def test_owner_provenance_does_not_authorize_arbitrary_recipient(system):
    view = await base.create(system)
    recipient = system.engine.personal_agents.create(
        request_id="untrusted-reader", name="Mira", instructions="原研究接收者", tool_names=[]
    )
    with pytest.raises(PersonalAgentError) as denied:
        await system.service.authorize_authoring_ref(
            view["notebook"]["authoring_provenance_refs"][0], recipient.id
        )
    assert denied.value.code == "scope_denied"
