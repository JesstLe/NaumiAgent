"""SQLite persistence for the local-first workbench."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import aiosqlite

from naumi_agent.workbench.models import (
    Approval,
    ApprovalState,
    ContextHealth,
    Decision,
    DecisionKind,
    FailureKind,
    IntentLock,
    IssueMetadata,
    Lease,
    LeaseState,
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

_CREATE_INTENT_LOCKS = """
CREATE TABLE IF NOT EXISTS workbench_intent_locks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    blocked_paths TEXT NOT NULL,
    allowed_paths TEXT NOT NULL,
    require_proposal_for_risk TEXT NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_LEASES = """
CREATE TABLE IF NOT EXISTS workbench_leases (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    worktree_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_CONTEXT_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS workbench_context_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    health TEXT NOT NULL,
    reasons TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_VALIDATION_RUNS = """
CREATE TABLE IF NOT EXISTS workbench_validation_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    output TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
)
"""

_CREATE_FAILURES = """
CREATE TABLE IF NOT EXISTS workbench_failures (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_APPROVALS = """
CREATE TABLE IF NOT EXISTS workbench_approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    requester TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision_note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        await db.execute(_CREATE_INTENT_LOCKS)
        await db.execute(_CREATE_LEASES)
        await db.execute(_CREATE_CONTEXT_SNAPSHOTS)
        await db.execute(_CREATE_VALIDATION_RUNS)
        await db.execute(_CREATE_FAILURES)
        await db.execute(_CREATE_APPROVALS)
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

    async def list_missions(self, session_id: str) -> list[Mission]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_missions
                   WHERE session_id = ?
                   ORDER BY created_at""",
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [
            Mission(
                id=row["id"],
                session_id=row["session_id"],
                title=row["title"],
                goal=row["goal"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

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

    async def set_issue_worktree(
        self,
        *,
        session_id: str,
        task_id: str,
        worktree_name: str,
    ) -> IssueMetadata | None:
        existing = await self.get_issue(session_id, task_id)
        if existing is None:
            return None
        return await self.upsert_issue(
            session_id=session_id,
            task_id=task_id,
            mission_id=existing.mission_id,
            parallel_mode=existing.parallel_mode,
            risk_level=existing.risk_level,
            requires_human_approval=existing.requires_human_approval,
            acceptance_criteria=existing.acceptance_criteria,
            expected_artifacts=existing.expected_artifacts,
            related_branch=existing.related_branch,
            related_worktree=worktree_name,
            related_pr=existing.related_pr,
        )

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

    async def add_intent_lock(
        self,
        *,
        session_id: str,
        mission_id: str,
        rule: str,
        blocked_paths: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        require_proposal_for_risk: RiskLevel = RiskLevel.HIGH,
    ) -> IntentLock:
        lock = IntentLock(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            rule=rule.strip(),
            blocked_paths=list(blocked_paths or []),
            allowed_paths=list(allowed_paths or []),
            require_proposal_for_risk=require_proposal_for_risk,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_intent_locks
                   (id, session_id, mission_id, rule, blocked_paths, allowed_paths,
                    require_proposal_for_risk, active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lock.id,
                    lock.session_id,
                    lock.mission_id,
                    lock.rule,
                    json.dumps(lock.blocked_paths, ensure_ascii=False),
                    json.dumps(lock.allowed_paths, ensure_ascii=False),
                    lock.require_proposal_for_risk.value,
                    1 if lock.active else 0,
                    lock.created_at,
                ),
            )
            await db.commit()
        return lock

    async def list_intent_locks(self, session_id: str, mission_id: str) -> list[IntentLock]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_intent_locks
                   WHERE session_id = ? AND mission_id = ?
                   ORDER BY created_at""",
                (session_id, mission_id),
            )
            rows = await cursor.fetchall()
        return [_row_to_intent_lock(dict(row)) for row in rows]

    async def create_lease(
        self,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
        expires_at: str,
        worktree_name: str = "",
    ) -> Lease:
        lease = Lease(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            state=LeaseState.ACTIVE,
            expires_at=expires_at,
            worktree_name=worktree_name,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_leases
                   (id, session_id, task_id, agent_id, state, expires_at, worktree_name,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lease.id,
                    lease.session_id,
                    lease.task_id,
                    lease.agent_id,
                    lease.state.value,
                    lease.expires_at,
                    lease.worktree_name,
                    lease.created_at,
                    lease.updated_at,
                ),
            )
            await db.commit()
        return lease

    async def get_active_lease(self, session_id: str, task_id: str) -> Lease | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_leases
                   WHERE session_id = ? AND task_id = ? AND state = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id, task_id, LeaseState.ACTIVE.value),
            )
            row = await cursor.fetchone()
        return _row_to_lease(dict(row)) if row else None

    async def update_lease_state(self, lease_id: str, state: LeaseState) -> Lease | None:
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                "UPDATE workbench_leases SET state = ?, updated_at = ? WHERE id = ?",
                (state.value, now, lease_id),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM workbench_leases WHERE id = ?", (lease_id,))
            row = await cursor.fetchone()
        return _row_to_lease(dict(row)) if row else None

    async def list_overdue_leases(self, session_id: str, now: str) -> list[Lease]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_leases
                   WHERE session_id = ? AND state = ? AND expires_at <= ?
                   ORDER BY expires_at""",
                (session_id, LeaseState.ACTIVE.value, now),
            )
            rows = await cursor.fetchall()
        return [_row_to_lease(dict(row)) for row in rows]

    async def force_lease_expiry_for_test(self, lease_id: str, expires_at: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                "UPDATE workbench_leases SET expires_at = ? WHERE id = ?",
                (expires_at, lease_id),
            )
            await db.commit()

    async def record_context_snapshot(
        self,
        *,
        session_id: str,
        agent_id: str,
        task_id: str,
        health: ContextHealth,
        reasons: list[str],
    ) -> dict[str, Any]:
        snapshot = {
            "id": uuid.uuid4().hex[:12],
            "session_id": session_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "health": health.value,
            "reasons": reasons,
            "created_at": now_iso(),
        }
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_context_snapshots
                   (id, session_id, agent_id, task_id, health, reasons, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["id"],
                    session_id,
                    agent_id,
                    task_id,
                    health.value,
                    json.dumps(reasons, ensure_ascii=False),
                    snapshot["created_at"],
                ),
            )
            await db.commit()
        return snapshot

    async def list_context_snapshots(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if task_id is not None:
                filters.append("task_id = ?")
                params.append(task_id)
            if agent_id is not None:
                filters.append("agent_id = ?")
                params.append(agent_id)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_context_snapshots
                   WHERE {where_clause}
                   ORDER BY created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in reversed(rows):
            snapshot = dict(row)
            snapshot["reasons"] = cast(list[str], json.loads(snapshot["reasons"]))
            snapshots.append(snapshot)
        return snapshots

    async def record_validation_run(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        command: list[str],
        cwd: str,
        status: str,
        exit_code: int,
        output: str,
        started_at: str,
        completed_at: str,
    ) -> dict[str, Any]:
        run = {
            "id": uuid.uuid4().hex[:12],
            "session_id": session_id,
            "task_id": task_id,
            "actor": actor,
            "command": command,
            "cwd": cwd,
            "status": status,
            "exit_code": exit_code,
            "output": output,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_validation_runs
                   (id, session_id, task_id, actor, command, cwd, status, exit_code,
                    output, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run["id"],
                    session_id,
                    task_id,
                    actor,
                    json.dumps(command, ensure_ascii=False),
                    cwd,
                    status,
                    exit_code,
                    output,
                    started_at,
                    completed_at,
                ),
            )
            await db.commit()
        return run

    async def list_validation_runs(
        self,
        session_id: str,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            if task_id is None:
                cursor = await db.execute(
                    """SELECT * FROM workbench_validation_runs
                       WHERE session_id = ?
                       ORDER BY completed_at DESC
                       LIMIT ?""",
                    (session_id, limit),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM workbench_validation_runs
                       WHERE session_id = ? AND task_id = ?
                       ORDER BY completed_at DESC
                       LIMIT ?""",
                    (session_id, task_id, limit),
                )
            rows = await cursor.fetchall()
        runs: list[dict[str, Any]] = []
        for row in reversed(rows):
            run = dict(row)
            run["command"] = cast(list[str], json.loads(run["command"]))
            runs.append(run)
        return runs

    async def create_failure(
        self,
        *,
        session_id: str,
        task_id: str,
        kind: FailureKind,
        title: str,
        detail: str,
        source_id: str,
    ) -> dict[str, Any]:
        failure = {
            "id": uuid.uuid4().hex[:12],
            "session_id": session_id,
            "task_id": task_id,
            "kind": kind.value,
            "title": title,
            "detail": detail,
            "source_id": source_id,
            "status": "open",
            "created_at": now_iso(),
        }
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_failures
                   (id, session_id, task_id, kind, title, detail, source_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    failure["id"],
                    session_id,
                    task_id,
                    failure["kind"],
                    title,
                    detail,
                    source_id,
                    "open",
                    failure["created_at"],
                ),
            )
            await db.commit()
        return failure

    async def list_failures(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_failures
                   WHERE session_id = ?
                   ORDER BY created_at DESC""",
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def add_approval(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        title: str,
        detail: str,
        requester: str,
        state: ApprovalState = ApprovalState.WAITING,
    ) -> Approval:
        now = now_iso()
        approval = Approval(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            task_id=task_id,
            state=state,
            title=title.strip(),
            detail=detail.strip(),
            requester=requester.strip(),
            reviewer="",
            decision_note="",
            created_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_approvals
                   (id, session_id, mission_id, task_id, state, title, detail,
                    requester, reviewer, decision_note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval.id,
                    approval.session_id,
                    approval.mission_id,
                    approval.task_id,
                    approval.state.value,
                    approval.title,
                    approval.detail,
                    approval.requester,
                    approval.reviewer,
                    approval.decision_note,
                    approval.created_at,
                    approval.updated_at,
                ),
            )
            await db.commit()
        return approval

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        state: ApprovalState,
        reviewer: str,
        decision_note: str,
    ) -> Approval | None:
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """UPDATE workbench_approvals
                   SET state = ?, reviewer = ?, decision_note = ?, updated_at = ?
                   WHERE id = ? AND session_id = ?""",
                (
                    state.value,
                    reviewer.strip(),
                    decision_note.strip(),
                    now,
                    approval_id,
                    session_id,
                ),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workbench_approvals WHERE id = ? AND session_id = ?",
                (approval_id, session_id),
            )
            row = await cursor.fetchone()
        return _row_to_approval(dict(row)) if row else None


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


def _row_to_intent_lock(row: dict[str, Any]) -> IntentLock:
    return IntentLock(
        id=row["id"],
        session_id=row["session_id"],
        mission_id=row["mission_id"],
        rule=row["rule"],
        blocked_paths=cast(list[str], json.loads(row["blocked_paths"])),
        allowed_paths=cast(list[str], json.loads(row["allowed_paths"])),
        require_proposal_for_risk=RiskLevel(row["require_proposal_for_risk"]),
        active=bool(row["active"]),
        created_at=row["created_at"],
    )


def _row_to_lease(row: dict[str, Any]) -> Lease:
    return Lease(
        id=row["id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        state=LeaseState(row["state"]),
        expires_at=row["expires_at"],
        worktree_name=row["worktree_name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_approval(row: dict[str, Any]) -> Approval:
    return Approval(
        id=row["id"],
        session_id=row["session_id"],
        mission_id=row["mission_id"],
        task_id=row["task_id"],
        state=ApprovalState(row["state"]),
        title=row["title"],
        detail=row["detail"],
        requester=row["requester"],
        reviewer=row["reviewer"],
        decision_note=row["decision_note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
