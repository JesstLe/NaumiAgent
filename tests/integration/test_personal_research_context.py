"""Explicit research context recovery over real journals, sessions and ACLs.

Outbound capture alone is a fixture. The original material, messages, grants and
model-send admission are production objects; these tests never call a provider.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.model.router import ModelTier
from naumi_agent.personal.memory_models import reference as memory_reference
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_tools import execute_research_operation
from naumi_agent.runtime.composition import create_agent_engine
from tests.integration import test_personal_research_egress as egress_base
from tests.integration import test_personal_research_service as base

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
WAIT = 60


@pytest.fixture
async def context_system(tmp_path, monkeypatch):
    async for system in base.system.__wrapped__(tmp_path, monkeypatch):
        yield system


@pytest.fixture
async def approved_context(tmp_path, monkeypatch):
    async for system in egress_base.egress.__wrapped__(tmp_path, monkeypatch):
        yield system


async def raw_refs(system, engine=None):
    engine = engine or system.engine
    path = engine.personal_artifacts.store.db_path
    if not path.exists():
        return []
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT refs_json FROM artifact_session_refs WHERE workspace_key=? AND session_id=?",
            (engine.personal_artifacts.workspace_key, engine._session.id),
        ).fetchone()
    return json.loads(row[0]) if row else []


async def preview(system, *, engine=None, actor_id="main", session_id=None):
    engine = engine or system.engine
    return await execute_research_operation(
        engine.personal_research,
        {
            "operation": "context_preview",
            "session_id": session_id or engine._session.id,
            "actor_id": actor_id,
        },
        trusted_channel="web",
    )


def recovery_arguments(view, request_id="context-original-rollforward"):
    return {
        "operation": "context_rollforward",
        "session_id": view["session_id"],
        "actor_id": view["actor_id"],
        "request_id": request_id,
        "expected_context_revision": view["context_revision"],
        "expected_plan_digest": view["plan_digest"],
    }


async def recover(system, view, request_id="context-original-rollforward", *, engine=None):
    engine = engine or system.engine
    return await execute_research_operation(
        engine.personal_research, recovery_arguments(view, request_id), trusted_channel="web"
    )


async def request(system, request_id="context-original-rollforward", *, engine=None):
    engine = engine or system.engine
    return await execute_research_operation(
        engine.personal_research,
        {"operation": "context_request", "request_id": request_id},
        trusted_channel="web",
    )


async def pinned(system, *, identity="context", stale=True):
    created = await base.model(
        system,
        "create",
        request_id=identity + "-create",
        title="研究上下文 " + identity,
        question="核对😀原始来源的收入和成本",
        domain="general",
    )
    captured = await base.capture(system, created, identity + "-capture")
    arguments = {"operation": "get", "id": captured["notebook"]["id"]}
    returned = await base.model(system, "get", id=captured["notebook"]["id"])
    returned = await system.service.model_result(arguments, returned, "main")
    refs = await raw_refs(system)
    snapshots = [
        await system.engine.personal_artifacts.get(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        for ref in refs
    ]
    research = [item for item in snapshots if item.get("evidence", {}).get("research_read_refs")]
    assert research
    session = system.engine._session
    session.add_message("user", "继续核对这份研究，保留原历史。😀")
    session.add_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": identity + "-tool-read",
                "type": "function",
                "function": {
                    "name": "personal_research",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    )
    session.add_message(
        "tool", json.dumps(returned, ensure_ascii=False), tool_call_id=identity + "-tool-read"
    )
    await system.engine.session_store.save(session)
    value = SimpleNamespace(
        view=captured,
        source=captured["sources"][0],
        original_refs=copy.deepcopy(refs),
        original_material=research[-1],
        messages=copy.deepcopy(session.messages),
        returned=returned,
    )
    if stale:
        await recheck(system, value, identity=identity)
    return value


async def recheck(system, original, *, identity="context", expected_revision=1):
    return await base.manual(
        system,
        "recheck",
        source_id=original.source["id"],
        expected_revision=expected_revision,
        request_id=identity + "-recheck-" + str(expected_revision),
    )


async def new_engine(system):
    engine = create_agent_engine(base.configuration(system.root))
    engine.personal_research.clock = system.clock
    engine.personal_research.capture_transport = system.capture
    assert await engine.load_session(system.engine._session.id)
    return engine


async def shared_input(system):
    artifacts = system.engine.personal_artifacts
    owner = system.engine.personal_agents.create(
        request_id="context-explicit-input-owner",
        name="Iris",
        instructions="保留用户指定资料的原授权",
        tool_names=[],
    )
    material = await artifacts.publish(
        actor_id=owner.id,
        request_id="context-shared-original",
        title="上下文原授权资料",
        kind="material",
        filename="original.txt",
        content="PRIVATE_CONTEXT_SOURCE_5208 😀".encode(),
    )
    grant = await artifacts.share(
        actor_id=owner.id,
        artifact_id=material["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="context-explicit-grant",
    )
    shared = await artifacts.get(actor_id="main", artifact_id=material["artifact_id"], version=1)
    ref = artifacts.reference(shared)
    await artifacts.bind_refs(system.engine._session.id, "main", [ref])
    return owner, grant, ref


async def revoke(system, owner, grant):
    await system.engine.personal_artifacts.revoke(
        actor_id=owner.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="context-revoke-original-grant",
    )


async def assert_send_blocked(system, engine=None):
    engine = engine or system.engine
    with pytest.raises(PersonalAgentError) as error:
        await engine._prepare_messages_for_model(copy.deepcopy(engine._session.messages))
    assert error.value.code in {
        "source_stale",
        "source_unavailable",
        "artifact_access_revoked",
        "agent_inactive",
    }


async def test_recheck_remains_blocked_until_explicit_context_rollforward(context_system):
    system = context_system
    original = await pinned(system)
    await assert_send_blocked(system)
    before_refs = await raw_refs(system)
    view = await preview(system)
    assert view["status"] == "ready" and view["context_revision"] == 0
    assert view["session_id"] == system.engine._session.id and view["actor_id"] == "main"
    assert re.fullmatch(r"[a-f0-9]{64}", view["plan_digest"])
    assert view["historical_notice"]
    assert any(
        change["source_id"] == original.source["id"]
        and change["from_revision"] == 1
        and change["to_revision"] == 2
        for change in view["changes"]
    )
    assert await raw_refs(system) == before_refs
    await assert_send_blocked(system)
    result = await recover(system, view)
    assert (
        result["status"] == "completed" and result["request_id"] == "context-original-rollforward"
    )
    assert result["session_id"] == view["session_id"] and result["actor_id"] == "main"
    assert result["context_revision"] == 1 and result["updated_count"] >= 1
    assert result["view"]["status"] == "current"
    await system.engine.personal_artifacts.authorize_engine()
    assert system.capture.calls == [base.URL, base.URL]


async def test_rollforward_preserves_original_refs_messages_and_exact_utf8_body(context_system):
    system = context_system
    original = await pinned(system)
    original_ref = system.engine.personal_artifacts.reference(original.original_material)
    raw_body = await system.engine.personal_artifacts._call(
        "read_content", actor_id="main", artifact_id=original_ref["artifact_id"], version=1
    )
    view = await preview(system)
    material = next(row for row in view["materials"] if row["original_ref"] == original_ref)
    assert material["body_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert material["read_count"] >= 1
    await recover(system, view)
    effective = await system.engine.personal_artifacts.session_refs(
        system.engine._session.id, "main"
    )
    assert original_ref not in effective and await raw_refs(system) == original.original_refs
    mirrors = []
    for ref in effective:
        snapshot, body = await system.engine.personal_artifacts.read(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        if snapshot.get("evidence", {}).get("research_context_refs"):
            mirrors.append((snapshot, body))
    matching = [
        (snapshot, body) for snapshot, body in mirrors if original_ref in snapshot["source_refs"]
    ]
    assert len(matching) == 1 and matching[0][1] == raw_body
    snapshot = matching[0][0]
    with sqlite3.connect(system.engine.personal_artifacts.store.db_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM artifact_versions WHERE artifact_id=? AND version=?",
            (snapshot["artifact_id"], snapshot["version"]),
        ).fetchone()
    assert row is not None and json.loads(row[0])["authority_version"] == 8
    context_ref = snapshot["evidence"]["research_context_refs"][0]
    assert context_ref["original_ref"] == original_ref
    assert context_ref["revision"] == 1 and re.fullmatch(r"rcu_[a-f0-9]{32}", context_ref["id"])
    assert context_ref["workspace_key"] == view["workspace_key"]
    assert re.fullmatch(r"[a-f0-9]{64}", context_ref["record_digest"])
    assert system.engine._session.messages == original.messages
    persisted = await system.engine.session_store.load(system.engine._session.id)
    assert persisted.messages == original.messages
    prepared = await system.engine._prepare_messages_for_model(copy.deepcopy(original.messages))
    original_tool = next(
        message["content"] for message in original.messages if message["role"] == "tool"
    )
    assert any(message.get("content") == original_tool for message in prepared)
    notices = [
        str(message.get("content", "")) for message in prepared if message.get("role") == "system"
    ]
    assert any(
        "旧资料" in notice and "历史依据" in notice and original.source["id"] in notice
        for notice in notices
    )
    assert any('"semantic_verification":"not_performed"' in notice for notice in notices)


async def test_original_artifact_stays_stale_outside_session_context_recovery(context_system):
    system = context_system
    original = await pinned(system)
    ref = system.engine.personal_artifacts.reference(original.original_material)
    await recover(system, await preview(system))
    await system.engine.personal_artifacts.authorize_engine()
    with pytest.raises(PersonalAgentError) as error:
        await system.engine.personal_artifacts.read(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
    assert error.value.code in {"source_stale", "source_unavailable"}


async def test_actual_model_call_boundary_accepts_only_explicit_recovered_messages(
    context_system, monkeypatch
):
    system = context_system
    original = await pinned(system)
    calls = []
    sentinel = object()

    async def transport(**arguments):
        calls.append(copy.deepcopy(arguments))
        return sentinel

    monkeypatch.setattr(system.engine, "_model_port", SimpleNamespace(call=transport))
    with pytest.raises(PersonalAgentError):
        await system.engine._call_model_with_recovery(
            messages=copy.deepcopy(original.messages), tier=ModelTier.FAST, tools=None
        )
    assert calls == []
    await recover(system, await preview(system))
    result = await system.engine._call_model_with_recovery(
        messages=copy.deepcopy(original.messages), tier=ModelTier.FAST, tools=None
    )
    assert result is sentinel and len(calls) == 1
    tool_body = next(
        message["content"] for message in original.messages if message["role"] == "tool"
    )
    assert any(message.get("content") == tool_body for message in calls[0]["messages"])
    assert "历史依据" in calls[0]["messages"][0]["content"]
    assert all("metadata" not in message for message in calls[0]["messages"])
    assert system.engine._session.messages == original.messages


async def test_original_acl_loss_during_message_preparation_stops_the_real_send_fence(
    context_system, monkeypatch
):
    system = context_system
    owner, grant, _ = await shared_input(system)
    original = await pinned(system)
    await recover(system, await preview(system))
    calls = []
    prepare = system.engine.personal_memory_runtime.prepare_messages
    injected = False

    async def preparing(messages):
        nonlocal injected
        result = await prepare(messages)
        if not injected:
            injected = True
            await revoke(system, owner, grant)
        return result

    async def transport(**arguments):
        calls.append(arguments)
        raise AssertionError("Revoked original material reached transport")

    monkeypatch.setattr(system.engine.personal_memory_runtime, "prepare_messages", preparing)
    monkeypatch.setattr(system.engine, "_model_port", SimpleNamespace(call=transport))
    with pytest.raises(PersonalAgentError):
        await system.engine._call_model_with_recovery(
            messages=copy.deepcopy(original.messages), tier=ModelTier.FAST, tools=None
        )
    assert injected and calls == []
    assert await raw_refs(system) == original.original_refs


async def test_context_operation_is_idempotent_and_changed_original_request_conflicts(
    context_system,
):
    system = context_system
    await pinned(system)
    view = await preview(system)
    first = await recover(system, view)
    after = await system.engine.personal_artifacts.session_refs(system.engine._session.id, "main")
    second = await recover(system, view)
    assert second == first
    assert (
        await system.engine.personal_artifacts.session_refs(system.engine._session.id, "main")
        == after
    )
    lookup = await request(system)
    assert lookup["status"] == "completed" and lookup["request_id"] == first["request_id"]
    changed = recovery_arguments(view)
    changed["expected_plan_digest"] = "f" * 64 if view["plan_digest"] != "f" * 64 else "e" * 64
    with pytest.raises(PersonalAgentError) as error:
        await execute_research_operation(system.service, changed, trusted_channel="web")
    assert error.value.code == "idempotency_conflict"


async def test_unknown_context_request_is_read_only_and_not_a_replay_instruction(context_system):
    system = context_system
    before = await raw_refs(system)
    result = await request(system, "never-created-context-request")
    assert result == {"status": "unknown", "request_id": "never-created-context-request"}
    assert await raw_refs(system) == before and system.capture.calls == []


async def test_current_context_preview_has_no_mutating_effect(context_system):
    system = context_system
    original = await pinned(system, stale=False)
    first = await preview(system)
    second = await preview(system)
    assert first == second and first["status"] == "current"
    assert first["changes"] == []
    assert await raw_refs(system) == original.original_refs
    assert system.capture.calls == [base.URL]


async def test_old_plan_is_rejected_after_another_source_observation(context_system):
    system = context_system
    original = await pinned(system)
    first = await preview(system)
    await recheck(system, original, expected_revision=2)
    with pytest.raises(PersonalAgentError) as error:
        await recover(system, first)
    assert error.value.code in {"source_stale", "version_conflict", "context_changed"}
    assert await raw_refs(system) == original.original_refs
    await assert_send_blocked(system)


async def test_old_plan_cannot_drop_a_late_bound_unrelated_input(context_system):
    system = context_system
    original = await pinned(system)
    first = await preview(system)
    artifacts = system.engine.personal_artifacts
    material = await artifacts.publish(
        actor_id="main",
        request_id="late-context-input",
        title="后来选择的独立资料",
        kind="material",
        filename="late.txt",
        content=b"UNRELATED_LATE_INPUT",
    )
    late_ref = artifacts.reference(material)
    with pytest.raises(PersonalAgentError) as error:
        await artifacts.bind_refs(system.engine._session.id, "main", [late_ref])
    assert error.value.code == "source_stale"
    assert late_ref in await raw_refs(system)
    with pytest.raises(PersonalAgentError) as error:
        await recover(system, first)
    assert error.value.code in {"source_stale", "version_conflict", "context_changed"}
    new_plan = await preview(system)
    await recover(system, new_plan, "context-with-late-input")
    assert {json.dumps(ref, sort_keys=True) for ref in await raw_refs(system)} == {
        json.dumps(ref, sort_keys=True) for ref in [*original.original_refs, late_ref]
    }
    effective = await artifacts.session_refs(system.engine._session.id, "main")
    assert late_ref in effective


async def test_rebinding_an_effective_mirror_keeps_raw_history_but_deduplicates_effective_refs(
    context_system,
):
    system = context_system
    original = await pinned(system)
    await recover(system, await preview(system))
    artifacts = system.engine.personal_artifacts
    before = await artifacts.session_refs(system.engine._session.id, "main")
    assert len(before) == 1
    mirror = before[0]
    assert mirror not in original.original_refs
    await artifacts.bind_refs(system.engine._session.id, "main", [mirror])
    raw = await raw_refs(system)
    assert all(ref in raw for ref in original.original_refs) and mirror in raw
    assert await artifacts.session_refs(system.engine._session.id, "main") == before
    await artifacts.bind_refs(system.engine._session.id, "main", [mirror])
    assert await raw_refs(system) == raw
    assert await artifacts.session_refs(system.engine._session.id, "main") == before


async def test_page_context_mirror_preserves_embedded_refs_and_exact_page_bytes(context_system):
    system = context_system
    original = await pinned(system, stale=False)
    artifacts = system.engine.personal_artifacts
    original_ref = artifacts.reference(original.original_material)
    page = {
        "version": 1,
        "type": "page",
        "title": "研究文档 😀",
        "sections": [
            {
                "id": "original-section",
                "title": "原始研究",
                "markdown": "旧结果保留原文，不代表最新事实。",
                "evidence_status": "unknown",
                "source_refs": [original_ref],
            }
        ],
    }
    page_body = json.dumps(page, ensure_ascii=False, indent=2).encode()
    snapshot = await artifacts.publish(
        actor_id="main",
        request_id="context-original-page",
        title=page["title"],
        kind="output",
        filename="original-page.json",
        content=page_body,
    )
    page_ref = artifacts.reference(snapshot)
    assert original_ref in snapshot["source_refs"]
    await artifacts.bind_refs(system.engine._session.id, "main", [page_ref])
    raw = await raw_refs(system)
    await recheck(system, original)
    plan = await preview(system)
    assert any(item["original_ref"] == page_ref for item in plan["materials"])
    await recover(system, plan)
    effective = await artifacts.session_refs(system.engine._session.id, "main")
    matches = []
    for ref in effective:
        value, body = await artifacts.read(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        proofs = value.get("evidence", {}).get("research_context_refs", [])
        if any(proof["original_ref"] == page_ref for proof in proofs):
            matches.append((value, body))
    assert len(matches) == 1
    mirrored, body = matches[0]
    assert body == page_body and json.loads(body) == page
    assert page_ref in mirrored["source_refs"] and original_ref in mirrored["source_refs"]
    assert await raw_refs(system) == raw
    await artifacts.authorize_engine()


async def test_persisted_running_turn_from_other_engine_blocks_until_original_run_is_terminal(
    context_system,
):
    system = context_system
    original = await pinned(system)
    plan = await preview(system)
    engine = await new_engine(system)
    run = await system.engine.chat_run_store.start_run(
        session_id=system.engine._session.id,
        user_message_id="context-original-running-message",
        run_id="context-original-live-run",
    )
    try:
        assert getattr(engine, "_personal_active_turn", None) is None
        with pytest.raises(PersonalAgentError) as error:
            await recover(system, plan, engine=engine)
        assert error.value.code == "research_busy"
        observed = await engine.chat_run_store.get_run(system.engine._session.id, run.id)
        assert observed is not None and observed.status == "running"
        assert await raw_refs(system) == original.original_refs
        await system.engine.chat_run_store.finish_run(run.id, status="completed")
        assert (await recover(system, plan, engine=engine))["status"] == "completed"
        observed = await engine.chat_run_store.get_run(system.engine._session.id, run.id)
        assert observed is not None and observed.status == "completed"
        await engine.personal_artifacts.authorize_engine()
    finally:
        await engine.shutdown()


async def test_new_actor_version_can_explicitly_checkpoint_but_old_mirror_remains_stale(
    context_system,
):
    system = context_system
    engine = system.engine
    profile = engine.personal_agents.create(
        request_id="context-explicit-versioned-agent",
        name="Mira",
        instructions="按用户选择研究真实资料",
        tool_names=[],
    )
    opened = await engine.personal_agents.open_session(profile.id)
    assert await engine.load_session(opened["session_id"])
    created = await base.model(
        system,
        "create",
        actor=profile.id,
        request_id="context-actor-notebook",
        title="版本化 Agent 研究",
        question="核对来源",
        domain="general",
    )
    captured = await base.model(
        system,
        "capture",
        actor=profile.id,
        request_id="context-actor-capture",
        id=created["notebook"]["id"],
        url=base.URL,
        max_age_days=1,
    )
    fields = {"operation": "get", "id": captured["notebook"]["id"]}
    body = await base.model(system, "get", actor=profile.id, id=captured["notebook"]["id"])
    await system.service.model_result(fields, body, profile.id)
    raw = await raw_refs(system)
    system.clock.value += timedelta(days=2)
    first_plan = await preview(system, actor_id=profile.id)
    first = await recover(system, first_plan, "context-actor-first-update")
    first_refs = await engine.personal_artifacts.session_refs(engine._session.id, profile.id)
    assert len(first_refs) == 1
    old_mirror = first_refs[0]
    revised = engine.personal_agents.update(
        profile.id, profile.version, instructions="继续核对，保留原研究依据与历史"
    )
    assert revised.version == profile.version + 1
    with pytest.raises(PersonalAgentError) as error:
        await engine.personal_artifacts.read(
            actor_id=profile.id,
            artifact_id=old_mirror["artifact_id"],
            version=old_mirror["version"],
        )
    assert error.value.code == "source_stale"
    current = await preview(system, actor_id=profile.id)
    assert current["actor_version"] == revised.version and current["status"] == "ready"
    second = await recover(system, current, "context-actor-second-update")
    assert second["context_revision"] == first["context_revision"] + 1
    await engine.personal_artifacts.authorize_engine()
    assert await raw_refs(system) == raw
    with pytest.raises(PersonalAgentError) as error:
        await engine.personal_artifacts.read(
            actor_id=profile.id,
            artifact_id=old_mirror["artifact_id"],
            version=old_mirror["version"],
        )
    assert error.value.code == "source_stale"


async def test_completed_context_survives_new_engine_and_does_not_capture_again(context_system):
    system = context_system
    original = await pinned(system)
    first = await recover(system, await preview(system))
    calls = list(system.capture.calls)
    engine = await new_engine(system)
    try:
        assert engine._session.messages == original.messages
        assert await raw_refs(system, engine) == original.original_refs
        cold = await preview(system, engine=engine)
        assert cold["status"] == "current" and cold["context_revision"] == first["context_revision"]
        await engine.personal_artifacts.authorize_engine()
        assert (await request(system, engine=engine))["status"] == "completed"
        assert system.capture.calls == calls
    finally:
        await engine.shutdown()


async def test_new_source_change_invalidates_mirror_and_requires_another_explicit_operation(
    context_system,
):
    system = context_system
    original = await pinned(system)
    first = await recover(system, await preview(system))
    await recheck(system, original, expected_revision=2)
    await assert_send_blocked(system)
    current = await preview(system)
    assert current["status"] == "ready" and current["context_revision"] == first["context_revision"]
    second = await recover(system, current, "context-second-rollforward")
    assert second["context_revision"] == first["context_revision"] + 1
    assert await raw_refs(system) == original.original_refs
    await system.engine.personal_artifacts.authorize_engine()


@pytest.mark.parametrize("when", ["before_preview", "after_preview", "after_completed"])
async def test_original_private_grant_is_never_bypassed_by_recovery(context_system, when):
    system = context_system
    owner, grant, _ = await shared_input(system)
    original = await pinned(system)
    if when == "before_preview":
        await revoke(system, owner, grant)
        try:
            view = await preview(system)
        except PersonalAgentError:
            pass
        else:
            assert view["status"] == "blocked" and view["materials"] == []
    else:
        view = await preview(system)
        if when == "after_completed":
            await recover(system, view)
        await revoke(system, owner, grant)
        if when == "after_preview":
            with pytest.raises(PersonalAgentError):
                await recover(system, view)
        else:
            lookup = await request(system)
            assert lookup["status"] == "completed" and "receipt" not in lookup
            assert base.BODY not in json.dumps(lookup, ensure_ascii=False)
    assert await raw_refs(system) == original.original_refs
    await assert_send_blocked(system)


async def test_expiry_rollforward_is_explicit_but_does_not_call_capture(context_system):
    system = context_system
    original = await pinned(system, stale=False)
    system.clock.value += timedelta(days=2)
    await assert_send_blocked(system)
    view = await preview(system)
    assert view["status"] == "ready"
    assert any(
        change["from_expired"] is False and change["to_expired"] is True
        for change in view["changes"]
    )
    await recover(system, view)
    await system.engine.personal_artifacts.authorize_engine()
    assert await raw_refs(system) == original.original_refs and system.capture.calls == [base.URL]


async def test_two_context_writers_have_one_cas_winner_and_preserve_every_original_ref(
    context_system,
):
    system = context_system
    original = await pinned(system)
    view = await preview(system)
    engine = await new_engine(system)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                recover(system, view, "context-writer-one"),
                recover(system, view, "context-writer-two", engine=engine),
                return_exceptions=True,
            ),
            timeout=WAIT,
        )
        passed = [value for value in results if isinstance(value, dict)]
        errors = [value for value in results if isinstance(value, PersonalAgentError)]
        assert len(passed) == len(errors) == 1
        assert passed[0]["context_revision"] == 1
        assert errors[0].code in {
            "version_conflict",
            "research_busy",
            "source_stale",
            "context_changed",
        }
        assert await raw_refs(system) == original.original_refs
        await system.engine.personal_artifacts.authorize_engine()
    finally:
        await engine.shutdown()


@pytest.mark.parametrize("mutation", ["source", "grant"])
async def test_mid_save_dependency_change_never_activates_partial_context(
    context_system, monkeypatch, mutation
):
    system = context_system
    owner = grant = None
    if mutation == "grant":
        owner, grant, _ = await shared_input(system)
    original = await pinned(system)
    view = await preview(system)
    artifacts = system.engine.personal_artifacts
    publish = artifacts.publish
    injected, mirrors = False, []

    async def changing_publish(**arguments):
        nonlocal injected
        result = await publish(**arguments)
        if arguments.get("evidence", {}).get("research_context_refs") and not injected:
            injected = True
            mirrors.append(artifacts.reference(result))
            if mutation == "grant":
                await revoke(system, owner, grant)
            else:
                await recheck(system, original, expected_revision=2)
        return result

    monkeypatch.setattr(artifacts, "publish", changing_publish)
    with pytest.raises(PersonalAgentError):
        await recover(system, view)
    assert injected and mirrors and await raw_refs(system) == original.original_refs
    lookup = await request(system)
    assert lookup["status"] == "pending" and "receipt" not in lookup and "resume" not in lookup
    for mirror in mirrors:
        with pytest.raises(PersonalAgentError):
            await artifacts.read(
                actor_id="main", artifact_id=mirror["artifact_id"], version=mirror["version"]
            )
    await assert_send_blocked(system)


async def test_nested_derived_material_keeps_ancestry_bytes_and_original_root(context_system):
    system = context_system
    original = await pinned(system, stale=False)
    artifacts = system.engine.personal_artifacts
    original_ref = artifacts.reference(original.original_material)
    first = await artifacts.publish(
        actor_id="main",
        request_id="context-derived-first",
        title="研究派生一",
        kind="output",
        filename="first.md",
        content="# 原研究派生 😀\n".encode(),
        source_refs=[original_ref],
    )
    second = await artifacts.publish(
        actor_id="main",
        request_id="context-derived-second",
        title="研究派生二",
        kind="output",
        filename="second.md",
        content=b"# Exact derived payload\n",
        source_refs=[artifacts.reference(first)],
    )
    second_ref = artifacts.reference(second)
    await artifacts.bind_refs(system.engine._session.id, "main", [second_ref])
    before = await raw_refs(system)
    await recheck(system, original)
    view = await preview(system)
    assert any(row["original_ref"] == second_ref for row in view["materials"])
    await recover(system, view)
    effective = await artifacts.session_refs(system.engine._session.id, "main")
    matches = []
    for ref in effective:
        material, content = await artifacts.read(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        if second_ref in material.get("source_refs", []):
            matches.append(content)
    assert matches == [b"# Exact derived payload\n"] and await raw_refs(system) == before
    with pytest.raises(PersonalAgentError):
        await artifacts.read(actor_id="main", artifact_id=second_ref["artifact_id"], version=1)


async def test_model_cannot_rollforward_by_calling_domain_or_shared_adapter(context_system):
    system = context_system
    await pinned(system)
    fields = recovery_arguments(await preview(system))
    for invoke in (
        lambda: system.service.execute_operation(fields, caller_actor="main"),
        lambda: execute_research_operation(system.service, fields, caller_actor="main"),
    ):
        with pytest.raises(PersonalAgentError) as error:
            await invoke()
        assert error.value.code == "scope_denied"
    await assert_send_blocked(system)


async def test_model_preview_is_bound_to_its_actual_actor_and_session(context_system):
    system = context_system
    original_session = system.engine._session.id
    view = await execute_research_operation(
        system.service,
        {"operation": "context_preview", "session_id": original_session, "actor_id": "main"},
        caller_actor="main",
    )
    assert view["session_id"] == original_session and view["status"] == "current"
    foreign = system.engine.personal_agents.create(
        request_id="context-other-agent", name="Mira", instructions="独立研究", tool_names=[]
    )
    for fields in (
        {"session_id": "abcdef123456", "actor_id": "main"},
        {"session_id": original_session, "actor_id": foreign.id},
    ):
        with pytest.raises(PersonalAgentError) as error:
            await execute_research_operation(
                system.service, {"operation": "context_preview", **fields}, caller_actor="main"
            )
        assert error.value.code in {"scope_denied", "binding_conflict", "not_found"}


async def test_active_model_turn_cannot_be_mutated_by_human_context_operation(context_system):
    system = context_system
    await pinned(system)
    view = await preview(system)
    system.engine._personal_active_turn = (system.engine._session.id, "main", 0)
    try:
        with pytest.raises((PersonalAgentError, RuntimeError)):
            await recover(system, view)
    finally:
        system.engine._personal_active_turn = None
    await assert_send_blocked(system)


async def test_approved_memory_ancestry_is_not_erased_by_historical_context_mirrors(
    approved_context,
):
    system = approved_context
    engine = system.engine
    view = await egress_base.notebook(system)
    memory = await engine.personal_memory.dispatch(
        "create",
        {
            "request_id": "context-original-private-memory",
            "topic": "研究的原记忆",
            "category": "fact",
            "content": "PRIVATE_CONTEXT_MEMORY_5208",
            "scope": "workspace",
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    engine.config.memory.long_term_enabled = True
    engine._personal_memory_runtime_refs = [memory_reference(memory)]
    approved = await egress_base.decide(system, await egress_base.propose(system, view))
    completed = await egress_base.execute(system, approved)
    source = completed["result"]["sources"][0]
    current = await engine.personal_research.execute_operation(
        {"operation": "get", "id": view["notebook"]["id"]}, caller_actor="main"
    )
    await engine.personal_research.model_result(
        {"operation": "get", "id": view["notebook"]["id"]}, current, "main"
    )
    original_refs = await raw_refs(system)
    await egress_base.manual(
        system,
        "recheck",
        source_id=source["id"],
        expected_revision=1,
        request_id="context-memory-recheck",
    )
    await assert_send_blocked(system)
    await recover(system, await preview(system))
    await engine.personal_artifacts.authorize_engine()
    await engine.personal_memory.dispatch(
        "delete",
        {
            "request_id": "context-erase-original-memory",
            "memory_id": memory["memory_id"],
            "expected_revision": memory["revision"],
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    with pytest.raises(PersonalAgentError):
        await engine.personal_artifacts.authorize_engine()
    lookup = await request(system)
    assert lookup["status"] == "completed" and "receipt" not in lookup
    assert await raw_refs(system) == original_refs and len(system.transport.captures) == 2
