"""Durable, dynamically revocable completion authority for one stable population."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_stage_completions import (
    EvolutionRevalidationStableStageCompletion,
)
from naumi_agent.evolution.stable_population_candidate_previews import (
    EvolutionStablePopulationAuthorityMaterial,
    EvolutionStablePopulationCandidatePreviewError,
    EvolutionStablePopulationCandidatePreviewService,
)

EVOLUTION_STABLE_POPULATION_COMPLETION_POLICY = (
    "evolution-stable-population-completion-v1"
)
_MAX_POPULATION = 10_000
_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_RECEIPT_RE = re.compile(r"^evstablepopcomplete_[0-9a-f]{24}$")
_SNAPSHOT_RE = re.compile(r"^relpopsnapshot_[0-9a-f]{24}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePopulationCompletionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-population-completion-v1"] = (
        EVOLUTION_STABLE_POPULATION_COMPLETION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evstablepopcomplete_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=_MAX_POPULATION)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_preview_source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_ids: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_POPULATION
    )
    intent_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_POPULATION)
    subject_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_POPULATION)
    completion_evidence_ids: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_POPULATION
    )
    completion_evidence_sha256: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_POPULATION
    )
    completion_source_set_sha256: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_POPULATION
    )
    population_snapshot_authority_at_issue: Literal[True] = True
    candidate_complete_at_issue: Literal[True] = True
    dynamic_revalidation_authority_at_issue: Literal[True] = True
    stable_population_completion_fact: Literal[True] = True
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Population Completion workspace 必须 canonical。")
        size = len(self.installation_member_ids)
        if not (
            size == self.population_denominator
            and size
            == len(self.intent_ids)
            == len(self.subject_ids)
            == len(self.completion_evidence_ids)
            == len(self.completion_evidence_sha256)
            == len(self.completion_source_set_sha256)
            and self.installation_member_ids
            == tuple(sorted(set(self.installation_member_ids)))
            and len(set(self.intent_ids)) == size
            and len(set(self.completion_evidence_ids)) == size
        ):
            raise ValueError("Stable Population Completion source projection 不一致。")
        _aware(self.completed_at)
        expected_source = _source_set_digest_from_receipt(self)
        if self.source_set_sha256 != expected_source:
            raise ValueError("Stable Population Completion source-set digest 不一致。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstablepopcomplete_{digest[:24]}"
        ):
            raise ValueError("Stable Population Completion identity 不一致。")
        return self


class EvolutionStablePopulationCompletionView(_StrictModel):
    receipt: EvolutionStablePopulationCompletionReceipt
    receipt_source_current: bool
    latest_for_snapshot: bool
    population_snapshot_current: bool
    population_snapshot_authority: bool
    candidate_complete: bool
    member_source_set_current: bool
    dynamic_revalidation_authority: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=16)
    stable_population_completion_authority: bool
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        expected = bool(
            self.receipt_source_current
            and self.latest_for_snapshot
            and self.population_snapshot_current
            and self.population_snapshot_authority
            and self.candidate_complete
            and self.member_source_set_current
            and self.dynamic_revalidation_authority
        )
        if not (
            self.stable_population_completion_authority is expected
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Population Completion View 投影不一致。")
        return self


class EvolutionStablePopulationCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePopulationCompletionStore:
    """Persist exact source-set receipts under one SQLite writer fence."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self, receipt_id: str
    ) -> EvolutionStablePopulationCompletionReceipt | None:
        normalized = _receipt_id(receipt_id)
        return await self._read("WHERE receipt_id = ?", (normalized,))

    async def latest(
        self, snapshot_id: str
    ) -> EvolutionStablePopulationCompletionReceipt | None:
        normalized = _snapshot_id(snapshot_id)
        return await self._read(
            "WHERE population_snapshot_id = ? ORDER BY rowid DESC LIMIT 1",
            (normalized,),
        )

    async def _read(self, where: str, parameters: tuple[object, ...]):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_stable_population_completions " + where,
                        parameters,
                    )
                ).fetchone()
            return None if row is None else _restore_receipt(row["receipt_json"])
        except EvolutionStablePopulationCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_source_invalid",
                "Stable Population Completion durable source 无效。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionStablePopulationCompletionReceipt,
    ) -> EvolutionStablePopulationCompletionReceipt:
        item = _validated_receipt(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_oversized",
                "Stable Population Completion Receipt 超过 16 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await self._require_exact_dependencies(db, item)
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_stable_population_completions "
                        "WHERE source_set_sha256 = ?",
                        (item.source_set_sha256,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_receipt(existing["receipt_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionStablePopulationCompletionError(
                            "stable_population_completion_source_conflict",
                            "同一 Stable Population source-set 已绑定不同 Completion。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_stable_population_completions "
                    "(receipt_id, receipt_sha256, source_set_sha256, "
                    "population_snapshot_id, receipt_json, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.source_set_sha256,
                        item.population_snapshot_id,
                        encoded,
                        item.completed_at,
                    ),
                )
                await db.commit()
        except EvolutionStablePopulationCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_store_error",
                "Stable Population Completion 无法持久化。",
            ) from exc
        return item

    async def _require_exact_dependencies(
        self,
        db: aiosqlite.Connection,
        item: EvolutionStablePopulationCompletionReceipt,
    ) -> None:
        table = await (
            await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                "name = 'evolution_revalidation_stable_stage_completions'"
            )
        ).fetchone()
        if table is None:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_dependency_missing",
                "Stable Population Completion 缺少 5f5r durable source。",
            )
        rows = await (
            await db.execute(
                "WITH latest AS (SELECT intent_id, MAX(rowid) AS latest_rowid "
                "FROM evolution_revalidation_stable_stage_completions "
                "GROUP BY intent_id), current AS (SELECT e.evidence_id, "
                "e.evidence_sha256, e.evidence_json FROM "
                "evolution_revalidation_stable_stage_completions e "
                "JOIN latest l ON e.rowid = l.latest_rowid) "
                "SELECT evidence_id, evidence_sha256, evidence_json FROM current "
                "WHERE json_extract(evidence_json, '$.population_snapshot_id') = ?",
                (item.population_snapshot_id,),
            )
        ).fetchall()
        restored: dict[str, EvolutionRevalidationStableStageCompletion] = {}
        for row in rows:
            try:
                evidence = (
                    EvolutionRevalidationStableStageCompletion.model_validate_json(
                        row["evidence_json"]
                    )
                )
            except (TypeError, ValueError) as exc:
                raise EvolutionStablePopulationCompletionError(
                    "stable_population_completion_dependency_corrupt",
                    "Stable Population Completion 引用的 5f5r source 已损坏。",
                ) from exc
            if (
                evidence.evidence_id != row["evidence_id"]
                or evidence.evidence_sha256 != row["evidence_sha256"]
            ):
                raise EvolutionStablePopulationCompletionError(
                    "stable_population_completion_dependency_corrupt",
                    "Stable Population Completion 引用的 5f5r source 已损坏。",
                )
            restored[evidence.evidence_id] = evidence
        expected = tuple(
            (
                member_id,
                intent_id,
                subject_id,
                evidence_id,
                evidence_sha,
                source_sha,
            )
            for member_id, intent_id, subject_id, evidence_id, evidence_sha, source_sha in zip(
                item.installation_member_ids,
                item.intent_ids,
                item.subject_ids,
                item.completion_evidence_ids,
                item.completion_evidence_sha256,
                item.completion_source_set_sha256,
                strict=True,
            )
        )
        actual = tuple(
            (
                evidence.installation_member_id,
                evidence.intent_id,
                evidence.subject_id,
                evidence.evidence_id,
                evidence.evidence_sha256,
                evidence.source_set_sha256,
            )
            for evidence in sorted(
                restored.values(), key=lambda value: value.installation_member_id
            )
        )
        if actual != expected:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_dependency_mismatch",
                "Stable Population Completion source-set 与 5f5r durable source 不一致。",
            )


class EvolutionStablePopulationCompletionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        preview_service: EvolutionStablePopulationCandidatePreviewService,
        store: EvolutionStablePopulationCompletionStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not isinstance(
            preview_service, EvolutionStablePopulationCandidatePreviewService
        ):
            raise TypeError("Stable Population Completion 需要 Candidate Preview Service。")
        if not isinstance(store, EvolutionStablePopulationCompletionStore):
            raise TypeError("Stable Population Completion 需要 Completion Store。")
        if preview_service.workspace_root != self.workspace_root:
            raise ValueError("Stable Population Completion workspace identity 不一致。")
        if preview_service.db_path != store.db_path:
            raise ValueError("Stable Population Completion 必须共用同一证据数据库。")
        self.preview_service = preview_service
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    async def complete(
        self,
        *,
        snapshot_id: str | None = None,
    ) -> EvolutionStablePopulationCompletionView:
        normalized = "" if snapshot_id is None else _snapshot_id(snapshot_id)
        lock = self._locks.setdefault(normalized or "__latest__", asyncio.Lock())
        async with lock:
            material = await self.preview_service.inspect_authority_material(
                snapshot_id=normalized or None,
                limit=1,
            )
            preview = material.preview
            if not preview.population_snapshot_id:
                raise EvolutionStablePopulationCompletionError(
                    "stable_population_completion_candidate_missing",
                    "当前没有可完成的 Stable Population candidate。",
                )
            if not preview.dynamic_revalidation_authority:
                raise EvolutionStablePopulationCompletionError(
                    "stable_population_completion_candidate_not_authoritative",
                    "Stable Population candidate 尚未通过完整动态重验。",
                )
            receipt = _build_receipt(material)
            stored = await self.store.record(receipt)
        return await self.inspect(receipt_id=stored.receipt_id)

    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStablePopulationCompletionView:
        receipt = await self.store.get(receipt_id)
        if receipt is None:
            raise EvolutionStablePopulationCompletionError(
                "stable_population_completion_missing",
                "指定的 Stable Population Completion 不存在。",
            )
        source_current = await self.store.get(receipt.receipt_id) == receipt
        latest = await self.store.latest(receipt.population_snapshot_id)
        latest_for_snapshot = latest == receipt
        reasons: set[str] = set()
        try:
            material = await self.preview_service.inspect_authority_material(
                snapshot_id=receipt.population_snapshot_id,
                limit=1,
            )
            preview = material.preview
            population_current = bool(
                preview.population_snapshot_id == receipt.population_snapshot_id
                and preview.population_snapshot_sha256
                == receipt.population_snapshot_sha256
                and preview.population_snapshot_sequence
                == receipt.population_snapshot_sequence
                and preview.population_denominator == receipt.population_denominator
            )
            population_authority = preview.population_snapshot_authority
            candidate_complete = preview.candidate_complete
            dynamic_authority = preview.dynamic_revalidation_authority
            member_source_current = (
                _source_set_digest_from_material(material) == receipt.source_set_sha256
            )
            reasons.update(preview.population_invalidation_reasons)
            for item in preview.items:
                reasons.update(item.dynamic_invalidation_reasons)
        except (
            EvolutionStablePopulationCandidatePreviewError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            population_current = False
            population_authority = False
            candidate_complete = False
            dynamic_authority = False
            member_source_current = False
            reasons.add(_safe_reason_code(getattr(exc, "code", "inspection_failed")))
        if not source_current:
            reasons.add("receipt_source_changed")
        if not latest_for_snapshot:
            reasons.add("completion_superseded")
        if not population_current:
            reasons.add("population_snapshot_changed")
        if not population_authority:
            reasons.add("population_snapshot_not_authoritative")
        if not candidate_complete:
            reasons.add("candidate_not_complete")
        if not member_source_current:
            reasons.add("member_source_set_changed")
        if not dynamic_authority:
            reasons.add("dynamic_revalidation_revoked")
        authority = bool(
            source_current
            and latest_for_snapshot
            and population_current
            and population_authority
            and candidate_complete
            and member_source_current
            and dynamic_authority
        )
        return EvolutionStablePopulationCompletionView(
            receipt=receipt,
            receipt_source_current=source_current,
            latest_for_snapshot=latest_for_snapshot,
            population_snapshot_current=population_current,
            population_snapshot_authority=population_authority,
            candidate_complete=candidate_complete,
            member_source_set_current=member_source_current,
            dynamic_revalidation_authority=dynamic_authority,
            invalidation_reasons=tuple(sorted(reasons))[:16],
            stable_population_completion_authority=authority,
        )


def render_stable_population_completion(
    view: EvolutionStablePopulationCompletionView,
) -> str:
    receipt = view.receipt
    state = "有效" if view.stable_population_completion_authority else "已撤权"
    reasons = "、".join(view.invalidation_reasons) or "无"
    return "\n".join(
        (
            "## Stable Population Completion",
            "",
            f"- 状态：**{state}**",
            f"- Receipt：`{receipt.receipt_id}`",
            "- Population："
            f"`{receipt.population_snapshot_id}`（{receipt.population_denominator} 个成员）",
            f"- Source set：`{receipt.source_set_sha256}`",
            f"- Completion authority：`{str(view.stable_population_completion_authority).lower()}`",
            f"- Stable rollout authority：`{str(view.stable_rollout_authority).lower()}`",
            f"- Promotion authority：`{str(view.promotion_authority).lower()}`",
            f"- 动态失效原因：{reasons}",
        )
    )


def _build_receipt(
    material: EvolutionStablePopulationAuthorityMaterial,
) -> EvolutionStablePopulationCompletionReceipt:
    preview = material.preview
    receipts = material.member_receipts
    if not receipts:
        raise EvolutionStablePopulationCompletionError(
            "stable_population_completion_candidate_missing",
            "Stable Population Completion 缺少成员 source。",
        )
    completed_at = max(_aware(item.assessed_at) for item in receipts).isoformat()
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_POPULATION_COMPLETION_POLICY,
        "source_set_sha256": _source_set_digest_from_material(material),
        "workspace_root": preview.workspace_root,
        "population_snapshot_id": preview.population_snapshot_id,
        "population_snapshot_sha256": preview.population_snapshot_sha256,
        "population_snapshot_sequence": preview.population_snapshot_sequence,
        "population_denominator": preview.population_denominator,
        "candidate_version": preview.candidate_version,
        "candidate_target": preview.candidate_target,
        "plan_id": preview.plan_id,
        "plan_sha256": preview.plan_sha256,
        "candidate_preview_source_set_sha256": preview.source_set_sha256,
        "installation_member_ids": [item.installation_member_id for item in receipts],
        "intent_ids": [item.intent_id for item in receipts],
        "subject_ids": [item.subject_id for item in receipts],
        "completion_evidence_ids": [item.evidence_id for item in receipts],
        "completion_evidence_sha256": [item.evidence_sha256 for item in receipts],
        "completion_source_set_sha256": [item.source_set_sha256 for item in receipts],
        "population_snapshot_authority_at_issue": True,
        "candidate_complete_at_issue": True,
        "dynamic_revalidation_authority_at_issue": True,
        "stable_population_completion_fact": True,
        "stable_rollout_authority": False,
        "promotion_authority": False,
        "completed_at": completed_at,
    }
    digest = _digest(core)
    return EvolutionStablePopulationCompletionReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstablepopcomplete_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _source_set_digest_from_material(
    material: EvolutionStablePopulationAuthorityMaterial,
) -> str:
    preview = material.preview
    return _digest(
        {
            "population_snapshot_id": preview.population_snapshot_id,
            "population_snapshot_sha256": preview.population_snapshot_sha256,
            "population_snapshot_sequence": preview.population_snapshot_sequence,
            "population_denominator": preview.population_denominator,
            "candidate_preview_source_set_sha256": preview.source_set_sha256,
            "members": [
                (
                    item.installation_member_id,
                    item.intent_id,
                    item.subject_id,
                    item.evidence_id,
                    item.evidence_sha256,
                    item.source_set_sha256,
                )
                for item in material.member_receipts
            ],
        }
    )


def _source_set_digest_from_receipt(
    receipt: EvolutionStablePopulationCompletionReceipt,
) -> str:
    return _digest(
        {
            "population_snapshot_id": receipt.population_snapshot_id,
            "population_snapshot_sha256": receipt.population_snapshot_sha256,
            "population_snapshot_sequence": receipt.population_snapshot_sequence,
            "population_denominator": receipt.population_denominator,
            "candidate_preview_source_set_sha256": (
                receipt.candidate_preview_source_set_sha256
            ),
            "members": list(
                zip(
                    receipt.installation_member_ids,
                    receipt.intent_ids,
                    receipt.subject_ids,
                    receipt.completion_evidence_ids,
                    receipt.completion_evidence_sha256,
                    receipt.completion_source_set_sha256,
                    strict=True,
                )
            ),
        }
    )


def _validated_receipt(value: object) -> EvolutionStablePopulationCompletionReceipt:
    if not isinstance(value, EvolutionStablePopulationCompletionReceipt):
        raise TypeError("Stable Population Completion Receipt 类型无效。")
    return EvolutionStablePopulationCompletionReceipt.model_validate_json(
        value.model_dump_json()
    )


def _restore_receipt(encoded: str) -> EvolutionStablePopulationCompletionReceipt:
    if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
        raise EvolutionStablePopulationCompletionError(
            "stable_population_completion_source_oversized",
            "Stable Population Completion durable source 超过 16 MiB。",
        )
    try:
        return EvolutionStablePopulationCompletionReceipt.model_validate_json(encoded)
    except ValueError as exc:
        raise EvolutionStablePopulationCompletionError(
            "stable_population_completion_source_corrupt",
            "Stable Population Completion durable source 已损坏。",
        ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_stable_population_completions (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL,
            source_set_sha256 TEXT NOT NULL UNIQUE,
            population_snapshot_id TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_stable_population_completions_snapshot "
        "ON evolution_stable_population_completions "
        "(population_snapshot_id, completed_at)"
    )


def _receipt_id(value: str) -> str:
    normalized = str(value).strip()
    if not _RECEIPT_RE.fullmatch(normalized):
        raise ValueError("receipt_id 必须是 evstablepopcomplete_ 加 24 位小写十六进制。")
    return normalized


def _snapshot_id(value: str) -> str:
    normalized = str(value).strip()
    if not _SNAPSHOT_RE.fullmatch(normalized):
        raise ValueError("snapshot_id 必须是 relpopsnapshot_ 加 24 位小写十六进制。")
    return normalized


def _safe_reason_code(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().lower())
    return normalized[:128] or "inspection_failed"


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_POPULATION_COMPLETION_POLICY",
    "EvolutionStablePopulationCompletionError",
    "EvolutionStablePopulationCompletionReceipt",
    "EvolutionStablePopulationCompletionService",
    "EvolutionStablePopulationCompletionStore",
    "EvolutionStablePopulationCompletionView",
    "render_stable_population_completion",
]
