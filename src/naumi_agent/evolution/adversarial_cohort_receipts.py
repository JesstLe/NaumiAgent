"""Durable immutable storage for completed adversarial cohort receipts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import aiosqlite

from naumi_agent.evolution.adversarial_cohort import (
    EvolutionAdversarialCohortReceipt,
)


class EvolutionAdversarialCohortReceiptStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionAdversarialCohortReceiptStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionAdversarialCohortReceipt,
    ) -> EvolutionAdversarialCohortReceipt:
        artifact = EvolutionAdversarialCohortReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_adversarial_cohort_receipts "
                        "WHERE receipt_id = ?",
                        (artifact.receipt_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != artifact:
                        await db.rollback()
                        raise EvolutionAdversarialCohortReceiptStoreError(
                            "adversarial_cohort_receipt_conflict",
                            "同一 Adversarial Cohort Receipt 不可覆盖为不同内容。",
                        )
                    await db.rollback()
                    return restored
                lane_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_adversarial_cohort_receipts "
                        "WHERE request_id = ? AND request_sha256 = ? "
                        "AND platform = ? AND phase = ?",
                        (
                            artifact.request_id,
                            artifact.request_sha256,
                            artifact.platform,
                            artifact.phase,
                        ),
                    )
                ).fetchone()
                if lane_row is not None:
                    _from_row(lane_row)
                    await db.rollback()
                    raise EvolutionAdversarialCohortReceiptStoreError(
                        "adversarial_cohort_receipt_conflict",
                        "同一 Request、平台和 phase 不可绑定不同完成回执。",
                    )
                await db.execute(
                    "INSERT INTO evolution_adversarial_cohort_receipts "
                    "(receipt_id, receipt_sha256, request_id, request_sha256, "
                    "platform, phase, receipt_json, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact.receipt_id,
                        artifact.receipt_sha256,
                        artifact.request_id,
                        artifact.request_sha256,
                        artifact.platform,
                        artifact.phase,
                        _json_dumps(artifact.model_dump(mode="json")),
                        artifact.completed_at,
                    ),
                )
                await db.commit()
        except EvolutionAdversarialCohortReceiptStoreError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialCohortReceiptStoreError(
                "adversarial_cohort_receipt_store_error",
                "Adversarial Cohort Receipt 无法持久化。",
            ) from exc
        restored = await self.get(artifact.receipt_id)
        assert restored is not None
        return restored

    async def get(
        self,
        receipt_id: str,
    ) -> EvolutionAdversarialCohortReceipt | None:
        if not isinstance(receipt_id, str) or re.fullmatch(
            r"evadvcohort_[0-9a-f]{24}", receipt_id
        ) is None:
            raise ValueError("Adversarial Cohort receipt_id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_adversarial_cohort_receipts "
                        "WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialCohortReceiptStoreError(
                "adversarial_cohort_receipt_store_corrupt",
                "Adversarial Cohort Receipt 损坏或无法读取。",
            ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_adversarial_cohort_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            platform TEXT NOT NULL,
            phase TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_adversarial_cohort_lane "
        "ON evolution_adversarial_cohort_receipts"
        "(request_id, request_sha256, platform, phase)"
    )


def _from_row(row: aiosqlite.Row) -> EvolutionAdversarialCohortReceipt:
    artifact = EvolutionAdversarialCohortReceipt.model_validate_json(
        str(row["receipt_json"])
    )
    if not (
        row["receipt_id"] == artifact.receipt_id
        and row["receipt_sha256"] == artifact.receipt_sha256
        and row["request_id"] == artifact.request_id
        and row["request_sha256"] == artifact.request_sha256
        and row["platform"] == artifact.platform
        and row["phase"] == artifact.phase
        and row["completed_at"] == artifact.completed_at
    ):
        raise ValueError("Adversarial Cohort Receipt Store row 与 payload 不一致。")
    return artifact


def _json_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "EvolutionAdversarialCohortReceiptStore",
    "EvolutionAdversarialCohortReceiptStoreError",
]
