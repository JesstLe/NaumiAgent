# NaumiAgent Mac Agent Workbench MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local-first collaboration core that lets a future Mac app visualize and govern missions, issues, agent leases, worktrees, validation runs, decisions, failures, approvals, and audit events.

**Architecture:** Add a focused `naumi_agent.workbench` package that extends the existing `TaskStore`, `WorktreeManager`, API routes, and terminal-ui bridge instead of creating a parallel agent system. The first MVP is backend-first: SQLite-backed collaboration state, policy checks, task-market claim/lease, worktree binding, validation records, event snapshots, REST/WebSocket surfaces, and terminal-ui protocol support that the Mac shell can consume later.

**Tech Stack:** Python 3.12+, dataclasses, `aiosqlite`, FastAPI, existing NaumiAgent `TaskStore`, existing `WorktreeManager`, existing streaming events, Node terminal-ui protocol tests, pytest.

---

## MVP Boundary

This plan intentionally does not build a full native Swift/Tauri shell in the first implementation slice. It creates the local collaboration kernel and UI/API contract that a Mac app can mount.

Included:
- Local-first mission dashboard data.
- Issue metadata layered on existing tasks.
- Claim / lease task market.
- Human intent locks and decision log.
- Agent profile and permission metadata.
- Worktree binding as a first-class card.
- Validation run records and failure cards.
- Audit ledger events.
- Context health snapshots.
- REST endpoints and WebSocket/terminal-ui protocol events.

Excluded from this MVP:
- Cloud sync.
- Multi-machine agent dispatch.
- GitHub PR creation against remote repositories.
- Native macOS packaging.
- Fully autonomous merge.
- Direct production deployment.

## File Structure

Create:
- `src/naumi_agent/workbench/__init__.py` — public package exports.
- `src/naumi_agent/workbench/models.py` — collaboration domain enums and dataclasses.
- `src/naumi_agent/workbench/store.py` — SQLite tables and persistence methods.
- `src/naumi_agent/workbench/policy.py` — intent lock and risk policy evaluator.
- `src/naumi_agent/workbench/market.py` — bid, claim, lease, release, and expiry logic.
- `src/naumi_agent/workbench/context_health.py` — context-health scoring and labels.
- `src/naumi_agent/workbench/validation.py` — validation-run command allowlist and result recording.
- `src/naumi_agent/workbench/service.py` — orchestration facade used by API, tools, and UI.
- `src/naumi_agent/workbench/tools.py` — optional LLM tools for proposal-safe workbench operations.
- `src/naumi_agent/api/routes/workbench.py` — REST endpoints for the Mac app.
- `tests/unit/test_workbench_models.py`
- `tests/unit/test_workbench_store.py`
- `tests/unit/test_workbench_policy.py`
- `tests/unit/test_workbench_market.py`
- `tests/unit/test_workbench_context_health.py`
- `tests/unit/test_workbench_validation.py`
- `tests/unit/test_workbench_service.py`
- `tests/unit/test_api_workbench.py`
- `tests/e2e/ui_scenarios/workbench_dashboard.yaml`

Modify:
- `src/naumi_agent/api/app.py` — include the workbench router.
- `src/naumi_agent/orchestrator/engine.py` — initialize `WorkbenchService` and register safe tools.
- `src/naumi_agent/streaming/events.py` — add workbench event types.
- `frontend/terminal-ui/protocol-contract.json` — add workbench server event and payload fields.
- `frontend/terminal-ui/src/protocol.js` — normalize workbench events.
- `frontend/terminal-ui/src/state.js` — store workbench dashboard snapshot.
- `frontend/terminal-ui/src/components/task-panel.js` — show issue owner, lease, and risk metadata when present.
- `frontend/terminal-ui/test/protocol.test.js`
- `frontend/terminal-ui/test/state.test.js`
- `frontend/terminal-ui/test/components.test.js`
- `tests/unit/test_engine.py` — verify service/tool registration.

## Data Model Contract

Use existing `TaskStore` as the source for basic task lifecycle:

```text
TaskStore.tasks
  id, session_id, subject, description, status, active_form, owner, blocks, blocked_by
```

Add workbench metadata tables keyed by `session_id` and task/mission IDs:

```text
workbench_missions
workbench_issues
workbench_agent_profiles
workbench_leases
workbench_decisions
workbench_intent_locks
workbench_approvals
workbench_validation_runs
workbench_failures
workbench_audit_events
workbench_context_snapshots
```

Do not duplicate task status. `workbench_issues.task_id` points to `tasks.id`; claim and completion update `TaskStore` through existing methods.

---

### Task 1: Domain Models

**Files:**
- Create: `src/naumi_agent/workbench/__init__.py`
- Create: `src/naumi_agent/workbench/models.py`
- Test: `tests/unit/test_workbench_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/unit/test_workbench_models.py`:

```python
from __future__ import annotations

from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    FailureKind,
    IssueMetadata,
    LeaseState,
    ParallelMode,
    RiskLevel,
    WorkbenchEvent,
)


def test_issue_metadata_defaults_are_safe() -> None:
    issue = IssueMetadata(session_id="s", task_id="1", mission_id="m1")

    assert issue.parallel_mode == ParallelMode.EXCLUSIVE
    assert issue.risk_level == RiskLevel.MEDIUM
    assert issue.requires_human_approval is True
    assert issue.acceptance_criteria == []
    assert issue.expected_artifacts == []


def test_event_payload_is_json_ready() -> None:
    event = WorkbenchEvent(
        session_id="s",
        type="issue.claimed",
        actor="Backend-Agent",
        subject_id="1",
        payload={"lease_id": "lease-1"},
    )

    assert event.to_dict()["type"] == "issue.claimed"
    assert event.to_dict()["payload"]["lease_id"] == "lease-1"


def test_enum_values_are_stable_for_api_contract() -> None:
    assert ParallelMode.COMPETITIVE.value == "competitive"
    assert RiskLevel.CRITICAL.value == "critical"
    assert LeaseState.EXPIRED.value == "expired"
    assert ApprovalState.WAITING.value == "waiting"
    assert DecisionKind.ARCHITECTURE.value == "architecture"
    assert FailureKind.SCOPE_VIOLATION.value == "scope_violation"
    assert ContextHealth.STALE.value == "stale"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/unit/test_workbench_models.py -q
```

Expected: fails with `ModuleNotFoundError: No module named 'naumi_agent.workbench'`.

- [ ] **Step 3: Implement the models**

Create `src/naumi_agent/workbench/models.py`:

```python
"""Local-first workbench domain models."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ParallelMode(StrEnum):
    EXCLUSIVE = "exclusive"
    COOPERATIVE = "cooperative"
    COMPETITIVE = "competitive"
    EXPLORATORY = "exploratory"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ApprovalState(StrEnum):
    WAITING = "waiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"


class DecisionKind(StrEnum):
    PRINCIPLE = "principle"
    ARCHITECTURE = "architecture"
    POLICY = "policy"
    TEMPORARY = "temporary"
    EXPERIMENT = "experiment"


class FailureKind(StrEnum):
    LEASE_EXPIRED = "lease_expired"
    AGENT_TIMEOUT = "agent_timeout"
    TEST_FAILED = "test_failed"
    MERGE_CONFLICT = "merge_conflict"
    REVIEW_REJECTED = "review_rejected"
    SCOPE_VIOLATION = "scope_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONTEXT_STALE = "context_stale"
    PERMISSION_DENIED = "permission_denied"
    WORKTREE_DIRTY = "worktree_dirty"


class ContextHealth(StrEnum):
    GOOD = "good"
    STALE = "stale"
    OVERLOADED = "overloaded"
    MISSING = "missing"
    CONFLICTED = "conflicted"


@dataclass
class Mission:
    id: str
    session_id: str
    title: str
    goal: str
    status: str = "planning"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class IssueMetadata:
    session_id: str
    task_id: str
    mission_id: str
    parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requires_human_approval: bool = True
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    related_branch: str = ""
    related_worktree: str = ""
    related_pr: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class AgentProfile:
    id: str
    session_id: str
    name: str
    role: str
    capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    max_parallel_tasks: int = 1
    status: str = "idle"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class Lease:
    id: str
    session_id: str
    task_id: str
    agent_id: str
    state: LeaseState
    expires_at: str
    worktree_name: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class IntentLock:
    id: str
    session_id: str
    mission_id: str
    rule: str
    blocked_paths: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    require_proposal_for_risk: RiskLevel = RiskLevel.HIGH
    active: bool = True
    created_at: str = field(default_factory=now_iso)


@dataclass
class Decision:
    id: str
    session_id: str
    mission_id: str
    kind: DecisionKind
    title: str
    content: str
    actor: str
    created_at: str = field(default_factory=now_iso)


@dataclass
class WorkbenchEvent:
    session_id: str
    type: str
    actor: str
    subject_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Create `src/naumi_agent/workbench/__init__.py`:

```python
"""Local-first collaboration workbench for NaumiAgent."""

from naumi_agent.workbench.models import (
    AgentProfile,
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
)

__all__ = [
    "AgentProfile",
    "ApprovalState",
    "ContextHealth",
    "Decision",
    "DecisionKind",
    "FailureKind",
    "IntentLock",
    "IssueMetadata",
    "Lease",
    "LeaseState",
    "Mission",
    "ParallelMode",
    "RiskLevel",
    "WorkbenchEvent",
]
```

- [ ] **Step 4: Verify model tests pass**

Run:

```bash
pytest tests/unit/test_workbench_models.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/workbench/__init__.py src/naumi_agent/workbench/models.py tests/unit/test_workbench_models.py
git commit -m "feat: add workbench domain models"
```

---

### Task 2: SQLite Store for Missions, Issues, Decisions, and Audit Events

**Files:**
- Create: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/unit/test_workbench_store.py`:

```python
from __future__ import annotations

import pytest

from naumi_agent.workbench.models import DecisionKind, ParallelMode, RiskLevel
from naumi_agent.workbench.store import WorkbenchStore


@pytest.fixture
def store(tmp_path) -> WorkbenchStore:
    return WorkbenchStore(str(tmp_path / "workbench.db"))


@pytest.mark.asyncio
async def test_create_mission_and_issue_metadata(store: WorkbenchStore) -> None:
    mission = await store.create_mission(
        session_id="s",
        title="构建 Mac 工作台",
        goal="让用户治理多 Agent 研发流程",
    )
    issue = await store.upsert_issue(
        session_id="s",
        task_id="1",
        mission_id=mission.id,
        parallel_mode=ParallelMode.EXCLUSIVE,
        risk_level=RiskLevel.HIGH,
        acceptance_criteria=["必须通过 claim 冲突测试"],
        expected_artifacts=["实现文档", "测试报告"],
    )

    loaded = await store.get_issue("s", "1")
    assert loaded == issue
    assert loaded.risk_level == RiskLevel.HIGH
    assert loaded.acceptance_criteria == ["必须通过 claim 冲突测试"]


@pytest.mark.asyncio
async def test_decision_and_audit_event_are_persisted(store: WorkbenchStore) -> None:
    mission = await store.create_mission("s", "M", "G")
    decision = await store.add_decision(
        session_id="s",
        mission_id=mission.id,
        kind=DecisionKind.ARCHITECTURE,
        title="任务认领必须使用租约",
        content="避免 agent 崩溃后任务永久占用。",
        actor="Human",
    )
    event = await store.append_event(
        session_id="s",
        type="decision.created",
        actor="Human",
        subject_id=decision.id,
        payload={"kind": decision.kind.value},
    )

    assert [d.id for d in await store.list_decisions("s", mission.id)] == [decision.id]
    assert [e.id for e in await store.list_events("s")] == [event.id]
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/unit/test_workbench_store.py -q
```

Expected: fails with `ModuleNotFoundError` or `ImportError` for `WorkbenchStore`.

- [ ] **Step 3: Implement the store skeleton and schema**

Create `src/naumi_agent/workbench/store.py` with these table constants and helpers:

```python
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
```

- [ ] **Step 4: Implement persistence methods**

Add the class implementation:

```python
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
```

Add row helpers:

```python
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
```

- [ ] **Step 5: Verify store tests pass**

Run:

```bash
pytest tests/unit/test_workbench_store.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/store.py tests/unit/test_workbench_store.py
git commit -m "feat: persist workbench missions and audit events"
```

---

### Task 3: Intent Lock and Risk Policy

**Files:**
- Create: `src/naumi_agent/workbench/policy.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_policy.py`

- [ ] **Step 1: Write failing policy tests**

Create `tests/unit/test_workbench_policy.py`:

```python
from __future__ import annotations

from naumi_agent.workbench.models import IntentLock, RiskLevel
from naumi_agent.workbench.policy import PolicyDecision, evaluate_intent_locks


def test_blocked_path_requires_proposal() -> None:
    decision = evaluate_intent_locks(
        mission_id="m1",
        changed_paths=["src/naumi_agent/model/router.py"],
        risk_level=RiskLevel.MEDIUM,
        intent_locks=[
            IntentLock(
                id="lock-1",
                session_id="s",
                mission_id="m1",
                rule="本轮不触碰模型路由",
                blocked_paths=["src/naumi_agent/model/"],
            )
        ],
    )

    assert decision == PolicyDecision(
        allowed=False,
        requires_proposal=True,
        reason="命中意图锁：本轮不触碰模型路由",
        matched_lock_id="lock-1",
    )


def test_high_risk_requires_proposal_even_without_path_match() -> None:
    decision = evaluate_intent_locks(
        mission_id="m1",
        changed_paths=["docs/README.md"],
        risk_level=RiskLevel.HIGH,
        intent_locks=[
            IntentLock(
                id="lock-2",
                session_id="s",
                mission_id="m1",
                rule="高风险任务先提交 proposal",
                require_proposal_for_risk=RiskLevel.HIGH,
            )
        ],
    )

    assert not decision.allowed
    assert decision.requires_proposal
    assert "高风险任务先提交 proposal" in decision.reason
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/unit/test_workbench_policy.py -q
```

Expected: fails with `ModuleNotFoundError` for `naumi_agent.workbench.policy`.

- [ ] **Step 3: Implement policy evaluator**

Create `src/naumi_agent/workbench/policy.py`:

```python
"""Human intent lock and risk policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from naumi_agent.workbench.models import IntentLock, RiskLevel


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_proposal: bool
    reason: str
    matched_lock_id: str = ""


def evaluate_intent_locks(
    *,
    mission_id: str,
    changed_paths: list[str],
    risk_level: RiskLevel,
    intent_locks: list[IntentLock],
) -> PolicyDecision:
    """Return whether an action may execute directly under active human intent locks."""
    normalized_paths = [path.strip() for path in changed_paths if path.strip()]
    for lock in intent_locks:
        if not lock.active or lock.mission_id != mission_id:
            continue
        for prefix in lock.blocked_paths:
            if any(path.startswith(prefix) for path in normalized_paths):
                return PolicyDecision(
                    allowed=False,
                    requires_proposal=True,
                    reason=f"命中意图锁：{lock.rule}",
                    matched_lock_id=lock.id,
                )
        if _RISK_ORDER[risk_level] >= _RISK_ORDER[lock.require_proposal_for_risk]:
            return PolicyDecision(
                allowed=False,
                requires_proposal=True,
                reason=f"风险等级需要先提交 proposal：{lock.rule}",
                matched_lock_id=lock.id,
            )
    return PolicyDecision(allowed=True, requires_proposal=False, reason="允许执行")
```

- [ ] **Step 4: Extend store for intent locks**

Add `workbench_intent_locks` table to `src/naumi_agent/workbench/store.py`:

```python
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
```

Update `_ensure_tables()` to execute `_CREATE_INTENT_LOCKS`.

Add methods:

```python
from naumi_agent.workbench.models import IntentLock


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
```

Add helper:

```python
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
```

- [ ] **Step 5: Add store test for intent locks**

Append to `tests/unit/test_workbench_store.py`:

```python
@pytest.mark.asyncio
async def test_intent_locks_round_trip(store: WorkbenchStore) -> None:
    mission = await store.create_mission("s", "M", "G")
    lock = await store.add_intent_lock(
        session_id="s",
        mission_id=mission.id,
        rule="本轮不动 UI",
        blocked_paths=["frontend/"],
        require_proposal_for_risk=RiskLevel.MEDIUM,
    )

    locks = await store.list_intent_locks("s", mission.id)
    assert locks == [lock]
```

- [ ] **Step 6: Verify policy and store tests**

Run:

```bash
pytest tests/unit/test_workbench_policy.py tests/unit/test_workbench_store.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/naumi_agent/workbench/policy.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_policy.py tests/unit/test_workbench_store.py
git commit -m "feat: enforce workbench intent locks"
```

---

### Task 4: Task Market Claim and Lease

**Files:**
- Create: `src/naumi_agent/workbench/market.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_market.py`

- [ ] **Step 1: Write failing market tests**

Create `tests/unit/test_workbench_market.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.market import TaskMarket
from naumi_agent.workbench.models import LeaseState
from naumi_agent.workbench.store import WorkbenchStore


@pytest.fixture
async def stores(tmp_path):
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    mission = await workbench_store.create_mission("s", "M", "G")
    task = await task_store.create_task("实现任务认领")
    await workbench_store.upsert_issue(session_id="s", task_id=task.id, mission_id=mission.id)
    return task_store, workbench_store, task


@pytest.mark.asyncio
async def test_claim_marks_task_in_progress_and_creates_lease(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)

    lease = await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=45)

    assert lease.agent_id == "Backend-Agent"
    assert lease.state == LeaseState.ACTIVE
    updated = await task_store.get_task(task.id)
    assert updated is not None
    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.owner == "agent:Backend-Agent"


@pytest.mark.asyncio
async def test_exclusive_issue_rejects_second_active_claim(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=45)

    with pytest.raises(ValueError, match="已经被 Backend-Agent 认领"):
        await market.claim(task_id=task.id, agent_id="Frontend-Agent", duration_minutes=45)


@pytest.mark.asyncio
async def test_expire_leases_returns_task_to_pending(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)
    lease = await market.claim(task_id=task.id, agent_id="Backend-Agent", duration_minutes=1)
    expired_at = (datetime.fromisoformat(lease.expires_at) - timedelta(minutes=2)).isoformat()
    await workbench_store.force_lease_expiry_for_test(lease.id, expired_at)

    expired = await market.expire_overdue_leases(now=datetime.fromisoformat(lease.expires_at))

    assert [item.id for item in expired] == [lease.id]
    refreshed = await task_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.owner is None
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/unit/test_workbench_market.py -q
```

Expected: fails because `TaskMarket` does not exist.

- [ ] **Step 3: Add lease table and store methods**

Add to `src/naumi_agent/workbench/store.py`:

```python
from naumi_agent.workbench.models import Lease, LeaseState

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
```

Execute `_CREATE_LEASES` in `_ensure_tables()`.

Add methods:

```python
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
```

Add helper:

```python
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
```

- [ ] **Step 4: Implement `TaskMarket`**

Create `src/naumi_agent/workbench/market.py`:

```python
"""Task market claim and lease logic."""

from __future__ import annotations

from datetime import datetime, timedelta

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import Lease, LeaseState
from naumi_agent.workbench.store import WorkbenchStore


class TaskMarket:
    """Coordinate issue claims without bypassing the existing TaskStore."""

    def __init__(self, *, task_store: TaskStore, workbench_store: WorkbenchStore) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store

    async def claim(
        self,
        *,
        task_id: str,
        agent_id: str,
        duration_minutes: int = 45,
        worktree_name: str = "",
    ) -> Lease:
        if not self._task_store.session_id:
            raise ValueError("当前没有活动会话，不能认领任务")
        task = await self._task_store.get_task(task_id)
        if task is None:
            raise ValueError(f"任务 #{task_id} 不存在")
        if task.status == TaskStatus.COMPLETED:
            raise ValueError(f"任务 #{task_id} 已完成，不能认领")

        active = await self._workbench_store.get_active_lease(self._task_store.session_id, task_id)
        if active is not None:
            raise ValueError(f"任务 #{task_id} 已经被 {active.agent_id} 认领")

        expires_at = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(
            timespec="seconds"
        )
        lease = await self._workbench_store.create_lease(
            session_id=self._task_store.session_id,
            task_id=task_id,
            agent_id=agent_id,
            expires_at=expires_at,
            worktree_name=worktree_name,
        )
        await self._task_store.update_task(
            task_id,
            status=TaskStatus.IN_PROGRESS,
            active_form=f"{agent_id} 已认领，租约到期：{lease.expires_at}",
            owner=f"agent:{agent_id}",
        )
        await self._workbench_store.append_event(
            session_id=self._task_store.session_id,
            type="issue.claimed",
            actor=agent_id,
            subject_id=task_id,
            payload={"lease_id": lease.id, "expires_at": lease.expires_at},
        )
        return lease

    async def release(self, lease_id: str) -> Lease | None:
        lease = await self._workbench_store.update_lease_state(lease_id, LeaseState.RELEASED)
        if lease is None:
            return None
        await self._task_store.update_task(
            lease.task_id,
            status=TaskStatus.PENDING,
            active_form=None,
            owner=None,
        )
        await self._workbench_store.append_event(
            session_id=lease.session_id,
            type="issue.released",
            actor=lease.agent_id,
            subject_id=lease.task_id,
            payload={"lease_id": lease.id},
        )
        return lease

    async def expire_overdue_leases(self, *, now: datetime | None = None) -> list[Lease]:
        if not self._task_store.session_id:
            return []
        now_text = (now or datetime.now()).isoformat(timespec="seconds")
        overdue = await self._workbench_store.list_overdue_leases(
            self._task_store.session_id,
            now_text,
        )
        expired: list[Lease] = []
        for lease in overdue:
            updated = await self._workbench_store.update_lease_state(lease.id, LeaseState.EXPIRED)
            if updated is None:
                continue
            await self._task_store.update_task(
                lease.task_id,
                status=TaskStatus.PENDING,
                active_form=None,
                owner=None,
            )
            await self._workbench_store.append_event(
                session_id=lease.session_id,
                type="lease.expired",
                actor="system",
                subject_id=lease.task_id,
                payload={"lease_id": lease.id, "agent_id": lease.agent_id},
            )
            expired.append(updated)
        return expired
```

- [ ] **Step 5: Verify market tests pass**

Run:

```bash
pytest tests/unit/test_workbench_market.py -q
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/market.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_market.py
git commit -m "feat: add workbench task market leases"
```

---

### Task 5: Worktree Binding Integration

**Files:**
- Modify: `src/naumi_agent/workbench/market.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_market.py`
- Test: `tests/unit/test_worktree.py`

- [ ] **Step 1: Add failing test for claim with worktree metadata**

Append to `tests/unit/test_workbench_market.py`:

```python
@pytest.mark.asyncio
async def test_claim_records_related_worktree_on_issue(stores) -> None:
    task_store, workbench_store, task = stores
    market = TaskMarket(task_store=task_store, workbench_store=workbench_store)

    await market.claim(
        task_id=task.id,
        agent_id="Backend-Agent",
        duration_minutes=45,
        worktree_name="issue-1-backend",
    )

    issue = await workbench_store.get_issue("s", task.id)
    assert issue is not None
    assert issue.related_worktree == "issue-1-backend"
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_workbench_market.py::test_claim_records_related_worktree_on_issue -q
```

Expected: fails because `related_worktree` is not updated.

- [ ] **Step 3: Add targeted store method**

Add to `WorkbenchStore`:

```python
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
```

- [ ] **Step 4: Update claim path**

In `TaskMarket.claim()`, after creating the lease:

```python
if worktree_name:
    await self._workbench_store.set_issue_worktree(
        session_id=self._task_store.session_id,
        task_id=task_id,
        worktree_name=worktree_name,
    )
```

- [ ] **Step 5: Verify task market and existing worktree tests**

Run:

```bash
pytest tests/unit/test_workbench_market.py tests/unit/test_worktree.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/market.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_market.py
git commit -m "feat: link workbench leases to worktrees"
```

---

### Task 6: Context Health Snapshots

**Files:**
- Create: `src/naumi_agent/workbench/context_health.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_context_health.py`

- [ ] **Step 1: Write failing context-health tests**

Create `tests/unit/test_workbench_context_health.py`:

```python
from __future__ import annotations

from naumi_agent.workbench.context_health import ContextHealthInput, evaluate_context_health
from naumi_agent.workbench.models import ContextHealth


def test_missing_acceptance_criteria_is_missing() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=False,
            minutes_since_sync=2,
            token_load_ratio=0.2,
            policy_conflict=False,
        )
    )

    assert result.health == ContextHealth.MISSING
    assert "缺少验收标准" in result.reasons


def test_stale_sync_is_stale() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=True,
            minutes_since_sync=90,
            token_load_ratio=0.2,
            policy_conflict=False,
        )
    )

    assert result.health == ContextHealth.STALE


def test_policy_conflict_wins_over_token_load() -> None:
    result = evaluate_context_health(
        ContextHealthInput(
            has_goal=True,
            has_acceptance_criteria=True,
            minutes_since_sync=2,
            token_load_ratio=0.95,
            policy_conflict=True,
        )
    )

    assert result.health == ContextHealth.CONFLICTED
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/unit/test_workbench_context_health.py -q
```

Expected: fails because module does not exist.

- [ ] **Step 3: Implement context health evaluator**

Create `src/naumi_agent/workbench/context_health.py`:

```python
"""Context health scoring for agent workbench cards."""

from __future__ import annotations

from dataclasses import dataclass, field

from naumi_agent.workbench.models import ContextHealth


@dataclass(frozen=True)
class ContextHealthInput:
    has_goal: bool
    has_acceptance_criteria: bool
    minutes_since_sync: int
    token_load_ratio: float
    policy_conflict: bool


@dataclass(frozen=True)
class ContextHealthResult:
    health: ContextHealth
    reasons: list[str] = field(default_factory=list)


def evaluate_context_health(value: ContextHealthInput) -> ContextHealthResult:
    reasons: list[str] = []
    if value.policy_conflict:
        return ContextHealthResult(ContextHealth.CONFLICTED, ["当前计划与意图锁或决策日志冲突"])
    if not value.has_goal:
        reasons.append("缺少 mission 目标")
    if not value.has_acceptance_criteria:
        reasons.append("缺少验收标准")
    if reasons:
        return ContextHealthResult(ContextHealth.MISSING, reasons)
    if value.minutes_since_sync >= 60:
        return ContextHealthResult(ContextHealth.STALE, ["超过 60 分钟未同步上下文"])
    if value.token_load_ratio >= 0.85:
        return ContextHealthResult(ContextHealth.OVERLOADED, ["上下文接近模型窗口上限"])
    return ContextHealthResult(ContextHealth.GOOD, ["上下文健康"])
```

- [ ] **Step 4: Add context snapshot table**

Add to `store.py`:

```python
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
```

Execute it in `_ensure_tables()`.

Add method:

```python
from naumi_agent.workbench.models import ContextHealth


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
```

- [ ] **Step 5: Verify context tests**

Run:

```bash
pytest tests/unit/test_workbench_context_health.py -q
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/context_health.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_context_health.py
git commit -m "feat: score workbench context health"
```

---

### Task 7: Validation Runs and Failure Cards

**Files:**
- Create: `src/naumi_agent/workbench/validation.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `tests/unit/test_workbench_validation.py`

- [ ] **Step 1: Write failing validation tests**

Create `tests/unit/test_workbench_validation.py`:

```python
from __future__ import annotations

import pytest

from naumi_agent.workbench.models import FailureKind
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationCommand, ValidationRunner


@pytest.mark.asyncio
async def test_validation_runner_records_success(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["python3", "-c"]])

    result = await runner.run(
        session_id="s",
        task_id="1",
        actor="Test-Agent",
        command=ValidationCommand(argv=["python3", "-c", "print('ok')"], cwd=str(tmp_path)),
    )

    assert result.status == "passed"
    assert "ok" in result.output


@pytest.mark.asyncio
async def test_validation_runner_rejects_unapproved_command(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["pytest"]])

    with pytest.raises(ValueError, match="不在允许列表"):
        await runner.run(
            session_id="s",
            task_id="1",
            actor="Test-Agent",
            command=ValidationCommand(argv=["rm", "-rf", "x"], cwd=str(tmp_path)),
        )


@pytest.mark.asyncio
async def test_failed_validation_creates_failure_card(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["python3", "-c"]])

    result = await runner.run(
        session_id="s",
        task_id="1",
        actor="Test-Agent",
        command=ValidationCommand(argv=["python3", "-c", "raise SystemExit(3)"], cwd=str(tmp_path)),
    )

    failures = await store.list_failures("s")
    assert result.status == "failed"
    assert failures[0]["kind"] == FailureKind.TEST_FAILED.value
    assert failures[0]["task_id"] == "1"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/unit/test_workbench_validation.py -q
```

Expected: fails because validation module and failure store methods do not exist.

- [ ] **Step 3: Add validation and failure tables**

Add to `store.py`:

```python
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
```

Execute both in `_ensure_tables()`.

Add methods:

```python
from naumi_agent.workbench.models import FailureKind


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
```

- [ ] **Step 4: Implement validation runner**

Create `src/naumi_agent/workbench/validation.py`:

```python
"""Validation run recording for workbench merge gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from naumi_agent.workbench.models import FailureKind, now_iso
from naumi_agent.workbench.store import WorkbenchStore


@dataclass(frozen=True)
class ValidationCommand:
    argv: list[str]
    cwd: str


@dataclass(frozen=True)
class ValidationResult:
    id: str
    status: str
    exit_code: int
    output: str


class ValidationRunner:
    """Run allowlisted validation commands and record their result."""

    def __init__(
        self,
        *,
        store: WorkbenchStore,
        allowed_commands: list[list[str]],
        timeout_seconds: int = 120,
    ) -> None:
        self._store = store
        self._allowed_commands = allowed_commands
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        command: ValidationCommand,
    ) -> ValidationResult:
        self._ensure_allowed(command.argv)
        started = now_iso()
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            output = "验证命令超时"
            exit_code = 124
        completed = now_iso()
        status = "passed" if exit_code == 0 else "failed"
        run = await self._store.record_validation_run(
            session_id=session_id,
            task_id=task_id,
            actor=actor,
            command=command.argv,
            cwd=command.cwd,
            status=status,
            exit_code=exit_code,
            output=output[-6000:],
            started_at=started,
            completed_at=completed,
        )
        if status == "failed":
            await self._store.create_failure(
                session_id=session_id,
                task_id=task_id,
                kind=FailureKind.TEST_FAILED,
                title="验证命令失败",
                detail=output[-6000:],
                source_id=run["id"],
            )
        return ValidationResult(
            id=str(run["id"]),
            status=status,
            exit_code=exit_code,
            output=output,
        )

    def _ensure_allowed(self, argv: list[str]) -> None:
        for prefix in self._allowed_commands:
            if argv[: len(prefix)] == prefix:
                return
        raise ValueError(f"验证命令不在允许列表：{' '.join(argv)}")
```

- [ ] **Step 5: Verify validation tests pass**

Run:

```bash
pytest tests/unit/test_workbench_validation.py -q
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/validation.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_validation.py
git commit -m "feat: record workbench validation failures"
```

---

### Task 8: Workbench Service Facade

**Files:**
- Create: `src/naumi_agent/workbench/service.py`
- Test: `tests/unit/test_workbench_service.py`

- [ ] **Step 1: Write failing service test**

Create `tests/unit/test_workbench_service.py`:

```python
from __future__ import annotations

import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


@pytest.mark.asyncio
async def test_dashboard_snapshot_contains_core_cards(tmp_path) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_store.set_session("s")
    workbench_store = WorkbenchStore(str(tmp_path / "workbench.db"))
    service = WorkbenchService(task_store=task_store, workbench_store=workbench_store)

    mission = await service.create_mission(
        session_id="s",
        title="Mac 工作台",
        goal="可视化治理多 Agent 研发",
    )
    task = await task_store.create_task("实现任务市场")
    await service.attach_issue(
        session_id="s",
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["认领冲突必须被拒绝"],
    )

    snapshot = await service.dashboard_snapshot("s")

    assert snapshot["missions"][0]["title"] == "Mac 工作台"
    assert snapshot["issues"][0]["task_id"] == task.id
    assert snapshot["tasks"][0]["subject"] == "实现任务市场"
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_workbench_service.py -q
```

Expected: fails because `WorkbenchService` does not exist.

- [ ] **Step 3: Implement service facade**

Create `src/naumi_agent/workbench/service.py`:

```python
"""Workbench application service used by API routes and UI bridges."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import Mission, ParallelMode, RiskLevel
from naumi_agent.workbench.store import WorkbenchStore


class WorkbenchService:
    """High-level facade for dashboard operations."""

    def __init__(self, *, task_store: TaskStore, workbench_store: WorkbenchStore) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store

    async def create_mission(self, *, session_id: str, title: str, goal: str) -> Mission:
        mission = await self._workbench_store.create_mission(session_id, title, goal)
        await self._workbench_store.append_event(
            session_id=session_id,
            type="mission.created",
            actor="Human",
            subject_id=mission.id,
            payload={"title": mission.title},
        )
        return mission

    async def attach_issue(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        acceptance_criteria: list[str],
        parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
    ) -> dict[str, Any]:
        issue = await self._workbench_store.upsert_issue(
            session_id=session_id,
            task_id=task_id,
            mission_id=mission_id,
            parallel_mode=parallel_mode,
            risk_level=risk_level,
            acceptance_criteria=acceptance_criteria,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="issue.created",
            actor="Planner-Agent",
            subject_id=task_id,
            payload={"mission_id": mission_id, "risk_level": risk_level.value},
        )
        return asdict(issue)

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        tasks = await self._task_store.list_tasks()
        events = await self._workbench_store.list_events(session_id, limit=50)
        failures = await self._workbench_store.list_failures(session_id)
        issues = []
        for task in tasks:
            issue = await self._workbench_store.get_issue(session_id, task.id)
            if issue is not None:
                issues.append(asdict(issue))
        return {
            "session_id": session_id,
            "missions": await self._list_missions_for_snapshot(session_id),
            "tasks": [asdict(task) for task in tasks],
            "issues": issues,
            "failures": failures,
            "events": [event.to_dict() for event in events],
        }

    async def _list_missions_for_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        missions = await self._workbench_store.list_missions(session_id)
        return [asdict(mission) for mission in missions]
```

- [ ] **Step 4: Add `list_missions()` to store**

Add to `WorkbenchStore`:

```python
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
```

- [ ] **Step 5: Verify service test**

Run:

```bash
pytest tests/unit/test_workbench_service.py -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/workbench/service.py src/naumi_agent/workbench/store.py tests/unit/test_workbench_service.py
git commit -m "feat: expose workbench dashboard service"
```

---

### Task 9: Engine Initialization and Safe Workbench Tools

**Files:**
- Create: `src/naumi_agent/workbench/tools.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Test: `tests/unit/test_engine.py`

- [ ] **Step 1: Add failing engine registration test**

Append to `tests/unit/test_engine.py`:

```python
@pytest.mark.asyncio
async def test_engine_registers_safe_workbench_tools(tmp_path) -> None:
    from naumi_agent.config.settings import AppConfig, MemoryConfig
    from naumi_agent.orchestrator.engine import AgentEngine

    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )
    try:
        names = set(engine.tool_registry.names)
        assert {
            "workbench_snapshot",
            "workbench_propose_issue",
        }.issubset(names)
    finally:
        await engine.shutdown()
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_engine.py::test_engine_registers_safe_workbench_tools -q
```

Expected: fails because tools are not registered.

- [ ] **Step 3: Implement read/proposal-only tools**

Create `src/naumi_agent/workbench/tools.py`:

```python
"""LLM tools for safe workbench interaction."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.tools.base import Tool
from naumi_agent.workbench.service import WorkbenchService


def create_workbench_tools(service: WorkbenchService) -> list[Tool]:
    return [
        WorkbenchSnapshotTool(service),
        WorkbenchProposeIssueTool(service),
    ]


class WorkbenchSnapshotTool(Tool):
    def __init__(self, service: WorkbenchService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "workbench_snapshot"

    @property
    def description(self) -> str:
        return "读取当前 Mac 工作台快照，包括 mission、issue、任务、失败卡片和审计事件。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话 ID"}},
            "required": ["session_id"],
        }

    async def execute(self, *, session_id: str, **kwargs: Any) -> str:  # type: ignore[override]
        snapshot = await self._service.dashboard_snapshot(session_id)
        return json.dumps(snapshot, ensure_ascii=False, indent=2)


class WorkbenchProposeIssueTool(Tool):
    def __init__(self, service: WorkbenchService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "workbench_propose_issue"

    @property
    def description(self) -> str:
        return "创建 proposal 级别的问题建议，不直接修改代码或认领任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mission_id": {"type": "string"},
                "title": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["session_id", "mission_id", "title", "reason"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        session_id: str,
        mission_id: str,
        title: str,
        reason: str,
        **kwargs: Any,
    ) -> str:
        await self._service._workbench_store.append_event(
            session_id=session_id,
            type="proposal.issue_suggested",
            actor="Agent",
            subject_id=mission_id,
            payload={"title": title, "reason": reason},
        )
        return f"已记录建议 issue：{title}"
```

- [ ] **Step 4: Initialize service in engine**

In `src/naumi_agent/orchestrator/engine.py`, import:

```python
from naumi_agent.workbench import WorkbenchStore
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.tools import create_workbench_tools
```

In `AgentEngine.__init__`, after `self.task_store` is created, add:

```python
self.workbench_store = WorkbenchStore(config.memory.session_db_path)
self.workbench_service = WorkbenchService(
    task_store=self.task_store,
    workbench_store=self.workbench_store,
)
```

In `_register_builtin_tools()`, register:

```python
for tool in create_workbench_tools(self.workbench_service):
    self.tool_registry.register(tool)
```

- [ ] **Step 5: Export store in package**

Modify `src/naumi_agent/workbench/__init__.py`:

```python
from naumi_agent.workbench.store import WorkbenchStore

__all__.append("WorkbenchStore")
```

- [ ] **Step 6: Verify engine test**

Run:

```bash
pytest tests/unit/test_engine.py::test_engine_registers_safe_workbench_tools -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/naumi_agent/workbench/tools.py src/naumi_agent/workbench/__init__.py src/naumi_agent/orchestrator/engine.py tests/unit/test_engine.py
git commit -m "feat: register safe workbench tools"
```

---

### Task 10: REST API Routes for the Mac App

**Files:**
- Create: `src/naumi_agent/api/routes/workbench.py`
- Modify: `src/naumi_agent/api/app.py`
- Test: `tests/unit/test_api_workbench.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/unit/test_api_workbench.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from naumi_agent.api.routes.workbench import get_workbench_snapshot


class _FakeSessionStore:
    def __init__(self, exists: bool) -> None:
        self.exists = exists

    async def load(self, session_id: str):
        if not self.exists:
            return None
        return SimpleNamespace(id=session_id)


class _FakeWorkbenchService:
    async def dashboard_snapshot(self, session_id: str):
        return {
            "session_id": session_id,
            "missions": [],
            "tasks": [],
            "issues": [],
            "failures": [],
            "events": [],
        }


class _FakeEngine:
    def __init__(self, exists: bool) -> None:
        self.session_store = _FakeSessionStore(exists)
        self.workbench_service = _FakeWorkbenchService()
        self.loaded: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return self.session_store.exists


def _fake_request(engine: _FakeEngine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("missing", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_returns_service_snapshot() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert response["session_id"] == "sess-1"
    assert "missions" in response
    assert "events" in response
```

- [ ] **Step 2: Run failing API test**

Run:

```bash
pytest tests/unit/test_api_workbench.py -q
```

Expected: fails because `naumi_agent.api.routes.workbench` does not exist.

- [ ] **Step 3: Implement workbench route**

Create `src/naumi_agent/api/routes/workbench.py`:

```python
"""Workbench routes for the local Mac app."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from naumi_agent.api.deps import AuthDep

router = APIRouter(tags=["workbench"])


@router.get("/workbench/sessions/{session_id}/snapshot")
async def get_workbench_snapshot(session_id: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await engine.workbench_service.dashboard_snapshot(session_id)
```

- [ ] **Step 4: Register route**

Modify `src/naumi_agent/api/app.py`:

```python
from naumi_agent.api.routes import health, messages, tools, workbench, ws

app.include_router(workbench.router, prefix="/api/v1")
```

- [ ] **Step 5: Verify API tests**

Run:

```bash
pytest tests/unit/test_api_workbench.py tests/unit/test_api.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/api/routes/workbench.py src/naumi_agent/api/app.py tests/unit/test_api_workbench.py
git commit -m "feat: expose workbench snapshot API"
```

---

### Task 11: Streaming and Terminal UI Protocol Contract

**Files:**
- Modify: `src/naumi_agent/streaming/events.py`
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/protocol.test.js`
- Test: `frontend/terminal-ui/test/state.test.js`

- [ ] **Step 1: Add failing protocol tests**

Append to `frontend/terminal-ui/test/protocol.test.js`:

```javascript
import { normalizeServerRecord } from "../src/protocol.js";

test("normalizes workbench snapshot events", () => {
  const record = normalizeServerRecord({
    type: "workbench/snapshot",
    version: 1,
    payload: {
      session_id: "s",
      missions: [{ id: "m1", title: "Mac 工作台" }],
      issues: [],
      tasks: [],
      failures: [],
      events: [],
    },
  });

  expect(record.payload.session_id).toBe("s");
  expect(record.payload.missions[0].title).toBe("Mac 工作台");
});
```

- [ ] **Step 2: Run failing protocol test**

Run:

```bash
cd frontend/terminal-ui && npm test -- protocol.test.js
```

Expected: fails with `未知 Bridge 事件: workbench/snapshot`.

- [ ] **Step 3: Add backend stream event types**

Modify `src/naumi_agent/streaming/events.py`:

```python
    # Workbench / Mac app
    WORKBENCH_EVENT = "workbench_event"
    WORKBENCH_SNAPSHOT = "workbench_snapshot"
```

- [ ] **Step 4: Extend protocol contract**

Modify `frontend/terminal-ui/protocol-contract.json`:

```json
"server_events": [
  "ready",
  "ack",
  "error",
  "pong",
  "user/message",
  "ui/message",
  "engine/event",
  "run/started",
  "run/completed",
  "session/replayed",
  "runtime/status",
  "mode/changed",
  "permission/request",
  "permission/resolved",
  "debug/trace",
  "workbench/snapshot",
  "workbench/event",
  "shutdown"
]
```

Add:

```json
"workbench": {
  "snapshot_fields": ["session_id", "missions", "tasks", "issues", "failures", "events"],
  "event_fields": ["id", "type", "actor", "subject_id", "payload", "timestamp"]
}
```

- [ ] **Step 5: Normalize workbench payloads**

In `frontend/terminal-ui/src/protocol.js`, add to `normalizeServerPayload()`:

```javascript
if (type === "workbench/snapshot") {
  return {
    ...payload,
    session_id: String(payload.session_id ?? ""),
    missions: Array.isArray(payload.missions) ? payload.missions : [],
    tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
    issues: Array.isArray(payload.issues) ? payload.issues : [],
    failures: Array.isArray(payload.failures) ? payload.failures : [],
    events: Array.isArray(payload.events) ? payload.events : [],
  };
}
if (type === "workbench/event") {
  return {
    ...payload,
    id: String(payload.id ?? ""),
    type: String(payload.type ?? ""),
    actor: String(payload.actor ?? ""),
    subject_id: String(payload.subject_id ?? ""),
    payload: normalizeObject(payload.payload),
    timestamp: String(payload.timestamp ?? ""),
  };
}
```

- [ ] **Step 6: Store workbench snapshot in UI state**

Inspect `frontend/terminal-ui/src/state.js` and add a `workbench` state bucket matching existing style:

```javascript
workbench: {
  session_id: "",
  missions: [],
  tasks: [],
  issues: [],
  failures: [],
  events: [],
}
```

Add event reducer behavior:

```javascript
if (record.type === "workbench/snapshot") {
  state.workbench = record.payload;
}
if (record.type === "workbench/event") {
  state.workbench.events = [...state.workbench.events, record.payload].slice(-100);
}
```

- [ ] **Step 7: Verify terminal-ui protocol tests**

Run:

```bash
cd frontend/terminal-ui && npm test -- protocol.test.js state.test.js
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/naumi_agent/streaming/events.py frontend/terminal-ui/protocol-contract.json frontend/terminal-ui/src/protocol.js frontend/terminal-ui/src/state.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/state.test.js
git commit -m "feat: add workbench UI protocol events"
```

---

### Task 12: Task Panel Workbench Metadata Rendering

**Files:**
- Modify: `frontend/terminal-ui/src/components/task-panel.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `tests/e2e/ui_scenarios/workbench_dashboard.yaml`

- [ ] **Step 1: Add failing component test**

Append to `frontend/terminal-ui/test/components.test.js`, and add `renderTaskPanel` to the existing task-panel import:

```javascript
import { parseTaskPanel, TaskPanel, renderTaskPanel } from "../src/components/task-panel.js";

test("task panel enriches todo rows with workbench issue metadata", () => {
  const content = ["任务面板", "Todo", "  ● #1 实现任务市场"].join("\n");
  const rendered = renderTaskPanel(content, 90, {
    width: 90,
    state: {
      workbench: {
        issues: [
          {
            task_id: "1",
            risk_level: "high",
            parallel_mode: "exclusive",
            related_worktree: "issue-1-backend",
          },
        ],
      },
    },
  });
  const plain = stripAnsi(rendered.join("\n"));

  assert(plain.includes("risk:high"));
  assert(plain.includes("exclusive"));
  assert(plain.includes("issue-1-backend"));
});
```

- [ ] **Step 2: Run failing component test**

Run:

```bash
cd frontend/terminal-ui && npm test -- components.test.js
```

Expected: fails until task panel uses `ctx.state.workbench.issues` to enrich todo rows.

- [ ] **Step 3: Update renderer**

In `frontend/terminal-ui/src/components/task-panel.js`, update `renderTaskPanel()`:

```javascript
export function renderTaskPanel(content, width, ctx = { width }) {
  const model = parseTaskPanel(content);
  const taskPanel = ctx.taskPanel ?? ctx.state?.taskPanel ?? {};
  const workbenchIssues = ctx.state?.workbench?.issues ?? [];
  const issuesByTaskId = issueByTaskId(workbenchIssues);
  const children = [
    line(`${color(ANSI.cyan, "tasks")} ${model.summary}`),
    ...renderSection("Timeline", model.sections.Timeline, ANSI.green, taskPanel),
    ...renderSection("Detail", model.sections.Detail, ANSI.green, taskPanel),
    ...renderSection("Todo", model.sections.Todo, ANSI.cyan, taskPanel, issuesByTaskId),
    ...renderSection("Subagent", model.sections.Subagent, ANSI.magenta, taskPanel),
    ...renderSection("Background", model.sections.Background, ANSI.yellow, taskPanel),
    ...renderSection("Browser Runs", model.sections["Browser Runs"], ANSI.blue, taskPanel),
    ...renderSection("面板警告", model.sections["面板警告"], ANSI.red, taskPanel),
  ];
  const rendered = renderComponent(boxComponent("tasks", children), ctx);
  const maxRenderLines = taskPanel.maxRenderLines ?? ctx.bodyHeight ?? DEFAULT_MAX_RENDER_LINES;
  return clampTaskPanelLines(rendered, width, maxRenderLines);
}
```

Add helper:

```javascript
function issueByTaskId(issues = []) {
  return new Map(issues.map((issue) => [String(issue.task_id), issue]));
}
```

Change `renderSection()` signature and call path:

```javascript
function renderSection(title, rows = [], style = ANSI.dim, taskPanel = {}, issuesByTaskId = new Map()) {
  if (!rows.length) return [];
  if (title === "Timeline") {
    return renderTimelineSection(rows, style, taskPanel);
  }
  const visible = rows.slice(0, 6);
  const hidden = rows.length - visible.length;
  return [
    line(color(style, title)),
    ...visible.flatMap((item) => renderTaskRow(title, item, taskPanel, issuesByTaskId)),
    hidden > 0 ? line(color(ANSI.dim, `  ... 还有 ${hidden} 项`)) : null,
  ].filter(Boolean);
}
```

Change `renderTaskRow()` to enrich Todo rows:

```javascript
function renderTaskRow(section, item, taskPanel = {}, issuesByTaskId = new Map()) {
  const parsed = parseTaskRow(item);
  const rowId = taskRowId(section, parsed.primary);
  const selected = rowId && rowId === taskPanel.selectedId;
  const expanded = rowId && taskPanel.expandedIds?.[rowId];
  const prefix = selected ? color(ANSI.green, "> ") : "  ";
  const primary = section === "Todo" ? enrichTodoPrimary(parsed.primary, rowId, issuesByTaskId) : parsed.primary;
  if (!parsed.detail) {
    return [line(`${prefix}${taskLineStyle(section, primary)}`)];
  }
  const rows = [
    line(`${prefix}${taskLineStyle(section, primary)}`),
    line(color(ANSI.dim, `    ${compactText(parsed.detail, 180)}`)),
  ];
  if (expanded) {
    rows.push(...renderExpandedTaskDetail(parsed.detail));
  }
  return rows;
}
```

Add enrichment helper:

```javascript
function enrichTodoPrimary(primary, rowId, issuesByTaskId) {
  const issue = issuesByTaskId.get(String(rowId));
  if (!issue) return primary;
  const parts = [];
  if (issue.risk_level) parts.push(`risk:${issue.risk_level}`);
  if (issue.parallel_mode) parts.push(issue.parallel_mode);
  if (issue.related_worktree) parts.push(issue.related_worktree);
  return parts.length ? `${primary} [${parts.join(" · ")}]` : primary;
}
```

- [ ] **Step 4: Add E2E scenario fixture**

Create `tests/e2e/ui_scenarios/workbench_dashboard.yaml`:

```yaml
name: workbench-dashboard
events:
  - type: workbench/snapshot
    payload:
      session_id: s
      missions:
        - id: m1
          title: Mac 工作台
      tasks:
        - id: "1"
          subject: 实现任务市场
          status: in_progress
          owner: agent:Backend-Agent
      issues:
        - task_id: "1"
          risk_level: high
          parallel_mode: exclusive
          related_worktree: issue-1-backend
      failures: []
      events: []
assertions:
  contains:
    - Mac 工作台
    - Backend-Agent
    - issue-1-backend
```

- [ ] **Step 5: Verify UI tests**

Run:

```bash
cd frontend/terminal-ui && npm test -- components.test.js state.test.js
pytest tests/unit/test_ui_scenarios.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/terminal-ui/src/components/task-panel.js frontend/terminal-ui/test/components.test.js tests/e2e/ui_scenarios/workbench_dashboard.yaml
git commit -m "feat: show workbench metadata in task panel"
```

---

### Task 13: Self-Review and Full Verification

**Files:**
- Modify only if verification reveals defects.

- [ ] **Step 1: Run targeted Python tests**

Run:

```bash
pytest tests/unit/test_workbench_models.py \
  tests/unit/test_workbench_store.py \
  tests/unit/test_workbench_policy.py \
  tests/unit/test_workbench_market.py \
  tests/unit/test_workbench_context_health.py \
  tests/unit/test_workbench_validation.py \
  tests/unit/test_workbench_service.py \
  tests/unit/test_api_workbench.py \
  tests/unit/test_worktree.py \
  tests/unit/test_engine.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run frontend contract tests**

Run:

```bash
cd frontend/terminal-ui && npm test -- protocol.test.js state.test.js components.test.js
```

Expected: all selected tests pass.

- [ ] **Step 3: Run lint**

Run:

```bash
ruff check src/ tests/unit/test_workbench_*.py tests/unit/test_api_workbench.py
```

Expected: no ruff errors.

- [ ] **Step 4: Run real scenario smoke**

Run:

```bash
pytest tests/e2e/test_ui_scenarios.py -q
```

Expected: the new `workbench-dashboard` scenario passes and existing scenarios still pass.

- [ ] **Step 5: Manual self-review checklist**

Confirm:

```text
不是 prompt 套壳：所有 workbench 能力都有 SQLite、状态机、验证或 UI 合同逻辑。
不是单 happy path：覆盖 claim 冲突、lease 过期、命令拒绝、失败卡片、上下文异常。
不是平行系统：TaskStore 仍是任务状态来源；WorktreeManager 仍管理 worktree。
中文优先：API/tool 用户可见错误和结果是中文。
人类治理：意图锁、proposal、安全工具和失败卡片都已落地。
```

- [ ] **Step 6: Final commit if fixes were made**

If Step 1-4 required fixes, commit them:

```bash
git add src/ tests/ frontend/terminal-ui/
git commit -m "fix: stabilize workbench mvp verification"
```

---

## Execution Notes

Recommended order:

1. Implement Tasks 1-2 to establish storage.
2. Implement Tasks 3-5 to make governance and leases real.
3. Implement Tasks 6-8 to make the dashboard meaningful.
4. Implement Tasks 9-12 to expose it to API/UI.
5. Run Task 13 before any PR or merge.

Each task should be committed independently. Do not bundle independent tasks into one commit.

## Plan Self-Review

Spec coverage:
- Shared canvas state: covered by dashboard snapshot, issue metadata, events, failures.
- Task market: covered by `TaskMarket.claim`, active lease conflict, expiry.
- Git workflow: covered by worktree linkage; remote PR creation intentionally excluded from MVP.
- Automated validation: covered by allowlisted `ValidationRunner` and failure cards.
- Human governance: covered by intent locks, risk policy, decisions, approvals-ready data model, proposal tool.
- Auditability: covered by `workbench_audit_events` and dashboard event feed.
- Context health: covered by evaluator and snapshot persistence.

Placeholder scan:
- This plan avoids open-ended implementation instructions and gives concrete tests, commands, and snippets.
- API and terminal-ui tests now use real local helper names and exported functions.

Type consistency:
- `session_id`, `task_id`, `mission_id`, `agent_id`, `risk_level`, `parallel_mode`, `related_worktree`, and `lease_id` are used consistently across models, store, service, API, and UI protocol tasks.
