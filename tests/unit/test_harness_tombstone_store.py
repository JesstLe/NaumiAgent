"""HAR-06.3a durable tombstone and retry lease tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.retention import LifecycleActor, LifecyclePolicy
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)
from naumi_agent.harness.tombstone import (
    ReconciliationFailureCode,
    ReconciliationFailureStage,
    ReconciliationTombstoneStatus,
    compute_retry_delay_seconds,
)

NOW = "2026-07-17T16:00:00+08:00"
DUE = "2026-07-17T17:00:00+08:00"
BEFORE_EXPIRY = "2026-07-17T17:00:10+08:00"
AFTER_EXPIRY = "2026-07-17T17:01:00+08:00"


async def _prepared(store: HarnessStore, workspace: Path, request_id: str) -> None:
    await store.prepare_session_delete_reconciliation(
        request_id=request_id,
        workspace_root=workspace,
        session_id=f"session-{request_id}",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )


def test_retry_delay_is_deterministic_bounded_and_increases() -> None:
    delays = [compute_retry_delay_seconds("request-1", attempt) for attempt in range(1, 12)]

    assert delays == [
        compute_retry_delay_seconds("request-1", attempt)
        for attempt in range(1, 12)
    ]
    assert delays[0] >= 5
    assert all(left <= right for left, right in zip(delays, delays[1:]))
    assert delays[-1] <= 3_600
    with pytest.raises(ValueError, match="attempt"):
        compute_retry_delay_seconds("request-1", 0)


@pytest.mark.asyncio
async def test_failure_event_is_idempotent_and_contains_no_raw_error(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    await _prepared(store, workspace, "request-1")

    first = await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage=ReconciliationFailureStage.SESSION_DELETE,
        error_code=ReconciliationFailureCode.SESSION_STORE_ERROR,
        occurred_at=NOW,
        max_attempts=3,
    )
    replay = await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage=ReconciliationFailureStage.SESSION_DELETE,
        error_code=ReconciliationFailureCode.SESSION_STORE_ERROR,
        occurred_at=NOW,
        max_attempts=3,
    )

    assert first == replay
    assert first.policy is LifecyclePolicy.DELETE
    assert first.status is ReconciliationTombstoneStatus.PENDING
    assert first.attempt_count == 1
    assert first.next_retry_at > first.updated_at
    with sqlite3.connect(db_path) as db:
        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(harness_session_reconciliation_tombstones)"
            )
        }
        event_count = db.execute(
            "SELECT COUNT(*) FROM harness_session_reconciliation_failure_events"
        ).fetchone()[0]
    assert "error_message" not in columns
    assert "objective" not in columns
    assert event_count == 1


@pytest.mark.asyncio
async def test_attempt_limit_exhausts_and_prevents_claim(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _prepared(store, workspace, "request-1")
    await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage="session_delete",
        error_code="session_store_error",
        occurred_at=NOW,
        max_attempts=2,
    )
    exhausted = await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-2",
        stage="session_delete",
        error_code="session_store_error",
        occurred_at=DUE,
        max_attempts=2,
    )

    assert exhausted.status is ReconciliationTombstoneStatus.EXHAUSTED
    assert exhausted.attempt_count == 2
    assert await store.claim_due_reconciliation_tombstones(
        worker_id="worker-1",
        now=AFTER_EXPIRY,
        lease_seconds=30,
    ) == ()


@pytest.mark.asyncio
async def test_concurrent_workers_claim_once_and_expired_lease_is_recoverable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    seed = HarnessStore(db_path)
    await _prepared(seed, workspace, "request-1")
    await seed.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage="session_delete",
        error_code="infrastructure_error",
        occurred_at=NOW,
    )

    claims = await asyncio.gather(
        HarnessStore(db_path).claim_due_reconciliation_tombstones(
            worker_id="worker-a",
            now=DUE,
            lease_seconds=30,
        ),
        HarnessStore(db_path).claim_due_reconciliation_tombstones(
            worker_id="worker-b",
            now=DUE,
            lease_seconds=30,
        ),
    )

    assert sum(len(batch) for batch in claims) == 1
    owner = next(batch[0].lease_owner for batch in claims if batch)
    assert await seed.claim_due_reconciliation_tombstones(
        worker_id="early-worker",
        now=BEFORE_EXPIRY,
        lease_seconds=30,
    ) == ()
    recovered = await seed.claim_due_reconciliation_tombstones(
        worker_id="recovery-worker",
        now=AFTER_EXPIRY,
        lease_seconds=30,
    )
    assert owner in {"worker-a", "worker-b"}
    assert recovered[0].lease_owner == "recovery-worker"


@pytest.mark.asyncio
async def test_only_lease_owner_can_resolve_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _prepared(store, workspace, "request-1")
    await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage="harness_records",
        error_code="harness_store_error",
        occurred_at=NOW,
    )
    await store.claim_due_reconciliation_tombstones(
        worker_id="owner",
        now=DUE,
        lease_seconds=30,
    )

    with pytest.raises(HarnessStoreConflictError, match="租约"):
        await store.resolve_reconciliation_tombstone(
            "request-1",
            worker_id="stale-worker",
            resolved_at=BEFORE_EXPIRY,
        )
    resolved = await store.resolve_reconciliation_tombstone(
        "request-1",
        worker_id="owner",
        resolved_at=BEFORE_EXPIRY,
    )
    replay = await store.resolve_reconciliation_tombstone(
        "request-1",
        worker_id="owner",
        resolved_at=AFTER_EXPIRY,
    )

    assert resolved == replay
    assert resolved.status is ReconciliationTombstoneStatus.RESOLVED
    assert await store.claim_due_reconciliation_tombstones(
        worker_id="later-worker",
        now=AFTER_EXPIRY,
        lease_seconds=30,
    ) == ()


@pytest.mark.asyncio
async def test_lease_validity_is_half_open_and_rejects_stale_time(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _prepared(store, workspace, "request-1")
    await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage="harness_records",
        error_code="harness_store_error",
        occurred_at=NOW,
    )
    claimed = await store.claim_due_reconciliation_tombstones(
        worker_id="owner",
        now=DUE,
        lease_seconds=30,
    )
    lease = claimed[0]

    with pytest.raises(HarnessStoreConflictError, match="早于"):
        await store.resolve_reconciliation_tombstone(
            "request-1",
            worker_id="owner",
            resolved_at=NOW,
        )
    with pytest.raises(HarnessStoreConflictError, match="过期"):
        await store.resolve_reconciliation_tombstone(
            "request-1",
            worker_id="owner",
            resolved_at=lease.lease_expires_at,
        )


@pytest.mark.asyncio
async def test_corrupt_tombstone_enum_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    await _prepared(store, workspace, "request-1")
    await store.record_reconciliation_failure(
        request_id="request-1",
        failure_id="failure-1",
        stage="session_delete",
        error_code="cancelled",
        occurred_at=NOW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA ignore_check_constraints = ON")
        db.execute(
            "UPDATE harness_session_reconciliation_tombstones SET status = 'broken'"
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(db_path).get_reconciliation_tombstone("request-1")


@pytest.mark.asyncio
async def test_unknown_failure_code_fails_before_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _prepared(store, workspace, "request-1")

    with pytest.raises(ValueError, match="未知"):
        await store.record_reconciliation_failure(
            request_id="request-1",
            failure_id="failure-1",
            stage="session_delete",
            error_code="raw exception text",
            occurred_at=NOW,
        )
    assert await store.get_reconciliation_tombstone("request-1") is None


@pytest.mark.asyncio
async def test_failure_time_cannot_predate_reconciliation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await store.prepare_session_delete_reconciliation(
        request_id="request-1",
        workspace_root=workspace,
        session_id="session-1",
        actor=LifecycleActor.USER,
        created_at=DUE,
    )

    with pytest.raises(HarnessStoreConflictError, match="早于"):
        await store.record_reconciliation_failure(
            request_id="request-1",
            failure_id="failure-1",
            stage="session_delete",
            error_code="infrastructure_error",
            occurred_at=NOW,
        )


@pytest.mark.asyncio
async def test_v3_database_migrates_additively_and_keeps_reconciliation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    original = HarnessStore(db_path)
    await _prepared(original, workspace, "legacy-request")
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE harness_session_reconciliation_failure_events")
        db.execute("DROP TABLE harness_session_reconciliation_tombstones")
        db.execute("PRAGMA user_version = 3")
        db.commit()

    migrated = HarnessStore(db_path)
    tombstone = await migrated.record_reconciliation_failure(
        request_id="legacy-request",
        failure_id="migrated-failure",
        stage="session_delete",
        error_code="infrastructure_error",
        occurred_at=NOW,
    )

    assert tombstone.request_id == "legacy-request"
    assert await migrated.get_session_delete_reconciliation(
        "legacy-request"
    ) is not None
    with sqlite3.connect(db_path) as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 13
