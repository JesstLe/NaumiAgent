"""Fenced reconciliation for interrupted Pursuit recovery attempts."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatHealth,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import (
    HarnessRunFenceReceipt,
    HarnessRunKind,
    HarnessRunLease,
    HarnessRunLeaseState,
)
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    PursuitRecoveryAttemptState,
)

if TYPE_CHECKING:
    from naumi_agent.orchestrator.pursuit_store import PursuitStore

_ATTEMPT_ID_RE = re.compile(r"^recovery-[0-9a-f]{64}$")
_RECEIPT_ID_RE = re.compile(r"^precon-[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FENCE_OPERATION_RE = re.compile(r"^precon-[0-9a-f]{32}$")
_MAX_TIMESTAMP = 253_402_300_799.0
DEFAULT_RECONCILE_GRACE_SECONDS = 30.0
DEFAULT_RECONCILE_LEASE_SECONDS = 30
logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PursuitRecoveryReconcileError(RuntimeError):
    """A stable, user-safe reason why reconciliation cannot mutate state."""

    def __init__(self, code: str, message: str) -> None:
        if not _CODE_RE.fullmatch(code):
            raise ValueError("Pursuit recovery reconcile error code 格式无效。")
        super().__init__(message)
        self.code = code
        self.public_message = message


class PursuitRecoveryReconciliationReceipt(_StrictModel):
    """Immutable proof binding fencing authority to same-store terminal facts."""

    schema_version: Literal[1] = 1
    receipt_id: str
    attempt_id: str
    run_id: str = Field(min_length=1, max_length=128)
    attempt_before_sha256: str
    attempt_after_sha256: str
    admitted_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    reconciled_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    admitted_lease_epoch: int = Field(gt=0)
    fence_epoch: int = Field(gt=0)
    fence_operation_id: str
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_created_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    boundary_decision_id: str
    boundary_recorded_at: float = Field(gt=0, le=_MAX_TIMESTAMP)
    result_code: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _integrity(self) -> PursuitRecoveryReconciliationReceipt:
        if not _RECEIPT_ID_RE.fullmatch(self.receipt_id):
            raise ValueError("Pursuit recovery reconcile receipt_id 格式无效。")
        if not _ATTEMPT_ID_RE.fullmatch(self.attempt_id):
            raise ValueError("Pursuit recovery reconcile attempt_id 格式无效。")
        for value in (
            self.attempt_before_sha256,
            self.attempt_after_sha256,
            self.boundary_decision_id,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("Pursuit recovery reconcile 摘要格式无效。")
        if not _FENCE_OPERATION_RE.fullmatch(self.fence_operation_id):
            raise ValueError("Pursuit recovery reconcile fence operation 格式无效。")
        if not _CODE_RE.fullmatch(self.result_code):
            raise ValueError("Pursuit recovery reconcile result_code 格式无效。")
        if not all(math.isfinite(value) for value in (
            self.admitted_at,
            self.reconciled_at,
            self.checkpoint_created_at,
            self.boundary_recorded_at,
        )):
            raise ValueError("Pursuit recovery reconcile 时间必须是有限值。")
        if self.reconciled_at < self.admitted_at:
            raise ValueError("Pursuit recovery reconcile 时间不得早于准入。")
        if self.checkpoint_created_at <= self.admitted_at:
            raise ValueError("Pursuit recovery reconcile checkpoint 必须晚于准入。")
        if self.boundary_recorded_at <= self.admitted_at:
            raise ValueError("Pursuit recovery reconcile 裁判必须晚于准入。")
        if (
            self.checkpoint_created_at > self.reconciled_at
            or self.boundary_recorded_at > self.reconciled_at
        ):
            raise ValueError("Pursuit recovery reconcile 终态证据不得来自未来。")
        if self.fence_epoch <= self.admitted_lease_epoch:
            raise ValueError("Pursuit recovery reconcile fence epoch 必须推进。")
        if self.receipt_id != pursuit_reconciliation_receipt_id(self):
            raise ValueError("Pursuit recovery reconcile receipt_id 与事实不一致。")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PursuitRecoveryReconcileResult(_StrictModel):
    """Public result; only a receipt proves that mutation happened."""

    schema_version: Literal[1] = 1
    status: Literal["reconciled", "unchanged", "blocked", "error"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=500)
    attempt_id: str = Field(default="", max_length=73)
    receipt: PursuitRecoveryReconciliationReceipt | None = None
    warning: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _receipt_matches_status(self) -> PursuitRecoveryReconcileResult:
        if self.status == "reconciled":
            if self.receipt is None:
                raise ValueError("reconciled 结果必须包含持久回执。")
            if self.attempt_id != self.receipt.attempt_id:
                raise ValueError("reconcile 结果与回执 attempt_id 不一致。")
        elif self.receipt is not None:
            raise ValueError("未完成 reconcile 不得携带成功回执。")
        if self.attempt_id and not _ATTEMPT_ID_RE.fullmatch(self.attempt_id):
            raise ValueError("reconcile result attempt_id 格式无效。")
        return self


class PursuitRecoveryReconcileAuthority(Protocol):
    async def get_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
    ) -> HarnessHeartbeat | None: ...

    async def get_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
    ) -> HarnessRunLease | None: ...

    async def acquire_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> HarnessRunLease | None: ...

    async def record_run_fence_decision(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        operation_id: str,
        owner_id: str,
        epoch: int,
        checked_at: str,
    ) -> HarnessRunFenceReceipt: ...

    async def release_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        epoch: int,
        now: str,
    ) -> HarnessRunLease | None: ...


def pursuit_reconciliation_receipt_id(
    receipt: PursuitRecoveryReconciliationReceipt,
) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("receipt_id", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"precon-{hashlib.sha256(encoded).hexdigest()}"


def new_pursuit_reconciliation_receipt(
    **facts: object,
) -> PursuitRecoveryReconciliationReceipt:
    payload = {"schema_version": 1, "receipt_id": "precon-" + "0" * 64, **facts}
    provisional = PursuitRecoveryReconciliationReceipt.model_construct(**payload)
    payload["receipt_id"] = pursuit_reconciliation_receipt_id(provisional)
    return PursuitRecoveryReconciliationReceipt.model_validate(payload)


async def reconcile_pursuit_recovery_attempt(
    *,
    store: PursuitStore,
    authority: PursuitRecoveryReconcileAuthority | None,
    workspace_root: str | Path | None,
    attempt_id: str,
    now: str,
    grace_seconds: float = DEFAULT_RECONCILE_GRACE_SECONDS,
    lease_seconds: int = DEFAULT_RECONCILE_LEASE_SECONDS,
) -> PursuitRecoveryReconcileResult:
    """Fence any former owner, then atomically reconcile authenticated facts."""
    normalized_attempt_id = str(attempt_id or "").strip()
    if not _ATTEMPT_ID_RE.fullmatch(normalized_attempt_id):
        return _result(
            "error",
            "invalid_attempt_id",
            "恢复请求 ID 格式无效。",
        )
    try:
        assessed_at = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return _result(
            "error",
            "invalid_assessed_at",
            "对账时间必须是带时区的 ISO 8601。",
            normalized_attempt_id,
        )
    if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
        return _result(
            "error",
            "invalid_assessed_at",
            "对账时间必须包含时区偏移。",
            normalized_attempt_id,
        )
    if (
        not math.isfinite(grace_seconds)
        or not 1 <= grace_seconds <= 86_400
        or isinstance(lease_seconds, bool)
        or not 3 <= lease_seconds <= 3_600
    ):
        return _result(
            "error",
            "invalid_policy",
            "对账宽限期或租约策略无效。",
            normalized_attempt_id,
        )

    try:
        existing_receipt = store.get_recovery_reconciliation(
            normalized_attempt_id
        )
        if existing_receipt is not None:
            return _reconciled_result(existing_receipt, code="already_reconciled")
        attempt = store.get_recovery_attempt(normalized_attempt_id)
    except Exception:
        logger.exception(
            "Failed to verify Pursuit recovery reconciliation ledger [%s]",
            normalized_attempt_id,
        )
        return _result(
            "error",
            "attempt_ledger_unavailable",
            "恢复请求账本无法验证，未执行对账。",
            normalized_attempt_id,
        )
    if attempt is None:
        return _result(
            "unchanged",
            "attempt_not_found",
            "没有找到该恢复请求。",
            normalized_attempt_id,
        )
    if attempt.state in {
        PursuitRecoveryAttemptState.RESOLVED,
        PursuitRecoveryAttemptState.FAILED,
    }:
        return _result(
            "unchanged",
            "already_terminal",
            "该恢复请求已经处于终态，不需要对账。",
            normalized_attempt_id,
        )
    if attempt.state is PursuitRecoveryAttemptState.REQUESTED:
        return _result(
            "blocked",
            "request_not_admitted",
            "该恢复请求尚未取得执行准入，不能按已执行结果收口。",
            normalized_attempt_id,
        )
    if attempt.lease_epoch <= 0:
        return _result(
            "blocked",
            "embedded_authority_unsupported",
            "该请求没有可推进的持久 RunLease epoch，需要人工审查。",
            normalized_attempt_id,
        )
    admitted_age = assessed_at.timestamp() - attempt.admitted_at
    if admitted_age < 0:
        return _result(
            "blocked",
            "clock_regression",
            "当前时间早于恢复准入时间，已拒绝对账。",
            normalized_attempt_id,
        )
    if admitted_age < grace_seconds:
        return _result(
            "blocked",
            "grace_period_active",
            f"恢复请求仍在 {round(grace_seconds)} 秒安全宽限期内。",
            normalized_attempt_id,
        )
    if authority is None or workspace_root is None:
        return _result(
            "blocked",
            "authority_unavailable",
            "Harness 心跳与 RunLease 权威未接入，不能安全对账。",
            normalized_attempt_id,
        )

    try:
        heartbeat = await authority.get_heartbeat(
            workspace_root=workspace_root,
            subject_kind=HarnessRunKind.PURSUIT,
            subject_id=attempt.run_id,
        )
        lease = await authority.get_run_lease(
            workspace_root=workspace_root,
            run_kind=HarnessRunKind.PURSUIT,
            run_id=attempt.run_id,
        )
    except Exception:
        logger.exception(
            "Failed to read Pursuit recovery fencing authority [%s]",
            normalized_attempt_id,
        )
        return _result(
            "error",
            "authority_read_failed",
            "Harness 心跳或 RunLease 无法验证，未执行对账。",
            normalized_attempt_id,
        )
    if heartbeat is not None:
        try:
            heartbeat_snapshot = assess_heartbeat(heartbeat, now=now)
        except ValueError:
            return _result(
                "blocked",
                "heartbeat_invalid",
                "Harness 心跳结构或时间无效，未执行对账。",
                normalized_attempt_id,
            )
        if heartbeat_snapshot.health in {
            HarnessHeartbeatHealth.STARTING,
            HarnessHeartbeatHealth.HEALTHY,
            HarnessHeartbeatHealth.DRAINING,
        }:
            return _result(
                "blocked",
                "live_heartbeat",
                "原执行者仍在发送健康心跳，不能并发收口。",
                normalized_attempt_id,
            )
        if heartbeat_snapshot.health is HarnessHeartbeatHealth.CLOCK_REGRESSION:
            return _result(
                "blocked",
                "clock_regression",
                "Harness 心跳检测到时钟回退，不能安全对账。",
                normalized_attempt_id,
            )
    if lease is None:
        return _result(
            "blocked",
            "lease_missing",
            "原恢复请求的 RunLease 历史缺失，不能证明 fencing 连续性。",
            normalized_attempt_id,
        )
    if (
        not isinstance(lease.state, HarnessRunLeaseState)
        or isinstance(lease.epoch, bool)
        or not isinstance(lease.epoch, int)
        or lease.epoch <= 0
    ):
        return _result(
            "blocked",
            "lease_invalid",
            "RunLease 状态或 epoch 无效，不能安全对账。",
            normalized_attempt_id,
        )
    try:
        lease_expires_at = datetime.fromisoformat(lease.expires_at)
    except (TypeError, ValueError):
        return _result(
            "blocked",
            "lease_invalid",
            "RunLease 到期时间无效，不能安全对账。",
            normalized_attempt_id,
        )
    if (
        lease_expires_at.tzinfo is None
        or lease_expires_at.utcoffset() is None
    ):
        return _result(
            "blocked",
            "lease_invalid",
            "RunLease 到期时间缺少时区，不能安全对账。",
            normalized_attempt_id,
        )
    if lease.epoch < attempt.lease_epoch:
        return _result(
            "blocked",
            "lease_epoch_regression",
            "当前 RunLease epoch 早于恢复准入记录，权威状态不一致。",
            normalized_attempt_id,
        )
    if heartbeat is not None and heartbeat.epoch > lease.epoch:
        return _result(
            "blocked",
            "authority_inconsistent",
            "Heartbeat epoch 超前于 RunLease，权威状态不一致。",
            normalized_attempt_id,
        )
    if (
        lease.state is HarnessRunLeaseState.ACTIVE
        and lease_expires_at > assessed_at
    ):
        return _result(
            "blocked",
            "live_lease",
            "原执行者的 RunLease 仍有效，不能并发收口。",
            normalized_attempt_id,
        )

    owner_id = f"pursuit-reconcile-{uuid.uuid4().hex}"
    claimed: HarnessRunLease | None = None
    result: PursuitRecoveryReconcileResult
    release_warning = ""
    try:
        claimed = await authority.acquire_run_lease(
            workspace_root=workspace_root,
            run_kind=HarnessRunKind.PURSUIT,
            run_id=attempt.run_id,
            owner_id=owner_id,
            now=now,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return _result(
                "blocked",
                "lease_claim_conflict",
                "另一个执行者已取得 RunLease，对账未执行。",
                normalized_attempt_id,
            )
        if (
            claimed.owner_id != owner_id
            or claimed.state is not HarnessRunLeaseState.ACTIVE
            or claimed.epoch <= attempt.lease_epoch
        ):
            result = _result(
                "blocked",
                "fence_not_advanced",
                "新 RunLease 未形成更高 epoch 的 fencing，未执行对账。",
                normalized_attempt_id,
            )
        else:
            operation_id = "precon-" + hashlib.sha256(
                f"{normalized_attempt_id}:{claimed.epoch}".encode()
            ).hexdigest()[:32]
            fence = await authority.record_run_fence_decision(
                workspace_root=workspace_root,
                run_kind=HarnessRunKind.PURSUIT,
                run_id=attempt.run_id,
                operation_id=operation_id,
                owner_id=owner_id,
                epoch=claimed.epoch,
                checked_at=now,
            )
            if (
                not fence.accepted
                or fence.active_epoch != claimed.epoch
                or fence.presented_owner_id != owner_id
            ):
                result = _result(
                    "blocked",
                    "fence_rejected",
                    "Harness fencing 裁决未接受本次对账所有权。",
                    normalized_attempt_id,
                )
            else:
                try:
                    receipt = store.reconcile_admitted_recovery_attempt(
                        normalized_attempt_id,
                        reconciled_at=assessed_at.timestamp(),
                        minimum_admitted_age_seconds=grace_seconds,
                        fence_epoch=claimed.epoch,
                        fence_operation_id=operation_id,
                    )
                except PursuitRecoveryReconcileError as exc:
                    result = _result(
                        "blocked",
                        exc.code,
                        exc.public_message,
                        normalized_attempt_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to commit Pursuit recovery reconciliation [%s]",
                        normalized_attempt_id,
                    )
                    result = _result(
                        "error",
                        "reconcile_store_failed",
                        "Pursuit 对账事务失败，恢复请求保持原状态。",
                        normalized_attempt_id,
                    )
                else:
                    result = _reconciled_result(receipt)
    except Exception:
        logger.exception(
            "Failed to mutate Pursuit recovery fencing authority [%s]",
            normalized_attempt_id,
        )
        result = _result(
            "error",
            "authority_mutation_failed",
            "Harness fencing 操作失败，恢复请求保持原状态。",
            normalized_attempt_id,
        )
    finally:
        if claimed is not None:
            try:
                released = await authority.release_run_lease(
                    workspace_root=workspace_root,
                    run_kind=HarnessRunKind.PURSUIT,
                    run_id=attempt.run_id,
                    owner_id=owner_id,
                    epoch=claimed.epoch,
                    now=now,
                )
                if released is None:
                    release_warning = (
                        "对账租约未能立即释放；它会在短租约到期后失效。"
                    )
            except Exception:
                logger.exception(
                    "Failed to release Pursuit recovery reconcile lease [%s]",
                    normalized_attempt_id,
                )
                release_warning = (
                    "对账租约释放失败；它会在短租约到期后失效。"
                )
    if release_warning:
        return result.model_copy(update={"warning": release_warning})
    return result


def format_pursuit_reconcile_result(
    result: PursuitRecoveryReconcileResult,
) -> str:
    marker = {
        "reconciled": "✅",
        "unchanged": "ℹ️",
        "blocked": "⛔",
        "error": "⚠️",
    }[result.status]
    lines = [
        f"{marker} {result.message}",
        "",
        f"- 状态码：`{result.code}`",
    ]
    if result.attempt_id:
        lines.append(f"- 恢复请求：`{result.attempt_id}`")
    if result.receipt is not None:
        receipt = result.receipt
        lines.extend((
            f"- 对账回执：`{receipt.receipt_id}`",
            f"- Fencing：lease epoch {receipt.admitted_lease_epoch}"
            f" → {receipt.fence_epoch}",
            f"- 终态裁判：`{receipt.result_code}`"
            f" · `{receipt.boundary_decision_id[:12]}`",
            f"- 后置 checkpoint：`{receipt.checkpoint_id}`",
        ))
    if result.warning:
        lines.append(f"- 提醒：{result.warning}")
    return "\n".join(lines)


def _reconciled_result(
    receipt: PursuitRecoveryReconciliationReceipt,
    *,
    code: str = "reconciled",
) -> PursuitRecoveryReconcileResult:
    message = (
        "该恢复请求已有经过复验的对账回执。"
        if code == "already_reconciled"
        else "已使用更高 RunLease epoch 栅栏旧执行者，并按机械证据收口。"
    )
    return PursuitRecoveryReconcileResult(
        status="reconciled",
        code=code,
        message=message,
        attempt_id=receipt.attempt_id,
        receipt=receipt,
    )


def _result(
    status: Literal["reconciled", "unchanged", "blocked", "error"],
    code: str,
    message: str,
    attempt_id: str = "",
) -> PursuitRecoveryReconcileResult:
    return PursuitRecoveryReconcileResult(
        status=status,
        code=code,
        message=message,
        attempt_id=attempt_id,
    )


__all__ = [
    "DEFAULT_RECONCILE_GRACE_SECONDS",
    "DEFAULT_RECONCILE_LEASE_SECONDS",
    "PursuitRecoveryReconcileAuthority",
    "PursuitRecoveryReconcileError",
    "PursuitRecoveryReconcileResult",
    "PursuitRecoveryReconciliationReceipt",
    "format_pursuit_reconcile_result",
    "new_pursuit_reconciliation_receipt",
    "pursuit_reconciliation_receipt_id",
    "reconcile_pursuit_recovery_attempt",
]
