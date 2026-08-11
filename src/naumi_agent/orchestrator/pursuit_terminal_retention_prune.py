"""Receipts for explicit terminal outbox retention prune execution."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

TERMINAL_OUTBOX_RETENTION_PRUNE_SCHEMA_VERSION = 1


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PursuitTerminalOutboxRetentionPruneCandidate(_StrictModel):
    candidate_id: str = Field(pattern=r"^ptorp_[0-9a-f]{24}$")
    recovery_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_write_count: int = Field(ge=1, le=8)
    deleted_row_count: int = Field(ge=0, le=50_000)


class PursuitTerminalOutboxRetentionPruneReceipt(_StrictModel):
    schema_version: Literal[1] = TERMINAL_OUTBOX_RETENTION_PRUNE_SCHEMA_VERSION
    prune_id: str = Field(pattern=r"^ptorpr_[0-9a-f]{24}$")
    prune_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["dry_run", "completed"]
    admission_id: str = Field(pattern=r"^ptora_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: str = Field(min_length=1, max_length=64)
    durable: bool
    execution_requested: bool
    physical_prune_completed: bool
    candidate_count: int = Field(ge=1, le=20)
    planned_write_count: int = Field(ge=1, le=160)
    executed_write_count: int = Field(ge=0, le=160)
    deleted_row_count: int = Field(ge=0, le=1_000_000)
    tombstone_count: int = Field(ge=0, le=20)
    recovery_snapshot_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[PursuitTerminalOutboxRetentionPruneCandidate, ...] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        _parse_timestamp(self.decided_at)
        if self.candidate_count != len(self.candidates):
            raise ValueError("terminal outbox retention prune candidate_count 不一致。")
        identities = tuple(item.candidate_id for item in self.candidates)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("terminal outbox retention prune candidates 顺序无效。")
        if self.planned_write_count != sum(
            item.planned_write_count for item in self.candidates
        ):
            raise ValueError("terminal outbox retention prune planned 写点不一致。")
        if self.deleted_row_count != sum(
            item.deleted_row_count for item in self.candidates
        ):
            raise ValueError("terminal outbox retention prune deleted 行数不一致。")
        if self.status == "dry_run":
            if (
                self.durable
                or self.execution_requested
                or self.physical_prune_completed
                or self.executed_write_count
                or self.deleted_row_count
                or self.tombstone_count
                or any(item.deleted_row_count for item in self.candidates)
            ):
                raise ValueError("dry-run terminal outbox retention prune 事实不一致。")
        elif (
            not self.durable
            or not self.execution_requested
            or not self.physical_prune_completed
            or self.executed_write_count != self.planned_write_count
            or self.tombstone_count != self.candidate_count
            or any(item.deleted_row_count <= 0 for item in self.candidates)
        ):
            raise ValueError("completed terminal outbox retention prune 事实不完整。")
        expected = _receipt_sha256(self)
        if self.prune_sha256 != expected:
            raise ValueError("terminal outbox retention prune receipt 摘要不一致。")
        if self.prune_id != f"ptorpr_{expected[:24]}":
            raise ValueError("terminal outbox retention prune receipt ID 不一致。")
        return self


def new_retention_prune_receipt(
    *,
    status: Literal["dry_run", "completed"],
    admission_id: str,
    admission_sha256: str,
    workspace_sha256: str,
    decided_at: float,
    recovery_snapshot_set_sha256: str,
    candidates: tuple[tuple[str, str, int, int], ...],
) -> PursuitTerminalOutboxRetentionPruneReceipt:
    items = tuple(sorted((
        PursuitTerminalOutboxRetentionPruneCandidate(
            candidate_id=candidate_id,
            recovery_snapshot_sha256=snapshot_sha256,
            planned_write_count=planned_writes,
            deleted_row_count=deleted_rows if status == "completed" else 0,
        )
        for candidate_id, snapshot_sha256, planned_writes, deleted_rows in candidates
    ), key=lambda item: item.candidate_id))
    planned = sum(item.planned_write_count for item in items)
    base = {
        "schema_version": TERMINAL_OUTBOX_RETENTION_PRUNE_SCHEMA_VERSION,
        "status": status,
        "admission_id": admission_id,
        "admission_sha256": admission_sha256,
        "workspace_sha256": workspace_sha256,
        "decided_at": datetime.fromtimestamp(decided_at, UTC).isoformat(),
        "durable": status == "completed",
        "execution_requested": status == "completed",
        "physical_prune_completed": status == "completed",
        "candidate_count": len(items),
        "planned_write_count": planned,
        "executed_write_count": planned if status == "completed" else 0,
        "deleted_row_count": sum(item.deleted_row_count for item in items),
        "tombstone_count": len(items) if status == "completed" else 0,
        "recovery_snapshot_set_sha256": recovery_snapshot_set_sha256,
        "candidates": [item.model_dump(mode="json") for item in items],
    }
    digest = _digest_json(base)
    return PursuitTerminalOutboxRetentionPruneReceipt(
        **base,
        prune_id=f"ptorpr_{digest[:24]}",
        prune_sha256=digest,
    )


def render_terminal_outbox_retention_prune(
    receipt: PursuitTerminalOutboxRetentionPruneReceipt,
) -> str:
    lines = [
        "## Pursuit 终态 Outbox retention prune",
        "",
        f"- 状态：`{receipt.status}`",
        f"- 回执：`{receipt.prune_id}` / `{receipt.prune_sha256}`",
        f"- Admission：`{receipt.admission_id}` / `{receipt.admission_sha256}`",
        f"- 候选/计划写点：{receipt.candidate_count}/{receipt.planned_write_count}",
    ]
    if receipt.status == "dry_run":
        lines.extend((
            "",
            "这是默认 dry-run：已认证 admission 与恢复计划，但未执行任何写点。",
            "只有 bypass 模式下显式传入 `--execute` 才会执行物理 prune。",
        ))
        return "\n".join(lines)
    lines.extend((
        f"- 已执行写点：{receipt.executed_write_count}",
        f"- 已删除行：{receipt.deleted_row_count}",
        f"- 永久抑制 tombstone：{receipt.tombstone_count}",
        "",
        "物理 prune 已与完成回执及防复活 tombstone 原子提交。",
    ))
    return "\n".join(lines)


def _receipt_sha256(
    receipt: PursuitTerminalOutboxRetentionPruneReceipt,
) -> str:
    return _digest_json(receipt.model_dump(
        mode="json",
        exclude={"prune_id", "prune_sha256"},
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
        raise ValueError("terminal outbox retention prune 时间必须包含时区。")
    return parsed.astimezone(UTC)


__all__ = [
    "PursuitTerminalOutboxRetentionPruneReceipt",
    "new_retention_prune_receipt",
    "render_terminal_outbox_retention_prune",
]
