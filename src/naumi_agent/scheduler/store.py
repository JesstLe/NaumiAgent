"""SQLite store for scheduler jobs and fired events."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from naumi_agent.scheduler.models import (
    ScheduleEvent,
    ScheduleJob,
    ScheduleKind,
    ScheduleStatus,
    ScheduleTarget,
)


class SchedulerStore:
    """Durable scheduler state backed by SQLite."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._db_path = self._base_dir / "schedules.db"
        self._init_db()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def next_id(self) -> str:
        jobs = self.list_jobs(include_inactive=True)
        numbers: list[int] = []
        for job in jobs:
            prefix, _, suffix = job.id.partition("_")
            if prefix == "sch" and suffix.isdigit():
                numbers.append(int(suffix))
        return f"sch_{(max(numbers) if numbers else 0) + 1:04d}"

    def save_job(self, job: ScheduleJob) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, kind, expression, prompt, target, status, next_fire_at,
                    created_at, last_fired_at, fired_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    expression=excluded.expression,
                    prompt=excluded.prompt,
                    target=excluded.target,
                    status=excluded.status,
                    next_fire_at=excluded.next_fire_at,
                    created_at=excluded.created_at,
                    last_fired_at=excluded.last_fired_at,
                    fired_count=excluded.fired_count
                """,
                (
                    job.id,
                    job.kind.value,
                    job.expression,
                    job.prompt,
                    job.target.value,
                    job.status.value,
                    job.next_fire_at,
                    job.created_at,
                    job.last_fired_at,
                    job.fired_count,
                ),
            )

    def get_job(self, schedule_id: str) -> ScheduleJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return _job_from_row(row) if row else None

    def list_jobs(self, *, include_inactive: bool = True) -> list[ScheduleJob]:
        query = "SELECT * FROM schedules"
        params: tuple[str, ...] = ()
        if not include_inactive:
            query += " WHERE status = ?"
            params = (ScheduleStatus.ACTIVE.value,)
        query += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_job_from_row(row) for row in rows]

    def due_jobs(self, now_iso: str) -> list[ScheduleJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schedules
                WHERE status = ? AND next_fire_at != '' AND next_fire_at <= ?
                ORDER BY next_fire_at ASC, id ASC
                """,
                (ScheduleStatus.ACTIVE.value, now_iso),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def add_event(self, event: ScheduleEvent) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO schedule_events (
                        id, schedule_id, fired_at, prompt, target, delivered
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.schedule_id,
                        event.fired_at,
                        event.prompt,
                        event.target.value,
                        int(event.delivered),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def pending_events(self, *, limit: int = 10) -> list[ScheduleEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schedule_events
                WHERE delivered = 0
                ORDER BY fired_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def mark_event_delivered(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_events SET delivered = 1 WHERE id = ?",
                (event_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    next_fire_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_fired_at TEXT NOT NULL DEFAULT '',
                    fired_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_events (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    target TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(schedule_id, fired_at),
                    FOREIGN KEY(schedule_id) REFERENCES schedules(id)
                )
                """
            )


def _job_from_row(row: sqlite3.Row) -> ScheduleJob:
    return ScheduleJob(
        id=row["id"],
        kind=ScheduleKind(row["kind"]),
        expression=row["expression"],
        prompt=row["prompt"],
        target=ScheduleTarget(row["target"]),
        status=ScheduleStatus(row["status"]),
        next_fire_at=row["next_fire_at"],
        created_at=row["created_at"],
        last_fired_at=row["last_fired_at"],
        fired_count=row["fired_count"],
    )


def _event_from_row(row: sqlite3.Row) -> ScheduleEvent:
    return ScheduleEvent(
        id=row["id"],
        schedule_id=row["schedule_id"],
        fired_at=row["fired_at"],
        prompt=row["prompt"],
        target=ScheduleTarget(row["target"]),
        delivered=bool(row["delivered"]),
    )
