"""Real SQLite, cold reopen, immutable receipts, CAS and durable lease proofs."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_store import (
    APPEND_ONLY,
    KINDS,
    NotificationStore,
    NotificationTransaction,
)
from naumi_agent.personal.owner_journal import canonical, digest

OWNER = "a" * 64
OTHER = "b" * 64
WORKSPACE = "c" * 64
EVENT = "ne_" + "1" * 32
BATCH = "nb_" + "2" * 32
DOCUMENT = {
    "id": EVENT,
    "workspace_key": WORKSPACE,
    "status": "available",
    "source_ref": {"opaque": "original"},
}

_WORKER = r"""
import json, pathlib, sys
from naumi_agent.personal.notification_store import NotificationStore
from naumi_agent.personal.models import PersonalAgentError
base, owner, ready_path, document_json = sys.argv[1:]
pathlib.Path(ready_path).write_text('READY', encoding='ascii')
assert sys.stdin.readline().strip() == 'GO'
store = NotificationStore(base, owner_key=owner)
try:
    result = store.mutate('one-source', 'ingest', {'source': 'original'},
        lambda tx: tx.put('event', json.loads(document_json), expected_revision=0))
    print(json.dumps({'status':'ok','instance':store.identity(),'result':result}), flush=True)
except PersonalAgentError as exc:
    print(json.dumps({'status':'error','code':exc.code}), flush=True)
"""


@pytest.fixture
def store(tmp_path):
    return NotificationStore(tmp_path / "notifications", owner_key=OWNER)


def create(store):
    return store.mutate(
        "one-source",
        "ingest",
        {"source": "original"},
        lambda tx: tx.put("event", copy.deepcopy(DOCUMENT), expected_revision=0),
    )


def assert_code(code, callback):
    with pytest.raises(PersonalAgentError) as caught:
        callback()
    assert caught.value.code == code


def counts(store):
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return tuple(
            db.execute(f"SELECT count(*) FROM notification_{table}").fetchone()[0]
            for table in ("records", "history", "requests", "lease_history")
        )


def test_store_is_distinct_owner_bound_with_hash_chained_fencing_and_no_execution_worker(store):
    assert KINDS == frozenset(
        {
            "preferences",
            "scan",
            "watch",
            "event",
            "outbox",
            "batch",
            "presentation",
            "notice_read",
            "read",
        }
    )
    assert APPEND_ONLY == frozenset({"event", "batch", "presentation", "notice_read", "read"})
    assert store.spec.namespace == "notification" and store.spec.parent_field == "workspace_key"
    assert store.spec.domain_hashes is store.spec.lease_proofs is True
    assert store.transaction_type is NotificationTransaction
    assert store.db_path.name == "personal_notifications.db"
    assert store.witness_path.name == ".personal-notifications-v1"
    assert store.lock_path.name == ".personal-notifications-bootstrap.lock"
    assert not hasattr(store, "start") and not hasattr(store, "tick")


def test_fresh_reads_do_not_initialize_files_or_virtual_preferences(store):
    assert not store.base_dir.exists()
    assert store.identity() is None
    assert store.read(lambda tx: [] if tx is None else tx.list("event")) == []
    assert not store.base_dir.exists()


def test_exact_event_and_request_survive_cold_reopen_without_aliasing_caller_memory(store):
    saved = create(store)
    instance = store.identity()
    assert len(instance) == 32 and store.witness_path.read_text("ascii") == instance
    saved["source_ref"]["opaque"] = "caller-edited"
    reopened = NotificationStore(store.base_dir, owner_key=OWNER, expected_instance_id=instance)
    original = reopened.read(lambda tx: tx.get("event", EVENT))
    assert original["source_ref"] == {"opaque": "original"}
    assert original["revision"] == 1
    assert create(reopened) == original
    assert counts(store) == (1, 1, 1, 0)


@pytest.mark.parametrize("kind", sorted(APPEND_ONLY))
def test_original_event_batch_presentation_notice_read_and_model_read_are_append_only(store, kind):
    document = {"id": EVENT, "workspace_key": WORKSPACE, "kind": kind}
    saved = store.write(lambda tx: tx.put(kind, document, expected_revision=0))
    assert_code(
        f"immutable_{kind}",
        lambda: store.write(lambda tx: tx.put(kind, document | {"new": True}, expected_revision=1)),
    )
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put(kind, document, expected_revision=0)),
    )
    assert store.read(lambda tx: tx.get(kind, EVENT)) == saved
    assert store.read(lambda tx: tx.get(kind, EVENT, revision=1)) == saved
    assert counts(store) == (1, 1, 0, 0)


@pytest.mark.parametrize("kind", sorted(KINDS - APPEND_ONLY))
def test_mutable_projection_kinds_use_cas_and_preserve_revision_history(store, kind):
    document = {"id": "opaque", "workspace_key": WORKSPACE, "status": "pending"}
    old = store.write(lambda tx: tx.put(kind, document, expected_revision=0))
    new = store.write(lambda tx: tx.put(kind, document | {"status": "done"}, expected_revision=1))
    assert new["revision"] == 2 and old["revision"] == 1
    assert store.read(lambda tx: tx.get(kind, "opaque", revision=1)) == old
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put(kind, document, expected_revision=1)),
    )
    assert counts(store) == (1, 2, 0, 0)


def test_batch_refs_outbox_state_and_request_identity_commit_as_one_fact_bundle(store):
    def operation(tx):
        event = tx.put("event", DOCUMENT, expected_revision=0)
        batch = tx.put(
            "batch",
            {
                "id": BATCH,
                "workspace_key": WORKSPACE,
                "member_refs": [{"event_id": event["id"], "event_digest": "f" * 64}],
            },
            expected_revision=0,
        )
        tx.put(
            "outbox",
            {"id": "opaque", "workspace_key": WORKSPACE, "batch_id": BATCH, "status": "sealed"},
            expected_revision=0,
        )
        return {"batch_id": batch["id"], "event_refs": batch["member_refs"]}

    receipt = store.mutate("batch-seal", "seal", {"events": [EVENT]}, operation)
    assert counts(store) == (3, 3, 1, 0)
    assert store.read(lambda tx: tx.request("batch-seal"))["result"] == receipt
    assert set(receipt) == {"batch_id", "event_refs"}
    assert len(store.read(lambda tx: tx.list("batch", parent=WORKSPACE))) == 1


def test_failed_bundle_or_stale_cas_rolls_back_all_intermediate_writes(store):
    create(store)

    def operation(tx):
        tx.put("batch", {"id": BATCH, "workspace_key": WORKSPACE}, expected_revision=0)
        tx.put("outbox", {"id": "opaque"}, expected_revision=1)

    assert_code("version_conflict", lambda: store.mutate("fail", "seal", {}, operation))
    assert store.read(lambda tx: tx.get("batch", BATCH, missing=True)) is None
    assert store.read(lambda tx: tx.request("fail")) is None
    assert counts(store) == (1, 1, 1, 0)


def test_replay_skips_callback_and_same_request_changed_envelope_fails_without_writes(store):
    original = create(store)
    invoked = []
    assert (
        store.mutate("one-source", "ingest", {"source": "original"}, lambda tx: invoked.append(tx))
        == original
    )
    for operation, payload in (
        ("other", {"source": "original"}),
        ("ingest", {"source": "changed"}),
    ):
        assert_code(
            "idempotency_conflict",
            lambda op=operation, p=payload: store.mutate(
                "one-source", op, p, lambda tx: invoked.append(tx)
            ),
        )
    assert not invoked and counts(store) == (1, 1, 1, 0)


def test_parent_status_live_keyset_pages_do_not_repeat_records(store):
    for number in range(6):
        store.write(
            lambda tx, n=number: tx.put(
                "event",
                {
                    "id": f"ne_{n:032x}",
                    "workspace_key": WORKSPACE,
                    "status": "available" if n % 2 == 0 else "other",
                },
                expected_revision=0,
            )
        )
    first = store.read(
        lambda tx: tx.page("event", parent=WORKSPACE, statuses=["available"], limit=2)
    )
    assert [entry["id"] for entry in first["records"]] == [f"ne_{n:032x}" for n in (0, 2)]
    second = store.read(
        lambda tx: tx.page(
            "event", parent=WORKSPACE, statuses=["available"], limit=2, after=first["next_after"]
        )
    )
    assert [entry["id"] for entry in second["records"]] == [f"ne_{4:032x}"]
    assert second["next_after"] is None


def test_concurrent_instances_idempotency_and_cas_admit_one_winner(store):
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(lambda _: create(NotificationStore(store.base_dir, owner_key=OWNER)), range(5))
        )
    assert results == [results[0]] * 5 and counts(store) == (1, 1, 1, 0)
    store.write(lambda tx: tx.put("outbox", {"id": "same"}, expected_revision=0))

    def change(number):
        try:
            return NotificationStore(store.base_dir, owner_key=OWNER).write(
                lambda tx: tx.put("outbox", {"id": "same", "winner": number}, expected_revision=1)
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(change, range(5)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count("version_conflict") == 4
    assert counts(store) == (2, 3, 1, 0)


def test_four_real_processes_share_first_initialization_and_exact_original_receipt(store, tmp_path):
    children = []
    try:
        for number in range(4):
            ready = tmp_path / f"ready-{number}"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _WORKER,
                    str(store.base_dir),
                    OWNER,
                    str(ready),
                    json.dumps(DOCUMENT),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            children.append((child, ready))
        deadline = time.monotonic() + 30
        while not all(ready.exists() for _, ready in children):
            assert all(child.poll() is None for child, _ in children)
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not store.db_path.exists()
        for child, _ in children:
            child.stdin.write("GO\n")
            child.stdin.flush()
        results = []
        for child, _ in children:
            stdout, stderr = child.communicate(timeout=40)
            assert child.returncode == 0 and not stderr
            results.append(json.loads(stdout))
        assert results == [results[0]] * 4
        assert results[0]["status"] == "ok" and results[0]["instance"] == store.identity()
        assert counts(store) == (1, 1, 1, 0)
    finally:
        for child, _ in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_owner_and_external_instance_pin_reject_replacement_without_repair(store):
    create(store)
    before = store.db_path.read_bytes(), store.witness_path.read_bytes()
    for candidate in (
        NotificationStore(store.base_dir, owner_key=OTHER),
        NotificationStore(store.base_dir, owner_key=OWNER, expected_instance_id="f" * 32),
    ):
        assert_code("storage_error", lambda c=candidate: c.read(lambda tx: tx.list("event")))
        assert_code("storage_error", lambda c=candidate: create(c))
    assert before == (store.db_path.read_bytes(), store.witness_path.read_bytes())


def test_deleted_db_and_local_witness_cannot_silently_reset_external_instance(store):
    create(store)
    instance = store.identity()
    store.db_path.unlink()
    store.witness_path.unlink()
    cold = NotificationStore(store.base_dir, owner_key=OWNER, expected_instance_id=instance)
    assert_code("storage_error", lambda: cold.read(lambda tx: tx.list("event")))
    assert_code("storage_error", lambda: create(cold))
    assert not store.db_path.exists() and not store.witness_path.exists()


@pytest.mark.parametrize("witness", [None, b"", b"short", b"f" * 32, b"f" * 33])
def test_missing_or_changed_local_witness_does_not_rebootstrap(store, witness):
    create(store)
    store.witness_path.unlink()
    if witness is not None:
        store.witness_path.write_bytes(witness)
    before = store.db_path.read_bytes()
    assert_code("storage_error", lambda: create(NotificationStore(store.base_dir, owner_key=OWNER)))
    assert store.db_path.read_bytes() == before
    assert (store.witness_path.read_bytes() if store.witness_path.exists() else None) == witness


@pytest.mark.parametrize("table", ["meta", "history", "requests", "lease_history"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_sqlite_fact_tables_are_immutable(store, table, operation):
    create(store)
    store.write(lambda tx: tx.claim("scan", "worker", "one", now=100))
    sql = (
        f"DELETE FROM notification_{table}"
        if operation == "DELETE"
        else f"UPDATE notification_{table} SET rowid=rowid"
    )
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)
    assert counts(store) == (1, 1, 1, 1)


def test_record_checksum_uses_notification_owner_domain_and_detects_payload_tampering(store):
    original = create(store)
    altered = original | {"source_ref": {"opaque": "forged"}}
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE notification_records SET payload=?,sha256=? WHERE kind='event' AND id=?",
            (canonical(altered), digest({"kind": "event", "value": altered}), EVENT),
        )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("event", EVENT)))


def test_lease_proofs_preserve_unknown_once_phase_across_cold_reopen_and_fence_old_worker(store):
    lease = store.write(
        lambda tx: tx.claim("prompt", BATCH, "client-a", now=100, ttl=10, once=True)
    )
    assert lease["fence"] == 1
    assert_code(
        "notification_busy",
        lambda: store.write(
            lambda tx: tx.claim("prompt", BATCH, "client-b", now=101, ttl=10, once=True)
        ),
    )
    store.write(lambda tx: tx.require_lease("prompt", BATCH, "client-a", 1, now=102, phase="sent"))
    store.write(
        lambda tx: tx.require_lease("prompt", BATCH, "client-a", 1, now=103, phase="unknown")
    )
    cold = NotificationStore(store.base_dir, owner_key=OWNER, expected_instance_id=store.identity())
    assert cold.read(lambda tx: tx.lease("prompt", BATCH))["phase"] == "unknown"
    assert_code(
        "notification_unknown",
        lambda: cold.write(
            lambda tx: tx.claim("prompt", BATCH, "client-b", now=120, ttl=10, once=True)
        ),
    )
    assert_code(
        "lease_lost",
        lambda: cold.write(
            lambda tx: tx.require_lease("prompt", BATCH, "client-a", 1, now=120, phase="sent")
        ),
    )
    assert counts(cold) == (0, 0, 0, 3)


def test_expired_worker_is_fenced_and_cannot_commit_under_new_holder(store):
    first = store.write(lambda tx: tx.claim("scan", "worker", "a", now=100, ttl=10))
    second = store.write(lambda tx: tx.claim("scan", "worker", "b", now=111, ttl=10))
    assert first["fence"] == 1 and second["fence"] == 2

    def old_commit(tx):
        tx.put("event", DOCUMENT, expected_revision=0)
        tx.require_lease("scan", "worker", "a", 1, now=112)

    assert_code("lease_lost", lambda: store.write(old_commit))
    assert store.read(lambda tx: tx.get("event", EVENT, missing=True)) is None
    store.write(lambda tx: tx.release("scan", "worker", "b", 2, now=113))
    assert store.read(lambda tx: tx.lease("scan", "worker"))["phase"] == "finished"
    assert counts(store) == (0, 0, 0, 3)


def test_modified_lease_projection_is_rejected_against_immutable_lease_chain(store):
    store.write(lambda tx: tx.claim("scan", "worker", "a", now=100, ttl=10))
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE notification_leases SET holder='forged'")
    assert_code("storage_error", lambda: store.read(lambda tx: tx.lease("scan", "worker")))


@pytest.mark.parametrize("revision", [True, -1, 1.0, "0", None])
def test_revision_types_are_never_coerced(store, revision):
    create(store)
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put("watch", {"id": "opaque"}, expected_revision=revision)
        ),
    )
    assert counts(store) == (1, 1, 1, 0)


def test_unknown_kinds_nonjson_oversized_records_and_private_hardlinks_are_rejected(
    store, tmp_path
):
    create(store)
    assert_code(
        "storage_error",
        lambda: store.write(lambda tx: tx.put("job", DOCUMENT, expected_revision=0)),
    )
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put("watch", {"id": "opaque", "x": float("nan")}, expected_revision=0)
        ),
    )
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put(
                "watch", {"id": "opaque", "x": "x" * (2 * 1024**2)}, expected_revision=0
            )
        ),
    )
    assert counts(store) == (1, 1, 1, 0)
    os.link(store.db_path, tmp_path / "aliased.db")
    assert_code("storage_error", lambda: store.read(lambda tx: tx.list("event")))
