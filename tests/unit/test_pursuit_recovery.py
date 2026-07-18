from __future__ import annotations

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.pursuit import (
    PursuitEvidence,
    PursuitRun,
    PursuitRunStatus,
)
from naumi_agent.orchestrator.pursuit_checkpoint import (
    CheckpointBudget,
    CheckpointCriterion,
    CheckpointGoal,
    PursuitCheckpoint,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.ui.pursuit_recovery import build_pursuit_recovery_snapshot

T0 = "2026-07-18T00:00:00+00:00"
T4 = "2026-07-18T00:00:04+00:00"


def _run(
    *,
    status: PursuitRunStatus = PursuitRunStatus.RUNNING,
    phase: str = "assess",
) -> PursuitRun:
    return PursuitRun(
        id="pursuit-recovery",
        goal="验证恢复健康",
        status=status,
        phase=phase,
        started_at=1.0,
        updated_at=2.0,
        iteration=1,
        criteria_total=1,
    )


def _checkpoint(*, phase: str = "assess") -> PursuitCheckpoint:
    return PursuitCheckpoint(
        run_id="pursuit-recovery",
        sequence=3,
        created_at=3.0,
        status="running",
        phase=phase,
        iteration=1,
        goal=CheckpointGoal(
            original_goal="验证恢复健康",
            description="组合权威事实",
            criteria=(CheckpointCriterion(
                id="c1",
                description="快照可验证",
                verification_command="pytest -q tests/unit/test_pursuit_recovery.py",
                status="in_progress",
                evidence="",
                last_checked=0.0,
            ),),
            constraints=(),
            estimated_complexity="S",
        ),
        pending_actions=(),
        next_action="继续核对",
        budget=CheckpointBudget(
            tokens_used=0,
            cost_usd=0.0,
            elapsed_seconds=1.0,
            max_iterations=50,
            max_budget_usd=None,
            max_time_seconds=None,
            stagnation_threshold=4,
            verify_interval=2,
            plan_depth=5,
            replan_on_stagnation=False,
        ),
        evidence_cursor=0,
        waiting_on=(),
        pending_interaction=None,
        recent_history=(),
        worktree_name="",
        worktree_path="",
    )


@pytest.mark.asyncio
async def test_active_snapshot_combines_real_heartbeat_lease_and_checkpoint(
    tmp_path,
) -> None:
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    run = _run()
    pursuit_store.save_run(run)
    checkpoint = _checkpoint()
    pursuit_store.save_checkpoint(checkpoint)
    harness_store = HarnessStore(tmp_path / "harness.db")
    lease = await harness_store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=run.id,
        owner_id="worker-a",
        now=T0,
        lease_seconds=30,
    )
    assert lease is not None
    await harness_store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id=run.id,
        instance_id=lease.owner_id,
        epoch=lease.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=T0,
        timeout_seconds=10,
        detail_code="lease_active",
    )

    snapshot = await build_pursuit_recovery_snapshot(
        run,
        pursuit_store,
        harness_store,
        workspace_root=tmp_path,
        now=T4,
    )

    assert snapshot.recovery_state == "active"
    assert snapshot.heartbeat.health == "healthy"
    assert snapshot.heartbeat.instance_id == snapshot.lease.owner_id == "worker-a"
    assert snapshot.heartbeat.epoch == snapshot.lease.epoch == 1
    assert snapshot.checkpoint.status == "ready"
    assert snapshot.checkpoint.checkpoint_id == checkpoint.checkpoint_id()
    assert snapshot.alerts == ()


@pytest.mark.asyncio
async def test_running_without_authorities_is_explicitly_orphaned(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    run = _run()
    store.save_run(run)

    snapshot = await build_pursuit_recovery_snapshot(
        run,
        store,
        None,
        workspace_root=tmp_path,
        now=T4,
    )

    assert snapshot.recovery_state == "orphaned"
    assert snapshot.heartbeat.health == "missing"
    assert snapshot.lease.status == "missing"
    assert snapshot.checkpoint.status == "missing"
    assert any("live lease" in item for item in snapshot.alerts)


@pytest.mark.asyncio
async def test_live_lease_with_mismatched_heartbeat_is_inconsistent(tmp_path) -> None:
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    run = _run()
    pursuit_store.save_run(run)
    harness_store = HarnessStore(tmp_path / "harness.db")
    lease = await harness_store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=run.id,
        owner_id="worker-a",
        now=T0,
        lease_seconds=30,
    )
    assert lease is not None
    await harness_store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id=run.id,
        instance_id="worker-b",
        epoch=lease.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=T0,
        timeout_seconds=10,
        detail_code="lease_active",
    )

    snapshot = await build_pursuit_recovery_snapshot(
        run,
        pursuit_store,
        harness_store,
        workspace_root=tmp_path,
        now=T4,
    )

    assert snapshot.recovery_state == "inconsistent"
    assert snapshot.heartbeat.instance_id == "worker-b"
    assert snapshot.lease.owner_id == "worker-a"
    assert any("owner/epoch 不一致" in item for item in snapshot.alerts)


@pytest.mark.asyncio
async def test_reconcile_boundary_and_reason_have_priority(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    run = _run(status=PursuitRunStatus.BLOCKED, phase="reconcile_required")
    run.evidence = [PursuitEvidence(
        kind="reconcile",
        source="stale_preparing",
        summary="后台 reservation 已陈旧",
        is_hard=False,
        timestamp=4.0,
    )]
    store.save_run(run)
    store.save_checkpoint(_checkpoint(phase="action_inflight"))

    snapshot = await build_pursuit_recovery_snapshot(
        run,
        store,
        None,
        workspace_root=tmp_path,
        now=T4,
    )

    assert snapshot.recovery_state == "reconcile_required"
    assert snapshot.reconcile_required is True
    assert snapshot.reconcile_reason == "stale_preparing"


@pytest.mark.asyncio
async def test_checkpoint_error_is_bounded_and_does_not_leak_exception(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    run = _run(status=PursuitRunStatus.WAITING, phase="waiting")
    store.save_run(run)
    monkeypatch.setattr(
        store,
        "get_checkpoint",
        lambda _: (_ for _ in ()).throw(RuntimeError("private database path")),
    )

    snapshot = await build_pursuit_recovery_snapshot(
        run,
        store,
        None,
        workspace_root=tmp_path,
        now=T4,
    )

    assert snapshot.recovery_state == "waiting"
    assert snapshot.checkpoint.status == "error"
    assert "private database path" not in str(snapshot.model_dump())
    assert any("Checkpoint 校验失败" in item for item in snapshot.alerts)


@pytest.mark.asyncio
async def test_generated_time_requires_timezone(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    run = _run()

    with pytest.raises(ValueError, match="时区偏移"):
        await build_pursuit_recovery_snapshot(
            run,
            store,
            None,
            workspace_root=tmp_path,
            now="2026-07-18T00:00:04",
        )
