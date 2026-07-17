"""Durable state model for cross-Store Session/Harness reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from naumi_agent.harness.retention import LifecycleActor


class SessionReconciliationState(StrEnum):
    """Monotonic states for one Session delete reconciliation request."""

    PREPARED = "prepared"
    SESSION_COMMITTED = "session_committed"
    RECORDS_COMMITTED = "records_committed"


class SessionReconciliationTerminalOutcome(StrEnum):
    """Terminal outcomes that stop recovery without claiming deletion."""

    RETENTION_POLICY_BLOCKED = "retention_policy_blocked"


class ReconciliationArtifactKind(StrEnum):
    """Typed artifact references retained after Harness rows are deleted."""

    CHECK_PATH = "check_path"
    EVIDENCE_URI = "evidence_uri"


class ReconciliationArtifactGcStatus(StrEnum):
    """Durable Artifact cleanup state for one Session delete request."""

    PENDING = "pending"
    COMPLETED = "completed"


class SessionReconciliationTransitionError(RuntimeError):
    """Raised when a reconciliation attempts to skip or reverse a state."""


@dataclass(frozen=True, slots=True, order=True)
class ReconciliationArtifactReference:
    kind: ReconciliationArtifactKind
    value: str


@dataclass(frozen=True, slots=True)
class SessionDeleteReconciliation:
    """Non-sensitive durable facts needed to resume one delete saga."""

    request_id: str
    workspace_root: str
    session_id: str
    actor: LifecycleActor
    state: SessionReconciliationState
    run_count: int
    deleted_run_count: int
    artifact_references: tuple[ReconciliationArtifactReference, ...]
    artifact_gc_status: ReconciliationArtifactGcStatus
    artifact_candidate_count: int
    artifact_deleted_count: int
    artifact_missing_count: int
    artifact_shared_count: int
    artifact_unsafe_count: int
    artifact_non_file_count: int
    artifact_gc_blocked_by_unresolved_live_reference: bool
    created_at: str
    updated_at: str


def validate_reconciliation_transition(
    current: SessionReconciliationState,
    requested: SessionReconciliationState,
) -> bool:
    """Return idempotency while rejecting skipped or reversed transitions."""
    if current is requested:
        return True
    allowed = {
        SessionReconciliationState.PREPARED: (
            SessionReconciliationState.SESSION_COMMITTED
        ),
        SessionReconciliationState.SESSION_COMMITTED: (
            SessionReconciliationState.RECORDS_COMMITTED
        ),
    }
    if allowed.get(current) is requested:
        return False
    if current is SessionReconciliationState.PREPARED:
        detail = "Session 删除尚未确认，不能清理 Harness 记录。"
    else:
        detail = "协调状态只能按顺序前进，不能跳级或回退。"
    raise SessionReconciliationTransitionError(detail)
