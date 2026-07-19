"""Canonical bounded runtime-health projection shared by terminal frontends."""

from __future__ import annotations

import math
import re
from typing import Protocol

from naumi_agent.harness.heartbeat_retention_periodic import (
    RuntimeHeartbeatRetentionState,
)

_PUBLIC_COUNT_MAX = 9_007_199_254_740_991
_PUBLIC_DELAY_MAX_SECONDS = 604_800.0
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class RuntimeHeartbeatRetentionSnapshotLike(Protocol):
    state: RuntimeHeartbeatRetentionState
    cycle_count: int
    deleted_count: int
    failure_count: int
    last_error_code: str
    last_cycle_at: str
    next_delay_seconds: float


def runtime_heartbeat_retention_status_payload(
    *,
    configured_enabled: bool,
    available: bool,
    snapshot: RuntimeHeartbeatRetentionSnapshotLike | None,
) -> dict[str, object]:
    """Return one exact public status shape for New UI and TUI."""
    state = "unavailable" if not available else "stopped"
    if available and snapshot is not None:
        state = (
            snapshot.state.value
            if isinstance(snapshot.state, RuntimeHeartbeatRetentionState)
            else "failed"
        )
    payload: dict[str, object] = {
        "configured_enabled": configured_enabled is True,
        "state": state,
        "cycle_count": 0,
        "deleted_count": 0,
        "failure_count": 0,
        "last_error_code": "",
        "last_cycle_at": "",
        "next_delay_seconds": 0.0,
    }
    if not available or snapshot is None:
        return payload
    payload.update(
        {
            "cycle_count": _bounded_count(snapshot.cycle_count),
            "deleted_count": _bounded_count(snapshot.deleted_count),
            "failure_count": _bounded_count(snapshot.failure_count),
            "last_error_code": _safe_error_code(snapshot.last_error_code),
            "last_cycle_at": _safe_timestamp(snapshot.last_cycle_at),
            "next_delay_seconds": _bounded_delay(snapshot.next_delay_seconds),
        }
    )
    return payload


def _bounded_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(_PUBLIC_COUNT_MAX, max(0, parsed))


def _safe_error_code(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text if _ERROR_CODE.fullmatch(text) else "status_invalid"


def _safe_timestamp(value: object) -> str:
    text = str(value or "")
    return text if _TIMESTAMP.fullmatch(text) else ""


def _bounded_delay(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return min(_PUBLIC_DELAY_MAX_SECONDS, max(0.0, parsed))


__all__ = [
    "RuntimeHeartbeatRetentionSnapshotLike",
    "runtime_heartbeat_retention_status_payload",
]
