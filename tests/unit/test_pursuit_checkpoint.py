from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.background.runner import BackgroundRunner
from naumi_agent.background.store import BackgroundTaskStore
from naumi_agent.harness.interaction import new_interaction_record
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.pursuit import (
    CriterionStatus,
    GoalPursuitLoop,
    GoalSpec,
    IterationCheckpoint,
    PursuitConfig,
    PursuitRun,
    PursuitRunStatus,
    SuccessCriterion,
)
from naumi_agent.orchestrator.pursuit_action_ledger import (
    PursuitActionRecord,
    PursuitActionState,
    canonical_action_arguments,
    make_action_key,
)
from naumi_agent.orchestrator.pursuit_checkpoint import (
    CheckpointBudget,
    CheckpointCriterion,
    CheckpointGoal,
    CheckpointInteraction,
    CheckpointInteractionRef,
    CheckpointIteration,
    PursuitCheckpoint,
    checkpoint_safe_text,
)
from naumi_agent.orchestrator.pursuit_store import (
    PursuitStore,
    PursuitStoreConflictError,
    PursuitStoreError,
)
from naumi_agent.user_interaction import normalize_interaction_request


def _checkpoint(*, sequence: int = 1, phase: str = "assess") -> PursuitCheckpoint:
    return PursuitCheckpoint(
        run_id="pursuit_checkpoint",
        sequence=sequence,
        created_at=float(sequence),
        status="running",
        phase=phase,
        iteration=1,
        goal=CheckpointGoal(
            original_goal="完成可恢复执行",
            description="进程重启后能安全恢复",
            criteria=(
                CheckpointCriterion(
                    id="c1",
                    description="checkpoint 可校验",
                    verification_command="pytest -q tests/unit/test_pursuit_checkpoint.py",
                    status="in_progress",
                    evidence="",
                    last_checked=0.0,
                ),
            ),
            constraints=("只运行小模块测试",),
            estimated_complexity="M",
        ),
        pending_actions=("a1: 写入 checkpoint",),
        next_action="执行 a1",
        budget=CheckpointBudget(
            tokens_used=120,
            cost_usd=0.02,
            elapsed_seconds=3.0,
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


def _store_with_run(tmp_path) -> PursuitStore:
    store = PursuitStore(tmp_path / "pursuit")
    store.save_run(PursuitRun(
        id="pursuit_checkpoint",
        goal="完成可恢复执行",
        status=PursuitRunStatus.RUNNING,
        phase="assess",
        started_at=1.0,
        updated_at=1.0,
        iteration=1,
        criteria_total=1,
    ))
    return store


def _action_record(
    *,
    tool_name: str = "background_run",
    command: str = "echo reconcile",
) -> PursuitActionRecord:
    arguments = {"command": command, "cwd": "", "timeout_seconds": 1800}
    _, digest, size = canonical_action_arguments(arguments)
    action_key = make_action_key(
        run_id="pursuit_checkpoint",
        iteration=1,
        action_id="a1",
        tool_name=tool_name,
        arguments_sha256=digest,
    )
    return PursuitActionRecord(
        action_key=action_key,
        run_id="pursuit_checkpoint",
        iteration=1,
        action_id="a1",
        tool_name=tool_name,
        arguments_sha256=digest,
        arguments_size_bytes=size,
        argument_summary=f"tool={tool_name}; arguments_sha256={digest}",
        state=PursuitActionState.PREPARED,
        sequence=1,
        dispatch_token=action_key,
        background_task_id="",
        result_status="",
        result_summary="",
        result_sha256="",
        prepared_at=1.0,
        updated_at=1.0,
    )


def _resume_loop(
    store: PursuitStore,
    *,
    background_source: BackgroundRunner | None = None,
) -> GoalPursuitLoop:
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        background_reconcile_source=background_source,
    )
    loop._assess = AsyncMock(return_value={  # type: ignore[method-assign]
        "checkpoint": IterationCheckpoint(
            iteration=2,
            timestamp=2.0,
            assessment="恢复后重新评估",
            gaps_found=["仍需下一步"],
            actions_planned=[],
            actions_taken=[],
            verification_results=[],
            criteria_status={"c1": "in_progress"},
            convergence_score=0.4,
        ),
        "gaps": ["仍需下一步"],
    })
    loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="reconcile 恢复报告")  # type: ignore[method-assign]
    return loop


def test_checkpoint_round_trips_across_store_reopen(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint()

    store.save_checkpoint(checkpoint)
    reopened = PursuitStore(store.base_dir)
    restored = reopened.get_checkpoint(checkpoint.run_id)

    assert restored == checkpoint
    assert restored is not None
    assert restored.checkpoint_id().startswith("pchk_")
    assert restored.canonical_json() == checkpoint.canonical_json()


def test_checkpoint_reads_pre_4b_payload_without_changing_content_id(tmp_path) -> None:
    payload = _checkpoint().model_dump(mode="json")
    for field_name in (
        "stagnation_threshold",
        "verify_interval",
        "plan_depth",
        "replan_on_stagnation",
    ):
        payload["budget"].pop(field_name)
    legacy_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    digest = hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
    store = _store_with_run(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO pursuit_checkpoints (
                run_id, sequence, schema_version, checkpoint_id,
                payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pursuit_checkpoint",
                1,
                1,
                f"pchk_{digest[:24]}",
                legacy_json,
                digest,
                1.0,
            ),
        )

    restored = store.get_checkpoint("pursuit_checkpoint")

    assert restored is not None
    assert restored.canonical_json() == legacy_json
    assert restored.budget.stagnation_threshold == 3
    assert restored.budget.verify_interval == 1


def test_checkpoint_sequence_is_monotonic_and_idempotent(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    first = _checkpoint()
    store.save_checkpoint(first)
    store.save_checkpoint(first)

    with pytest.raises(PursuitStoreConflictError, match="已绑定不同内容"):
        store.save_checkpoint(_checkpoint(phase="plan"))

    second = _checkpoint(sequence=2, phase="plan")
    store.save_checkpoint(second)
    with pytest.raises(PursuitStoreConflictError, match="序号倒退"):
        store.save_checkpoint(first)
    assert store.get_checkpoint(first.run_id) == second


def test_concurrent_same_sequence_cannot_overwrite_checkpoint(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    store.save_checkpoint(_checkpoint())
    barrier = threading.Barrier(2)

    def write(phase: str) -> str:
        barrier.wait()
        try:
            store.save_checkpoint(_checkpoint(sequence=2, phase=phase))
        except PursuitStoreConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ("plan", "verify")))

    assert sorted(outcomes) == ["conflict", "saved"]
    restored = store.get_checkpoint("pursuit_checkpoint")
    assert restored is not None
    assert restored.phase in {"plan", "verify"}


def test_checkpoint_tampering_fails_closed(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    store.save_checkpoint(_checkpoint())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_checkpoints SET payload_json = payload_json || ' '",
        )

    with pytest.raises(PursuitStoreError, match="摘要校验失败"):
        store.get_checkpoint("pursuit_checkpoint")


def test_checkpoint_metadata_tampering_fails_closed(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    store.save_checkpoint(_checkpoint())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_checkpoints SET checkpoint_id = 'pchk_forged'",
        )

    with pytest.raises(PursuitStoreError, match="ID 校验失败"):
        store.get_checkpoint("pursuit_checkpoint")


def test_checkpoint_rejects_unknown_fields_and_non_finite_budget() -> None:
    payload = _checkpoint().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PursuitCheckpoint.model_validate(payload)

    payload.pop("unexpected")
    payload["budget"]["cost_usd"] = float("nan")
    with pytest.raises(ValidationError, match="finite_number"):
        PursuitCheckpoint.model_validate(payload)


def test_checkpoint_redacts_common_secret_shapes_and_bounds_text() -> None:
    text = checkpoint_safe_text(
        "api_key=top-secret password: hunter2 sk-abcdefghijklmnop tail",
        limit=80,
    )

    assert "top-secret" not in text
    assert "hunter2" not in text
    assert "sk-abcdefghijklmnop" not in text
    assert text.count("[REDACTED]") == 3
    assert len(text) <= 80


def test_get_checkpoint_does_not_create_storage_when_absent(tmp_path) -> None:
    store = PursuitStore(tmp_path / "not-created")

    assert store.get_checkpoint("missing") is None
    assert not store.base_dir.exists()


def test_restore_rebuilds_goal_history_budget_and_cursor(tmp_path) -> None:
    checkpoint = _checkpoint().model_copy(update={
        "recent_history": (
            CheckpointIteration(
                iteration=1,
                timestamp=1.5,
                assessment="完成一半",
                gaps_found=("gap",),
                actions_planned=("a1",),
                actions_taken=("[completed] a1",),
                criteria_status={"c1": "in_progress"},
                convergence_score=0.5,
            ),
        ),
    })
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        config=PursuitConfig(stagnation_threshold=4, verify_interval=2),
    )

    spec = loop._restore_checkpoint_state(checkpoint)

    assert spec.original_goal == "完成可恢复执行"
    assert spec.success_criteria[0].status is CriterionStatus.IN_PROGRESS
    assert spec.constraints == {"只运行小模块测试": True}
    assert loop._history[0].actions_taken == ["[completed] a1"]
    assert loop._history[0].tokens_used == 120
    assert loop._total_tokens == 120
    assert loop._total_cost == 0.02
    assert loop._config.max_iterations == 50
    assert loop._config.max_budget_usd == float("inf")
    assert loop._config.stagnation_threshold == 4
    assert loop._config.verify_interval == 2
    assert loop._config.plan_depth == 5
    assert loop._config.replan_on_stagnation is False
    assert loop._checkpoint_sequence == checkpoint.sequence


@pytest.mark.asyncio
async def test_resume_continues_from_verified_checkpoint_without_reparsing(
    tmp_path,
) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint()
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._parse_goal = AsyncMock()  # type: ignore[method-assign]
    loop._assess = AsyncMock(return_value={  # type: ignore[method-assign]
        "checkpoint": IterationCheckpoint(
            iteration=2,
            timestamp=2.0,
            assessment="仍需继续",
            gaps_found=["gap"],
            actions_planned=[],
            actions_taken=[],
            verification_results=[],
            criteria_status={"c1": "in_progress"},
            convergence_score=0.3,
        ),
        "gaps": ["gap"],
    })
    loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(  # type: ignore[method-assign]
        return_value="恢复执行报告"
    )

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "恢复执行（lease epoch 0）" in result
    assert result.endswith("恢复执行报告")
    loop._parse_goal.assert_not_awaited()
    assert restored is not None
    assert restored.status is PursuitRunStatus.BLOCKED
    assert restored.iteration == 2
    reconciled = store.get_checkpoint(checkpoint.run_id)
    assert reconciled is not None
    assert reconciled.sequence > checkpoint.sequence
    assert reconciled.status == "blocked"
    assert reconciled.iteration == 2
    assert reconciled.goal.original_goal == checkpoint.goal.original_goal
    assert reconciled.budget.tokens_used == checkpoint.budget.tokens_used
    assert reconciled.evidence_cursor == len(restored.evidence)


@pytest.mark.asyncio
async def test_resume_rejects_stale_checkpoint_after_checkpoint_write_error(
    tmp_path,
) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint()
    store.save_checkpoint(checkpoint)
    failed_run = store.get_run(checkpoint.run_id)
    assert failed_run is not None
    failed_run.status = PursuitRunStatus.BLOCKED
    failed_run.phase = "checkpoint_error"
    failed_run.blocked_reason = "checkpoint 2 持久化失败"
    store.save_run(failed_run)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "旧 checkpoint 不足以安全续跑" in result
    assert restored is not None
    assert restored.phase == "checkpoint_error"
    assert restored.blocked_reason == "checkpoint 2 持久化失败"


@pytest.mark.asyncio
async def test_resume_blocks_inflight_action_without_replaying_tools(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_checkpoint")
    assert run is not None
    run.phase = "action_inflight"
    store.save_run(run)
    checkpoint = _checkpoint(phase="action_inflight").model_copy(update={
        "pending_actions": ("a1: 修改文件",),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "安全停在恢复边界" in result
    assert restored is not None
    assert restored.phase == "reconcile_required"
    assert "没有行动账本" in restored.blocked_reason
    loop._assess.assert_not_awaited()
    assert store.get_checkpoint(checkpoint.run_id) == checkpoint


@pytest.mark.asyncio
async def test_resume_abandons_prepared_action_and_continues_from_new_checkpoint(
    tmp_path,
) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_checkpoint")
    assert run is not None
    run.phase = "action_inflight"
    store.save_run(run)
    checkpoint = _checkpoint(phase="action_inflight")
    store.save_checkpoint(checkpoint)
    action = store.prepare_action(_action_record())
    loop = _resume_loop(store)

    result = await loop.resume_persisted(checkpoint.run_id)
    restored_action = store.get_action(action.action_key)
    reconciled = store.get_checkpoint(checkpoint.run_id)

    assert "恢复执行（lease epoch 0）" in result
    assert restored_action is not None
    assert restored_action.state is PursuitActionState.FAILED
    assert restored_action.result_status == "abandoned_before_dispatch"
    assert reconciled is not None
    assert reconciled.sequence > checkpoint.sequence
    assert reconciled.phase != "action_inflight"
    loop._assess.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_reconciles_terminal_background_receipt_and_continues(
    tmp_path,
) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_checkpoint")
    assert run is not None
    run.phase = "action_inflight"
    store.save_run(run)
    checkpoint = _checkpoint(phase="action_inflight")
    store.save_checkpoint(checkpoint)
    action = store.prepare_action(_action_record())
    store.mark_action_dispatched(action.action_key, updated_at=2.0)
    background_store = BackgroundTaskStore(tmp_path / "background")
    background_store.save(BackgroundTask(
        id="bg_0042",
        command="echo reconcile",
        cwd=str(tmp_path),
        status=BackgroundStatus.COMPLETED,
        output_path=str(background_store.artifacts_dir / "bg_0042.log"),
        exit_code=0,
        started_at="2026-07-18T12:00:00",
        completed_at="2026-07-18T12:00:01",
        idempotency_key=action.dispatch_token,
    ))
    background_runner = BackgroundRunner(background_store)
    loop = _resume_loop(store, background_source=background_runner)

    result = await loop.resume_persisted(checkpoint.run_id)
    restored_action = store.get_action(action.action_key)

    assert "恢复执行（lease epoch 0）" in result
    assert restored_action is not None
    assert restored_action.state is PursuitActionState.COMPLETED
    assert restored_action.result_status == "completed"
    loop._assess.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_reconstructs_wait_from_live_background_receipt(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_checkpoint")
    assert run is not None
    run.phase = "action_inflight"
    store.save_run(run)
    checkpoint = _checkpoint(phase="action_inflight")
    store.save_checkpoint(checkpoint)
    command = f'{sys.executable} -c "import time; time.sleep(10)"'
    action = store.prepare_action(_action_record(command=command))
    store.mark_action_dispatched(action.action_key, updated_at=2.0)
    background_store = BackgroundTaskStore(tmp_path / "background")
    background_runner = BackgroundRunner(background_store)
    background_task = await background_runner.run(
        command,
        idempotency_key=action.dispatch_token,
    )
    loop = _resume_loop(store, background_source=background_runner)

    try:
        result = await loop.resume_persisted(checkpoint.run_id)
        restored = store.get_run(checkpoint.run_id)
        reconciled = store.get_checkpoint(checkpoint.run_id)

        assert "仍在等待后台任务" in result or "持久状态已安全检查" in result
        assert restored is not None
        assert restored.status is PursuitRunStatus.WAITING
        assert restored.waiting_on is not None
        assert restored.waiting_on[0].task_id == background_task.id
        assert reconciled is not None
        assert reconciled.sequence == checkpoint.sequence + 1
        assert reconciled.phase == "waiting"
        loop._assess.assert_not_awaited()
    finally:
        await background_runner.shutdown()


@pytest.mark.asyncio
async def test_resume_blocks_stale_preparing_background_reservation(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_checkpoint")
    assert run is not None
    run.phase = "action_inflight"
    store.save_run(run)
    checkpoint = _checkpoint(phase="action_inflight")
    store.save_checkpoint(checkpoint)
    action = store.prepare_action(_action_record())
    store.mark_action_dispatched(action.action_key, updated_at=2.0)
    background_store = BackgroundTaskStore(tmp_path / "background")
    background_store.save(BackgroundTask(
        id="bg_0044",
        command="echo reconcile",
        cwd=str(tmp_path),
        status=BackgroundStatus.PREPARING,
        output_path=str(background_store.artifacts_dir / "bg_0044.log"),
        started_at="2026-01-01T00:00:00",
        idempotency_key=action.dispatch_token,
    ))
    background_runner = BackgroundRunner(background_store)
    loop = _resume_loop(store, background_source=background_runner)

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "安全停在恢复边界" in result
    assert restored is not None
    assert restored.phase == "reconcile_required"
    assert "没有 PID" in restored.blocked_reason
    loop._assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_waiting_for_interaction_consumes_no_model_turn(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint().model_copy(update={
        "pending_interaction": CheckpointInteraction(
            interaction_id="ask-1",
            prompt="选择发布策略",
            options=("安全发布", "暂不发布"),
            allow_custom_input=True,
            created_at=1.0,
        ),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "安全停在恢复边界" in result
    assert restored is not None
    assert restored.phase == "interaction_required"
    loop._assess.assert_not_awaited()


def _durable_interaction_record(
    *,
    interaction_id: str,
    created_at: str,
    timeout_seconds: int | None = None,
):
    request = normalize_interaction_request({
        "header": "目标恢复",
        "question": "请选择恢复策略",
        "options": [
            {"value": "continue", "label": "继续", "description": "继续目标"},
            {"value": "stop", "label": "停止", "description": "停止目标"},
        ],
        "allow_custom": True,
        "custom_label": "其他策略",
        "timeout_seconds": timeout_seconds,
    })
    return new_interaction_record(
        request=request,
        subject_kind="pursuit",
        subject_id="pursuit_checkpoint",
        session_id="session-checkpoint",
        agent_name="main",
        owner_id="bridge-checkpoint",
        created_at=created_at,
        owner_lease_seconds=30,
        timeout_seconds=timeout_seconds,
        interaction_id=interaction_id,
    )


@pytest.mark.asyncio
async def test_resume_pending_durable_interaction_consumes_no_model_turn(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = HarnessStore(tmp_path / "harness.db")
    record = _durable_interaction_record(
        interaction_id="ask-pursuit-pending",
        created_at="2026-07-18T00:00:00+00:00",
    )
    await authority.create_interaction(workspace_root=workspace, record=record)
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint().model_copy(update={
        "pending_interaction": CheckpointInteractionRef(
            interaction_id=record.interaction_id,
        ),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        lease_port=authority,
        workspace_root=workspace,
        interaction_port=authority,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "安全停在恢复边界" in result
    assert restored is not None
    assert restored.phase == "interaction_required"
    assert record.interaction_id in restored.blocked_reason
    loop._assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_expired_durable_interaction_consumes_no_model_turn(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = HarnessStore(tmp_path / "harness.db")
    record = _durable_interaction_record(
        interaction_id="ask-pursuit-expired",
        created_at="2026-07-18T00:00:00+00:00",
        timeout_seconds=3,
    )
    await authority.create_interaction(workspace_root=workspace, record=record)
    expired = await authority.expire_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
        expected_sequence=record.sequence,
        now="2026-07-18T00:00:03+00:00",
    )
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint().model_copy(update={
        "pending_interaction": CheckpointInteractionRef(
            interaction_id=expired.interaction_id,
        ),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        lease_port=authority,
        workspace_root=workspace,
        interaction_port=authority,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "安全停在恢复边界" in result
    assert restored is not None
    assert restored.phase == "interaction_expired"
    assert "已超时" in restored.blocked_reason
    loop._assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_answered_durable_interaction_clears_checkpoint_reference(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = HarnessStore(tmp_path / "harness.db")
    record = _durable_interaction_record(
        interaction_id="ask-pursuit-answered",
        created_at="2026-07-18T00:00:00+00:00",
    )
    await authority.create_interaction(workspace_root=workspace, record=record)
    answered = await authority.answer_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
        expected_sequence=record.sequence,
        owner_id=record.owner_id,
        owner_epoch=record.owner_epoch,
        response={"kind": "option", "value": "continue"},
        answered_by="user",
        now="2026-07-18T00:00:01+00:00",
    )
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint().model_copy(update={
        "pending_interaction": CheckpointInteractionRef(
            interaction_id=answered.interaction_id,
        ),
        "budget": _checkpoint().budget.model_copy(update={
            "max_budget_usd": 0.02,
        }),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        lease_port=authority,
        workspace_root=workspace,
        interaction_port=authority,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="预算终止报告")  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored_run = store.get_run(checkpoint.run_id)
    restored_checkpoint = store.get_checkpoint(checkpoint.run_id)

    assert result.endswith("预算终止报告")
    assert restored_run is not None
    assert any(
        item.kind == "interaction" and item.source == answered.interaction_id
        for item in restored_run.evidence
    )
    assert restored_checkpoint is not None
    assert restored_checkpoint.sequence > checkpoint.sequence
    assert restored_checkpoint.pending_interaction is None
    loop._assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_enforces_cumulative_budget_before_new_assessment(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint().model_copy(update={
        "budget": CheckpointBudget(
            tokens_used=120,
            cost_usd=0.02,
            elapsed_seconds=3.0,
            max_iterations=50,
            max_budget_usd=0.02,
            max_time_seconds=None,
        ),
    })
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._assess = AsyncMock()  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="预算终止报告")  # type: ignore[method-assign]

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "恢复执行（lease epoch 0）" in result
    assert result.endswith("预算终止报告")
    assert restored is not None
    assert restored.status is PursuitRunStatus.BUDGET_EXCEEDED
    loop._assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_persists_inflight_boundary_before_tool_dispatch(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    original_save_checkpoint = store.save_checkpoint
    store.save_checkpoint = MagicMock(wraps=original_save_checkpoint)  # type: ignore[method-assign]
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        config=PursuitConfig(max_iterations=1, verify_interval=99),
    )
    spec = GoalSpec(
        original_goal="执行一个行动",
        description="执行一个行动",
        success_criteria=[SuccessCriterion(
            id="c1",
            description="行动完成",
            verification_command="echo ok",
        )],
        constraints={},
    )
    assessment = IterationCheckpoint(
        iteration=1,
        timestamp=1.0,
        assessment="需要行动",
        gaps_found=["gap"],
        actions_planned=[],
        actions_taken=[],
        verification_results=[],
        criteria_status={"c1": "in_progress"},
        convergence_score=0.2,
    )
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": assessment, "gaps": ["gap"]}
    )
    loop._plan = AsyncMock(return_value=[{  # type: ignore[method-assign]
        "id": "a1",
        "description": "执行行动",
        "tool": "bash_run",
        "expected": "成功",
    }])
    loop._execute_actions = AsyncMock(return_value=[{  # type: ignore[method-assign]
        "action_id": "a1",
        "status": "completed",
        "output": "ok",
    }])
    loop._generate_report = AsyncMock(return_value="报告")  # type: ignore[method-assign]

    assert await loop.pursue("执行一个行动") == "报告"

    phases = [call.args[0].phase for call in store.save_checkpoint.call_args_list]
    assert phases.index("planned") < phases.index("action_inflight")
    assert phases.index("action_inflight") < phases.index("action_result")
    loop._execute_actions.assert_awaited_once()
