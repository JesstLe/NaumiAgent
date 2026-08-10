"""Append-only failure authority for Pursuit terminal outbox delivery."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_OUTBOX_ID_RE = re.compile(r"^ptout-[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^ptfail_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_TIMESTAMP = 253_402_300_799.0


class PursuitTerminalOutboxFailureDisposition(StrEnum):
    """Mechanical retry authority carried by one failure event."""

    RETRYABLE = "retryable"
    RETRY_EXHAUSTED = "retry_exhausted"
    PERMANENT = "permanent"


class PursuitTerminalOutboxFailureEvent(BaseModel):
    """One authenticated failure linked to an exact claimed dispatch event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    event_id: str
    outbox_id: str
    pending_outbox_sha256: str
    claimed_dispatch_sha256: str
    sequence: int = Field(ge=1, le=1_000_000)
    attempt_count: int = Field(ge=1, le=1_000_000)
    failure_code: str
    disposition: PursuitTerminalOutboxFailureDisposition
    previous_failure_sha256: str = ""
    occurred_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    retry_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    automatic_retry_authority: bool
    dead_letter_authority: bool
    manual_review_required: bool

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalOutboxFailureEvent:
        if not _EVENT_ID_RE.fullmatch(self.event_id):
            raise ValueError("terminal outbox failure event_id 格式无效。")
        if not _OUTBOX_ID_RE.fullmatch(self.outbox_id):
            raise ValueError("terminal outbox failure outbox_id 格式无效。")
        for label, value in (
            ("pending outbox", self.pending_outbox_sha256),
            ("claimed dispatch", self.claimed_dispatch_sha256),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"terminal outbox failure {label} digest 格式无效。")
        if self.previous_failure_sha256 and not _SHA256_RE.fullmatch(
            self.previous_failure_sha256
        ):
            raise ValueError("terminal outbox failure previous digest 格式无效。")
        if not _FAILURE_CODE_RE.fullmatch(self.failure_code):
            raise ValueError("terminal outbox failure code 格式无效。")
        if not all(math.isfinite(value) for value in (self.occurred_at, self.retry_at)):
            raise ValueError("terminal outbox failure 时间必须是有限值。")
        retryable = self.disposition is PursuitTerminalOutboxFailureDisposition.RETRYABLE
        if retryable:
            if (
                self.retry_at <= self.occurred_at
                or not self.automatic_retry_authority
                or self.dead_letter_authority
                or self.manual_review_required
            ):
                raise ValueError("retryable terminal outbox failure 权威不一致。")
        elif (
            self.retry_at
            or self.automatic_retry_authority
            or not self.dead_letter_authority
            or not self.manual_review_required
        ):
            raise ValueError("dead-letter terminal outbox failure 权威不一致。")
        if self.event_id != self.expected_event_id():
            raise ValueError("terminal outbox failure event_id 与事实不一致。")
        return self

    def expected_event_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"event_id"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "ptfail_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def new_pursuit_terminal_outbox_failure_event(
    *,
    outbox_id: str,
    pending_outbox_sha256: str,
    claimed_dispatch_sha256: str,
    sequence: int,
    attempt_count: int,
    failure_code: str,
    disposition: PursuitTerminalOutboxFailureDisposition,
    previous_failure_sha256: str,
    occurred_at: float,
    retry_at: float,
) -> PursuitTerminalOutboxFailureEvent:
    retryable = disposition is PursuitTerminalOutboxFailureDisposition.RETRYABLE
    payload = {
        "outbox_id": outbox_id,
        "pending_outbox_sha256": pending_outbox_sha256,
        "claimed_dispatch_sha256": claimed_dispatch_sha256,
        "sequence": sequence,
        "attempt_count": attempt_count,
        "failure_code": failure_code,
        "disposition": disposition,
        "previous_failure_sha256": previous_failure_sha256,
        "occurred_at": occurred_at,
        "retry_at": retry_at,
        "automatic_retry_authority": retryable,
        "dead_letter_authority": not retryable,
        "manual_review_required": not retryable,
    }
    constructed = PursuitTerminalOutboxFailureEvent.model_construct(
        **payload,
        schema_version=1,
        event_id="",
    )
    return PursuitTerminalOutboxFailureEvent.model_validate({
        **payload,
        "event_id": constructed.expected_event_id(),
    })


__all__ = [
    "PursuitTerminalOutboxFailureDisposition",
    "PursuitTerminalOutboxFailureEvent",
    "new_pursuit_terminal_outbox_failure_event",
]
