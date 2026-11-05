"""Real SQLite planning CAS, immutable history, cold reopen and process initialization."""

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
from naumi_agent.personal.owner_journal import canonical, digest
from naumi_agent.personal.today_store import KINDS, TodayStore, TodayTransaction

OWNER = "a" * 64
OTHER = "b" * 64
DAY = "td_" + "1" * 32
METADATA = "tm_" + "2" * 32
CHANGE = "tc_" + "3" * 32
DOCUMENT = {"id": DAY, "workspace_key": "c" * 64, "status": "active", "placements": []}

_WORKER = r"""
import json, pathlib, sys
from naumi_agent.personal.today_store import TodayStore
from naumi_agent.personal.models import PersonalAgentError
base, owner, mode, ready_path, entered_path, document_json = sys.argv[1:]
ready, entered = pathlib.Path(ready_path), pathlib.Path(entered_path)
document = json.loads(document_json)

class ObservedStore(TodayStore):
    paused = False
    def _validate(self, db):
        result = super()._validate(db)
        if mode == 'pause-create' and not self._seen and not self.paused:
            self.paused = True
            entered.write_text('INITIALIZED_NOT_COMMITTED', encoding='ascii')
            assert sys.stdin.readline().strip() == 'GO'
        return result

store = ObservedStore(base, owner_key=owner)
ready.write_text('READY', encoding='ascii')
assert sys.stdin.readline().strip() == 'GO'
try:
    if mode == 'read':
        entered.write_text('READING', encoding='ascii')
        def read(tx):
            if tx is None:
                return {'records': [], 'instance': None}
            instance = tx.db.execute('SELECT instance_id FROM today_meta').fetchone()[0]
            return {'records': tx.list('day'), 'instance': instance}
        result = store.read(read)
    else:
        result = store.mutate('same-first-request', 'place', {'day_id': document['id']},
            lambda tx: tx.put('day', document, expected_revision=0))
    print(json.dumps({'status': 'ok', 'result': result}, ensure_ascii=True), flush=True)
except PersonalAgentError as exc:
    print(json.dumps({'status': 'error', 'code': exc.code}, ensure_ascii=True), flush=True)
"""


@pytest.fixture
def store(tmp_path):
    return TodayStore(tmp_path / "today", owner_key=OWNER)


@pytest.fixture
def processes():
    children = []
    try:
        yield children
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def create(store, request_id="same-first-request"):
    return store.mutate(
        request_id,
        "place",
        {"day_id": DAY},
        lambda tx: tx.put("day", copy.deepcopy(DOCUMENT), expected_revision=0),
    )


def assert_code(code, callback):
    with pytest.raises(PersonalAgentError) as caught:
        callback()
    assert caught.value.code == code


def counts(store):
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return tuple(
            db.execute(f"SELECT count(*) FROM today_{table}").fetchone()[0]
            for table in ("records", "history", "requests")
        )


def start(processes, tmp_path, store, *, owner=OWNER, mode="create"):
    index = len(processes)
    ready, entered = tmp_path / f"today-{index}.ready", tmp_path / f"today-{index}.entered"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WORKER,
            str(store.base_dir),
            owner,
            mode,
            str(ready),
            str(entered),
            json.dumps(DOCUMENT),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    processes.append(child)
    return child, ready, entered


def wait_marker(child, marker):
    deadline = time.monotonic() + 30
    while not marker.exists():
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"进程提前退出：{child.returncode}, {stdout}, {stderr}")
        if time.monotonic() > deadline:
            pytest.fail(f"进程未抵达同步点：{marker.name}")
        time.sleep(0.01)


def release(child):
    child.stdin.write("GO\n")
    child.stdin.flush()


def finish(child):
    stdout, stderr = child.communicate(timeout=40)
    assert child.returncode == 0 and not stderr, (child.returncode, stdout, stderr)
    return json.loads(stdout)


def test_today_store_is_a_distinct_owner_journal_without_an_executor_or_worker(store):
    assert KINDS == frozenset({"metadata", "day", "change", "read"})
    assert store.spec.namespace == "today" and store.spec.parent_field == "day_id"
    assert store.spec.domain_hashes is True and store.spec.lease_proofs is False
    assert store.transaction_type is TodayTransaction
    assert store.db_path.name == "personal_today.db"
    assert store.witness_path.name == ".personal-today-v1"
    assert not hasattr(store, "start") and not hasattr(store, "tick")


def test_fresh_read_has_no_bootstrap_or_filesystem_side_effect(store):
    assert not store.base_dir.exists()
    assert store.identity() is None
    assert store.read(lambda tx: "empty" if tx is None else "unexpected") == "empty"
    assert not store.base_dir.exists() and store._seen is False


def test_sqlite_roundtrip_cas_history_and_original_request_are_distinct(store):
    original = create(store)
    instance = store.identity()
    assert len(instance) == 32 and store.witness_path.read_text("ascii") == instance
    assert counts(store) == (1, 1, 1)
    with store.connection() as tx:
        assert isinstance(tx, TodayTransaction)
        assert tx.get("day", DAY) == original
    changed = store.write(
        lambda tx: tx.put(
            "day", {**original, "placements": [{"id": "opaque"}]}, expected_revision=1
        )
    )
    assert changed["revision"] == 2
    assert store.read(lambda tx: tx.get("day", DAY, revision=1)) == original
    assert store.read(lambda tx: tx.get("day", DAY)) == changed
    assert create(store) == original
    assert counts(store) == (1, 2, 1)
    changed["placements"].append("caller mutation")
    assert store.read(lambda tx: tx.get("day", DAY))["placements"] == [{"id": "opaque"}]
    reopened = TodayStore(store.base_dir, owner_key=OWNER, expected_instance_id=instance)
    assert reopened.identity() == instance
    assert reopened.read(lambda tx: tx.get("day", DAY))["revision"] == 2


def test_metadata_day_change_and_identity_only_receipt_commit_atomically(store):
    def write(tx):
        meta = tx.put("metadata", {"id": METADATA, "priority": "normal"}, expected_revision=0)
        day = tx.put("day", DOCUMENT, expected_revision=0)
        tx.put(
            "change",
            {"id": CHANGE, "day_id": DAY, "operation": "place", "metadata_id": METADATA},
            expected_revision=0,
        )
        return {
            "day_id": DAY,
            "day_revision": day["revision"],
            "metadata_ids": [meta["id"]],
            "change_id": CHANGE,
        }

    identity = store.mutate("one-bundle", "place", {"source": "opaque"}, write)
    assert counts(store) == (3, 3, 1)
    assert set(identity) == {"day_id", "day_revision", "metadata_ids", "change_id"}
    assert store.read(lambda tx: tx.request("one-bundle"))["result"] == identity
    assert store.read(lambda tx: tx.list("change", parent=DAY))[0]["id"] == CHANGE


def test_partial_metadata_day_change_failure_rolls_back_every_business_fact(store):
    original = create(store)

    def fail(tx):
        tx.put("metadata", {"id": METADATA, "priority": "high"}, expected_revision=0)
        tx.put("day", {**original, "placements": [METADATA]}, expected_revision=1)
        tx.put("change", {"id": CHANGE, "day_id": DAY}, expected_revision=0)
        raise PersonalAgentError("intentional", "测试中止")

    assert_code("intentional", lambda: store.mutate("bundle-failure", "place", {}, fail))
    assert counts(store) == (1, 1, 1)
    assert store.read(lambda tx: tx.get("day", DAY)) == original
    assert store.read(lambda tx: tx.get("metadata", METADATA, missing=True)) is None
    assert store.read(lambda tx: tx.request("bundle-failure")) is None


def test_stale_cas_rolls_back_metadata_written_before_day_check(store):
    create(store)

    def conflict(tx):
        tx.put("metadata", {"id": METADATA}, expected_revision=0)
        tx.put("day", DOCUMENT, expected_revision=0)

    assert_code("version_conflict", lambda: store.mutate("cas-failure", "place", {}, conflict))
    assert counts(store) == (1, 1, 1)
    assert store.read(lambda tx: tx.request("cas-failure")) is None


def test_replay_skips_callback_and_conflicting_payload_or_operation_never_writes(store):
    original = create(store)
    invoked = []
    replay = store.mutate(
        "same-first-request", "place", {"day_id": DAY}, lambda tx: invoked.append(tx)
    )
    assert replay == original and not invoked
    for operation, payload in (("remove", {"day_id": DAY}), ("place", {"day_id": "changed"})):
        assert_code(
            "idempotency_conflict",
            lambda op=operation, p=payload: store.mutate(
                "same-first-request", op, p, lambda tx: invoked.append(tx)
            ),
        )
    assert not invoked and counts(store) == (1, 1, 1)


def test_change_facts_are_append_only_at_the_transaction_adapter(store):
    original = store.write(lambda tx: tx.put("change", {"id": CHANGE}, expected_revision=0))
    assert_code(
        "immutable_change",
        lambda: store.write(
            lambda tx: tx.put("change", {"id": CHANGE, "x": 1}, expected_revision=1)
        ),
    )
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put("change", {"id": CHANGE}, expected_revision=0)),
    )
    assert store.read(lambda tx: tx.get("change", CHANGE)) == original
    assert counts(store) == (1, 1, 0)


def test_read_snapshots_are_append_only_and_hold_exact_refs_without_artifact_evidence_truncation(
    store,
):
    snapshot = {
        "id": "read:" + "4" * 64,
        "workspace_key": "c" * 64,
        "body_digest": "d" * 64,
        "item_refs": [
            {
                "kind": "item",
                "workspace_key": "c" * 64,
                "item_id": f"pi_{number:032x}",
                "item_revision": 1,
                "once_key": f"{number:032x}",
                "definition_sha256": "e" * 64,
                "assignee_id": "main",
                "assignee_version": 0,
            }
            for number in range(500)
        ],
    }
    assert len(canonical(snapshot).encode("utf-8")) > 64 * 1024
    saved = store.write(lambda tx: tx.put("read", snapshot, expected_revision=0))
    assert saved["revision"] == 1 and len(saved["item_refs"]) == 500
    assert_code(
        "immutable_read",
        lambda: store.write(
            lambda tx: tx.put("read", {**saved, "body_digest": "f" * 64}, expected_revision=1)
        ),
    )
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put("read", snapshot, expected_revision=0)),
    )
    reopened = TodayStore(store.base_dir, owner_key=OWNER, expected_instance_id=store.identity())
    assert reopened.read(lambda tx: tx.get("read", snapshot["id"], revision=1)) == saved
    assert counts(store) == (1, 1, 0)


def test_parent_status_keyset_pages_do_not_skip_or_repeat_equal_status_records(store):
    for number in range(6):
        store.write(
            lambda tx, n=number: tx.put(
                "change",
                {"id": f"tc_{n:032x}", "day_id": DAY, "status": "kept" if n % 2 == 0 else "other"},
                expected_revision=0,
            )
        )
    first = store.read(lambda tx: tx.page("change", parent=DAY, statuses=["kept"], limit=2))
    assert [r["id"] for r in first["records"]] == [f"tc_{n:032x}" for n in (0, 2)]
    assert first["next_after"] == f"tc_{2:032x}"
    last = store.read(
        lambda tx: tx.page(
            "change", parent=DAY, statuses=["kept"], limit=2, after=first["next_after"]
        )
    )
    assert [r["id"] for r in last["records"]] == [f"tc_{4:032x}"]
    assert last["next_after"] is None


def test_independent_store_instances_share_one_request_and_cas_one_winner(store):
    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(
            pool.map(lambda _: create(TodayStore(store.base_dir, owner_key=OWNER)), range(6))
        )
    assert created == [created[0]] * 6 and counts(store) == (1, 1, 1)

    def update(number):
        try:
            return TodayStore(store.base_dir, owner_key=OWNER).write(
                lambda tx: tx.put("day", {**DOCUMENT, "winner": number}, expected_revision=1)
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(update, range(6)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 5
    assert counts(store) == (1, 2, 1)


def test_first_initialization_in_four_real_os_processes_has_one_instance(
    processes, tmp_path, store
):
    children = [start(processes, tmp_path, store) for _ in range(4)]
    for child, ready, _ in children:
        wait_marker(child, ready)
    assert not store.db_path.exists()
    for child, _, _ in children:
        release(child)
    results = [finish(child) for child, _, _ in children]
    assert results == [results[0]] * 4 and results[0]["status"] == "ok"
    assert counts(store) == (1, 1, 1)
    assert store.lock_path.read_bytes() == b"\0"
    assert store.identity() == store.witness_path.read_text("ascii")


def test_competing_owner_processes_do_not_admit_two_owners(processes, tmp_path, store):
    children = [
        start(processes, tmp_path, store, owner=owner) for owner in (OWNER, OTHER, OWNER, OTHER)
    ]
    for child, ready, _ in children:
        wait_marker(child, ready)
    for child, _, _ in children:
        release(child)
    results = [finish(child) for child, _, _ in children]
    assert sum(r["status"] == "ok" for r in results) == 2
    assert results.count({"status": "error", "code": "storage_error"}) == 2
    assert counts(store) == (1, 1, 1)


def test_reader_waits_for_real_schema_and_witness_commit(processes, tmp_path, store):
    writer, ready, entered = start(processes, tmp_path, store, mode="pause-create")
    wait_marker(writer, ready)
    release(writer)
    wait_marker(writer, entered)
    reader, read_ready, reading = start(processes, tmp_path, store, mode="read")
    wait_marker(reader, read_ready)
    release(reader)
    wait_marker(reader, reading)
    assert reader.poll() is None
    release(writer)
    written, read = finish(writer), finish(reader)
    assert written["status"] == read["status"] == "ok"
    assert read["result"]["instance"] == store.identity()
    assert read["result"]["records"] in ([], [written["result"]])


def test_owner_and_expected_instance_mismatch_never_repair_original(store):
    create(store)
    before = store.db_path.read_bytes(), store.witness_path.read_bytes()
    wrong_owner = TodayStore(store.base_dir, owner_key=OTHER)
    wrong_pin = TodayStore(store.base_dir, owner_key=OWNER, expected_instance_id="f" * 32)
    for candidate in (wrong_owner, wrong_pin):
        assert_code("storage_error", lambda c=candidate: c.read(lambda tx: tx.list("day")))
        assert_code("storage_error", lambda c=candidate: create(c))
    assert (store.db_path.read_bytes(), store.witness_path.read_bytes()) == before


def test_external_pin_rejects_missing_database_and_witness_after_cold_reopen(store):
    create(store)
    instance = store.identity()
    store.db_path.unlink()
    store.witness_path.unlink()
    cold = TodayStore(store.base_dir, owner_key=OWNER, expected_instance_id=instance)
    assert_code("storage_error", lambda: cold.read(lambda tx: tx.list("day")))
    assert_code("storage_error", lambda: create(cold))
    assert not store.db_path.exists() and not store.witness_path.exists()


@pytest.mark.parametrize("witness", [None, b"", b"short", b"f" * 32, b"f" * 33])
def test_missing_or_replaced_local_witness_is_not_auto_recreated(store, witness):
    create(store)
    store.witness_path.unlink()
    if witness is not None:
        store.witness_path.write_bytes(witness)
    before = store.db_path.read_bytes()
    assert_code("storage_error", lambda: create(TodayStore(store.base_dir, owner_key=OWNER)))
    assert store.db_path.read_bytes() == before
    assert (store.witness_path.read_bytes() if store.witness_path.exists() else None) == witness


@pytest.mark.parametrize("contents", [b"", b"not a sqlite database"])
def test_preexisting_unknown_database_is_not_bootstrapped_over(store, contents):
    store.base_dir.mkdir()
    store.db_path.write_bytes(contents)
    assert_code("storage_error", lambda: create(store))
    assert store.db_path.read_bytes() == contents and not store.witness_path.exists()


@pytest.mark.parametrize("table", ["today_meta", "today_history", "today_requests"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_sqlite_rejects_changes_to_original_fact_tables(store, table, operation):
    create(store)
    sql = f"DELETE FROM {table}" if operation == "DELETE" else f"UPDATE {table} SET rowid=rowid"
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)
    assert counts(store) == (1, 1, 1)


def test_modified_record_payload_or_nondomain_hash_is_detected(store):
    original = create(store)
    altered = {**original, "placements": ["forged"]}
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE today_records SET payload=?,sha256=? WHERE kind='day' AND id=?",
            (canonical(altered), digest({"kind": "day", "value": altered}), DAY),
        )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("day", DAY)))


def test_missing_historical_revision_is_not_treated_as_fresh_record(store):
    create(store)
    with sqlite3.connect(store.db_path) as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='today_history_immutable_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER today_history_immutable_delete")
        db.execute("DELETE FROM today_history")
        db.execute(sql)
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("day", DAY)))


def test_private_database_hardlink_is_not_an_alternative_access_path(store, tmp_path):
    create(store)
    os.link(store.db_path, tmp_path / "database-alias.db")
    assert_code("storage_error", lambda: store.read(lambda tx: tx.list("day")))


@pytest.mark.parametrize("revision", [True, -1, 1.0, "0"])
def test_revision_values_do_not_coerce_boolean_float_or_strings(store, revision):
    create(store)
    assert_code(
        "invalid_input",
        lambda: store.write(lambda tx: tx.put("day", DOCUMENT, expected_revision=revision)),
    )
    assert counts(store) == (1, 1, 1)


def test_unknown_kinds_nonjson_and_oversized_records_leave_no_new_facts(store):
    create(store)
    assert_code(
        "storage_error",
        lambda: store.write(lambda tx: tx.put("routine", DOCUMENT, expected_revision=0)),
    )
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put("metadata", {"id": METADATA, "x": float("nan")}, expected_revision=0)
        ),
    )
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put(
                "metadata", {"id": METADATA, "x": "x" * (2 * 1024**2)}, expected_revision=0
            )
        ),
    )
    assert counts(store) == (1, 1, 1)
