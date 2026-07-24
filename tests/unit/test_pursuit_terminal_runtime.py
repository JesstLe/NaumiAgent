from __future__ import annotations

import hashlib
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.orchestrator.pursuit import (
    CriterionStatus,
    GoalPursuitLoop,
    GoalSpec,
    IterationCheckpoint,
    PursuitBackgroundWait,
    PursuitConfig,
    PursuitRun,
    PursuitRunStatus,
    SuccessCriterion,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore, PursuitStoreError
from naumi_agent.orchestrator.pursuit_terminal import (
    PursuitBoundaryFacts,
    decide_pursuit_boundary,
)


def _spec() -> GoalSpec:
    return GoalSpec(
        original_goal="完成一个可验证目标",
        description="完成一个可验证目标",
        success_criteria=[
            SuccessCriterion(
                id="c1",
                description="目标文件通过定向验证",
                verification_command="pytest tests/unit/test_target.py -q",
            )
        ],
        constraints={},
    )


def _checkpoint(*, convergence: float = 0.5) -> IterationCheckpoint:
    return IterationCheckpoint(
        iteration=1,
        timestamp=time.time(),
        assessment="仍需处理",
        gaps_found=["尚未完成"],
        actions_planned=[],
        actions_taken=[],
        verification_results=[],
        criteria_status={"c1": "in_progress"},
        convergence_score=convergence,
    )


def _loop(*, config: PursuitConfig | None = None) -> GoalPursuitLoop:
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        config=config,
    )
    loop._ensure_worktree_for_code_goal = AsyncMock()  # type: ignore[method-assign]
    loop._generate_report = AsyncMock(return_value="机械终态报告")  # type: ignore[method-assign]
    return loop


def _decision_evidence(loop: GoalPursuitLoop):
    assert loop._run is not None
    return [
        item for item in loop._run.evidence or []
        if item.kind == "boundary_decision"
    ]


def test_boundary_decision_survives_restart_and_rejects_store_tampering(
    tmp_path,
) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    now = time.time()
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=1,
        hard_evidence_count=1,
        final_verification="passed",
    ))
    run = PursuitRun(
        id="pursuit_terminal_store",
        goal="保存终态裁判",
        status=PursuitRunStatus.COMPLETED,
        phase="completed",
        started_at=now,
        updated_at=now,
        criteria_total=1,
        criteria_verified=1,
        boundary_decision=decision,
    )

    store.save_run(run)
    restored = PursuitStore(tmp_path / "pursuit").get_run(run.id)

    assert restored is not None
    assert restored.boundary_decision == decision
    assert store.list_boundary_decisions(run.id) == [decision]

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            UPDATE pursuit_boundary_decisions
            SET payload_json = ?
            WHERE run_id = ? AND decision_id = ?
            """,
            ("{}", run.id, decision.decision_id),
        )

    with pytest.raises(PursuitStoreError, match="digest"):
        PursuitStore(tmp_path / "pursuit").get_run(run.id)


def test_current_boundary_pointer_handles_repeated_decision_identity(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    now = time.time()
    running = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=0,
        hard_evidence_count=0,
    ))
    waiting = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=0,
        hard_evidence_count=0,
        waiting_kind="background",
        waiting_count=1,
    ))
    run = PursuitRun(
        id="pursuit_repeated_boundary",
        goal="重复经过同一机械边界",
        status=PursuitRunStatus.RUNNING,
        phase="assess",
        started_at=now,
        updated_at=now,
        criteria_total=1,
        boundary_decision=running,
    )
    store.save_run(run)
    run.status = PursuitRunStatus.WAITING
    run.phase = "waiting"
    run.boundary_decision = waiting
    run.updated_at = now + 1
    store.save_run(run)
    run.status = PursuitRunStatus.RUNNING
    run.phase = "assess"
    run.boundary_decision = running
    run.updated_at = now + 2
    store.save_run(run)

    restored = PursuitStore(tmp_path / "pursuit").get_run(run.id)

    assert restored is not None
    assert restored.status is PursuitRunStatus.RUNNING
    assert restored.boundary_decision == running
    assert set(store.list_boundary_decisions(run.id)) == {running, waiting}


def test_store_migrates_legacy_run_and_backfills_boundary_pointer(tmp_path) -> None:
    base_dir = tmp_path / "pursuit"
    base_dir.mkdir()
    db_path = base_dir / "pursuit.db"
    now = time.time()
    decision = decide_pursuit_boundary(PursuitBoundaryFacts(
        criterion_count=1,
        verified_count=0,
        hard_evidence_count=0,
        blocker="planner_empty",
    ))
    payload = decision.model_dump_json()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE pursuit_runs (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                iteration INTEGER NOT NULL DEFAULT 0,
                criteria_total INTEGER NOT NULL DEFAULT 0,
                criteria_verified INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                blocked_reason TEXT NOT NULL DEFAULT '',
                next_action TEXT NOT NULL DEFAULT '',
                worktree_name TEXT NOT NULL DEFAULT '',
                worktree_path TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE pursuit_boundary_decisions (
                run_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                PRIMARY KEY(run_id, decision_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pursuit_runs (
                id, goal, status, phase, started_at, updated_at,
                criteria_total, blocked_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pursuit_legacy_boundary",
                "迁移旧边界记录",
                "blocked",
                "blocked",
                now,
                now,
                1,
                decision.reason,
            ),
        )
        connection.execute(
            """
            INSERT INTO pursuit_boundary_decisions (
                run_id, decision_id, payload_json, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "pursuit_legacy_boundary",
                decision.decision_id,
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                now,
            ),
        )

    restored = PursuitStore(base_dir).get_run("pursuit_legacy_boundary")

    assert restored is not None
    assert restored.boundary_decision == decision
    with sqlite3.connect(db_path) as connection:
        pointer = connection.execute(
            "SELECT boundary_decision_id FROM pursuit_runs WHERE id = ?",
            (restored.id,),
        ).fetchone()
    assert pointer == (decision.decision_id,)


@pytest.mark.asyncio
async def test_real_interaction_wait_uses_same_boundary_authority() -> None:
    loop = _loop()
    now = time.time()
    loop._run = PursuitRun(
        id="pursuit_interaction",
        goal="等待选择",
        status=PursuitRunStatus.RUNNING,
        phase="action_inflight",
        started_at=now,
        updated_at=now,
        criteria_total=1,
    )

    await loop._begin_user_interaction("ask-interaction-01", {})

    assert loop._run.status is PursuitRunStatus.WAITING
    assert loop._run.phase == "interaction_required"
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == "waiting_for_interaction"
    assert _decision_evidence(loop)[0].source == loop._last_stop_decision.decision_id


@pytest.mark.asyncio
async def test_interaction_answer_replaces_stale_waiting_decision() -> None:
    loop = _loop()
    now = time.time()
    loop._run = PursuitRun(
        id="pursuit_interaction_answer",
        goal="等待选择",
        status=PursuitRunStatus.RUNNING,
        phase="action_inflight",
        started_at=now,
        updated_at=now,
        criteria_total=1,
    )

    await loop._begin_user_interaction("ask-interaction-02", {})
    waiting_id = loop._run.boundary_decision.decision_id
    await loop._resolve_user_interaction(
        "ask-interaction-02",
        {"label": "继续"},
    )

    assert loop._run.status is PursuitRunStatus.RUNNING
    assert loop._run.phase == "action_inflight"
    assert loop._run.boundary_decision is not None
    assert loop._run.boundary_decision.code == "criteria_incomplete"
    assert loop._run.boundary_decision.decision_id != waiting_id


@pytest.mark.asyncio
async def test_real_loop_blocks_when_goal_has_no_success_criteria() -> None:
    loop = _loop()
    spec = _spec()
    spec.success_criteria = []
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]

    await loop.pursue(spec.original_goal)

    assert loop._run is not None
    assert loop._run.status is PursuitRunStatus.BLOCKED
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == "no_success_criteria"


@pytest.mark.asyncio
async def test_real_loop_completes_only_after_final_verification() -> None:
    loop = _loop()
    spec = _spec()

    async def assess(_spec: GoalSpec):
        _spec.success_criteria[0].status = CriterionStatus.VERIFIED
        _spec.success_criteria[0].evidence = "Command output: 1 passed"
        return {"checkpoint": _checkpoint(convergence=1.0), "gaps": []}

    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(side_effect=assess)  # type: ignore[method-assign]
    loop._final_verification = AsyncMock(return_value=True)  # type: ignore[method-assign]

    report = await loop.pursue(spec.original_goal)

    assert report == "机械终态报告"
    assert loop._run is not None
    assert loop._run.status is PursuitRunStatus.COMPLETED
    assert loop._run.phase == "completed"
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == "completed_verified"
    evidence = _decision_evidence(loop)
    assert len(evidence) == 1
    assert evidence[0].source == loop._last_stop_decision.decision_id
    loop._final_verification.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_loop_blocks_on_empty_plan_with_auditable_decision() -> None:
    loop = _loop(config=PursuitConfig(max_iterations=3))
    spec = _spec()
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": _checkpoint(), "gaps": ["尚未完成"]}
    )
    loop._plan = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await loop.pursue(spec.original_goal)

    assert loop._run is not None
    assert loop._run.status is PursuitRunStatus.BLOCKED
    assert loop._run.phase == "blocked"
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == "planner_empty"
    assert _decision_evidence(loop)[0].is_hard is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "code"),
    [
        (PursuitConfig(max_time_seconds=-1), "time_budget_exceeded"),
        (PursuitConfig(max_budget_usd=0), "cost_budget_exceeded"),
        (PursuitConfig(max_iterations=0), "iteration_budget_exceeded"),
    ],
)
async def test_real_loop_uses_one_authority_for_all_budget_boundaries(
    config: PursuitConfig,
    code: str,
) -> None:
    loop = _loop(config=config)
    spec = _spec()
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]

    await loop.pursue(spec.original_goal)

    assert loop._run is not None
    assert loop._run.status is PursuitRunStatus.BUDGET_EXCEEDED
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == code
    assert _decision_evidence(loop)[0].source == loop._last_stop_decision.decision_id


@pytest.mark.asyncio
async def test_real_loop_cancellation_precedes_planning_and_is_audited() -> None:
    loop = _loop()
    spec = _spec()

    async def parse(_goal: str) -> GoalSpec:
        loop.cancel()
        return spec

    loop._parse_goal = AsyncMock(side_effect=parse)  # type: ignore[method-assign]

    await loop.pursue(spec.original_goal)

    assert loop._run is not None
    assert loop._run.status is PursuitRunStatus.CANCELLED
    assert loop._last_stop_decision is not None
    assert loop._last_stop_decision.code == "user_cancelled"
    assert _decision_evidence(loop)[0].source == loop._last_stop_decision.decision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("with_authority", [True, False])
async def test_real_loop_waiting_requires_durable_background_authority(
    with_authority: bool,
) -> None:
    loop = _loop()
    spec = _spec()
    loop._parse_goal = AsyncMock(return_value=spec)  # type: ignore[method-assign]
    loop._assess = AsyncMock(  # type: ignore[method-assign]
        return_value={"checkpoint": _checkpoint(), "gaps": ["等待后台任务"]}
    )
    loop._plan = AsyncMock(return_value=[{  # type: ignore[method-assign]
        "id": "a1",
        "description": "运行定向后台验证",
        "tool": "bash_run",
        "arguments": {"command": "pytest tests/unit/test_target.py -q"},
    }])

    async def execute(_spec: GoalSpec, _actions: list[dict[str, object]]):
        if with_authority:
            loop._pending_background = [
                PursuitBackgroundWait(
                    task_id="bg_0001",
                    action_id="a1",
                    command="pytest tests/unit/test_target.py -q",
                    created_at=time.time(),
                )
            ]
        return [{"status": "waiting", "action_id": "a1", "output": "后台运行中"}]

    loop._execute_actions = AsyncMock(side_effect=execute)  # type: ignore[method-assign]

    await loop.pursue(spec.original_goal)

    assert loop._run is not None
    assert loop._last_stop_decision is not None
    if with_authority:
        assert loop._run.status is PursuitRunStatus.WAITING
        assert loop._last_stop_decision.code == "waiting_for_background"
        assert loop._run.waiting_on[0].task_id == "bg_0001"
    else:
        assert loop._run.status is PursuitRunStatus.BLOCKED
        assert loop._last_stop_decision.code == "waiting_without_authority"
    assert len(_decision_evidence(loop)) == 1
