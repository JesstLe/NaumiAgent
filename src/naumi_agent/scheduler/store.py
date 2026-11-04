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
                    created_at, last_fired_at, fired_count, scope_key, binding_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    expression=excluded.expression,
                    prompt=excluded.prompt,
                    target=excluded.target,
                    status=excluded.status,
                    next_fire_at=excluded.next_fire_at,
                    created_at=excluded.created_at,
                    last_fired_at=excluded.last_fired_at,
                    fired_count=excluded.fired_count,
                    scope_key=excluded.scope_key,
                    binding_sha256=excluded.binding_sha256
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
                    job.scope_key,
                    job.binding_sha256,
                ),
            )

    def get_job(self, schedule_id: str) -> ScheduleJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return _job_from_row(row) if row else None

    def create_managed_job(self, job: ScheduleJob) -> ScheduleJob:
        """Create once by immutable identity, without resurrecting an existing job."""
        if job.target is not ScheduleTarget.PERSONAL_ITEM or not job.scope_key:
            raise ValueError("事项调度缺少实际作用范围")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM schedules WHERE id = ?", (job.id,)).fetchone()
            if row is not None:
                existing = _job_from_row(row)
                fields = (
                    "kind", "expression", "prompt", "target", "scope_key", "binding_sha256",
                )
                if any(getattr(existing, key) != getattr(job, key) for key in fields):
                    raise ValueError("调度请求编号已绑定其他事项版本")
                return existing
            conn.execute(
                """INSERT INTO schedules (
                id,kind,expression,prompt,target,status,next_fire_at,created_at,
                last_fired_at,fired_count,scope_key,binding_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.kind.value, job.expression, job.prompt, job.target.value,
                 job.status.value, job.next_fire_at, job.created_at, "", 0,
                 job.scope_key, job.binding_sha256),
            )
        return job

    def transition_managed(
        self, schedule_id: str, *, scope_key: str, binding_sha256: str, action: str,
    ) -> ScheduleJob | None:
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("调度操作无效")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            if (
                job.target is not ScheduleTarget.PERSONAL_ITEM or job.scope_key != scope_key
                or job.binding_sha256 != binding_sha256
            ):
                raise ValueError("事项调度身份不一致")
            if job.status not in {ScheduleStatus.CANCELLED, ScheduleStatus.COMPLETED}:
                job.status = {
                    "pause": ScheduleStatus.PAUSED, "resume": ScheduleStatus.ACTIVE,
                    "cancel": ScheduleStatus.CANCELLED,
                }[action]
                if action == "cancel":
                    job.next_fire_at = ""
                conn.execute(
                    "UPDATE schedules SET status=?,next_fire_at=? WHERE id=?",
                    (job.status.value, job.next_fire_at, job.id),
                )
        return job

    def apply_fire(
        self, schedule_id: str, *, expected_fire_at: str, event_id: str,
        next_fire_at: str,
    ) -> bool:
        """Commit the event and schedule transition in one SQLite transaction."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                return False
            job = _job_from_row(row)
            if job.status is not ScheduleStatus.ACTIVE or job.next_fire_at != expected_fire_at:
                return False
            inserted = conn.execute(
                """INSERT OR IGNORE INTO schedule_events
                (id,schedule_id,fired_at,prompt,target,delivered,scope_key,binding_sha256)
                VALUES (?,?,?,?,?,0,?,?)""",
                (event_id, job.id, expected_fire_at, job.prompt, job.target.value,
                 job.scope_key, job.binding_sha256),
            ).rowcount
            status = ScheduleStatus.COMPLETED if job.kind is ScheduleKind.ONCE else job.status
            conn.execute(
                """UPDATE schedules SET status=?,next_fire_at=?,last_fired_at=?,
                fired_count=fired_count+1 WHERE id=?""",
                (status.value, next_fire_at, expected_fire_at, job.id),
            )
        return inserted == 1

    def get_event(self, event_id: str) -> ScheduleEvent | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedule_events WHERE id=?", (event_id,)).fetchone()
        return _event_from_row(row) if row is not None else None

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
                        id, schedule_id, fired_at, prompt, target, delivered,
                        scope_key, binding_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.schedule_id,
                        event.fired_at,
                        event.prompt,
                        event.target.value,
                        int(event.delivered),
                        event.scope_key,
                        event.binding_sha256,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def pending_events(
        self, *, limit: int = 10, target: ScheduleTarget | None = None,
        scope_key: str | None = None,
        after: tuple[str, str] | None = None,
    ) -> list[ScheduleEvent]:
        clauses, values = ["delivered = 0"], []
        if target is not None:
            clauses.append("target = ?")
            values.append(str(target))
        if scope_key is not None:
            clauses.append("scope_key = ?")
            values.append(scope_key)
        if after is not None:
            clauses.append("(fired_at > ? OR (fired_at = ? AND id > ?))")
            values.extend((after[0], after[0], after[1]))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_events WHERE " + " AND ".join(clauses)
                + " ORDER BY fired_at ASC, id ASC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def mark_event_delivered(self, event_id: str) -> None:
        self.mark_event_consumed(event_id, target=ScheduleTarget.SESSION_MESSAGE)

    def mark_event_consumed(
        self, event_id: str, *, target: ScheduleTarget, scope_key: str | None = None,
    ) -> bool:
        clauses, values = ["id = ?", "target = ?"], [event_id, str(target)]
        if scope_key is not None:
            clauses.append("scope_key = ?")
            values.append(scope_key)
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE schedule_events SET delivered = 1 WHERE " + " AND ".join(clauses),
                values,
            ).rowcount
        return changed == 1

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            for table in ("schedules", "schedule_events"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column in ("scope_key", "binding_sha256"):
                    if column not in columns:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
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
        scope_key=row["scope_key"],
        binding_sha256=row["binding_sha256"],
    )


def _event_from_row(row: sqlite3.Row) -> ScheduleEvent:
    return ScheduleEvent(
        id=row["id"],
        schedule_id=row["schedule_id"],
        fired_at=row["fired_at"],
        prompt=row["prompt"],
        target=ScheduleTarget(row["target"]),
        delivered=bool(row["delivered"]),
        scope_key=row["scope_key"],
        binding_sha256=row["binding_sha256"],
    )
