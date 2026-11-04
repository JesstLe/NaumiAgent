"""SQLite identity, history, fencing and real-process connection-store contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy

import pytest

from naumi_agent.personal.connection_models import canonical, digest
from naumi_agent.personal.connection_store import ConnectionStore
from naumi_agent.personal.models import PersonalAgentError

OWNER = "a" * 64
OTHER = "b" * 64
CONNECTION = "pc_" + "1" * 32
DOCUMENT = {"id": CONNECTION, "status": "connected", "auth_epoch": 1, "title": "本地日程"}
PAYLOAD = {"id": CONNECTION, "title": "本地日程"}

_WORKER = r"""
import json, pathlib, sys
from naumi_agent.personal.connection_store import ConnectionStore
from naumi_agent.personal.models import PersonalAgentError
base, owner, mode, ready, entered, document_json = sys.argv[1:]
ready, entered = pathlib.Path(ready), pathlib.Path(entered)
document = json.loads(document_json)

class ObservedStore(ConnectionStore):
    def _validate(self, db):
        result = super()._validate(db)
        if mode == 'wait':
            def trace(sql):
                if sql.strip().upper() == 'BEGIN IMMEDIATE':
                    entered.write_text('WAITING_SQLITE', encoding='ascii')
            db.set_trace_callback(trace)
        if mode == 'pause-create' and not self._seen:
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
            instance = tx.db.execute('SELECT instance_id FROM connection_meta').fetchone()[0]
            return {'records': tx.list('connection'), 'instance': instance}
        result = store.read(read)
    elif mode == 'wait':
        result = store.write(lambda tx: tx.put('connection', document, expected_revision=2))
    elif mode == 'claim':
        result = store.write(lambda tx: tx.claim(
            'refresh', 'same-token', document['holder'], now=100, ttl=60, once=True))
    else:
        result = store.mutate(
            'same-first-request', 'create',
            {'id': document['id'], 'title': document['title']},
            lambda tx: tx.put('connection', document, expected_revision=0))
    print(json.dumps({'status':'ok','result':result}, ensure_ascii=True), flush=True)
except PersonalAgentError as exc:
    print(json.dumps({'status':'error','code':exc.code}, ensure_ascii=True), flush=True)
"""


@pytest.fixture
def store(tmp_path):
    return ConnectionStore(tmp_path / "connections", owner_key=OWNER)


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


def create(store, *, request_id="same-first-request", document=None):
    value = deepcopy(DOCUMENT if document is None else document)
    return store.mutate(
        request_id,
        "create",
        {"id": value["id"], "title": value["title"]},
        lambda tx: tx.put("connection", value, expected_revision=0),
    )


def assert_code(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    assert caught.value.code == code, (caught.value.code, str(caught.value))


def state(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def start(processes, tmp_path, store, *, mode="create", owner=OWNER, document=None):
    index = len(processes)
    ready, entered = tmp_path / f"store-{index}.ready", tmp_path / f"store-{index}.entered"
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
            json.dumps(DOCUMENT if document is None else document, ensure_ascii=True),
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


def wait_marker(child, marker, *, timeout=25):
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"真实进程提前退出：{child.returncode}, {stdout}, {stderr}")
        if time.monotonic() > deadline:
            pytest.fail(f"真实进程未抵达同步点：{marker.name}")
        time.sleep(0.01)


def release(child):
    child.stdin.write("GO\n")
    child.stdin.flush()


def finish(child):
    stdout, stderr = child.communicate(timeout=30)
    assert child.returncode == 0, (child.returncode, stdout, stderr)
    assert not stderr, stderr
    return json.loads(stdout)


def authority(store, *, records=1, history=1, requests=1):
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        owner, instance, version = db.execute(
            "SELECT owner_key,instance_id,schema_version FROM connection_meta"
        ).fetchone()
        assert owner == OWNER and version == 1 and len(instance) == 32
        assert store.witness_path.read_text("ascii") == instance
        assert db.execute("SELECT count(*) FROM connection_records").fetchone() == (records,)
        assert db.execute("SELECT count(*) FROM connection_history").fetchone() == (history,)
        assert db.execute("SELECT count(*) FROM connection_requests").fetchone() == (requests,)
    return instance


def corrupt_immutable(store, table, operation, sql, params=()):
    trigger = f"{table}_immutable_{operation.lower()}"
    with sqlite3.connect(store.db_path) as db:
        original = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger,)).fetchone()[
            0
        ]
        db.execute(f"DROP TRIGGER {trigger}")
        db.execute(sql, params)
        db.execute(original)


def test_fresh_read_has_no_directory_database_witness_or_lock_side_effect(store):
    assert store._seen is False and not store.base_dir.exists()
    assert store.read(lambda tx: "empty" if tx is None else "unexpected") == "empty"
    assert store._seen is False and not store.base_dir.exists()


@pytest.mark.parametrize("existing_lock", [False, True])
def test_empty_read_callback_releases_instance_and_bootstrap_locks(store, existing_lock):
    if existing_lock:
        store.base_dir.mkdir()
        store.lock_path.write_bytes(b"\0")
    blocked = False
    started = threading.Event()

    def writer():
        started.set()
        return create(store)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connection() as tx:
            assert tx is None
            future = pool.submit(writer)
            assert started.wait(timeout=5)
            try:
                result = future.result(timeout=5)
            except TimeoutError:
                blocked = True
        if blocked:
            future.result(timeout=10)
    assert not blocked, "空库 read callback 持有了写入所需的初始化锁"
    assert result["revision"] == 1


def test_real_sqlite_roundtrip_history_and_detached_values(store):
    first = create(store)
    instance = authority(store)
    original = deepcopy(first)
    first["title"] = "caller mutation"
    assert store.read(lambda tx: tx.get("connection", CONNECTION)) == original
    second = store.write(
        lambda tx: tx.put(
            "connection",
            {**original, "status": "paused"},
            expected_revision=1,
        )
    )
    assert second["revision"] == 2
    assert store.read(lambda tx: tx.get("connection", CONNECTION, revision=1)) == original
    assert create(store) == original
    assert store.read(lambda tx: tx.get("connection", CONNECTION)) == second
    assert authority(store, history=2) == instance
    reopened = ConnectionStore(store.base_dir, owner_key=OWNER)
    assert reopened.read(lambda tx: tx.get("connection", CONNECTION)) == second


def test_read_filters_parent_status_limit_and_missing_are_exact(store):
    create(store)
    for index, status in enumerate(["synced", "cancelled", "synced"]):
        store.write(
            lambda tx, i=index, s=status: tx.put(
                "event",
                {
                    "id": f"event-{i}",
                    "connection_id": CONNECTION,
                    "status": s,
                },
                expected_revision=0,
            )
        )
    result = store.read(
        lambda tx: tx.list("event", parent=CONNECTION, statuses=["synced"], limit=1)
    )
    assert [entry["id"] for entry in result] == ["event-0"]
    assert store.read(lambda tx: tx.get("event", "absent", missing=True)) is None
    assert_code("not_found", lambda: store.read(lambda tx: tx.get("event", "absent")))


def test_owner_mismatch_and_witness_mismatch_do_not_modify_original(store):
    create(store)
    before = state(store.db_path), state(store.witness_path)
    other = ConnectionStore(store.base_dir, owner_key=OTHER)
    assert_code("storage_error", lambda: other.read(lambda tx: tx.list("connection")))
    assert_code("storage_error", lambda: create(other))
    assert (state(store.db_path), state(store.witness_path)) == before


def test_same_request_payload_replay_is_exact_and_conflicting_body_never_runs_callback(store):
    first = create(store)
    assert create(store) == first
    invoked = []
    assert_code(
        "idempotency_conflict",
        lambda: store.mutate(
            "same-first-request",
            "create",
            {**PAYLOAD, "title": "changed"},
            lambda tx: invoked.append(tx),
        ),
    )
    assert invoked == []
    assert authority(store)


def test_write_callback_failure_rolls_back_history_head_and_request(store):
    create(store)

    def fail(tx):
        tx.put("connection", {**DOCUMENT, "status": "paused"}, expected_revision=1)
        raise PersonalAgentError("intentional", "测试中止")

    assert_code("intentional", lambda: store.mutate("failed", "pause", {"x": 1}, fail))
    assert store.read(lambda tx: tx.get("connection", CONNECTION))["revision"] == 1
    assert store.read(lambda tx: tx.request("failed")) is None
    authority(store)


def test_independent_instances_concurrently_replay_one_request_and_cas_one_winner(store):
    barrier = threading.Barrier(8)

    def first(_index):
        independent = ConnectionStore(store.base_dir, owner_key=OWNER)
        barrier.wait()
        return create(independent)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(first, range(8)))
    assert results == [results[0]] * 8

    def update(index):
        try:
            return ConnectionStore(store.base_dir, owner_key=OWNER).write(
                lambda tx: tx.put(
                    "connection",
                    {**DOCUMENT, "title": f"winner-{index}"},
                    expected_revision=1,
                )
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        updates = list(pool.map(update, range(8)))
    assert sum(isinstance(result, dict) for result in updates) == 1
    assert updates.count("version_conflict") == 7
    authority(store, history=2)


def test_first_creation_in_eight_actual_os_processes_has_one_authority(processes, tmp_path, store):
    children = [start(processes, tmp_path, store) for _ in range(8)]
    for child, ready, _entered in children:
        wait_marker(child, ready)
    assert not store.db_path.exists()
    for child, _ready, _entered in children:
        release(child)
    outcomes = [finish(child) for child, _ready, _entered in children]
    assert outcomes == [outcomes[0]] * 8
    assert outcomes[0]["status"] == "ok"
    assert store.lock_path.read_bytes() == b"\0"
    authority(store)


def test_competing_owner_processes_choose_exactly_one_owner(processes, tmp_path, store):
    children = [start(processes, tmp_path, store, owner=owner) for owner in [OWNER, OTHER] * 3]
    for child, ready, _entered in children:
        wait_marker(child, ready)
    for child, _ready, _entered in children:
        release(child)
    outcomes = [finish(child) for child, _ready, _entered in children]
    assert sum(item["status"] == "ok" for item in outcomes) == 3
    assert sum(item == {"status": "error", "code": "storage_error"} for item in outcomes) == 3
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM connection_records").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM connection_requests").fetchone() == (1,)


def test_reader_waits_for_witness_schema_commit_instead_of_partial_initialization(
    processes,
    tmp_path,
    store,
):
    writer, ready, entered = start(processes, tmp_path, store, mode="pause-create")
    wait_marker(writer, ready)
    release(writer)
    wait_marker(writer, entered)
    assert store.db_path.exists() and store.witness_path.exists()
    reader, reader_ready, reading = start(processes, tmp_path, store, mode="read")
    wait_marker(reader, reader_ready)
    release(reader)
    wait_marker(reader, reading)
    assert reader.poll() is None
    release(writer)
    written, read = finish(writer), finish(reader)
    assert written["status"] == read["status"] == "ok"
    assert read["result"]["instance"] == store.witness_path.read_text("ascii")
    assert read["result"]["records"] in ([], [written["result"]])
    assert store.read(lambda tx: tx.list("connection")) == [written["result"]]


def test_reader_retries_cross_sample_first_bootstrap_error(store, monkeypatch):
    actual = type(store.db_path).is_file
    fired, committed = False, {}

    def observed(path):
        nonlocal fired
        sample = actual(path)
        if path == store.db_path and not sample and not fired:
            fired = True
            committed["value"] = create(ConnectionStore(store.base_dir, owner_key=OWNER))
        return sample

    monkeypatch.setattr(type(store.db_path), "is_file", observed)
    result = store.read(lambda tx: [] if tx is None else tx.list("connection"))
    assert fired and result == [committed["value"]]


def test_waiting_business_writer_does_not_block_third_os_process_reader(processes, tmp_path, store):
    original = create(store)
    with store.connection(write=True) as tx:
        tx.put("connection", {**DOCUMENT, "status": "paused"}, expected_revision=1)
        waiter, ready, waiting = start(
            processes,
            tmp_path,
            store,
            mode="wait",
            document={**DOCUMENT, "status": "resumed"},
        )
        wait_marker(waiter, ready)
        release(waiter)
        wait_marker(waiter, waiting)
        assert waiter.poll() is None
        reader, read_ready, reading = start(processes, tmp_path, store, mode="read")
        wait_marker(reader, read_ready)
        release(reader)
        wait_marker(reader, reading)
        result = finish(reader)
        assert result["status"] == "ok" and result["result"]["records"] == [original]
        assert waiter.poll() is None
    result = finish(waiter)
    assert result["status"] == "ok" and result["result"]["revision"] == 3
    authority(store, history=3)


@pytest.mark.parametrize(
    "contents", ["empty-file", "empty-sqlite", "corrupt", "other-schema", "newer"]
)
def test_preexisting_unknown_database_is_not_reinitialized(store, contents):
    store.base_dir.mkdir()
    if contents == "empty-file":
        store.db_path.touch()
    elif contents == "corrupt":
        store.db_path.write_bytes(b"ORIGINAL_CORRUPT_DATABASE")
    else:
        with sqlite3.connect(store.db_path) as db:
            if contents in {"other-schema", "newer"}:
                db.execute("CREATE TABLE unrelated (id INTEGER)")
                db.execute("PRAGMA user_version=" + ("99" if contents == "newer" else "1"))
    before = state(store.db_path)
    assert_code("storage_error", lambda: create(store))
    assert state(store.db_path) == before and store._seen is False
    assert not store.witness_path.exists()


def test_lost_database_is_not_recreated_by_original_or_new_instance(store, tmp_path):
    create(store)
    preserved = tmp_path / "original.db"
    store.db_path.rename(preserved)
    before, witness = state(preserved), state(store.witness_path)
    for instance in [store, ConnectionStore(store.base_dir, owner_key=OWNER)]:
        assert_code("storage_error", lambda: instance.read(lambda tx: tx.list("connection")))
        assert_code("storage_error", lambda: create(instance))
        assert not store.db_path.exists()
    assert state(preserved) == before and state(store.witness_path) == witness


@pytest.mark.parametrize("value", [None, b"", b"short", b"f" * 32, b"f" * 33])
def test_missing_or_corrupt_witness_is_not_repaired(store, value):
    create(store)
    store.witness_path.unlink()
    if value is not None:
        store.witness_path.write_bytes(value)
    before = state(store.db_path)
    witness = None if value is None else state(store.witness_path)
    for instance in [store, ConnectionStore(store.base_dir, owner_key=OWNER)]:
        assert_code("storage_error", lambda: instance.read(lambda tx: tx.list("connection")))
        assert_code("storage_error", lambda: create(instance))
        assert instance._seen is (instance is store)
    assert state(store.db_path) == before
    assert (None if value is None else state(store.witness_path)) == witness
    assert store.witness_path.exists() is (value is not None)


@pytest.mark.parametrize("table", ["connection_meta", "connection_history", "connection_requests"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_fact_tables_are_sqlite_immutable(store, table, operation):
    create(store)
    with sqlite3.connect(store.db_path) as db:
        sql = f"DELETE FROM {table}" if operation == "DELETE" else f"UPDATE {table} SET rowid=rowid"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)
    authority(store)


def test_missing_or_replaced_immutable_trigger_fails_schema_validation(store):
    create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER connection_history_immutable_delete")
        db.execute(
            "CREATE TRIGGER connection_history_immutable_delete BEFORE DELETE "
            "ON connection_history BEGIN SELECT 1; END"
        )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.list("connection")))


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", "{}"),
        ("sha256", "0" * 64),
        ("revision", 99),
        ("parent_id", "forged"),
        ("status", "forged"),
    ],
)
def test_head_hash_revision_and_index_columns_are_checked(store, field, value):
    create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute(f"UPDATE connection_records SET {field}=?", (value,))
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("connection", CONNECTION)))


@pytest.mark.parametrize("missing_revision", [1, 2])
def test_head_requires_contiguous_history_and_exact_latest_snapshot(store, missing_revision):
    create(store)
    store.write(
        lambda tx: tx.put("connection", {**DOCUMENT, "status": "paused"}, expected_revision=1)
    )
    corrupt_immutable(
        store,
        "connection_history",
        "DELETE",
        "DELETE FROM connection_history WHERE revision=?",
        (missing_revision,),
    )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("connection", CONNECTION)))


def test_noninteger_old_revision_cannot_fake_history_count_min_max(store):
    create(store)
    store.write(
        lambda tx: tx.put("connection", {**DOCUMENT, "title": "second"}, expected_revision=1)
    )
    store.write(
        lambda tx: tx.put("connection", {**DOCUMENT, "title": "third"}, expected_revision=2)
    )
    corrupt_immutable(
        store,
        "connection_history",
        "UPDATE",
        "UPDATE connection_history SET revision=1.5 WHERE revision=2",
    )
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT count(*),min(revision),max(revision) FROM connection_history"
        ).fetchone() == (3, 1, 3)
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("connection", CONNECTION)))
    assert_code(
        "storage_error",
        lambda: store.read(
            lambda tx: tx.get(
                "connection",
                CONNECTION,
                revision=2,
            )
        ),
    )


def test_history_revision_boolean_is_not_interpreted_as_revision_one(store):
    create(store)
    assert_code(
        "invalid_input",
        lambda: store.read(
            lambda tx: tx.get(
                "connection",
                CONNECTION,
                revision=True,
            )
        ),
    )


def test_historical_payload_hash_is_checked_on_explicit_revision_read(store):
    create(store)
    store.write(
        lambda tx: tx.put("connection", {**DOCUMENT, "status": "paused"}, expected_revision=1)
    )
    corrupt_immutable(
        store,
        "connection_history",
        "UPDATE",
        "UPDATE connection_history SET payload='{}' WHERE revision=1",
    )
    assert_code(
        "storage_error", lambda: store.read(lambda tx: tx.get("connection", CONNECTION, revision=1))
    )


@pytest.mark.parametrize("entrypoint", ["get", "get-missing", "list"])
def test_orphan_history_after_head_deletion_is_not_reported_as_absence(store, entrypoint):
    create(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM connection_records")

    def operation(tx):
        if entrypoint == "list":
            return tx.list("connection", statuses=["not-present"])
        return tx.get("connection", CONNECTION, missing=entrypoint == "get-missing")

    assert_code("storage_error", lambda: store.read(operation))


def test_valid_head_checksum_still_requires_exact_immutable_latest_snapshot(store):
    original = create(store)
    forged = {**original, "title": "forged-but-valid-hash"}
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE connection_records SET payload=?,sha256=?",
            (
                canonical(forged),
                digest({"kind": "connection", "value": forged}),
            ),
        )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("connection", CONNECTION)))


def test_request_hash_binds_original_request_identity(store):
    create(store)
    corrupt_immutable(
        store,
        "connection_requests",
        "UPDATE",
        "UPDATE connection_requests SET request_id='forged-request'",
    )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.request("forged-request")))


def test_record_hash_binds_kind_even_if_matching_history_is_moved(store):
    create(store)
    corrupt_immutable(
        store, "connection_history", "UPDATE", "UPDATE connection_history SET kind='action'"
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE connection_records SET kind='action'")
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get("action", CONNECTION)))


@pytest.mark.parametrize("expected", [True, -1, 1.5, "1", None])
def test_expected_revision_rejects_nonintegers_and_negative_values(store, expected):
    create(store)
    assert_code(
        "invalid_input",
        lambda: store.write(
            lambda tx: tx.put(
                "connection",
                DOCUMENT,
                expected_revision=expected,
            )
        ),
    )
    authority(store)


def test_persistent_lease_once_sent_remains_unknown_after_restart_and_expiry(store):
    claimed = store.write(
        lambda tx: tx.claim("refresh", CONNECTION, "worker-a", now=100, ttl=10, once=True)
    )
    assert claimed["fence"] == 1 and claimed["phase"] == "claimed"
    store.write(
        lambda tx: tx.require_lease("refresh", CONNECTION, "worker-a", 1, now=101, phase="sent")
    )
    reopened = ConnectionStore(store.base_dir, owner_key=OWNER)
    assert_code(
        "connection_unknown",
        lambda: reopened.write(
            lambda tx: tx.claim(
                "refresh",
                CONNECTION,
                "worker-b",
                now=111,
                ttl=10,
                once=True,
            )
        ),
    )
    assert reopened.read(lambda tx: tx.lease("refresh", CONNECTION))["phase"] == "sent"


def test_expired_lease_fences_old_holder_and_new_claim_is_monotone(store):
    store.write(lambda tx: tx.claim("sync", CONNECTION, "worker-a", now=100, ttl=1))
    assert_code(
        "connection_busy",
        lambda: store.write(lambda tx: tx.claim("sync", CONNECTION, "worker-b", now=100.5)),
    )
    newer = store.write(lambda tx: tx.claim("sync", CONNECTION, "worker-b", now=102, ttl=10))
    assert newer["fence"] == 2
    assert_code(
        "lease_lost",
        lambda: store.write(
            lambda tx: tx.require_lease("sync", CONNECTION, "worker-a", 1, now=102.5)
        ),
    )
    assert_code(
        "lease_lost",
        lambda: store.write(
            lambda tx: tx.require_lease("sync", CONNECTION, "worker-b", 2, now=112)
        ),
    )


def test_lease_phase_returns_updated_row_and_cannot_reset_sent_to_claimed(store):
    store.write(lambda tx: tx.claim("refresh", CONNECTION, "worker", now=100, ttl=20))
    sent = store.write(
        lambda tx: tx.require_lease(
            "refresh",
            CONNECTION,
            "worker",
            1,
            now=101,
            phase="sent",
        )
    )
    assert sent["phase"] == "sent"
    assert_code(
        "lease_lost",
        lambda: store.write(
            lambda tx: tx.require_lease(
                "refresh",
                CONNECTION,
                "worker",
                1,
                now=102,
                phase="claimed",
            )
        ),
    )
    finished = store.write(
        lambda tx: tx.require_lease(
            "refresh",
            CONNECTION,
            "worker",
            1,
            now=103,
            phase="finished",
        )
    )
    assert finished["phase"] == "finished" and finished["expires_at"] == 103
    newer = store.write(lambda tx: tx.claim("refresh", CONNECTION, "new-worker", now=103))
    assert newer["fence"] == 2 and newer["holder"] == "new-worker"


def test_parallel_real_process_refresh_claim_has_one_winner(processes, tmp_path, store):
    children = [
        start(processes, tmp_path, store, mode="claim", document={"holder": f"worker-{index}"})
        for index in range(6)
    ]
    for child, ready, _entered in children:
        wait_marker(child, ready)
    for child, _ready, _entered in children:
        release(child)
    outcomes = [finish(child) for child, _ready, _entered in children]
    assert sum(result["status"] == "ok" for result in outcomes) == 1
    assert outcomes.count({"status": "error", "code": "connection_busy"}) == 5


@pytest.mark.parametrize("now", [True, float("inf"), float("-inf"), float("nan"), "100", None])
def test_lease_time_rejects_nonfinite_or_non_numeric_values(store, now):
    assert_code(
        "invalid_input",
        lambda: store.write(lambda tx: tx.claim("refresh", CONNECTION, "worker", now=now)),
    )


@pytest.mark.parametrize("target", ["db_path", "witness_path", "lock_path"])
@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_linked_storage_paths_do_not_write_external_target(store, tmp_path, target, link_kind):
    store.base_dir.mkdir()
    outside = tmp_path / "external-private-file"
    outside.write_bytes(b"ORIGINAL_EXTERNAL_BYTES")
    named = getattr(store, target)
    if link_kind == "hardlink":
        os.link(outside, named)
    else:
        try:
            named.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"平台未启用符号链接创建：{type(exc).__name__}")
    before = state(outside)
    assert_code("storage_error", lambda: create(store))
    assert state(outside) == before
    if target != "db_path":
        assert not store.db_path.exists()


def test_nonempty_lock_content_is_not_truncated(store):
    store.base_dir.mkdir()
    store.lock_path.write_bytes(b"EXTERNAL_LOCK_CONTENT")
    before = state(store.lock_path)
    assert_code("storage_error", lambda: create(store))
    assert state(store.lock_path) == before and not store.db_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生目录联接验证")
@pytest.mark.parametrize("target", ["base_dir", "db_path", "witness_path", "lock_path"])
def test_windows_directory_junction_never_writes_external_target(store, tmp_path, target):
    outside = tmp_path / "external-junction-target"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"UNCHANGED_EXTERNAL_DIRECTORY")
    named = getattr(store, target)
    if target != "base_dir":
        store.base_dir.mkdir()
    command = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(named), str(outside)],
        capture_output=True,
        text=True,
    )
    if command.returncode:
        pytest.skip("Windows 目录联接创建未成功")
    before = state(sentinel)
    try:
        assert_code("storage_error", lambda: create(store))
        assert state(sentinel) == before
        assert sorted(path.name for path in outside.iterdir()) == ["sentinel.bin"]
    finally:
        os.rmdir(named)


def test_failed_initial_validation_does_not_mark_database_seen(store):
    store.base_dir.mkdir()
    store.db_path.write_bytes(b"NOT_A_DATABASE")
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    assert_code("storage_error", lambda: create(store))
    assert not store._seen
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
