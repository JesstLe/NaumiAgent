"""Tests for the typed read-only Goal/Pursuit UI projection."""

from __future__ import annotations

import time

import pytest

from naumi_agent.harness.interaction import new_interaction_record
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.goal_store import GoalStatus, GoalStore
from naumi_agent.orchestrator.pursuit import (
    PursuitBackgroundWait,
    PursuitEvidence,
    PursuitRun,
    PursuitRunStatus,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.ui.goal_panel import (
    build_goal_pursuit_snapshot,
    build_goal_pursuit_snapshot_with_recovery,
    render_goal_pursuit_snapshot,
)
from naumi_agent.user_interaction import normalize_interaction_request


def test_empty_snapshot_does_not_create_missing_databases(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")

    snapshot = build_goal_pursuit_snapshot(goal_store, pursuit_store)

    assert snapshot.current_goal_id == ""
    assert snapshot.goals == ()
    assert snapshot.warnings == ()
    assert not goal_store.base_dir.exists()
    assert not pursuit_store.base_dir.exists()


def test_snapshot_preserves_stable_link_and_bounds_public_details(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    goal = goal_store.create("可视化长期目标\x1b[31m")
    now = time.time()
    run = PursuitRun(
        id="pursuit_visual",
        goal=goal.objective,
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=now,
        updated_at=now,
        iteration=7,
        criteria_total=4,
        criteria_verified=2,
        next_action="等待用户选择",
        waiting_on=[
            PursuitBackgroundWait(
                task_id=f"bg_{index}",
                action_id=f"action_{index}",
                command="echo secret\x00",
                created_at=now,
            )
            for index in range(25)
        ],
        evidence=[
            PursuitEvidence(
                kind="test",
                source=f"case:{index}",
                summary="验证证据\x1b[32m",
                is_hard=index % 2 == 0,
                timestamp=now,
            )
            for index in range(25)
        ],
    )
    pursuit_store.save_run(run)
    goal_store.attach_pursuit(goal.id, run.id)

    payload = build_goal_pursuit_snapshot(
        goal_store,
        pursuit_store,
    ).to_protocol_dict()

    assert payload["schema_version"] == 1
    assert payload["current_goal_id"] == goal.id
    assert len(payload["goals"]) == 1
    item = payload["goals"][0]
    assert item["pursuit_run_id"] == run.id
    assert item["pursuit_link_status"] == "ready"
    assert item["pursuit"]["run_id"] == run.id
    assert item["pursuit"]["status"] == "waiting"
    assert len(item["pursuit"]["waits"]) == 20
    assert len(item["pursuit"]["evidence"]) == 20
    assert item["pursuit"]["evidence"][0]["source"] == "case:5"
    assert "\x1b" not in str(payload)
    assert "private" not in str(payload)


@pytest.mark.asyncio
async def test_recovery_projection_is_shared_by_typed_and_text_views(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    goal = goal_store.create("展示恢复健康")
    run = PursuitRun(
        id="pursuit_recovery_view",
        goal=goal.objective,
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=1.0,
        updated_at=2.0,
    )
    pursuit_store.save_run(run)
    goal_store.attach_pursuit(goal.id, run.id)

    snapshot = await build_goal_pursuit_snapshot_with_recovery(
        goal_store,
        pursuit_store,
        None,
        workspace_root=tmp_path,
    )
    recovery = snapshot.goals[0]["pursuit"]["recovery"]

    assert recovery["run_id"] == run.id
    assert recovery["recovery_state"] == "waiting"
    assert recovery["heartbeat"]["health"] == "missing"
    rendered = render_goal_pursuit_snapshot(snapshot)
    assert "恢复健康：安全等待" in rendered
    assert "心跳 缺失" in rendered


@pytest.mark.asyncio
async def test_goal_snapshot_projects_only_linked_interaction_public_state(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    harness_store = HarnessStore(tmp_path / "harness.db")
    goal = goal_store.create("展示等待用户的问题")
    run = PursuitRun(
        id="pursuit_interaction_view",
        goal=goal.objective,
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=1.0,
        updated_at=2.0,
    )
    pursuit_store.save_run(run)
    goal_store.attach_pursuit(goal.id, run.id)
    request = normalize_interaction_request({
        "header": "继续方式",
        "question": "是否继续执行？",
        "options": [
            {"value": "yes", "label": "继续"},
            {"value": "no", "label": "停止"},
        ],
    })
    linked = new_interaction_record(
        request=request,
        subject_kind="pursuit",
        subject_id=run.id,
        session_id="session-1",
        agent_name="main",
        owner_id="bridge-a",
        created_at="2026-07-18T00:00:00+00:00",
        owner_lease_seconds=30,
        interaction_id="ask-goal-linked",
    )
    unrelated = new_interaction_record(
        request=request,
        subject_kind="pursuit",
        subject_id="pursuit-unrelated",
        session_id="session-2",
        agent_name="main",
        owner_id="bridge-b",
        created_at="2026-07-18T00:00:00+00:00",
        owner_lease_seconds=30,
        interaction_id="ask-goal-unrelated",
    )
    await harness_store.create_interaction(workspace_root=tmp_path, record=linked)
    await harness_store.create_interaction(workspace_root=tmp_path, record=unrelated)

    snapshot = await build_goal_pursuit_snapshot_with_recovery(
        goal_store,
        pursuit_store,
        harness_store,
        workspace_root=tmp_path,
    )
    payload = snapshot.to_protocol_dict()

    assert payload["interactions"] == [{
        "interaction_id": "ask-goal-linked",
        "pursuit_run_id": run.id,
        "state": "pending",
        "sequence": 1,
        "header": "继续方式",
        "question": "是否继续执行？",
        "created_at": "2026-07-18T00:00:00+00:00",
        "expires_at": "",
        "updated_at": "2026-07-18T00:00:00+00:00",
        "can_cancel": True,
    }]
    assert "owner_id" not in str(payload["interactions"])
    rendered = render_goal_pursuit_snapshot(snapshot)
    assert "ask-goal-linked" in rendered
    assert "/goal interaction cancel ask-goal-linked" in rendered


def test_snapshot_exposes_missing_link_without_creating_pursuit_db(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    goal = goal_store.create("恢复缺失追踪记录")
    goal_store.attach_pursuit(goal.id, "pursuit_missing")

    snapshot = build_goal_pursuit_snapshot(goal_store, pursuit_store)
    item = snapshot.goals[0]

    assert item["pursuit_link_status"] == "missing"
    assert item["pursuit"] is None
    assert "追踪记录 pursuit_missing 不可用" in snapshot.warnings[0]
    assert not pursuit_store.base_dir.exists()
    assert "追踪记录不可用" in render_goal_pursuit_snapshot(snapshot)


@pytest.mark.parametrize(
    "status",
    [GoalStatus.ACTIVE, GoalStatus.PAUSED, GoalStatus.BLOCKED],
)
def test_snapshot_preserves_each_open_goal_status(tmp_path, status: GoalStatus) -> None:
    goal_store = GoalStore(tmp_path / status.value / "goals")
    pursuit_store = PursuitStore(tmp_path / status.value / "pursuit")
    goal = goal_store.create(f"{status.value} 目标")
    if status is not GoalStatus.ACTIVE:
        goal_store.update(goal.id, status)

    snapshot = build_goal_pursuit_snapshot(goal_store, pursuit_store)

    assert snapshot.current_goal_id == goal.id
    assert snapshot.goals[0]["status"] == status.value


def test_snapshot_orders_history_and_marks_truncation(tmp_path) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    first = goal_store.create("已完成目标")
    goal_store.update(first.id, GoalStatus.COMPLETED)
    second = goal_store.create("已取消目标")
    goal_store.update(second.id, GoalStatus.CANCELLED)
    current = goal_store.create("当前目标")

    snapshot = build_goal_pursuit_snapshot(goal_store, pursuit_store, limit=2)

    assert snapshot.current_goal_id == current.id
    assert [item["goal_id"] for item in snapshot.goals] == [current.id, second.id]
    assert [item["status"] for item in snapshot.goals] == ["active", "cancelled"]
    assert snapshot.truncated is True


def test_snapshot_reports_corrupt_or_unreadable_goal_store(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal_store = GoalStore(tmp_path / "goals")
    pursuit_store = PursuitStore(tmp_path / "pursuit")
    goal_store.create("触发读取")
    monkeypatch.setattr(goal_store, "list", lambda **_: (_ for _ in ()).throw(RuntimeError()))

    snapshot = build_goal_pursuit_snapshot(goal_store, pursuit_store)

    assert snapshot.goals == ()
    assert "Goal 状态读取失败" in snapshot.warnings[0]
