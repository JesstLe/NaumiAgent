"""Actual SQLite authority, erasure, grants and lexical retrieval contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from naumi_agent.personal.memory_models import (
    content_digest,
    memory_id_for_request,
    normalize_human,
    normalize_ref,
    normalize_scope,
    normalize_source,
    reference,
    request_fingerprint,
    topic_key,
)
from naumi_agent.personal.memory_store import PersonalMemoryStore
from naumi_agent.personal.models import PersonalAgentError

OWNER = hashlib.sha256(b"owner-catalog").hexdigest()
OTHER_OWNER = hashlib.sha256(b"other-catalog").hexdigest()
PA = "pa_" + "a" * 32
PB = "pa_" + "b" * 32
WORK_A = "1" * 64
WORK_B = "2" * 64
PERSONAL = {"kind": "personal", "workspace_key": "", "item_id": ""}
WORKSPACE = {"kind": "workspace", "workspace_key": WORK_A, "item_id": ""}
ITEM = {"kind": "item", "workspace_key": WORK_A, "item_id": "pi_" + "c" * 32}
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def human(action):
    return {"channel": "management_web", "action": action, "actor_kind": "human"}


def source(
    content="用户明确输入的内容", *, kind="manual", authority="user", ceiling=None, refs=None
):
    return {
        "kind": kind,
        "authority": authority,
        "locator": {"request_id": "source-original"},
        "excerpt": content,
        "excerpt_sha256": content_digest(content),
        "artifact_refs": refs or [],
        "scope_ceiling": ceiling or PERSONAL,
        "captured_at": NOW.isoformat(),
    }


@pytest.fixture
def store(tmp_path):
    return PersonalMemoryStore(tmp_path / "memory", owner_key=OWNER, clock=lambda: NOW)


def create(store, request="create-one", **updates):
    fields = {
        "owner_key": OWNER,
        "actor_id": "main",
        "request_id": request,
        "scope": PERSONAL,
        "topic": "界面配色",
        "category": "preference",
        "content": "界面使用黑白配色",
        "source": source("界面使用黑白配色"),
        "human": human("create"),
        **updates,
    }
    return store.create(**fields)


def confirm(store, record, *, request="confirm-one", expected=None):
    return store.confirm(
        owner_key=OWNER,
        actor_id=record["actor_id"],
        memory_id=record["memory_id"],
        expected_revision=record["revision"],
        expected_content_sha256=record["content_sha256"],
        expected_active_ref=expected,
        request_id=request,
        human=human("confirm"),
    )


def revise(store, record, content, *, request="revise-one"):
    return store.revise(
        owner_key=OWNER,
        actor_id=record["actor_id"],
        memory_id=record["memory_id"],
        expected_revision=record["revision"],
        expected_content_sha256=record["content_sha256"],
        content=content,
        source=source(content),
        request_id=request,
        human=human("revise"),
    )


def remove(store, record, *, request="delete-one", action="delete"):
    return store.control(
        owner_key=OWNER,
        actor_id=record["actor_id"],
        memory_id=record["memory_id"],
        expected_revision=record["revision"],
        request_id=request,
        action=action,
        human=human(action),
    )


def share(store, record, *, request="share-one", recipient=PA, version=2, refs=None):
    return store.share(
        owner_key=OWNER,
        actor_id=record["actor_id"],
        memory_id=record["memory_id"],
        expected_revision=record["revision"],
        expected_content_sha256=record["content_sha256"],
        request_id=request,
        recipient_actor_id=recipient,
        recipient_version=version,
        source_refs=refs or [],
        human=human("share"),
    )


def error(code, callback):
    with pytest.raises(PersonalAgentError) as exc:
        callback()
    assert exc.value.code == code, exc.value


def test_fresh_constructor_and_reads_do_not_create_authority(store):
    assert store.identity(owner_key=OWNER) is None
    assert store.list(owner_key=OWNER) == []
    assert store.active_records(owner_key=OWNER) == []
    assert store.visible_grants(owner_key=OWNER, recipient_actor_id=PA) == []
    assert store.pending_index(owner_key=OWNER) == []
    assert store.lexical_search(owner_key=OWNER, query="黑白") == []
    assert store.find_request(owner_key=OWNER, request_id="absent") is None
    assert not store.base_dir.exists()


def test_real_manual_save_and_model_proposal_keep_source_and_confirmation_separate(store):
    active = create(store)
    candidate = create(
        store,
        "assistant-proposal",
        topic="计划建议",
        human=None,
        scope=WORKSPACE,
        source=source(kind="message", authority="assistant", ceiling=WORKSPACE),
    )
    assert active["state"] == "active" and active["confirmation"]["actor_kind"] == "human"
    assert candidate["state"] == "proposed" and candidate["confirmation"] is None
    confirmed = confirm(store, candidate)
    assert confirmed["state"] == "active"
    assert confirmed["source"]["authority"] == "assistant"
    assert confirmed["source"]["kind"] == "message"
    assert confirmed["confirmation"]["channel"] == "management_web"
    assert len(store.active_records(owner_key=OWNER)) == 2


def test_slot_collision_does_not_replace_until_exact_human_confirmation(store):
    old = create(store)
    candidate = create(store, "new-colour", content="界面使用浅灰配色", source=source("浅灰"))
    assert candidate["state"] == "proposed"
    assert store.get(owner_key=OWNER, memory_id=old["memory_id"])["state"] == "active"
    error("memory_conflict", lambda: confirm(store, candidate))
    current = confirm(store, candidate, expected=reference(old))
    retired = store.get(owner_key=OWNER, memory_id=old["memory_id"])
    assert current["state"] == "active" and current["revision"] == 2
    assert retired["state"] == "superseded" and retired["content"] == ""
    assert retired["source"] is None
    error(
        "memory_access_revoked",
        lambda: store.authorize_ref(owner_key=OWNER, actor_id="main", ref=reference(old)),
    )
    assert (
        store.create(
            owner_key=OWNER,
            actor_id="main",
            request_id="create-one",
            scope=PERSONAL,
            topic="界面配色",
            category="preference",
            content="界面使用黑白配色",
            source=source("界面使用黑白配色"),
            human=human("create"),
        )["state"]
        == "superseded"
    )


def test_two_real_connections_confirm_one_active_topic(store):
    a = create(store, "proposal-a", human=None)
    b = create(store, "proposal-b", human=None)

    def apply(record):
        independent = PersonalMemoryStore(store.base_dir, owner_key=OWNER, clock=lambda: NOW)
        try:
            return confirm(independent, record, request="confirm-" + record["memory_id"])
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [a, b]))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert [r for r in results if isinstance(r, str)] == ["memory_conflict"]
    assert len(store.active_records(owner_key=OWNER)) == 1


def test_parallel_replacements_fence_original_revision(store):
    old = create(store)
    candidates = [create(store, "replacement-" + str(i), human=None) for i in range(2)]

    def replace(record):
        independent = PersonalMemoryStore(store.base_dir, owner_key=OWNER, clock=lambda: NOW)
        try:
            return confirm(
                independent,
                record,
                request="confirm-" + record["memory_id"],
                expected=reference(old),
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(replace, candidates))
    assert len([v for v in values if isinstance(v, dict)]) == 1
    assert [v for v in values if isinstance(v, str)] == ["version_conflict"]
    assert len(store.active_records(owner_key=OWNER)) == 1


def test_actor_and_scope_topics_are_independent_and_scope_is_not_promoted(store):
    values = [
        create(store, "p-main"),
        create(store, "p-a", actor_id=PA),
        create(store, "w-main", scope=WORKSPACE),
        create(store, "i-main", scope=ITEM),
    ]
    assert all(v["state"] == "active" for v in values)
    assert len(store.list(owner_key=OWNER, actor_id="main", scope=PERSONAL)) == 1
    assert len(store.list(owner_key=OWNER, actor_id=PA)) == 1
    error(
        "scope_denied",
        lambda: create(
            store,
            "forbidden-personal",
            topic="项目资料",
            source=source("项目私有", kind="artifact", authority="external", ceiling=WORKSPACE),
        ),
    )
    assert len(store.active_records(owner_key=OWNER)) == 4


def test_create_replay_resolves_current_head_and_binds_every_original_field(store):
    active = create(store)
    assert create(store) == active
    changed = revise(store, active, "只使用浅色布局")
    assert create(store) == changed
    error("idempotency_conflict", lambda: create(store, content="another-value"))
    error("idempotency_conflict", lambda: create(store, actor_id=PA))
    fresh_source = source("界面使用黑白配色")
    fresh_source["captured_at"] = (NOW + timedelta(days=1)).isoformat()
    assert create(store, source=fresh_source) == changed


def test_revision_cas_source_lineage_and_old_body_erasure(store):
    old = create(store, content="OLD_SECRET_3b30", source=source("OLD_EXCERPT_7c54"))
    grant = share(store, old)
    changed = revise(store, old, "新记忆正文")
    assert changed["revision"] == 2 and changed["scope"] == old["scope"]
    assert changed["topic_key"] == old["topic_key"]
    events = store.history(owner_key=OWNER, memory_id=old["memory_id"])
    assert events[-1]["previous_source_sha256"] == old["source_sha256"]
    assert "OLD_SECRET" not in json.dumps(events)
    assert "OLD_EXCERPT" not in json.dumps(events)
    assert store.grants(owner_key=OWNER, memory_id=old["memory_id"])[0]["state"] == "revoked"
    error(
        "memory_access_revoked",
        lambda: store.authorize_ref(owner_key=OWNER, actor_id=PA, ref=reference(old, grant)),
    )
    error("version_conflict", lambda: revise(store, old, "stale", request="stale-revise"))
    with sqlite3.connect(store.db_path) as db:
        rows = db.execute("SELECT payload_json FROM memory_payloads").fetchall()
        assert len(rows) == 1 and "OLD_SECRET" not in rows[0][0] and "OLD_EXCERPT" not in rows[0][0]


def test_delete_removes_plaintext_fts_grants_and_original_request_replay(store):
    marker = "MEMORY_DELETE_SENTINEL_f250"
    old = create(store, topic=marker, content=marker, source=source(marker))
    shared = share(store, old)
    deleted = remove(store, old)
    assert deleted["state"] == "deleted" and deleted["content"] == ""
    assert deleted["topic"] == "" and deleted["source"] is None
    replay = create(store, topic=marker, content=marker, source=source(marker))
    assert replay == deleted
    receipt = store.find_request(owner_key=OWNER, request_id="create-one")
    assert receipt["record"] == deleted and receipt["result"] == deleted
    assert marker not in json.dumps(store.history(owner_key=OWNER, memory_id=old["memory_id"]))
    assert store.lexical_search(owner_key=OWNER, query=marker) == []
    assert store.visible_grants(owner_key=OWNER, recipient_actor_id=PA) == []
    error(
        "memory_access_revoked",
        lambda: store.authorize_ref(owner_key=OWNER, actor_id=PA, ref=reference(old, shared)),
    )
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_payloads").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
    reopened = PersonalMemoryStore(store.base_dir, owner_key=OWNER)
    assert reopened.get(owner_key=OWNER, memory_id=old["memory_id"]) == deleted


def test_gui_prepared_body_is_server_only_and_erased_with_target_or_finish(store):
    request = "gui-create"
    payload = {"request_id": request, "topic": "敏感记忆", "content": "PREPARED_SECRET_59a0"}
    fingerprint = request_fingerprint("main", WORK_A, "create", payload)
    metadata = store.prepare_request(
        owner_key=OWNER,
        request_id=request,
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=fingerprint,
    )
    assert "payload" not in metadata and "PREPARED_SECRET" not in json.dumps(metadata)
    assert metadata["target_memory_id"] == memory_id_for_request(OWNER, request)
    assert store.get_prepared_request(owner_key=OWNER, request_id=request)["payload"] == payload
    record = create(store, request, content=payload["content"], source=source(payload["content"]))
    deleted = remove(store, record)
    assert store.get_prepared_request(owner_key=OWNER, request_id=request)["payload"] is None
    result = {"memory_id": record["memory_id"], "revision": deleted["revision"], "state": "deleted"}
    finished = store.finish_request(owner_key=OWNER, request_id=request, result=result)
    assert finished["state"] == "applied" and "payload" not in finished
    assert store.finish_request(owner_key=OWNER, request_id=request, result=result) == finished
    assert (
        store.prepare_request(
            owner_key=OWNER,
            request_id=request,
            actor_id="main",
            workspace_key=WORK_A,
            operation="create",
            payload=payload,
            fingerprint=fingerprint,
        )["state"]
        == "applied"
    )
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT payload_json FROM memory_gui_requests").fetchone()[0] is None


def test_finished_create_request_erases_body_and_preserves_only_ids(store):
    payload = {"request_id": "gui-accepted", "content": "PENDING_BODY_c354"}
    store.prepare_request(
        owner_key=OWNER,
        request_id="gui-accepted",
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=request_fingerprint("main", WORK_A, "create", payload),
    )
    error(
        "binding_conflict",
        lambda: store.finish_request(
            owner_key=OWNER, request_id="gui-accepted", result={"memory_id": "pm_" + "0" * 32}
        ),
    )
    record = create(store, "gui-accepted")
    finished = store.finish_request(
        owner_key=OWNER,
        request_id="gui-accepted",
        result={"memory_id": record["memory_id"], "ref": reference(record)},
    )
    assert finished["result"]["ref"] == reference(record)
    assert store.get_prepared_request(owner_key=OWNER, request_id="gui-accepted")["payload"] is None
    error(
        "invalid_input",
        lambda: store.finish_request(
            owner_key=OWNER, request_id="gui-accepted", result={"content": "bad"}
        ),
    )


def test_prepared_request_identity_and_fingerprint_are_immutable(store):
    payload = {"request_id": "gui-id", "content": "原始正文"}
    fields = {
        "owner_key": OWNER,
        "request_id": "gui-id",
        "actor_id": "main",
        "workspace_key": WORK_A,
        "operation": "create",
        "payload": payload,
        "fingerprint": request_fingerprint("main", WORK_A, "create", payload),
    }
    assert store.prepare_request(**fields) == store.prepare_request(**fields)
    changed = {**payload, "content": "新的正文"}
    error(
        "idempotency_conflict",
        lambda: store.prepare_request(
            **{
                **fields,
                "payload": changed,
                "fingerprint": request_fingerprint("main", WORK_A, "create", changed),
            }
        ),
    )
    error("binding_conflict", lambda: store.prepare_request(**{**fields, "fingerprint": "f" * 64}))


def test_exact_share_and_revoke_do_not_use_replacement_grant(store):
    record = create(store)
    first = share(store, record)
    assert share(store, record) == first
    first_ref = reference(record, first)
    assert (
        store.authorize_ref(owner_key=OWNER, actor_id=PA, ref=first_ref)["content"]
        == record["content"]
    )
    error(
        "memory_access_revoked",
        lambda: store.authorize_ref(owner_key=OWNER, actor_id=PB, ref=first_ref),
    )
    revoked = store.revoke(
        owner_key=OWNER,
        actor_id="main",
        grant_id=first["grant_id"],
        expected_revision=1,
        request_id="revoke-original",
        human=human("revoke"),
    )
    assert revoked["state"] == "revoked" and revoked["revision"] == 2
    second = share(store, record, request="new-explicit-share")
    assert second["grant_id"] != first["grant_id"]
    error(
        "memory_access_revoked",
        lambda: store.authorize_ref(owner_key=OWNER, actor_id=PA, ref=first_ref),
    )
    assert store.authorize_ref(owner_key=OWNER, actor_id=PA, ref=reference(record, second))
    assert share(store, record)["state"] == "revoked"


def test_share_binds_actual_recipient_source_grant_and_version(store):
    original = {
        "artifact_id": "art_" + "1" * 32,
        "version": 1,
        "sha256": "2" * 64,
        "grant_id": "",
        "grant_revision": 0,
    }
    recipient_ref = {**original, "grant_id": "asg_" + "3" * 32, "grant_revision": 1}
    record = create(
        store,
        scope=WORKSPACE,
        source=source(
            "共享来源", kind="artifact", authority="external", ceiling=WORKSPACE, refs=[original]
        ),
    )
    error("binding_conflict", lambda: share(store, record))
    granted = share(store, record, request="exact-sources", refs=[recipient_ref])
    assert granted["source_refs"] == [recipient_ref]
    assert granted["recipient_version"] == 2
    error(
        "version_conflict",
        lambda: share(store, record, request="version-changed", version=3, refs=[recipient_ref]),
    )


def test_chinese_english_actual_fts_and_filter_before_limit(store):
    a = create(
        store, "workspace-a", scope=WORKSPACE, content="晚上整理资料 使用黑白布局 prefer light mode"
    )
    b_scope = {"kind": "workspace", "workspace_key": WORK_B, "item_id": ""}
    create(store, "workspace-b", scope=b_scope, content="prefer light mode light mode light mode")
    create(store, "other-agent", actor_id=PA, content="prefer light mode light mode")
    assert (
        store.lexical_search(owner_key=OWNER, query="黑白", actor_id="main", scopes=[WORKSPACE])[0][
            "memory_id"
        ]
        == a["memory_id"]
    )
    hits = store.lexical_search(
        owner_key=OWNER, query="light mode", actor_id="main", scopes=[WORKSPACE], limit=1
    )
    assert len(hits) == 1 and hits[0]["memory_id"] == a["memory_id"]
    assert hits[0]["tokenizer_version"] == "cjk-bigram-word-v1"
    assert store.lexical_search(owner_key=OWNER, query="light", scopes=[]) == []
    assert store.lexical_search(owner_key=OWNER, query="light", memory_ids=[]) == []
    assert store.lexical_search(owner_key=OWNER, query="") == []
    assert store.lexical_search(owner_key=OWNER, query='" OR * ; DROP TABLE memory_heads --') == []
    assert store.get(owner_key=OWNER, memory_id=a["memory_id"])["state"] == "active"


def test_index_outbox_is_content_free_durable_and_acknowledged_exactly(store):
    record = create(store, content="INDEX_PRIVATE_BODY_25b4")
    pending = store.pending_index(owner_key=OWNER)
    assert len(pending) == 1 and pending[0]["action"] == "upsert"
    assert "INDEX_PRIVATE_BODY" not in json.dumps(pending)
    store.ack_index(owner_key=OWNER, event_id=pending[0]["event_id"])
    store.ack_index(owner_key=OWNER, event_id=pending[0]["event_id"])
    assert store.pending_index(owner_key=OWNER) == []
    new = revise(store, record, "新正文")
    pending = store.pending_index(owner_key=OWNER)
    assert {x["action"] for x in pending} == {"delete", "upsert"}
    assert {x["revision"] for x in pending} == {1, new["revision"]}
    restarted = PersonalMemoryStore(store.base_dir, owner_key=OWNER)
    assert restarted.pending_index(owner_key=OWNER) == pending


@pytest.mark.parametrize(
    "kind,authority",
    [
        ("message", "assistant"),
        ("artifact", "external"),
        ("legacy", "unknown"),
        ("compaction", "unknown"),
    ],
)
def test_nonmanual_proposals_do_not_self_activate(store, kind, authority):
    record = create(
        store,
        "candidate-" + kind,
        scope=WORKSPACE,
        human=None,
        source=source("材料", kind=kind, authority=authority, ceiling=WORKSPACE),
    )
    assert record["state"] == "proposed"
    assert store.active_records(owner_key=OWNER) == []
    assert store.lexical_search(owner_key=OWNER, query="黑白") == []
    rejected = remove(store, record, action="reject", request="reject-" + kind)
    assert rejected["state"] == "rejected" and rejected["content"] == ""
    assert store.get(owner_key=OWNER, memory_id=record["memory_id"])["source"] is None


@pytest.mark.parametrize("mutator", ["confirm", "revise", "delete", "share"])
def test_mutations_require_trusted_human_channel(store, mutator):
    record = create(store, human=None)
    if mutator == "confirm":

        def callback():
            return store.confirm(
                owner_key=OWNER,
                actor_id="main",
                memory_id=record["memory_id"],
                expected_revision=1,
                expected_content_sha256=record["content_sha256"],
                expected_active_ref=None,
                request_id="bad-human",
                human=None,
            )
    elif mutator == "revise":

        def callback():
            return store.revise(
                owner_key=OWNER,
                actor_id="main",
                memory_id=record["memory_id"],
                expected_revision=1,
                expected_content_sha256=record["content_sha256"],
                content="新值",
                source=source("新值"),
                request_id="bad-human",
                human=None,
            )
    elif mutator == "delete":

        def callback():
            return store.control(
                owner_key=OWNER,
                actor_id="main",
                memory_id=record["memory_id"],
                expected_revision=1,
                action="delete",
                request_id="bad-human",
                human=None,
            )
    else:
        record = confirm(store, record)

        def callback():
            return store.share(
                owner_key=OWNER,
                actor_id="main",
                memory_id=record["memory_id"],
                expected_revision=record["revision"],
                expected_content_sha256=record["content_sha256"],
                request_id="bad-human",
                recipient_actor_id=PA,
                recipient_version=1,
                source_refs=[],
                human=None,
            )

    error("human_confirmation_required", callback)


def test_owner_binding_missing_db_and_missing_trigger_fail_closed(store):
    record = create(store)
    identity = store.identity(owner_key=OWNER)
    assert len(identity) == 32
    error("not_found", lambda: store.list(owner_key=OTHER_OWNER))
    alternate = PersonalMemoryStore(store.base_dir, owner_key=OTHER_OWNER)
    error("storage_error", lambda: alternate.list(owner_key=OTHER_OWNER))
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER memory_events_no_delete")
    error("storage_error", lambda: store.get(owner_key=OWNER, memory_id=record["memory_id"]))
    error("storage_error", lambda: create(store, "do-not-repair"))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_heads").fetchone()[0] == 1


def test_local_witness_prevents_missing_or_replaced_database_recreation(store):
    create(store)
    moved = store.db_path.with_suffix(".original")
    store.db_path.rename(moved)
    fresh = PersonalMemoryStore(store.base_dir, owner_key=OWNER)
    error("storage_error", lambda: fresh.list(owner_key=OWNER))
    error("storage_error", lambda: create(fresh, "lost-db"))
    assert not store.db_path.exists()
    sqlite3.connect(store.db_path).close()
    error("storage_error", lambda: create(fresh, "empty-replacement"))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_corrupted_erasable_body_and_immutable_history_are_detected(store):
    record = create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE memory_payloads SET payload_json='{}' WHERE memory_id=?", (record["memory_id"],)
        )
    error("storage_error", lambda: store.get(owner_key=OWNER, memory_id=record["memory_id"]))
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM memory_events")


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "personal", "workspace_key": WORK_A, "item_id": ""},
        {"kind": "workspace", "workspace_key": WORK_A, "item_id": ITEM["item_id"]},
        {"kind": "item", "workspace_key": WORK_A, "item_id": ""},
        {"kind": "personal", "workspace_key": "", "item_id": "", "actor": "main"},
    ],
)
def test_strict_scope_rejects_invalid_identity_combinations(scope):
    error("invalid_input", lambda: normalize_scope(scope))


def test_normalizers_bind_excerpt_ref_topic_and_typed_human():
    assert topic_key("  LIGHT　Mode ") == topic_key("light mode")
    invalid_source = source()
    invalid_source["excerpt_sha256"] = "f" * 64
    error("invalid_input", lambda: normalize_source(invalid_source))
    nested = source()
    nested["locator"] = {"data": {"content": "private-body"}}
    error("invalid_input", lambda: normalize_source(nested))
    error("invalid_input", lambda: normalize_human({"channel": "model", "actor_kind": "human"}))
    error(
        "invalid_input", lambda: normalize_human({"channel": "management_web", "content": "body"})
    )
    ref = {
        "memory_id": "pm_" + "f" * 32,
        "revision": 1,
        "content_sha256": "e" * 64,
        "grant_id": "",
        "grant_revision": 1,
    }
    error("invalid_input", lambda: normalize_ref(ref))


def test_payload_hash_tampering_in_prepared_gui_request_is_not_accepted(store):
    payload = {"content": "exact-body"}
    store.prepare_request(
        owner_key=OWNER,
        request_id="prepared",
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=request_fingerprint("main", WORK_A, "create", payload),
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE memory_gui_requests SET payload_json='{}'")
    error(
        "storage_error", lambda: store.get_prepared_request(owner_key=OWNER, request_id="prepared")
    )


def test_candidate_confirmation_and_delete_replays_after_later_changes_are_current(store):
    candidate = create(store, human=None)
    active = confirm(store, candidate)
    deleted = remove(store, active)
    assert confirm(store, candidate) == deleted
    assert remove(store, active) == deleted
    assert store.find_request(owner_key=OWNER, request_id="confirm-one")["result"] == deleted


def test_prepared_request_cannot_be_claimed_by_another_actor_or_operation(store):
    request = "reserved-gui-id"
    payload = {"request_id": request, "content": "原GUI正文"}
    store.prepare_request(
        owner_key=OWNER,
        request_id=request,
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=request_fingerprint("main", WORK_A, "create", payload),
    )
    error("idempotency_conflict", lambda: create(store, request, actor_id=PA))
    assert store.list(owner_key=OWNER) == []
    accepted = create(store, request)
    assert accepted["actor_id"] == "main"
    store.finish_request(
        owner_key=OWNER, request_id=request, result={"memory_id": accepted["memory_id"]}
    )
    direct = create(store, "already-direct")
    later_payload = {"request_id": "already-direct", "memory_id": direct["memory_id"]}
    error(
        "idempotency_conflict",
        lambda: store.prepare_request(
            owner_key=OWNER,
            request_id="already-direct",
            actor_id="main",
            workspace_key=WORK_A,
            operation="delete",
            payload=later_payload,
            fingerprint=request_fingerprint("main", WORK_A, "delete", later_payload),
        ),
    )


def test_deleted_prepared_update_does_not_resurrect_later_and_same_id_fence_remains(store):
    old = create(store)
    payload = {
        "request_id": "pending-correction",
        "memory_id": old["memory_id"],
        "content": "STALE_PREPARED_c75a",
    }
    store.prepare_request(
        owner_key=OWNER,
        request_id="pending-correction",
        actor_id="main",
        workspace_key=WORK_A,
        operation="revise",
        payload=payload,
        fingerprint=request_fingerprint("main", WORK_A, "revise", payload),
    )
    remove(store, old)
    pending = store.get_prepared_request(owner_key=OWNER, request_id="pending-correction")
    assert pending["state"] == "erased" and pending["payload"] is None
    error(
        "lifecycle_conflict",
        lambda: revise(
            store,
            store.get(owner_key=OWNER, memory_id=old["memory_id"]),
            "new",
            request="new-correction",
        ),
    )
    assert "STALE_PREPARED" not in json.dumps(pending)


def test_schema_indices_and_rewound_state_fail_closed_instead_of_empty_recall(store):
    record = create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE memory_heads SET state='deleted'")
    error("storage_error", lambda: store.active_records(owner_key=OWNER))
    error("storage_error", lambda: store.lexical_search(owner_key=OWNER, query="黑白"))
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE memory_heads SET state='active'")
        db.execute("DROP INDEX memory_active_topic")
    error("storage_error", lambda: store.get(owner_key=OWNER, memory_id=record["memory_id"]))


def test_fts5_secure_delete_is_actually_enabled_and_payload_identity_is_exact(store):
    record = create(store)
    with sqlite3.connect(store.db_path) as db:
        configured = dict(db.execute("SELECT k,v FROM memory_fts_config").fetchall())
        assert configured["secure-delete"] == 1
        stored = db.execute(
            "SELECT payload_json FROM memory_payloads WHERE memory_id=?", (record["memory_id"],)
        ).fetchone()[0]
    assert json.loads(stored)["content"] == record["content"]
    assert json.loads(stored)["excerpt"] == record["source"]["excerpt"]


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "PLAINTEXT_HIDDEN_IN_TYPE"},
        {"memory_id": "not-an-id"},
        {"grant_id": "not-a-grant"},
        {"state": "body-text"},
        {"conflict": "yes"},
        {"operation": "body with whitespace"},
        {"receipt_id": "raw body with whitespace"},
    ],
)
def test_finished_request_result_has_only_typed_metadata(store, bad):
    error(
        "invalid_input",
        lambda: store.finish_request(owner_key=OWNER, request_id="none", result=bad),
    )


def test_same_create_request_from_separate_connections_chooses_exact_winner(store):
    def apply(index):
        independent = PersonalMemoryStore(store.base_dir, owner_key=OWNER, clock=lambda: NOW)
        evidence = source("界面使用黑白配色")
        evidence["captured_at"] = (NOW + timedelta(seconds=index)).isoformat()
        return create(independent, source=evidence)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(apply, range(16)))
    assert all(r == results[0] for r in results)
    assert len(store.list(owner_key=OWNER)) == 1


def test_first_authority_creation_is_serialized_across_real_os_processes(store):
    script = """
import json, sys
from naumi_agent.personal.memory_store import PersonalMemoryStore
store = PersonalMemoryStore(sys.argv[1], owner_key=sys.argv[2])
fields = json.loads(sys.argv[3])
print('READY', flush=True)
assert sys.stdin.readline().strip() == 'GO'
record = store.create(**fields)
print(json.dumps({'record': record, 'instance': store.identity(owner_key=sys.argv[2])}), flush=True)
"""
    fields = {
        "owner_key": OWNER,
        "actor_id": "main",
        "request_id": "first-process-request",
        "scope": PERSONAL,
        "topic": "进程间首次创建",
        "category": "preference",
        "content": "真实进程共享同一资料库和同一请求赢家",
        "source": source(),
        "human": human("create"),
    }
    processes = []
    try:
        for _ in range(6):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(store.base_dir), OWNER, json.dumps(fields)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            ))
        for process in processes:
            assert process.stdout.readline().strip() == "READY"
        assert not store.db_path.exists()
        for process in processes:
            process.stdin.write("GO\n")
            process.stdin.flush()
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=25)
            assert process.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
        assert all(outcome == outcomes[0] for outcome in outcomes)
        assert len(store.list(owner_key=OWNER)) == 1
        assert store._bootstrap.read_bytes() == b"\x00"
        assert store.identity(owner_key=OWNER) == outcomes[0]["instance"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_bootstrap_lock_is_released_by_os_after_holder_process_exits(store):
    script = """
import sys
from naumi_agent.personal.memory_store import PersonalMemoryStore
store = PersonalMemoryStore(sys.argv[1], owner_key=sys.argv[2])
store.base_dir.mkdir(parents=True)
with store._bootstrap_guard(create=True):
    print('LOCKED', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(store.base_dir), OWNER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        assert not store.db_path.exists()
        process.kill()
        process.wait(timeout=5)
        assert create(store)["state"] == "active"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_bootstrap_lock_rejects_hardlink_without_touching_external_file(store, tmp_path):
    outside = tmp_path / "external"
    outside.write_bytes(b"ORIGINAL_EXTERNAL_FILE")
    store.base_dir.mkdir()
    os.link(outside, store._bootstrap)
    error("storage_error", lambda: create(store))
    assert outside.read_bytes() == b"ORIGINAL_EXTERNAL_FILE"
    assert not store.db_path.exists()


def test_discard_unapplied_gui_draft_erases_body_and_never_revives_same_request(store):
    request = "discard-unapplied"
    payload = {"request_id": request, "content": "ABANDONED_DRAFT_5b7f"}
    fingerprint = request_fingerprint("main", WORK_A, "create", payload)
    store.prepare_request(
        owner_key=OWNER,
        request_id=request,
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=fingerprint,
    )
    discarded = store.discard_request(
        owner_key=OWNER, request_id=request, expected_fingerprint=fingerprint
    )
    assert discarded["state"] == "erased" and "payload" not in discarded
    assert "ABANDONED_DRAFT" not in json.dumps(discarded)
    assert store.get_prepared_request(owner_key=OWNER, request_id=request)["payload"] is None
    assert (
        store.discard_request(owner_key=OWNER, request_id=request, expected_fingerprint=fingerprint)
        == discarded
    )
    assert (
        store.prepare_request(
            owner_key=OWNER,
            request_id=request,
            actor_id="main",
            workspace_key=WORK_A,
            operation="create",
            payload=payload,
            fingerprint=fingerprint,
        )
        == discarded
    )
    error(
        "request_discarded",
        lambda: create(
            store, request, content=payload["content"], source=source(payload["content"])
        ),
    )
    assert store.list(owner_key=OWNER) == []
    assert store.find_request(owner_key=OWNER, request_id=request) is None
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT payload_json FROM memory_gui_requests").fetchone()[0] is None


def test_discard_requires_exact_fingerprint_and_preserves_original_draft_on_mismatch(store):
    request = "discard-fingerprint"
    payload = {"request_id": request, "content": "EXACT_GUI_DRAFT_2c10"}
    store.prepare_request(
        owner_key=OWNER,
        request_id=request,
        actor_id=PA,
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=request_fingerprint(PA, WORK_A, "create", payload),
    )
    before = store.get_prepared_request(owner_key=OWNER, request_id=request)
    error(
        "version_conflict",
        lambda: store.discard_request(
            owner_key=OWNER, request_id=request, expected_fingerprint="f" * 64
        ),
    )
    after = store.get_prepared_request(owner_key=OWNER, request_id=request)
    assert after == before and after["payload"] == payload
    assert store.list(owner_key=OWNER) == []
    error(
        "not_found",
        lambda: store.discard_request(
            owner_key=OWNER, request_id="absent-draft", expected_fingerprint="a" * 64
        ),
    )


def test_discard_after_real_commit_preserves_current_fact_and_later_tombstone(store):
    request = "commit-before-discard"
    payload = {"request_id": request, "content": "COMMITTED_BODY_521c"}
    fingerprint = request_fingerprint("main", WORK_A, "create", payload)
    store.prepare_request(
        owner_key=OWNER,
        request_id=request,
        actor_id="main",
        workspace_key=WORK_A,
        operation="create",
        payload=payload,
        fingerprint=fingerprint,
    )
    active = create(store, request, content=payload["content"], source=source(payload["content"]))
    assert store.get_prepared_request(owner_key=OWNER, request_id=request)["payload"] is not None
    discarded = store.discard_request(
        owner_key=OWNER, request_id=request, expected_fingerprint=fingerprint
    )
    assert discarded["state"] == "applied"
    assert discarded["result"] == {"memory_id": active["memory_id"]}
    assert store.get(owner_key=OWNER, memory_id=active["memory_id"]) == active
    assert (
        create(store, request, content=payload["content"], source=source(payload["content"]))
        == active
    )
    deleted = remove(store, active, request="explicit-delete-after-commit")
    assert (
        store.discard_request(
            owner_key=OWNER, request_id=request, expected_fingerprint=fingerprint
        )["state"]
        == "applied"
    )
    assert (
        create(store, request, content=payload["content"], source=source(payload["content"]))
        == deleted
    )
    assert deleted["state"] == "deleted" and deleted["content"] == ""


def test_discard_and_apply_real_sqlite_transactions_obey_both_winner_orders(tmp_path):
    for winner in ("discard", "apply"):
        base = tmp_path / winner
        coordinator = PersonalMemoryStore(base, owner_key=OWNER, clock=lambda: NOW)
        apply_store = PersonalMemoryStore(base, owner_key=OWNER, clock=lambda: NOW)
        discard_store = PersonalMemoryStore(base, owner_key=OWNER, clock=lambda: NOW)
        request = "race-" + winner
        payload = {"request_id": request, "content": "PREFETCHED_BODY_" + winner}
        fingerprint = request_fingerprint("main", WORK_A, "create", payload)
        coordinator.prepare_request(
            owner_key=OWNER,
            request_id=request,
            actor_id="main",
            workspace_key=WORK_A,
            operation="create",
            payload=payload,
            fingerprint=fingerprint,
        )
        # The applying request has already read its body before either transaction wins.
        prefetched = apply_store.get_prepared_request(owner_key=OWNER, request_id=request)[
            "payload"
        ]
        reached, release, contender = Event(), Event(), Event()
        original_write = discard_store._write_prepared
        original_receipt = apply_store._save_receipt

        def hold_erasure(db, value):
            original_write(db, value)
            reached.set()
            assert release.wait(timeout=5)

        def hold_committed_receipt(*args, **kwargs):
            original_receipt(*args, **kwargs)
            reached.set()
            assert release.wait(timeout=5)

        if winner == "discard":
            discard_store._write_prepared = hold_erasure
        else:
            apply_store._save_receipt = hold_committed_receipt

        def apply():
            if winner == "discard":
                contender.set()
            try:
                return create(
                    apply_store,
                    request,
                    content=prefetched["content"],
                    source=source(prefetched["content"]),
                )
            except PersonalAgentError as exc:
                return exc.code

        def discard():
            if winner == "apply":
                contender.set()
            return discard_store.discard_request(
                owner_key=OWNER, request_id=request, expected_fingerprint=fingerprint
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(discard if winner == "discard" else apply)
            try:
                assert reached.wait(timeout=5)
                second = pool.submit(apply if winner == "discard" else discard)
                assert contender.wait(timeout=5)
            finally:
                release.set()
            first_result, second_result = first.result(timeout=10), second.result(timeout=10)
        applied, discarded = (
            (second_result, first_result) if winner == "discard" else (first_result, second_result)
        )
        current = coordinator.get_prepared_request(owner_key=OWNER, request_id=request)
        assert current["payload"] is None
        if winner == "discard":
            assert applied == "request_discarded" and discarded["state"] == "erased"
            assert coordinator.list(owner_key=OWNER) == []
            assert coordinator.find_request(owner_key=OWNER, request_id=request) is None
        else:
            assert applied["state"] == "active" and discarded["state"] == "applied"
            assert coordinator.get(owner_key=OWNER, memory_id=applied["memory_id"]) == applied
