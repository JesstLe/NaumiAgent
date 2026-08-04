from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.pursuit import PursuitRun, PursuitRunStatus
from naumi_agent.orchestrator.pursuit_checkpoint import (
    CheckpointBudget,
    CheckpointCriterion,
    CheckpointGoal,
    PursuitCheckpoint,
)
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    PursuitRecoveryAttemptState,
    new_recovery_attempt,
)
from naumi_agent.orchestrator.pursuit_recovery_reconcile import (
    PursuitRecoveryReconcileError,
    format_pursuit_reconcile_result,
    new_pursuit_reconciliation_receipt,
    reconcile_pursuit_recovery_attempt,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore, PursuitStoreError
from naumi_agent.orchestrator.pursuit_terminal import (
    PursuitBoundaryFacts,
    decide_pursuit_boundary,
)
from naumi_agent.orchestrator.pursuit_terminal_outbox import (
    PursuitTerminalDispatchState,
    PursuitTerminalOutboxState,
)
from naumi_agent.orchestrator.pursuit_terminal_outbox_worker import (
    PursuitTerminalOutboxWorker,
    PursuitTerminalOutboxWorkerPolicy,
    PursuitTerminalWorkerState,
)

T0 = datetime.fromisoformat("2026-07-20T00:00:00+00:00")


def _checkpoint(
    *,
    sequence: int,
    created_at: float,
    status: str,
    phase: str,
) -> PursuitCheckpoint:
    criterion_status = "verified" if status == "completed" else "in_progress"
    return PursuitCheckpoint(
        run_id="pursuit-reconcile",
        sequence=sequence,
        created_at=created_at,
        status=status,
        phase=phase,
        iteration=2,
        goal=CheckpointGoal(
            original_goal="可靠收口恢复请求",
            description="只按持久机械证据完成对账",
            criteria=(CheckpointCriterion(
                id="c1",
                description="对账回执可验证",
                verification_command=(
                    "pytest -q tests/unit/test_pursuit_recovery_reconcile.py"
                ),
                status=criterion_status,
                evidence="focused test" if status == "completed" else "",
                last_checked=created_at if status == "completed" else 0.0,
            ),),
            constraints=("不能并发覆盖仍存活执行者",),
            estimated_complexity="M",
        ),
        pending_actions=(),
        next_action="保留对账证据",
        budget=CheckpointBudget(
            tokens_used=10,
            cost_usd=0.0,
            elapsed_seconds=10.0,
            max_iterations=50,
            max_budget_usd=None,
            max_time_seconds=None,
        ),
        evidence_cursor=0,
        waiting_on=(),
        pending_interaction=None,
        recent_history=(),
        worktree_name="",
        worktree_path="",
    )


def _admitted_store(tmp_path) -> tuple[PursuitStore, str, float]:
    base = T0.timestamp()
    store = PursuitStore(tmp_path / "pursuit")
    store.save_run(PursuitRun(
        id="pursuit-reconcile",
        goal="可靠收口恢复请求",
        status=PursuitRunStatus.RUNNING,
        phase="resume",
        started_at=base,
        updated_at=base,
        iteration=1,
        criteria_total=1,
    ))
    old_checkpoint = _checkpoint(
        sequence=1,
        created_at=base + 0.5,
        status="running",
        phase="resume",
    )
    store.save_checkpoint(old_checkpoint)
    requested, _ = store.prepare_recovery_attempt(new_recovery_attempt(
        run_id="pursuit-reconcile",
        source_request_id="permission-call-reconcile",
        requested_at=base,
    ))
    store.mark_recovery_attempt_admitted(
        requested.attempt_id,
        admitted_at=base + 1,
        lease_epoch=1,
        checkpoint_id=old_checkpoint.checkpoint_id(),
    )
    return store, requested.attempt_id, base + 1


def _persist_completed_evidence(
    store: PursuitStore,
    *,
    admitted_at: float,
    delay_seconds: float = 10,
) -> None:
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    observed_at = admitted_at + delay_seconds
    store.save_run(PursuitRun(
        id="pursuit-reconcile",
        goal="可靠收口恢复请求",
        status=PursuitRunStatus.COMPLETED,
        phase="complete",
        started_at=T0.timestamp(),
        updated_at=observed_at,
        iteration=2,
        criteria_total=1,
        criteria_verified=1,
        boundary_decision=decision,
    ))
    store.save_checkpoint(_checkpoint(
        sequence=2,
        created_at=observed_at + 0.1,
        status="completed",
        phase="complete",
    ))


def test_store_atomically_reconciles_and_authenticates_receipt(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    receipt = store.reconcile_admitted_recovery_attempt(
        attempt_id,
        reconciled_at=admitted_at + 40,
        minimum_admitted_age_seconds=30,
        fence_epoch=2,
        fence_operation_id="precon-" + "a" * 32,
    )

    attempt = store.get_recovery_attempt(attempt_id)
    assert attempt is not None
    assert attempt.state is PursuitRecoveryAttemptState.RESOLVED
    assert attempt.result_code == "completed_verified"
    assert attempt.boundary_decision_id == receipt.boundary_decision_id
    assert receipt.attempt_after_sha256 == attempt.digest()
    assert receipt.fence_epoch == 2
    assert PursuitStore(store.base_dir).get_recovery_reconciliation(
        attempt_id
    ) == receipt
    with sqlite3.connect(store.db_path) as conn:
        outbox_id = conn.execute(
            "SELECT outbox_id FROM pursuit_terminal_outbox WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
    delivered = store.get_terminal_outbox(outbox_id)
    assert delivered is not None
    assert delivered.state is PursuitTerminalOutboxState.DELIVERED
    assert delivered.terminal_attempt_sha256 == attempt.digest()


def test_terminal_checkpoint_atomically_creates_recoverable_outbox(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)

    _persist_completed_evidence(store, admitted_at=admitted_at)

    pending = store.list_pending_terminal_outbox(limit=1)
    assert len(pending) == 1
    assert pending[0].attempt_id == attempt_id
    assert pending[0].state is PursuitTerminalOutboxState.PENDING
    assert pending[0].boundary_decision_id
    assert pending[0].checkpoint_id == store.get_checkpoint(
        "pursuit-reconcile"
    ).checkpoint_id()
    reopened = PursuitStore(store.base_dir)
    assert reopened.get_terminal_outbox(pending[0].outbox_id) == pending[0]
    dispatch = reopened.get_terminal_outbox_dispatch(pending[0].outbox_id)
    assert dispatch is not None
    assert dispatch.state is PursuitTerminalDispatchState.IDLE
    assert dispatch.next_attempt_at == admitted_at + 30


def test_terminal_dispatch_claim_takeover_release_and_backoff(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    due_at = admitted_at + 30

    assert store.claim_next_terminal_outbox(
        owner_id="worker-a",
        now=due_at - 0.1,
    ) is None
    first = store.claim_next_terminal_outbox(
        owner_id="worker-a",
        now=due_at,
        lease_seconds=10,
    )
    assert first is not None
    assert first.outbox == pending
    assert first.dispatch.state is PursuitTerminalDispatchState.CLAIMED
    assert first.dispatch.claim_epoch == 1
    assert first.dispatch.attempt_count == 1
    assert "worker-a" not in first.dispatch.canonical_json()
    second_store = PursuitStore(store.base_dir)
    assert second_store.claim_next_terminal_outbox(
        owner_id="worker-b",
        now=due_at + 9,
    ) is None
    takeover = second_store.claim_next_terminal_outbox(
        owner_id="worker-b",
        now=due_at + 10,
        lease_seconds=10,
    )
    assert takeover is not None
    assert takeover.dispatch.claim_epoch == 2
    assert takeover.dispatch.attempt_count == 2
    with pytest.raises(PursuitStoreError, match="已失效或不属于"):
        store.release_terminal_outbox_claim(
            pending.outbox_id,
            owner_id="worker-a",
            claim_epoch=1,
            now=due_at + 11,
            retry_delay_seconds=5,
            failure_code="live_lease",
        )
    released = second_store.release_terminal_outbox_claim(
        pending.outbox_id,
        owner_id="worker-b",
        claim_epoch=2,
        now=due_at + 11,
        retry_delay_seconds=5,
        failure_code="live_lease",
    )
    assert released.state is PursuitTerminalDispatchState.IDLE
    assert released.next_attempt_at == due_at + 16
    assert released.last_failure_code == "live_lease"
    assert second_store.claim_next_terminal_outbox(
        owner_id="worker-c",
        now=due_at + 15.9,
    ) is None
    third = second_store.claim_next_terminal_outbox(
        owner_id="worker-c",
        now=due_at + 16,
    )
    assert third is not None
    assert third.dispatch.claim_epoch == 3
    assert third.dispatch.attempt_count == 3


def test_terminal_dispatch_backlog_classifies_durable_states(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    due_at = admitted_at + 30
    assert store.terminal_outbox_backlog(now=due_at - 1).backoff == 1
    assert store.terminal_outbox_backlog(now=due_at).due == 1
    claim = store.claim_next_terminal_outbox(
        owner_id="worker-a",
        now=due_at,
        lease_seconds=10,
    )
    assert claim is not None
    live = store.terminal_outbox_backlog(now=due_at + 5)
    assert live.live_claimed == 1
    expired = store.terminal_outbox_backlog(now=due_at + 10)
    assert expired.expired_claimed == 1


def test_terminal_dispatch_claim_is_single_flight_under_parallel_workers(
    tmp_path,
) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    due_at = admitted_at + 30

    def claim(index: int):
        return PursuitStore(store.base_dir).claim_next_terminal_outbox(
            owner_id=f"parallel-worker-{index}",
            now=due_at,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0].dispatch.claim_epoch == 1
    assert claimed[0].dispatch.attempt_count == 1


def test_tampered_terminal_dispatch_fails_closed(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE pursuit_terminal_outbox_dispatch
            SET payload_sha256 = ? WHERE outbox_id = ?
            """,
            ("f" * 64, pending.outbox_id),
        )

    with pytest.raises(PursuitStoreError, match="dispatch 快照摘要"):
        store.get_terminal_outbox_dispatch(pending.outbox_id)


def test_existing_f1_outbox_backfills_dispatch_on_reopen(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE pursuit_terminal_outbox_dispatch_events")
        conn.execute("DROP TABLE pursuit_terminal_outbox_dispatch")

    reopened = PursuitStore(store.base_dir)
    dispatch = reopened.get_terminal_outbox_dispatch(pending.outbox_id)
    assert dispatch is not None
    assert dispatch.state is PursuitTerminalDispatchState.IDLE
    assert dispatch.pending_outbox_sha256 == pending.digest()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"interval_seconds": float("nan")}, "周期间隔"),
        ({"claim_lease_seconds": True}, "claim 租约"),
        ({"scan_limit": True}, "scan limit"),
        ({"jitter_ratio": float("inf")}, "jitter"),
    ],
)
def test_terminal_worker_policy_rejects_non_finite_or_boolean_numbers(
    override,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        PursuitTerminalOutboxWorkerPolicy(**override)


@pytest.mark.asyncio
async def test_terminal_worker_rejects_naive_clock(tmp_path) -> None:
    worker = PursuitTerminalOutboxWorker(
        store=PursuitStore(tmp_path / "naive-clock-pursuit"),
        authority=HarnessStore(tmp_path / "naive-clock-harness.db"),
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(jitter_ratio=0),
        now=lambda: T0.replace(tzinfo=None),
    )

    with pytest.raises(ValueError, match="时钟必须包含时区"):
        await worker.run_once()


@pytest.mark.asyncio
async def test_terminal_worker_rejects_invalid_random_source(tmp_path) -> None:
    worker = PursuitTerminalOutboxWorker(
        store=PursuitStore(tmp_path / "invalid-random-pursuit"),
        authority=HarnessStore(tmp_path / "invalid-random-harness.db"),
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(jitter_ratio=0.1),
        now=lambda: T0,
        random_value=lambda: float("nan"),
    )

    with pytest.raises(ValueError, match="random source"):
        await worker.run_once()


@pytest.mark.asyncio
async def test_terminal_worker_uses_real_harness_fence_and_delivers(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness-worker.db")
    original = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="crashed-worker",
        now=T0.isoformat(),
        lease_seconds=5,
    )
    assert original is not None and original.epoch == 1
    assessed = T0 + timedelta(seconds=40)
    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            interval_seconds=30,
            max_empty_backoff_seconds=300,
            claim_lease_seconds=60,
            scan_limit=5,
            reconcile_grace_seconds=30,
            retry_base_seconds=5,
            retry_max_seconds=60,
            jitter_ratio=0,
        ),
        owner_id="terminal-worker-a",
        now=lambda: assessed,
    )

    result = await worker.run_once()

    assert result.claimed == 1
    assert result.delivered == 1
    assert result.failures == 0
    assert store.get_recovery_attempt(
        attempt_id
    ).state is PursuitRecoveryAttemptState.RESOLVED
    assert store.list_pending_terminal_outbox() == []
    snapshot = worker.snapshot()
    assert snapshot.state is PursuitTerminalWorkerState.WAITING
    assert snapshot.delivered_count == 1


@pytest.mark.asyncio
async def test_terminal_worker_schedules_backoff_for_live_lease(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness-worker-live.db")
    lease_now = T0 + timedelta(seconds=29)
    live = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="live-worker",
        now=lease_now.isoformat(),
        lease_seconds=60,
    )
    assert live is not None and live.epoch == 1
    assessed = T0 + timedelta(seconds=40)
    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            interval_seconds=30,
            max_empty_backoff_seconds=300,
            claim_lease_seconds=60,
            scan_limit=5,
            reconcile_grace_seconds=30,
            retry_base_seconds=5,
            retry_max_seconds=60,
            jitter_ratio=0,
        ),
        owner_id="terminal-worker-a",
        now=lambda: assessed,
    )

    result = await worker.run_once()

    assert result.claimed == 1
    assert result.delivered == 0
    assert result.retry_scheduled == 1
    assert result.failure_codes == ("live_lease",)
    pending = store.list_pending_terminal_outbox()[0]
    dispatch = store.get_terminal_outbox_dispatch(pending.outbox_id)
    assert dispatch is not None
    assert dispatch.state is PursuitTerminalDispatchState.IDLE
    assert dispatch.next_attempt_at == assessed.timestamp() + 5
    assert dispatch.last_failure_code == "live_lease"


@pytest.mark.asyncio
async def test_terminal_worker_start_stop_is_single_flight(tmp_path) -> None:
    worker = PursuitTerminalOutboxWorker(
        store=PursuitStore(tmp_path / "empty-pursuit"),
        authority=HarnessStore(tmp_path / "empty-harness.db"),
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            interval_seconds=60,
            max_empty_backoff_seconds=120,
            jitter_ratio=0,
        ),
        owner_id="terminal-worker-lifecycle",
        now=lambda: T0,
    )

    assert worker.start() is True
    assert worker.start() is False
    await asyncio.sleep(0)
    assert await worker.stop() is True
    assert worker.snapshot().state is PursuitTerminalWorkerState.STOPPED
    assert await worker.stop() is False


@pytest.mark.asyncio
async def test_terminal_worker_serializes_explicit_and_periodic_passes(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness-worker-single-flight.db")
    await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="crashed-worker",
        now=T0.isoformat(),
        lease_seconds=5,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingAuthority:
        async def get_heartbeat(self, **kwargs):
            entered.set()
            await release.wait()
            return await harness.get_heartbeat(**kwargs)

        def __getattr__(self, name):
            return getattr(harness, name)

    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=BlockingAuthority(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(jitter_ratio=0),
        owner_id="terminal-worker-single-flight",
        now=lambda: T0 + timedelta(seconds=40),
    )

    first = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.delivered == 1
    assert second_result.claimed == 0


@pytest.mark.asyncio
async def test_terminal_worker_empty_backoff_caps_large_pass_count(tmp_path) -> None:
    worker = PursuitTerminalOutboxWorker(
        store=PursuitStore(tmp_path / "empty-backoff-pursuit"),
        authority=HarnessStore(tmp_path / "empty-backoff-harness.db"),
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            interval_seconds=1,
            max_empty_backoff_seconds=300,
            jitter_ratio=0,
        ),
        now=lambda: T0,
    )
    worker._consecutive_empty_passes = 10_000

    result = await worker.run_once()

    assert result.claimed == 0
    assert worker.snapshot().next_delay_seconds == 300


@pytest.mark.asyncio
async def test_terminal_worker_shutdown_drains_inflight_reconcile(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness-worker-drain.db")
    original = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="crashed-worker",
        now=T0.isoformat(),
        lease_seconds=5,
    )
    assert original is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingAuthority:
        async def get_heartbeat(self, **kwargs):
            entered.set()
            await release.wait()
            return await harness.get_heartbeat(**kwargs)

        def __getattr__(self, name):
            return getattr(harness, name)

    assessed = T0 + timedelta(seconds=40)
    worker = PursuitTerminalOutboxWorker(
        store=store,
        authority=BlockingAuthority(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        policy=PursuitTerminalOutboxWorkerPolicy(
            interval_seconds=60,
            max_empty_backoff_seconds=120,
            jitter_ratio=0,
        ),
        owner_id="terminal-worker-drain",
        now=lambda: assessed,
    )

    assert worker.start() is True
    await asyncio.wait_for(entered.wait(), timeout=1)
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    assert await asyncio.wait_for(stopping, timeout=2) is True
    assert worker.snapshot().state is PursuitTerminalWorkerState.STOPPED
    assert store.list_pending_terminal_outbox() == []


def test_inline_attempt_resolution_atomically_delivers_outbox(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    run = store.get_run("pursuit-reconcile")
    assert run is not None and run.boundary_decision is not None

    terminal = store.resolve_recovery_attempt(
        attempt_id,
        resolved_at=admitted_at + 20,
        result_code=run.boundary_decision.code,
        boundary_decision_id=run.boundary_decision.decision_id,
    )

    delivered = store.get_terminal_outbox(pending.outbox_id)
    assert delivered is not None
    assert delivered.state is PursuitTerminalOutboxState.DELIVERED
    assert delivered.terminal_attempt_sha256 == terminal.digest()
    assert store.list_pending_terminal_outbox() == []


def test_outbox_delivery_failure_rolls_back_attempt_resolution(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    run = store.get_run("pursuit-reconcile")
    assert run is not None and run.boundary_decision is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_terminal_outbox_delivery
            BEFORE INSERT ON pursuit_terminal_outbox_events
            WHEN NEW.sequence = 2
            BEGIN
              SELECT RAISE(ABORT, 'injected delivery failure');
            END
            """
        )

    with pytest.raises(PursuitStoreError, match="injected delivery failure"):
        store.resolve_recovery_attempt(
            attempt_id,
            resolved_at=admitted_at + 20,
            result_code=run.boundary_decision.code,
            boundary_decision_id=run.boundary_decision.decision_id,
        )

    attempt = store.get_recovery_attempt(attempt_id)
    assert attempt is not None
    assert attempt.state is PursuitRecoveryAttemptState.ADMITTED
    assert store.get_terminal_outbox(pending.outbox_id) == pending


def test_parallel_terminal_checkpoint_replay_creates_one_outbox(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    observed_at = admitted_at + 10
    store.save_run(PursuitRun(
        id="pursuit-reconcile",
        goal="可靠收口恢复请求",
        status=PursuitRunStatus.COMPLETED,
        phase="complete",
        started_at=T0.timestamp(),
        updated_at=observed_at,
        iteration=2,
        criteria_total=1,
        criteria_verified=1,
        boundary_decision=decision,
    ))
    checkpoint = _checkpoint(
        sequence=2,
        created_at=observed_at + 0.1,
        status="completed",
        phase="complete",
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: store.save_checkpoint(checkpoint), range(16)))

    assert len(store.list_pending_terminal_outbox()) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_terminal_outbox_events"
        ).fetchone()[0] == 1


def test_outbox_insert_failure_rolls_back_terminal_checkpoint(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    old_checkpoint = store.get_checkpoint("pursuit-reconcile")
    assert old_checkpoint is not None
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    observed_at = admitted_at + 10
    store.save_run(PursuitRun(
        id="pursuit-reconcile",
        goal="可靠收口恢复请求",
        status=PursuitRunStatus.COMPLETED,
        phase="complete",
        started_at=T0.timestamp(),
        updated_at=observed_at,
        iteration=2,
        criteria_total=1,
        criteria_verified=1,
        boundary_decision=decision,
    ))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_terminal_outbox
            BEFORE INSERT ON pursuit_terminal_outbox
            BEGIN
              SELECT RAISE(ABORT, 'injected outbox failure');
            END
            """
        )

    with pytest.raises(PursuitStoreError, match="injected outbox failure"):
        store.save_checkpoint(_checkpoint(
            sequence=2,
            created_at=observed_at + 0.1,
            status="completed",
            phase="complete",
        ))

    assert store.get_checkpoint("pursuit-reconcile") == old_checkpoint
    assert store.list_pending_terminal_outbox() == []


def test_terminal_checkpoint_without_matching_boundary_fails_closed(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    old_checkpoint = store.get_checkpoint("pursuit-reconcile")

    with pytest.raises(PursuitStoreError, match="终态不一致"):
        store.save_checkpoint(_checkpoint(
            sequence=2,
            created_at=admitted_at + 10,
            status="completed",
            phase="complete",
        ))

    assert store.get_checkpoint("pursuit-reconcile") == old_checkpoint
    assert store.list_pending_terminal_outbox() == []


def test_multiple_admitted_attempts_block_terminal_outbox_commit(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    old_checkpoint = store.get_checkpoint("pursuit-reconcile")
    assert old_checkpoint is not None
    second, _ = store.prepare_recovery_attempt(new_recovery_attempt(
        run_id="pursuit-reconcile",
        source_request_id="second-concurrent-recovery",
        requested_at=admitted_at + 0.1,
    ))
    store.mark_recovery_attempt_admitted(
        second.attempt_id,
        admitted_at=admitted_at + 0.2,
        lease_epoch=2,
        checkpoint_id=old_checkpoint.checkpoint_id(),
    )
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    observed_at = admitted_at + 10
    store.save_run(PursuitRun(
        id="pursuit-reconcile",
        goal="可靠收口恢复请求",
        status=PursuitRunStatus.COMPLETED,
        phase="complete",
        started_at=T0.timestamp(),
        updated_at=observed_at,
        iteration=2,
        criteria_total=1,
        criteria_verified=1,
        boundary_decision=decision,
    ))

    with pytest.raises(PursuitStoreError, match="多个 admitted"):
        store.save_checkpoint(_checkpoint(
            sequence=2,
            created_at=observed_at + 0.1,
            status="completed",
            phase="complete",
        ))

    assert store.get_checkpoint("pursuit-reconcile") == old_checkpoint
    assert store.list_pending_terminal_outbox() == []


def test_tampered_terminal_outbox_fails_closed(tmp_path) -> None:
    store, _attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    pending = store.list_pending_terminal_outbox()[0]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE pursuit_terminal_outbox
            SET payload_sha256 = ? WHERE outbox_id = ?
            """,
            ("f" * 64, pending.outbox_id),
        )

    with pytest.raises(PursuitStoreError, match="快照摘要"):
        store.get_terminal_outbox(pending.outbox_id)


@pytest.mark.parametrize("limit", [0, 1001, True, 1.0])
def test_terminal_outbox_recovery_catalog_is_strictly_bounded(
    tmp_path,
    limit,
) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    with pytest.raises(ValueError, match="1..1000"):
        store.list_pending_terminal_outbox(limit=limit)


def test_store_rejects_missing_post_admission_evidence_without_mutation(
    tmp_path,
) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)

    with pytest.raises(
        PursuitRecoveryReconcileError,
        match="后置机械裁判",
    ):
        store.reconcile_admitted_recovery_attempt(
            attempt_id,
            reconciled_at=admitted_at + 40,
            minimum_admitted_age_seconds=30,
            fence_epoch=2,
            fence_operation_id="precon-" + "b" * 32,
        )

    attempt = store.get_recovery_attempt(attempt_id)
    assert attempt is not None
    assert attempt.state is PursuitRecoveryAttemptState.ADMITTED
    assert store.get_recovery_reconciliation(attempt_id) is None


def test_store_rejects_terminal_evidence_from_future(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(
        store,
        admitted_at=admitted_at,
        delay_seconds=50,
    )

    with pytest.raises(
        PursuitRecoveryReconcileError,
        match="晚于本次对账",
    ):
        store.reconcile_admitted_recovery_attempt(
            attempt_id,
            reconciled_at=admitted_at + 40,
            minimum_admitted_age_seconds=30,
            fence_epoch=2,
            fence_operation_id="precon-" + "9" * 32,
        )

    assert store.get_recovery_attempt(
        attempt_id
    ).state is PursuitRecoveryAttemptState.ADMITTED


def test_store_reconciliation_is_idempotent_under_parallel_retry(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)

    def reconcile(_: int):
        return store.reconcile_admitted_recovery_attempt(
            attempt_id,
            reconciled_at=admitted_at + 40,
            minimum_admitted_age_seconds=30,
            fence_epoch=2,
            fence_operation_id="precon-" + "c" * 32,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(reconcile, range(16)))

    assert len({item.receipt_id for item in receipts}) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_recovery_reconciliations"
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM pursuit_recovery_attempt_events
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()[0] == 3


def test_tampered_reconciliation_receipt_fails_closed(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    store.reconcile_admitted_recovery_attempt(
        attempt_id,
        reconciled_at=admitted_at + 40,
        minimum_admitted_age_seconds=30,
        fence_epoch=2,
        fence_operation_id="precon-" + "d" * 32,
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE pursuit_recovery_reconciliations
            SET payload_sha256 = ?
            WHERE attempt_id = ?
            """,
            ("f" * 64, attempt_id),
        )

    with pytest.raises(PursuitStoreError, match="回执摘要"):
        store.get_recovery_reconciliation(attempt_id)


def test_self_consistent_forged_receipt_cannot_rebind_admission_history(
    tmp_path,
) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    original = store.reconcile_admitted_recovery_attempt(
        attempt_id,
        reconciled_at=admitted_at + 40,
        minimum_admitted_age_seconds=30,
        fence_epoch=2,
        fence_operation_id="precon-" + "e" * 32,
    )
    facts = original.model_dump(mode="json")
    facts.pop("receipt_id")
    facts["attempt_before_sha256"] = "f" * 64
    forged = new_pursuit_reconciliation_receipt(**facts)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE pursuit_recovery_reconciliations
            SET receipt_id = ?, payload_json = ?, payload_sha256 = ?
            WHERE attempt_id = ?
            """,
            (
                forged.receipt_id,
                forged.canonical_json(),
                forged.digest(),
                attempt_id,
            ),
        )

    with pytest.raises(PursuitStoreError, match="attempt 终态不一致"):
        store.get_recovery_reconciliation(attempt_id)


@pytest.mark.asyncio
async def test_live_heartbeat_blocks_without_claiming_or_mutating(tmp_path) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness.db")
    lease = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="worker-live",
        now=T0.isoformat(),
        lease_seconds=120,
    )
    assert lease is not None
    await harness.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="pursuit-reconcile",
        instance_id=lease.owner_id,
        epoch=lease.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=(T0 + timedelta(seconds=35)).isoformat(),
        timeout_seconds=30,
        detail_code="model_loop",
    )

    result = await reconcile_pursuit_recovery_attempt(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        attempt_id=attempt_id,
        now=(T0 + timedelta(seconds=40)).isoformat(),
        grace_seconds=30,
    )

    assert result.status == "blocked"
    assert result.code == "live_heartbeat"
    assert store.get_recovery_attempt(
        attempt_id
    ).state is PursuitRecoveryAttemptState.ADMITTED
    current = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
    )
    assert current == lease


@pytest.mark.asyncio
async def test_real_harness_fence_reconciles_and_releases_short_lease(
    tmp_path,
) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    _persist_completed_evidence(store, admitted_at=admitted_at)
    harness = HarnessStore(tmp_path / "harness.db")
    original = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="worker-crashed",
        now=T0.isoformat(),
        lease_seconds=5,
    )
    assert original is not None
    await harness.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="pursuit-reconcile",
        instance_id=original.owner_id,
        epoch=original.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=T0.isoformat(),
        timeout_seconds=3,
        detail_code="model_loop",
    )

    result = await reconcile_pursuit_recovery_attempt(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        attempt_id=attempt_id,
        now=(T0 + timedelta(seconds=40)).isoformat(),
        grace_seconds=30,
    )

    assert result.status == "reconciled"
    assert result.code == "reconciled"
    assert result.receipt is not None
    assert result.receipt.fence_epoch == original.epoch + 1
    assert result.warning == ""
    current = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
    )
    assert current is not None
    assert current.epoch == result.receipt.fence_epoch
    assert current.state is HarnessRunLeaseState.RELEASED
    rendered = format_pursuit_reconcile_result(result)
    assert "已使用更高 RunLease epoch" in rendered
    assert result.receipt.receipt_id in rendered

    repeated = await reconcile_pursuit_recovery_attempt(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        attempt_id=attempt_id,
        now=(T0 + timedelta(seconds=41)).isoformat(),
    )
    assert repeated.status == "reconciled"
    assert repeated.code == "already_reconciled"
    assert repeated.receipt == result.receipt


@pytest.mark.asyncio
async def test_retry_after_missing_evidence_uses_new_fence_operation(
    tmp_path,
) -> None:
    store, attempt_id, admitted_at = _admitted_store(tmp_path)
    harness = HarnessStore(tmp_path / "harness.db")
    original = await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
        owner_id="worker-crashed",
        now=T0.isoformat(),
        lease_seconds=5,
    )
    assert original is not None

    missing = await reconcile_pursuit_recovery_attempt(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        attempt_id=attempt_id,
        now=(T0 + timedelta(seconds=40)).isoformat(),
        grace_seconds=30,
    )
    assert missing.status == "blocked"
    assert missing.code == "terminal_evidence_missing"
    first_claim = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
    )
    assert first_claim is not None
    assert first_claim.epoch == 2
    assert first_claim.state is HarnessRunLeaseState.RELEASED

    _persist_completed_evidence(store, admitted_at=admitted_at)
    completed = await reconcile_pursuit_recovery_attempt(
        store=store,
        authority=harness,
        workspace_root=tmp_path,
        attempt_id=attempt_id,
        now=(T0 + timedelta(seconds=41)).isoformat(),
        grace_seconds=30,
    )
    assert completed.status == "reconciled"
    assert completed.receipt is not None
    assert completed.receipt.fence_epoch == 3
    second_claim = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-reconcile",
    )
    assert second_claim is not None
    assert second_claim.epoch == 3
    assert second_claim.state is HarnessRunLeaseState.RELEASED
