"""Tamper-evident request ledger for persisted Pursuit recovery."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ATTEMPT_ID_RE = re.compile(r"^recovery-[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESULT_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_SOURCE_REQUEST_ID_CHARS = 512
_MAX_TIMESTAMP = 253_402_300_799.0


class PursuitRecoveryAttemptState(StrEnum):
    REQUESTED = "requested"
    ADMITTED = "admitted"
    RESOLVED = "resolved"
    FAILED = "failed"


class PursuitRecoveryAttempt(BaseModel):
    """One immutable state in a recovery request event chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    attempt_id: str
    run_id: str
    source_request_sha256: str
    sequence: int = Field(ge=1, le=3)
    state: PursuitRecoveryAttemptState
    requested_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    updated_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    admitted_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    resolved_at: float = Field(default=0, ge=0, le=_MAX_TIMESTAMP)
    lease_epoch: int = Field(default=0, ge=0)
    checkpoint_id: str = Field(default="", max_length=128)
    result_code: str = Field(default="", max_length=64)
    boundary_decision_id: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _integrity(self) -> PursuitRecoveryAttempt:
        if not _ATTEMPT_ID_RE.fullmatch(self.attempt_id):
            raise ValueError("recovery attempt_id 格式无效。")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("recovery run_id 格式无效。")
        if not _SHA256_RE.fullmatch(self.source_request_sha256):
            raise ValueError("recovery source request digest 格式无效。")
        if self.attempt_id != _attempt_id_from_digest(
            run_id=self.run_id,
            source_request_sha256=self.source_request_sha256,
        ):
            raise ValueError("recovery attempt_id 与请求事实不一致。")
        if not all(math.isfinite(value) for value in (
            self.requested_at,
            self.updated_at,
            self.admitted_at,
            self.resolved_at,
        )):
            raise ValueError("recovery attempt 时间必须是有限值。")
        if self.updated_at < self.requested_at:
            raise ValueError("recovery attempt updated_at 不得早于 requested_at。")
        if self.checkpoint_id != self.checkpoint_id.strip():
            raise ValueError("recovery checkpoint_id 不得包含首尾空白。")
        if self.result_code and not _RESULT_CODE_RE.fullmatch(self.result_code):
            raise ValueError("recovery result_code 格式无效。")
        if self.boundary_decision_id and not _SHA256_RE.fullmatch(
            self.boundary_decision_id
        ):
            raise ValueError("recovery boundary decision digest 格式无效。")

        if self.state is PursuitRecoveryAttemptState.REQUESTED:
            if self.sequence != 1 or any((
                self.admitted_at,
                self.resolved_at,
                self.lease_epoch,
            )) or self.checkpoint_id or self.result_code or self.boundary_decision_id:
                raise ValueError("requested recovery attempt 不得携带后续阶段事实。")
        elif self.state is PursuitRecoveryAttemptState.ADMITTED:
            if (
                self.sequence != 2
                or self.admitted_at <= 0
                or self.admitted_at < self.requested_at
                or self.resolved_at
                or not self.checkpoint_id
                or self.result_code
                or self.boundary_decision_id
            ):
                raise ValueError("admitted recovery attempt 事实不完整。")
        else:
            expected_sequence = 3 if self.admitted_at else 2
            if (
                self.sequence != expected_sequence
                or self.resolved_at <= 0
                or self.resolved_at < self.requested_at
                or not self.result_code
            ):
                raise ValueError("terminal recovery attempt 事实不完整。")
            if self.admitted_at:
                if (
                    self.admitted_at < self.requested_at
                    or self.resolved_at < self.admitted_at
                    or not self.checkpoint_id
                ):
                    raise ValueError("admitted recovery terminal 事实不完整。")
            elif self.lease_epoch or self.checkpoint_id:
                raise ValueError("未 admitted 的 recovery attempt 不得携带 lease/checkpoint。")
            if (
                self.state is PursuitRecoveryAttemptState.FAILED
                and self.boundary_decision_id
            ):
                raise ValueError("failed recovery attempt 不得伪造机械边界裁判。")
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


def new_recovery_attempt(
    *,
    run_id: str,
    source_request_id: str,
    requested_at: float,
) -> PursuitRecoveryAttempt:
    """Create a privacy-safe content identity without retaining request text."""
    normalized_run_id = str(run_id or "").strip()
    normalized_request_id = _normalize_source_request_id(source_request_id)
    source_digest = hashlib.sha256(
        normalized_request_id.encode("utf-8")
    ).hexdigest()
    attempt_id = pursuit_recovery_attempt_id(
        run_id=normalized_run_id,
        source_request_id=normalized_request_id,
    )
    return PursuitRecoveryAttempt(
        attempt_id=attempt_id,
        run_id=normalized_run_id,
        source_request_sha256=source_digest,
        sequence=1,
        state=PursuitRecoveryAttemptState.REQUESTED,
        requested_at=requested_at,
        updated_at=requested_at,
    )


def pursuit_recovery_attempt_id(
    *,
    run_id: str,
    source_request_id: str,
) -> str:
    """Return the stable opaque identity shared by caller and runtime."""
    normalized_run_id = str(run_id or "").strip()
    normalized_request_id = _normalize_source_request_id(source_request_id)
    source_digest = hashlib.sha256(
        normalized_request_id.encode("utf-8")
    ).hexdigest()
    return _attempt_id_from_digest(
        run_id=normalized_run_id,
        source_request_sha256=source_digest,
    )


def _attempt_id_from_digest(
    *,
    run_id: str,
    source_request_sha256: str,
) -> str:
    identity = hashlib.sha256(
        json.dumps(
            {
                "run_id": run_id,
                "source_request_sha256": source_request_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"recovery-{identity}"


def _normalize_source_request_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("recovery source_request_id 不能为空。")
    if len(normalized) > _MAX_SOURCE_REQUEST_ID_CHARS:
        raise ValueError(
            "recovery source_request_id 过长，"
            f"最多 {_MAX_SOURCE_REQUEST_ID_CHARS} 个字符。"
        )
    return normalized


def format_recovery_attempts(
    attempts: list[PursuitRecoveryAttempt],
) -> str:
    """Render a bounded public history without request digests."""
    if not attempts:
        return "#### 恢复请求\n- 暂无"
    lines = ["#### 恢复请求"]
    labels = {
        PursuitRecoveryAttemptState.REQUESTED: "已记录",
        PursuitRecoveryAttemptState.ADMITTED: "已准入",
        PursuitRecoveryAttemptState.RESOLVED: "已完成",
        PursuitRecoveryAttemptState.FAILED: "失败关闭",
    }
    result_labels = {
        "already_terminal": "运行已处于终态",
        "cancelled": "恢复被取消",
        "checkpoint_inconsistent": "checkpoint 不一致",
        "checkpoint_invalid": "checkpoint 校验失败",
        "checkpoint_persistence_error": "checkpoint 持久化失败",
        "checkpoint_required": "缺少可恢复 checkpoint",
        "criteria_incomplete": "验收条件尚未完成",
        "internal_error": "恢复内部异常",
        "lease_lost": "恢复租约丢失",
        "lease_unavailable": "恢复租约不可用",
        "operation_busy": "运行器正忙",
        "reconcile_required": "需要人工核对",
        "resume_checked": "持久状态已检查",
        "waiting_for_background": "等待后台任务",
        "waiting_for_interaction": "等待用户交互",
    }
    for attempt in attempts:
        observed = datetime.fromtimestamp(
            attempt.updated_at,
            UTC,
        ).isoformat(timespec="seconds")
        result = ""
        if attempt.result_code:
            result_label = result_labels.get(
                attempt.result_code,
                attempt.result_code,
            )
            result = f" · {result_label}（{attempt.result_code}）"
        lines.append(
            f"- `{attempt.attempt_id}` · {labels[attempt.state]}{result} · {observed}"
        )
        if attempt.admitted_at:
            lease = (
                f"lease epoch {attempt.lease_epoch}"
                if attempt.lease_epoch
                else "embedded fallback"
            )
            lines.append(
                f"  - 准入：{lease} · checkpoint "
                f"`{attempt.checkpoint_id[:16]}`"
            )
            if attempt.state is PursuitRecoveryAttemptState.ADMITTED:
                lines.append(
                    "  - 对账："
                    f"`/pursue reconcile {attempt.attempt_id}`"
                )
        if attempt.boundary_decision_id:
            lines.append(
                "  - 机械裁判："
                f"`{attempt.boundary_decision_id[:12]}`"
            )
    return "\n".join(lines)


__all__ = [
    "format_recovery_attempts",
    "PursuitRecoveryAttempt",
    "PursuitRecoveryAttemptState",
    "new_recovery_attempt",
    "pursuit_recovery_attempt_id",
]
