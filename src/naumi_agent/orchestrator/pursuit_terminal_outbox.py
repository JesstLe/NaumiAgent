"""Durable terminal publication facts for admitted Pursuit recovery attempts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_OUTBOX_ID_RE = re.compile(r"^ptout-[0-9a-f]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^recovery-[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_ID_RE = re.compile(r"^pchk_[0-9a-f]{24}$")
_MAX_TIMESTAMP = 253_402_300_799.0
_FAILURE_CODE_RE = re.compile(r"^(?:|[a-z][a-z0-9_]{0,63})$")
_RUN_RECEIPT_ID_RE = re.compile(r"^ptorun_[0-9a-f]{24}$")


class PursuitTerminalOutboxState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"


class PursuitTerminalDispatchState(StrEnum):
    IDLE = "idle"
    CLAIMED = "claimed"
    DELIVERED = "delivered"


class PursuitTerminalOutboxRunStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_DUE = "no_due"
    FAILED = "failed"


class PursuitTerminalOutboxRunReceipt(BaseModel):
    """Immutable, identity-free receipt for one explicit bounded recovery pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    receipt_id: str
    source_request_sha256: str
    status: PursuitTerminalOutboxRunStatus
    pending_before: int = Field(ge=0, le=10_000)
    pending_after: int = Field(ge=0, le=10_000)
    claimed: int = Field(ge=0, le=1_000)
    delivered: int = Field(ge=0, le=1_000)
    retry_scheduled: int = Field(ge=0, le=1_000)
    failures: int = Field(ge=0, le=1_000)
    failure_codes: tuple[str, ...] = ()
    created_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    receipt_sha256: str

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalOutboxRunReceipt:
        if not _RUN_RECEIPT_ID_RE.fullmatch(self.receipt_id):
            raise ValueError("terminal outbox run receipt_id 格式无效。")
        if not _SHA256_RE.fullmatch(self.source_request_sha256):
            raise ValueError("terminal outbox run request digest 格式无效。")
        if not _SHA256_RE.fullmatch(self.receipt_sha256):
            raise ValueError("terminal outbox run receipt digest 格式无效。")
        if self.receipt_id != f"ptorun_{self.source_request_sha256[:24]}":
            raise ValueError("terminal outbox run receipt_id 与请求摘要不一致。")
        if tuple(sorted(set(self.failure_codes))) != self.failure_codes or any(
            not code or not _FAILURE_CODE_RE.fullmatch(code)
            for code in self.failure_codes
        ):
            raise ValueError("terminal outbox run failure_codes 无效。")
        if self.delivered + self.retry_scheduled > self.claimed:
            raise ValueError("terminal outbox run 处置计数超过 claimed。")
        if self.status is PursuitTerminalOutboxRunStatus.NO_DUE and any((
            self.claimed,
            self.delivered,
            self.retry_scheduled,
            self.failures,
        )):
            raise ValueError("no_due terminal outbox run 不得携带执行计数。")
        if self.status is PursuitTerminalOutboxRunStatus.FAILED and not (
            self.failures or self.failure_codes
        ):
            raise ValueError("failed terminal outbox run 必须包含失败事实。")
        if self.receipt_sha256 != self.expected_sha256():
            raise ValueError("terminal outbox run receipt digest 不匹配。")
        return self

    def expected_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_terminal_outbox_run_receipt(
    *,
    source_request_id: str,
    status: PursuitTerminalOutboxRunStatus,
    pending_before: int,
    pending_after: int,
    claimed: int,
    delivered: int,
    retry_scheduled: int,
    failures: int,
    failure_codes: tuple[str, ...],
    created_at: float,
) -> PursuitTerminalOutboxRunReceipt:
    normalized_request = str(source_request_id or "").strip()
    if not normalized_request or len(normalized_request) > 256:
        raise ValueError("terminal outbox run source_request_id 必须为 1 到 256 个字符。")
    request_sha256 = hashlib.sha256(normalized_request.encode("utf-8")).hexdigest()
    payload = {
        "receipt_id": f"ptorun_{request_sha256[:24]}",
        "source_request_sha256": request_sha256,
        "status": status,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "claimed": claimed,
        "delivered": delivered,
        "retry_scheduled": retry_scheduled,
        "failures": failures,
        "failure_codes": tuple(sorted(set(failure_codes))),
        "created_at": created_at,
    }
    canonical = json.dumps(
        PursuitTerminalOutboxRunReceipt.model_construct(
            **payload,
            schema_version=1,
            receipt_sha256="",
        ).model_dump(mode="json", exclude={"receipt_sha256"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PursuitTerminalOutboxRunReceipt.model_validate({
        **payload,
        "receipt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    })


class PursuitTerminalOutboxRecord(BaseModel):
    """One authenticated state in the terminal outbox event chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    outbox_id: str
    attempt_id: str
    run_id: str
    admitted_attempt_sha256: str
    boundary_decision_id: str
    checkpoint_id: str
    sequence: int = Field(ge=1, le=2)
    state: PursuitTerminalOutboxState
    created_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    updated_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    delivered_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    terminal_attempt_sha256: str = ""

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalOutboxRecord:
        if not _OUTBOX_ID_RE.fullmatch(self.outbox_id):
            raise ValueError("terminal outbox_id 格式无效。")
        if not _ATTEMPT_ID_RE.fullmatch(self.attempt_id):
            raise ValueError("terminal outbox attempt_id 格式无效。")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("terminal outbox run_id 格式无效。")
        for label, value in (
            ("admitted attempt", self.admitted_attempt_sha256),
            ("boundary decision", self.boundary_decision_id),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"terminal outbox {label} digest 格式无效。")
        if not _CHECKPOINT_ID_RE.fullmatch(self.checkpoint_id):
            raise ValueError("terminal outbox checkpoint_id 格式无效。")
        if self.terminal_attempt_sha256 and not _SHA256_RE.fullmatch(
            self.terminal_attempt_sha256
        ):
            raise ValueError("terminal outbox terminal attempt digest 格式无效。")
        if self.outbox_id != pursuit_terminal_outbox_id(
            attempt_id=self.attempt_id,
            admitted_attempt_sha256=self.admitted_attempt_sha256,
            boundary_decision_id=self.boundary_decision_id,
            checkpoint_id=self.checkpoint_id,
        ):
            raise ValueError("terminal outbox_id 与终态事实不一致。")
        if not all(math.isfinite(value) for value in (
            self.created_at,
            self.updated_at,
            self.delivered_at,
        )):
            raise ValueError("terminal outbox 时间必须是有限值。")
        if self.updated_at < self.created_at:
            raise ValueError("terminal outbox updated_at 不得早于 created_at。")
        if self.state is PursuitTerminalOutboxState.PENDING:
            if self.sequence != 1 or self.delivered_at or self.terminal_attempt_sha256:
                raise ValueError("pending terminal outbox 不得携带 delivered 事实。")
        elif (
            self.sequence != 2
            or self.delivered_at < self.created_at
            or self.updated_at != self.delivered_at
            or not self.terminal_attempt_sha256
        ):
            raise ValueError("delivered terminal outbox 事实不完整。")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PursuitTerminalOutboxDispatch(BaseModel):
    """Fenced scheduling state for one pending outbox record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    outbox_id: str
    pending_outbox_sha256: str
    sequence: int = Field(ge=1, le=1_000_000)
    state: PursuitTerminalDispatchState
    claim_owner_sha256: str = ""
    claim_epoch: int = Field(default=0, ge=0)
    claim_expires_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    attempt_count: int = Field(default=0, ge=0, le=1_000_000)
    next_attempt_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    last_failure_code: str = ""
    created_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    updated_at: float = Field(gt=0, le=_MAX_TIMESTAMP)

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalOutboxDispatch:
        if not _OUTBOX_ID_RE.fullmatch(self.outbox_id):
            raise ValueError("terminal dispatch outbox_id 格式无效。")
        if not _SHA256_RE.fullmatch(self.pending_outbox_sha256):
            raise ValueError("terminal dispatch pending outbox digest 格式无效。")
        if self.claim_owner_sha256 and not _SHA256_RE.fullmatch(
            self.claim_owner_sha256
        ):
            raise ValueError("terminal dispatch owner digest 格式无效。")
        if not _FAILURE_CODE_RE.fullmatch(self.last_failure_code):
            raise ValueError("terminal dispatch failure code 格式无效。")
        if not all(math.isfinite(value) for value in (
            self.claim_expires_at,
            self.next_attempt_at,
            self.created_at,
            self.updated_at,
        )):
            raise ValueError("terminal dispatch 时间必须是有限值。")
        if self.updated_at < self.created_at:
            raise ValueError("terminal dispatch updated_at 不得早于 created_at。")
        if self.state is PursuitTerminalDispatchState.IDLE:
            if self.claim_owner_sha256 or self.claim_expires_at:
                raise ValueError("idle terminal dispatch 不得携带 live claim。")
            if self.next_attempt_at < self.created_at:
                raise ValueError("idle terminal dispatch 下次尝试时间无效。")
        elif self.state is PursuitTerminalDispatchState.CLAIMED:
            if (
                not self.claim_owner_sha256
                or self.claim_epoch <= 0
                or self.claim_expires_at <= self.updated_at
                or self.attempt_count <= 0
                or self.next_attempt_at
            ):
                raise ValueError("claimed terminal dispatch 事实不完整。")
        elif (
            self.claim_owner_sha256
            or self.claim_expires_at
            or self.next_attempt_at
        ):
            raise ValueError("delivered terminal dispatch 不得携带调度事实。")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def pursuit_terminal_outbox_id(
    *,
    attempt_id: str,
    admitted_attempt_sha256: str,
    boundary_decision_id: str,
    checkpoint_id: str,
) -> str:
    payload = json.dumps(
        {
            "admitted_attempt_sha256": admitted_attempt_sha256,
            "attempt_id": attempt_id,
            "boundary_decision_id": boundary_decision_id,
            "checkpoint_id": checkpoint_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ptout-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "PursuitTerminalDispatchState",
    "PursuitTerminalOutboxDispatch",
    "PursuitTerminalOutboxRecord",
    "PursuitTerminalOutboxState",
    "PursuitTerminalOutboxRunReceipt",
    "PursuitTerminalOutboxRunStatus",
    "new_terminal_outbox_run_receipt",
    "pursuit_terminal_outbox_id",
]
