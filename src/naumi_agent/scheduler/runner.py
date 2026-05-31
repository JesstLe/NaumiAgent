"""Scheduler runner for reminders and cron jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from naumi_agent.scheduler.cron import CronSchedule
from naumi_agent.scheduler.models import (
    ScheduleEvent,
    ScheduleJob,
    ScheduleKind,
    ScheduleStatus,
    ScheduleTarget,
)
from naumi_agent.scheduler.store import SchedulerStore


class SchedulerRunner:
    """Create, scan, and deliver persistent schedule jobs."""

    def __init__(
        self,
        store: SchedulerStore,
        *,
        now_fn: Callable[[], datetime] | None = None,
        poll_seconds: float = 30,
    ) -> None:
        self._store = store
        self._now_fn = now_fn or _now
        self._poll_seconds = poll_seconds
        self._loop_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> SchedulerStore:
        return self._store

    def start(self) -> None:
        """Start an async polling loop when an event loop is running."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop_task = loop.create_task(self._run_loop())

    async def shutdown(self) -> None:
        """Stop the polling loop."""
        if self._loop_task is None:
            return
        task = self._loop_task
        self._loop_task = None
        if task.done():
            return
        task.cancel()
        if task.get_loop() is asyncio.get_running_loop():
            await asyncio.gather(task, return_exceptions=True)

    def create(
        self,
        *,
        kind: str,
        expression: str,
        prompt: str,
        target: str = ScheduleTarget.SESSION_MESSAGE.value,
    ) -> ScheduleJob:
        """Create a schedule and persist it."""
        normalized_kind = _parse_kind(kind)
        normalized_target = _parse_target(target)
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("提醒内容不能为空")

        now = self._now_fn()
        expression = expression.strip()
        if normalized_kind == ScheduleKind.ONCE:
            next_fire_at = _parse_once_datetime(expression, now=now)
        else:
            cron = CronSchedule.parse(expression)
            next_fire_at = cron.next_after(_as_local(now))

        job = ScheduleJob(
            id=self._store.next_id(),
            kind=normalized_kind,
            expression=expression,
            prompt=prompt,
            target=normalized_target,
            status=ScheduleStatus.ACTIVE,
            next_fire_at=_to_utc_iso(next_fire_at),
            created_at=_to_utc_iso(now),
        )
        self._store.save_job(job)
        return job

    def list_jobs(self, *, include_inactive: bool = True) -> list[ScheduleJob]:
        return self._store.list_jobs(include_inactive=include_inactive)

    def get(self, schedule_id: str) -> ScheduleJob | None:
        return self._store.get_job(schedule_id)

    def cancel(self, schedule_id: str) -> ScheduleJob | None:
        job = self._store.get_job(schedule_id)
        if job is None:
            return None
        if job.status not in {ScheduleStatus.CANCELLED, ScheduleStatus.COMPLETED}:
            job.status = ScheduleStatus.CANCELLED
            job.next_fire_at = ""
            self._store.save_job(job)
        return job

    def pause(self, schedule_id: str) -> ScheduleJob | None:
        job = self._store.get_job(schedule_id)
        if job is None:
            return None
        if job.status == ScheduleStatus.ACTIVE:
            job.status = ScheduleStatus.PAUSED
            self._store.save_job(job)
        return job

    def resume(self, schedule_id: str) -> ScheduleJob | None:
        job = self._store.get_job(schedule_id)
        if job is None:
            return None
        if job.status in {ScheduleStatus.CANCELLED, ScheduleStatus.COMPLETED}:
            return job
        if job.kind == ScheduleKind.CRON:
            job.next_fire_at = _to_utc_iso(
                CronSchedule.parse(job.expression).next_after(_as_local(self._now_fn()))
            )
        elif not job.next_fire_at or _parse_datetime(job.next_fire_at) < _as_utc(self._now_fn()):
            job.next_fire_at = _to_utc_iso(self._now_fn())
        job.status = ScheduleStatus.ACTIVE
        self._store.save_job(job)
        return job

    def tick_due(self) -> int:
        """Scan due jobs once and create durable pending events."""
        now = _as_utc(self._now_fn())
        fired = 0
        for job in self._store.due_jobs(_to_utc_iso(now)):
            if not job.next_fire_at:
                continue
            fired_at = job.next_fire_at
            event = ScheduleEvent(
                id=_event_id(job.id, fired_at),
                schedule_id=job.id,
                fired_at=fired_at,
                prompt=job.prompt,
                target=job.target,
            )
            if self._store.add_event(event):
                fired += 1

            job.last_fired_at = fired_at
            job.fired_count += 1
            if job.kind == ScheduleKind.ONCE:
                job.status = ScheduleStatus.COMPLETED
                job.next_fire_at = ""
            else:
                next_base = max(_parse_datetime(fired_at), now)
                next_fire_at = CronSchedule.parse(job.expression).next_after(
                    _as_local(next_base)
                )
                job.next_fire_at = _to_utc_iso(next_fire_at)
            self._store.save_job(job)
        return fired

    def collect_notifications(self, limit: int = 10) -> list[str]:
        """Return due schedule notifications and mark them delivered."""
        self.tick_due()
        notifications: list[str] = []
        for event in self._store.pending_events(limit=limit):
            notifications.append(format_notification(event))
            self._store.mark_event_delivered(event.id)
        return notifications

    async def _run_loop(self) -> None:
        while True:
            self.tick_due()
            await asyncio.sleep(self._poll_seconds)


def format_job(job: ScheduleJob) -> str:
    next_fire = job.next_fire_at or "无"
    last_fired = job.last_fired_at or "无"
    return (
        f"### 调度 {job.id}\n"
        f"- 状态：{_status_label(job.status)}\n"
        f"- 类型：{_kind_label(job.kind)}\n"
        f"- 表达式：`{job.expression}`\n"
        f"- 目标：{_target_label(job.target)}\n"
        f"- 下次触发：{next_fire}\n"
        f"- 上次触发：{last_fired}\n"
        f"- 已触发次数：{job.fired_count}\n"
        f"- 内容：{job.prompt}"
    )


def format_job_list(jobs: list[ScheduleJob]) -> str:
    if not jobs:
        return "当前没有调度任务。"
    return "\n\n".join(format_job(job) for job in jobs)


def format_notification(event: ScheduleEvent) -> str:
    return (
        "<schedule_notification>\n"
        f"调度ID：{event.schedule_id}\n"
        f"触发时间：{event.fired_at}\n"
        f"目标：{_target_label(event.target)}\n"
        f"内容：{event.prompt}\n"
        "</schedule_notification>"
    )


def _parse_kind(value: str) -> ScheduleKind:
    try:
        return ScheduleKind(value.strip())
    except ValueError as e:
        raise ValueError("调度类型必须是 once 或 cron") from e


def _parse_target(value: str) -> ScheduleTarget:
    try:
        return ScheduleTarget(value.strip() or ScheduleTarget.SESSION_MESSAGE.value)
    except ValueError as e:
        raise ValueError("调度目标当前只支持 session_message") from e


def _parse_once_datetime(expression: str, *, now: datetime) -> datetime:
    if not expression:
        raise ValueError("once 调度必须提供 ISO 时间，例如 2026-06-01T10:30:00")
    value = _parse_datetime(expression)
    if value < _as_utc(now):
        raise ValueError("once 调度时间不能早于当前时间")
    return value


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError("时间必须是 ISO 格式，例如 2026-06-01T10:30:00") from e
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return _as_utc(parsed)


def _now() -> datetime:
    return datetime.now().astimezone()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone()
    return value.astimezone()


def _to_utc_iso(value: datetime) -> str:
    return _as_utc(value).replace(microsecond=0).isoformat()


def _event_id(schedule_id: str, fired_at: str) -> str:
    safe_time = fired_at.replace(":", "").replace("+", "p").replace("-", "")
    return f"{schedule_id}_{safe_time}"


def _status_label(status: ScheduleStatus) -> str:
    return {
        ScheduleStatus.ACTIVE: "启用中",
        ScheduleStatus.PAUSED: "已暂停",
        ScheduleStatus.CANCELLED: "已取消",
        ScheduleStatus.COMPLETED: "已完成",
    }[status]


def _kind_label(kind: ScheduleKind) -> str:
    return {
        ScheduleKind.ONCE: "一次性",
        ScheduleKind.CRON: "Cron",
    }[kind]


def _target_label(target: ScheduleTarget) -> str:
    return {
        ScheduleTarget.SESSION_MESSAGE: "会话提醒",
    }[target]
