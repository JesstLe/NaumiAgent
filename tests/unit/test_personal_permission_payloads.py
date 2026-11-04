"""Encrypted overflow permissions preserve full approval and schema-1 readers."""

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from naumi_agent.daemons.permission_decisions import permission_arguments_sha256
from naumi_agent.personal.collaboration_store import CollaborationStore
from naumi_agent.personal.models import PersonalAgentError

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
TABLE = "collaboration_permission_payloads"


def create(store, *, request="payload-test", workspace="workspace-a"):
    return store.create(
        workspace_key=workspace,
        source_session_id="source",
        source_agent_id="",
        request_id=request,
        title="确认",
        context="",
        steps=[
            {
                "id": "write",
                "agent_id": "pa_" + "a" * 32,
                "agent_version": 1,
                "task": "写入用户确认的内容",
                "depends_on": [],
            }
        ],
    )


def payload(plan, text):
    return {
        "call_id": "write-call",
        "session_id": plan["steps"][0]["session_id"],
        "tool_name": "file_write",
        "arguments": {"path": "report.md", "content": text},
        "reason": "写入文件需要确认",
        "risk_level": "high",
        "requires_confirmation": True,
        "permission_mode": "moderate",
        "scope": "call",
        "choices": ["allow_once", "deny"],
    }


def append(store, plan, data, key="permission:write:write-call"):
    return store.append_permission_request(
        plan["id"], plan["workspace_key"], key=key, step_id="write", data=data
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", KEY)
    return CollaborationStore(tmp_path / "collaboration")


@pytest.mark.parametrize("text", ["x" * 70000, "中文验收" * 8000], ids=["ascii70k", "utf8-96k"])
def test_large_payload_is_encrypted_and_full_parameters_survive_restart(store, text):
    plan = create(store)
    original = payload(plan, text)
    journal = append(store, plan, original)
    assert len(json.dumps(journal["data"], ensure_ascii=False).encode()) < 64 * 1024
    assert "arguments" not in journal["data"]
    # Old UIs must not offer blind approval of an unknown reference-only request.
    assert "session_id" not in journal["data"]
    assert "call_id" not in journal["data"]
    with sqlite3.connect(store.db_path) as db:
        raw = db.execute(f"SELECT envelope_json FROM {TABLE}").fetchone()[0]
        assert text not in raw
        assert "ciphertext_base64" in raw
        assert (
            len(db.execute("SELECT data_json FROM collaboration_events").fetchone()[0].encode())
            < 64 * 1024
        )
    restarted = CollaborationStore(store.base_dir)
    restored = restarted.permission_payload(plan["id"], "workspace-a", key=journal["key"])
    assert restored == original
    assert permission_arguments_sha256(restored["arguments"]) == permission_arguments_sha256(
        original["arguments"]
    )
    restored["arguments"]["content"] = "client mutation"
    assert restarted.permission_payload(plan["id"], "workspace-a", key=journal["key"]) == original


def test_small_requests_and_plain_journal_do_not_resolve_secret_key(store, monkeypatch):
    # An invalid explicitly supplied key makes any eager key load observable.
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "invalid-key-must-not-be-read")
    plan = create(store)
    data = payload(plan, "small")
    event = append(store, plan, data)
    assert event["data"] == data
    assert (
        CollaborationStore(store.base_dir).permission_payload(
            plan["id"], "workspace-a", key=event["key"]
        )
        == data
    )
    assert store.list("workspace-a")
    assert store.events(plan["id"], "workspace-a")


def test_same_identity_is_idempotent_and_changed_payload_conflicts(store):
    plan = create(store)
    data = payload(plan, "x" * 70000)
    first = append(store, plan, data)
    assert append(store, plan, deepcopy(data)) == first
    data["arguments"]["content"] += "different"
    with pytest.raises(PersonalAgentError) as exc:
        append(store, plan, data)
    assert exc.value.code == "idempotency_conflict"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM collaboration_events").fetchone()[0] == 1


def test_wrong_workspace_never_recovers_permission_arguments(store):
    plan = create(store)
    event = append(store, plan, payload(plan, "x" * 70000))
    with pytest.raises(PersonalAgentError) as exc:
        store.permission_payload(plan["id"], "workspace-b", key=event["key"])
    assert exc.value.code == "not_found"


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "changed_digest"])
def test_missing_or_corrupted_payload_fails_closed(store, mutation):
    plan = create(store)
    event = append(store, plan, payload(plan, "x" * 70000))
    with sqlite3.connect(store.db_path) as db:
        if mutation == "missing":
            db.execute(f"DELETE FROM {TABLE}")
        elif mutation == "corrupt":
            db.execute(f"UPDATE {TABLE} SET envelope_json='{{}}'")
        else:
            db.execute(f"UPDATE {TABLE} SET payload_sha256=?", ("0" * 64,))
    with pytest.raises(PersonalAgentError):
        store.permission_payload(plan["id"], "workspace-a", key=event["key"])


def test_cross_collaboration_envelope_swap_is_rejected(store):
    first = create(store, request="first")
    second = create(store, request="second")
    a = append(store, first, payload(first, "x" * 70000))
    append(store, second, payload(second, "y" * 70000))
    with sqlite3.connect(store.db_path) as db:
        replacement = db.execute(
            f"SELECT envelope_json FROM {TABLE} WHERE collaboration_id=?", (second["id"],)
        ).fetchone()[0]
        db.execute(
            f"UPDATE {TABLE} SET envelope_json=? WHERE collaboration_id=?",
            (replacement, first["id"]),
        )
    with pytest.raises(PersonalAgentError):
        store.permission_payload(first["id"], "workspace-a", key=a["key"])


def test_existing_256k_argument_limit_is_not_relaxed(store):
    plan = create(store)
    data = payload(plan, "x" * (256 * 1024))
    with pytest.raises(ValueError):
        permission_arguments_sha256(data["arguments"])
    with pytest.raises(PersonalAgentError):
        append(store, plan, data)
    assert store.events(plan["id"], "workspace-a") == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0


def test_preexisting_inline_permission_event_still_reads_without_payload_table_row(store):
    plan = create(store)
    data = payload(plan, "legacy inline")
    old = store.append_event(
        plan["id"],
        "workspace-a",
        key="permission:legacy",
        kind="permission_requested",
        step_id="write",
        data=data,
    )
    assert store.permission_payload(plan["id"], "workspace-a", key=old["key"]) == data


def test_v1_database_adds_payload_table_without_version_bump(store, monkeypatch):
    plan = create(store)
    old = store.append_event(
        plan["id"], "workspace-a", key="old-event", kind="handoff", data={"old": True}
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(f"DROP TABLE IF EXISTS {TABLE}")
        db.execute("PRAGMA user_version=1")
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "invalid-key-must-not-be-read")
    restarted = CollaborationStore(store.base_dir)
    assert restarted.events(plan["id"], "workspace-a") == [old]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()


def test_argument_limit_excludes_permission_metadata_overhead(store):
    plan = create(store)
    data = payload(plan, "")
    overhead = len(
        json.dumps(
            data["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    data["arguments"]["content"] = "x" * (256 * 1024 - overhead)
    data["reason"] = "r" * 16384
    permission_arguments_sha256(data["arguments"])
    event = append(store, plan, data)
    assert store.permission_payload(plan["id"], "workspace-a", key=event["key"]) == data


def test_total_payload_has_its_own_bounded_limit(store):
    plan = create(store)
    data = payload(plan, "x" * 70000)
    data["reason"] = "r" * (512 * 1024)
    with pytest.raises(PersonalAgentError):
        append(store, plan, data)
    assert store.events(plan["id"], "workspace-a") == []


def test_wrong_key_does_not_expose_or_replace_original_payload(store, monkeypatch):
    plan = create(store)
    data = payload(plan, "x" * 70000)
    event = append(store, plan, data)
    other_key = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", other_key)
    with pytest.raises(PersonalAgentError):
        store.permission_payload(plan["id"], "workspace-a", key=event["key"])
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", KEY)
    assert store.permission_payload(plan["id"], "workspace-a", key=event["key"]) == data


@pytest.mark.parametrize("field", ["step_id", "session_id", "call_id"])
def test_stored_execution_identity_is_authenticated(store, field):
    plan = create(store)
    event = append(store, plan, payload(plan, "x" * 70000))
    with sqlite3.connect(store.db_path) as db:
        db.execute(f"UPDATE {TABLE} SET {field}='other-identity'")
    with pytest.raises(PersonalAgentError):
        store.permission_payload(plan["id"], "workspace-a", key=event["key"])


def test_missing_key_leaves_no_partial_permission_event(store, monkeypatch):
    plan = create(store)
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "invalid-key")
    with pytest.raises(PersonalAgentError):
        append(store, plan, payload(plan, "x" * 70000))
    assert store.events(plan["id"], "workspace-a") == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0


def test_journal_failure_rolls_back_the_encrypted_payload(store):
    plan = create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER fail_permission_journal BEFORE INSERT ON collaboration_events
                      WHEN NEW.kind='permission_requested'
                      BEGIN SELECT RAISE(ABORT, 'controlled insert failure'); END""")
    with pytest.raises(PersonalAgentError):
        append(store, plan, payload(plan, "x" * 70000))
    assert store.events(plan["id"], "workspace-a") == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_permission_journal")
    assert append(store, plan, payload(plan, "x" * 70000))["sequence"] == 1


def test_cross_instance_concurrent_idempotency_keeps_one_payload(store):
    plan = create(store)
    data = payload(plan, "x" * 70000)

    def operation(_):
        return append(CollaborationStore(store.base_dir), plan, deepcopy(data))

    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(operation, range(8)))
    assert all(event == events[0] for event in events)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM collaboration_events").fetchone()[0] == 1
