"""Durable fencing contracts for long-running Harness executions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HarnessRunKind(StrEnum):
    """Long-running execution domains sharing the Harness lease authority."""

    PURSUIT = "pursuit"
    TOOL = "tool"
    BROWSER = "browser"
    AGENT = "agent"
    RUNTIME = "runtime"


class HarnessRunLeaseState(StrEnum):
    """Durable lifecycle state of one run lease row."""

    ACTIVE = "active"
    RELEASED = "released"


class HarnessRunFenceDecision(StrEnum):
    """Whether a result may cross the lease fencing boundary."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class HarnessRunFenceReason(StrEnum):
    """Mechanical reason recorded for one fencing decision."""

    CURRENT = "current"
    MISSING = "missing"
    RELEASED = "released"
    CLOCK_REGRESSION = "clock_regression"
    EXPIRED = "expired"
    OWNER_MISMATCH = "owner_mismatch"
    EPOCH_MISMATCH = "epoch_mismatch"


@dataclass(frozen=True, slots=True)
class HarnessRunLease:
    """Current durable ownership token for one workspace-scoped run."""

    workspace_root: str
    run_kind: HarnessRunKind
    run_id: str
    owner_id: str
    epoch: int
    state: HarnessRunLeaseState
    acquired_at: str
    expires_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class HarnessRunFenceReceipt:
    """Immutable audit receipt for accepting or rejecting one run result."""

    workspace_root: str
    run_kind: HarnessRunKind
    run_id: str
    operation_id: str
    presented_owner_id: str
    presented_epoch: int
    active_owner_id: str
    active_epoch: int
    decision: HarnessRunFenceDecision
    reason: HarnessRunFenceReason
    checked_at: str

    @property
    def accepted(self) -> bool:
        return self.decision is HarnessRunFenceDecision.ACCEPTED


__all__ = [
    "HarnessRunFenceDecision",
    "HarnessRunFenceReason",
    "HarnessRunFenceReceipt",
    "HarnessRunKind",
    "HarnessRunLease",
    "HarnessRunLeaseState",
]
