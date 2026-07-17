"""Read-only lifecycle models shared by session surfaces."""

from __future__ import annotations

from dataclasses import dataclass


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
