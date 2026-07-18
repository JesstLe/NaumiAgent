"""SQLite persistence for pursuit run state."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from naumi_agent.orchestrator.pursuit import (
    PursuitBackgroundWait,
    PursuitEvidence,
    PursuitRun,
    PursuitRunStatus,
)


class PursuitStore:
    """Durable store for pursuit runs, evidence, and async waits."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._db_path = self._base_dir / "pursuit.db"
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def save_run(self, run: PursuitRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pursuit_runs (
                    id, goal, status, phase, started_at, updated_at, iteration,
                    criteria_total, criteria_verified, failure_count,
                    blocked_reason, next_action, worktree_name, worktree_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,
                    status=excluded.status,
                    phase=excluded.phase,
                    updated_at=excluded.updated_at,
                    iteration=excluded.iteration,
                    criteria_total=excluded.criteria_total,
                    criteria_verified=excluded.criteria_verified,
                    failure_count=excluded.failure_count,
                    blocked_reason=excluded.blocked_reason,
                    next_action=excluded.next_action,
                    worktree_name=excluded.worktree_name,
                    worktree_path=excluded.worktree_path
                """,
                (
                    run.id,
                    run.goal,
                    run.status.value,
                    run.phase,
                    run.started_at,
                    run.updated_at,
                    run.iteration,
                    run.criteria_total,
                    run.criteria_verified,
                    run.failure_count,
                    run.blocked_reason,
                    run.next_action,
                    run.worktree_name,
                    run.worktree_path,
                ),
            )
            conn.execute("DELETE FROM pursuit_evidence WHERE run_id = ?", (run.id,))
            for index, evidence in enumerate(run.evidence or []):
                conn.execute(
                    """
                    INSERT INTO pursuit_evidence (
                        run_id, seq, kind, source, summary, is_hard, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        index,
                        evidence.kind,
                        evidence.source,
                        evidence.summary,
                        int(evidence.is_hard),
                        evidence.timestamp,
                    ),
                )
            conn.execute("DELETE FROM pursuit_waits WHERE run_id = ?", (run.id,))
            for wait in run.waiting_on or []:
                conn.execute(
                    """
                    INSERT INTO pursuit_waits (
                        run_id, task_id, action_id, command, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        wait.task_id,
                        wait.action_id,
                        wait.command,
                        wait.created_at,
                    ),
                )

    def get_run(self, run_id: str) -> PursuitRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pursuit_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            evidence_rows = conn.execute(
                """
                SELECT * FROM pursuit_evidence
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
            wait_rows = conn.execute(
                """
                SELECT * FROM pursuit_waits
                WHERE run_id = ?
                ORDER BY created_at ASC, task_id ASC
                """,
                (run_id,),
            ).fetchall()
        return _run_from_rows(row, evidence_rows, wait_rows)

    def list_runs(self, *, include_finished: bool = True) -> list[PursuitRun]:
        query = "SELECT * FROM pursuit_runs"
        params: tuple[str, ...] = ()
        if not include_finished:
            query += " WHERE status IN (?, ?)"
            params = (PursuitRunStatus.RUNNING.value, PursuitRunStatus.WAITING.value)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        runs: list[PursuitRun] = []
        for row in rows:
            run = self.get_run(row["id"])
            if run is not None:
                runs.append(run)
        return runs

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        return self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._base_dir.mkdir(parents=True, exist_ok=True)
            with self._open_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_runs (
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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_evidence (
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        source TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        is_hard INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        PRIMARY KEY(run_id, seq),
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_waits (
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        action_id TEXT NOT NULL,
                        command TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(run_id, task_id),
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                    )
                    """
                )
            self._initialized = True


def format_run(run: PursuitRun) -> str:
    """Format one pursuit run for users and tool output."""
    waits = run.waiting_on or []
    evidence = run.evidence or []
    wait_lines = "\n".join(
        f"  - {wait.task_id} / action {wait.action_id}: `{wait.command}`"
        for wait in waits
    ) or "  - 无"
    evidence_lines = "\n".join(
        f"  - [{item.kind}] {item.source}: {item.summary[:160]}"
        for item in evidence[-5:]
    ) or "  - 暂无"
    blocked = f"\n- 阻塞原因：{run.blocked_reason}" if run.blocked_reason else ""
    worktree = (
        f"\n- Worktree：{run.worktree_name} `{run.worktree_path}`"
        if run.worktree_name or run.worktree_path
        else ""
    )
    return (
        f"### PursuitRun {run.id}\n"
        f"- 状态：{_status_label(run.status)}\n"
        f"- 阶段：{run.phase}\n"
        f"- 目标：{run.goal}\n"
        f"- 轮次：{run.iteration}\n"
        f"- 成功标准：{run.criteria_verified}/{run.criteria_total}\n"
        f"- 失败计数：{run.failure_count}\n"
        f"- 下一步：{run.next_action or '无'}"
        f"{worktree}{blocked}\n"
        f"- 等待任务：\n{wait_lines}\n"
        f"- 最近证据：\n{evidence_lines}"
    )


def format_run_list(runs: list[PursuitRun]) -> str:
    if not runs:
        return "当前没有目标追踪运行记录。"
    return "\n\n".join(format_run(run) for run in runs)


def _run_from_rows(
    row: sqlite3.Row,
    evidence_rows: list[sqlite3.Row],
    wait_rows: list[sqlite3.Row],
) -> PursuitRun:
    return PursuitRun(
        id=row["id"],
        goal=row["goal"],
        status=PursuitRunStatus(row["status"]),
        phase=row["phase"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        iteration=row["iteration"],
        criteria_total=row["criteria_total"],
        criteria_verified=row["criteria_verified"],
        failure_count=row["failure_count"],
        blocked_reason=row["blocked_reason"],
        next_action=row["next_action"],
        worktree_name=row["worktree_name"],
        worktree_path=row["worktree_path"],
        waiting_on=[
            PursuitBackgroundWait(
                task_id=item["task_id"],
                action_id=item["action_id"],
                command=item["command"],
                created_at=item["created_at"],
            )
            for item in wait_rows
        ],
        evidence=[
            PursuitEvidence(
                kind=item["kind"],
                source=item["source"],
                summary=item["summary"],
                is_hard=bool(item["is_hard"]),
                timestamp=item["timestamp"],
            )
            for item in evidence_rows
        ],
    )


def _status_label(status: PursuitRunStatus) -> str:
    return {
        PursuitRunStatus.RUNNING: "运行中",
        PursuitRunStatus.WAITING: "等待中",
        PursuitRunStatus.BLOCKED: "已阻塞",
        PursuitRunStatus.COMPLETED: "已完成",
        PursuitRunStatus.FAILED: "失败",
        PursuitRunStatus.CANCELLED: "已取消",
        PursuitRunStatus.BUDGET_EXCEEDED: "预算耗尽",
    }[status]
