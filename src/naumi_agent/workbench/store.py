"""SQLite persistence for the local-first workbench."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import aiosqlite

from naumi_agent.workbench.models import (
    Decision,
    DecisionKind,
    IssueMetadata,
    Mission,
    ParallelMode,
    RiskLevel,
    WorkbenchEvent,
    now_iso,
)

_CREATE_MISSIONS = """
CREATE TABLE IF NOT EXISTS workbench_missions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_ISSUES = """
CREATE TABLE IF NOT EXISTS workbench_issues (
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    parallel_mode TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    requires_human_approval INTEGER NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    expected_artifacts TEXT NOT NULL,
    related_branch TEXT NOT NULL DEFAULT '',
    related_worktree TEXT NOT NULL DEFAULT '',
    related_pr TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, task_id)
)
"""

_CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS workbench_decisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS workbench_audit_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""


class WorkbenchStore:
    """SQLite-backed state for the workbench dashboard."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        if self._initialized:
            return
        await db.execute(_CREATE_MISSIONS)
        await db.execute(_CREATE_ISSUES)
        await db.execute(_CREATE_DECISIONS)
        await db.execute(_CREATE_EVENTS)
        await db.commit()
        self._initialized = True

    async def create_mission(self, session_id: str, title: str, goal: str) -> Mission:
        now = now_iso()
        mission = Mission(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            title=title.strip(),
            goal=goal.strip(),
            created_at=now,
            updated_at=now,
        )
        if not mission.title:
            raise ValueError("mission 标题不能为空")
        if not mission.goal:
            raise ValueError("mission 目标不能为空")
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_missions
                   (id, session_id, title, goal, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mission.id,
                    mission.session_id,
                    mission.title,
                    mission.goal,
                    mission.status,
                    mission.created_at,
                    mission.updated_at,
                ),
            )
            await db.commit()
        return mission

    async def upsert_issue(
        self,
        *,
        session_id: str,
        task_id: str,
        mission_id: str,
        parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        requires_human_approval: bool | None = None,
        acceptance_criteria: list[str] | None = None,
        expected_artifacts: list[str] | None = None,
        related_branch: str = "",
        related_worktree: str = "",
        related_pr: str = "",
    ) -> IssueMetadata:
        now = now_iso()
        issue = IssueMetadata(
            session_id=session_id,
            task_id=task_id,
            mission_id=mission_id,
            parallel_mode=parallel_mode,
            risk_level=risk_level,
            requires_human_approval=(
                risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}
                if requires_human_approval is None
                else requires_human_approval
            ),
            acceptance_criteria=list(acceptance_criteria or []),
            expected_artifacts=list(expected_artifacts or []),
            related_branch=related_branch,
            related_worktree=related_worktree,
            related_pr=related_pr,
            created_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT OR REPLACE INTO workbench_issues
                   (session_id, task_id, mission_id, parallel_mode, risk_level,
                    requires_human_approval, acceptance_criteria, expected_artifacts,
                    related_branch, related_worktree, related_pr, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    issue.session_id,
                    issue.task_id,
                    issue.mission_id,
                    issue.parallel_mode.value,
                    issue.risk_level.value,
                    1 if issue.requires_human_approval else 0,
                    json.dumps(issue.acceptance_criteria, ensure_ascii=False),
                    json.dumps(issue.expected_artifacts, ensure_ascii=False),
                    issue.related_branch,
                    issue.related_worktree,
                    issue.related_pr,
                    issue.created_at,
                    issue.updated_at,
                ),
            )
            await db.commit()
        return issue

    async def get_issue(self, session_id: str, task_id: str) -> IssueMetadata | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workbench_issues WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
            row = await cursor.fetchone()
        return _row_to_issue(dict(row)) if row else None

    async def add_decision(
        self,
        *,
        session_id: str,
        mission_id: str,
        kind: DecisionKind,
        title: str,
        content: str,
        actor: str,
    ) -> Decision:
        decision = Decision(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            kind=kind,
            title=title.strip(),
            content=content.strip(),
            actor=actor.strip(),
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_decisions
                   (id, session_id, mission_id, kind, title, content, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.id,
                    decision.session_id,
                    decision.mission_id,
                    decision.kind.value,
                    decision.title,
                    decision.content,
                    decision.actor,
                    decision.created_at,
                ),
            )
            await db.commit()
        return decision

    async def list_decisions(self, session_id: str, mission_id: str) -> list[Decision]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_decisions
                   WHERE session_id = ? AND mission_id = ?
                   ORDER BY created_at""",
                (session_id, mission_id),
            )
            rows = await cursor.fetchall()
        return [_row_to_decision(dict(row)) for row in rows]

    async def append_event(
        self,
        *,
        session_id: str,
        type: str,
        actor: str,
        subject_id: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkbenchEvent:
        event = WorkbenchEvent(
            session_id=session_id,
            type=type,
            actor=actor,
            subject_id=subject_id,
            payload=payload or {},
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_audit_events
                   (id, session_id, type, actor, subject_id, payload, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.session_id,
                    event.type,
                    event.actor,
                    event.subject_id,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.timestamp,
                ),
            )
            await db.commit()
        return event

    async def list_events(self, session_id: str, limit: int = 100) -> list[WorkbenchEvent]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_audit_events
                   WHERE session_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_event(dict(row)) for row in reversed(rows)]


def _row_to_issue(row: dict[str, Any]) -> IssueMetadata:
    return IssueMetadata(
        session_id=row["session_id"],
        task_id=row["task_id"],
        mission_id=row["mission_id"],
        parallel_mode=ParallelMode(row["parallel_mode"]),
        risk_level=RiskLevel(row["risk_level"]),
        requires_human_approval=bool(row["requires_human_approval"]),
        acceptance_criteria=cast(list[str], json.loads(row["acceptance_criteria"])),
        expected_artifacts=cast(list[str], json.loads(row["expected_artifacts"])),
        related_branch=row["related_branch"],
        related_worktree=row["related_worktree"],
        related_pr=row["related_pr"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_decision(row: dict[str, Any]) -> Decision:
    return Decision(
        id=row["id"],
        session_id=row["session_id"],
        mission_id=row["mission_id"],
        kind=DecisionKind(row["kind"]),
        title=row["title"],
        content=row["content"],
        actor=row["actor"],
        created_at=row["created_at"],
    )


def _row_to_event(row: dict[str, Any]) -> WorkbenchEvent:
    return WorkbenchEvent(
        id=row["id"],
        session_id=row["session_id"],
        type=row["type"],
        actor=row["actor"],
        subject_id=row["subject_id"],
        payload=cast(dict[str, Any], json.loads(row["payload"])),
        timestamp=row["timestamp"],
    )
