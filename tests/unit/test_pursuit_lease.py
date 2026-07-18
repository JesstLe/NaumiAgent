"""Pursuit integration tests for HAR-10.1b fenced execution ownership."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.pursuit import (
    CriterionStatus,
    GoalPursuitLoop,
    GoalSpec,
    IterationCheckpoint,
    PursuitBackgroundWait,
    PursuitRun,
    PursuitRunStatus,
    SuccessCriterion,
)
from naumi_agent.orchestrator.pursuit_lease import (
    PursuitLeaseError,
    PursuitLeaseLostError,
    PursuitLeaseSession,
    PursuitLeaseUnavailableError,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.tools.pursuit import PursueTool

T0 = "2026-07-18T00:00:00+00:00"
T4 = "2026-07-18T00:00:04+00:00"


class _Clock:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


@pytest.mark.asyncio
async def test_session_claims_fences_releases_and_preserves_epoch(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    clock = _Clock(T0)
    first = PursuitLeaseSession(
        port=store,
        workspace_root=tmp_path,
        run_id="pursuit-session",
        owner_id="worker-a",
        lease_seconds=30,
        now_provider=clock,
        auto_renew=False,
    )
    second = PursuitLeaseSession(
        port=HarnessStore(store.db_path),
        workspace_root=tmp_path,
        run_id="pursuit-session",
        owner_id="worker-b",
        lease_seconds=30,
        now_provider=clock,
        auto_renew=False,
    )

    lease = await first.acquire()
    receipt = await first.require_current("before-action")
    assert lease.epoch == 1
    assert receipt.accepted is True
    clock.value = T4
    renewed = await first.renew_now()
    assert renewed.epoch == lease.epoch
    assert renewed.updated_at == T4
    with pytest.raises(PursuitLeaseUnavailableError, match="worker-a"):
        await second.acquire()

    assert await first.close() is True
    takeover = await second.acquire()
    assert takeover.epoch == 2
    assert takeover.owner_id == "worker-b"
    assert await second.close() is True


@pytest.mark.asyncio
async def test_session_fails_closed_after_expired_takeover(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    clock = _Clock(T0)
    first = PursuitLeaseSession(
        port=store,
        workspace_root=tmp_path,
        run_id="pursuit-stale",
        owner_id="worker-a",
        lease_seconds=3,
        now_provider=clock,
        auto_renew=False,
    )
    await first.acquire()
    clock.value = T4
    second = PursuitLeaseSession(
        port=HarnessStore(store.db_path),
        workspace_root=tmp_path,
        run_id="pursuit-stale",
        owner_id="worker-b",
        lease_seconds=30,
        now_provider=clock,
        auto_renew=False,
    )
    takeover = await second.acquire()
    assert takeover.epoch == 2

    with pytest.raises(PursuitLeaseLostError, match="fencing 已拒绝"):
        await first.require_current("late-result")
    assert first.is_owned is False
    assert await first.close() is False
    assert await second.close() is True


@pytest.mark.asyncio
async def test_keepalive_renews_exact_epoch_and_stops_on_close(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    clock = _Clock(T0)
    first_sleep = asyncio.Event()
    block = asyncio.Event()
    sleep_calls = 0

    async def controlled_sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            clock.value = T4
            first_sleep.set()
            return
        await block.wait()

    session = PursuitLeaseSession(
        port=store,
        workspace_root=tmp_path,
        run_id="pursuit-keepalive",
        owner_id="worker-a",
        lease_seconds=30,
        renew_interval_seconds=10,
        now_provider=clock,
        sleep_provider=controlled_sleep,
    )
    acquired = await session.acquire()
    await first_sleep.wait()
    renewed = acquired
    for _ in range(20):
        candidate = await store.get_run_lease(
            workspace_root=tmp_path,
            run_kind=HarnessRunKind.PURSUIT,
            run_id="pursuit-keepalive",
        )
        assert candidate is not None
        renewed = candidate
        if renewed.updated_at == T4:
            break
        await asyncio.sleep(0)

    assert renewed.epoch == acquired.epoch
    assert renewed.updated_at == T4
    heartbeat = None
    for _ in range(20):
        heartbeat = await store.get_heartbeat(
            workspace_root=tmp_path,
            subject_kind=HarnessRunKind.PURSUIT,
            subject_id="pursuit-keepalive",
        )
        if heartbeat is not None and heartbeat.sequence == 2:
            break
        await asyncio.sleep(0)
    assert heartbeat is not None
    assert heartbeat.instance_id == "worker-a"
    assert heartbeat.epoch == acquired.epoch
    assert heartbeat.sequence == 2
    assert heartbeat.phase is HarnessHeartbeatPhase.RUNNING
    assert await session.close() is True
    assert session.is_owned is False
    stopped = await store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="pursuit-keepalive",
    )
    assert stopped is not None
    assert stopped.sequence == 3
    assert stopped.phase is HarnessHeartbeatPhase.STOPPED


@pytest.mark.asyncio
async def test_heartbeat_admission_failure_releases_new_lease(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    store.record_heartbeat = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("heartbeat unavailable")
    )
    session = PursuitLeaseSession(
        port=store,
        workspace_root=tmp_path,
        run_id="pursuit-heartbeat-failure",
        owner_id="worker-a",
        lease_seconds=30,
        now_provider=_Clock(T0),
        auto_renew=False,
    )

    with pytest.raises(PursuitLeaseError, match="心跳初始化失败"):
        await session.acquire()

    lease = await HarnessStore(store.db_path).get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-heartbeat-failure",
    )
    assert lease is not None
    assert lease.state is HarnessRunLeaseState.RELEASED
    assert session.is_owned is False


@pytest.mark.asyncio
async def test_full_loop_commits_blocked_terminal_under_fence(tmp_path) -> None:
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    harness_store = HarnessStore(tmp_path / "harness.db")
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=pursuit_store,
        lease_port=harness_store,
        workspace_root=tmp_path,
    )
    spec = _spec()
    spec.original_goal = "需要被 lease 保护的目标"
    spec.description = spec.original_goal
    checkpoint = _checkpoint()
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": checkpoint, "gaps": ["gap"]}
    )
    loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="报告")  # type: ignore[method-assign]

    result = await loop.pursue("需要被 lease 保护的目标")

    assert result == "报告"
    assert loop._run is not None
    assert loop._run.id.startswith("pursuit_")
    assert len(loop._run.id) == len("pursuit_") + 24
    restored = pursuit_store.get_run(loop._run.id)
    assert restored is not None
    assert restored.status is PursuitRunStatus.BLOCKED
    persisted_checkpoint = pursuit_store.get_checkpoint(loop._run.id)
    assert persisted_checkpoint is not None
    assert persisted_checkpoint.status == "blocked"
    assert persisted_checkpoint.goal.original_goal == "需要被 lease 保护的目标"
    assert persisted_checkpoint.goal.criteria[0].id == "c1"
    assert persisted_checkpoint.recent_history[0].iteration == 1
    assert persisted_checkpoint.evidence_cursor == len(restored.evidence)
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=loop._run.id,
    )
    assert lease is not None
    assert lease.state is HarnessRunLeaseState.RELEASED
    assert lease.epoch == 1
    with sqlite3.connect(harness_store.db_path) as db:
        fence_rows = db.execute(
            "SELECT operation_id, decision, reason "
            "FROM harness_run_fence_events WHERE run_id = ?",
            (loop._run.id,),
        ).fetchall()
    assert any("run-start" in row[0] for row in fence_rows)
    assert any("terminal-blocked" in row[0] for row in fence_rows)
    assert all(row[1:] == ("accepted", "current") for row in fence_rows)

    resumed_checkpoint = _checkpoint()
    resumed_checkpoint.iteration = 2
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": resumed_checkpoint, "gaps": ["gap"]}
    )
    resumed = await loop.resume_persisted(loop._run.id)

    assert "恢复执行（lease epoch 2）" in resumed
    assert resumed.endswith("报告")
    assert loop._parse_goal.await_count == 1
    resumed_lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=loop._run.id,
    )
    assert resumed_lease is not None
    assert resumed_lease.state is HarnessRunLeaseState.RELEASED
    assert resumed_lease.epoch == 2


@pytest.mark.asyncio
async def test_resume_refuses_live_owner_without_mutating_run(tmp_path) -> None:
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    harness_store = HarnessStore(tmp_path / "harness.db")
    now = time.time()
    run = PursuitRun(
        id="pursuit-owned",
        goal="等待后台任务",
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=now,
        updated_at=now,
        waiting_on=[
            PursuitBackgroundWait(
                task_id="bg-1",
                action_id="a-1",
                command="echo done",
                created_at=now,
            )
        ],
    )
    pursuit_store.save_run(run)
    baseline = pursuit_store.get_run(run.id)
    assert baseline is not None
    await harness_store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id=run.id,
        owner_id="other-runtime",
        now="2099-01-01T00:00:00+00:00",
        lease_seconds=300,
    )
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=pursuit_store,
        lease_port=harness_store,
        workspace_root=tmp_path,
    )

    result = await loop.resume_persisted(run.id)
    restored = pursuit_store.get_run(run.id)

    assert "暂不能恢复" in result
    assert "other-runtime" in result
    assert restored == baseline


@pytest.mark.asyncio
async def test_failed_final_verification_never_persists_completed(tmp_path) -> None:
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    harness_store = HarnessStore(tmp_path / "harness.db")
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=pursuit_store,
        lease_port=harness_store,
        workspace_root=tmp_path,
    )
    spec = _spec()
    spec.success_criteria[0].status = CriterionStatus.VERIFIED
    spec.success_criteria[0].evidence = "Command output: ok"
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": _checkpoint(), "gaps": []}
    )
    loop._final_verification = AsyncMock(return_value=False)  # type: ignore[method-assign]
    loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="未完成")  # type: ignore[method-assign]

    await loop.pursue("最终验证必须通过")

    assert loop._run is not None
    restored = pursuit_store.get_run(loop._run.id)
    assert restored is not None
    assert restored.status is PursuitRunStatus.BLOCKED
    assert restored.status is not PursuitRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_execution_adapter_does_not_swallow_lease_loss() -> None:
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
    )
    loop._llm_call = AsyncMock(  # type: ignore[method-assign]
        return_value="pytest -q tests/unit/test_demo.py"
    )
    loop._should_run_in_background = MagicMock(return_value=True)  # type: ignore[method-assign]
    loop._start_background_action = AsyncMock(  # type: ignore[method-assign]
        side_effect=PursuitLeaseLostError("epoch changed")
    )

    with pytest.raises(PursuitLeaseLostError, match="epoch changed"):
        await loop._execute_via_bash(MagicMock(), "运行测试", "a1")


@pytest.mark.asyncio
async def test_tool_waits_for_durable_admission_and_reports_store_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import naumi_agent.tools.pursuit as pursuit_module

    class BrokenLeasePort:
        async def acquire_run_lease(self, **kwargs):
            raise OSError("database unavailable")

    pursuit_store = PursuitStore(tmp_path / "pursuit")
    base = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=pursuit_store,
        lease_port=BrokenLeasePort(),
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(pursuit_module, "_global_pursuit_loop", base)

    result = await PursueTool().execute(goal="不能幽灵启动")

    assert "未启动" in result
    assert "OSError" in result
    assert pursuit_store.list_runs() == []


def _spec() -> GoalSpec:
    return GoalSpec(
        original_goal="目标",
        description="目标",
        success_criteria=[
            SuccessCriterion(
                id="c1",
                description="标准",
                verification_command="echo ok",
            )
        ],
        constraints={},
    )


def _checkpoint() -> IterationCheckpoint:
    return IterationCheckpoint(
        iteration=1,
        timestamp=time.time(),
        assessment="尚未完成",
        gaps_found=["gap"],
        actions_planned=[],
        actions_taken=[],
        verification_results=[],
        criteria_status={"c1": "in_progress"},
        convergence_score=0.1,
    )
