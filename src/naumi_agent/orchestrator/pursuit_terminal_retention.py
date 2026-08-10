"""Tamper-evident, read-only retention preview for abandoned terminal outboxes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.orchestrator.pursuit_store import (
    PursuitTerminalOutboxRetentionPage,
)

TERMINAL_OUTBOX_RETENTION_SCHEMA_VERSION = 1
ProtectionKind = Literal[
    "recovery_attempt",
    "recovery_attempt_event",
    "outbox_snapshot",
    "outbox_event",
    "dispatch_snapshot",
    "dispatch_event",
    "failure_head",
    "failure_event",
    "requeue_receipt",
    "abandon_receipt",
    "boundary_decision",
    "checkpoint_pointer",
]
AbandonReason = Literal[
    "no_longer_required",
    "superseded",
    "external_resolution",
    "invalid_target",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PursuitTerminalOutboxRetentionPolicy(_StrictModel):
    retention_days: int = Field(ge=1, le=3_650)
    limit: int = Field(ge=1, le=20)
    scan_limit: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _limits_are_coherent(self) -> Self:
        if self.scan_limit < self.limit:
            raise ValueError("terminal outbox retention scan_limit 不能小于 limit。")
        return self


class PursuitTerminalOutboxProtectionRef(_StrictModel):
    kind: ProtectionKind
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PursuitTerminalOutboxRetentionCandidate(_StrictModel):
    candidate_id: str = Field(pattern=r"^ptorp_[0-9a-f]{24}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dead_letter_id: str = Field(pattern=r"^ptfail_[0-9a-f]{24}$")
    abandon_receipt_id: str = Field(pattern=r"^ptabn_[0-9a-f]{24}$")
    reason: AbandonReason
    eligibility_reason: Literal["abandoned_before_cutoff"]
    protection_reason: Literal["protected_pending_apply_authority"]
    failure_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    failure_sequence: int = Field(ge=1, le=1_000_000)
    abandoned_at: str = Field(min_length=1, max_length=64)
    age_seconds: int = Field(ge=0)
    protection_refs: tuple[PursuitTerminalOutboxProtectionRef, ...] = Field(
        min_length=10,
        max_length=5_000,
    )
    protection_refs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        _parse_timestamp(self.abandoned_at, "candidate.abandoned_at")
        ordered = tuple(
            sorted(
                self.protection_refs,
                key=lambda item: (item.kind, item.reference_sha256),
            )
        )
        if ordered != self.protection_refs:
            raise ValueError("terminal outbox protection refs 顺序不稳定。")
        identities = tuple(
            (item.kind, item.reference_sha256) for item in self.protection_refs
        )
        if len(identities) != len(set(identities)):
            raise ValueError("terminal outbox protection refs 不得重复。")
        expected_refs = _digest_json([
            item.model_dump(mode="json") for item in self.protection_refs
        ])
        if self.protection_refs_sha256 != expected_refs:
            raise ValueError("terminal outbox protection refs 摘要不一致。")
        expected = _candidate_sha256(self)
        if self.candidate_sha256 != expected:
            raise ValueError("terminal outbox retention candidate 摘要不一致。")
        if self.candidate_id != f"ptorp_{expected[:24]}":
            raise ValueError("terminal outbox retention candidate ID 不一致。")
        return self


class PursuitTerminalOutboxRetentionPreview(_StrictModel):
    schema_version: Literal[1] = TERMINAL_OUTBOX_RETENTION_SCHEMA_VERSION
    preview_id: str = Field(pattern=r"^ptorpv_[0-9a-f]{24}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessed_at: str = Field(min_length=1, max_length=64)
    cutoff_at: str = Field(min_length=1, max_length=64)
    policy: PursuitTerminalOutboxRetentionPolicy
    total_outbox_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    abandoned_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    deferred_eligible_count: int = Field(ge=0)
    scan_truncated: bool
    selection_truncated: bool
    candidates: tuple[PursuitTerminalOutboxRetentionCandidate, ...] = Field(
        max_length=20
    )

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        assessed = _parse_timestamp(self.assessed_at, "assessed_at")
        cutoff = _parse_timestamp(self.cutoff_at, "cutoff_at")
        if cutoff != assessed - timedelta(days=self.policy.retention_days):
            raise ValueError("terminal outbox retention cutoff 与 policy 不一致。")
        if self.total_outbox_count != (
            self.pending_count + self.delivered_count + self.abandoned_count
        ):
            raise ValueError("terminal outbox effective-state 汇总不一致。")
        if self.eligible_count > self.abandoned_count:
            raise ValueError("terminal outbox eligible_count 超过 abandoned_count。")
        if self.scanned_count != min(self.eligible_count, self.policy.scan_limit):
            raise ValueError("terminal outbox scanned_count 与 scan_limit 不一致。")
        if self.selected_count != len(self.candidates) or self.selected_count != min(
            self.scanned_count,
            self.policy.limit,
        ):
            raise ValueError("terminal outbox selected_count 与 candidates 不一致。")
        deferred = self.eligible_count - self.selected_count
        if self.deferred_eligible_count != deferred:
            raise ValueError("terminal outbox deferred 计数不一致。")
        if self.scan_truncated is not (
            self.eligible_count > self.policy.scan_limit
        ):
            raise ValueError("terminal outbox scan_truncated 不一致。")
        if self.selection_truncated is not (deferred > 0):
            raise ValueError("terminal outbox selection_truncated 不一致。")
        ordering = tuple(
            (item.abandoned_at, item.abandon_receipt_id)
            for item in self.candidates
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("terminal outbox retention candidates 顺序不稳定。")
        for candidate in self.candidates:
            abandoned = _parse_timestamp(
                candidate.abandoned_at,
                "candidate.abandoned_at",
            )
            if abandoned > cutoff:
                raise ValueError("terminal outbox retention candidate 尚未超过 cutoff。")
            if candidate.age_seconds != int((assessed - abandoned).total_seconds()):
                raise ValueError("terminal outbox retention candidate age 不一致。")
        expected = _preview_sha256(self)
        if self.preview_sha256 != expected:
            raise ValueError("terminal outbox retention preview 摘要不一致。")
        if self.preview_id != f"ptorpv_{expected[:24]}":
            raise ValueError("terminal outbox retention preview ID 不一致。")
        return self


def build_terminal_outbox_retention_preview(
    page: PursuitTerminalOutboxRetentionPage,
    *,
    workspace_root: str,
) -> PursuitTerminalOutboxRetentionPreview:
    assessed = datetime.fromtimestamp(page.assessed_at, UTC)
    candidates: list[PursuitTerminalOutboxRetentionCandidate] = []
    for record in page.records:
        receipt = record.disposed.receipt
        failure = record.disposed.failure
        abandoned = datetime.fromtimestamp(receipt.abandoned_at, UTC)
        refs = tuple(
            PursuitTerminalOutboxProtectionRef(
                kind=ref.kind,
                reference_sha256=ref.reference_sha256,
                fact_sha256=ref.fact_sha256,
            )
            for ref in record.protection_refs
        )
        base = {
            "dead_letter_id": receipt.dead_letter_id,
            "abandon_receipt_id": receipt.receipt_id,
            "reason": receipt.reason.value,
            "eligibility_reason": "abandoned_before_cutoff",
            "protection_reason": "protected_pending_apply_authority",
            "failure_code": failure.failure_code,
            "failure_sequence": receipt.failure_sequence,
            "abandoned_at": abandoned.isoformat(),
            "age_seconds": int((assessed - abandoned).total_seconds()),
            "protection_refs": [item.model_dump(mode="json") for item in refs],
            "protection_refs_sha256": _digest_json([
                item.model_dump(mode="json") for item in refs
            ]),
        }
        digest = _digest_json(base)
        candidates.append(PursuitTerminalOutboxRetentionCandidate(
            candidate_id=f"ptorp_{digest[:24]}",
            candidate_sha256=digest,
            **base,
        ))
    policy = PursuitTerminalOutboxRetentionPolicy(
        retention_days=page.retention_days,
        limit=page.limit,
        scan_limit=page.scan_limit,
    )
    payload = {
        "schema_version": TERMINAL_OUTBOX_RETENTION_SCHEMA_VERSION,
        "workspace_sha256": hashlib.sha256(
            str(workspace_root).encode("utf-8")
        ).hexdigest(),
        "assessed_at": assessed.isoformat(),
        "cutoff_at": datetime.fromtimestamp(page.cutoff_at, UTC).isoformat(),
        "policy": policy.model_dump(mode="json"),
        "total_outbox_count": page.total_outbox_count,
        "pending_count": page.pending_count,
        "delivered_count": page.delivered_count,
        "abandoned_count": page.abandoned_count,
        "eligible_count": page.eligible_count,
        "scanned_count": page.scanned_count,
        "selected_count": len(candidates),
        "deferred_eligible_count": page.eligible_count - len(candidates),
        "scan_truncated": page.eligible_count > page.scan_limit,
        "selection_truncated": page.eligible_count > len(candidates),
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }
    digest = _digest_json(payload)
    return PursuitTerminalOutboxRetentionPreview(
        preview_id=f"ptorpv_{digest[:24]}",
        preview_sha256=digest,
        **payload,
    )


def render_terminal_outbox_retention_preview(
    preview: PursuitTerminalOutboxRetentionPreview,
) -> str:
    lines = [
        "## Pursuit 终态 Outbox retention 预览",
        "",
        "本结果是只读候选预演，不是删除授权；未删除、归档或修改任何记录。",
        "",
        f"- 预览回执：`{preview.preview_id}`",
        f"- 评估时间：`{preview.assessed_at}`",
        f"- 截止时间：`{preview.cutoff_at}`（保留 {preview.policy.retention_days} 天）",
        f"- Effective-state：pending {preview.pending_count} · delivered "
        f"{preview.delivered_count} · abandoned {preview.abandoned_count}",
        f"- 年龄合格：{preview.eligible_count}",
        f"- 本轮扫描/选择：{preview.scanned_count}/{preview.selected_count}",
        f"- 延后：{preview.deferred_eligible_count}",
        "",
    ]
    if preview.scan_truncated:
        lines.append("⚠ 合格记录超过扫描上限；未扫描部分不会被声称已认证。")
    if preview.selection_truncated:
        lines.append("⚠ 选择上限已生效；其余合格记录保持不变。")
    if not preview.candidates:
        lines.append("当前没有超过保留期的 authenticated abandoned outbox。")
    for candidate in preview.candidates:
        kind_counts: dict[str, int] = {}
        for ref in candidate.protection_refs:
            kind_counts[ref.kind] = kind_counts.get(ref.kind, 0) + 1
        summary = " · ".join(
            f"{kind} {count}" for kind, count in sorted(kind_counts.items())
        )
        lines.extend((
            f"### 候选 `{candidate.candidate_id}`",
            f"- 死信/处置回执：`{candidate.dead_letter_id}` / "
            f"`{candidate.abandon_receipt_id}`",
            f"- 原因：`{candidate.reason}` · failure "
            f"`{candidate.failure_code}` seq {candidate.failure_sequence}",
            "- 保留判断：已处置时间早于 cutoff；当前仍受保护，"
            "尚无 apply authority。",
            f"- 已处置：`{candidate.abandoned_at}` · age {candidate.age_seconds}s",
            f"- 保护引用：{len(candidate.protection_refs)} 项 · {summary}",
            f"- 引用摘要：`{candidate.protection_refs_sha256}`",
            "",
        ))
    lines.append("该 preview 永远不能直接执行物理删除；后续动作必须重新认证全部引用。")
    return "\n".join(lines)


def _candidate_sha256(
    candidate: PursuitTerminalOutboxRetentionCandidate,
) -> str:
    return _digest_json(candidate.model_dump(
        mode="json",
        exclude={"candidate_id", "candidate_sha256"},
    ))


def _preview_sha256(preview: PursuitTerminalOutboxRetentionPreview) -> str:
    return _digest_json(preview.model_dump(
        mode="json",
        exclude={"preview_id", "preview_sha256"},
    ))


def _digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str, field: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed.astimezone(UTC)


__all__ = [
    "PursuitTerminalOutboxProtectionRef",
    "PursuitTerminalOutboxRetentionCandidate",
    "PursuitTerminalOutboxRetentionPolicy",
    "PursuitTerminalOutboxRetentionPreview",
    "build_terminal_outbox_retention_preview",
    "render_terminal_outbox_retention_preview",
]
