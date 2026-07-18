"""Typed durable heartbeat contracts for long-running Harness workers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from naumi_agent.harness.run_lease import HarnessRunKind


class HarnessHeartbeatPhase(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class HarnessHeartbeatHealth(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DRAINING = "draining"
    STALE = "stale"
    OFFLINE = "offline"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOCK_REGRESSION = "clock_regression"


@dataclass(frozen=True, slots=True)
class HarnessHeartbeat:
    workspace_root: str
    subject_kind: HarnessRunKind
    subject_id: str
    instance_id: str
    epoch: int
    sequence: int
    phase: HarnessHeartbeatPhase
    observed_at: str
    timeout_seconds: int
    detail_code: str


@dataclass(frozen=True, slots=True)
class HarnessHeartbeatSnapshot:
    heartbeat: HarnessHeartbeat
    health: HarnessHeartbeatHealth
    age_seconds: float
    assessed_at: str


def assess_heartbeat(
    heartbeat: HarnessHeartbeat,
    *,
    now: str,
    offline_multiplier: float = 3.0,
) -> HarnessHeartbeatSnapshot:
    """Mechanically classify one heartbeat without mutating durable state."""
    if not math.isfinite(offline_multiplier) or offline_multiplier < 1.0:
        raise ValueError("offline_multiplier 必须大于或等于 1。")
    if (
        isinstance(heartbeat.timeout_seconds, bool)
        or not isinstance(heartbeat.timeout_seconds, int)
        or not 3 <= heartbeat.timeout_seconds <= 86_400
    ):
        raise ValueError("Heartbeat timeout_seconds 必须在 3 到 86400 之间。")
    try:
        observed = datetime.fromisoformat(heartbeat.observed_at)
        assessed = datetime.fromisoformat(now)
    except (TypeError, ValueError) as exc:
        raise ValueError("Heartbeat 时间必须是 ISO 8601。") from exc
    if (
        observed.tzinfo is None
        or observed.utcoffset() is None
        or assessed.tzinfo is None
        or assessed.utcoffset() is None
    ):
        raise ValueError("Heartbeat 时间必须包含时区偏移。")

    age = (assessed - observed).total_seconds()
    if age < 0:
        health = HarnessHeartbeatHealth.CLOCK_REGRESSION
    elif heartbeat.phase is HarnessHeartbeatPhase.STOPPED:
        health = HarnessHeartbeatHealth.STOPPED
    elif heartbeat.phase is HarnessHeartbeatPhase.FAILED:
        health = HarnessHeartbeatHealth.FAILED
    elif age > heartbeat.timeout_seconds * offline_multiplier:
        health = HarnessHeartbeatHealth.OFFLINE
    elif age > heartbeat.timeout_seconds:
        health = HarnessHeartbeatHealth.STALE
    elif heartbeat.phase is HarnessHeartbeatPhase.STARTING:
        health = HarnessHeartbeatHealth.STARTING
    elif heartbeat.phase is HarnessHeartbeatPhase.DRAINING:
        health = HarnessHeartbeatHealth.DRAINING
    else:
        health = HarnessHeartbeatHealth.HEALTHY
    return HarnessHeartbeatSnapshot(
        heartbeat=heartbeat,
        health=health,
        age_seconds=max(0.0, age),
        assessed_at=assessed.isoformat(),
    )


__all__ = [
    "HarnessHeartbeat",
    "HarnessHeartbeatHealth",
    "HarnessHeartbeatPhase",
    "HarnessHeartbeatSnapshot",
    "assess_heartbeat",
]
