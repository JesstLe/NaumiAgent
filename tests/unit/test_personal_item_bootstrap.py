"""Real-process contracts for first-use personal-item SQLite authority initialization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from naumi_agent.personal import item_store
from naumi_agent.personal.item_store import PersonalItemStore
from naumi_agent.personal.models import PersonalAgentError

OWNER = hashlib.sha256(b"item-bootstrap-owner").hexdigest()
OTHER_OWNER = hashlib.sha256(b"item-bootstrap-other-owner").hexdigest()

# Each child imports the actual implementation and uses actual SQLite files. The files and
# stdin messages coordinate process scheduling only; no storage operation is replaced.
_WORKER = """
import json
import pathlib
import sys

from naumi_agent.personal.item_store import PersonalItemStore
from naumi_agent.personal.models import PersonalAgentError

base, owner, mode, ready, entered, definition_json = sys.argv[1:]
ready = pathlib.Path(ready)
entered = pathlib.Path(entered)

class PausedStore(PersonalItemStore):
    def _validate_schema(self, connection, *, create, owner):
        super()._validate_schema(connection, create=create, owner=owner)
        if create:
            entered.write_text('SCHEMA_READY', encoding='ascii')
            if sys.stdin.readline() != 'GO\\n':
                raise RuntimeError('bootstrap release was not received')

class PausedMutationStore(PersonalItemStore):
    def _save_request(self, *args, **kwargs):
        super()._save_request(*args, **kwargs)
        entered.write_text('BUSINESS_WRITES_READY', encoding='ascii')
        if sys.stdin.readline() != 'GO\\n':
            raise RuntimeError('business transaction release was not received')

def trace_sql(statement):
    if statement.strip().upper() == 'BEGIN IMMEDIATE':
        entered.write_text('BEGIN_IMMEDIATE', encoding='ascii')

class ObservedWaiterStore(PersonalItemStore):
    def _validate_schema(self, connection, *, create, owner):
        super()._validate_schema(connection, create=create, owner=owner)
        # Install the observer only after the actual SQLite connection is initialized.
        connection.set_trace_callback(trace_sql)
        entered.with_suffix('.validated').write_text('VALIDATED', encoding='ascii')

store_type = {
    'paused-create': PausedStore,
    'hold-business': PausedMutationStore,
    'wait-business': ObservedWaiterStore,
}.get(mode, PersonalItemStore)
store = store_type(base)
ready.write_text('READY', encoding='ascii')
if sys.stdin.readline() != 'GO\\n':
    raise RuntimeError('start release was not received')

try:
    if mode in {'hold', 'probe'}:
        store.base_dir.mkdir(parents=True, exist_ok=True)
        with store._bootstrap_guard(create=mode == 'hold'):
            entered.write_text('LOCKED', encoding='ascii')
            if mode == 'hold' and sys.stdin.readline() != 'GO\\n':
                raise RuntimeError('lock release was not received')
        result = {'status': 'ok'}
    elif mode == 'hold-business':
        original = store.list(owner_key=owner)[0]
        item = store.control(
            owner_key=owner,
            item_id=original['item_id'],
            expected_revision=original['revision'],
            request_id='hold-business-pause',
            action='pause',
        )
        result = {'status': 'ok', 'item': item}
    elif mode == 'read':
        entered.write_text('READING', encoding='ascii')
        result = {
            'status': 'ok',
            'instance': store.identity(owner_key=owner),
            'items': store.list(owner_key=owner),
        }
    else:
        if mode not in {'paused-create', 'wait-business'}:
            entered.write_text('CREATING', encoding='ascii')
        item = store.create(
            owner_key=owner,
            request_id='same-first-request',
            definition=json.loads(definition_json),
        )
        result = {
            'status': 'ok',
            'owner': owner,
            'item': item,
            'instance': store.identity(owner_key=owner),
        }
except PersonalAgentError as error:
    cause = error.__cause__
    result = {
        'status': 'error',
        'owner': owner,
        'code': error.code,
        'message': error.message,
        'cause_type': None if cause is None else type(cause).__name__,
        'cause': None if cause is None else str(cause),
    }
print(json.dumps(result, ensure_ascii=True), flush=True)
"""


@pytest.fixture
def definition(tmp_path):
    return {
        "title": "整理资料",
        "instructions": "保留原文与来源，保存一份真实报告。",
        "tags": ["工作"],
        "assignee": {"id": "main", "version": 0, "name": "主 Agent", "kind": "main"},
        "origin": {"actor_id": "main", "session_id": "bootstrap-session"},
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
    return PersonalItemStore(tmp_path / "items")


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


def _start(processes, tmp_path, store, definition, *, mode="create", owner=OWNER):
    index = len(processes)
    ready = tmp_path / f"process-{index}.ready"
    entered = tmp_path / f"process-{index}.entered"
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
            json.dumps(definition, ensure_ascii=True),
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


def _wait_marker(child, marker, *, timeout=20):
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(
                f"进程在同步点前退出：exit={child.returncode}, stdout={stdout}, stderr={stderr}"
            )
        if time.monotonic() >= deadline:
            pytest.fail(f"进程未按时抵达同步点：{marker.name}")
        time.sleep(0.01)


def _release(child):
    assert child.stdin is not None
    child.stdin.write("GO\n")
    child.stdin.flush()


def _finish(child, *, timeout=25):
    stdout, stderr = child.communicate(timeout=timeout)
    assert child.returncode == 0, (child.returncode, stdout, stderr)
    assert not stderr, stderr
    return json.loads(stdout)


def _create(store, definition):
    return store.create(
        owner_key=OWNER, request_id="same-first-request", definition=definition
    )


def _storage_error(callback):
    with pytest.raises(PersonalAgentError) as error:
        callback()
    assert error.value.code == "storage_error", error.value


def _assert_authority(store, *, owner=OWNER, revisions=1, requests=1):
    instance = store.identity(owner_key=owner)
    assert isinstance(instance, str) and len(instance) == 32
    assert store._witness.read_text("ascii") == instance
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute(
            "SELECT owner_key,instance_id FROM item_store_meta"
        ).fetchall() == [(owner, instance)]
        for table, expected in (
            ("personal_items", 1),
            ("personal_item_revisions", revisions),
            ("personal_item_requests", requests),
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (expected,)
    return instance


def _file_state(path):
    info = path.stat()
    return path.read_bytes(), info.st_size, info.st_mtime_ns


def test_independent_instances_synchronize_first_create_and_keep_one_identity(store, definition):
    barrier = threading.Barrier(8)

    def create_from_independent_instance(_index):
        other = PersonalItemStore(store.base_dir)
        barrier.wait(timeout=15)
        item = _create(other, definition)
        return item, other.identity(owner_key=OWNER)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create_from_independent_instance, range(8)))
    assert all(result == results[0] for result in results)
    assert _assert_authority(store) == results[0][1]
    assert PersonalItemStore(store.base_dir).get(
        owner_key=OWNER, item_id=results[0][0]["item_id"]
    ) == results[0][0]


def test_os_processes_share_first_request_winner_and_database_identity(
    processes, tmp_path, store, definition
):
    children = [_start(processes, tmp_path, store, definition) for _ in range(8)]
    for child, ready, _entered in children:
        _wait_marker(child, ready)
    assert not store.base_dir.exists()
    for child, _ready, _entered in children:
        _release(child)
    results = [_finish(child) for child, _ready, _entered in children]
    assert all(result["status"] == "ok" for result in results), results
    assert all(result == results[0] for result in results)
    assert _assert_authority(store) == results[0]["instance"]
    assert store.find_request(owner_key=OWNER, request_id="same-first-request")["item"] == (
        results[0]["item"]
    )


def test_competing_owner_processes_bind_first_database_to_exactly_one_owner(
    processes, tmp_path, store, definition
):
    owners = [OWNER, OTHER_OWNER] * 3
    children = [
        _start(processes, tmp_path, store, definition, owner=owner) for owner in owners
    ]
    for child, ready, _entered in children:
        _wait_marker(child, ready)
    for child, _ready, _entered in children:
        _release(child)
    results = [_finish(child) for child, _ready, _entered in children]
    accepted = [result for result in results if result["status"] == "ok"]
    rejected = [result for result in results if result["status"] == "error"]
    assert len(accepted) == len(rejected) == 3, results
    assert len({result["owner"] for result in accepted}) == 1
    assert all(result == accepted[0] for result in accepted)
    assert {result["code"] for result in rejected} == {"storage_error"}
    assert {result["owner"] for result in rejected} == (
        {OWNER, OTHER_OWNER} - {accepted[0]["owner"]}
    )
    assert _assert_authority(store, owner=accepted[0]["owner"]) == accepted[0]["instance"]


def test_same_request_with_competing_payloads_commits_exactly_one_definition(
    processes, tmp_path, store, definition
):
    children = [
        _start(processes, tmp_path, store, {**definition, "title": f"整理资料 {index}"})
        for index in range(6)
    ]
    for child, ready, _entered in children:
        _wait_marker(child, ready)
    for child, _ready, _entered in children:
        _release(child)
    results = [_finish(child) for child, _ready, _entered in children]
    accepted = [result for result in results if result["status"] == "ok"]
    rejected = [result for result in results if result["status"] == "error"]
    assert len(accepted) == 1 and len(rejected) == 5, results
    assert {result["code"] for result in rejected} == {"idempotency_conflict"}
    assert _assert_authority(store) == accepted[0]["instance"]
    assert store.list(owner_key=OWNER) == [accepted[0]["item"]]


def test_reader_waits_for_schema_and_witness_instead_of_observing_partial_bootstrap(
    processes, tmp_path, store, definition
):
    writer, ready, schema_ready = _start(
        processes, tmp_path, store, definition, mode="paused-create"
    )
    _wait_marker(writer, ready)
    _release(writer)
    _wait_marker(writer, schema_ready)
    assert store.db_path.is_file()
    assert not store._witness.exists()

    reader, reader_ready, reading = _start(processes, tmp_path, store, definition, mode="read")
    _wait_marker(reader, reader_ready)
    _release(reader)
    _wait_marker(reader, reading)
    time.sleep(0.2)
    assert reader.poll() is None, "读取进程必须等待初始化事务完整提交"

    _release(writer)
    created, observed = _finish(writer), _finish(reader)
    assert created["status"] == observed["status"] == "ok", (created, observed)
    assert observed["instance"] == created["instance"] == _assert_authority(store)
    # The user mutation follows bootstrap in its own transaction; either committed view is valid.
    assert observed["items"] in ([], [created["item"]])


@pytest.mark.parametrize("entrypoint", ["list", "identity", "connection"])
def test_reader_retries_when_writer_commits_between_database_and_witness_samples(
    store, definition, monkeypatch, entrypoint
):
    actual_is_file = type(store.db_path).is_file
    committed = {}
    fired = False

    def sample_database_then_schedule_writer(path):
        nonlocal fired
        observed = actual_is_file(path)
        if path == store.db_path and not fired:
            assert observed is False
            assert not store._bootstrap.exists()
            assert not store._witness.exists()
            fired = True
            # Retain the real old filesystem sample, but let an independent store finish
            # actual lock/SQLite/witness initialization before the reader checks its witness.
            writer = PersonalItemStore(store.base_dir)
            committed["item"] = _create(writer, definition)
            committed["instance"] = writer.identity(owner_key=OWNER)
            assert store._bootstrap.is_file()
            assert store._witness.read_text("ascii") == committed["instance"]
        return observed

    monkeypatch.setattr(type(store.db_path), "is_file", sample_database_then_schedule_writer)
    if entrypoint == "list":
        observed = store.list(owner_key=OWNER)
        assert observed == [committed["item"]]
    elif entrypoint == "identity":
        observed = store.identity(owner_key=OWNER)
        assert observed == committed["instance"]
    else:
        # Exercise the connection's observation separately from public methods' _exists check.
        with store._connection(OWNER) as connection:
            observed = connection.execute("SELECT item_id FROM personal_items").fetchall()
            assert [row[0] for row in observed] == [committed["item"]["item_id"]]
    assert fired, "测试必须在无 DB 样本与 witness 样本之间实际完成首次写入"
    assert store.list(owner_key=OWNER) == [committed["item"]]
    assert store.get(owner_key=OWNER, item_id=committed["item"]["item_id"]) == committed["item"]
    assert store.identity(owner_key=OWNER) == committed["instance"]
    assert _assert_authority(store) == committed["instance"]


def test_os_releases_bootstrap_lock_after_abrupt_holder_exit(
    processes, tmp_path, store, definition
):
    holder, ready, locked = _start(processes, tmp_path, store, definition, mode="hold")
    _wait_marker(holder, ready)
    _release(holder)
    _wait_marker(holder, locked)
    contender, contender_ready, creating = _start(processes, tmp_path, store, definition)
    _wait_marker(contender, contender_ready)
    _release(contender)
    _wait_marker(contender, creating)
    time.sleep(0.2)
    assert contender.poll() is None, "首次写入必须等待仍然存活的锁持有进程"
    assert not store.db_path.exists()

    holder.kill()
    holder.communicate(timeout=10)
    assert holder.returncode != 0
    result = _finish(contender)
    assert result["status"] == "ok", result
    assert result["instance"] == _assert_authority(store)


def test_bootstrap_lock_is_not_held_while_user_transaction_is_yielded(
    processes, tmp_path, store, definition
):
    _create(store, definition)
    with store._connection(OWNER, write=True) as connection:
        assert connection.in_transaction
        probe, ready, locked = _start(processes, tmp_path, store, definition, mode="probe")
        _wait_marker(probe, ready)
        _release(probe)
        _wait_marker(probe, locked, timeout=5)
        assert _finish(probe) == {"status": "ok"}
        assert connection.in_transaction
    _assert_authority(store)


def test_waiting_business_writer_does_not_block_third_process_reading_committed_items(
    processes, tmp_path, store, definition
):
    original = _create(store, definition)
    identity = store.identity(owner_key=OWNER)

    writer, writer_ready, business_written = _start(
        processes, tmp_path, store, definition, mode="hold-business"
    )
    _wait_marker(writer, writer_ready)
    _release(writer)
    _wait_marker(writer, business_written)
    assert business_written.read_text("ascii") == "BUSINESS_WRITES_READY"

    waiter, waiter_ready, beginning = _start(
        processes, tmp_path, store, definition, mode="wait-business"
    )
    _wait_marker(waiter, waiter_ready)
    _release(waiter)
    _wait_marker(waiter, beginning)
    assert beginning.read_text("ascii") == "BEGIN_IMMEDIATE"
    assert beginning.with_suffix(".validated").read_text("ascii") == "VALIDATED"
    assert writer.poll() is waiter.poll() is None

    # P1 still owns a real SQLite writer transaction. P2 has validated the bootstrap and
    # reached its actual BEGIN IMMEDIATE, so P3 must read without waiting for either writer.
    reader, reader_ready, _reading = _start(
        processes, tmp_path, store, definition, mode="read"
    )
    _wait_marker(reader, reader_ready)
    _release(reader)
    observed = _finish(reader, timeout=5)
    assert observed == {"status": "ok", "instance": identity, "items": [original]}
    assert writer.poll() is waiter.poll() is None

    _release(writer)
    committed, replay = _finish(writer), _finish(waiter)
    assert committed["status"] == replay["status"] == "ok", (committed, replay)
    assert committed["item"]["state"] == "paused"
    assert committed["item"]["revision"] == original["revision"] + 1
    assert replay["item"] == committed["item"]
    assert replay["instance"] == _assert_authority(store, revisions=2, requests=2) == identity


def test_real_os_lock_timeout_explains_initialization_and_preserves_retry(
    processes, tmp_path, store, definition, monkeypatch
):
    holder, ready, locked = _start(processes, tmp_path, store, definition, mode="hold")
    _wait_marker(holder, ready)
    _release(holder)
    _wait_marker(holder, locked)
    with monkeypatch.context() as patcher:
        # Keep the OS lock, clock and SQLite real; only shorten the configured waiting budget.
        patcher.setattr(item_store, "_BOOTSTRAP_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(PersonalAgentError) as error:
            _create(store, definition)
    assert error.value.code == "storage_error"
    assert "正在初始化" in error.value.message
    assert "稍后重试" in error.value.message
    assert holder.poll() is None
    assert not store.db_path.exists()
    assert not store._witness.exists()

    _release(holder)
    assert _finish(holder) == {"status": "ok"}
    item = _create(store, definition)
    assert store.list(owner_key=OWNER) == [item]
    _assert_authority(store)


def test_acquired_lock_rechecks_open_descriptor_against_replaced_path(
    store, tmp_path, monkeypatch
):
    store.base_dir.mkdir()
    store._bootstrap.touch()
    detached = tmp_path / "detached-bootstrap.lock"
    replacement_denied = []
    acquired = []

    def replace_after_acquire():
        if acquired:
            return
        acquired.append(True)
        try:
            store._bootstrap.rename(detached)
        except OSError as error:
            replacement_denied.append(error)
            return
        store._bootstrap.write_bytes(b"")

    if os.name == "nt":
        import msvcrt

        actual_lock = msvcrt.locking

        def observed_lock(descriptor, operation, count):
            result = actual_lock(descriptor, operation, count)
            if operation == msvcrt.LK_NBLCK:
                replace_after_acquire()
            return result

        monkeypatch.setattr(msvcrt, "locking", observed_lock)
    else:
        import fcntl

        actual_lock = fcntl.flock

        def observed_lock(descriptor, operation):
            result = actual_lock(descriptor, operation)
            if operation & fcntl.LOCK_EX:
                replace_after_acquire()
            return result

        monkeypatch.setattr(fcntl, "flock", observed_lock)

    failure = None
    yielded = False
    try:
        with store._bootstrap_guard(create=True):
            yielded = True
    except PersonalAgentError as error:
        failure = error
    assert acquired, "测试必须先获得真实 OS 文件锁"
    if replacement_denied:
        pytest.skip(f"当前系统拒绝替换已锁定路径：{replacement_denied[0]}")
    assert failure is not None and failure.code == "storage_error"
    assert not yielded
    assert store._bootstrap.read_bytes() == detached.read_bytes() == b""
    assert not os.path.samestat(store._bootstrap.stat(), detached.stat())
    assert not store.db_path.exists()
    assert not store._witness.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("contents", [b"", b"ORIGINAL_EXTERNAL_CONTENT"], ids=["empty", "data"])
def test_bootstrap_lock_links_do_not_modify_external_file(
    store, tmp_path, definition, kind, contents
):
    outside = tmp_path / "external-file"
    outside.write_bytes(contents)
    store.base_dir.mkdir()
    if kind == "hardlink":
        os.link(outside, store._bootstrap)
    else:
        try:
            store._bootstrap.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"当前系统未开启符号链接创建：{error}")
    before = _file_state(outside)
    _storage_error(lambda: _create(store, definition))
    _storage_error(lambda: store.list(owner_key=OWNER))
    assert _file_state(outside) == before
    assert not store.db_path.exists()
    assert not store._witness.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接验证")
def test_bootstrap_lock_reparse_point_does_not_write_external_directory(
    store, tmp_path, definition
):
    outside = tmp_path / "external-directory"
    outside.mkdir()
    marker = outside / "original.txt"
    marker.write_bytes(b"ORIGINAL_EXTERNAL_DIRECTORY")
    store.base_dir.mkdir()
    completed = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(store._bootstrap), str(outside)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    try:
        assert store._bootstrap.lstat().st_file_attributes & 0x400
        before = _file_state(marker)
        _storage_error(lambda: _create(store, definition))
        _storage_error(lambda: store.identity(owner_key=OWNER))
        assert _file_state(marker) == before
        assert [path.name for path in outside.iterdir()] == [marker.name]
        assert not store.db_path.exists()
        assert not store._witness.exists()
    finally:
        # Remove the junction itself, never traverse or recursively delete its target.
        os.rmdir(store._bootstrap)


def test_nonempty_bootstrap_lock_is_rejected_without_truncation(store, definition):
    store.base_dir.mkdir()
    store._bootstrap.write_bytes(b"UNRECOGNIZED_LOCK_CONTENT")
    before = _file_state(store._bootstrap)
    _storage_error(lambda: _create(store, definition))
    assert _file_state(store._bootstrap) == before
    assert not store.db_path.exists()
    assert not store._witness.exists()


@pytest.mark.parametrize("state", ["empty-file", "empty-sqlite", "corrupt", "other-schema"])
def test_preexisting_unrecognized_database_is_never_bootstrapped(store, definition, state):
    store.base_dir.mkdir()
    if state == "empty-file":
        store.db_path.touch()
    elif state == "corrupt":
        store.db_path.write_bytes(b"NOT_A_SQLITE_DATABASE")
    else:
        with sqlite3.connect(store.db_path) as connection:
            if state == "other-schema":
                connection.execute("CREATE TABLE original_fact (value TEXT)")
                connection.execute("INSERT INTO original_fact VALUES ('preserve me')")
            else:
                connection.execute("PRAGMA user_version=0")
    before = _file_state(store.db_path)
    for other in (store, PersonalItemStore(store.base_dir)):
        _storage_error(lambda other=other: other.list(owner_key=OWNER))
        _storage_error(lambda other=other: _create(other, definition))
        assert _file_state(store.db_path) == before
        assert not store._witness.exists()


def test_missing_original_database_is_not_recreated_by_any_instance(
    store, tmp_path, definition
):
    item = _create(store, definition)
    identity = store.identity(owner_key=OWNER)
    witness_before = _file_state(store._witness)
    preserved = tmp_path / "original-personal-items.db"
    store.db_path.rename(preserved)
    preserved_before = _file_state(preserved)
    for other in (store, PersonalItemStore(store.base_dir)):
        _storage_error(lambda other=other: other.identity(owner_key=OWNER))
        _storage_error(lambda other=other: other.list(owner_key=OWNER))
        _storage_error(lambda other=other: _create(other, definition))
        assert not store.db_path.exists()
    assert _file_state(preserved) == preserved_before
    assert _file_state(store._witness) == witness_before
    preserved.rename(store.db_path)
    assert _assert_authority(PersonalItemStore(store.base_dir)) == identity
    assert store.get(owner_key=OWNER, item_id=item["item_id"]) == item


def test_missing_witness_is_not_recreated_for_an_existing_database(store, definition):
    _create(store, definition)
    store._witness.unlink()
    before = _file_state(store.db_path)
    for other in (store, PersonalItemStore(store.base_dir)):
        _storage_error(lambda other=other: other.identity(owner_key=OWNER))
        _storage_error(lambda other=other: _create(other, definition))
        assert not store._witness.exists()
    assert _file_state(store.db_path) == before


def test_witness_without_database_is_not_treated_as_fresh_storage(store, definition):
    store.base_dir.mkdir()
    store._witness.write_text("f" * 32, encoding="ascii")
    before = _file_state(store._witness)
    _storage_error(lambda: store.identity(owner_key=OWNER))
    _storage_error(lambda: _create(store, definition))
    assert _file_state(store._witness) == before
    assert not store.db_path.exists()
