"""Durable workspace goal state backed by SQLite."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MAX_GOAL_OBJECTIVE_CHARS = 8_000
MAX_GOAL_NOTE_CHARS = 4_000
MAX_GOAL_ID_CHARS = 128
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class GoalStoreError(ValueError):
    """A user-correctable durable goal error."""


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {GoalStatus.COMPLETED, GoalStatus.CANCELLED}


_OPEN_STATUSES = (
    GoalStatus.ACTIVE.value,
    GoalStatus.PAUSED.value,
    GoalStatus.BLOCKED.value,
)
_ALLOWED_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.ACTIVE: frozenset({
        GoalStatus.PAUSED,
        GoalStatus.BLOCKED,
        GoalStatus.COMPLETED,
        GoalStatus.CANCELLED,
    }),
    GoalStatus.PAUSED: frozenset({
        GoalStatus.ACTIVE,
        GoalStatus.BLOCKED,
        GoalStatus.COMPLETED,
        GoalStatus.CANCELLED,
    }),
    GoalStatus.BLOCKED: frozenset({
        GoalStatus.ACTIVE,
        GoalStatus.PAUSED,
        GoalStatus.COMPLETED,
        GoalStatus.CANCELLED,
    }),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class Goal:
    id: str
    objective: str
    status: GoalStatus
    note: str
    session_id: str
    pursuit_run_id: str
    created_at: float
    updated_at: float


class GoalStore:
    """Persist one unfinished goal per workspace with transactional transitions."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._db_path = self._base_dir / "goals.db"
        self._init_db()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create(self, objective: str, *, session_id: str = "") -> Goal:
        normalized = _normalize_text(
            objective,
            field="目标",
            max_chars=MAX_GOAL_OBJECTIVE_CHARS,
            required=True,
        )
        normalized_session = _normalize_identifier(session_id, field="session_id", optional=True)
        now = _now()
        goal = Goal(
            id=f"goal_{uuid.uuid4().hex[:12]}",
            objective=normalized,
            status=GoalStatus.ACTIVE,
            note="",
            session_id=normalized_session,
            pursuit_run_id="",
            created_at=now,
            updated_at=now,
        )
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO goals (
                        id, objective, status, note, session_id, pursuit_run_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal.id,
                        goal.objective,
                        goal.status.value,
                        goal.note,
                        goal.session_id,
                        goal.pursuit_run_id,
                        goal.created_at,
                        goal.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            current = self.current()
            suffix = f"（{current.id}：{current.objective}）" if current else ""
            raise GoalStoreError(f"当前已有未完成目标{suffix}，请先完成或取消它。") from exc
        return goal

    def get(self, goal_id: str) -> Goal | None:
        normalized = _normalize_identifier(goal_id, field="目标 ID")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (normalized,)).fetchone()
        return _goal_from_row(row) if row is not None else None

    def current(self) -> Goal | None:
        placeholders = ", ".join("?" for _ in _OPEN_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM goals
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,  # noqa: S608 - placeholders are generated from a fixed tuple
                _OPEN_STATUSES,
            ).fetchone()
        return _goal_from_row(row) if row is not None else None

    def list(self, *, include_finished: bool = True, limit: int = 50) -> list[Goal]:
        bounded_limit = max(1, min(int(limit), 200))
        params: tuple[object, ...]
        if include_finished:
            query = "SELECT * FROM goals ORDER BY updated_at DESC, id DESC LIMIT ?"
            params = (bounded_limit,)
        else:
            placeholders = ", ".join("?" for _ in _OPEN_STATUSES)
            query = (
                f"SELECT * FROM goals WHERE status IN ({placeholders}) "  # noqa: S608
                "ORDER BY updated_at DESC, id DESC LIMIT ?"
            )
            params = (*_OPEN_STATUSES, bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_goal_from_row(row) for row in rows]

    def update(
        self,
        goal_id: str,
        status: GoalStatus | str,
        *,
        note: str = "",
    ) -> Goal:
        normalized_id = _normalize_identifier(goal_id, field="目标 ID")
        try:
            target = status if isinstance(status, GoalStatus) else GoalStatus(str(status))
        except ValueError as exc:
            choices = ", ".join(item.value for item in GoalStatus)
            raise GoalStoreError(f"不支持的目标状态：{status}。可选：{choices}。") from exc
        normalized_note = _normalize_text(
            note,
            field="说明",
            max_chars=MAX_GOAL_NOTE_CHARS,
            required=False,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (normalized_id,)).fetchone()
            if row is None:
                raise GoalStoreError(f"目标不存在：{normalized_id}")
            goal = _goal_from_row(row)
            if goal.status is target:
                raise GoalStoreError(f"目标已经是 {target.value} 状态。")
            if goal.status.is_terminal:
                raise GoalStoreError(f"目标已进入终态 {goal.status.value}，不能再次变更。")
            if target not in _ALLOWED_TRANSITIONS[goal.status]:
                raise GoalStoreError(
                    f"不允许从 {goal.status.value} 转为 {target.value}。"
                )
            now = _now()
            conn.execute(
                "UPDATE goals SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                (target.value, normalized_note, now, normalized_id),
            )
        updated = self.get(normalized_id)
        assert updated is not None
        return updated

    def attach_pursuit(self, goal_id: str, run_id: str) -> Goal:
        normalized_id = _normalize_identifier(goal_id, field="目标 ID")
        normalized_run_id = _normalize_identifier(run_id, field="Pursuit run_id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (normalized_id,)).fetchone()
            if row is None:
                raise GoalStoreError(f"目标不存在：{normalized_id}")
            goal = _goal_from_row(row)
            if goal.status.is_terminal:
                raise GoalStoreError("已完成或取消的目标不能关联新的 Pursuit 运行。")
            conn.execute(
                "UPDATE goals SET pursuit_run_id = ?, updated_at = ? WHERE id = ?",
                (normalized_run_id, _now(), normalized_id),
            )
        updated = self.get(normalized_id)
        assert updated is not None
        return updated

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    pursuit_run_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS goals_one_unfinished
                ON goals ((1))
                WHERE status IN ('active', 'paused', 'blocked')
                """
            )


def format_goal(goal: Goal) -> str:
    status = {
        GoalStatus.ACTIVE: "进行中",
        GoalStatus.PAUSED: "已暂停",
        GoalStatus.BLOCKED: "已阻塞",
        GoalStatus.COMPLETED: "已完成",
        GoalStatus.CANCELLED: "已取消",
    }[goal.status]
    lines = [
        f"### Goal {goal.id}",
        f"- 状态：{status} (`{goal.status.value}`)",
        f"- 目标：{goal.objective}",
        f"- 会话：{goal.session_id or '未绑定'}",
        f"- Pursuit：{goal.pursuit_run_id or '未启动'}",
    ]
    if goal.note:
        lines.append(f"- 说明：{goal.note}")
    return "\n".join(lines)


def format_goal_list(goals: list[Goal]) -> str:
    if not goals:
        return "当前没有目标记录。"
    return "\n\n".join(format_goal(goal) for goal in goals)


def _goal_from_row(row: sqlite3.Row) -> Goal:
    return Goal(
        id=row["id"],
        objective=row["objective"],
        status=GoalStatus(row["status"]),
        note=row["note"],
        session_id=row["session_id"],
        pursuit_run_id=row["pursuit_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _normalize_text(value: object, *, field: str, max_chars: int, required: bool) -> str:
    text = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if required and not text:
        raise GoalStoreError(f"{field}不能为空。")
    if len(text) > max_chars:
        raise GoalStoreError(f"{field}过长，最多 {max_chars} 个字符。")
    return text


def _normalize_identifier(value: object, *, field: str, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if not text:
        raise GoalStoreError(f"{field}不能为空。")
    if len(text) > MAX_GOAL_ID_CHARS or not _ID_RE.fullmatch(text):
        raise GoalStoreError(f"{field}格式无效。")
    return text


def _now() -> float:
    return time.time_ns() / 1_000_000_000
