"""Real cross-instance tombstone retry lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tombstone import ReconciliationTombstoneStatus

NOW = "2026-07-17T16:00:00+08:00"
DUE = "2026-07-17T17:00:00+08:00"
LATER = "2026-07-17T18:00:00+08:00"


@pytest.mark.asyncio
async def test_tombstone_survives_restart_retry_and_resolution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    first = HarnessStore(db_path)
    await first.prepare_session_delete_reconciliation(
        request_id="real-request",
        workspace_root=workspace,
        session_id="real-session",
        actor=LifecycleActor.USER,
        created_at=NOW,
    )
    await first.record_reconciliation_failure(
        request_id="real-request",
        failure_id="failure-1",
        stage="session_delete",
        error_code="session_store_error",
        occurred_at=NOW,
        max_attempts=3,
    )

    second = HarnessStore(db_path)
    claimed = await second.claim_due_reconciliation_tombstones(
        worker_id="worker-1",
        now=DUE,
        lease_seconds=60,
    )
    assert len(claimed) == 1
    failed_again = await second.record_reconciliation_failure(
        request_id="real-request",
        failure_id="failure-2",
        stage="session_delete",
        error_code="session_store_error",
        occurred_at=DUE,
        max_attempts=3,
        worker_id="worker-1",
    )
    assert failed_again.attempt_count == 2

    third = HarnessStore(db_path)
    claimed_again = await third.claim_due_reconciliation_tombstones(
        worker_id="worker-2",
        now=LATER,
        lease_seconds=60,
    )
    resolved = await third.resolve_reconciliation_tombstone(
        "real-request",
        worker_id="worker-2",
        resolved_at=LATER,
    )

    assert len(claimed_again) == 1
    assert resolved.status is ReconciliationTombstoneStatus.RESOLVED
    restored = await HarnessStore(db_path).get_reconciliation_tombstone(
        "real-request"
    )
    assert restored == resolved
