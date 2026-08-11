"""Review eligibility over current passing stable Population observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
    EvolutionStablePromotionObservationContractError,
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_promotion_population_observation_assessments import (
    EvolutionStablePromotionPopulationObservationAssessment,
    EvolutionStablePromotionPopulationObservationAssessmentError,
    EvolutionStablePromotionPopulationObservationAssessmentView,
    EvolutionStablePromotionPopulationObservationStatus,
)

EVOLUTION_STABLE_PROMOTION_OUTCOME_ELIGIBILITY_POLICY = (
    "evolution-stable-promotion-outcome-eligibility-v1"
)
_ELIGIBILITY_RE = re.compile(r"^evstablepromeligible_[0-9a-f]{24}$")
_ASSESSMENT_RE = re.compile(r"^evstableprompopobserve_[0-9a-f]{24}$")
_MAX_ARTIFACT_BYTES = 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionPopulationObservationInspectionPort(Protocol):
    async def inspect(
        self, *, assessment_id: str
    ) -> EvolutionStablePromotionPopulationObservationAssessmentView: ...


class EvolutionStablePromotionOutcomeEligibility(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-outcome-eligibility-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OUTCOME_ELIGIBILITY_POLICY
    eligibility_id: str = Field(pattern=r"^evstablepromeligible_[0-9a-f]{24}$")
    eligibility_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_assessment_id: str = Field(
        pattern=r"^evstableprompopobserve_[0-9a-f]{24}$"
    )
    population_assessment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_assessment_source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_finalization_receipt_id: str = Field(
        pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$"
    )
    population_finalization_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    passing_count: int = Field(ge=1, le=10_000)
    assessment_coverage_bps: Literal[10000] = 10_000
    duration_coverage_bps: Literal[10000] = 10_000
    workbench_session_id: str = Field(min_length=1, max_length=128)
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    approval_decision_id: str = Field(pattern=r"^evreapprovaldecision_[0-9a-f]{24}$")
    approval_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_at: str = Field(min_length=1, max_length=100)
    population_sustained_health_verified: Literal[True] = True
    promotion_outcome_review_ready: Literal[True] = True
    independent_outcome_decision_required: Literal[True] = True
    promoted: Literal[False] = False
    superseded: Literal[False] = False
    outcome_decision_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Promotion Outcome Eligibility workspace 必须 canonical。")
        _aware(self.eligible_at)
        if self.passing_count != self.population_denominator:
            raise ValueError("Promotion Outcome Eligibility 必须覆盖 exact Population。")
        expected_source = _source(
            self.contract_id,
            self.contract_sha256,
            self.population_assessment_id,
            self.population_assessment_sha256,
        )
        if not hmac.compare_digest(self.source_sha256, expected_source):
            raise ValueError("Promotion Outcome Eligibility source identity 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"eligibility_id", "eligibility_sha256"})
        )
        if not (
            hmac.compare_digest(self.eligibility_sha256, digest)
            and self.eligibility_id == f"evstablepromeligible_{digest[:24]}"
        ):
            raise ValueError("Promotion Outcome Eligibility content identity 不一致。")
        return self


class EvolutionStablePromotionOutcomeEligibilityView(_StrictModel):
    eligibility: EvolutionStablePromotionOutcomeEligibility
    current_eligibility: EvolutionStablePromotionOutcomeEligibility | None
    durable_eligibility_valid: bool
    latest_eligibility_current: bool
    observation_contract_authority: bool
    population_assessment_authority: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    outcome_review_ready_authority: bool
    promoted: Literal[False] = False
    outcome_decision_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_eligibility_valid
            and self.latest_eligibility_current
            and self.observation_contract_authority
            and self.population_assessment_authority
            and self.current_eligibility is not None
        )
        if not (
            self.outcome_review_ready_authority is expected
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Promotion Outcome Eligibility authority projection 不一致。")
        return self


class EvolutionStablePromotionOutcomeEligibilityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionOutcomeEligibilityStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        contract_store: EvolutionStablePromotionObservationContractStore,
        population_assessment_store: object,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        assessment_path = Path(getattr(population_assessment_store, "db_path")).resolve()
        if self.db_path != contract_store.db_path or self.db_path != assessment_path:
            raise ValueError("Promotion Outcome Eligibility sources 必须共享 session SQLite。")
        self.contract_store = contract_store
        self.population_assessment_store = population_assessment_store

    async def get(
        self, eligibility_id: str
    ) -> EvolutionStablePromotionOutcomeEligibility | None:
        identifier = _eligibility_id(eligibility_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_eligibilities "
                        "WHERE eligibility_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_eligibility(row)
        except EvolutionStablePromotionOutcomeEligibilityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_eligibility_store_corrupt",
                "Promotion Outcome Eligibility 损坏或无法读取。",
            ) from exc

    async def latest(
        self, *, contract_id: str
    ) -> EvolutionStablePromotionOutcomeEligibility | None:
        identifier = str(contract_id or "").strip()
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_eligibilities "
                        "WHERE contract_id = ? ORDER BY rowid DESC LIMIT 1",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_eligibility(row)
        except EvolutionStablePromotionOutcomeEligibilityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_eligibility_store_corrupt",
                "Promotion Outcome Eligibility 损坏或无法读取。",
            ) from exc

    async def record(
        self, eligibility: EvolutionStablePromotionOutcomeEligibility
    ) -> EvolutionStablePromotionOutcomeEligibility:
        item = EvolutionStablePromotionOutcomeEligibility.model_validate(eligibility)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_eligibility_oversized",
                "Promotion Outcome Eligibility 超过 1 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_exact_sources(db, item)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_eligibilities "
                        "WHERE source_sha256 = ?",
                        (item.source_sha256,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _row_eligibility(row)
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionOutcomeEligibilityError(
                        "stable_promotion_outcome_eligibility_source_conflict",
                        "同一 Eligibility source 已绑定不同内容。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_outcome_eligibilities "
                    "(eligibility_id, eligibility_sha256, source_sha256, contract_id, "
                    "population_assessment_id, eligibility_json, eligible_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.eligibility_id,
                        item.eligibility_sha256,
                        item.source_sha256,
                        item.contract_id,
                        item.population_assessment_id,
                        encoded,
                        item.eligible_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionOutcomeEligibilityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_eligibility_store_failed",
                "Promotion Outcome Eligibility 无法持久化。",
            ) from exc


class EvolutionStablePromotionOutcomeEligibilityService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionStablePromotionObservationContractStore,
        contract_service: EvolutionStablePromotionObservationContractService,
        population_assessment_service: (
            EvolutionStablePromotionPopulationObservationInspectionPort
        ),
        store: EvolutionStablePromotionOutcomeEligibilityStore,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        assessment_store = getattr(population_assessment_service, "store", None)
        if not (
            store.contract_store is contract_store
            and store.population_assessment_store is assessment_store
            and contract_service.store is contract_store
        ):
            raise ValueError("Promotion Outcome Eligibility Service dependency 不一致。")
        self.workspace_root = root
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.population_assessment_service = population_assessment_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self, *, population_assessment_id: str
    ) -> EvolutionStablePromotionOutcomeEligibilityView:
        assessment_id = _assessment_id(population_assessment_id)
        lock = self._locks.setdefault(assessment_id, asyncio.Lock())
        async with lock:
            contract, assessment = await self._authoritative_sources(assessment_id)
            eligibility = build_stable_promotion_outcome_eligibility(
                workspace_root=self.workspace_root,
                contract=contract,
                assessment=assessment,
            )
            stored = await self.store.record(eligibility)
        return await self.inspect(eligibility_id=stored.eligibility_id)

    async def inspect(
        self, *, eligibility_id: str
    ) -> EvolutionStablePromotionOutcomeEligibilityView:
        eligibility = await self.store.get(eligibility_id)
        if eligibility is None:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_eligibility_missing",
                "Promotion Outcome Eligibility 不存在。",
            )
        durable = await self.store.get(eligibility.eligibility_id) == eligibility
        latest = await self.store.latest(contract_id=eligibility.contract_id) == eligibility
        contract_current = assessment_current = False
        current = None
        reasons: list[str] = []
        try:
            contract, assessment = await self._authoritative_sources(
                eligibility.population_assessment_id
            )
            contract_current = bool(
                contract.contract_id == eligibility.contract_id
                and contract.contract_sha256 == eligibility.contract_sha256
            )
            assessment_current = bool(
                assessment.assessment_id == eligibility.population_assessment_id
                and assessment.assessment_sha256
                == eligibility.population_assessment_sha256
            )
            if durable and latest and contract_current and assessment_current:
                current = eligibility
        except _INSPECTION_ERRORS:
            reasons.append("authoritative_source_unavailable")
        checks = {
            "eligibility_source_changed": durable,
            "newer_eligibility_exists": latest,
            "observation_contract_stale": contract_current,
            "population_assessment_stale": assessment_current,
        }
        reasons.extend(reason for reason, passed in checks.items() if not passed)
        authority = bool(all(checks.values()) and current is not None)
        return EvolutionStablePromotionOutcomeEligibilityView(
            eligibility=eligibility,
            current_eligibility=current,
            durable_eligibility_valid=durable,
            latest_eligibility_current=latest,
            observation_contract_authority=contract_current,
            population_assessment_authority=assessment_current,
            invalidation_reasons=tuple(sorted(set(reasons))),
            outcome_review_ready_authority=authority,
        )

    async def _authoritative_sources(
        self, assessment_id: str
    ) -> tuple[
        EvolutionStablePromotionObservationContract,
        EvolutionStablePromotionPopulationObservationAssessment,
    ]:
        assessment_view = await self.population_assessment_service.inspect(
            assessment_id=assessment_id
        )
        assessment = assessment_view.current_assessment
        if not (
            assessment_view.population_long_term_observation_authority
            and assessment is not None
            and assessment.status
            is EvolutionStablePromotionPopulationObservationStatus.PASSING
            and assessment.passing_count == assessment.population_denominator
            and assessment.assessment_coverage_bps == 10_000
            and assessment.duration_coverage_bps == 10_000
            and not any(
                (
                    assessment.breached_count,
                    assessment.censored_count,
                    assessment.insufficient_count,
                    assessment.missing_count,
                )
            )
        ):
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_population_not_passing",
                "只有 current exact Population passing 才能进入 Outcome 审批。",
            )
        contract = await self.contract_store.get_by_finalization(
            assessment.population_finalization_receipt_id
        )
        if contract is None:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_contract_missing",
                "Population Assessment 缺少 Observation Contract。",
            )
        contract_view = await self.contract_service.inspect(contract=contract)
        if not contract_view.observation_contract_authority:
            raise EvolutionStablePromotionOutcomeEligibilityError(
                "stable_promotion_outcome_contract_stale",
                "Observation Contract authority 已失效。",
            )
        return contract, assessment


def build_stable_promotion_outcome_eligibility(
    *,
    workspace_root: str | Path,
    contract: EvolutionStablePromotionObservationContract,
    assessment: EvolutionStablePromotionPopulationObservationAssessment,
) -> EvolutionStablePromotionOutcomeEligibility:
    root = Path(workspace_root).expanduser().resolve(strict=True)
    contract_item = EvolutionStablePromotionObservationContract.model_validate(contract)
    assessment_item = EvolutionStablePromotionPopulationObservationAssessment.model_validate(
        assessment
    )
    if not (
        contract_item.workspace_root == str(root)
        and contract_item.contract_id == assessment_item.contract_id
        and contract_item.contract_sha256 == assessment_item.contract_sha256
        and assessment_item.status
        is EvolutionStablePromotionPopulationObservationStatus.PASSING
        and assessment_item.passing_count == assessment_item.population_denominator
        and assessment_item.assessment_coverage_bps == 10_000
        and assessment_item.duration_coverage_bps == 10_000
        and not any(
            (
                assessment_item.breached_count,
                assessment_item.censored_count,
                assessment_item.insufficient_count,
                assessment_item.missing_count,
            )
        )
    ):
        raise EvolutionStablePromotionOutcomeEligibilityError(
            "stable_promotion_outcome_eligibility_lineage_mismatch",
            "Contract 与 passing Population Assessment lineage 不一致。",
        )
    source = _source(
        contract_item.contract_id,
        contract_item.contract_sha256,
        assessment_item.assessment_id,
        assessment_item.assessment_sha256,
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OUTCOME_ELIGIBILITY_POLICY,
        "source_sha256": source,
        "workspace_root": str(root),
        "contract_id": contract_item.contract_id,
        "contract_sha256": contract_item.contract_sha256,
        "population_assessment_id": assessment_item.assessment_id,
        "population_assessment_sha256": assessment_item.assessment_sha256,
        "population_assessment_source_set_sha256": assessment_item.source_set_sha256,
        "population_finalization_receipt_id": (
            assessment_item.population_finalization_receipt_id
        ),
        "population_finalization_receipt_sha256": (
            assessment_item.population_finalization_receipt_sha256
        ),
        "population_snapshot_id": assessment_item.population_snapshot_id,
        "population_snapshot_sha256": assessment_item.population_snapshot_sha256,
        "population_snapshot_sequence": assessment_item.population_snapshot_sequence,
        "population_denominator": assessment_item.population_denominator,
        "passing_count": assessment_item.passing_count,
        "assessment_coverage_bps": 10_000,
        "duration_coverage_bps": 10_000,
        "workbench_session_id": contract_item.workbench_session_id,
        "workbench_proposal_id": contract_item.workbench_proposal_id,
        "proposal_id": contract_item.proposal_id,
        "candidate_id": contract_item.candidate_id,
        "candidate_revision": contract_item.candidate_revision,
        "candidate_sha256": contract_item.candidate_sha256,
        "candidate_version": contract_item.candidate_version,
        "candidate_target": contract_item.candidate_target,
        "approval_decision_id": contract_item.approval_decision_id,
        "approval_decision_sha256": contract_item.approval_decision_sha256,
        "eligible_at": assessment_item.assessed_at,
        "population_sustained_health_verified": True,
        "promotion_outcome_review_ready": True,
        "independent_outcome_decision_required": True,
        "promoted": False,
        "superseded": False,
        "outcome_decision_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionOutcomeEligibility.model_validate(
        {
            **core,
            "eligibility_id": f"evstablepromeligible_{digest[:24]}",
            "eligibility_sha256": digest,
        }
    )


def render_stable_promotion_outcome_eligibility(
    view: EvolutionStablePromotionOutcomeEligibilityView,
) -> str:
    item = view.current_eligibility or view.eligibility
    return "\n".join(
        (
            "## 稳定推广 Outcome 审批资格",
            "",
            f"- Eligibility：`{item.eligibility_id}`",
            f"- Population Assessment：`{item.population_assessment_id}`",
            f"- Proposal：`{item.workbench_proposal_id}`",
            f"- Candidate：`{item.candidate_id}` revision `{item.candidate_revision}`",
            f"- Population passing：`{item.passing_count}/{item.population_denominator}`",
            "- Assessment / Duration coverage：`100.00% / 100.00%`",
            f"- 当前可进入独立审批：`{str(view.outcome_review_ready_authority).lower()}`",
            f"- 撤权原因：`{', '.join(view.invalidation_reasons) or 'none'}`",
            "- 独立 Outcome decision required：`true`",
            "- Promoted / Learning / Promotion / Execution authority：`false`",
        )
    )


async def _require_exact_sources(
    db: aiosqlite.Connection,
    item: EvolutionStablePromotionOutcomeEligibility,
) -> None:
    contract_row = await (
        await db.execute(
            "SELECT contract_json FROM evolution_stable_promotion_observation_contracts "
            "WHERE contract_id = ?",
            (item.contract_id,),
        )
    ).fetchone()
    assessment_row = await (
        await db.execute(
            "SELECT assessment_json FROM evolution_stable_promotion_population_"
            "observation_assessments WHERE assessment_id = ?",
            (item.population_assessment_id,),
        )
    ).fetchone()
    if contract_row is None or assessment_row is None:
        raise EvolutionStablePromotionOutcomeEligibilityError(
            "stable_promotion_outcome_eligibility_source_changed",
            "Contract 或 Population Assessment durable source 已变化。",
        )
    contract = EvolutionStablePromotionObservationContract.model_validate_json(
        contract_row["contract_json"]
    )
    assessment = EvolutionStablePromotionPopulationObservationAssessment.model_validate_json(
        assessment_row["assessment_json"]
    )
    expected = build_stable_promotion_outcome_eligibility(
        workspace_root=item.workspace_root,
        contract=contract,
        assessment=assessment,
    )
    if expected != item:
        raise EvolutionStablePromotionOutcomeEligibilityError(
            "stable_promotion_outcome_eligibility_source_changed",
            "Eligibility 与 durable Contract/Population Assessment 不一致。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_outcome_eligibilities ("
        "eligibility_id TEXT PRIMARY KEY, eligibility_sha256 TEXT NOT NULL UNIQUE, "
        "source_sha256 TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL, "
        "population_assessment_id TEXT NOT NULL UNIQUE, eligibility_json TEXT NOT NULL, "
        "eligible_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_outcome_eligibility_contract "
        "ON evolution_stable_promotion_outcome_eligibilities(contract_id)"
    )


def _row_eligibility(row) -> EvolutionStablePromotionOutcomeEligibility:
    try:
        item = EvolutionStablePromotionOutcomeEligibility.model_validate_json(
            row["eligibility_json"]
        )
        if not (
            row["eligibility_id"] == item.eligibility_id
            and row["eligibility_sha256"] == item.eligibility_sha256
            and row["source_sha256"] == item.source_sha256
            and row["contract_id"] == item.contract_id
            and row["population_assessment_id"] == item.population_assessment_id
            and row["eligible_at"] == item.eligible_at
        ):
            raise ValueError("eligibility row mismatch")
        return item
    except ValueError as exc:
        raise EvolutionStablePromotionOutcomeEligibilityError(
            "stable_promotion_outcome_eligibility_store_corrupt",
            "Promotion Outcome Eligibility durable row 已损坏。",
        ) from exc


def _source(
    contract_id: str,
    contract_sha256: str,
    assessment_id: str,
    assessment_sha256: str,
) -> str:
    return _digest(
        {
            "contract_id": contract_id,
            "contract_sha256": contract_sha256,
            "population_assessment_id": assessment_id,
            "population_assessment_sha256": assessment_sha256,
        }
    )


def _eligibility_id(value: str) -> str:
    item = str(value or "").strip()
    if _ELIGIBILITY_RE.fullmatch(item) is None:
        raise ValueError("eligibility_id 格式无效。")
    return item


def _assessment_id(value: str) -> str:
    item = str(value or "").strip()
    if _ASSESSMENT_RE.fullmatch(item) is None:
        raise ValueError("population_assessment_id 格式无效。")
    return item


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 timezone。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: (
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item.value
        ),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


_INSPECTION_ERRORS = (
    EvolutionStablePromotionOutcomeEligibilityError,
    EvolutionStablePromotionObservationContractError,
    EvolutionStablePromotionPopulationObservationAssessmentError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OUTCOME_ELIGIBILITY_POLICY",
    "EvolutionStablePromotionOutcomeEligibility",
    "EvolutionStablePromotionOutcomeEligibilityError",
    "EvolutionStablePromotionOutcomeEligibilityService",
    "EvolutionStablePromotionOutcomeEligibilityStore",
    "EvolutionStablePromotionOutcomeEligibilityView",
    "EvolutionStablePromotionPopulationObservationInspectionPort",
    "build_stable_promotion_outcome_eligibility",
    "render_stable_promotion_outcome_eligibility",
]
