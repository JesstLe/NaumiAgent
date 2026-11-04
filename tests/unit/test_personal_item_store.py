"""Real SQLite contracts for personal item definitions, preparations and identity refs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from naumi_agent.daemons.agent_jobs import AgentJobPayload, agent_job_id_for_request
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest, issue_agent_worker_request
from naumi_agent.personal.item_models import digest, normalize_definition, normalize_intent
from naumi_agent.personal.item_store import PersonalItemStore
from naumi_agent.personal.models import PersonalAgentError

OWNER = hashlib.sha256(b"local-catalog").hexdigest()
OTHER_OWNER = hashlib.sha256(b"other-owner").hexdigest()
NOW = datetime(2026, 11, 4, 8, tzinfo=UTC)


@pytest.fixture
def definition(tmp_path):
    return {
        "title": "整理资料",
        "instructions": "保留原文与来源，保存一份真实报告。",
        "tags": ["生活", "工作"],
        "assignee": {"id": "main", "version": 0, "name": "主 Agent", "kind": "main"},
        "origin": {"actor_id": "main", "session_id": "origin-session"},
        "route": {
            "workspace_key": "a" * 64,
            "workspace_root": str(tmp_path),
            "session_db_key": "b" * 64,
        },
        "schedule": {
            "mode": "once",
            "local_time": "2026-11-05T09:00:00",
            "timezone": "Asia/Shanghai",
            "fold": None,
            "due_at": "2026-11-05T01:00:00+00:00",
            "misfire_policy": "run_once",
            "grace_seconds": 86400,
        },
        "inputs": {"artifact_refs": [], "directories": []},
        "policy": {
            "tool_names": ["personal_artifacts", "code_execute"],
            "artifact_operations": ["get", "read", "publish"],
            "model_tier": "capable",
            "max_turns": 12,
            "max_cost_microusd": 2000000,
            "timeout_seconds": 180,
        },
        "output": {"destination": "personal_library", "format": "report"},
    }


@pytest.fixture
def store(tmp_path):
    return PersonalItemStore(tmp_path / "items", clock=lambda: NOW)


def create(store, definition, request="create-one"):
    return store.create(owner_key=OWNER, request_id=request, definition=definition)


def prepared(item, **updates):
    definition = item["definition"]
    occurrence = "pio_" + uuid.uuid4().hex
    session = "pie_" + uuid.uuid4().hex
    payload = AgentJobPayload(
        task_id=occurrence,
        session_id=session,
        task=definition["instructions"],
        context=json.dumps({"inputs": [], "definition_sha256": item["definition_sha256"]}),
        message_topic="personal-item-" + item["once_key"],
    )
    policy = definition["policy"]
    request = issue_agent_worker_request(
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_name=definition["assignee"]["id"],
        task=payload.task,
        context=payload.context,
        tool_scope=tuple(policy["tool_names"]),
        permission_mode="strict",
        model_tier=policy["model_tier"],
        max_turns=policy["max_turns"],
        max_budget_usd=policy["max_cost_microusd"] / 1000000,
        timeout_seconds=policy["timeout_seconds"],
        message_topic=payload.message_topic,
        issued_at=NOW.isoformat(),
    )
    value = {
        "occurrence_id": occurrence,
        "item_id": item["item_id"],
        "item_revision": item["revision"],
        "once_key": item["once_key"],
        "definition_sha256": item["definition_sha256"],
        "workspace_key": definition["route"]["workspace_key"],
        "schedule_id": "sch_pi_" + item["once_key"],
        "event_id": "event-" + item["once_key"],
        "execution_session_id": session,
        "task_id": occurrence,
        "actor_id": definition["assignee"]["id"],
        "agent_version": definition["assignee"]["version"],
        "input_refs": [],
        "input_manifest": [],
        "policy": policy,
        "main_policy_sha256": "c" * 64 if definition["assignee"]["id"] == "main" else "",
        "request": asdict(request),
        "payload": asdict(payload),
        "issued_at": NOW.isoformat(),
    }
    value = json.loads(json.dumps(value))
    value.update(updates)
    value["prepared_sha256"] = digest(value)
    return value


def prepare(store, item, intent=None):
    return store.prepare(
        owner_key=OWNER,
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=prepared(item) if intent is None else intent,
    )


def control(store, item, action, request=None):
    return store.control(
        owner_key=OWNER,
        item_id=item["item_id"],
        expected_revision=item["revision"],
        request_id=request or action + "-" + uuid.uuid4().hex,
        action=action,
    )


def assert_code(code, callback):
    with pytest.raises(PersonalAgentError) as error:
        callback()
    assert error.value.code == code, error.value


def test_constructor_and_fresh_reads_do_not_create_database(store):
    assert not store.base_dir.exists()
    assert store.identity(owner_key=OWNER) is None
    assert store.list(owner_key=OWNER) == []
    assert store.list_intents(owner_key=OWNER) == []
    assert store.find_request(owner_key=OWNER, request_id="missing") is None
    assert_code("not_found", lambda: store.get(owner_key=OWNER, item_id="pi_" + "1" * 32))
    assert not store.base_dir.exists()


def test_multiple_items_routes_restart_and_detached_returns(store, definition):
    first = create(store, definition)
    second_definition = deepcopy(definition)
    second_definition["route"]["workspace_key"] = "d" * 64
    second_definition["title"] = "学习资料"
    second = create(store, second_definition, "create-two")
    assert first["item_id"] != second["item_id"]
    assert len(store.list(owner_key=OWNER)) == 2
    assert store.list(owner_key=OWNER, workspace_key="d" * 64)[0]["item_id"] == second["item_id"]
    assert len(store.list(owner_key=OWNER, query="学习")) == 1
    first["definition"]["policy"]["tool_names"].append("forged")
    reopened = PersonalItemStore(store.base_dir)
    saved = reopened.get(owner_key=OWNER, item_id=first["item_id"])
    assert "forged" not in saved["definition"]["policy"]["tool_names"]
    assert saved["owner_key"] == OWNER
    assert_code(
        "storage_error", lambda: reopened.get(owner_key=OTHER_OWNER, item_id=first["item_id"])
    )


def test_create_idempotency_raw_now_fingerprint_and_no_resurrection(store, definition):
    first = create(store, definition)
    assert create(store, definition) == first
    changed = deepcopy(definition)
    changed["title"] = "新内容"
    assert_code("idempotency_conflict", lambda: create(store, changed))
    paused = control(store, first, "pause")
    assert create(store, definition) == paused
    receipt = store.find_request(owner_key=OWNER, request_id="create-one")
    assert receipt["revision"] == 1 and receipt["item"] == paused
    fingerprint = "e" * 64
    now_item = store.create(
        owner_key=OWNER,
        request_id="raw-now",
        definition=definition,
        request_fingerprint=fingerprint,
    )
    assert (
        store.create(
            owner_key=OWNER,
            request_id="raw-now",
            definition=changed,
            request_fingerprint=fingerprint,
        )
        == now_item
    )


def test_concurrent_identical_create_and_cas_preserve_one_winner(store, definition):
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda _: create(store, definition), range(20)))
    assert len({item["item_id"] for item in created}) == 1
    item = created[0]

    def update(index):
        try:
            return control(store, item, "pause", "pause-" + str(index))
        except PersonalAgentError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(update, range(8)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 7
    assert len(store.history(owner_key=OWNER, item_id=item["item_id"])) == 2


def test_separate_store_instances_share_sqlite_idempotency_and_stable_identity(store, definition):
    stores = [PersonalItemStore(store.base_dir) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(lambda other: create(other, definition), stores))
    assert len({item["item_id"] for item in items}) == 1
    identities = {other.identity(owner_key=OWNER) for other in stores}
    assert len(identities) == 1 and len(next(iter(identities))) == 32


def test_control_revisions_preserve_definition_and_prepared_once(store, definition):
    item = create(store, definition)
    intent = prepare(store, item)
    paused = control(store, item, "pause", "pause-one")
    assert paused["revision"] == 2
    assert paused["definition_sha256"] == item["definition_sha256"]
    assert paused["once_key"] == item["once_key"]
    resumed = control(store, paused, "resume")
    assert resumed["revision"] == 3 and resumed["state"] == "active"
    assert (
        store.prepare(
            owner_key=OWNER,
            item_id=item["item_id"],
            expected_definition_sha256=item["definition_sha256"],
            intent=intent,
        )
        == intent
    )
    assert control(store, item, "pause", "pause-one") == resumed
    assert len(store.list_intents(owner_key=OWNER)) == 1
    assert_code(
        "definition_conflict",
        lambda: store.update_definition(
            owner_key=OWNER,
            item_id=item["item_id"],
            expected_revision=3,
            request_id="edit-after-prepare",
            definition=definition,
        ),
    )


def test_definition_before_prepare_and_explicit_restart_get_new_once(store, definition):
    item = create(store, definition)
    changed = deepcopy(definition)
    changed["title"] = "更新定义"
    updated = store.update_definition(
        owner_key=OWNER,
        item_id=item["item_id"],
        expected_revision=1,
        request_id="update",
        definition=changed,
    )
    assert updated["revision"] == 2 and updated["definition_sha256"] != item["definition_sha256"]
    assert updated["once_key"] == item["once_key"]
    prepare(store, updated)
    cancelled = control(store, updated, "cancel")
    assert_code("lifecycle_conflict", lambda: control(store, cancelled, "resume"))
    restarted = store.restart(
        owner_key=OWNER,
        item_id=item["item_id"],
        expected_revision=3,
        request_id="restart",
        definition=changed,
    )
    assert restarted["once_key"] != item["once_key"] and restarted["revision"] == 4
    prepare(store, restarted)
    assert len(store.list_intents(owner_key=OWNER)) == 2
    assert (
        store.restart(
            owner_key=OWNER,
            item_id=item["item_id"],
            expected_revision=3,
            request_id="restart",
            definition=changed,
        )
        == restarted
    )


def test_prepare_concurrency_returns_original_signed_request(store, definition):
    item = create(store, definition)
    candidates = [prepared(item) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = list(pool.map(lambda intent: prepare(store, item, intent), candidates))
    assert len({intent["prepared_sha256"] for intent in winners}) == 1
    assert len({intent["execution_session_id"] for intent in winners}) == 1
    assert len(store.list_intents(owner_key=OWNER)) == 1
    assert normalize_intent(winners[0])["request"] == winners[0]["request"]


def test_bind_identity_is_monotone_and_not_execution_state(store, definition):
    item = create(store, definition)
    intent = prepare(store, item)
    fields = dict(intent["request"])
    fields["tool_scope"] = tuple(fields["tool_scope"])
    job_id = agent_job_id_for_request(AgentWorkerRequest(**fields))
    args = {
        "owner_key": OWNER,
        "occurrence_id": intent["occurrence_id"],
        "expected_prepared_sha256": intent["prepared_sha256"],
    }
    bound = store.bind(
        **args, references={"job_id": job_id, "namespace": "personal-engine-v1:" + "a" * 64}
    )
    result = store.bind(**args, references={"run_id": "real-run"})
    assert result["references"]["job_id"] == job_id
    assert result["prepared_sha256"] == intent["prepared_sha256"]
    assert store.bind(**args, references={"run_id": "real-run"}) == result
    assert_code("binding_conflict", lambda: store.bind(**args, references={"run_id": "other-run"}))
    assert_code("binding_conflict", lambda: store.bind(**args, references={"job_id": "wrong-job"}))
    for field in ("job_state", "read", "answer", "delivered"):
        assert_code(
            "invalid_input", lambda field=field: store.bind(**args, references={field: "x"})
        )
    assert bound["references_sha256"] != result["references_sha256"]
    cancelled = control(store, item, "cancel")
    assert cancelled["state"] == "cancelled"
    assert (
        store.bind(**args, references={"publication_id": "original-publication"})["references"][
            "run_id"
        ]
        == "real-run"
    )


@pytest.mark.parametrize(
    "section", ["assignee", "origin", "route", "schedule", "inputs", "policy", "output"]
)
def test_unknown_nested_fields_rejected(definition, section):
    definition[section]["unknown"] = True
    assert_code("invalid_input", lambda: normalize_definition(definition))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_turns", True),
        ("max_turns", 0),
        ("max_cost_microusd", True),
        ("max_cost_microusd", -1),
        ("timeout_seconds", 0),
        ("timeout_seconds", 1.5),
        ("tool_names", ["browser_goto"]),
        ("artifact_operations", ["share"]),
        ("max_cost_microusd", float("nan")),
    ],
)
def test_policy_numbers_and_scope_are_strict(definition, field, bad):
    definition["policy"][field] = bad
    assert_code("invalid_input", lambda: normalize_definition(definition))


@pytest.mark.parametrize(
    "local,fold,due,accept",
    [
        ("2026-03-08T02:30:00", None, "2026-03-08T07:30:00+00:00", False),
        ("2026-11-01T01:30:00", None, "2026-11-01T05:30:00+00:00", False),
        ("2026-11-01T01:30:00", 0, "2026-11-01T05:30:00+00:00", True),
        ("2026-11-01T01:30:00", 1, "2026-11-01T06:30:00+00:00", True),
        ("2026-11-01T01:30:00", True, "2026-11-01T06:30:00+00:00", False),
    ],
)
def test_iana_dst_gap_overlap_and_utc_are_checked(definition, local, fold, due, accept):
    definition["schedule"].update(
        timezone="America/New_York", local_time=local, fold=fold, due_at=due
    )
    if accept:
        assert normalize_definition(definition)["schedule"]["fold"] == fold
    else:
        assert_code("invalid_input", lambda: normalize_definition(definition))


@pytest.mark.parametrize("kind", ["main", "pa"])
def test_assignee_contract_has_no_occupational_default(store, definition, kind):
    if kind == "pa":
        definition["assignee"] = {
            "id": "pa_" + "1" * 32,
            "version": 3,
            "name": "Mira 🌙",
            "kind": "specialist",
        }
    item = create(store, definition)
    assert item["definition"]["assignee"] == definition["assignee"]
    assert prepare(store, item)["actor_id"] == definition["assignee"]["id"]


def test_intent_hash_payload_policy_and_inputs_fail_closed(store, definition):
    item = create(store, definition)
    for field, value in (
        ("prepared_sha256", "0" * 64),
        ("workspace_key", "d" * 64),
        ("agent_version", 1),
        ("execution_session_id", "pie_" + "e" * 32),
    ):
        candidate = prepared(item)
        candidate[field] = value
        if field != "prepared_sha256":
            candidate["prepared_sha256"] = digest(
                {k: v for k, v in candidate.items() if k != "prepared_sha256"}
            )
        with pytest.raises(PersonalAgentError):
            prepare(store, item, candidate)
    assert store.list_intents(owner_key=OWNER) == []


def test_directory_bounds_and_ref_schema_rejected(definition):
    directory = {"path": "notes", "pattern": "**/*", "max_files": 100, "max_bytes": 50 * 1024**2}
    definition["inputs"]["directories"] = [directory]
    assert normalize_definition(definition)["inputs"]["directories"] == [directory]
    for field, bad in (
        ("path", "../private"),
        ("pattern", "../*"),
        ("max_files", True),
        ("max_files", 101),
        ("max_bytes", 50 * 1024**2 + 1),
    ):
        changed = deepcopy(definition)
        changed["inputs"]["directories"][0][field] = bad
        assert_code("invalid_input", lambda: normalize_definition(changed))
    definition["inputs"]["artifact_refs"] = [{"artifact_id": "art_" + "f" * 32}]
    assert_code("invalid_input", lambda: normalize_definition(definition))


def test_sqlite_immutable_tables_and_corruption_are_not_silenced(store, definition):
    item = create(store, definition)
    intent = prepare(store, item)
    with sqlite3.connect(store.db_path) as db:
        for table in ("personal_item_revisions", "personal_item_requests", "personal_item_intents"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE personal_items SET snapshot='{}'")
    assert_code("storage_error", lambda: store.get(owner_key=OWNER, item_id=item["item_id"]))
    assert_code(
        "storage_error",
        lambda: store.get_intent(owner_key=OWNER, occurrence_id=intent["occurrence_id"]),
    )


def test_missing_original_database_and_unknown_schema_do_not_reinitialize(store, definition):
    item = create(store, definition)
    backup = store.db_path.with_suffix(".backup")
    store.db_path.rename(backup)
    assert_code("storage_error", lambda: store.get(owner_key=OWNER, item_id=item["item_id"]))
    reopened = PersonalItemStore(store.base_dir)
    assert_code("storage_error", lambda: reopened.list(owner_key=OWNER))
    assert not store.db_path.exists()
    backup.rename(store.db_path)
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA user_version=99")
    assert_code("unsupported_schema", lambda: reopened.list(owner_key=OWNER))


def test_trigger_decision_is_exact_and_does_not_fake_job_state(store, definition):
    item = create(store, definition)
    args = {
        "owner_key": OWNER,
        "item_id": item["item_id"],
        "once_key": item["once_key"],
        "event_id": "original-event",
        "decision": "expired",
        "detail": "超过补执行窗口",
    }
    receipt = store.record_decision(**args)
    assert store.record_decision(**args) == receipt
    assert_code(
        "idempotency_conflict", lambda: store.record_decision(**{**args, "decision": "skip"})
    )
    assert_code("invalid_input", lambda: store.record_decision(**{**args, "decision": "completed"}))
    assert store.list_intents(owner_key=OWNER) == []
    assert store.get(owner_key=OWNER, item_id=item["item_id"])["state"] == "active"


def test_input_error_decision_has_immutable_receipt_and_pure_projection(store, definition):
    item = create(store, definition)
    args = {
        "owner_key": OWNER,
        "item_id": item["item_id"],
        "once_key": item["once_key"],
        "event_id": "failed-input-event",
        "decision": "input-error",
        "detail": "原资料授权已撤销，模型未启动",
    }
    receipt = store.record_decision(**args)
    assert receipt["receipt_sha256"] == digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    before = store.db_path.stat().st_mtime_ns
    assert store.list_decisions(owner_key=OWNER, item_id=item["item_id"]) == [receipt]
    assert store.db_path.stat().st_mtime_ns == before
    assert store.list_intents(owner_key=OWNER) == []
    closed = control(store, item, "close")
    assert closed["definition_sha256"] == item["definition_sha256"]
    assert store.list_decisions(owner_key=OWNER, item_id=item["item_id"]) == [receipt]


def test_raw_mutation_fingerprint_and_run_now_keep_original_committed_once(store, definition):
    item = create(store, definition)
    fingerprint = "d" * 64
    args = {
        "owner_key": OWNER,
        "item_id": item["item_id"],
        "expected_revision": 1,
        "request_id": "run-original",
        "action": "run-now",
        "request_fingerprint": fingerprint,
    }
    assert store.control(**args) == item
    receipt = store.find_request(owner_key=OWNER, request_id="run-original")
    assert receipt["request_fingerprint"] == fingerprint
    assert receipt["committed_item"] == item
    cancelled = control(store, item, "cancel")
    assert_code(
        "item_inactive",
        lambda: store.control(
            owner_key=OWNER,
            item_id=cancelled["item_id"],
            expected_revision=2,
            request_id="inactive-now",
            action="run-now",
        ),
    )
    restarted = store.restart(
        owner_key=OWNER,
        item_id=item["item_id"],
        expected_revision=2,
        request_id="restart-for-new",
        definition=definition,
    )
    assert restarted["once_key"] != item["once_key"]
    replay = store.find_request(owner_key=OWNER, request_id="run-original")
    assert replay["item"] == restarted and replay["committed_item"] == item
    assert store.control(**args) == restarted
    assert len(store.history(owner_key=OWNER, item_id=item["item_id"])) == 3


def test_definition_mutation_raw_fingerprint_replay_does_not_resample(store, definition):
    item = create(store, definition)
    first = deepcopy(definition)
    first["title"] = "改为立即任务"
    args = {
        "owner_key": OWNER,
        "item_id": item["item_id"],
        "expected_revision": 1,
        "request_id": "raw-update",
        "request_fingerprint": "e" * 64,
    }
    updated = store.update_definition(**args, definition=first)
    changed = deepcopy(first)
    changed["instructions"] = "模拟派生正文变化，原raw请求已冻结"
    assert store.update_definition(**args, definition=changed) == updated
    paused = control(store, updated, "pause")
    assert_code(
        "item_inactive",
        lambda: store.control(
            owner_key=OWNER,
            item_id=item["item_id"],
            expected_revision=paused["revision"],
            request_id="paused-now",
            action="run-now",
        ),
    )


def test_prepared_exact_input_manifest_and_source_limits(store, definition):
    item = create(store, definition)
    ref = {
        "artifact_id": "art_" + "f" * 32,
        "version": 1,
        "sha256": "d" * 64,
        "grant_id": "",
        "grant_revision": 0,
    }
    manifest = [
        {
            "source": "artifact",
            "artifact_ref": ref,
            "filename": "notes.md",
            "bytes": 120,
            "sha256": "d" * 64,
        }
    ]
    intent = prepared(item, input_refs=[ref], input_manifest=manifest)
    value = prepare(store, item, intent)
    assert value["input_refs"] == [ref] and value["input_manifest"] == manifest
    changed = prepared(item, input_refs=[ref], input_manifest=[{**manifest[0], "bytes": 121}])
    assert_code("idempotency_conflict", lambda: prepare(store, item, changed))
    for field, bad in (("bytes", True), ("sha256", "e" * 64), ("filename", "../secret")):
        data = deepcopy(manifest)
        data[0][field] = bad
        assert_code("invalid_input", lambda: normalize_intent(prepared(item, input_manifest=data)))


def test_reference_head_detects_missing_history_and_preserves_prepared_digest(store, definition):
    item = create(store, definition)
    intent = prepare(store, item)
    bound = store.bind(
        owner_key=OWNER,
        occurrence_id=intent["occurrence_id"],
        expected_prepared_sha256=intent["prepared_sha256"],
        references={"run_id": "run-1"},
    )
    assert bound["prepared_sha256"] == intent["prepared_sha256"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE personal_item_reference_heads SET sequence=99")
    assert_code(
        "storage_error",
        lambda: store.get_intent(
            owner_key=OWNER,
            occurrence_id=intent["occurrence_id"],
        ),
    )


@pytest.mark.parametrize("limit,offset", [(True, 0), (0, 0), (501, 0), (10, -1), (10, True)])
def test_pagination_rejects_bool_negative_and_extreme_values(store, limit, offset):
    assert_code("invalid_input", lambda: store.list(owner_key=OWNER, limit=limit, offset=offset))
    assert_code(
        "invalid_input", lambda: store.list_intents(owner_key=OWNER, limit=limit, offset=offset)
    )
