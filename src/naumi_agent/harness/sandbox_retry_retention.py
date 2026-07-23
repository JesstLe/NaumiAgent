"""Read-only retention preview for terminal Sandbox retry dispatch cohorts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.sandbox_retry_detail import (
    HarnessSandboxRetryProtectionRef,
    build_sandbox_retry_detail_snapshot,
)
from naumi_agent.harness.store import HarnessSandboxRetryRetentionPage

SANDBOX_RETRY_RETENTION_SCHEMA_VERSION = 1
RetentionPreviewStatus = Literal["ok", "unavailable"]
TerminalState = Literal["completed", "failed", "cancelled"]
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HarnessSandboxRetryRetentionPolicy(_StrictModel):
    retention_days: int = Field(ge=1, le=3_650)
    limit: int = Field(ge=1, le=20)
    scan_limit: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.scan_limit < self.limit:
            raise ValueError("Sandbox retry retention scan_limit 不能小于 limit。")
        return self


class HarnessSandboxRetryRetentionCandidate(_StrictModel):
    candidate_id: str = Field(pattern=r"^hsrrp_[0-9a-f]{24}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_action_id: str = Field(pattern=r"^hsar_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^hsard_[0-9a-f]{24}$")
    terminal_state: TerminalState
    terminal_code: str = Field(max_length=128)
    updated_at: str = Field(min_length=1, max_length=64)
    age_seconds: int = Field(ge=0)
    retry_receipt_id: str = Field(pattern=r"^hsarr_[0-9a-f]{24}$")
    cancel_receipt_id: str = Field(pattern=r"^hsacr_[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^hseval_[0-9a-f]{24}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    suite_id: str = Field(pattern=r"^harness_sandbox_[0-9a-f]{24}$")
    persisted_samples: int = Field(ge=0, le=100)
    detail_snapshot_id: str = Field(pattern=r"^hsrrd_[0-9a-f]{24}$")
    detail_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_refs: tuple[HarnessSandboxRetryProtectionRef, ...] = Field(
        min_length=6,
        max_length=106,
    )
    protection_refs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        _require_timestamp(self.updated_at, field="candidate.updated_at")
        expected_refs = _digest_json(
            [ref.model_dump(mode="json") for ref in self.protection_refs]
        )
        if self.protection_refs_sha256 != expected_refs:
            raise ValueError("Sandbox retry retention protection refs 摘要不一致。")
        expected = _candidate_sha256(self)
        if self.candidate_sha256 != expected:
            raise ValueError("Sandbox retry retention candidate SHA-256 不一致。")
        if self.candidate_id != f"hsrrp_{expected[:24]}":
            raise ValueError("Sandbox retry retention candidate ID 不一致。")
        return self


class HarnessSandboxRetryRetentionPreview(_StrictModel):
    schema_version: Literal[1] = SANDBOX_RETRY_RETENTION_SCHEMA_VERSION
    preview_id: str = Field(pattern=r"^hsrrpv_[0-9a-f]{24}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RetentionPreviewStatus
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessed_at: str = Field(min_length=1, max_length=64)
    cutoff_at: str = Field(min_length=1, max_length=64)
    policy: HarnessSandboxRetryRetentionPolicy
    total_dispatch_count: int = Field(ge=0)
    open_dispatch_count: int = Field(ge=0)
    terminal_dispatch_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    deferred_eligible_count: int = Field(ge=0)
    scan_truncated: bool
    selection_truncated: bool
    candidates: tuple[HarnessSandboxRetryRetentionCandidate, ...] = Field(
        max_length=20
    )
    error_code: str = Field(max_length=128)

    @model_validator(mode="after")
    def validate_preview(self) -> Self:
        assessed = _parse_timestamp(self.assessed_at, field="assessed_at")
        cutoff = _parse_timestamp(self.cutoff_at, field="cutoff_at")
        if cutoff >= assessed:
            raise ValueError("Sandbox retry retention cutoff 必须早于 assessed_at。")
        if cutoff != assessed - timedelta(days=self.policy.retention_days):
            raise ValueError("Sandbox retry retention cutoff 与 policy 不一致。")
        if self.status == "ok":
            if self.error_code:
                raise ValueError("ok retention preview 不能包含错误码。")
            if self.selected_count != len(self.candidates):
                raise ValueError("retention selected_count 与 candidates 不一致。")
            if self.scanned_count < self.selected_count:
                raise ValueError("retention scanned_count 小于 selected_count。")
            if self.eligible_count < self.scanned_count:
                raise ValueError("retention eligible_count 小于 scanned_count。")
            if self.scanned_count != min(
                self.eligible_count,
                self.policy.scan_limit,
            ):
                raise ValueError("retention scanned_count 与 scan_limit 不一致。")
            if self.selected_count != min(
                self.scanned_count,
                self.policy.limit,
            ):
                raise ValueError("retention selected_count 与 limit 不一致。")
            if self.terminal_dispatch_count < self.eligible_count:
                raise ValueError("retention terminal_count 小于 eligible_count。")
            if (
                self.total_dispatch_count
                < self.open_dispatch_count + self.terminal_dispatch_count
            ):
                raise ValueError("retention dispatch 汇总不一致。")
            expected_deferred = self.eligible_count - self.selected_count
            if self.deferred_eligible_count != expected_deferred:
                raise ValueError("retention deferred 计数不一致。")
            if self.scan_truncated is not (
                self.eligible_count > self.policy.scan_limit
            ):
                raise ValueError("retention scan_truncated 不一致。")
            if self.selection_truncated is not (expected_deferred > 0):
                raise ValueError("retention selection_truncated 不一致。")
            if tuple(
                (item.updated_at, item.retry_action_id) for item in self.candidates
            ) != tuple(
                sorted(
                    (item.updated_at, item.retry_action_id)
                    for item in self.candidates
                )
            ):
                raise ValueError("retention candidates 顺序不稳定。")
            for candidate in self.candidates:
                updated = _parse_timestamp(
                    candidate.updated_at,
                    field="candidate.updated_at",
                )
                if updated > cutoff:
                    raise ValueError("retention candidate 尚未超过 cutoff。")
                expected_age = int((assessed - updated).total_seconds())
                if candidate.age_seconds != expected_age:
                    raise ValueError("retention candidate age_seconds 不一致。")
        elif (
            self.candidates
            or self.total_dispatch_count
            or self.open_dispatch_count
            or self.terminal_dispatch_count
            or self.eligible_count
            or self.selected_count
            or self.scanned_count
            or self.deferred_eligible_count
            or self.scan_truncated
            or self.selection_truncated
            or not _ERROR_CODE_RE.fullmatch(self.error_code)
        ):
            raise ValueError("unavailable retention preview 必须为空并包含稳定错误码。")
        expected = _preview_sha256(self)
        if self.preview_sha256 != expected:
            raise ValueError("Sandbox retry retention preview SHA-256 不一致。")
        if self.preview_id != f"hsrrpv_{expected[:24]}":
            raise ValueError("Sandbox retry retention preview ID 不一致。")
        return self


def build_sandbox_retry_retention_preview(
    page: HarnessSandboxRetryRetentionPage,
) -> HarnessSandboxRetryRetentionPreview:
    assessed = _parse_timestamp(page.assessed_at, field="assessed_at")
    candidates: list[HarnessSandboxRetryRetentionCandidate] = []
    for record in page.records[: page.limit]:
        detail_snapshot = build_sandbox_retry_detail_snapshot(record)
        detail = detail_snapshot.detail
        if detail is None or detail.recovery_status != "terminal":
            raise ValueError("Sandbox retry retention 只能选择已对账终态 dispatch。")
        age_seconds = int(
            (assessed - _parse_timestamp(detail.updated_at, field="updated_at"))
            .total_seconds()
        )
        base = {
            "retry_action_id": detail.retry_action_id,
            "dispatch_id": detail.dispatch_id,
            "terminal_state": detail.dispatch_state,
            "terminal_code": detail.terminal_code,
            "updated_at": detail.updated_at,
            "age_seconds": age_seconds,
            "retry_receipt_id": detail.retry_receipt_id,
            "cancel_receipt_id": detail.cancel_receipt_id,
            "request_id": detail.request_id,
            "batch_id": detail.batch_id,
            "suite_id": detail.suite_id,
            "persisted_samples": detail.persisted_samples,
            "detail_snapshot_id": detail_snapshot.snapshot_id,
            "detail_snapshot_sha256": detail_snapshot.snapshot_sha256,
            "protection_refs": [
                ref.model_dump(mode="json") for ref in detail.protection_refs
            ],
            "protection_refs_sha256": _digest_json(
                [ref.model_dump(mode="json") for ref in detail.protection_refs]
            ),
        }
        digest = _digest_json(base)
        candidates.append(
            HarnessSandboxRetryRetentionCandidate(
                candidate_id=f"hsrrp_{digest[:24]}",
                candidate_sha256=digest,
                **base,
            )
        )
    policy = HarnessSandboxRetryRetentionPolicy(
        retention_days=page.retention_days,
        limit=page.limit,
        scan_limit=page.scan_limit,
    )
    payload = {
        "schema_version": SANDBOX_RETRY_RETENTION_SCHEMA_VERSION,
        "status": "ok",
        "workspace_sha256": hashlib.sha256(
            page.workspace_root.encode("utf-8")
        ).hexdigest(),
        "assessed_at": page.assessed_at,
        "cutoff_at": page.cutoff_at,
        "policy": policy.model_dump(mode="json"),
        "total_dispatch_count": page.total_dispatch_count,
        "open_dispatch_count": page.open_dispatch_count,
        "terminal_dispatch_count": page.terminal_dispatch_count,
        "eligible_count": page.eligible_count,
        "scanned_count": page.scanned_count,
        "selected_count": len(candidates),
        "deferred_eligible_count": page.eligible_count - len(candidates),
        "scan_truncated": page.eligible_count > page.scan_limit,
        "selection_truncated": page.eligible_count > len(candidates),
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "error_code": "",
    }
    return _preview(payload)


def unavailable_sandbox_retry_retention_preview(
    *,
    workspace_root: str,
    assessed_at: str | None = None,
    retention_days: int = 30,
    limit: int = 20,
    scan_limit: int = 100,
    error_code: str = "sandbox_retry_retention_unavailable",
) -> HarnessSandboxRetryRetentionPreview:
    assessment = _normalize_timestamp(
        assessed_at or datetime.now(UTC).isoformat(),
        field="assessed_at",
    )
    cutoff_at = (
        datetime.fromisoformat(assessment) - timedelta(days=retention_days)
    ).isoformat()
    normalized_error = error_code.strip().lower()
    if not _ERROR_CODE_RE.fullmatch(normalized_error):
        normalized_error = "sandbox_retry_retention_unavailable"
    payload = {
        "schema_version": SANDBOX_RETRY_RETENTION_SCHEMA_VERSION,
        "status": "unavailable",
        "workspace_sha256": hashlib.sha256(
            workspace_root.encode("utf-8")
        ).hexdigest(),
        "assessed_at": assessment,
        "cutoff_at": cutoff_at,
        "policy": HarnessSandboxRetryRetentionPolicy(
            retention_days=retention_days,
            limit=limit,
            scan_limit=scan_limit,
        ).model_dump(mode="json"),
        "total_dispatch_count": 0,
        "open_dispatch_count": 0,
        "terminal_dispatch_count": 0,
        "eligible_count": 0,
        "scanned_count": 0,
        "selected_count": 0,
        "deferred_eligible_count": 0,
        "scan_truncated": False,
        "selection_truncated": False,
        "candidates": [],
        "error_code": normalized_error,
    }
    return _preview(payload)


def render_sandbox_retry_retention_preview(
    preview: HarnessSandboxRetryRetentionPreview,
) -> str:
    if preview.status == "unavailable":
        return (
            "## Sandbox retry retention 预览暂不可用\n\n"
            f"- 错误码：`{preview.error_code}`\n\n"
            "没有删除、归档或修改任何记录。请运行 `/harness doctor`。"
        )
    lines = [
        "## Sandbox retry retention 预览",
        "",
        "本结果仅为只读候选预览，不是删除授权，不会生成 prune receipt。",
        "",
        f"- 评估时间：`{preview.assessed_at}`",
        f"- 截止时间：`{preview.cutoff_at}`（保留 {preview.policy.retention_days} 天）",
        f"- Dispatch：总计 {preview.total_dispatch_count} · 打开 "
        f"{preview.open_dispatch_count} · 终态 {preview.terminal_dispatch_count}",
        f"- 年龄合格：{preview.eligible_count}",
        f"- 本轮扫描/选择：{preview.scanned_count}/{preview.selected_count}",
        f"- 延后：{preview.deferred_eligible_count}",
        "",
    ]
    if preview.scan_truncated:
        lines.append("⚠ 年龄合格记录超过扫描上限；未扫描部分不会被声称已校验。")
    if preview.selection_truncated:
        lines.append("⚠ 本轮选择上限已生效；其余合格记录保持不变。")
    if not preview.candidates:
        lines.append("当前没有超过保留期的已对账终态 retry dispatch。")
    else:
        lines.extend(("### 只读候选", ""))
        for candidate in preview.candidates:
            kinds: dict[str, int] = {}
            for ref in candidate.protection_refs:
                kinds[ref.kind] = kinds.get(ref.kind, 0) + 1
            lines.extend(
                (
                    f"#### `{candidate.dispatch_id}`",
                    "",
                    f"- 状态：`{candidate.terminal_state}`",
                    f"- Retry action：`{candidate.retry_action_id}`",
                    f"- 更新时间：`{candidate.updated_at}`",
                    f"- 年龄：{candidate.age_seconds // 86_400} 天",
                    f"- Batch/Suite：`{candidate.batch_id}` / `{candidate.suite_id}`",
                    f"- H5a：{candidate.persisted_samples}",
                    "- 保护引用："
                    + " · ".join(f"{kind} {count}" for kind, count in kinds.items()),
                    f"- 引用摘要：`{candidate.protection_refs_sha256}`",
                    f"- Candidate：`{candidate.candidate_sha256}`",
                    "",
                )
            )
    lines.extend(
        (
            "打开中的 dispatch 以及每个候选的完整引用链仍受保护。",
            "下一阶段必须重新校验 preview/candidate/fence 后签发显式 prune receipt；"
            "本预览本身永远不能执行删除。",
            "",
            f"Preview：`{preview.preview_sha256}`",
        )
    )
    return "\n".join(lines)


def _candidate_sha256(candidate: HarnessSandboxRetryRetentionCandidate) -> str:
    return _digest_json(
        candidate.model_dump(
            mode="json",
            exclude={"candidate_id", "candidate_sha256"},
        )
    )


def _preview_sha256(preview: HarnessSandboxRetryRetentionPreview) -> str:
    return _digest_json(
        preview.model_dump(
            mode="json",
            exclude={"preview_id", "preview_sha256"},
        )
    )


def _preview(payload: dict[str, object]) -> HarnessSandboxRetryRetentionPreview:
    digest = _digest_json(payload)
    return HarnessSandboxRetryRetentionPreview(
        preview_id=f"hsrrpv_{digest[:24]}",
        preview_sha256=digest,
        **payload,
    )


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Sandbox retry retention {field} 必须是 ISO 8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Sandbox retry retention {field} 必须包含时区偏移。")
    return parsed.astimezone(UTC)


def _normalize_timestamp(value: str, *, field: str) -> str:
    return _parse_timestamp(value, field=field).isoformat()


def _require_timestamp(value: str, *, field: str) -> None:
    _parse_timestamp(value, field=field)


__all__ = [
    "HarnessSandboxRetryRetentionCandidate",
    "HarnessSandboxRetryRetentionPreview",
    "build_sandbox_retry_retention_preview",
    "render_sandbox_retry_retention_preview",
    "unavailable_sandbox_retry_retention_preview",
]
