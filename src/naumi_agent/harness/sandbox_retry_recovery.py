"""Bounded, read-only startup projection for durable Sandbox retry dispatches."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.store import HarnessSandboxRetryCatalogPage

SANDBOX_RETRY_RECOVERY_LIMIT = 20
SANDBOX_RETRY_RECOVERY_SCHEMA_VERSION = 1

SandboxRetryRecoveryStatus = Literal[
    "pending",
    "live",
    "recovery_required",
    "reconcile_required",
    "clock_regression",
]

_ACTIONABLE_STATUSES = frozenset({"pending", "recovery_required"})
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessSandboxRetryRecoveryCounts(_StrictModel):
    """Mechanical recovery categories for one bounded startup page."""

    pending: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    live: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    recovery_required: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    reconcile_required: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    clock_regression: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    actionable: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)

    @property
    def classified_total(self) -> int:
        return (
            self.pending
            + self.live
            + self.recovery_required
            + self.reconcile_required
            + self.clock_regression
        )


class HarnessSandboxRetryRecoveryItem(_StrictModel):
    """Safe recovery facts for one dispatch without execution authority."""

    dispatch_id: str = Field(pattern=r"^hsard_[0-9a-f]{24}$")
    retry_action_id: str = Field(pattern=r"^hsar_[0-9a-f]{24}$")
    retry_receipt_id: str = Field(pattern=r"^hsarr_[0-9a-f]{24}$")
    retry_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(min_length=1, max_length=128)
    suite_id: str = Field(min_length=1, max_length=128)
    requested_samples: int = Field(ge=1, le=1_000)
    persisted_samples: int = Field(ge=0, le=1_000)
    recovery_status: SandboxRetryRecoveryStatus
    dispatch_state: Literal["pending", "claimed"]
    dispatch_epoch: int = Field(ge=0)
    ticket_id: str = Field(max_length=128)
    ticket_epoch: int = Field(ge=0)
    ticket_state: str = Field(max_length=32)
    ticket_lease_expires_at: str = Field(max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    can_resume: bool
    resume_command: str = Field(max_length=512)

    @model_validator(mode="after")
    def validate_facts(self) -> Self:
        if self.persisted_samples > self.requested_samples:
            raise ValueError("Sandbox retry recovery H5a 不能超过请求样本数。")
        expected_actionable = self.recovery_status in _ACTIONABLE_STATUSES
        expected_command = (
            _resume_command(
                retry_action_id=self.retry_action_id,
                dispatch_id=self.dispatch_id,
                retry_receipt_id=self.retry_receipt_id,
                retry_receipt_sha256=self.retry_receipt_sha256,
            )
            if expected_actionable
            else ""
        )
        if self.can_resume is not expected_actionable:
            raise ValueError("Sandbox retry recovery can_resume 与分类不一致。")
        if self.resume_command != expected_command:
            raise ValueError("Sandbox retry recovery resume 命令与持久事实不一致。")
        if self.recovery_status == "pending":
            if (
                self.dispatch_state != "pending"
                or self.dispatch_epoch != 0
                or self.ticket_id
                or self.ticket_epoch != 0
                or self.ticket_state
                or self.ticket_lease_expires_at
            ):
                raise ValueError("pending recovery item 不能伪造 ticket 或 claim。")
        else:
            if self.dispatch_state != "claimed":
                raise ValueError("非 pending recovery item 必须来自 claimed dispatch。")
            if not re.fullmatch(r"hsadm_[0-9a-f]{24}", self.ticket_id):
                raise ValueError("claimed recovery item 缺少有效 ticket。")
            if self.ticket_epoch < 1 or not self.ticket_state:
                raise ValueError("claimed recovery item 缺少 ticket fence。")
        _require_aware_timestamp(self.updated_at, field="updated_at")
        if self.ticket_lease_expires_at:
            _require_aware_timestamp(
                self.ticket_lease_expires_at,
                field="ticket_lease_expires_at",
            )
        return self


class HarnessSandboxRetryRecoverySnapshot(_StrictModel):
    """Tamper-evident, bounded startup discovery snapshot."""

    schema_version: Literal[1] = SANDBOX_RETRY_RECOVERY_SCHEMA_VERSION
    snapshot_id: str = Field(pattern=r"^hsrrs_[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "unavailable"]
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessed_at: str = Field(min_length=1, max_length=64)
    limit: int = Field(ge=1, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    total: int = Field(ge=0, le=SANDBOX_RETRY_RECOVERY_LIMIT)
    truncated: bool
    counts: HarnessSandboxRetryRecoveryCounts
    items: tuple[HarnessSandboxRetryRecoveryItem, ...] = Field(
        max_length=SANDBOX_RETRY_RECOVERY_LIMIT
    )
    error_code: str = Field(max_length=128)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_aware_timestamp(self.assessed_at, field="assessed_at")
        if self.total != len(self.items):
            raise ValueError("Sandbox retry recovery total 与 items 不一致。")
        if self.total > self.limit:
            raise ValueError("Sandbox retry recovery items 超过扫描上限。")
        if self.counts.classified_total != self.total:
            raise ValueError("Sandbox retry recovery counts 与 total 不一致。")
        if self.counts.actionable != (
            self.counts.pending + self.counts.recovery_required
        ):
            raise ValueError("Sandbox retry recovery actionable 计数不一致。")
        if self.truncated and self.total != self.limit:
            raise ValueError("Sandbox retry recovery 截断页必须填满扫描上限。")
        if self.status == "ready":
            if self.error_code:
                raise ValueError("ready recovery snapshot 不能包含 error_code。")
        elif (
            self.total
            or self.truncated
            or self.counts.classified_total
            or self.counts.actionable
            or self.error_code == ""
        ):
            raise ValueError("unavailable recovery snapshot 必须为空且包含错误码。")
        expected_sha256 = _snapshot_sha256(
            status=self.status,
            workspace_sha256=self.workspace_sha256,
            assessed_at=self.assessed_at,
            limit=self.limit,
            total=self.total,
            truncated=self.truncated,
            counts=self.counts,
            items=self.items,
            error_code=self.error_code,
        )
        if self.snapshot_sha256 != expected_sha256:
            raise ValueError("Sandbox retry recovery snapshot SHA-256 不一致。")
        if self.snapshot_id != f"hsrrs_{expected_sha256[:24]}":
            raise ValueError("Sandbox retry recovery snapshot ID 不一致。")
        return self


def build_sandbox_retry_recovery_snapshot(
    page: HarnessSandboxRetryCatalogPage,
) -> HarnessSandboxRetryRecoverySnapshot:
    """Project one validated open catalog page without claiming any dispatch."""
    if page.state_filter != "open":
        raise ValueError("Sandbox retry startup recovery 只能消费 open catalog。")
    if not 1 <= page.limit <= SANDBOX_RETRY_RECOVERY_LIMIT:
        raise ValueError("Sandbox retry startup recovery 扫描上限必须为 1..20。")
    if len(page.items) > page.limit:
        raise ValueError("Sandbox retry catalog 返回项目超过请求上限。")

    items = tuple(
        HarnessSandboxRetryRecoveryItem(
            dispatch_id=item.dispatch.dispatch_id,
            retry_action_id=item.dispatch.retry_action_id,
            retry_receipt_id=item.dispatch.retry_receipt_id,
            retry_receipt_sha256=item.dispatch.retry_receipt_sha256,
            batch_id=item.batch_id,
            suite_id=item.suite_id,
            requested_samples=item.requested_samples,
            persisted_samples=item.persisted_samples,
            recovery_status=item.recovery_status,
            dispatch_state=item.dispatch.state,
            dispatch_epoch=item.dispatch.epoch,
            ticket_id=item.dispatch.ticket_id,
            ticket_epoch=item.dispatch.ticket_epoch,
            ticket_state=item.ticket_state,
            ticket_lease_expires_at=item.ticket_lease_expires_at,
            updated_at=item.dispatch.updated_at,
            can_resume=item.recovery_status in _ACTIONABLE_STATUSES,
            resume_command=(
                _resume_command(
                    retry_action_id=item.dispatch.retry_action_id,
                    dispatch_id=item.dispatch.dispatch_id,
                    retry_receipt_id=item.dispatch.retry_receipt_id,
                    retry_receipt_sha256=item.dispatch.retry_receipt_sha256,
                )
                if item.recovery_status in _ACTIONABLE_STATUSES
                else ""
            ),
        )
        for item in page.items
    )
    counts = _counts(items)
    workspace_sha256 = _workspace_sha256(page.workspace_root)
    digest = _snapshot_sha256(
        status="ready",
        workspace_sha256=workspace_sha256,
        assessed_at=page.assessed_at,
        limit=page.limit,
        total=len(items),
        truncated=page.has_more,
        counts=counts,
        items=items,
        error_code="",
    )
    return HarnessSandboxRetryRecoverySnapshot(
        snapshot_id=f"hsrrs_{digest[:24]}",
        snapshot_sha256=digest,
        status="ready",
        workspace_sha256=workspace_sha256,
        assessed_at=page.assessed_at,
        limit=page.limit,
        total=len(items),
        truncated=page.has_more,
        counts=counts,
        items=items,
        error_code="",
    )


def unavailable_sandbox_retry_recovery_snapshot(
    workspace_root: str | Path,
    *,
    error_code: str,
    assessed_at: str | None = None,
    limit: int = SANDBOX_RETRY_RECOVERY_LIMIT,
) -> HarnessSandboxRetryRecoverySnapshot:
    """Build a safe degraded snapshot without leaking exception text."""
    if not 1 <= limit <= SANDBOX_RETRY_RECOVERY_LIMIT:
        raise ValueError("Sandbox retry startup recovery 扫描上限必须为 1..20。")
    normalized_error = (
        error_code.strip().lower() if isinstance(error_code, str) else ""
    )
    if not _ERROR_CODE_PATTERN.fullmatch(normalized_error):
        normalized_error = "sandbox_retry_recovery_unavailable"
    normalized_time = _normalize_timestamp(
        assessed_at or datetime.now(UTC).isoformat(),
        field="assessed_at",
    )
    counts = HarnessSandboxRetryRecoveryCounts(
        pending=0,
        live=0,
        recovery_required=0,
        reconcile_required=0,
        clock_regression=0,
        actionable=0,
    )
    workspace_sha256 = _workspace_sha256(workspace_root)
    digest = _snapshot_sha256(
        status="unavailable",
        workspace_sha256=workspace_sha256,
        assessed_at=normalized_time,
        limit=limit,
        total=0,
        truncated=False,
        counts=counts,
        items=(),
        error_code=normalized_error,
    )
    return HarnessSandboxRetryRecoverySnapshot(
        snapshot_id=f"hsrrs_{digest[:24]}",
        snapshot_sha256=digest,
        status="unavailable",
        workspace_sha256=workspace_sha256,
        assessed_at=normalized_time,
        limit=limit,
        total=0,
        truncated=False,
        counts=counts,
        items=(),
        error_code=normalized_error,
    )


def render_sandbox_retry_recovery_snapshot(
    snapshot: HarnessSandboxRetryRecoverySnapshot,
) -> str:
    """Render a manual recovery queue shared by New UI and Textual TUI."""
    if snapshot.status == "unavailable":
        return "\n".join(
            (
                "### Sandbox retry 恢复发现降级",
                "",
                "启动扫描暂不可用；系统没有自动 claim 或重放任何任务。",
                "请运行 `/harness eval sandbox retries --state open` 重新检查。",
                f"错误码：`{snapshot.error_code}`",
            )
        )

    counts = snapshot.counts
    lines = [
        "### Sandbox retry 启动恢复队列",
        "",
        "本队列只展示持久事实，不会在启动时自动 claim、续租或重放任务。",
        (
            f"发现 {snapshot.total} 项：可恢复 {counts.actionable} · "
            f"存活 {counts.live} · 需对账 {counts.reconcile_required} · "
            f"时钟异常 {counts.clock_regression}"
        ),
    ]
    status_labels = {
        "pending": "等待首次 claim",
        "live": "当前 ticket 仍存活",
        "recovery_required": "租约已过期，可显式恢复",
        "reconcile_required": "ticket 已终态，需先对账",
        "clock_regression": "时钟倒退，禁止恢复",
    }
    for index, item in enumerate(snapshot.items, start=1):
        lines.extend(
            (
                "",
                (
                    f"{index}. **{status_labels[item.recovery_status]}** · "
                    f"`{item.batch_id}/{item.suite_id}` · "
                    f"H5a {item.persisted_samples}/{item.requested_samples}"
                ),
                (
                    f"   Dispatch `{item.dispatch_id}` · "
                    f"epoch `{item.dispatch_epoch}` · 更新 `{item.updated_at}`"
                ),
            )
        )
        if item.can_resume:
            lines.extend(
                (
                    "",
                    "   ```text",
                    f"   {item.resume_command}",
                    "   ```",
                )
            )
        elif item.recovery_status == "live":
            lines.append("   当前 lease 尚有效，不提供并发恢复命令。")
        else:
            lines.append(
                "   当前状态不能直接 resume；请运行 "
                "`/harness eval sandbox retries --state open` 查看完整 fence。"
            )
    if snapshot.truncated:
        lines.extend(
            (
                "",
                "队列已达到启动扫描上限；其余项目未在启动时加载。请使用 "
                "`/harness eval sandbox retries --state open` 有界翻页。",
            )
        )
    return "\n".join(lines)


def _counts(
    items: tuple[HarnessSandboxRetryRecoveryItem, ...],
) -> HarnessSandboxRetryRecoveryCounts:
    by_status = {
        name: sum(item.recovery_status == name for item in items)
        for name in (
            "pending",
            "live",
            "recovery_required",
            "reconcile_required",
            "clock_regression",
        )
    }
    return HarnessSandboxRetryRecoveryCounts(
        **by_status,
        actionable=by_status["pending"] + by_status["recovery_required"],
    )


def _resume_command(
    *,
    retry_action_id: str,
    dispatch_id: str,
    retry_receipt_id: str,
    retry_receipt_sha256: str,
) -> str:
    return (
        f"/harness eval sandbox resume {retry_action_id} "
        f"--dispatch {dispatch_id} "
        f"--receipt {retry_receipt_id} "
        f"--sha256 {retry_receipt_sha256}"
    )


def _workspace_sha256(workspace_root: str | Path) -> str:
    canonical = str(Path(workspace_root).expanduser().resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_sha256(
    *,
    status: Literal["ready", "unavailable"],
    workspace_sha256: str,
    assessed_at: str,
    limit: int,
    total: int,
    truncated: bool,
    counts: HarnessSandboxRetryRecoveryCounts,
    items: tuple[HarnessSandboxRetryRecoveryItem, ...],
    error_code: str,
) -> str:
    payload = {
        "schema_version": SANDBOX_RETRY_RECOVERY_SCHEMA_VERSION,
        "status": status,
        "workspace_sha256": workspace_sha256,
        "assessed_at": assessed_at,
        "limit": limit,
        "total": total,
        "truncated": truncated,
        "counts": counts.model_dump(mode="json"),
        "items": [item.model_dump(mode="json") for item in items],
        "error_code": error_code,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_aware_timestamp(value: str, *, field: str) -> None:
    _normalize_timestamp(value, field=field)


def _normalize_timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Sandbox retry recovery {field} 必须是 ISO 8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Sandbox retry recovery {field} 必须包含时区偏移。")
    return parsed.astimezone(UTC).isoformat()


__all__ = [
    "SANDBOX_RETRY_RECOVERY_LIMIT",
    "HarnessSandboxRetryRecoveryCounts",
    "HarnessSandboxRetryRecoveryItem",
    "HarnessSandboxRetryRecoverySnapshot",
    "build_sandbox_retry_recovery_snapshot",
    "render_sandbox_retry_recovery_snapshot",
    "unavailable_sandbox_retry_recovery_snapshot",
]
