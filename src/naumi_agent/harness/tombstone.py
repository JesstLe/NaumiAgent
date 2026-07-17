"""Non-sensitive retry tombstones for failed Session reconciliation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from naumi_agent.harness.retention import LifecyclePolicy


class ReconciliationFailureStage(StrEnum):
    SESSION_DELETE = "session_delete"
    HARNESS_RECORDS = "harness_records"
    ARTIFACT_GC = "artifact_gc"


class ReconciliationFailureCode(StrEnum):
    SESSION_STORE_ERROR = "session_store_error"
    HARNESS_STORE_ERROR = "harness_store_error"
    CANCELLED = "cancelled"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class ReconciliationTombstoneStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class ReconciliationTombstone:
    request_id: str
    policy: LifecyclePolicy
    stage: ReconciliationFailureStage
    error_code: ReconciliationFailureCode
    status: ReconciliationTombstoneStatus
    attempt_count: int
    max_attempts: int
    next_retry_at: str
    lease_owner: str
    lease_expires_at: str
    last_failure_id: str
    created_at: str
    updated_at: str


def compute_retry_delay_seconds(
    request_id: str,
    attempt: int,
    *,
    base_seconds: int = 5,
    cap_seconds: int = 3_600,
) -> int:
    """Return deterministic exponential backoff with stable bounded jitter."""
    normalized_request_id = request_id.strip() if isinstance(request_id, str) else ""
    if not normalized_request_id:
        raise ValueError("request_id 不能为空。")
    if attempt < 1:
        raise ValueError("attempt 必须大于或等于 1。")
    if base_seconds < 1 or cap_seconds < base_seconds:
        raise ValueError("重试退避边界无效。")
    exponential = min(cap_seconds, base_seconds * (2 ** min(attempt - 1, 30)))
    digest = hashlib.sha256(
        f"{normalized_request_id}:{attempt}".encode()
    ).digest()
    jitter_ceiling = max(1, exponential // 5)
    jitter = int.from_bytes(digest[:4], "big") % (jitter_ceiling + 1)
    return min(cap_seconds, exponential + jitter)
