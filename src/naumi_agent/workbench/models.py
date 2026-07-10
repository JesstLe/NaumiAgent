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


class DecisionStrength(StrEnum):
    """How strongly a decision constrains downstream agent actions.

    ADVISORY: a note/guideline agents should consider but may override.
    REQUIRED: agents must comply; violating it blocks the action pending review.
    BLOCKING: hard stop — the action cannot proceed until the decision is revised.
    """

    ADVISORY = "advisory"
    REQUIRED = "required"
    BLOCKING = "blocking"


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
class IssueBid:
    """A single agent's bid to claim an issue (task).

    Bids express confidence, an effort estimate, an ETA, and a free-form note.
    They are persisted independently of leases so the market can show competing
    bids before a lease is granted.
    """

    id: str
    session_id: str
    task_id: str
    agent_id: str
    confidence: float
    estimate_minutes: int
    eta: str
    note: str
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
    created_by: str = "Human"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class Decision:
    id: str
    session_id: str
    mission_id: str
    kind: DecisionKind
    title: str
    content: str
    actor: str
    strength: DecisionStrength = DecisionStrength.REQUIRED
    created_at: str = field(default_factory=now_iso)


@dataclass
class Approval:
    id: str
    session_id: str
    mission_id: str
    task_id: str
    state: ApprovalState
    title: str
    detail: str
    requester: str
    reviewer: str
    decision_note: str
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


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
