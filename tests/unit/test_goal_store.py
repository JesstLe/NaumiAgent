"""Tests for durable workspace goals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from naumi_agent.orchestrator.goal_store import (
    GoalStatus,
    GoalStore,
    GoalStoreError,
)


def test_goal_store_persists_current_goal_and_metadata(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")

    goal = store.create("完成新的交互式 UI", session_id="session-1")
    reopened = GoalStore(tmp_path / "goals")

    assert goal.status is GoalStatus.ACTIVE
    assert goal.id.startswith("goal_")
    assert reopened.current() == goal
    assert reopened.get(goal.id) == goal


def test_goal_store_allows_only_one_unfinished_goal(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    first = store.create("第一个目标")

    with pytest.raises(GoalStoreError, match=first.id):
        store.create("第二个目标")

    assert [item.objective for item in store.list()] == ["第一个目标"]


def test_goal_store_unique_constraint_is_safe_under_concurrent_create(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")

    def create(index: int) -> str:
        try:
            return store.create(f"并发目标 {index}").id
        except GoalStoreError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))

    assert len([item for item in results if item != "rejected"]) == 1
    assert results.count("rejected") == 3
    assert len(store.list()) == 1


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (GoalStatus.ACTIVE, GoalStatus.PAUSED),
        (GoalStatus.ACTIVE, GoalStatus.BLOCKED),
        (GoalStatus.PAUSED, GoalStatus.ACTIVE),
        (GoalStatus.PAUSED, GoalStatus.COMPLETED),
        (GoalStatus.BLOCKED, GoalStatus.PAUSED),
        (GoalStatus.BLOCKED, GoalStatus.CANCELLED),
    ],
)
def test_goal_store_applies_supported_transitions(tmp_path, initial, target) -> None:
    store = GoalStore(tmp_path / f"goals-{initial}-{target}")
    goal = store.create("验证状态机")
    if initial is not GoalStatus.ACTIVE:
        goal = store.update(goal.id, initial, note="进入初始状态")

    updated = store.update(goal.id, target, note="状态已更新")

    assert updated.status is target
    assert updated.note == "状态已更新"
    assert store.current() == (None if target.is_terminal else updated)


def test_goal_store_rejects_terminal_and_same_state_transitions(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    goal = store.create("验证非法状态")

    with pytest.raises(GoalStoreError, match="已经是 active"):
        store.update(goal.id, GoalStatus.ACTIVE)

    completed = store.update(goal.id, GoalStatus.COMPLETED)
    with pytest.raises(GoalStoreError, match="终态"):
        store.update(completed.id, GoalStatus.ACTIVE)


def test_goal_store_validates_text_and_can_link_pursuit_run(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")

    with pytest.raises(GoalStoreError, match="不能为空"):
        store.create("\x00\n")

    goal = store.create("关联追踪运行")
    linked = store.attach_pursuit(goal.id, "pursuit_demo-1")

    assert linked.pursuit_run_id == "pursuit_demo-1"
    assert store.current() == linked


def test_goal_store_list_orders_latest_update_first(tmp_path) -> None:
    store = GoalStore(tmp_path / "goals")
    first = store.create("先完成的目标")
    store.update(first.id, GoalStatus.COMPLETED)
    second = store.create("当前目标")

    assert [goal.id for goal in store.list()] == [second.id, first.id]
    assert store.list(include_finished=False) == [second]
