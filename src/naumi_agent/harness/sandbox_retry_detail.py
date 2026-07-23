"""Tamper-evident public detail projection for one Sandbox retry dispatch."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.store import HarnessSandboxRetryDetailRecord

SANDBOX_RETRY_DETAIL_SCHEMA_VERSION = 2

RecoveryStatus = Literal[
    "pending",
    "live",
    "recovery_required",
    "reconcile_required",
    "clock_regression",
    "terminal",
]
DetailStatus = Literal["ok", "not_found", "unavailable"]
ProtectionKind = Literal[
    "dispatch",
    "retry_receipt",
    "cancel_receipt",
    "request_manifest",
    "source_ticket",
    "ticket",
    "h5a_sample",
]

_ACTIONABLE = frozenset({"pending", "recovery_required"})
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HarnessSandboxRetryDetailCheck(_StrictModel):
    order: int = Field(ge=1, le=80)
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    argv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(ge=1, le=3_600)


class HarnessSandboxRetryDetailTicket(_StrictModel):
    ticket_id: str = Field(pattern=r"^hsadm_[0-9a-f]{24}$")
    epoch: int = Field(ge=1)
    state: Literal[
        "queued",
        "active",
        "completed",
        "failed",
        "cancelled",
        "expired",
    ]
    lease_expires_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    terminal_code: str = Field(max_length=128)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_ticket(self) -> Self:
        _require_timestamp(self.lease_expires_at, field="ticket.lease_expires_at")
        _require_timestamp(self.updated_at, field="ticket.updated_at")
        terminal = self.state in {"completed", "failed", "cancelled", "expired"}
        if not terminal and self.terminal_code:
            raise ValueError("非终态 Sandbox ticket 不能包含 terminal_code。")
        return self


class HarnessSandboxRetryDetailSample(_StrictModel):
    sample_index: int = Field(ge=0, le=99)
    status: Literal["passed", "failed", "evaluation_error"]
    identity_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        _require_timestamp(self.created_at, field="sample.created_at")
        return self


class HarnessSandboxRetryProtectionRef(_StrictModel):
    kind: ProtectionKind
    ref_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal[
        "dispatch_authority",
        "retry_authority",
        "cancel_chain",
        "request_replay",
        "source_ticket_chain",
        "current_ticket_fence",
        "continuous_h5a_prefix",
    ]


class HarnessSandboxRetryDetail(_StrictModel):
    retry_action_id: str = Field(pattern=r"^hsar_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^hsard_[0-9a-f]{24}$")
    recovery_status: RecoveryStatus
    dispatch_state: Literal["pending", "claimed", "completed", "failed", "cancelled"]
    dispatch_epoch: int = Field(ge=0)
    dispatch_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    terminal_code: str = Field(max_length=128)
    retry_receipt_id: str = Field(pattern=r"^hsarr_[0-9a-f]{24}$")
    retry_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cancel_receipt_id: str = Field(pattern=r"^hsacr_[0-9a-f]{24}$")
    cancel_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ticket_id: str = Field(pattern=r"^hsadm_[0-9a-f]{24}$")
    source_ticket_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(pattern=r"^hseval_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    suite_id: str = Field(pattern=r"^harness_sandbox_[0-9a-f]{24}$")
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=0, le=100)
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_total_duration_seconds: int = Field(ge=60, le=3_600)
    checks: tuple[HarnessSandboxRetryDetailCheck, ...] = Field(
        min_length=1,
        max_length=80,
    )
    ticket: HarnessSandboxRetryDetailTicket | None
    samples: tuple[HarnessSandboxRetryDetailSample, ...] = Field(max_length=100)
    protection_refs: tuple[HarnessSandboxRetryProtectionRef, ...] = Field(
        min_length=5,
        max_length=106,
    )
    can_resume: bool
    resume_command: str = Field(max_length=512)

    @model_validator(mode="after")
    def validate_detail(self) -> Self:
        _require_timestamp(self.created_at, field="dispatch.created_at")
        _require_timestamp(self.updated_at, field="dispatch.updated_at")
        if datetime.fromisoformat(self.updated_at) < datetime.fromisoformat(
            self.created_at
        ):
            raise ValueError("Sandbox retry detail updated_at 早于 created_at。")
        if self.persisted_samples != len(self.samples):
            raise ValueError("Sandbox retry detail persisted_samples 与 samples 不一致。")
        if tuple(sample.sample_index for sample in self.samples) != tuple(
            range(len(self.samples))
        ):
            raise ValueError("Sandbox retry detail H5a 必须是连续前缀。")
        if self.persisted_samples > self.requested_samples:
            raise ValueError("Sandbox retry detail H5a 超过请求样本数。")
        if tuple(check.order for check in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Sandbox retry detail checks 顺序不连续。")
        if len({check.check_id for check in self.checks}) != len(self.checks):
            raise ValueError("Sandbox retry detail checks 不得重复。")
        if self.dispatch_state == "pending":
            if self.dispatch_epoch != 0 or self.ticket is not None or self.terminal_code:
                raise ValueError("pending dispatch 不能包含 claim、ticket 或终态字段。")
        else:
            if self.dispatch_epoch < 1 or self.ticket is None:
                raise ValueError("非 pending dispatch 缺少当前 ticket fence。")
        if self.dispatch_state in {"completed", "failed", "cancelled"}:
            if (
                self.recovery_status != "terminal"
                or self.ticket is None
                or self.ticket.state != self.dispatch_state
                or self.ticket.terminal_code != self.terminal_code
            ):
                raise ValueError("terminal dispatch 与 ticket 终态不一致。")
        elif self.recovery_status == "terminal":
            raise ValueError("非终态 dispatch 不能声明 terminal recovery。")
        actionable = self.recovery_status in _ACTIONABLE
        if self.can_resume is not actionable:
            raise ValueError("Sandbox retry detail can_resume 与恢复分类不一致。")
        expected_command = (
            _resume_command(
                retry_action_id=self.retry_action_id,
                dispatch_id=self.dispatch_id,
                retry_receipt_id=self.retry_receipt_id,
                retry_receipt_sha256=self.retry_receipt_sha256,
            )
            if actionable
            else ""
        )
        if self.resume_command != expected_command:
            raise ValueError("Sandbox retry detail resume 命令 identity fence 无效。")
        expected_refs = _protection_refs(self)
        if self.protection_refs != expected_refs:
            raise ValueError("Sandbox retry detail protection refs 与权威事实不一致。")
        return self


class HarnessSandboxRetryDetailSnapshot(_StrictModel):
    schema_version: Literal[2] = SANDBOX_RETRY_DETAIL_SCHEMA_VERSION
    snapshot_id: str = Field(pattern=r"^hsrrd_[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DetailStatus
    retry_action_id: str = Field(pattern=r"^hsar_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^hsard_[0-9a-f]{24}$")
    assessed_at: str = Field(min_length=1, max_length=64)
    detail: HarnessSandboxRetryDetail | None
    error_code: str = Field(max_length=128)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_timestamp(self.assessed_at, field="assessed_at")
        if self.status == "ok":
            if self.detail is None or self.error_code:
                raise ValueError("ok Sandbox retry detail 缺少 detail 或包含错误码。")
            if (
                self.detail.retry_action_id != self.retry_action_id
                or self.detail.dispatch_id != self.dispatch_id
            ):
                raise ValueError("Sandbox retry detail 与 lookup identity 不一致。")
        elif self.detail is not None or not _ERROR_CODE_RE.fullmatch(self.error_code):
            raise ValueError("非 ok Sandbox retry detail 必须为空并包含稳定错误码。")
        expected = _snapshot_sha256(
            status=self.status,
            retry_action_id=self.retry_action_id,
            dispatch_id=self.dispatch_id,
            assessed_at=self.assessed_at,
            detail=self.detail,
            error_code=self.error_code,
        )
        if self.snapshot_sha256 != expected:
            raise ValueError("Sandbox retry detail snapshot SHA-256 不一致。")
        if self.snapshot_id != f"hsrrd_{expected[:24]}":
            raise ValueError("Sandbox retry detail snapshot ID 不一致。")
        return self


def build_sandbox_retry_detail_snapshot(
    record: HarnessSandboxRetryDetailRecord,
) -> HarnessSandboxRetryDetailSnapshot:
    item = record.item
    dispatch = item.dispatch
    retry = record.retry_receipt
    cancel = record.cancel_receipt
    request = record.request_manifest.request
    ticket = record.ticket
    samples = tuple(
        HarnessSandboxRetryDetailSample(
            sample_index=sample.sample_index,
            status=str(sample.result.status),
            identity_sha256=sample.identity_sha256,
            result_sha256=sample.result_sha256,
            created_at=sample.created_at,
        )
        for sample in record.samples
    )
    checks = tuple(
        HarnessSandboxRetryDetailCheck(
            order=check.order,
            check_id=check.check_id,
            spec_sha256=check.spec_sha256,
            argv_sha256=check.argv_sha256,
            timeout_seconds=check.timeout_seconds,
        )
        for check in request.checks
    )
    ticket_view = (
        HarnessSandboxRetryDetailTicket(
            ticket_id=ticket.ticket_id,
            epoch=ticket.epoch,
            state=ticket.state,
            lease_expires_at=ticket.lease_expires_at,
            updated_at=ticket.updated_at,
            terminal_code=ticket.terminal_code,
            request_sha256=ticket.request_sha256,
        )
        if ticket is not None
        else None
    )
    actionable = item.recovery_status in _ACTIONABLE
    draft = HarnessSandboxRetryDetail.model_construct(
        retry_action_id=dispatch.retry_action_id,
        dispatch_id=dispatch.dispatch_id,
        recovery_status=item.recovery_status,
        dispatch_state=dispatch.state,
        dispatch_epoch=dispatch.epoch,
        dispatch_request_sha256=dispatch.request_sha256,
        created_at=dispatch.created_at,
        updated_at=dispatch.updated_at,
        terminal_code=dispatch.terminal_code,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        source_ticket_id=cancel.ticket_id,
        source_ticket_request_sha256=record.source_ticket.request_sha256,
        request_id=request.request_id,
        request_sha256=request.request_sha256,
        batch_id=request.batch_id,
        suite_id=request.suite_id,
        requested_samples=request.requested_samples,
        persisted_samples=len(samples),
        source_revision=request.source_revision,
        source_tree_sha256=request.source_tree_sha256,
        profile_sha256=request.profile_sha256,
        max_total_duration_seconds=request.max_total_duration_seconds,
        checks=checks,
        ticket=ticket_view,
        samples=samples,
        protection_refs=(),
        can_resume=actionable,
        resume_command=(
            _resume_command(
                retry_action_id=dispatch.retry_action_id,
                dispatch_id=dispatch.dispatch_id,
                retry_receipt_id=retry.receipt_id,
                retry_receipt_sha256=retry.receipt_sha256,
            )
            if actionable
            else ""
        ),
    )
    detail = HarnessSandboxRetryDetail.model_validate(
        {
            **draft.model_dump(mode="json"),
            "protection_refs": [
                ref.model_dump(mode="json") for ref in _protection_refs(draft)
            ],
        }
    )
    return _snapshot(
        status="ok",
        retry_action_id=dispatch.retry_action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=record.assessed_at,
        detail=detail,
        error_code="",
    )


def missing_sandbox_retry_detail_snapshot(
    *,
    retry_action_id: str,
    dispatch_id: str,
    assessed_at: str | None = None,
) -> HarnessSandboxRetryDetailSnapshot:
    return _snapshot(
        status="not_found",
        retry_action_id=retry_action_id,
        dispatch_id=dispatch_id,
        assessed_at=_normalize_timestamp(
            assessed_at or datetime.now(UTC).isoformat(),
            field="assessed_at",
        ),
        detail=None,
        error_code="sandbox_retry_detail_not_found",
    )


def unavailable_sandbox_retry_detail_snapshot(
    *,
    retry_action_id: str,
    dispatch_id: str,
    error_code: str = "sandbox_retry_detail_unavailable",
    assessed_at: str | None = None,
) -> HarnessSandboxRetryDetailSnapshot:
    normalized_error = (
        error_code.strip().lower() if isinstance(error_code, str) else ""
    )
    if not _ERROR_CODE_RE.fullmatch(normalized_error):
        normalized_error = "sandbox_retry_detail_unavailable"
    return _snapshot(
        status="unavailable",
        retry_action_id=retry_action_id,
        dispatch_id=dispatch_id,
        assessed_at=_normalize_timestamp(
            assessed_at or datetime.now(UTC).isoformat(),
            field="assessed_at",
        ),
        detail=None,
        error_code=normalized_error,
    )


def render_sandbox_retry_detail(
    snapshot: HarnessSandboxRetryDetailSnapshot,
) -> str:
    if snapshot.status == "not_found":
        return (
            "## Sandbox retry dispatch 未找到\n\n"
            f"- Retry action：`{snapshot.retry_action_id}`\n"
            f"- Dispatch：`{snapshot.dispatch_id}`\n\n"
            "当前工作区没有匹配的成对权威记录；没有执行任何恢复或清理动作。"
        )
    if snapshot.status == "unavailable":
        return (
            "## Sandbox retry dispatch 详情暂不可用\n\n"
            f"- 错误码：`{snapshot.error_code}`\n\n"
            "状态库读取失败；没有执行任何恢复或清理动作。请运行 `/harness doctor`。"
        )
    assert snapshot.detail is not None
    detail = snapshot.detail
    labels = {
        "pending": "等待首次 claim",
        "live": "当前 ticket 仍存活",
        "recovery_required": "租约已过期，可显式恢复",
        "reconcile_required": "ticket 已终态，需先对账",
        "clock_regression": "时钟倒退，禁止恢复",
        "terminal": "已终态",
    }
    lines = [
        "## Sandbox retry dispatch 详情",
        "",
        "本页面只读取持久事实，不会 claim、续租、恢复或清理任何记录。",
        "",
        f"- 状态：**{labels[detail.recovery_status]}** (`{detail.recovery_status}`)",
        f"- Retry action：`{snapshot.retry_action_id}`",
        (
            f"- Dispatch：`{snapshot.dispatch_id}` / `{detail.dispatch_state}` / "
            f"epoch `{detail.dispatch_epoch}`"
        ),
        f"- Dispatch digest：`{detail.dispatch_request_sha256}`",
        f"- 创建/更新：`{detail.created_at}` / `{detail.updated_at}`",
        f"- Retry receipt：`{detail.retry_receipt_id}` / `{detail.retry_receipt_sha256}`",
        f"- Cancel receipt：`{detail.cancel_receipt_id}` / `{detail.cancel_receipt_sha256}`",
        (
            f"- Source ticket：`{detail.source_ticket_id}` / "
            f"`{detail.source_ticket_request_sha256}`"
        ),
        "",
        "### Request Manifest",
        "",
        f"- Request：`{detail.request_id}` / `{detail.request_sha256}`",
        f"- Batch/Suite：`{detail.batch_id}` / `{detail.suite_id}`",
        f"- H5a：{detail.persisted_samples}/{detail.requested_samples}",
        f"- Source revision：`{detail.source_revision}`",
        f"- Source tree：`{detail.source_tree_sha256}`",
        f"- Profile：`{detail.profile_sha256}`",
        f"- 总时限：{detail.max_total_duration_seconds}s",
        "- Checks：" + ", ".join(
            f"`{check.check_id}`({check.timeout_seconds}s)" for check in detail.checks
        ),
        "",
        "### 当前 ticket fence",
        "",
    ]
    if detail.ticket is None:
        lines.append("- 尚未 claim；没有 ticket。")
    else:
        lines.extend(
            (
                (
                    f"- Ticket：`{detail.ticket.ticket_id}` / "
                    f"`{detail.ticket.state}` / epoch `{detail.ticket.epoch}`"
                ),
                f"- Lease：`{detail.ticket.lease_expires_at}`",
                f"- 更新：`{detail.ticket.updated_at}`",
                f"- Ticket digest：`{detail.ticket.request_sha256}`",
                f"- Terminal code：`{detail.ticket.terminal_code or '-'}`",
            )
        )
    lines.extend(("", "### 连续 H5a", ""))
    if detail.samples:
        lines.extend(
            f"- #{sample.sample_index} `{sample.status}` · `{sample.result_sha256}`"
            for sample in detail.samples
        )
    else:
        lines.append("- 尚无持久化样本。")
    kinds: dict[str, int] = {}
    for ref in detail.protection_refs:
        kinds[ref.kind] = kinds.get(ref.kind, 0) + 1
    lines.extend(
        (
            "",
            "### Retention 保护集合（只读）",
            "",
            f"- 共 {len(detail.protection_refs)} 项："
            + " · ".join(f"{kind} {count}" for kind, count in kinds.items()),
            "- 当前切片只证明引用关系；不会删除、归档或生成 prune receipt。",
        )
    )
    if detail.can_resume:
        lines.extend(("", "### 显式恢复命令", "", "```text", detail.resume_command, "```"))
    elif detail.recovery_status == "live":
        lines.extend(("", "当前 lease 仍有效，不提供并发恢复命令。"))
    else:
        lines.extend(("", "当前状态不可直接恢复；请先解决 ticket/dispatch 对账。"))
    lines.extend(("", f"Snapshot：`{snapshot.snapshot_sha256}`"))
    return "\n".join(lines)


def _protection_refs(
    detail: HarnessSandboxRetryDetail,
) -> tuple[HarnessSandboxRetryProtectionRef, ...]:
    refs = [
        HarnessSandboxRetryProtectionRef(
            kind="dispatch",
            ref_id=detail.dispatch_id,
            sha256=detail.dispatch_request_sha256,
            reason="dispatch_authority",
        ),
        HarnessSandboxRetryProtectionRef(
            kind="retry_receipt",
            ref_id=detail.retry_receipt_id,
            sha256=detail.retry_receipt_sha256,
            reason="retry_authority",
        ),
        HarnessSandboxRetryProtectionRef(
            kind="cancel_receipt",
            ref_id=detail.cancel_receipt_id,
            sha256=detail.cancel_receipt_sha256,
            reason="cancel_chain",
        ),
        HarnessSandboxRetryProtectionRef(
            kind="request_manifest",
            ref_id=detail.request_id,
            sha256=detail.request_sha256,
            reason="request_replay",
        ),
        HarnessSandboxRetryProtectionRef(
            kind="source_ticket",
            ref_id=detail.source_ticket_id,
            sha256=detail.source_ticket_request_sha256,
            reason="source_ticket_chain",
        ),
    ]
    if detail.ticket is not None:
        refs.append(
            HarnessSandboxRetryProtectionRef(
                kind="ticket",
                ref_id=detail.ticket.ticket_id,
                sha256=detail.ticket.request_sha256,
                reason="current_ticket_fence",
            )
        )
    refs.extend(
        HarnessSandboxRetryProtectionRef(
            kind="h5a_sample",
            ref_id=f"sample:{sample.sample_index}",
            sha256=sample.result_sha256,
            reason="continuous_h5a_prefix",
        )
        for sample in detail.samples
    )
    return tuple(refs)


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


def _snapshot(
    *,
    status: DetailStatus,
    retry_action_id: str,
    dispatch_id: str,
    assessed_at: str,
    detail: HarnessSandboxRetryDetail | None,
    error_code: str,
) -> HarnessSandboxRetryDetailSnapshot:
    payload = {
        "schema_version": SANDBOX_RETRY_DETAIL_SCHEMA_VERSION,
        "status": status,
        "retry_action_id": retry_action_id,
        "dispatch_id": dispatch_id,
        "assessed_at": assessed_at,
        "detail": detail.model_dump(mode="json") if detail is not None else None,
        "error_code": error_code,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return HarnessSandboxRetryDetailSnapshot(
        snapshot_id=f"hsrrd_{digest[:24]}",
        snapshot_sha256=digest,
        **payload,
    )


def _snapshot_sha256(
    *,
    status: DetailStatus,
    retry_action_id: str,
    dispatch_id: str,
    assessed_at: str,
    detail: HarnessSandboxRetryDetail | None,
    error_code: str,
) -> str:
    payload = {
        "schema_version": SANDBOX_RETRY_DETAIL_SCHEMA_VERSION,
        "status": status,
        "retry_action_id": retry_action_id,
        "dispatch_id": dispatch_id,
        "assessed_at": assessed_at,
        "detail": detail.model_dump(mode="json") if detail is not None else None,
        "error_code": error_code,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_timestamp(value: str, *, field: str) -> None:
    _normalize_timestamp(value, field=field)


def _normalize_timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Sandbox retry detail {field} 必须是 ISO 8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Sandbox retry detail {field} 必须包含时区偏移。")
    return parsed.astimezone(UTC).isoformat()


__all__ = [
    "HarnessSandboxRetryDetail",
    "HarnessSandboxRetryDetailSnapshot",
    "HarnessSandboxRetryProtectionRef",
    "build_sandbox_retry_detail_snapshot",
    "missing_sandbox_retry_detail_snapshot",
    "render_sandbox_retry_detail",
    "unavailable_sandbox_retry_detail_snapshot",
]
