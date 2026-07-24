from __future__ import annotations

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
