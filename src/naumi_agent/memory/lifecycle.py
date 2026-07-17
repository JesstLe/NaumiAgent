"""Read-only lifecycle models shared by session surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionDeletePreview:
    """Exact persisted impact known before deleting one session."""

    session_id: str
    title: str
    workspace_root: str
    message_count: int
    is_active: bool
    harness_run_count: int
    criterion_count: int
    check_count: int
    evidence_count: int
    replay_baseline_count: int
    check_artifact_reference_count: int
    evidence_artifact_reference_count: int

    @property
    def artifact_reference_count(self) -> int:
        """Return reference rows, not unique or safely deletable files."""
        return (
            self.check_artifact_reference_count
            + self.evidence_artifact_reference_count
        )


@dataclass(frozen=True, slots=True)
class SessionRetentionCandidate:
    """Minimal Session Store projection; message payload is never decoded."""

    session_id: str
    title: str
    status: str
    last_accessed_at: datetime
    archived_at: datetime | None
    payload_bytes: int

    @property
    def effective_last_accessed_at(self) -> datetime:
        if self.archived_at is None:
            return self.last_accessed_at
        return max(self.last_accessed_at, self.archived_at)


@dataclass(frozen=True, slots=True)
class SessionRetentionScan:
    """Bounded read-only candidate scan plus full aggregate totals."""

    candidates: tuple[SessionRetentionCandidate, ...]
    total_archived_count: int
    total_archived_bytes: int

    @property
    def scanned_count(self) -> int:
        return len(self.candidates)

    @property
    def scan_truncated(self) -> bool:
        return self.total_archived_count > self.scanned_count
