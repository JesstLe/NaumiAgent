"""Real workspace journal bootstrap, atomic facts, process races and instance fencing."""

from __future__ import annotations

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
from naumi_agent.personal.workspace_store import (
    APPEND_ONLY,
    KINDS,
    WorkspaceStore,
    WorkspaceTransaction,
)

OWNER, OTHER, WORKSPACE = "a" * 64, "b" * 64, "c" * 64
GRANT = "wg_" + "1" * 32
DOCUMENT = {"id": GRANT, "workspace_key": WORKSPACE, "status": "active", "opaque": "fixture"}


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path / "workspaces", owner_key=OWNER)


def assert_code(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    assert caught.value.code == code


def create(store):
    return store.mutate(
        "grant-original",
        "issue",
        {"grant": GRANT},
        lambda tx: tx.put("grant", DOCUMENT, expected_revision=0),
    )


def counts(store):
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return tuple(
            db.execute(f"SELECT count(*) FROM workspace_{table}").fetchone()[0]
            for table in ("records", "history", "requests", "lease_history")
        )


def test_workspace_store_is_independent_authentic_journal_without_execution(store):
    assert KINDS == frozenset({"grant", "connection", "source_read", "read"})
    assert APPEND_ONLY == frozenset({"source_read", "read"})
    assert store.transaction_type is WorkspaceTransaction
    assert store.spec.namespace == "workspace" and store.spec.parent_field == "workspace_key"
    assert store.spec.domain_hashes is store.spec.lease_proofs is True
    assert store.db_path.name == "personal_workspaces.db"
    assert store.witness_path.name == ".personal-workspaces-v1"
    assert store.lock_path.name == ".personal-workspaces-bootstrap.lock"
    assert not hasattr(store, "start") and not hasattr(store, "tick")


def test_fresh_read_has_no_files_or_enrollment(store):
    assert store.identity() is None
    assert store.read(lambda tx: tx is None) is True
    assert not store.base_dir.exists()


def test_metadata_only_bootstrap_has_no_business_or_request_records(store):
    assert store.write(lambda tx: None) is None
    identity = store.identity()
    assert len(identity) == 32 and store.witness_path.read_text("ascii") == identity
    assert counts(store) == (0, 0, 0, 0)
    cold = WorkspaceStore(store.base_dir, owner_key=OWNER, expected_instance_id=identity)
    assert cold.identity() == identity


@pytest.mark.parametrize("kind", ["grant", "connection"])
def test_lifecycle_records_use_cas_with_authenticated_old_revisions(store, kind):
    old = store.write(lambda tx: tx.put(kind, DOCUMENT, expected_revision=0))
    new = store.write(lambda tx: tx.put(kind, {**old, "status": "revoked"}, expected_revision=1))
    assert old["revision"] == 1 and new["revision"] == 2
    assert store.read(lambda tx: tx.get(kind, GRANT, revision=1)) == old
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put(kind, DOCUMENT, expected_revision=1)),
    )
    assert counts(store) == (1, 2, 0, 0)


@pytest.mark.parametrize("kind", sorted(APPEND_ONLY))
def test_source_and_recipient_read_proofs_are_append_only(store, kind):
    document = {"id": "read-opaque", "workspace_key": WORKSPACE, "body_sha256": "d" * 64}
    original = store.write(lambda tx: tx.put(kind, document, expected_revision=0))
    assert_code(
        f"immutable_{kind}",
        lambda: store.write(lambda tx: tx.put(kind, document, expected_revision=1)),
    )
    assert_code(
        "version_conflict",
        lambda: store.write(lambda tx: tx.put(kind, document, expected_revision=0)),
    )
    assert store.read(lambda tx: tx.get(kind, document["id"])) == original
    assert counts(store) == (1, 1, 0, 0)


def test_grant_read_and_opaque_request_commit_atomically_and_rollback_together(store):
    create(store)

    def fail(tx):
        tx.put("read", {"id": "read-opaque", "workspace_key": WORKSPACE}, expected_revision=0)
        tx.put("grant", DOCUMENT, expected_revision=0)

    assert_code("version_conflict", lambda: store.mutate("failing-request", "read", {}, fail))
    assert store.read(lambda tx: tx.get("read", "read-opaque", missing=True)) is None
    assert store.read(lambda tx: tx.request("failing-request")) is None
    assert counts(store) == (1, 1, 1, 0)


def test_request_replay_is_exact_and_conflicting_envelope_never_executes(store):
    original, invoked = create(store), []
    assert (
        store.mutate("grant-original", "issue", {"grant": GRANT}, lambda tx: invoked.append(tx))
        == original
    )
    for operation, payload in (("revoke", {"grant": GRANT}), ("issue", {"grant": "other"})):
        assert_code(
            "idempotency_conflict",
            lambda op=operation, value=payload: store.mutate(
                "grant-original", op, value, lambda tx: invoked.append(tx)
            ),
        )
    assert not invoked and counts(store) == (1, 1, 1, 0)


def test_scope_keysets_authenticate_each_record_and_keep_cross_workspace_facts_separate(store):
    for number in range(6):
        store.write(
            lambda tx, n=number: tx.put(
                "connection",
                {
                    "id": f"wc_{n:032x}",
                    "workspace_key": WORKSPACE if n % 2 == 0 else OTHER,
                    "status": "active",
                },
                expected_revision=0,
            )
        )
    first = store.read(lambda tx: tx.page("connection", parent=WORKSPACE, limit=2))
    assert [value["id"] for value in first["records"]] == [f"wc_{number:032x}" for number in (0, 2)]
    last = store.read(
        lambda tx: tx.page("connection", parent=WORKSPACE, limit=2, after=first["next_after"])
    )
    assert [value["id"] for value in last["records"]] == [f"wc_{4:032x}"]
    assert last["next_after"] is None


def test_simultaneous_instances_admit_one_request_and_one_cas_winner(store):
    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(
            pool.map(lambda _: create(WorkspaceStore(store.base_dir, owner_key=OWNER)), range(5))
        )
    assert values == [values[0]] * 5

    def update(number):
        try:
            return WorkspaceStore(store.base_dir, owner_key=OWNER).write(
                lambda tx: tx.put("grant", {**DOCUMENT, "winner": number}, expected_revision=1)
            )
        except PersonalAgentError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=5) as pool:
        updates = list(pool.map(update, range(5)))
    assert sum(isinstance(value, dict) for value in updates) == 1
    assert updates.count("version_conflict") == 4
    assert counts(store) == (1, 2, 1, 0)


def test_four_real_processes_share_one_initial_instance_and_receipt(store, tmp_path):
    worker = r"""
import json,pathlib,sys
from naumi_agent.personal.workspace_store import WorkspaceStore
base,ready,document=sys.argv[1:]
pathlib.Path(ready).write_text('READY',encoding='ascii')
assert sys.stdin.readline().strip()=='GO'
store=WorkspaceStore(base,owner_key='a'*64)
result=store.mutate('same','issue',{'same':True},
    lambda tx:tx.put('grant',json.loads(document),expected_revision=0))
print(json.dumps({'instance':store.identity(),'result':result}),flush=True)
"""
    children = []
    try:
        for number in range(4):
            ready = tmp_path / f"ready-{number}"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(store.base_dir),
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
            children.append((process, ready))
        deadline = time.monotonic() + 30
        while not all(ready.exists() for _, ready in children):
            assert time.monotonic() < deadline and all(
                process.poll() is None for process, _ in children
            )
            time.sleep(0.01)
        assert not store.db_path.exists()
        for process, _ in children:
            process.stdin.write("GO\n")
            process.stdin.flush()
        results = []
        for process, _ in children:
            stdout, stderr = process.communicate(timeout=40)
            assert process.returncode == 0 and not stderr
            results.append(json.loads(stdout))
        assert results == [results[0]] * 4 and results[0]["instance"] == store.identity()
        assert counts(store) == (1, 1, 1, 0)
    finally:
        for process, _ in children:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_durable_slot_lease_unknown_is_not_reprovision_permission(store):
    lease = store.write(lambda tx: tx.claim("vault", GRANT, "first", now=100, ttl=10, once=True))
    store.write(
        lambda tx: tx.require_lease("vault", GRANT, "first", lease["fence"], now=101, phase="sent")
    )
    store.write(
        lambda tx: tx.require_lease(
            "vault", GRANT, "first", lease["fence"], now=102, phase="unknown"
        )
    )
    reopened = WorkspaceStore(
        store.base_dir, owner_key=OWNER, expected_instance_id=store.identity()
    )
    assert_code(
        "workspace_unknown",
        lambda: reopened.write(lambda tx: tx.claim("vault", GRANT, "second", now=120, once=True)),
    )
    assert reopened.read(lambda tx: tx.lease("vault", GRANT))["phase"] == "unknown"
    assert counts(store) == (0, 0, 0, 3)


def test_owner_instance_and_missing_witness_are_not_repaired(store):
    create(store)
    before = store.db_path.read_bytes(), store.witness_path.read_bytes()
    for candidate in (
        WorkspaceStore(store.base_dir, owner_key=OTHER),
        WorkspaceStore(store.base_dir, owner_key=OWNER, expected_instance_id="f" * 32),
    ):
        assert_code("storage_error", lambda item=candidate: item.read(lambda tx: tx.list("grant")))
    assert before == (store.db_path.read_bytes(), store.witness_path.read_bytes())
    store.witness_path.unlink()
    assert_code("storage_error", lambda: create(WorkspaceStore(store.base_dir, owner_key=OWNER)))
    assert not store.witness_path.exists()


def test_external_instance_pin_refuses_deleted_database_and_witness(store):
    create(store)
    identity = store.identity()
    store.db_path.unlink()
    store.witness_path.unlink()
    reopened = WorkspaceStore(store.base_dir, owner_key=OWNER, expected_instance_id=identity)
    assert_code("storage_error", lambda: reopened.read(lambda tx: tx.list("grant")))
    assert_code("storage_error", lambda: create(reopened))
    assert not store.db_path.exists() and not store.witness_path.exists()


@pytest.mark.parametrize("kind", ["grant", "source_read"])
def test_record_payload_uses_owner_domain_and_full_history_validation(store, kind):
    original = store.write(lambda tx: tx.put(kind, DOCUMENT, expected_revision=0))
    altered = {**original, "opaque": "forged"}
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE workspace_records SET payload=?,sha256=? WHERE kind=?",
            (canonical(altered), digest({"kind": kind, "value": altered}), kind),
        )
    assert_code("storage_error", lambda: store.read(lambda tx: tx.get(kind, GRANT)))


@pytest.mark.parametrize("table", ["meta", "history", "requests", "lease_history"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_sqlite_immutable_tables_are_not_mutable_through_other_clients(store, table, operation):
    create(store)
    store.write(lambda tx: tx.claim("slot", GRANT, "one", now=100))
    sql = (
        f"DELETE FROM workspace_{table}"
        if operation == "DELETE"
        else f"UPDATE workspace_{table} SET rowid=rowid"
    )
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)


@pytest.mark.parametrize("revision", [True, -1, "0", 0.0, None])
def test_revision_type_is_strict(store, revision):
    assert_code(
        "invalid_input",
        lambda: store.write(lambda tx: tx.put("grant", DOCUMENT, expected_revision=revision)),
    )


def test_private_database_hardlink_is_not_an_alternative_authority(store, tmp_path):
    create(store)
    os.link(store.db_path, tmp_path / "aliased.db")
    assert_code("storage_error", lambda: store.read(lambda tx: tx.list("grant")))
