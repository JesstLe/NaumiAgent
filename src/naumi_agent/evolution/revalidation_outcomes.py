"""Durable Revalidation Outcome and atomic invalidation of old promotion evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestService,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionHarnessRevalidationRunner,
    EvolutionRevalidationValidationReceipt,
    EvolutionRevalidationValidationStatus,
    EvolutionRevalidationValidationStore,
)

EVOLUTION_REVALIDATION_OUTCOME_POLICY = "evolution-revalidation-outcome-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOutcomeStatus(StrEnum):
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"


class EvolutionInvalidatedAuthority(_StrictModel):
    order: int = Field(ge=1, le=80)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    authority_id: str = Field(min_length=1, max_length=256)
    authority_sha256: str = Field(pattern=_SHA256_RE)
    disposition: Literal["superseded_for_promotion"] = "superseded_for_promotion"
    reason: Literal["fresh_revalidation_evidence_issued"] = (
        "fresh_revalidation_evidence_issued"
    )


class EvolutionRevalidationOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-outcome-v1"] = (
        EVOLUTION_REVALIDATION_OUTCOME_POLICY
    )
    outcome_id: str = Field(pattern=r"^evrevalout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    validation_receipt_id: str = Field(pattern=r"^evrevalidate_[0-9a-f]{24}$")
    validation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    harness_plan_sha256: str = Field(pattern=_SHA256_RE)
    status: EvolutionRevalidationOutcomeStatus
    invalidated_authorities: tuple[EvolutionInvalidatedAuthority, ...] = Field(
        min_length=6,
        max_length=80,
    )
    old_evidence_invalidated: Literal[True] = True
    final_evaluation_reissue_required: Literal[True] = True
    approval_reaggregation_required: Literal[True] = True
    professional_signatures_reissue_required: Literal[True] = True
    rollout_candidate_eligible_at_issue: bool
    promotion_authority: Literal[False] = False
    merge_executed: Literal[False] = False
    push_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _outcome_is_exact(self) -> Self:
        if tuple(item.order for item in self.invalidated_authorities) != tuple(
            range(1, len(self.invalidated_authorities) + 1)
        ):
            raise ValueError("Invalidated authorities 顺序不连续。")
        keys = tuple(
            (item.kind, item.authority_id) for item in self.invalidated_authorities
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Invalidated authorities 不得重复。")
        expected_eligible = self.status is EvolutionRevalidationOutcomeStatus.VALIDATED
        if self.rollout_candidate_eligible_at_issue is not expected_eligible:
            raise ValueError("Revalidation Outcome rollout eligibility 投影不一致。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Revalidation Outcome created_at 必须包含 UTC offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        )
        if not hmac.compare_digest(self.outcome_sha256, digest):
            raise ValueError("Revalidation Outcome 摘要不一致。")
        if self.outcome_id != f"evrevalout_{digest[:24]}":
            raise ValueError("Revalidation Outcome identity 不一致。")
        return self


class EvolutionRevalidationOutcomeView(_StrictModel):
    outcome: EvolutionRevalidationOutcome
    source_current: bool
    current_status: Literal["validated", "validation_failed", "stale"]
    rollout_candidate_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected_status = (
            self.outcome.status.value if self.source_current else "stale"
        )
        if self.current_status != expected_status:
            raise ValueError("Revalidation Outcome current status 投影不一致。")
        if self.rollout_candidate_eligible is not (
            self.source_current
            and self.outcome.status is EvolutionRevalidationOutcomeStatus.VALIDATED
        ):
            raise ValueError("Revalidation Outcome rollout gate 投影不一致。")
        return self


class EvolutionRevalidationOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOutcomeStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, item: EvolutionRevalidationOutcome) -> EvolutionRevalidationOutcome:
        encoded = item.model_dump_json()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            terminal = await (
                await db.execute(
                    "SELECT outcome_json FROM evolution_revalidation_outcomes "
                    "WHERE validation_receipt_id = ?",
                    (item.validation_receipt_id,),
                )
            ).fetchone()
            if terminal is not None:
                restored = EvolutionRevalidationOutcome.model_validate_json(
                    terminal["outcome_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationOutcomeError(
                        "revalidation_outcome_conflict",
                        "同一 Validation Receipt 已绑定不同 Outcome。",
                    )
                return restored
            validation_rows = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_validations "
                    "WHERE request_id = ? AND receipt_json != ''",
                    (item.request_id,),
                )
            ).fetchall()
            receipts = tuple(
                EvolutionRevalidationValidationReceipt.model_validate_json(
                    row["receipt_json"]
                )
                for row in validation_rows
            )
            receipt = next(
                (
                    candidate
                    for candidate in receipts
                    if candidate.receipt_id == item.validation_receipt_id
                ),
                None,
            )
            if receipt is None:
                await db.rollback()
                raise EvolutionRevalidationOutcomeError(
                    "revalidation_outcome_validation_missing",
                    "持久化 Validation Receipt 不存在，拒绝签发 Outcome。",
                )
            if not (
                receipt.receipt_id == item.validation_receipt_id
                and receipt.receipt_sha256 == item.validation_receipt_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationOutcomeError(
                    "revalidation_outcome_validation_mismatch",
                    "Validation Receipt 与 Outcome 输入不一致。",
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_outcomes "
                "(outcome_id, outcome_sha256, request_id, validation_receipt_id, "
                "status, outcome_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.outcome_id,
                    item.outcome_sha256,
                    item.request_id,
                    item.validation_receipt_id,
                    item.status.value,
                    encoded,
                    item.created_at,
                ),
            )
            for authority in item.invalidated_authorities:
                await db.execute(
                    "INSERT INTO evolution_promotion_authority_invalidations "
                    "(authority_kind, authority_id, authority_sha256, outcome_id, "
                    "outcome_sha256, disposition, reason, invalidated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        authority.kind,
                        authority.authority_id,
                        authority.authority_sha256,
                        item.outcome_id,
                        item.outcome_sha256,
                        authority.disposition,
                        authority.reason,
                        item.created_at,
                    ),
                )
            await db.commit()
        return item

    async def get_by_request(self, request_id: str) -> EvolutionRevalidationOutcome | None:
        if re.fullmatch(r"evrevalidation_[0-9a-f]{24}", str(request_id)) is None:
            raise ValueError("Revalidation Request ID 格式无效。")
        return await self._read("request_id", request_id, latest=True)

    async def get_by_validation_receipt(
        self, receipt_id: str
    ) -> EvolutionRevalidationOutcome | None:
        if re.fullmatch(r"evrevalidate_[0-9a-f]{24}", str(receipt_id)) is None:
            raise ValueError("Revalidation Validation Receipt ID 格式无效。")
        return await self._read("validation_receipt_id", receipt_id)

    async def get(self, outcome_id: str) -> EvolutionRevalidationOutcome | None:
        if re.fullmatch(r"evrevalout_[0-9a-f]{24}", str(outcome_id)) is None:
            raise ValueError("Revalidation Outcome ID 格式无效。")
        return await self._read("outcome_id", outcome_id)

    async def invalidation(
        self, *, kind: str, authority_id: str
    ) -> EvolutionInvalidatedAuthority | None:
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT authority_kind, authority_id, authority_sha256 "
                    "FROM evolution_promotion_authority_invalidations "
                    "WHERE authority_kind = ? AND authority_id = ? "
                    "ORDER BY invalidated_at DESC LIMIT 1",
                    (kind, authority_id),
                )
            ).fetchone()
        if row is None:
            return None
        return EvolutionInvalidatedAuthority(
            order=1,
            kind=row["authority_kind"],
            authority_id=row["authority_id"],
            authority_sha256=row["authority_sha256"],
        )

    async def _read(
        self, field: str, value: str, *, latest: bool = False
    ) -> EvolutionRevalidationOutcome | None:
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            suffix = " ORDER BY created_at DESC LIMIT 1" if latest else ""
            row = await (
                await db.execute(
                    "SELECT outcome_json FROM evolution_revalidation_outcomes "
                    f"WHERE {field} = ?{suffix}",
                    (value,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationOutcome.model_validate_json(row["outcome_json"])
        )


class EvolutionRevalidationOutcomeService:
    def __init__(
        self,
        *,
        request_service: EvolutionRevalidationRequestService,
        validation_store: EvolutionRevalidationValidationStore,
        package_input_store: EvolutionPromotionPackageInputStore,
        harness: EvolutionHarnessRevalidationRunner,
        store: EvolutionRevalidationOutcomeStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._request_service = request_service
        self._validation_store = validation_store
        self._package_input_store = package_input_store
        self._harness = harness
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(
        self, *, workspace_root: str | Path, request_id: str
    ) -> EvolutionRevalidationOutcomeView:
        request_view, receipt, package = await self._sources(workspace_root, request_id)
        existing = await self._store.get_by_validation_receipt(receipt.receipt_id)
        if existing is not None:
            return await self.inspect(workspace_root=workspace_root, outcome_id=existing.outcome_id)
        source_current = await self._source_current(request_view, receipt, package)
        if not source_current:
            raise EvolutionRevalidationOutcomeError(
                "revalidation_outcome_source_stale",
                "Revalidation 验证来源已漂移，拒绝首次签发 Outcome。",
            )
        authorities = _invalidated_authorities(request_view.request, package, receipt)
        status = (
            EvolutionRevalidationOutcomeStatus.VALIDATED
            if receipt.status is EvolutionRevalidationValidationStatus.PASSED
            else EvolutionRevalidationOutcomeStatus.VALIDATION_FAILED
        )
        now = self._clock()
        if now.utcoffset() is None:
            raise EvolutionRevalidationOutcomeError(
                "revalidation_outcome_clock_invalid", "Outcome 时钟必须包含 UTC offset。"
            )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_OUTCOME_POLICY,
            "request_id": request_view.request.request_id,
            "request_sha256": request_view.request.request_sha256,
            "validation_receipt_id": receipt.receipt_id,
            "validation_receipt_sha256": receipt.receipt_sha256,
            "target_head": receipt.target_head,
            "target_tree_sha256": receipt.target_tree_sha256,
            "profile_sha256": receipt.profile_sha256,
            "harness_plan_sha256": receipt.harness_plan_sha256,
            "status": status.value,
            "invalidated_authorities": [item.model_dump(mode="json") for item in authorities],
            "old_evidence_invalidated": True,
            "final_evaluation_reissue_required": True,
            "approval_reaggregation_required": True,
            "professional_signatures_reissue_required": True,
            "rollout_candidate_eligible_at_issue": (
                status is EvolutionRevalidationOutcomeStatus.VALIDATED
            ),
            "promotion_authority": False,
            "merge_executed": False,
            "push_executed": False,
            "publish_executed": False,
            "created_at": now.isoformat(),
        }
        digest = _sha256_payload(payload)
        outcome = EvolutionRevalidationOutcome.model_validate(
            {**payload, "outcome_id": f"evrevalout_{digest[:24]}", "outcome_sha256": digest}
        )
        await self._store.record(outcome)
        return EvolutionRevalidationOutcomeView(
            outcome=outcome,
            source_current=True,
            current_status=outcome.status.value,
            rollout_candidate_eligible=outcome.rollout_candidate_eligible_at_issue,
        )

    async def inspect(
        self, *, workspace_root: str | Path, outcome_id: str
    ) -> EvolutionRevalidationOutcomeView:
        outcome = await self._store.get(outcome_id)
        if outcome is None:
            raise EvolutionRevalidationOutcomeError(
                "revalidation_outcome_not_found", "Revalidation Outcome 不存在。"
            )
        request_view, receipt, package = await self._sources(
            workspace_root, outcome.request_id
        )
        source_current = bool(
            receipt.receipt_id == outcome.validation_receipt_id
            and receipt.receipt_sha256 == outcome.validation_receipt_sha256
            and await self._source_current(request_view, receipt, package)
        )
        return EvolutionRevalidationOutcomeView(
            outcome=outcome,
            source_current=source_current,
            current_status=outcome.status.value if source_current else "stale",
            rollout_candidate_eligible=bool(
                source_current
                and outcome.status is EvolutionRevalidationOutcomeStatus.VALIDATED
            ),
        )

    async def _sources(self, workspace_root: str | Path, request_id: str):
        request_view = await self._request_service.inspect(
            workspace_root=workspace_root, request_id=request_id
        )
        receipt = await self._validation_store.get_by_request(request_id)
        if receipt is None:
            raise EvolutionRevalidationOutcomeError(
                "revalidation_outcome_validation_missing",
                "Revalidation 尚无持久化 Harness validation 终态回执。",
            )
        package_view = await self._package_input_store.get(
            request_view.request.promotion_input_id
        )
        if package_view is None:
            raise EvolutionRevalidationOutcomeError(
                "revalidation_outcome_package_missing", "Promotion Package Input 不存在。"
            )
        return request_view, receipt, package_view.package_input

    async def _source_current(self, request_view, receipt, package) -> bool:
        if not (
            request_view.execution_eligible
            and request_view.request.request_sha256 == receipt.request_sha256
            and request_view.current_target_head == receipt.target_head
        ):
            return False
        plan = await self._harness.prepare_evolution_revalidation(
            changed_paths=tuple(item.path for item in package.patch.files)
        )
        return bool(
            plan.profile_sha256 == receipt.profile_sha256
            and plan.plan_sha256 == receipt.harness_plan_sha256
        )


def _invalidated_authorities(request, package, receipt):
    raw = [
        (item.kind.value, item.authority_id, item.authority_sha256)
        for item in package.evidence_refs
    ]
    raw.extend(
        [
            ("promotion_input", request.promotion_input_id, request.promotion_input_sha256),
            ("promotion_package", request.package_id, request.package_sha256),
            ("approval_requirement", request.requirement_id, request.requirement_sha256),
            ("approval_decision", request.decision_id, request.decision_sha256),
        ]
    )
    unique = []
    seen = set()
    for kind, authority_id, authority_sha256 in raw:
        key = (kind, authority_id)
        if key in seen or authority_id == receipt.receipt_id:
            continue
        seen.add(key)
        unique.append((kind, authority_id, authority_sha256))
    return tuple(
        EvolutionInvalidatedAuthority(
            order=index,
            kind=kind,
            authority_id=authority_id,
            authority_sha256=authority_sha256,
        )
        for index, (kind, authority_id, authority_sha256) in enumerate(unique, 1)
    )


def render_evolution_revalidation_outcome(view: EvolutionRevalidationOutcomeView) -> str:
    item = view.outcome
    return "\n".join(
        [
            f"# Evolution Revalidation Outcome `{item.outcome_id}`",
            "",
            f"- Current status：`{view.current_status}`",
            f"- Validation Receipt：`{item.validation_receipt_id}`",
            f"- Invalidated old authorities：{len(item.invalidated_authorities)}",
            "- Old evidence invalidated：`true`",
            "- Final evaluation / approval / signatures：必须重新签发与聚合",
            f"- Rollout candidate eligible：`{str(view.rollout_candidate_eligible).lower()}`",
            "- Promotion authority：`false`",
        ]
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_outcomes ("
        "outcome_id TEXT PRIMARY KEY, outcome_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL, validation_receipt_id TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL, outcome_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_promotion_authority_invalidations ("
        "authority_kind TEXT NOT NULL, authority_id TEXT NOT NULL, "
        "authority_sha256 TEXT NOT NULL, outcome_id TEXT NOT NULL, "
        "outcome_sha256 TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL, "
        "invalidated_at TEXT NOT NULL, "
        "PRIMARY KEY(authority_kind, authority_id, outcome_id))"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
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
    "EVOLUTION_REVALIDATION_OUTCOME_POLICY",
    "EvolutionInvalidatedAuthority",
    "EvolutionRevalidationOutcome",
    "EvolutionRevalidationOutcomeError",
    "EvolutionRevalidationOutcomeService",
    "EvolutionRevalidationOutcomeStatus",
    "EvolutionRevalidationOutcomeStore",
    "EvolutionRevalidationOutcomeView",
    "render_evolution_revalidation_outcome",
]
