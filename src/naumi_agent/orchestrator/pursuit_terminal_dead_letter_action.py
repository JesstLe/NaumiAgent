"""Immutable authority receipts for manual Pursuit terminal dead-letter actions."""

from __future__ import annotations

import hashlib
import json
import math
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DEAD_LETTER_ID_RE = re.compile(r"^ptfail_[0-9a-f]{24}$")
_RECEIPT_ID_RE = re.compile(r"^ptreq_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMESTAMP = 253_402_300_799.0


class PursuitTerminalDeadLetterRequeueReceipt(BaseModel):
    """First-write-wins proof that one exact dead letter was requeued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    receipt_id: str
    dead_letter_id: str
    source_request_sha256: str
    prior_failure_sha256: str
    dispatch_before_sha256: str
    dispatch_after_sha256: str
    failure_sequence: int = Field(ge=1, le=1_000_000)
    requeued_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    next_attempt_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    receipt_sha256: str

    @model_validator(mode="after")
    def _integrity(self) -> PursuitTerminalDeadLetterRequeueReceipt:
        if not _RECEIPT_ID_RE.fullmatch(self.receipt_id):
            raise ValueError("terminal dead-letter requeue receipt_id 格式无效。")
        if not _DEAD_LETTER_ID_RE.fullmatch(self.dead_letter_id):
            raise ValueError("terminal dead-letter requeue target 格式无效。")
        for label, value in (
            ("source request", self.source_request_sha256),
            ("prior failure", self.prior_failure_sha256),
            ("dispatch before", self.dispatch_before_sha256),
            ("dispatch after", self.dispatch_after_sha256),
            ("receipt", self.receipt_sha256),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"terminal dead-letter requeue {label} digest 格式无效。")
        if not all(math.isfinite(value) for value in (
            self.requeued_at,
            self.next_attempt_at,
        )):
            raise ValueError("terminal dead-letter requeue 时间必须是有限值。")
        if self.next_attempt_at != self.requeued_at:
            raise ValueError("manual requeue 必须立即授予下一次领取资格。")
        if self.receipt_id != self.expected_receipt_id():
            raise ValueError("terminal dead-letter requeue receipt_id 与事实不一致。")
        if self.receipt_sha256 != self.expected_sha256():
            raise ValueError("terminal dead-letter requeue receipt digest 不匹配。")
        return self

    def expected_receipt_id(self) -> str:
        payload = {
            "dead_letter_id": self.dead_letter_id,
            "prior_failure_sha256": self.prior_failure_sha256,
            "source_request_sha256": self.source_request_sha256,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "ptreq_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def expected_sha256(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"receipt_sha256"},
        )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def new_pursuit_terminal_dead_letter_requeue_receipt(
    *,
    dead_letter_id: str,
    source_request_id: str,
    prior_failure_sha256: str,
    dispatch_before_sha256: str,
    dispatch_after_sha256: str,
    failure_sequence: int,
    requeued_at: float,
) -> PursuitTerminalDeadLetterRequeueReceipt:
    normalized_request = str(source_request_id or "").strip()
    if not normalized_request or len(normalized_request) > 256:
        raise ValueError("terminal dead-letter requeue source request id 无效。")
    request_sha256 = hashlib.sha256(normalized_request.encode("utf-8")).hexdigest()
    payload = {
        "dead_letter_id": dead_letter_id,
        "source_request_sha256": request_sha256,
        "prior_failure_sha256": prior_failure_sha256,
        "dispatch_before_sha256": dispatch_before_sha256,
        "dispatch_after_sha256": dispatch_after_sha256,
        "failure_sequence": failure_sequence,
        "requeued_at": requeued_at,
        "next_attempt_at": requeued_at,
    }
    provisional = PursuitTerminalDeadLetterRequeueReceipt.model_construct(
        **payload,
        schema_version=1,
        receipt_id="",
        receipt_sha256="",
    )
    with_id = {**payload, "receipt_id": provisional.expected_receipt_id()}
    digest_source = PursuitTerminalDeadLetterRequeueReceipt.model_construct(
        **with_id,
        schema_version=1,
        receipt_sha256="",
    )
    return PursuitTerminalDeadLetterRequeueReceipt.model_validate({
        **with_id,
        "receipt_sha256": digest_source.expected_sha256(),
    })


__all__ = [
    "PursuitTerminalDeadLetterRequeueReceipt",
    "new_pursuit_terminal_dead_letter_requeue_receipt",
]
