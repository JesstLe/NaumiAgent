"""Immutable authority receipts for exact Pursuit terminal dead-letter abandon."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DEAD_LETTER_ID_RE = re.compile(r"^ptfail_[0-9a-f]{24}$")
_RECEIPT_ID_RE = re.compile(r"^ptabn_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMESTAMP = 253_402_300_799.0


class PursuitTerminalDeadLetterAbandonReason(StrEnum):
    """Bounded operator reasons that remain safe to project and aggregate."""

    NO_LONGER_REQUIRED = "no_longer_required"
    SUPERSEDED = "superseded"
    EXTERNAL_RESOLUTION = "external_resolution"
    INVALID_TARGET = "invalid_target"


class PursuitTerminalDeadLetterAbandonReceipt(BaseModel):
    """First-write-wins proof that one exact dead letter was abandoned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    receipt_id: str
    dead_letter_id: str
    source_request_sha256: str
    prior_failure_sha256: str
    dispatch_sha256: str
    failure_sequence: int = Field(ge=1, le=1_000_000)
    reason: PursuitTerminalDeadLetterAbandonReason
    abandoned_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    receipt_sha256: str

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalDeadLetterAbandonReceipt:
        if not _RECEIPT_ID_RE.fullmatch(self.receipt_id):
            raise ValueError("terminal dead-letter abandon receipt_id 格式无效。")
        if not _DEAD_LETTER_ID_RE.fullmatch(self.dead_letter_id):
            raise ValueError("terminal dead-letter abandon target 格式无效。")
        for label, value in (
            ("source request", self.source_request_sha256),
            ("prior failure", self.prior_failure_sha256),
            ("dispatch", self.dispatch_sha256),
            ("receipt", self.receipt_sha256),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"terminal dead-letter abandon {label} digest 格式无效。")
        if not math.isfinite(self.abandoned_at):
            raise ValueError("terminal dead-letter abandon 时间必须是有限值。")
        if self.receipt_id != self.expected_receipt_id():
            raise ValueError("terminal dead-letter abandon receipt_id 与事实不一致。")
        if self.receipt_sha256 != self.expected_sha256():
            raise ValueError("terminal dead-letter abandon receipt digest 不匹配。")
        return self

    def expected_receipt_id(self) -> str:
        canonical = json.dumps(
            {
                "dead_letter_id": self.dead_letter_id,
                "prior_failure_sha256": self.prior_failure_sha256,
                "source_request_sha256": self.source_request_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "ptabn_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def expected_sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"receipt_sha256"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def new_pursuit_terminal_dead_letter_abandon_receipt(
    *,
    dead_letter_id: str,
    source_request_id: str,
    prior_failure_sha256: str,
    dispatch_sha256: str,
    failure_sequence: int,
    reason: PursuitTerminalDeadLetterAbandonReason,
    abandoned_at: float,
) -> PursuitTerminalDeadLetterAbandonReceipt:
    normalized_request = str(source_request_id or "").strip()
    if not normalized_request or len(normalized_request) > 256:
        raise ValueError("terminal dead-letter abandon source request id 无效。")
    request_sha256 = hashlib.sha256(normalized_request.encode("utf-8")).hexdigest()
    payload = {
        "dead_letter_id": dead_letter_id,
        "source_request_sha256": request_sha256,
        "prior_failure_sha256": prior_failure_sha256,
        "dispatch_sha256": dispatch_sha256,
        "failure_sequence": failure_sequence,
        "reason": reason,
        "abandoned_at": abandoned_at,
    }
    provisional = PursuitTerminalDeadLetterAbandonReceipt.model_construct(
        **payload,
        schema_version=1,
        receipt_id="",
        receipt_sha256="",
    )
    with_id = {**payload, "receipt_id": provisional.expected_receipt_id()}
    digest_source = PursuitTerminalDeadLetterAbandonReceipt.model_construct(
        **with_id,
        schema_version=1,
        receipt_sha256="",
    )
    return PursuitTerminalDeadLetterAbandonReceipt.model_validate({
        **with_id,
        "receipt_sha256": digest_source.expected_sha256(),
    })


__all__ = [
    "PursuitTerminalDeadLetterAbandonReason",
    "PursuitTerminalDeadLetterAbandonReceipt",
    "new_pursuit_terminal_dead_letter_abandon_receipt",
]
