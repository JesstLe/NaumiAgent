from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import ApprovalState
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


async def _append(store: WorkbenchStore, session_id: str, index: int):
    return await store.append_event(
        session_id=session_id,
        type="timeline.tested",
        actor="Test",
        subject_id=f"event-{index}",
        payload={"index": index},
    )


@pytest.mark.asyncio
async def test_timeline_cursor_is_durable_monotonic_and_session_scoped(tmp_path) -> None:
    db_path = tmp_path / "workbench.db"
    first_store = WorkbenchStore(str(db_path))

    first = await _append(first_store, "session-a", 1)
    second = await _append(first_store, "session-a", 2)
    other = await _append(first_store, "session-b", 1)

    assert (first.cursor, second.cursor, other.cursor) == (1, 2, 1)
    first_window = await first_store.timeline_replay_window(
        "session-a", after_cursor=0
    )
    restarted_store = WorkbenchStore(str(db_path))
    restarted_window = await restarted_store.timeline_replay_window(
        "session-a",
        after_cursor=1,
        expected_stream_id=first_window.stream_id,
    )
    assert restarted_window.stream_id == first_window.stream_id
    assert [event.cursor for event in restarted_window.events] == [2]


@pytest.mark.asyncio
async def test_concurrent_appends_reserve_unique_contiguous_cursors(tmp_path) -> None:
    db_path = tmp_path / "workbench.db"
    bootstrap = WorkbenchStore(str(db_path))
    await bootstrap.timeline_replay_window("session-a", after_cursor=0)
    stores = [WorkbenchStore(str(db_path)) for _ in range(12)]

    events = await asyncio.gather(
        *(_append(store, "session-a", index) for index, store in enumerate(stores))
    )

    assert sorted(event.cursor for event in events) == list(range(1, 13))
    window = await bootstrap.timeline_replay_window(
        "session-a",
        after_cursor=0,
    )
    assert [event.cursor for event in window.events] == list(range(1, 13))


@pytest.mark.asyncio
async def test_replay_reports_stream_and_cursor_gaps_without_guessing(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    await _append(store, "session-a", 1)
    await _append(store, "session-a", 2)
    current = await store.timeline_replay_window("session-a", after_cursor=0)

    changed = await store.timeline_replay_window(
        "session-a", after_cursor=1, expected_stream_id="different-stream"
    )
    missing_identity = await store.timeline_replay_window(
        "session-a", after_cursor=1
    )
    ahead = await store.timeline_replay_window(
        "session-a",
        after_cursor=99,
        expected_stream_id=current.stream_id,
    )

    assert (changed.gap, changed.gap_reason, changed.events) == (
        True,
        "stream_changed",
        (),
    )
    assert missing_identity.gap_reason == "stream_identity_required"
    assert ahead.gap_reason == "cursor_ahead"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            """DELETE FROM workbench_audit_events
               WHERE session_id = 'session-a' AND cursor = 1"""
        )
        await db.commit()
    before_retention = await store.timeline_replay_window(
        "session-a",
        after_cursor=0,
        expected_stream_id=current.stream_id,
    )
    assert before_retention.gap_reason == "cursor_before_retention"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "DELETE FROM workbench_audit_events WHERE session_id = 'session-a'"
        )
        await db.commit()
    fully_pruned = await store.timeline_replay_window(
        "session-a",
        after_cursor=0,
        expected_stream_id=current.stream_id,
    )
    assert fully_pruned.earliest_cursor == fully_pruned.latest_cursor + 1
    assert fully_pruned.gap_reason == "cursor_before_retention"


@pytest.mark.asyncio
async def test_empty_replay_is_read_only_and_limits_are_strict(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))

    empty = await store.timeline_replay_window("session-a", after_cursor=0)

    assert empty.stream_id == ""
    assert empty.events == ()
    async with aiosqlite.connect(store.db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM workbench_audit_streams")
        assert (await cursor.fetchone())[0] == 0
    with pytest.raises(ValueError, match="1..100"):
        await store.timeline_replay_window("session-a", after_cursor=0, limit=101)
    with pytest.raises(TypeError, match="必须是整数"):
        await store.timeline_replay_window("session-a", after_cursor=True)


@pytest.mark.asyncio
async def test_legacy_events_are_backfilled_once_in_stable_order(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE workbench_audit_events (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
                actor TEXT NOT NULL, subject_id TEXT NOT NULL, payload TEXT NOT NULL,
                timestamp TEXT NOT NULL, correlation_id TEXT, parent_event_id TEXT,
                severity TEXT NOT NULL DEFAULT 'info'
            )"""
        )
        await db.executemany(
            """INSERT INTO workbench_audit_events
               (id, session_id, type, actor, subject_id, payload, timestamp, severity)
               VALUES (?, 'session-a', 'legacy', 'Test', ?, '{}', ?, 'info')""",
            [
                ("later", "later", "2026-01-02T00:00:00"),
                ("earlier", "earlier", "2026-01-01T00:00:00"),
            ],
        )
        await db.commit()

    store = WorkbenchStore(str(db_path))
    first = await store.timeline_replay_window("session-a", after_cursor=0)
    restarted = WorkbenchStore(str(db_path))
    second = await restarted.timeline_replay_window("session-a", after_cursor=0)

    assert [(event.id, event.cursor) for event in first.events] == [
        ("earlier", 1),
        ("later", 2),
    ]
    assert second.stream_id == first.stream_id
    assert [event.cursor for event in second.events] == [1, 2]


@pytest.mark.asyncio
async def test_approval_resolution_allocates_cursor_in_same_transaction(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    approval = await store.add_approval(
        session_id="session-a",
        mission_id="mission-a",
        task_id="task-a",
        title="发布变更",
        detail="等待人工决定",
        requester="Agent",
    )

    resolved = await store.resolve_approval(
        "session-a",
        approval.id,
        ApprovalState.APPROVED,
        "Human",
        "通过",
    )
    window = await store.timeline_replay_window("session-a", after_cursor=0)

    assert resolved is not None
    assert len(window.events) == 1
    assert window.events[0].type == "approval.resolved"
    assert window.events[0].cursor == 1


@pytest.mark.asyncio
async def test_service_exposes_json_ready_enriched_timeline_window(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("session-a")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=workbench_store,
    )
    task = await task_store.create_task(subject="检查 Timeline")
    event = await workbench_store.append_event(
        session_id="session-a",
        type="issue.created",
        actor="Planner-Agent",
        subject_id=task.id,
        payload={"task_id": task.id},
    )

    result = await service.timeline_replay_window(
        "session-a",
        after_cursor=0,
        limit=10,
    )

    assert result["schema_version"] == 1
    assert result["latest_cursor"] == event.cursor == 1
    assert result["events"][0]["cursor"] == 1
    assert result["events"][0]["severity"] == "info"
    assert result["events"][0]["task"]["subject"] == "检查 Timeline"
