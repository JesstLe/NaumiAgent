"""SQLite persistence for the local-first workbench."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, cast

import aiosqlite

from naumi_agent.workbench.models import (
    AgentProfile,
    Approval,
    ApprovalState,
    ContextHealth,
    Decision,
    DecisionKind,
    DecisionStrength,
    EventSeverity,
    FailureKind,
    IntentLock,
    IssueBid,
    IssueMetadata,
    Lease,
    LeaseState,
    Mission,
    ParallelMode,
    ProposalSourceKind,
    ProposalState,
    RiskLevel,
    WorkbenchEvent,
    WorkbenchProposal,
    now_iso,
)

# Payload keys that likely carry secrets and must never be persisted in audit
# events. Matching is case-insensitive substring so it catches ``api_key``,
# ``authorization``, ``bearer_token``, etc.
_SENSITIVE_PAYLOAD_KEYS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "bearer",
    "auth",
    "authorization",
    "private_key",
)


def _is_sensitive_payload_key(key: str) -> bool:
    """True when a payload key likely holds a secret."""
    lower = key.lower()
    return any(term in lower for term in _SENSITIVE_PAYLOAD_KEYS)


def _redact_payload(value: Any) -> Any:
    """Recursively replace sensitive-looking values with a redaction marker."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_payload_key(key) else _redact_payload(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an audit-event payload with secrets redacted.

    Used at ``append_event`` time so secrets never reach SQLite, and at export
    time for defense-in-depth.
    """
    return _redact_payload(payload)  # type: ignore[return-value]


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

_CREATE_AGENT_PROFILES = """
CREATE TABLE IF NOT EXISTS workbench_agent_profiles (
    id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    permissions TEXT NOT NULL,
    max_parallel_tasks INTEGER NOT NULL,
    status TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
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
    strength TEXT NOT NULL DEFAULT 'required',
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
    timestamp TEXT NOT NULL,
    correlation_id TEXT,
    parent_event_id TEXT,
    severity TEXT NOT NULL DEFAULT 'info'
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
    created_by TEXT NOT NULL DEFAULT 'Human',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

_CREATE_BIDS = """
CREATE TABLE IF NOT EXISTS workbench_bids (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    estimate_minutes INTEGER NOT NULL,
    eta TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


_CREATE_PROPOSALS = """
CREATE TABLE IF NOT EXISTS workbench_proposals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    impact_scope TEXT NOT NULL,
    intended_files TEXT NOT NULL DEFAULT '[]',
    validation_plan TEXT NOT NULL DEFAULT '[]',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    questions TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open',
    decision_note TEXT NOT NULL DEFAULT '',
    converted_issue_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    source_id TEXT NOT NULL DEFAULT '',
    source_revision INTEGER NOT NULL DEFAULT 0,
    source_occurrence_count INTEGER NOT NULL DEFAULT 0,
    source_sha256 TEXT NOT NULL DEFAULT '',
    source_proposal_id TEXT NOT NULL DEFAULT '',
    generator_version TEXT NOT NULL DEFAULT '',
    proposal_kind TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL DEFAULT '',
    decision_at TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL DEFAULT '',
    merged_into_id TEXT NOT NULL DEFAULT '',
    governance_policy_version TEXT NOT NULL DEFAULT '',
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
        await db.execute(_CREATE_AGENT_PROFILES)
        await db.execute(_CREATE_DECISIONS)
        await db.execute(_CREATE_EVENTS)
        await db.execute(_CREATE_INTENT_LOCKS)
        await db.execute(_CREATE_LEASES)
        await db.execute(_CREATE_CONTEXT_SNAPSHOTS)
        await db.execute(_CREATE_VALIDATION_RUNS)
        await db.execute(_CREATE_FAILURES)
        await db.execute(_CREATE_APPROVALS)
        await db.execute(_CREATE_BIDS)
        await db.execute(_CREATE_PROPOSALS)
        await self._migrate_columns(db)
        await db.commit()
        self._initialized = True

    async def _migrate_columns(self, db: aiosqlite.Connection) -> None:
        """Idempotently adds columns introduced after the initial schema.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` does not add new columns to an
        existing table, so each additive migration checks ``PRAGMA table_info``
        before issuing a ``ADD COLUMN`` with a safe default.
        """
        await self._ensure_column(
            db, "workbench_decisions", "strength", "TEXT NOT NULL DEFAULT 'required'"
        )
        await self._ensure_column(
            db, "workbench_intent_locks", "created_by", "TEXT NOT NULL DEFAULT 'Human'"
        )
        await self._ensure_column(
            db, "workbench_intent_locks", "updated_at", "TEXT NOT NULL DEFAULT ''"
        )
        await self._ensure_column(
            db,
            "workbench_agent_profiles",
            "last_heartbeat_at",
            "TEXT NOT NULL DEFAULT ''",
        )
        await self._ensure_column(
            db, "workbench_audit_events", "correlation_id", "TEXT"
        )
        await self._ensure_column(
            db, "workbench_audit_events", "parent_event_id", "TEXT"
        )
        await self._ensure_column(
            db, "workbench_audit_events", "severity", "TEXT NOT NULL DEFAULT 'info'"
        )
        proposal_columns = {
            "source_kind": "TEXT NOT NULL DEFAULT 'manual'",
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "source_revision": "INTEGER NOT NULL DEFAULT 0",
            "source_occurrence_count": "INTEGER NOT NULL DEFAULT 0",
            "source_sha256": "TEXT NOT NULL DEFAULT ''",
            "source_proposal_id": "TEXT NOT NULL DEFAULT ''",
            "generator_version": "TEXT NOT NULL DEFAULT ''",
            "proposal_kind": "TEXT NOT NULL DEFAULT ''",
            "idempotency_key": "TEXT NOT NULL DEFAULT ''",
            "reviewer": "TEXT NOT NULL DEFAULT ''",
            "decision_at": "TEXT NOT NULL DEFAULT ''",
            "cooldown_until": "TEXT NOT NULL DEFAULT ''",
            "merged_into_id": "TEXT NOT NULL DEFAULT ''",
            "governance_policy_version": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in proposal_columns.items():
            await self._ensure_column(db, "workbench_proposals", column, definition)
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS
               idx_workbench_proposals_session_idempotency
               ON workbench_proposals(session_id, idempotency_key)
               WHERE idempotency_key <> ''"""
        )

    async def _ensure_column(
        self, db: aiosqlite.Connection, table: str, column: str, definition: str
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

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

    async def list_missions(
        self,
        session_id: str,
        status: str | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Mission]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row

            conditions = ["session_id = ?"]
            params: list[Any] = [session_id]
            if status is not None:
                conditions.append("status = ?")
                params.append(status)

            where_clause = " AND ".join(conditions)
            order_clause = "created_at DESC" if newest_first else "created_at"
            limit_clause = " LIMIT ?" if limit is not None else ""
            if limit is not None:
                params.append(limit)

            cursor = await db.execute(
                f"""SELECT * FROM workbench_missions
                   WHERE {where_clause}
                   ORDER BY {order_clause}{limit_clause}""",
                tuple(params),
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

    async def list_issues(
        self,
        session_id: str,
        mission_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
    ) -> list[IssueMetadata]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if mission_id is not None:
                filters.append("mission_id = ?")
                params.append(mission_id)
            if risk_level is not None:
                filters.append("risk_level = ?")
                params.append(risk_level)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_issues
                   WHERE {where_clause}
                   ORDER BY created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_issue(dict(row)) for row in rows]

    async def upsert_agent_profile(
        self,
        *,
        session_id: str,
        agent_id: str,
        name: str,
        role: str,
        capabilities: list[str] | None = None,
        permissions: list[str] | None = None,
        max_parallel_tasks: int = 1,
        status: str = "idle",
    ) -> AgentProfile:
        cleaned_id = agent_id.strip()
        cleaned_name = name.strip()
        cleaned_role = role.strip()
        cleaned_status = status.strip() or "idle"
        if not cleaned_id:
            raise ValueError("agent_id 不能为空")
        if not cleaned_name:
            raise ValueError("Agent 名称不能为空")
        if not cleaned_role:
            raise ValueError("Agent 角色不能为空")
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks 必须大于 0")

        now = now_iso()
        profile = AgentProfile(
            id=cleaned_id,
            session_id=session_id,
            name=cleaned_name,
            role=cleaned_role,
            capabilities=[item.strip() for item in (capabilities or []) if item.strip()],
            permissions=[item.strip() for item in (permissions or []) if item.strip()],
            max_parallel_tasks=max_parallel_tasks,
            status=cleaned_status,
            updated_at=now,
        )
        existing = await self.get_agent_profile(session_id, cleaned_id)
        if existing is not None:
            profile.created_at = existing.created_at

        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT OR REPLACE INTO workbench_agent_profiles
                   (id, session_id, name, role, capabilities, permissions,
                    max_parallel_tasks, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.id,
                    profile.session_id,
                    profile.name,
                    profile.role,
                    json.dumps(profile.capabilities, ensure_ascii=False),
                    json.dumps(profile.permissions, ensure_ascii=False),
                    profile.max_parallel_tasks,
                    profile.status,
                    profile.created_at,
                    profile.updated_at,
                ),
            )
            await db.commit()
        return profile

    async def get_agent_profile(
        self, session_id: str, agent_id: str
    ) -> AgentProfile | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_agent_profiles
                   WHERE session_id = ? AND id = ?""",
                (session_id, agent_id),
            )
            row = await cursor.fetchone()
        return _row_to_agent_profile(dict(row)) if row else None

    async def list_agent_profiles(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AgentProfile]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if status is not None:
                filters.append("status = ?")
                params.append(status)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_agent_profiles
                   WHERE {where_clause}
                   ORDER BY updated_at DESC, created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_agent_profile(dict(row)) for row in rows]

    async def record_agent_heartbeat(
        self, session_id: str, agent_id: str
    ) -> AgentProfile | None:
        """Records a heartbeat for an agent and returns the updated profile."""
        profile = await self.get_agent_profile(session_id, agent_id)
        if profile is None:
            return None
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """UPDATE workbench_agent_profiles
                   SET last_heartbeat_at = ?, updated_at = ?
                   WHERE session_id = ? AND id = ?""",
                (now, now, session_id, agent_id),
            )
            await db.commit()
        return await self.get_agent_profile(session_id, agent_id)

    async def get_agent_active_lease(
        self, session_id: str, agent_id: str
    ) -> Lease | None:
        """Returns the newest active lease for the given agent, if any."""
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_leases
                   WHERE session_id = ? AND agent_id = ? AND state = ?
                   ORDER BY updated_at DESC, created_at DESC LIMIT 1""",
                (session_id, agent_id, LeaseState.ACTIVE.value),
            )
            row = await cursor.fetchone()
        return _row_to_lease(dict(row)) if row else None

    async def add_decision(
        self,
        *,
        session_id: str,
        mission_id: str,
        kind: DecisionKind,
        title: str,
        content: str,
        actor: str,
        strength: DecisionStrength = DecisionStrength.REQUIRED,
    ) -> Decision:
        decision = Decision(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            kind=kind,
            title=title.strip(),
            content=content.strip(),
            actor=actor.strip(),
            strength=strength,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_decisions
                   (id, session_id, mission_id, kind, title, content, actor,
                    strength, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.id,
                    decision.session_id,
                    decision.mission_id,
                    decision.kind.value,
                    decision.title,
                    decision.content,
                    decision.actor,
                    decision.strength.value,
                    decision.created_at,
                ),
            )
            await db.commit()
        return decision

    async def list_decisions(
        self,
        session_id: str,
        mission_id: str,
        kind: DecisionKind | str | None = None,
    ) -> list[Decision]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            filters = ["session_id = ?", "mission_id = ?"]
            params: list[Any] = [session_id, mission_id]
            if kind is not None:
                filters.append("kind = ?")
                params.append(kind.value if isinstance(kind, DecisionKind) else kind)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_decisions
                   WHERE {where_clause}
                   ORDER BY created_at""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_decision(dict(row)) for row in rows]

    async def get_decision(
        self, session_id: str, mission_id: str, decision_id: str
    ) -> Decision | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_decisions
                   WHERE session_id = ? AND mission_id = ? AND id = ?""",
                (session_id, mission_id, decision_id),
            )
            row = await cursor.fetchone()
        return _row_to_decision(dict(row)) if row else None

    async def append_event(
        self,
        *,
        session_id: str,
        type: str,
        actor: str,
        subject_id: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> WorkbenchEvent:
        event = WorkbenchEvent(
            session_id=session_id,
            type=type,
            actor=actor,
            subject_id=subject_id,
            payload=redact_event_payload(payload or {}),
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            severity=severity,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_audit_events
                   (id, session_id, type, actor, subject_id, payload, timestamp,
                    correlation_id, parent_event_id, severity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.session_id,
                    event.type,
                    event.actor,
                    event.subject_id,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.timestamp,
                    event.correlation_id,
                    event.parent_event_id,
                    event.severity.value,
                ),
            )
            await db.commit()
        return event

    async def list_events(
        self,
        session_id: str,
        limit: int = 100,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        severity: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> list[WorkbenchEvent]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if event_type is not None:
                filters.append("type = ?")
                params.append(event_type)
            if subject_id is not None:
                filters.append("subject_id = ?")
                params.append(subject_id)
            if actor is not None:
                filters.append("actor = ?")
                params.append(actor)
            if since is not None:
                filters.append("timestamp > ?")
                params.append(since)
            if severity is not None:
                filters.append("severity = ?")
                params.append(severity)
            if correlation_id is not None:
                filters.append("correlation_id = ?")
                params.append(correlation_id)
            if parent_event_id is not None:
                filters.append("parent_event_id = ?")
                params.append(parent_event_id)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_audit_events
                   WHERE {where_clause}
                   ORDER BY timestamp DESC, rowid DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_event(dict(row)) for row in rows]

    async def get_event(self, session_id: str, event_id: str) -> WorkbenchEvent | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_audit_events
                   WHERE session_id = ? AND id = ?""",
                (session_id, event_id),
            )
            row = await cursor.fetchone()
        return _row_to_event(dict(row)) if row else None

    async def add_intent_lock(
        self,
        *,
        session_id: str,
        mission_id: str,
        rule: str,
        blocked_paths: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        require_proposal_for_risk: RiskLevel = RiskLevel.HIGH,
        active: bool = True,
        created_by: str = "Human",
    ) -> IntentLock:
        now = now_iso()
        lock = IntentLock(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            rule=rule.strip(),
            blocked_paths=list(blocked_paths or []),
            allowed_paths=list(allowed_paths or []),
            require_proposal_for_risk=require_proposal_for_risk,
            active=active,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_intent_locks
                   (id, session_id, mission_id, rule, blocked_paths, allowed_paths,
                    require_proposal_for_risk, active, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lock.id,
                    lock.session_id,
                    lock.mission_id,
                    lock.rule,
                    json.dumps(lock.blocked_paths, ensure_ascii=False),
                    json.dumps(lock.allowed_paths, ensure_ascii=False),
                    lock.require_proposal_for_risk.value,
                    1 if lock.active else 0,
                    lock.created_by,
                    lock.created_at,
                    lock.updated_at,
                ),
            )
            await db.commit()
        return lock

    async def deactivate_intent_lock(
        self, session_id: str, lock_id: str
    ) -> IntentLock | None:
        """Marks an intent lock inactive so it no longer blocks actions.

        Returns the updated lock, or ``None`` when no matching lock exists.
        """
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                """UPDATE workbench_intent_locks
                   SET active = 0, updated_at = ?
                   WHERE session_id = ? AND id = ?""",
                (now, session_id, lock_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return None
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_intent_locks
                   WHERE session_id = ? AND id = ?""",
                (session_id, lock_id),
            )
            row = await cursor.fetchone()
        return _row_to_intent_lock(dict(row)) if row else None

    async def list_intent_locks(
        self,
        session_id: str,
        mission_id: str,
        active: bool | None = None,
    ) -> list[IntentLock]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id, mission_id]
            filters = ["session_id = ?", "mission_id = ?"]
            if active is not None:
                filters.append("active = ?")
                params.append(1 if active else 0)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_intent_locks
                   WHERE {where_clause}
                   ORDER BY created_at""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_intent_lock(dict(row)) for row in rows]

    async def get_intent_lock(
        self, session_id: str, mission_id: str, lock_id: str
    ) -> IntentLock | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_intent_locks
                   WHERE session_id = ? AND mission_id = ? AND id = ?""",
                (session_id, mission_id, lock_id),
            )
            row = await cursor.fetchone()
        return _row_to_intent_lock(dict(row)) if row else None

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

    async def update_lease_state(
        self,
        lease_id: str,
        state: LeaseState,
        *,
        session_id: str | None = None,
    ) -> Lease | None:
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            if session_id is None:
                await db.execute(
                    "UPDATE workbench_leases SET state = ?, updated_at = ? WHERE id = ?",
                    (state.value, now, lease_id),
                )
            else:
                await db.execute(
                    """UPDATE workbench_leases
                       SET state = ?, updated_at = ?
                       WHERE id = ? AND session_id = ?""",
                    (state.value, now, lease_id, session_id),
                )
            await db.commit()
            db.row_factory = aiosqlite.Row
            if session_id is None:
                cursor = await db.execute(
                    "SELECT * FROM workbench_leases WHERE id = ?", (lease_id,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM workbench_leases WHERE id = ? AND session_id = ?",
                    (lease_id, session_id),
                )
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

    async def list_leases(
        self,
        session_id: str,
        state: LeaseState | str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[Lease]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if state is not None:
                filters.append("state = ?")
                params.append(state.value if isinstance(state, LeaseState) else state)
            if task_id is not None:
                filters.append("task_id = ?")
                params.append(task_id)
            if agent_id is not None:
                filters.append("agent_id = ?")
                params.append(agent_id)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_leases
                   WHERE {where_clause}
                   ORDER BY updated_at DESC, created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_lease(dict(row)) for row in rows]

    async def get_lease(self, session_id: str, lease_id: str) -> Lease | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_leases
                   WHERE session_id = ? AND id = ?""",
                (session_id, lease_id),
            )
            row = await cursor.fetchone()
        return _row_to_lease(dict(row)) if row else None

    async def force_lease_expiry_for_test(self, lease_id: str, expires_at: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                "UPDATE workbench_leases SET expires_at = ? WHERE id = ?",
                (expires_at, lease_id),
            )
            await db.commit()

    async def create_bid(
        self,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
        confidence: float,
        estimate_minutes: int,
        eta: str,
        note: str,
    ) -> IssueBid:
        """Persist a new agent bid for an issue (task)."""
        confidence = max(0.0, min(1.0, float(confidence)))
        estimate_minutes = max(0, int(estimate_minutes))
        bid = IssueBid(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            confidence=confidence,
            estimate_minutes=estimate_minutes,
            eta=eta,
            note=note,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            await db.execute(
                """INSERT INTO workbench_bids
                   (id, session_id, task_id, agent_id, confidence, estimate_minutes,
                    eta, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bid.id,
                    bid.session_id,
                    bid.task_id,
                    bid.agent_id,
                    bid.confidence,
                    bid.estimate_minutes,
                    bid.eta,
                    bid.note,
                    bid.created_at,
                    bid.updated_at,
                ),
            )
            await db.commit()
        return bid

    async def list_bids(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[IssueBid]:
        """List bids, optionally filtered by task and/or agent."""
        filters = ["session_id = ?"]
        params: list[Any] = [session_id]
        if task_id is not None:
            filters.append("task_id = ?")
            params.append(task_id)
        if agent_id is not None:
            filters.append("agent_id = ?")
            params.append(agent_id)
        params.append(limit)
        where_clause = " AND ".join(filters)
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM workbench_bids
                   WHERE {where_clause}
                   ORDER BY created_at ASC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_bid(dict(row)) for row in rows]

    async def list_bids_for_snapshot(
        self, session_id: str, task_ids: list[str]
    ) -> list[IssueBid]:
        """Fetch bids for a snapshot's tasks in one query.

        Used by the snapshot builder so the market can surface competing bids
        without an N+1 round-trip per task.
        """
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        params: list[Any] = [session_id, *task_ids]
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM workbench_bids
                   WHERE session_id = ? AND task_id IN ({placeholders})
                   ORDER BY created_at ASC""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_bid(dict(row)) for row in rows]

    async def create_proposal(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        agent_id: str,
        title: str,
        impact_scope: str,
        intended_files: list[str] | None = None,
        validation_plan: list[str] | None = None,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        questions: list[str] | None = None,
        source_kind: ProposalSourceKind = ProposalSourceKind.MANUAL,
        source_id: str = "",
        source_revision: int = 0,
        source_occurrence_count: int = 0,
        source_sha256: str = "",
        source_proposal_id: str = "",
        generator_version: str = "",
        proposal_kind: str = "",
        idempotency_key: str = "",
    ) -> WorkbenchProposal:
        """Persist a new human-governed proposal."""
        proposal, _ = await self.create_proposal_with_status(
            session_id=session_id,
            mission_id=mission_id,
            task_id=task_id,
            agent_id=agent_id,
            title=title,
            impact_scope=impact_scope,
            intended_files=intended_files,
            validation_plan=validation_plan,
            risk_level=risk_level,
            questions=questions,
            source_kind=source_kind,
            source_id=source_id,
            source_revision=source_revision,
            source_occurrence_count=source_occurrence_count,
            source_sha256=source_sha256,
            source_proposal_id=source_proposal_id,
            generator_version=generator_version,
            proposal_kind=proposal_kind,
            idempotency_key=idempotency_key,
        )
        return proposal

    async def create_proposal_with_status(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        agent_id: str,
        title: str,
        impact_scope: str,
        intended_files: list[str] | None = None,
        validation_plan: list[str] | None = None,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        questions: list[str] | None = None,
        source_kind: ProposalSourceKind = ProposalSourceKind.MANUAL,
        source_id: str = "",
        source_revision: int = 0,
        source_occurrence_count: int = 0,
        source_sha256: str = "",
        source_proposal_id: str = "",
        generator_version: str = "",
        proposal_kind: str = "",
        idempotency_key: str = "",
    ) -> tuple[WorkbenchProposal, bool]:
        """Create a proposal once, returning whether this call inserted it."""
        _validate_proposal_provenance(
            source_kind=source_kind,
            source_id=source_id,
            source_revision=source_revision,
            source_occurrence_count=source_occurrence_count,
            source_sha256=source_sha256,
            source_proposal_id=source_proposal_id,
            generator_version=generator_version,
            proposal_kind=proposal_kind,
            idempotency_key=idempotency_key,
        )
        proposal = WorkbenchProposal(
            id=uuid.uuid4().hex[:12],
            session_id=session_id,
            mission_id=mission_id,
            task_id=task_id,
            agent_id=agent_id,
            title=title,
            impact_scope=impact_scope,
            intended_files=list(intended_files or []),
            validation_plan=list(validation_plan or []),
            risk_level=risk_level,
            questions=list(questions or []),
            source_kind=source_kind,
            source_id=source_id,
            source_revision=source_revision,
            source_occurrence_count=source_occurrence_count,
            source_sha256=source_sha256,
            source_proposal_id=source_proposal_id,
            generator_version=generator_version,
            proposal_kind=proposal_kind,
            idempotency_key=idempotency_key,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                """INSERT INTO workbench_proposals
                   (id, session_id, mission_id, task_id, agent_id, title,
                    impact_scope, intended_files, validation_plan, risk_level,
                    questions, state, decision_note, converted_issue_id,
                    source_kind, source_id, source_revision, source_occurrence_count,
                    source_sha256,
                    source_proposal_id, generator_version, proposal_kind,
                    idempotency_key, reviewer, decision_at, cooldown_until,
                    merged_into_id, governance_policy_version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, idempotency_key)
                   WHERE idempotency_key <> '' DO NOTHING""",
                (
                    proposal.id,
                    proposal.session_id,
                    proposal.mission_id,
                    proposal.task_id,
                    proposal.agent_id,
                    proposal.title,
                    proposal.impact_scope,
                    json.dumps(proposal.intended_files, ensure_ascii=False),
                    json.dumps(proposal.validation_plan, ensure_ascii=False),
                    proposal.risk_level.value,
                    json.dumps(proposal.questions, ensure_ascii=False),
                    proposal.state.value,
                    proposal.decision_note,
                    proposal.converted_issue_id,
                    proposal.source_kind.value,
                    proposal.source_id,
                    proposal.source_revision,
                    proposal.source_occurrence_count,
                    proposal.source_sha256,
                    proposal.source_proposal_id,
                    proposal.generator_version,
                    proposal.proposal_kind,
                    proposal.idempotency_key,
                    proposal.reviewer,
                    proposal.decision_at,
                    proposal.cooldown_until,
                    proposal.merged_into_id,
                    proposal.governance_policy_version,
                    proposal.created_at,
                    proposal.updated_at,
                ),
            )
            await db.commit()
            if cursor.rowcount == 1:
                return proposal, True
            db.row_factory = aiosqlite.Row
            existing_cursor = await db.execute(
                """SELECT * FROM workbench_proposals
                   WHERE session_id = ? AND idempotency_key = ?""",
                (session_id, idempotency_key),
            )
            row = await existing_cursor.fetchone()
        if row is None:
            raise RuntimeError("Proposal 幂等写入失败，且无法读取已有记录。")
        existing = _row_to_proposal(dict(row))
        if _proposal_identity(existing) != _proposal_identity(proposal):
            raise ValueError("Proposal 幂等键已绑定到不同内容，拒绝覆盖。")
        return existing, False

    async def list_proposals(
        self,
        session_id: str,
        *,
        mission_id: str | None = None,
        task_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[WorkbenchProposal]:
        """List proposals, optionally filtered by mission, task, and/or state."""
        filters = ["session_id = ?"]
        params: list[Any] = [session_id]
        if mission_id is not None:
            filters.append("mission_id = ?")
            params.append(mission_id)
        if task_id is not None:
            filters.append("task_id = ?")
            params.append(task_id)
        if state is not None:
            filters.append("state = ?")
            params.append(state)
        params.append(limit)
        where_clause = " AND ".join(filters)
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM workbench_proposals
                   WHERE {where_clause}
                   ORDER BY created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_proposal(dict(row)) for row in rows]

    async def get_proposal(
        self, session_id: str, proposal_id: str
    ) -> WorkbenchProposal | None:
        """Fetch a single proposal by id."""
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_proposals
                   WHERE session_id = ? AND id = ?""",
                (session_id, proposal_id),
            )
            row = await cursor.fetchone()
        return _row_to_proposal(dict(row)) if row else None

    async def update_proposal_state(
        self,
        session_id: str,
        proposal_id: str,
        *,
        state: ProposalState,
        decision_note: str = "",
        converted_issue_id: str = "",
    ) -> WorkbenchProposal | None:
        """Transition a proposal to a new state (approve/reject/convert).

        Only OPEN proposals may be transitioned; resolving an already-decided
        proposal is a no-op returning the current record unchanged.
        """
        existing = await self.get_proposal(session_id, proposal_id)
        if existing is None:
            return None
        if existing.state is not ProposalState.OPEN:
            return existing
        now = now_iso()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                """UPDATE workbench_proposals
                   SET state = ?, decision_note = ?, converted_issue_id = ?,
                       updated_at = ?
                   WHERE session_id = ? AND id = ? AND state = 'open'""",
                (
                    state.value,
                    decision_note,
                    converted_issue_id,
                    now,
                    session_id,
                    proposal_id,
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return await self.get_proposal(session_id, proposal_id)
        return await self.get_proposal(session_id, proposal_id)

    async def transition_proposal_state(
        self,
        session_id: str,
        proposal_id: str,
        *,
        expected_states: set[ProposalState],
        state: ProposalState,
        reviewer: str,
        decision_note: str,
        decision_at: str,
        cooldown_until: str = "",
        merged_into_id: str = "",
        governance_policy_version: str,
    ) -> tuple[WorkbenchProposal | None, bool]:
        """Atomically apply one governance transition with compare-and-swap."""
        if not expected_states:
            raise ValueError("Proposal transition 必须声明 expected state。")
        placeholders = ",".join("?" for _ in expected_states)
        ordered_states = sorted(item.value for item in expected_states)
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                f"""UPDATE workbench_proposals
                    SET state = ?, reviewer = ?, decision_note = ?, decision_at = ?,
                        cooldown_until = ?, merged_into_id = ?,
                        governance_policy_version = ?, updated_at = ?
                    WHERE session_id = ? AND id = ?
                      AND state IN ({placeholders})""",
                (
                    state.value,
                    reviewer,
                    decision_note,
                    decision_at,
                    cooldown_until,
                    merged_into_id,
                    governance_policy_version,
                    now_iso(),
                    session_id,
                    proposal_id,
                    *ordered_states,
                ),
            )
            await db.commit()
        return await self.get_proposal(session_id, proposal_id), cursor.rowcount == 1

    async def latest_proposal_for_source(
        self,
        *,
        source_kind: ProposalSourceKind,
        source_id: str,
    ) -> WorkbenchProposal | None:
        """Return the newest durable governance record for one source identity."""
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_proposals
                   WHERE source_kind = ? AND source_id = ?
                   ORDER BY source_revision DESC, updated_at DESC, rowid DESC
                   LIMIT 1""",
                (source_kind.value, source_id),
            )
            row = await cursor.fetchone()
        return _row_to_proposal(dict(row)) if row else None

    async def latest_proposals_for_sources(
        self,
        *,
        source_kind: ProposalSourceKind,
        source_ids: list[str],
    ) -> dict[str, WorkbenchProposal]:
        """Return one newest durable governance record per bounded source id."""
        clean_ids = sorted({value.strip() for value in source_ids if value.strip()})
        if len(clean_ids) > 500:
            raise ValueError("批量治理查询最多支持 500 个 Candidate。")
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM (
                       SELECT p.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY source_id
                                  ORDER BY source_revision DESC, updated_at DESC, rowid DESC
                              ) AS source_rank
                       FROM workbench_proposals AS p
                       WHERE source_kind = ? AND source_id IN ({placeholders})
                   ) AS ranked
                   WHERE source_rank = 1""",
                (source_kind.value, *clean_ids),
            )
            rows = await cursor.fetchall()
        return {
            str(row["source_id"]): _row_to_proposal(dict(row))
            for row in rows
        }

    async def list_proposals_for_snapshot(
        self, session_id: str
    ) -> list[WorkbenchProposal]:
        """Fetch all proposals for a snapshot (newest first)."""
        return await self.list_proposals(session_id, limit=200)

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
        health: str | None = None,
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
            if health is not None:
                filters.append("health = ?")
                params.append(health)
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
        return [self._context_snapshot_from_row(row) for row in reversed(rows)]

    @staticmethod
    def _context_snapshot_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        snapshot = dict(row)
        snapshot["reasons"] = cast(list[str], json.loads(snapshot["reasons"]))
        return snapshot

    async def get_context_snapshot(
        self,
        session_id: str,
        snapshot_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_context_snapshots
                   WHERE session_id = ? AND id = ?""",
                (session_id, snapshot_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._context_snapshot_from_row(row)

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

    @staticmethod
    def _validation_run_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        run = dict(row)
        run["command"] = cast(list[str], json.loads(run["command"]))
        return run

    async def list_validation_runs(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            filters = ["session_id = ?"]
            params: list[Any] = [session_id]
            if task_id is not None:
                filters.append("task_id = ?")
                params.append(task_id)
            if status is not None:
                filters.append("status = ?")
                params.append(status)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_validation_runs
                   WHERE {where_clause}
                   ORDER BY completed_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        runs: list[dict[str, Any]] = []
        for row in reversed(rows):
            runs.append(self._validation_run_from_row(row))
        return runs

    async def get_validation_run(
        self,
        session_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_validation_runs
                   WHERE session_id = ? AND id = ?""",
                (session_id, run_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._validation_run_from_row(row)

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

    async def list_failures(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
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
            if status is not None:
                filters.append("status = ?")
                params.append(status)
            if kind is not None:
                filters.append("kind = ?")
                params.append(kind)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_failures
                   WHERE {where_clause}
                   ORDER BY created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_failure(
        self,
        session_id: str,
        failure_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_failures
                   WHERE session_id = ? AND id = ?""",
                (session_id, failure_id),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

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

    async def list_approvals(
        self,
        session_id: str,
        state: ApprovalState | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            params: list[Any] = [session_id]
            filters = ["session_id = ?"]
            if state is not None:
                filters.append("state = ?")
                params.append(state.value)
            if mission_id is not None:
                filters.append("mission_id = ?")
                params.append(mission_id)
            if task_id is not None:
                filters.append("task_id = ?")
                params.append(task_id)
            params.append(limit)
            where_clause = " AND ".join(filters)
            cursor = await db.execute(
                f"""SELECT * FROM workbench_approvals
                   WHERE {where_clause}
                   ORDER BY updated_at DESC, created_at DESC
                   LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [_row_to_approval(dict(row)) for row in rows]

    async def get_approval(self, session_id: str, approval_id: str) -> Approval | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM workbench_approvals
                   WHERE session_id = ? AND id = ?""",
                (session_id, approval_id),
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


def _row_to_agent_profile(row: dict[str, Any]) -> AgentProfile:
    return AgentProfile(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        role=row["role"],
        capabilities=cast(list[str], json.loads(row["capabilities"])),
        permissions=cast(list[str], json.loads(row["permissions"])),
        max_parallel_tasks=row["max_parallel_tasks"],
        status=row["status"],
        last_heartbeat_at=row.get("last_heartbeat_at", ""),
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
        strength=DecisionStrength(row.get("strength") or DecisionStrength.REQUIRED.value),
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
        correlation_id=row.get("correlation_id"),
        parent_event_id=row.get("parent_event_id"),
        severity=EventSeverity(row.get("severity") or "info"),
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
        created_by=row.get("created_by") or "Human",
        created_at=row["created_at"],
        updated_at=row.get("updated_at") or row["created_at"],
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


def _row_to_bid(row: dict[str, Any]) -> IssueBid:
    return IssueBid(
        id=row["id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        confidence=float(row["confidence"]),
        estimate_minutes=int(row["estimate_minutes"]),
        eta=row["eta"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_proposal(row: dict[str, Any]) -> WorkbenchProposal:
    return WorkbenchProposal(
        id=row["id"],
        session_id=row["session_id"],
        mission_id=row["mission_id"],
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        title=row["title"],
        impact_scope=row["impact_scope"],
        intended_files=json.loads(row.get("intended_files") or "[]"),
        validation_plan=json.loads(row.get("validation_plan") or "[]"),
        risk_level=RiskLevel(row.get("risk_level") or "medium"),
        questions=json.loads(row.get("questions") or "[]"),
        state=ProposalState(row.get("state") or "open"),
        decision_note=row.get("decision_note") or "",
        converted_issue_id=row.get("converted_issue_id") or "",
        source_kind=ProposalSourceKind(row.get("source_kind") or "manual"),
        source_id=row.get("source_id") or "",
        source_revision=int(row.get("source_revision") or 0),
        source_occurrence_count=int(row.get("source_occurrence_count") or 0),
        source_sha256=row.get("source_sha256") or "",
        source_proposal_id=row.get("source_proposal_id") or "",
        generator_version=row.get("generator_version") or "",
        proposal_kind=row.get("proposal_kind") or "",
        idempotency_key=row.get("idempotency_key") or "",
        reviewer=row.get("reviewer") or "",
        decision_at=row.get("decision_at") or "",
        cooldown_until=row.get("cooldown_until") or "",
        merged_into_id=row.get("merged_into_id") or "",
        governance_policy_version=row.get("governance_policy_version") or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVOLUTION_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_EVOLUTION_PROPOSAL_ID_RE = re.compile(r"^evp_[0-9a-f]{24}$")
_PROPOSAL_KINDS = frozenset({"knowledge", "profile", "prompt", "tool", "test", "code"})


def _validate_proposal_provenance(
    *,
    source_kind: ProposalSourceKind,
    source_id: str,
    source_revision: int,
    source_occurrence_count: int,
    source_sha256: str,
    source_proposal_id: str,
    generator_version: str,
    proposal_kind: str,
    idempotency_key: str,
) -> None:
    if not isinstance(source_kind, ProposalSourceKind):
        raise ValueError("Proposal source_kind 必须使用已注册枚举。")
    if source_kind is ProposalSourceKind.MANUAL:
        source_values = (
            source_id,
            source_sha256,
            source_proposal_id,
            generator_version,
            proposal_kind,
            idempotency_key,
        )
        if any(source_values) or source_revision != 0 or source_occurrence_count != 0:
            raise ValueError("手动 Proposal 不得伪造自动来源字段。")
        return
    valid = (
        _EVOLUTION_CANDIDATE_ID_RE.fullmatch(source_id)
        and source_revision >= 1
        and source_occurrence_count >= 1
        and _SHA256_RE.fullmatch(source_sha256)
        and _EVOLUTION_PROPOSAL_ID_RE.fullmatch(source_proposal_id)
        and generator_version == "evolution-proposal-v1"
        and proposal_kind in _PROPOSAL_KINDS
        and idempotency_key == f"evolution:{source_proposal_id}"
    )
    if not valid:
        raise ValueError("Evolution Proposal 来源字段不完整或不可信。")


def _proposal_identity(proposal: WorkbenchProposal) -> tuple[Any, ...]:
    return (
        proposal.mission_id,
        proposal.task_id,
        proposal.title,
        proposal.impact_scope,
        tuple(proposal.intended_files),
        tuple(proposal.validation_plan),
        proposal.risk_level,
        tuple(proposal.questions),
        proposal.source_kind,
        proposal.source_id,
        proposal.source_revision,
        proposal.source_occurrence_count,
        proposal.source_sha256,
        proposal.source_proposal_id,
        proposal.generator_version,
        proposal.proposal_kind,
        proposal.idempotency_key,
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
