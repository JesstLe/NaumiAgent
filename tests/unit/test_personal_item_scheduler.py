"""Real SQLite races and legacy compatibility for managed personal schedules."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.scheduler.models import ScheduleStatus, ScheduleTarget
from naumi_agent.scheduler.runner import SchedulerRunner
from naumi_agent.scheduler.store import SchedulerStore

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def make(tmp_path):
    return SchedulerRunner(SchedulerStore(tmp_path), now_fn=lambda: NOW)


def managed(runner, **overrides):
    return runner.create_personal_item(
        **{
            "schedule_id": "sch_pi_" + "a" * 64,
            "due_at": NOW.isoformat(),
            "prompt": "original-binding",
            "scope_key": "workspace-a",
            "binding_sha256": "b" * 64,
            **overrides,
        }
    )


def test_dual_runners_fire_one_event_and_one_count(tmp_path):
    first, second = make(tmp_path), make(tmp_path)
    job = managed(first)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda i: [first, second][i % 2].tick_due(), range(24)))
    assert sum(counts) == 1
    assert first.get(job.id).fired_count == 1
    assert len(first.store.pending_events()) == 1
    assert first.get(job.id).status == ScheduleStatus.COMPLETED


def test_cancel_fences_stale_fire_and_never_resurrects(tmp_path):
    runner = make(tmp_path)
    job = managed(runner)
    runner.store.transition_managed(
        job.id,
        scope_key=job.scope_key,
        binding_sha256=job.binding_sha256,
        action="cancel",
    )
    assert not runner.store.apply_fire(
        job.id,
        expected_fire_at=job.next_fire_at,
        event_id="stale",
        next_fire_at="",
    )
    assert managed(runner).status == ScheduleStatus.CANCELLED
    assert runner.get(job.id).fired_count == 0
    assert runner.store.pending_events() == []


def test_run_now_and_due_share_original_occurrence(tmp_path):
    runner = make(tmp_path)
    job = managed(runner, due_at=(NOW + timedelta(days=1)).isoformat())
    first = runner.fire_personal_item_now(
        job.id,
        scope_key=job.scope_key,
        binding_sha256=job.binding_sha256,
    )
    second = runner.fire_personal_item_now(
        job.id,
        scope_key=job.scope_key,
        binding_sha256=job.binding_sha256,
    )
    assert first == second
    assert first.fired_at == job.next_fire_at
    assert runner.tick_due() == 0
    assert runner.get(job.id).fired_count == 1


def test_reminder_collector_and_legacy_controls_do_not_steal_items(tmp_path):
    runner = make(tmp_path)
    item = managed(runner)
    reminder = runner.create(kind="once", expression=NOW.isoformat(), prompt="提醒")
    assert len(runner.collect_notifications()) == 1
    assert [j.id for j in runner.list_jobs()] == [reminder.id]
    event = runner.store.pending_events(target=ScheduleTarget.PERSONAL_ITEM)[0]
    runner.store.mark_event_delivered(event.id)
    assert not runner.store.get_event(event.id).delivered
    assert not runner.store.mark_event_consumed(
        event.id,
        target=ScheduleTarget.PERSONAL_ITEM,
        scope_key="wrong-workspace",
    )
    assert runner.store.mark_event_consumed(
        event.id,
        target=ScheduleTarget.PERSONAL_ITEM,
        scope_key=item.scope_key,
    )
    assert runner.store.mark_event_consumed(
        event.id,
        target=ScheduleTarget.PERSONAL_ITEM,
        scope_key=item.scope_key,
    )
    for control in (runner.cancel, runner.pause, runner.resume):
        with pytest.raises(ValueError, match="事项"):
            control(item.id)


def test_managed_identity_immutable_and_scope_fenced(tmp_path):
    runner = make(tmp_path)
    job = managed(runner)
    assert managed(runner) == job
    with pytest.raises(ValueError):
        managed(runner, prompt="changed")
    with pytest.raises(ValueError):
        runner.store.transition_managed(
            job.id,
            scope_key="wrong",
            binding_sha256=job.binding_sha256,
            action="pause",
        )
    assert runner.get(job.id) == job
    assert runner.store.create_managed_job(replace(job)) == job


def test_paused_due_keeps_same_nominal_time_on_resume(tmp_path):
    runner = make(tmp_path)
    job = managed(runner)
    runner.store.transition_managed(
        job.id,
        scope_key=job.scope_key,
        binding_sha256=job.binding_sha256,
        action="pause",
    )
    assert runner.tick_due() == 0
    with pytest.raises(ValueError, match="暂停"):
        runner.fire_personal_item_now(
            job.id,
            scope_key=job.scope_key,
            binding_sha256=job.binding_sha256,
        )
    runner.store.transition_managed(
        job.id,
        scope_key=job.scope_key,
        binding_sha256=job.binding_sha256,
        action="resume",
    )
    assert runner.tick_due() == 1
    assert runner.get(job.id).last_fired_at == job.next_fire_at


def test_real_old_database_migrates_without_losing_reminders(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    with sqlite3.connect(tmp_path / "schedules.db") as conn:
        conn.executescript("""
        CREATE TABLE schedules (id TEXT PRIMARY KEY,kind TEXT,expression TEXT,prompt TEXT,
        target TEXT,status TEXT,next_fire_at TEXT,created_at TEXT,
        last_fired_at TEXT,fired_count INT);
        CREATE TABLE schedule_events (id TEXT PRIMARY KEY,
        schedule_id TEXT,fired_at TEXT,
        prompt TEXT,target TEXT,delivered INT,UNIQUE(schedule_id,fired_at));
        """)
        conn.execute(
            "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "sch_0001",
                "once",
                NOW.isoformat(),
                "原提醒",
                "session_message",
                "active",
                NOW.isoformat(),
                NOW.isoformat(),
                "",
                0,
            ),
        )
    runner = make(tmp_path)
    assert runner.get("sch_0001").scope_key == ""
    assert len(runner.collect_notifications()) == 1
    assert runner.get("sch_0001").fired_count == 1
    assert make(tmp_path).collect_notifications() == []
