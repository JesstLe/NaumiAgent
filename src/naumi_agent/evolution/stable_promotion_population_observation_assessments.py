"""Durable population aggregation over stable-promotion installation assessments."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_installation_observation_assessments import (
    EvolutionStablePromotionInstallationObservationAssessment,
    EvolutionStablePromotionInstallationObservationAssessmentError,
    EvolutionStablePromotionInstallationObservationAssessmentService,
    EvolutionStablePromotionInstallationObservationAssessmentStore,
    EvolutionStablePromotionInstallationObservationStatus,
)
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
    EvolutionStablePromotionObservationContractError,
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmission,
    EvolutionStablePromotionRuntimeObservationAdmissionError,
    EvolutionStablePromotionRuntimeObservationAdmissionService,
    EvolutionStablePromotionRuntimeObservationAdmissionStore,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationError,
    EvolutionStableRemotePopulationFinalizationMember,
    EvolutionStableRemotePopulationFinalizationReceipt,
    EvolutionStableRemotePopulationFinalizationService,
    EvolutionStableRemotePopulationFinalizationStore,
)

EVOLUTION_STABLE_PROMOTION_POPULATION_OBSERVATION_ASSESSMENT_POLICY = (
    "evolution-stable-promotion-population-observation-assessment-v1"
)
_ASSESSMENT_RE = re.compile(r"^evstableprompopobserve_[0-9a-f]{24}$")
_FINALIZATION_RE = re.compile(r"^evstableremotepopfinal_[0-9a-f]{24}$")
_MAX_MEMBERS = 10_000
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_INSPECTION_CONCURRENCY = 32


class EvolutionStablePromotionPopulationMemberObservationStatus(StrEnum):
    MISSING = "missing"
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"
    CENSORED = "censored"


class EvolutionStablePromotionPopulationObservationStatus(StrEnum):
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"
    CENSORED = "censored"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionPopulationObservationMember(_StrictModel):
    schema_version: Literal[1] = 1
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    finalization_member_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_id: str = Field(default="", pattern=r"^(?:|evstablepromadmit_[0-9a-f]{24})$")
    admission_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    assessment_id: str = Field(
        default="", pattern=r"^(?:|evstableprominstallobserve_[0-9a-f]{24})$"
    )
    assessment_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    status: EvolutionStablePromotionPopulationMemberObservationStatus
    reason: str = Field(default="", max_length=96)
    sample_count: int = Field(default=0, ge=0, le=5_000)
    operational_sample_count: int = Field(default=0, ge=0, le=5_000)
    observation_seconds: int = Field(default=0, ge=0, le=604_800)
    minimum_observation_seconds: Literal[3600] = 3600
    last_observed_at: str = Field(default="", max_length=100)
    valid_until: str = Field(default="", max_length=100)
    assessment_current_at_issue: bool = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        admission_present = bool(self.admission_id)
        assessment_present = bool(self.assessment_id)
        if not (
            admission_present is bool(self.admission_sha256)
            and assessment_present is bool(self.assessment_sha256)
            and (not assessment_present or admission_present)
        ):
            raise ValueError("Population observation member source identity 不完整。")
        if self.last_observed_at:
            _aware(self.last_observed_at)
        if self.valid_until:
            _aware(self.valid_until)
        if self.status is EvolutionStablePromotionPopulationMemberObservationStatus.MISSING:
            if self.assessment_current_at_issue or self.reason not in {
                "runtime_admission_missing",
                "installation_assessment_missing",
                "installation_assessment_expired",
            }:
                raise ValueError("Population missing member 投影无效。")
        elif not (assessment_present and self.assessment_current_at_issue and not self.reason):
            raise ValueError("Population assessed member 投影无效。")
        if self.status in {
            EvolutionStablePromotionPopulationMemberObservationStatus.INSUFFICIENT,
            EvolutionStablePromotionPopulationMemberObservationStatus.PASSING,
        } and not self.valid_until:
            raise ValueError("Population 非终态 member 缺少有效期。")
        digest = _digest(self.model_dump(mode="json", exclude={"source_sha256"}))
        if not hmac.compare_digest(self.source_sha256, digest):
            raise ValueError("Population observation member content identity 不一致。")
        return self


class EvolutionStablePromotionPopulationObservationAssessment(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-population-observation-assessment-v1"
    ] = EVOLUTION_STABLE_PROMOTION_POPULATION_OBSERVATION_ASSESSMENT_POLICY
    assessment_id: str = Field(pattern=r"^evstableprompopobserve_[0-9a-f]{24}$")
    assessment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_finalization_receipt_id: str = Field(
        pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$"
    )
    population_finalization_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=_MAX_MEMBERS)
    members: tuple[EvolutionStablePromotionPopulationObservationMember, ...] = Field(
        min_length=1, max_length=_MAX_MEMBERS
    )
    assessed_at: str = Field(min_length=1, max_length=100)
    valid_until: str = Field(default="", max_length=100)
    current_assessment_count: int = Field(ge=0, le=_MAX_MEMBERS)
    assessment_coverage_bps: int = Field(ge=0, le=10_000)
    passing_count: int = Field(ge=0, le=_MAX_MEMBERS)
    breached_count: int = Field(ge=0, le=_MAX_MEMBERS)
    censored_count: int = Field(ge=0, le=_MAX_MEMBERS)
    insufficient_count: int = Field(ge=0, le=_MAX_MEMBERS)
    missing_count: int = Field(ge=0, le=_MAX_MEMBERS)
    members_meeting_duration: int = Field(ge=0, le=_MAX_MEMBERS)
    duration_coverage_bps: int = Field(ge=0, le=10_000)
    missing_member_ids: tuple[str, ...] = Field(max_length=_MAX_MEMBERS)
    status: EvolutionStablePromotionPopulationObservationStatus
    population_long_term_metrics_recorded: Literal[True] = True
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Population observation workspace 必须 canonical。")
        _aware(self.assessed_at)
        if self.valid_until:
            _aware(self.valid_until)
        projection = _aggregate(self.members)
        fields = (
            "valid_until",
            "current_assessment_count",
            "assessment_coverage_bps",
            "passing_count",
            "breached_count",
            "censored_count",
            "insufficient_count",
            "missing_count",
            "members_meeting_duration",
            "duration_coverage_bps",
            "missing_member_ids",
            "status",
        )
        member_ids = tuple(item.installation_member_id for item in self.members)
        if not (
            self.population_denominator == len(self.members)
            and member_ids == tuple(sorted(member_ids))
            and len(member_ids) == len(set(member_ids))
            and all(getattr(self, key) == projection[key] for key in fields)
            and self.source_set_sha256
            == _source_set(
                contract_id=self.contract_id,
                contract_sha256=self.contract_sha256,
                finalization_id=self.population_finalization_receipt_id,
                finalization_sha256=self.population_finalization_receipt_sha256,
                members=self.members,
            )
        ):
            raise ValueError("Population observation aggregate projection 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"assessment_id", "assessment_sha256"})
        )
        if not (
            hmac.compare_digest(self.assessment_sha256, digest)
            and self.assessment_id == f"evstableprompopobserve_{digest[:24]}"
        ):
            raise ValueError("Population observation assessment identity 不一致。")
        return self


class EvolutionStablePromotionPopulationObservationAssessmentView(_StrictModel):
    receipt: EvolutionStablePromotionPopulationObservationAssessment
    current_assessment: EvolutionStablePromotionPopulationObservationAssessment | None
    durable_receipt_valid: bool
    latest_receipt_current: bool
    contract_authority: bool
    population_finalization_authority: bool
    member_source_set_current: bool
    assessment_temporally_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    population_long_term_observation_authority: bool
    population_health_alert_authority: bool
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.durable_receipt_valid
            and self.latest_receipt_current
            and self.contract_authority
            and self.population_finalization_authority
            and self.member_source_set_current
            and self.assessment_temporally_current
            and self.current_assessment is not None
        )
        passing = bool(
            current
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionStablePromotionPopulationObservationStatus.PASSING
        )
        breached = bool(
            current
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionStablePromotionPopulationObservationStatus.BREACHED
        )
        if not (
            self.population_long_term_observation_authority is passing
            and self.population_health_alert_authority is breached
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Population observation authority projection 不一致。")
        return self


class EvolutionStablePromotionPopulationObservationAssessmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionPopulationObservationAssessmentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        contract_store: EvolutionStablePromotionObservationContractStore,
        finalization_store: EvolutionStableRemotePopulationFinalizationStore,
        admission_store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
        installation_assessment_store: (
            EvolutionStablePromotionInstallationObservationAssessmentStore
        ),
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            self.db_path
            == contract_store.db_path
            == finalization_store.db_path
            == admission_store.db_path
            == installation_assessment_store.db_path
        ):
            raise ValueError("Population observation sources 必须共享 session SQLite。")
        self.contract_store = contract_store
        self.finalization_store = finalization_store
        self.admission_store = admission_store
        self.installation_assessment_store = installation_assessment_store

    async def get(
        self, assessment_id: str
    ) -> EvolutionStablePromotionPopulationObservationAssessment | None:
        identifier = _assessment_id(assessment_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_population_"
                        "observation_assessments WHERE assessment_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionStablePromotionPopulationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_assessment_store_corrupt",
                "Population observation assessment 损坏或无法读取。",
            ) from exc

    async def latest(
        self, *, contract_id: str
    ) -> EvolutionStablePromotionPopulationObservationAssessment | None:
        identifier = str(contract_id or "").strip()
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_population_"
                        "observation_assessments WHERE contract_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionStablePromotionPopulationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_assessment_store_corrupt",
                "Population observation assessment 损坏或无法读取。",
            ) from exc

    async def record(
        self, assessment: EvolutionStablePromotionPopulationObservationAssessment
    ) -> EvolutionStablePromotionPopulationObservationAssessment:
        item = EvolutionStablePromotionPopulationObservationAssessment.model_validate(assessment)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_assessment_oversized",
                "Population observation assessment 超过 16 MiB。",
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
                        "SELECT * FROM evolution_stable_promotion_population_"
                        "observation_assessments WHERE source_set_sha256 = ?",
                        (item.source_set_sha256,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _row_assessment(row)
                    await db.rollback()
                    if _same_source_projection(restored, item):
                        return restored
                    raise EvolutionStablePromotionPopulationObservationAssessmentError(
                        "stable_promotion_population_assessment_source_conflict",
                        "同一 Population observation source-set 已绑定不同内容。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_population_observation_"
                    "assessments (assessment_id, assessment_sha256, source_set_sha256, "
                    "contract_id, population_finalization_receipt_id, status, "
                    "assessment_json, assessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.assessment_id,
                        item.assessment_sha256,
                        item.source_set_sha256,
                        item.contract_id,
                        item.population_finalization_receipt_id,
                        item.status.value,
                        encoded,
                        item.assessed_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionPopulationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_assessment_store_failed",
                "Population observation assessment 无法持久化。",
            ) from exc


class EvolutionStablePromotionPopulationObservationAssessmentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionStablePromotionObservationContractStore,
        contract_service: EvolutionStablePromotionObservationContractService,
        finalization_service: EvolutionStableRemotePopulationFinalizationService,
        admission_store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
        admission_service: EvolutionStablePromotionRuntimeObservationAdmissionService,
        installation_assessment_store: (
            EvolutionStablePromotionInstallationObservationAssessmentStore
        ),
        installation_assessment_service: (
            EvolutionStablePromotionInstallationObservationAssessmentService
        ),
        store: EvolutionStablePromotionPopulationObservationAssessmentStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            store.contract_store is contract_store
            and store.finalization_store is finalization_service.store
            and store.admission_store is admission_store
            and store.installation_assessment_store is installation_assessment_store
            and installation_assessment_service.store is installation_assessment_store
            and finalization_service.workspace_root
            == Path(workspace_root).expanduser().resolve(strict=True)
        ):
            raise ValueError("Population observation Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.finalization_service = finalization_service
        self.admission_store = admission_store
        self.admission_service = admission_service
        self.installation_assessment_store = installation_assessment_store
        self.installation_assessment_service = installation_assessment_service
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def assess(
        self, *, finalization_receipt_id: str
    ) -> EvolutionStablePromotionPopulationObservationAssessmentView:
        receipt_id = _finalization_id(finalization_receipt_id)
        lock = self._locks.setdefault(receipt_id, asyncio.Lock())
        async with lock:
            assessed_at = self._now()
            contract, finalization, members = await self._current_sources(
                receipt_id, assessed_at=assessed_at
            )
            artifact = build_stable_promotion_population_observation_assessment(
                workspace_root=self.workspace_root,
                contract=contract,
                finalization=finalization,
                members=members,
                assessed_at=assessed_at,
            )
            stored = await self.store.record(artifact)
        return await self.inspect(assessment_id=stored.assessment_id)

    async def inspect(
        self, *, assessment_id: str
    ) -> EvolutionStablePromotionPopulationObservationAssessmentView:
        receipt = await self.store.get(assessment_id)
        if receipt is None:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_assessment_missing",
                "Population observation assessment 不存在。",
            )
        source = await self.store.get(receipt.assessment_id)
        latest = await self.store.latest(contract_id=receipt.contract_id)
        durable = source == receipt
        latest_current = latest == receipt
        contract_authority = finalization_authority = member_current = False
        temporal_current = _temporally_current(receipt, self._now())
        current = None
        reasons: list[str] = []
        try:
            contract, finalization, members = await self._current_sources(
                receipt.population_finalization_receipt_id,
                assessed_at=_aware(receipt.assessed_at),
            )
            contract_authority = bool(
                contract.contract_id == receipt.contract_id
                and contract.contract_sha256 == receipt.contract_sha256
            )
            finalization_authority = bool(
                finalization.receipt_id == receipt.population_finalization_receipt_id
                and finalization.receipt_sha256
                == receipt.population_finalization_receipt_sha256
            )
            member_current = tuple(members) == receipt.members
            if (
                contract_authority
                and finalization_authority
                and member_current
                and temporal_current
            ):
                current = receipt
        except _INSPECTION_ERRORS:
            reasons.append("authoritative_source_unavailable")
        checks = {
            "population_assessment_source_changed": durable,
            "newer_population_assessment_exists": latest_current,
            "observation_contract_stale": contract_authority,
            "population_finalization_stale": finalization_authority,
            "population_member_source_set_changed": member_current,
            "population_assessment_expired": temporal_current,
        }
        reasons.extend(reason for reason, passed in checks.items() if not passed)
        all_current = bool(all(checks.values()) and current is not None)
        return EvolutionStablePromotionPopulationObservationAssessmentView(
            receipt=receipt,
            current_assessment=current,
            durable_receipt_valid=durable,
            latest_receipt_current=latest_current,
            contract_authority=contract_authority,
            population_finalization_authority=finalization_authority,
            member_source_set_current=member_current,
            assessment_temporally_current=temporal_current,
            invalidation_reasons=tuple(sorted(set(reasons))),
            population_long_term_observation_authority=bool(
                all_current
                and receipt.status is EvolutionStablePromotionPopulationObservationStatus.PASSING
            ),
            population_health_alert_authority=bool(
                all_current
                and receipt.status is EvolutionStablePromotionPopulationObservationStatus.BREACHED
            ),
        )

    async def _current_sources(
        self, receipt_id: str, *, assessed_at: datetime
    ) -> tuple[
        EvolutionStablePromotionObservationContract,
        EvolutionStableRemotePopulationFinalizationReceipt,
        tuple[EvolutionStablePromotionPopulationObservationMember, ...],
    ]:
        contract = await self.contract_store.get_by_finalization(receipt_id)
        if contract is None:
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_contract_missing",
                "Control Plane 缺少 Population Observation Contract。",
            )
        contract_view = await self.contract_service.inspect(contract=contract)
        finalization_view = await self.finalization_service.inspect(receipt_id=receipt_id)
        if not (
            contract_view.observation_contract_authority
            and finalization_view.stable_population_finalization_authority
            and contract.population_finalization_receipt_sha256
            == finalization_view.receipt.receipt_sha256
        ):
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_source_stale",
                "Observation Contract 或 Population Finalization authority 已失效。",
            )
        finalization = finalization_view.receipt
        admissions = await self.admission_store.list_for_contract(contract.contract_id)
        by_member: dict[str, list[EvolutionStablePromotionRuntimeObservationAdmission]] = (
            defaultdict(list)
        )
        exact_ids = {item.installation_member_id for item in finalization.members}
        for admission in admissions:
            if admission.installation_member_id not in exact_ids:
                raise EvolutionStablePromotionPopulationObservationAssessmentError(
                    "stable_promotion_population_extra_admission",
                    "Runtime Admission 包含 Population 之外的 installation member。",
                )
            by_member[admission.installation_member_id].append(admission)
        if any(len(items) > 1 for items in by_member.values()):
            raise EvolutionStablePromotionPopulationObservationAssessmentError(
                "stable_promotion_population_admission_conflict",
                "同一 Population member 存在多个 durable Runtime Admission，必须先仲裁。",
            )
        semaphore = asyncio.Semaphore(_INSPECTION_CONCURRENCY)

        async def project(
            member: EvolutionStableRemotePopulationFinalizationMember,
        ) -> EvolutionStablePromotionPopulationObservationMember:
            async with semaphore:
                found = by_member.get(member.installation_member_id, [])
                if not found:
                    return _member_projection(member=member)
                admission = found[0]
                admission_view = await self.admission_service.inspect(admission=admission)
                if not admission_view.runtime_observation_input_authority:
                    raise EvolutionStablePromotionPopulationObservationAssessmentError(
                        "stable_promotion_population_admission_stale",
                        "至少一个 Runtime Admission authority 已失效。",
                    )
                assessment = await self.installation_assessment_store.latest(
                    admission_id=admission.admission_id
                )
                if assessment is None:
                    return _member_projection(member=member, admission=admission)
                view = await self.installation_assessment_service.inspect(
                    admission_id=admission.admission_id
                )
                if view.current_assessment is not None:
                    return _member_projection(
                        member=member,
                        admission=admission,
                        assessment=view.current_assessment,
                        assessed_at=assessed_at,
                    )
                if set(view.invalidation_reasons) <= {"assessment_expired"}:
                    return _member_projection(
                        member=member,
                        admission=admission,
                        assessment=assessment,
                        assessed_at=assessed_at,
                        force_expired=True,
                    )
                raise EvolutionStablePromotionPopulationObservationAssessmentError(
                    "stable_promotion_population_installation_assessment_stale",
                    "至少一个 Installation Assessment durable source 已失效。",
                )

        members = tuple(
            await asyncio.gather(*(project(member) for member in finalization.members))
        )
        return contract, finalization, members

    def _now(self) -> datetime:
        return _aware(self.clock())


def build_stable_promotion_population_observation_assessment(
    *,
    workspace_root: str | Path,
    contract: EvolutionStablePromotionObservationContract,
    finalization: EvolutionStableRemotePopulationFinalizationReceipt,
    members: tuple[EvolutionStablePromotionPopulationObservationMember, ...],
    assessed_at: str | datetime,
) -> EvolutionStablePromotionPopulationObservationAssessment:
    root = Path(workspace_root).expanduser().resolve(strict=True)
    contract_item = EvolutionStablePromotionObservationContract.model_validate(contract)
    finalization_item = EvolutionStableRemotePopulationFinalizationReceipt.model_validate(
        finalization
    )
    member_items = tuple(
        sorted(
            (
                EvolutionStablePromotionPopulationObservationMember.model_validate(item)
                for item in members
            ),
            key=lambda item: item.installation_member_id,
        )
    )
    if not (
        contract_item.workspace_root == str(root)
        and contract_item.population_finalization_receipt_id == finalization_item.receipt_id
        and contract_item.population_finalization_receipt_sha256
        == finalization_item.receipt_sha256
        and contract_item.population_snapshot_id == finalization_item.population_snapshot_id
        and contract_item.population_snapshot_sha256
        == finalization_item.population_snapshot_sha256
        and contract_item.population_snapshot_sequence == finalization_item.population_sequence
        and contract_item.population_denominator == finalization_item.population_denominator
        and len(member_items) == finalization_item.population_denominator
        and tuple(item.installation_member_id for item in member_items)
        == tuple(item.installation_member_id for item in finalization_item.members)
    ):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_lineage_mismatch",
            "Contract、Finalization 与 Population member lineage 不一致。",
        )
    projection = _aggregate(member_items)
    core = {
        "schema_version": 1,
        "policy_version": (
            EVOLUTION_STABLE_PROMOTION_POPULATION_OBSERVATION_ASSESSMENT_POLICY
        ),
        "source_set_sha256": _source_set(
            contract_id=contract_item.contract_id,
            contract_sha256=contract_item.contract_sha256,
            finalization_id=finalization_item.receipt_id,
            finalization_sha256=finalization_item.receipt_sha256,
            members=member_items,
        ),
        "workspace_root": str(root),
        "contract_id": contract_item.contract_id,
        "contract_sha256": contract_item.contract_sha256,
        "population_finalization_receipt_id": finalization_item.receipt_id,
        "population_finalization_receipt_sha256": finalization_item.receipt_sha256,
        "population_snapshot_id": finalization_item.population_snapshot_id,
        "population_snapshot_sha256": finalization_item.population_snapshot_sha256,
        "population_snapshot_sequence": finalization_item.population_sequence,
        "population_denominator": finalization_item.population_denominator,
        "members": member_items,
        "assessed_at": _aware(assessed_at).isoformat(),
        **projection,
        "population_long_term_metrics_recorded": True,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionPopulationObservationAssessment.model_validate(
        {
            **core,
            "assessment_id": f"evstableprompopobserve_{digest[:24]}",
            "assessment_sha256": digest,
        }
    )


def render_stable_promotion_population_observation_assessment(
    view: EvolutionStablePromotionPopulationObservationAssessmentView,
) -> str:
    item = view.current_assessment or view.receipt
    labels = {
        EvolutionStablePromotionPopulationObservationStatus.INSUFFICIENT: "不足",
        EvolutionStablePromotionPopulationObservationStatus.PASSING: "通过",
        EvolutionStablePromotionPopulationObservationStatus.BREACHED: "违约",
        EvolutionStablePromotionPopulationObservationStatus.CENSORED: "删失",
    }
    return "\n".join(
        (
            "## 稳定推广群体长期观察",
            "",
            f"- Assessment：`{item.assessment_id}`",
            f"- Population：`{item.population_snapshot_id}`",
            f"- 状态：**{labels[item.status]}（{item.status.value}）**",
            f"- 覆盖：`{item.current_assessment_count}/{item.population_denominator}` "
            f"（`{item.assessment_coverage_bps / 100:.2f}%`）",
            f"- 通过 / 违约 / 删失：`{item.passing_count}` / "
            f"`{item.breached_count}` / `{item.censored_count}`",
            f"- 不足 / 缺失：`{item.insufficient_count}` / `{item.missing_count}`",
            f"- 时长覆盖：`{item.duration_coverage_bps / 100:.2f}%`",
            f"- 缺失成员：`{', '.join(item.missing_member_ids) or 'none'}`",
            f"- 撤权原因：`{', '.join(view.invalidation_reasons) or 'none'}`",
            "- Population health authority："
            f"`{str(view.population_long_term_observation_authority).lower()}`",
            "- Population alert authority："
            f"`{str(view.population_health_alert_authority).lower()}`",
            "- Promoted Outcome / Learning / Promotion authority：`false`",
        )
    )


def _member_projection(
    *,
    member: EvolutionStableRemotePopulationFinalizationMember,
    admission: EvolutionStablePromotionRuntimeObservationAdmission | None = None,
    assessment: EvolutionStablePromotionInstallationObservationAssessment | None = None,
    assessed_at: datetime | None = None,
    force_expired: bool = False,
) -> EvolutionStablePromotionPopulationObservationMember:
    if admission is not None and not (
        admission.installation_member_id == member.installation_member_id
        and admission.member_source_sha256 == member.member_source_sha256
    ):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_member_admission_mismatch",
            "Runtime Admission 与 exact Population member 不一致。",
        )
    if assessment is not None and not (
        admission is not None
        and assessment.admission.admission_id == admission.admission_id
        and assessment.admission.admission_sha256 == admission.admission_sha256
    ):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_member_assessment_mismatch",
            "Installation Assessment 与 Runtime Admission 不一致。",
        )
    status = EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
    reason = "runtime_admission_missing"
    sample_count = operational = observation_seconds = 0
    last_observed = valid_until = ""
    current = False
    if admission is not None:
        reason = "installation_assessment_missing"
    if assessment is not None:
        sample_count = assessment.sample_count
        operational = assessment.operational_sample_count
        observation_seconds = assessment.observation_seconds
        last_observed = assessment.last_observed_at
        if assessment.status in {
            EvolutionStablePromotionInstallationObservationStatus.INSUFFICIENT,
            EvolutionStablePromotionInstallationObservationStatus.PASSING,
        }:
            valid_until = (
                _aware(assessment.last_observed_at)
                + timedelta(seconds=assessment.maximum_gap_seconds)
            ).isoformat()
        expired = bool(
            force_expired
            or (
                assessed_at is not None
                and valid_until
                and _aware(assessed_at) > _aware(valid_until)
            )
        )
        if expired:
            reason = "installation_assessment_expired"
        else:
            status = EvolutionStablePromotionPopulationMemberObservationStatus(
                assessment.status.value
            )
            reason = ""
            current = True
    core = {
        "schema_version": 1,
        "installation_member_id": member.installation_member_id,
        "installation_credential_id": member.installation_credential_id,
        "finalization_member_source_sha256": member.member_source_sha256,
        "admission_id": "" if admission is None else admission.admission_id,
        "admission_sha256": "" if admission is None else admission.admission_sha256,
        "assessment_id": "" if assessment is None else assessment.assessment_id,
        "assessment_sha256": "" if assessment is None else assessment.assessment_sha256,
        "status": status,
        "reason": reason,
        "sample_count": sample_count,
        "operational_sample_count": operational,
        "observation_seconds": observation_seconds,
        "minimum_observation_seconds": 3600,
        "last_observed_at": last_observed,
        "valid_until": valid_until,
        "assessment_current_at_issue": current,
    }
    return EvolutionStablePromotionPopulationObservationMember.model_validate(
        {**core, "source_sha256": _digest(core)}
    )


def _aggregate(
    members: tuple[EvolutionStablePromotionPopulationObservationMember, ...],
) -> dict[str, object]:
    denominator = len(members)
    if not 1 <= denominator <= _MAX_MEMBERS:
        raise ValueError("Population observation member 数量无效。")
    counts = {status: 0 for status in EvolutionStablePromotionPopulationMemberObservationStatus}
    for item in members:
        counts[item.status] += 1
    current_count = denominator - counts[
        EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
    ]
    duration_sum = sum(min(item.observation_seconds, 3600) for item in members)
    deadlines = tuple(
        item.valid_until
        for item in members
        if item.assessment_current_at_issue and item.valid_until
    )
    if counts[EvolutionStablePromotionPopulationMemberObservationStatus.BREACHED]:
        status = EvolutionStablePromotionPopulationObservationStatus.BREACHED
    elif counts[EvolutionStablePromotionPopulationMemberObservationStatus.CENSORED]:
        status = EvolutionStablePromotionPopulationObservationStatus.CENSORED
    elif counts[EvolutionStablePromotionPopulationMemberObservationStatus.MISSING] or counts[
        EvolutionStablePromotionPopulationMemberObservationStatus.INSUFFICIENT
    ]:
        status = EvolutionStablePromotionPopulationObservationStatus.INSUFFICIENT
    else:
        status = EvolutionStablePromotionPopulationObservationStatus.PASSING
    return {
        "valid_until": min(deadlines, key=_aware) if deadlines else "",
        "current_assessment_count": current_count,
        "assessment_coverage_bps": current_count * 10_000 // denominator,
        "passing_count": counts[
            EvolutionStablePromotionPopulationMemberObservationStatus.PASSING
        ],
        "breached_count": counts[
            EvolutionStablePromotionPopulationMemberObservationStatus.BREACHED
        ],
        "censored_count": counts[
            EvolutionStablePromotionPopulationMemberObservationStatus.CENSORED
        ],
        "insufficient_count": counts[
            EvolutionStablePromotionPopulationMemberObservationStatus.INSUFFICIENT
        ],
        "missing_count": counts[
            EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
        ],
        "members_meeting_duration": sum(
            item.observation_seconds >= 3600 for item in members
        ),
        "duration_coverage_bps": duration_sum * 10_000 // (denominator * 3600),
        "missing_member_ids": tuple(
            item.installation_member_id
            for item in members
            if item.status is EvolutionStablePromotionPopulationMemberObservationStatus.MISSING
        ),
        "status": status,
    }


async def _require_exact_sources(
    db: aiosqlite.Connection,
    item: EvolutionStablePromotionPopulationObservationAssessment,
) -> None:
    contract_row = await (
        await db.execute(
            "SELECT contract_json FROM evolution_stable_promotion_observation_contracts "
            "WHERE contract_id = ?",
            (item.contract_id,),
        )
    ).fetchone()
    finalization_row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_stable_remote_population_finalizations "
            "WHERE receipt_id = ?",
            (item.population_finalization_receipt_id,),
        )
    ).fetchone()
    if contract_row is None or finalization_row is None:
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_source_changed",
            "Contract 或 Population Finalization durable source 已变化。",
        )
    contract = EvolutionStablePromotionObservationContract.model_validate_json(
        contract_row["contract_json"]
    )
    finalization = EvolutionStableRemotePopulationFinalizationReceipt.model_validate_json(
        finalization_row["receipt_json"]
    )
    if not (
        contract.contract_sha256 == item.contract_sha256
        and finalization.receipt_sha256 == item.population_finalization_receipt_sha256
    ):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_source_changed",
            "Contract 或 Population Finalization identity 已变化。",
        )
    rows = await (
        await db.execute(
            "SELECT admission_json FROM evolution_stable_promotion_runtime_admissions "
            "WHERE observation_contract_id = ? ORDER BY installation_member_id, "
            "admitted_at, admission_id LIMIT 10001",
            (item.contract_id,),
        )
    ).fetchall()
    if len(rows) > _MAX_MEMBERS:
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_member_limit_exceeded",
            "Runtime Admission 超过 Population 上限。",
        )
    admissions = tuple(
        EvolutionStablePromotionRuntimeObservationAdmission.model_validate_json(
            row["admission_json"]
        )
        for row in rows
    )
    grouped: dict[str, list[EvolutionStablePromotionRuntimeObservationAdmission]] = defaultdict(
        list
    )
    for admission in admissions:
        grouped[admission.installation_member_id].append(admission)
    if any(len(values) > 1 for values in grouped.values()):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_admission_conflict",
            "Writer 发现同一 member 存在多个 Runtime Admission。",
        )
    finalization_members = {
        member.installation_member_id: member for member in finalization.members
    }
    if set(grouped) - set(finalization_members):
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_extra_admission",
            "Writer 发现 Population 之外的 Runtime Admission。",
        )
    rebuilt = []
    assessed_at = _aware(item.assessed_at)
    for source in finalization.members:
        found = grouped.get(source.installation_member_id, [])
        admission = found[0] if found else None
        assessment = None
        if admission is not None:
            assessment_row = await (
                await db.execute(
                    "SELECT assessment_json FROM evolution_stable_promotion_installation_"
                    "observation_assessments WHERE admission_id = ? ORDER BY "
                    "last_sequence DESC, assessed_at DESC, assessment_id DESC LIMIT 1",
                    (admission.admission_id,),
                )
            ).fetchone()
            if assessment_row is not None:
                assessment = (
                    EvolutionStablePromotionInstallationObservationAssessment.model_validate_json(
                        assessment_row["assessment_json"]
                    )
                )
        rebuilt.append(
            _member_projection(
                member=source,
                admission=admission,
                assessment=assessment,
                assessed_at=assessed_at,
            )
        )
    if tuple(rebuilt) != item.members:
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_source_changed",
            "Writer 发现 Admission 或 Installation Assessment source-set 已变化。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_population_"
        "observation_assessments (assessment_id TEXT PRIMARY KEY, "
        "assessment_sha256 TEXT NOT NULL UNIQUE, source_set_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL, population_finalization_receipt_id TEXT NOT NULL, "
        "status TEXT NOT NULL, assessment_json TEXT NOT NULL, assessed_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_population_observation_contract "
        "ON evolution_stable_promotion_population_observation_assessments "
        "(contract_id, assessed_at)"
    )


def _row_assessment(row) -> EvolutionStablePromotionPopulationObservationAssessment:
    try:
        item = EvolutionStablePromotionPopulationObservationAssessment.model_validate_json(
            row["assessment_json"]
        )
        if not (
            row["assessment_id"] == item.assessment_id
            and row["assessment_sha256"] == item.assessment_sha256
            and row["source_set_sha256"] == item.source_set_sha256
            and row["contract_id"] == item.contract_id
            and row["population_finalization_receipt_id"]
            == item.population_finalization_receipt_id
            and row["status"] == item.status.value
            and row["assessed_at"] == item.assessed_at
        ):
            raise ValueError("population assessment row mismatch")
        return item
    except ValueError as exc:
        raise EvolutionStablePromotionPopulationObservationAssessmentError(
            "stable_promotion_population_assessment_store_corrupt",
            "Population observation assessment durable row 已损坏。",
        ) from exc


def _source_set(
    *,
    contract_id: str,
    contract_sha256: str,
    finalization_id: str,
    finalization_sha256: str,
    members: tuple[EvolutionStablePromotionPopulationObservationMember, ...],
) -> str:
    return _digest(
        {
            "contract_id": contract_id,
            "contract_sha256": contract_sha256,
            "population_finalization_receipt_id": finalization_id,
            "population_finalization_receipt_sha256": finalization_sha256,
            "member_source_sha256": tuple(item.source_sha256 for item in members),
        }
    )


def _same_source_projection(
    left: EvolutionStablePromotionPopulationObservationAssessment,
    right: EvolutionStablePromotionPopulationObservationAssessment,
) -> bool:
    excluded = {"assessment_id", "assessment_sha256", "assessed_at"}
    return left.model_dump(mode="json", exclude=excluded) == right.model_dump(
        mode="json", exclude=excluded
    )


def _temporally_current(
    assessment: EvolutionStablePromotionPopulationObservationAssessment,
    now: str | datetime,
) -> bool:
    return not assessment.valid_until or _aware(now) <= _aware(assessment.valid_until)


def _assessment_id(value: str) -> str:
    item = str(value or "").strip()
    if _ASSESSMENT_RE.fullmatch(item) is None:
        raise ValueError("Population observation assessment_id 格式无效。")
    return item


def _finalization_id(value: str) -> str:
    item = str(value or "").strip()
    if _FINALIZATION_RE.fullmatch(item) is None:
        raise ValueError("Population finalization receipt_id 格式无效。")
    return item


def _aware(value: str | datetime) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
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
    EvolutionStablePromotionPopulationObservationAssessmentError,
    EvolutionStablePromotionObservationContractError,
    EvolutionStableRemotePopulationFinalizationError,
    EvolutionStablePromotionRuntimeObservationAdmissionError,
    EvolutionStablePromotionInstallationObservationAssessmentError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_POPULATION_OBSERVATION_ASSESSMENT_POLICY",
    "EvolutionStablePromotionPopulationMemberObservationStatus",
    "EvolutionStablePromotionPopulationObservationAssessment",
    "EvolutionStablePromotionPopulationObservationAssessmentError",
    "EvolutionStablePromotionPopulationObservationAssessmentService",
    "EvolutionStablePromotionPopulationObservationAssessmentStore",
    "EvolutionStablePromotionPopulationObservationAssessmentView",
    "EvolutionStablePromotionPopulationObservationMember",
    "EvolutionStablePromotionPopulationObservationStatus",
    "build_stable_promotion_population_observation_assessment",
    "render_stable_promotion_population_observation_assessment",
]
