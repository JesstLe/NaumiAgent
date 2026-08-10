from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore, PursuitStoreError
from naumi_agent.orchestrator.pursuit_terminal_dead_letter import (
    PursuitTerminalOutboxFailureDisposition,
)
from naumi_agent.orchestrator.pursuit_terminal_outbox_worker import (
    PursuitTerminalFailureClass,
    PursuitTerminalOutboxWorker,
    PursuitTerminalOutboxWorkerPolicy,
    classify_terminal_outbox_failure,
)
from naumi_agent.ui.goal_panel import build_goal_pursuit_snapshot_with_recovery
from tests.unit.test_pursuit_recovery_reconcile import (
    T0,
    _admitted_store,
    _persist_completed_evidence,
)


def _pending_store(tmp_path) -> tuple[PursuitStore, str, float]:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox(limit=1)
    assert len(pending) == 1
    return store, pending[0].outbox_id, admitted_at + 30


def test_retryable_failure_budget_persists_dead_letter_authority(tmp_path) -> None:
    store, outbox_id, due_at = _pending_store(tmp_path)
    first_claim = store.claim_next_terminal_outbox(
        owner_id="dead-letter-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert first_claim is not None
    first, first_dispatch = store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="dead-letter-worker",
        claim_epoch=first_claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="authority_read_failed",
        max_failures=2,
    )
    assert first.disposition is PursuitTerminalOutboxFailureDisposition.RETRYABLE
    assert first.automatic_retry_authority is True
    assert first_dispatch.next_attempt_at == due_at + 6

    second_claim = store.claim_next_terminal_outbox(
        owner_id="dead-letter-worker",
        now=due_at + 6,
        lease_seconds=30,
    )
    assert second_claim is not None
    second, _ = store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="dead-letter-worker",
        claim_epoch=second_claim.dispatch.claim_epoch,
        now=due_at + 7,
        retry_delay_seconds=10,
        failure_code="authority_read_failed",
        max_failures=2,
    )
    assert second.disposition is (
        PursuitTerminalOutboxFailureDisposition.RETRY_EXHAUSTED
    )
    assert second.dead_letter_authority is True
    assert second.previous_failure_sha256 == first.digest()

    reopened = PursuitStore(store.base_dir)
    failures = reopened.list_terminal_outbox_failures(outbox_id)
    assert failures == [first, second]
    backlog = reopened.terminal_outbox_backlog(now=due_at + 7)
    assert backlog.total_pending == 1
    assert backlog.dead_letter == 1
    assert backlog.due == backlog.backoff == 0
    catalog = reopened.terminal_outbox_dead_letter_catalog(limit=20)
    assert catalog.total == 1
    assert catalog.truncated is False
    assert catalog.records == (second,)
    assert reopened.claim_next_terminal_outbox(
        owner_id="another-worker",
        now=due_at + 100,
    ) is None


def test_tampered_failure_event_fails_closed(tmp_path) -> None:
    store, outbox_id, due_at = _pending_store(tmp_path)
    claim = store.claim_next_terminal_outbox(
        owner_id="tamper-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert claim is not None
    store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="tamper-worker",
        claim_epoch=claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="authority_read_failed",
        max_failures=2,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_failures "
            "SET payload_json = replace(payload_json, 'authority_read_failed', "
            "'authority_mutation_failed') WHERE outbox_id = ?",
            (outbox_id,),
        )

    with pytest.raises(PursuitStoreError, match="摘要校验失败"):
        PursuitStore(store.base_dir).list_terminal_outbox_failures(outbox_id)


def test_deleted_failure_tail_remains_unclaimable_and_fails_closed(tmp_path) -> None:
    store, outbox_id, due_at = _pending_store(tmp_path)
    claim = store.claim_next_terminal_outbox(
        owner_id="delete-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert claim is not None
    store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="delete-worker",
        claim_epoch=claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="lease_missing",
        max_failures=8,
        permanent=True,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM pursuit_terminal_outbox_failures WHERE outbox_id = ?",
            (outbox_id,),
        )

    reopened = PursuitStore(store.base_dir)
    with pytest.raises(PursuitStoreError, match="head 缺少事件链"):
        reopened.list_terminal_outbox_failures(outbox_id)
    assert reopened.claim_next_terminal_outbox(
        owner_id="must-not-reclaim",
        now=due_at + 100,
    ) is None


def test_tampered_dead_letter_head_cannot_reenable_claim(tmp_path) -> None:
    store, outbox_id, due_at = _pending_store(tmp_path)
    claim = store.claim_next_terminal_outbox(
        owner_id="head-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert claim is not None
    store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="head-worker",
        claim_epoch=claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="lease_missing",
        max_failures=8,
        permanent=True,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_failure_heads "
            "SET dead_letter = 0 WHERE outbox_id = ?",
            (outbox_id,),
        )

    with pytest.raises(PursuitStoreError, match="head 与事件链末端不一致"):
        PursuitStore(store.base_dir).claim_next_terminal_outbox(
            owner_id="must-not-reclaim",
            now=due_at + 100,
        )
    with pytest.raises(PursuitStoreError, match="head 与事件链末端不一致"):
        PursuitStore(store.base_dir).terminal_outbox_dead_letter_catalog()


def test_dead_letter_catalog_returns_authenticated_bounded_records(tmp_path) -> None:
    store, outbox_id, due_at = _pending_store(tmp_path)
    claim = store.claim_next_terminal_outbox(
        owner_id="catalog-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert claim is not None
    event, _ = store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="catalog-worker",
        claim_epoch=claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="lease_missing",
        max_failures=8,
        permanent=True,
    )

    catalog = store.terminal_outbox_dead_letter_catalog(limit=1, scan_limit=1)

    assert catalog.records == (event,)
    assert catalog.total == 1
    assert catalog.truncated is False
    with pytest.raises(ValueError, match="catalog 策略无效"):
        store.terminal_outbox_dead_letter_catalog(limit=2, scan_limit=1)


@pytest.mark.asyncio
async def test_safe_wait_does_not_consume_failure_budget(tmp_path) -> None:
    store, outbox_id, _due_at = _pending_store(tmp_path)
    harness = HarnessStore(tmp_path / "live-harness.db")
    live_at = T0 + timedelta(seconds=29)
    lease = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="live-worker",
        now=live_at.isoformat(),
        lease_seconds=60,
    )
    assert lease is not None
    assessed = T0 + timedelta(seconds=40)
    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            max_attempts=1,
            retry_base_seconds=5,
            retry_max_seconds=5,
            jitter_ratio=0,
        ),
        owner_id="safe-wait-worker",
        now=lambda: assessed,
    )

    result = await worker.run_once()

    assert result.retry_scheduled == 1
    assert result.dead_lettered == 0
    assert result.failures == 0
    assert result.failure_codes == ("live_lease",)
    assert store.list_terminal_outbox_failures(outbox_id) == []
    assert store.terminal_outbox_backlog(now=assessed.timestamp()).dead_letter == 0


@pytest.mark.asyncio
async def test_permanent_invariant_enters_dead_letter_immediately(tmp_path) -> None:
    store, outbox_id, _due_at = _pending_store(tmp_path)
    assessed = T0 + timedelta(seconds=40)
    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=HarnessStore(tmp_path / "missing-lease-harness.db"),
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            max_attempts=8,
            jitter_ratio=0,
        ),
        owner_id="permanent-worker",
        now=lambda: assessed,
    )

    result = await worker.run_once()

    assert result.claimed == 1
    assert result.retry_scheduled == 0
    assert result.dead_lettered == 1
    assert result.failures == 1
    failures = store.list_terminal_outbox_failures(outbox_id)
    assert len(failures) == 1
    assert failures[0].failure_code == "lease_missing"
    assert failures[0].disposition is (
        PursuitTerminalOutboxFailureDisposition.PERMANENT
    )
    assert store.terminal_outbox_backlog(now=assessed.timestamp()).dead_letter == 1
    projection = (
        await build_goal_pursuit_snapshot_with_recovery(
            GoalStore(tmp_path / "goals"),
            store,
            None,
            workspace_root=tmp_path,
            terminal_outbox_enabled=True,
            terminal_outbox_worker_snapshot=worker.snapshot,
            assessed_at=assessed.isoformat(),
        )
    ).terminal_outbox
    assert projection is not None
    assert projection.schema_version == 3
    assert projection.dead_letters[0].dead_letter_id == failures[0].event_id
    assert projection.dead_letters[0].failure_code == "lease_missing"
    assert "outbox_id" not in projection.model_dump(mode="json")["dead_letters"][0]


def test_failure_classification_separates_waits_transients_and_invariants() -> None:
    assert classify_terminal_outbox_failure("live_heartbeat") is (
        PursuitTerminalFailureClass.SAFE_WAIT
    )
    assert classify_terminal_outbox_failure("authority_read_failed") is (
        PursuitTerminalFailureClass.RETRYABLE
    )
    assert classify_terminal_outbox_failure("lease_missing") is (
        PursuitTerminalFailureClass.PERMANENT
    )
    assert classify_terminal_outbox_failure("future_unknown_code") is (
        PursuitTerminalFailureClass.PERMANENT
    )
