from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from naumi_agent.orchestrator.pursuit import (
    GoalPursuitLoop,
    PursuitRun,
    PursuitRunStatus,
)
from naumi_agent.orchestrator.pursuit_checkpoint import (
    CheckpointBudget,
    CheckpointCriterion,
    CheckpointGoal,
    PursuitCheckpoint,
    checkpoint_safe_text,
)
from naumi_agent.orchestrator.pursuit_store import (
    PursuitStore,
    PursuitStoreConflictError,
    PursuitStoreError,
)


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
        ),
        evidence_cursor=2,
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
    ))
    return store


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


@pytest.mark.asyncio
async def test_resume_reports_verified_checkpoint_without_fake_running(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    checkpoint = _checkpoint()
    store.save_checkpoint(checkpoint)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
    )

    result = await loop.resume_persisted(checkpoint.run_id)
    restored = store.get_run(checkpoint.run_id)

    assert "持久状态已安全检查" in result
    assert restored is not None
    assert restored.status is PursuitRunStatus.BLOCKED
    assert restored.phase == "checkpoint_ready"
    reconciled = store.get_checkpoint(checkpoint.run_id)
    assert reconciled is not None
    assert reconciled.sequence == checkpoint.sequence + 1
    assert reconciled.status == "blocked"
    assert reconciled.phase == "checkpoint_ready"
    assert reconciled.evidence_cursor == len(restored.evidence)
    assert reconciled.checkpoint_id() in restored.blocked_reason
    assert "不能伪装成正在运行" in restored.blocked_reason


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
