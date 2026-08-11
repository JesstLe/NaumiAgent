"""Authenticated admission decisions for terminal outbox retention apply."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

TERMINAL_OUTBOX_RETENTION_ADMISSION_SCHEMA_VERSION = 1

RetentionAdmissionRejection = Literal[
    "no_candidates",
    "preview_authority_changed",
]
RetentionDeleteOperation = Literal[
    "delete_requeue_receipts",
    "delete_abandon_receipt",
    "delete_failure_head",
    "delete_failure_events",
    "delete_dispatch_events",
    "delete_dispatch_snapshot",
    "delete_outbox_events",
    "delete_outbox_snapshot",
]
_DELETE_OPERATION_ORDER = (
    "delete_requeue_receipts",
    "delete_abandon_receipt",
    "delete_failure_head",
    "delete_failure_events",
    "delete_dispatch_events",
    "delete_dispatch_snapshot",
    "delete_outbox_events",
    "delete_outbox_snapshot",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PursuitTerminalOutboxRetentionPlanStep(_StrictModel):
    ordinal: int = Field(ge=1, le=8)
    operation: RetentionDeleteOperation
    record_count: int = Field(ge=1, le=10_000)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    precondition: Literal["exact_authenticated_snapshot"]
    recovery_action: Literal["rollback_candidate_transaction"]
    before_killpoint: str = Field(pattern=r"^ptork_before_[0-9a-f]{16}$")
    after_killpoint: str = Field(pattern=r"^ptork_after_[0-9a-f]{16}$")
    step_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        expected = _step_sha256(self)
        if self.step_sha256 != expected:
            raise ValueError("terminal outbox retention plan step 摘要不一致。")
        if self.before_killpoint != f"ptork_before_{expected[:16]}":
            raise ValueError("terminal outbox retention before killpoint 不一致。")
        if self.after_killpoint != f"ptork_after_{expected[:16]}":
            raise ValueError("terminal outbox retention after killpoint 不一致。")
        return self


class PursuitTerminalOutboxRetentionCandidatePlan(_StrictModel):
    candidate_id: str = Field(pattern=r"^ptorp_[0-9a-f]{24}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_refs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_row_count: int = Field(ge=1, le=50_000)
    steps: tuple[PursuitTerminalOutboxRetentionPlanStep, ...] = Field(
        min_length=1,
        max_length=8,
    )
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        if tuple(step.ordinal for step in self.steps) != tuple(
            range(1, len(self.steps) + 1)
        ):
            raise ValueError("terminal outbox retention plan step 序号不连续。")
        if len({step.operation for step in self.steps}) != len(self.steps):
            raise ValueError("terminal outbox retention plan operation 不得重复。")
        operation_indexes = tuple(
            _DELETE_OPERATION_ORDER.index(step.operation) for step in self.steps
        )
        if operation_indexes != tuple(sorted(operation_indexes)):
            raise ValueError("terminal outbox retention plan operation 顺序无效。")
        if any(step.scope_sha256 != self.candidate_sha256 for step in self.steps):
            raise ValueError("terminal outbox retention plan step scope 不一致。")
        if self.recovery_row_count != sum(step.record_count for step in self.steps):
            raise ValueError("terminal outbox retention recovery row 计数不一致。")
        if self.plan_sha256 != _candidate_plan_sha256(self):
            raise ValueError("terminal outbox retention candidate plan 摘要不一致。")
        return self


class PursuitTerminalOutboxRetentionAdmission(_StrictModel):
    schema_version: Literal[1] = TERMINAL_OUTBOX_RETENTION_ADMISSION_SCHEMA_VERSION
    admission_id: str = Field(pattern=r"^ptora_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["admitted", "rejected"]
    preview_id: str = Field(pattern=r"^ptorpv_[0-9a-f]{24}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: str = Field(min_length=1, max_length=64)
    durable: bool
    execution_authority: Literal[False] = False
    physical_prune_authority: Literal[False] = False
    candidate_count: int = Field(ge=0, le=20)
    planned_write_count: int = Field(ge=0, le=160)
    recovery_snapshot_set_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    rejection_reasons: tuple[RetentionAdmissionRejection, ...] = Field(max_length=2)
    candidates: tuple[PursuitTerminalOutboxRetentionCandidatePlan, ...] = Field(
        max_length=20
    )

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        _parse_timestamp(self.decided_at)
        if tuple(sorted(set(self.rejection_reasons))) != self.rejection_reasons:
            raise ValueError("terminal outbox retention rejection_reasons 无效。")
        if self.preview_id != f"ptorpv_{self.preview_sha256[:24]}":
            raise ValueError("terminal outbox retention preview ID 与摘要不一致。")
        if self.status == "admitted":
            if (
                not self.durable
                or self.rejection_reasons
                or not self.candidates
                or not self.recovery_snapshot_set_sha256
            ):
                raise ValueError("admitted terminal outbox retention 准入事实不完整。")
        elif (
            self.durable
            or self.candidates
            or self.candidate_count
            or self.planned_write_count
            or self.recovery_snapshot_set_sha256
            or not self.rejection_reasons
        ):
            raise ValueError("rejected terminal outbox retention 准入事实不一致。")
        if self.candidate_count != len(self.candidates):
            raise ValueError("terminal outbox retention candidate_count 不一致。")
        if self.planned_write_count != sum(
            len(candidate.steps) for candidate in self.candidates
        ):
            raise ValueError("terminal outbox retention planned_write_count 不一致。")
        identities = tuple(candidate.candidate_id for candidate in self.candidates)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("terminal outbox retention candidate plan 顺序无效。")
        expected = _admission_sha256(self)
        if self.admission_sha256 != expected:
            raise ValueError("terminal outbox retention admission 摘要不一致。")
        if self.admission_id != f"ptora_{expected[:24]}":
            raise ValueError("terminal outbox retention admission ID 不一致。")
        return self


def new_retention_plan_step(
    *,
    ordinal: int,
    operation: RetentionDeleteOperation,
    record_count: int,
    scope_sha256: str,
) -> PursuitTerminalOutboxRetentionPlanStep:
    base = {
        "ordinal": ordinal,
        "operation": operation,
        "record_count": record_count,
        "scope_sha256": scope_sha256,
        "precondition": "exact_authenticated_snapshot",
        "recovery_action": "rollback_candidate_transaction",
    }
    digest = _digest_json(base)
    return PursuitTerminalOutboxRetentionPlanStep(
        **base,
        before_killpoint=f"ptork_before_{digest[:16]}",
        after_killpoint=f"ptork_after_{digest[:16]}",
        step_sha256=digest,
    )


def new_retention_candidate_plan(
    *,
    candidate_id: str,
    candidate_sha256: str,
    protection_refs_sha256: str,
    recovery_snapshot_sha256: str,
    operation_counts: tuple[tuple[RetentionDeleteOperation, int], ...],
) -> PursuitTerminalOutboxRetentionCandidatePlan:
    steps = tuple(
        new_retention_plan_step(
            ordinal=ordinal,
            operation=operation,
            record_count=count,
            scope_sha256=candidate_sha256,
        )
        for ordinal, (operation, count) in enumerate(operation_counts, start=1)
        if count > 0
    )
    base = {
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha256,
        "protection_refs_sha256": protection_refs_sha256,
        "recovery_snapshot_sha256": recovery_snapshot_sha256,
        "recovery_row_count": sum(step.record_count for step in steps),
        "steps": [step.model_dump(mode="json") for step in steps],
    }
    return PursuitTerminalOutboxRetentionCandidatePlan(
        **base,
        plan_sha256=_digest_json(base),
    )


def new_retention_admission(
    *,
    status: Literal["admitted", "rejected"],
    preview_id: str,
    preview_sha256: str,
    workspace_sha256: str,
    decided_at: float,
    candidates: tuple[PursuitTerminalOutboxRetentionCandidatePlan, ...] = (),
    recovery_snapshot_set_sha256: str = "",
    rejection_reasons: tuple[RetentionAdmissionRejection, ...] = (),
) -> PursuitTerminalOutboxRetentionAdmission:
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    base = {
        "schema_version": TERMINAL_OUTBOX_RETENTION_ADMISSION_SCHEMA_VERSION,
        "status": status,
        "preview_id": preview_id,
        "preview_sha256": preview_sha256,
        "workspace_sha256": workspace_sha256,
        "decided_at": datetime.fromtimestamp(decided_at, UTC).isoformat(),
        "durable": status == "admitted",
        "execution_authority": False,
        "physical_prune_authority": False,
        "candidate_count": len(ordered_candidates),
        "planned_write_count": sum(len(item.steps) for item in ordered_candidates),
        "recovery_snapshot_set_sha256": recovery_snapshot_set_sha256,
        "rejection_reasons": tuple(sorted(set(rejection_reasons))),
        "candidates": [item.model_dump(mode="json") for item in ordered_candidates],
    }
    digest = _digest_json(base)
    return PursuitTerminalOutboxRetentionAdmission(
        **base,
        admission_id=f"ptora_{digest[:24]}",
        admission_sha256=digest,
    )


def render_terminal_outbox_retention_admission(
    admission: PursuitTerminalOutboxRetentionAdmission,
) -> str:
    lines = [
        "## Pursuit 终态 Outbox retention apply 准入",
        "",
        f"- 决策：`{admission.status}`",
        f"- 准入回执：`{admission.admission_id}`",
        f"- 绑定 preview：`{admission.preview_id}` / `{admission.preview_sha256}`",
        f"- 决策时间：`{admission.decided_at}`",
    ]
    if admission.status == "rejected":
        labels = {
            "no_candidates": "preview 没有可规划候选",
            "preview_authority_changed": "preview 或保护引用已变化，请重新预演",
        }
        lines.extend((
            f"- 拒绝原因：{'；'.join(labels[item] for item in admission.rejection_reasons)}",
            "",
            "未持久化准入计划，也未修改任何 outbox 记录。",
        ))
        return "\n".join(lines)
    lines.extend((
        f"- 候选：{admission.candidate_count}",
        f"- 计划写点：{admission.planned_write_count}（每个写点均有 before/after killpoint）",
        f"- 恢复快照集合摘要：`{admission.recovery_snapshot_set_sha256}`",
        "",
        "准入计划已持久化，但 execution_authority=false、physical_prune_authority=false；",
        "本次没有删除、归档或改写任何 outbox 事实。",
    ))
    for candidate in admission.candidates:
        lines.extend((
            "",
            f"### 候选 `{candidate.candidate_id}`",
            f"- 恢复快照：{candidate.recovery_row_count} 行 / "
            f"`{candidate.recovery_snapshot_sha256}`",
            f"- 写点：{len(candidate.steps)}",
        ))
        for step in candidate.steps:
            lines.append(
                f"  {step.ordinal}. `{step.operation}` · {step.record_count} 行 · "
                f"`{step.before_killpoint}` / `{step.after_killpoint}`"
            )
    return "\n".join(lines)


def _step_sha256(step: PursuitTerminalOutboxRetentionPlanStep) -> str:
    return _digest_json(step.model_dump(
        mode="json",
        exclude={"before_killpoint", "after_killpoint", "step_sha256"},
    ))


def _candidate_plan_sha256(
    plan: PursuitTerminalOutboxRetentionCandidatePlan,
) -> str:
    return _digest_json(plan.model_dump(mode="json", exclude={"plan_sha256"}))


def _admission_sha256(admission: PursuitTerminalOutboxRetentionAdmission) -> str:
    return _digest_json(admission.model_dump(
        mode="json",
        exclude={"admission_id", "admission_sha256"},
    ))


def _digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("terminal outbox retention decided_at 必须包含时区。")
    return parsed.astimezone(UTC)


__all__ = [
    "PursuitTerminalOutboxRetentionAdmission",
    "PursuitTerminalOutboxRetentionCandidatePlan",
    "PursuitTerminalOutboxRetentionPlanStep",
    "new_retention_admission",
    "new_retention_candidate_plan",
    "render_terminal_outbox_retention_admission",
]
