"""Control-Plane assessments over verified stable-promotion revision ledgers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
    EvolutionStablePromotionObservationContractError,
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_promotion_observation_revision_deliveries import (
    EvolutionStablePromotionObservationRevisionDeliveryError,
    EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    EvolutionStablePromotionObservationRevisionDeliveryService,
    EvolutionStablePromotionObservationRevisionDeliveryStore,
    EvolutionStablePromotionObservationRevisionSubmission,
    stable_promotion_observation_revision_receipt_matches_submission,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmission,
    EvolutionStablePromotionRuntimeObservationAdmissionError,
    EvolutionStablePromotionRuntimeObservationAdmissionService,
    EvolutionStablePromotionRuntimeObservationAdmissionStore,
)

EVOLUTION_STABLE_PROMOTION_INSTALLATION_OBSERVATION_ASSESSMENT_POLICY = (
    "evolution-stable-promotion-installation-observation-assessment-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_ADMISSION_RE = re.compile(r"^evstablepromadmit_[0-9a-f]{24}$")
_ASSESSMENT_RE = re.compile(r"^evstableprominstallobserve_[0-9a-f]{24}$")
_MAX_REVISIONS = 5_000
_MAX_BATCHES = 5_000
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class EvolutionStablePromotionInstallationObservationStatus(StrEnum):
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


class EvolutionStablePromotionInstallationObservationBatch(_StrictModel):
    submission: EvolutionStablePromotionObservationRevisionSubmission
    receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if not stable_promotion_observation_revision_receipt_matches_submission(
            self.receipt,
            self.submission,
        ):
            raise ValueError("Observation assessment batch Receipt binding 无效。")
        return self


class EvolutionStablePromotionInstallationObservationAssessment(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-promotion-installation-observation-assessment-v1"] = (
        EVOLUTION_STABLE_PROMOTION_INSTALLATION_OBSERVATION_ASSESSMENT_POLICY
    )
    assessment_id: str = Field(pattern=r"^evstableprominstallobserve_[0-9a-f]{24}$")
    assessment_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract: EvolutionStablePromotionObservationContract
    admission: EvolutionStablePromotionRuntimeObservationAdmission
    batches: tuple[EvolutionStablePromotionInstallationObservationBatch, ...] = Field(
        min_length=1,
        max_length=_MAX_BATCHES,
    )
    assessed_at: str = Field(min_length=1, max_length=100)
    first_sequence: int = Field(ge=1, le=_MAX_REVISIONS)
    last_sequence: int = Field(ge=1, le=_MAX_REVISIONS)
    ledger_head_revision_sha256: str = Field(pattern=_SHA256_RE)
    ledger_head_receipt_id: str = Field(pattern=r"^evstablepromrevreceive_[0-9a-f]{24}$")
    ledger_head_receipt_sha256: str = Field(pattern=_SHA256_RE)
    first_observed_at: str = Field(min_length=1, max_length=100)
    last_observed_at: str = Field(min_length=1, max_length=100)
    sample_count: int = Field(ge=1, le=_MAX_REVISIONS)
    operational_sample_count: int = Field(ge=0, le=_MAX_REVISIONS)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: Literal[3600] = 3600
    minimum_operational_samples: Literal[12] = 12
    maximum_gap_seconds: int = Field(ge=3, le=86_400)
    maximum_observed_gap_seconds: int = Field(ge=0, le=86_400)
    latest_age_seconds: int = Field(ge=0, le=604_800)
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    censor_reasons: tuple[str, ...] = Field(max_length=4)
    status: EvolutionStablePromotionInstallationObservationStatus
    remote_revision_chain_evaluated: Literal[True] = True
    installation_long_term_metrics_recorded: Literal[True] = True
    historical_installation_health_passed: bool
    installation_health_alert_recorded: bool
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Installation observation assessment workspace 必须 canonical。")
        projection = _evaluate(
            contract=self.contract,
            admission=self.admission,
            batches=self.batches,
            assessed_at=self.assessed_at,
        )
        fields = (
            "first_sequence",
            "last_sequence",
            "ledger_head_revision_sha256",
            "ledger_head_receipt_id",
            "ledger_head_receipt_sha256",
            "first_observed_at",
            "last_observed_at",
            "sample_count",
            "operational_sample_count",
            "observation_seconds",
            "minimum_observation_seconds",
            "minimum_operational_samples",
            "maximum_gap_seconds",
            "maximum_observed_gap_seconds",
            "latest_age_seconds",
            "insufficient_reasons",
            "breach_reasons",
            "censor_reasons",
            "status",
            "historical_installation_health_passed",
            "installation_health_alert_recorded",
        )
        if any(getattr(self, name) != projection[name] for name in fields):
            raise ValueError("Installation observation evidence projection 不一致。")
        if any(
            (
                self.population_observation_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("Installation assessment 不得扩张 Population authority。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"assessment_id", "assessment_sha256"},
            )
        )
        if not (
            hmac.compare_digest(self.assessment_sha256, digest)
            and self.assessment_id == f"evstableprominstallobserve_{digest[:24]}"
        ):
            raise ValueError("Installation observation assessment identity 不一致。")
        return self


class EvolutionStablePromotionInstallationObservationAssessmentView(_StrictModel):
    receipt: EvolutionStablePromotionInstallationObservationAssessment
    current_assessment: EvolutionStablePromotionInstallationObservationAssessment | None
    receipt_source_current: bool
    latest_receipt_current: bool
    contract_authority: bool
    admission_authority: bool
    remote_revision_delivery_authority: bool
    observation_ledger_current: bool
    assessment_temporally_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    installation_long_term_health_authority: bool
    installation_health_alert_authority: bool
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        durable = bool(
            self.receipt_source_current
            and self.latest_receipt_current
            and self.contract_authority
            and self.admission_authority
            and self.remote_revision_delivery_authority
            and self.observation_ledger_current
            and self.assessment_temporally_current
            and self.current_assessment is not None
        )
        passing = bool(
            durable
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionStablePromotionInstallationObservationStatus.PASSING
        )
        breached = bool(
            durable
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionStablePromotionInstallationObservationStatus.BREACHED
        )
        if not (
            self.installation_long_term_health_authority is passing
            and self.installation_health_alert_authority is breached
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Installation observation authority projection 不一致。")
        return self


class EvolutionStablePromotionInstallationObservationAssessmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionInstallationObservationAssessmentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        contract_store: EvolutionStablePromotionObservationContractStore,
        admission_store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
        revision_store: EvolutionStablePromotionObservationRevisionDeliveryStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            self.db_path
            == contract_store.db_path
            == admission_store.db_path
            == revision_store.db_path
        ):
            raise ValueError("Installation assessment sources 必须共享 session SQLite。")
        self.contract_store = contract_store
        self.admission_store = admission_store
        self.revision_store = revision_store

    async def get(
        self,
        assessment_id: str,
    ) -> EvolutionStablePromotionInstallationObservationAssessment | None:
        identifier = _assessment_id(assessment_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_stable_promotion_installation_observation_assessments "
                        "WHERE assessment_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionStablePromotionInstallationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_store_corrupt",
                "Installation observation assessment 损坏或无法读取。",
            ) from exc

    async def latest(
        self,
        *,
        admission_id: str,
    ) -> EvolutionStablePromotionInstallationObservationAssessment | None:
        identifier = _admission_id(admission_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_stable_promotion_installation_observation_assessments "
                        "WHERE admission_id = ? ORDER BY last_sequence DESC, "
                        "assessed_at DESC, assessment_id DESC LIMIT 1",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionStablePromotionInstallationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_store_corrupt",
                "Installation observation assessment 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        assessment: EvolutionStablePromotionInstallationObservationAssessment,
    ) -> EvolutionStablePromotionInstallationObservationAssessment:
        item = _assessment(assessment)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_oversized",
                "Installation observation assessment 超过 16 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_exact_sources(db, item)
                existing = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_stable_promotion_installation_observation_assessments "
                        "WHERE assessment_id = ?",
                        (item.assessment_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _row_assessment(existing)
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionInstallationObservationAssessmentError(
                        "stable_promotion_installation_assessment_identity_conflict",
                        "同一 Installation assessment ID 已绑定不同内容。",
                    )
                await db.execute(
                    "INSERT INTO "
                    "evolution_stable_promotion_installation_observation_assessments "
                    "(assessment_id, assessment_sha256, contract_id, admission_id, "
                    "installation_member_id, subject_id, status, last_sequence, "
                    "ledger_head_revision_sha256, assessment_json, assessed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.assessment_id,
                        item.assessment_sha256,
                        item.contract.contract_id,
                        item.admission.admission_id,
                        item.admission.installation_member_id,
                        item.admission.subject_id,
                        item.status.value,
                        item.last_sequence,
                        item.ledger_head_revision_sha256,
                        encoded,
                        item.assessed_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionInstallationObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_store_failed",
                "Installation observation assessment 无法持久化。",
            ) from exc


class EvolutionStablePromotionInstallationObservationAssessmentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionStablePromotionObservationContractStore,
        contract_service: EvolutionStablePromotionObservationContractService,
        admission_store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
        admission_service: EvolutionStablePromotionRuntimeObservationAdmissionService,
        revision_store: EvolutionStablePromotionObservationRevisionDeliveryStore,
        revision_service: EvolutionStablePromotionObservationRevisionDeliveryService,
        store: EvolutionStablePromotionInstallationObservationAssessmentStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            store.contract_store is contract_store
            and store.admission_store is admission_store
            and store.revision_store is revision_store
            and revision_service.store is revision_store
        ):
            raise ValueError("Installation assessment Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.admission_store = admission_store
        self.admission_service = admission_service
        self.revision_store = revision_store
        self.revision_service = revision_service
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def assess(
        self,
        *,
        admission_id: str,
    ) -> EvolutionStablePromotionInstallationObservationAssessmentView:
        identifier = _admission_id(admission_id)
        lock = self._locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            contract, admission, batches = await self._authoritative_sources(identifier)
            assessed_at = self._now().isoformat()
            artifact = build_stable_promotion_installation_observation_assessment(
                contract=contract,
                admission=admission,
                batches=batches,
                assessed_at=assessed_at,
            )
            recorded = await self.store.record(artifact)
            return await self._view(recorded, assessed_at=assessed_at)

    async def inspect(
        self,
        *,
        admission_id: str,
    ) -> EvolutionStablePromotionInstallationObservationAssessmentView:
        latest = await self.store.latest(admission_id=admission_id)
        if latest is None:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_missing",
                "尚未形成 Installation observation assessment。",
            )
        return await self._view(latest, assessed_at=self._now().isoformat())

    async def _authoritative_sources(
        self,
        admission_id: str,
    ) -> tuple[
        EvolutionStablePromotionObservationContract,
        EvolutionStablePromotionRuntimeObservationAdmission,
        tuple[EvolutionStablePromotionInstallationObservationBatch, ...],
    ]:
        admission = await self.admission_store.get_by_id(admission_id)
        if admission is None:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_admission_missing",
                "Control Plane 缺少 Runtime Observation Admission。",
            )
        contract = await self.contract_store.get_by_finalization(
            admission.population_finalization_receipt_id
        )
        if contract is None:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_contract_missing",
                "Control Plane 缺少 Observation Contract。",
            )
        chain = await self.revision_service.authoritative_received_chain(admission_id)
        if not chain:
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_revision_missing",
                "Control Plane 尚未接收 observation revision。",
            )
        batches = tuple(
            EvolutionStablePromotionInstallationObservationBatch(
                submission=submission,
                receipt=receipt,
            )
            for submission, receipt in chain
        )
        contract_view = await self.contract_service.inspect(contract=contract)
        admission_view = await self.admission_service.inspect(admission=admission)
        if not (
            contract_view.observation_contract_authority
            and admission_view.runtime_observation_input_authority
        ):
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_source_stale",
                "Contract、Admission 或 remote revision authority 已失效。",
            )
        return contract, admission, batches

    async def _view(
        self,
        receipt: EvolutionStablePromotionInstallationObservationAssessment,
        *,
        assessed_at: str,
    ) -> EvolutionStablePromotionInstallationObservationAssessmentView:
        source = await self.store.get(receipt.assessment_id)
        latest = await self.store.latest(admission_id=receipt.admission.admission_id)
        receipt_current = source == receipt
        latest_current = latest == receipt
        contract_authority = admission_authority = revision_authority = False
        ledger_current = False
        temporal_current = False
        current = None
        invalidation: list[str] = []
        try:
            contract, admission, batches = await self._authoritative_sources(
                receipt.admission.admission_id
            )
            contract_authority = contract == receipt.contract
            admission_authority = admission == receipt.admission
            revision_authority = True
            ledger_current = batches == receipt.batches
            temporal_current = _assessment_temporally_current(
                receipt,
                assessed_at=assessed_at,
            )
            if contract_authority and admission_authority and ledger_current and temporal_current:
                current = receipt
        except (
            EvolutionStablePromotionInstallationObservationAssessmentError,
            EvolutionStablePromotionObservationContractError,
            EvolutionStablePromotionRuntimeObservationAdmissionError,
            EvolutionStablePromotionObservationRevisionDeliveryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            invalidation.append("authoritative_source_unavailable")
        checks = {
            "assessment_source_changed": receipt_current,
            "newer_assessment_exists": latest_current,
            "observation_contract_stale": contract_authority,
            "runtime_admission_stale": admission_authority,
            "remote_revision_delivery_stale": revision_authority,
            "observation_ledger_stale": ledger_current,
            "assessment_expired": temporal_current,
        }
        invalidation.extend(reason for reason, passed in checks.items() if not passed)
        return EvolutionStablePromotionInstallationObservationAssessmentView(
            receipt=receipt,
            current_assessment=current,
            receipt_source_current=receipt_current,
            latest_receipt_current=latest_current,
            contract_authority=contract_authority,
            admission_authority=admission_authority,
            remote_revision_delivery_authority=revision_authority,
            observation_ledger_current=ledger_current,
            assessment_temporally_current=temporal_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            installation_long_term_health_authority=bool(
                all(checks.values())
                and current is not None
                and current.status is EvolutionStablePromotionInstallationObservationStatus.PASSING
            ),
            installation_health_alert_authority=bool(
                all(checks.values())
                and current is not None
                and current.status is EvolutionStablePromotionInstallationObservationStatus.BREACHED
            ),
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


def build_stable_promotion_installation_observation_assessment(
    *,
    contract: EvolutionStablePromotionObservationContract,
    admission: EvolutionStablePromotionRuntimeObservationAdmission,
    batches: tuple[EvolutionStablePromotionInstallationObservationBatch, ...],
    assessed_at: str | datetime,
) -> EvolutionStablePromotionInstallationObservationAssessment:
    item_contract = EvolutionStablePromotionObservationContract.model_validate(contract)
    item_admission = EvolutionStablePromotionRuntimeObservationAdmission.model_validate(admission)
    items = tuple(
        EvolutionStablePromotionInstallationObservationBatch.model_validate(item)
        for item in batches
    )
    timestamp = _aware(assessed_at).isoformat()
    projection = _evaluate(
        contract=item_contract,
        admission=item_admission,
        batches=items,
        assessed_at=timestamp,
    )
    core = {
        "schema_version": 1,
        "policy_version": (EVOLUTION_STABLE_PROMOTION_INSTALLATION_OBSERVATION_ASSESSMENT_POLICY),
        "workspace_root": item_admission.workspace_root,
        "contract": item_contract,
        "admission": item_admission,
        "batches": items,
        "assessed_at": timestamp,
        **projection,
        "remote_revision_chain_evaluated": True,
        "installation_long_term_metrics_recorded": True,
        "population_observation_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionInstallationObservationAssessment.model_validate(
        {
            **core,
            "assessment_id": f"evstableprominstallobserve_{digest[:24]}",
            "assessment_sha256": digest,
        }
    )


def render_stable_promotion_installation_observation_assessment(
    view: EvolutionStablePromotionInstallationObservationAssessmentView,
) -> str:
    current = view.current_assessment or view.receipt
    status_labels = {
        EvolutionStablePromotionInstallationObservationStatus.INSUFFICIENT: "不足",
        EvolutionStablePromotionInstallationObservationStatus.PASSING: "通过",
        EvolutionStablePromotionInstallationObservationStatus.BREACHED: "违约",
        EvolutionStablePromotionInstallationObservationStatus.CENSORED: "删失",
    }
    return "\n".join(
        (
            "## 稳定推广单安装长期观察",
            "",
            f"- Assessment：`{view.receipt.assessment_id}`",
            f"- Admission：`{current.admission.admission_id}`",
            f"- Installation：`{current.admission.installation_member_id}`",
            f"- 状态：**{status_labels[current.status]}（{current.status.value}）**",
            f"- Revision head：`{current.last_sequence}`",
            f"- 运行样本：`{current.operational_sample_count}` / "
            f"`{current.minimum_operational_samples}`",
            f"- 观察时长：`{current.observation_seconds}s` / "
            f"`{current.minimum_observation_seconds}s`",
            f"- 最大间隔 / 最新年龄：`{current.maximum_observed_gap_seconds}s` / "
            f"`{current.latest_age_seconds}s`",
            f"- 不足：`{', '.join(current.insufficient_reasons) or 'none'}`",
            f"- Breach：`{', '.join(current.breach_reasons) or 'none'}`",
            f"- Censor：`{', '.join(current.censor_reasons) or 'none'}`",
            f"- Ledger current：`{str(view.observation_ledger_current).lower()}`",
            f"- Assessment 时间有效：`{str(view.assessment_temporally_current).lower()}`",
            f"- 撤权原因：`{', '.join(view.invalidation_reasons) or 'none'}`",
            "- Installation health authority："
            f"`{str(view.installation_long_term_health_authority).lower()}`",
            "- Installation alert authority："
            f"`{str(view.installation_health_alert_authority).lower()}`",
            "- Population / Promoted Outcome authority：`false`",
        )
    )


def _evaluate(*, contract, admission, batches, assessed_at) -> dict[str, object]:
    if not batches or len(batches) > _MAX_BATCHES:
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_batch_bounds",
            "Remote revision batch 数量无效。",
        )
    if not (
        contract.workspace_root == admission.workspace_root
        and contract.contract_id == admission.observation_contract_id
        and contract.contract_sha256 == admission.observation_contract_sha256
        and contract.population_snapshot_id == admission.population_snapshot_id
        and contract.population_snapshot_sha256 == admission.population_snapshot_sha256
        and contract.population_finalization_receipt_id
        == admission.population_finalization_receipt_id
        and contract.population_finalization_receipt_sha256
        == admission.population_finalization_receipt_sha256
    ):
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_lineage_mismatch",
            "Observation Contract 与 Runtime Admission lineage 不一致。",
        )
    revisions = []
    prior_sequence = 0
    prior_revision_sha = ""
    for batch in batches:
        submission = batch.submission
        payload = submission.payload
        if not (
            payload.admission_id == admission.admission_id
            and payload.admission_sha256 == admission.admission_sha256
            and submission.signature.installation_member_id == admission.installation_member_id
            and payload.prior_remote_head_sequence == prior_sequence
            and payload.prior_remote_head_revision_sha256 == prior_revision_sha
            and payload.first_sequence == prior_sequence + 1
            and stable_promotion_observation_revision_receipt_matches_submission(
                batch.receipt,
                submission,
            )
        ):
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_remote_chain_broken",
                "Remote revision batch chain 不连续或 Receipt 无效。",
            )
        revisions.extend(payload.revisions)
        prior_sequence = payload.last_sequence
        prior_revision_sha = payload.revisions[-1].revision_sha256
    if not 1 <= len(revisions) <= min(_MAX_REVISIONS, contract.maximum_sample_count):
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_sample_bounds",
            "Remote revision sample 数量无效。",
        )
    maximum_gap = 0.0
    previous = None
    for expected_sequence, revision in enumerate(revisions, start=1):
        sample = revision.observation
        if not (
            revision.revision_sequence == expected_sequence
            and revision.admission_id == admission.admission_id
            and revision.admission_sha256 == admission.admission_sha256
            and revision.observation_contract_id == contract.contract_id
            and revision.observation_contract_sha256 == contract.contract_sha256
            and revision.installation_member_id == admission.installation_member_id
            and revision.binding_id == admission.binding_id
            and revision.binding_sha256 == admission.binding_sha256
            and revision.subject_id == admission.subject_id
            and sample.workspace_root == admission.workspace_root
            and sample.surface == admission.surface
            and sample.subject_id == admission.subject_id
            and sample.instance_id == admission.instance_id
            and sample.epoch == admission.epoch
            and sample.timeout_seconds == admission.timeout_seconds
            and sample.chain_origin_kind == contract.required_chain_origin_kind
            and sample.chain_origin_sequence == contract.required_chain_origin_sequence
            and _aware(sample.observed_at) >= _aware(contract.window_not_before_at)
        ):
            raise EvolutionStablePromotionInstallationObservationAssessmentError(
                "stable_promotion_installation_assessment_sample_binding_mismatch",
                "Remote revision sample 未绑定 exact admitted runtime。",
            )
        if previous is None:
            if not (
                sample.sample_id == admission.origin_sample_id
                and sample.sample_sha256 == admission.origin_sample_sha256
                and sample.phase.value == admission.origin_phase
                and not sample.previous_sample_sha256
            ):
                raise EvolutionStablePromotionInstallationObservationAssessmentError(
                    "stable_promotion_installation_assessment_origin_mismatch",
                    "Remote revision chain 未从 admitted startup origin 开始。",
                )
        else:
            if sample.previous_sample_sha256 != previous.sample_sha256:
                raise EvolutionStablePromotionInstallationObservationAssessmentError(
                    "stable_promotion_installation_assessment_sample_chain_broken",
                    "Remote observation sample hash chain 不连续。",
                )
            gap = (_aware(sample.observed_at) - _aware(previous.observed_at)).total_seconds()
            if gap < 0:
                raise EvolutionStablePromotionInstallationObservationAssessmentError(
                    "stable_promotion_installation_assessment_clock_regression",
                    "Remote observation sample 时间发生倒退。",
                )
            maximum_gap = max(maximum_gap, gap)
        previous = sample
    samples = tuple(revision.observation for revision in revisions)
    assessed = _aware(assessed_at)
    last_at = _aware(samples[-1].observed_at)
    if assessed < last_at:
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_predates_head",
            "Assessment time 不能早于 remote ledger head。",
        )
    operational = set(contract.operational_phases)
    last_phase = samples[-1].phase.value
    end = len(samples) if last_phase in operational else len(samples) - 1
    start = end
    while start > 0 and samples[start - 1].phase.value in operational:
        start -= 1
    operational_samples = samples[start:end]
    observation_seconds = (
        0
        if not operational_samples
        else min(
            604_800,
            max(
                0,
                int(
                    (
                        _aware(operational_samples[-1].observed_at)
                        - _aware(operational_samples[0].observed_at)
                    ).total_seconds()
                ),
            ),
        )
    )
    latest_age = max(0.0, (assessed - last_at).total_seconds())
    breaches: list[str] = []
    if any(sample.phase.value in contract.breach_phases for sample in samples):
        breaches.append("runtime_failed")
    if maximum_gap > admission.timeout_seconds:
        breaches.append("heartbeat_gap")
    terminal = last_phase in contract.censor_phases
    if not terminal and latest_age > admission.timeout_seconds:
        breaches.append("heartbeat_stale")
    breach_reasons = tuple(sorted(set(breaches)))
    censor_reasons = () if breach_reasons or not terminal else (f"runtime_{last_phase}",)
    insufficient: list[str] = []
    if not breach_reasons and not censor_reasons:
        if last_phase not in operational:
            insufficient.append("runtime_not_operational")
        if len(operational_samples) < contract.minimum_operational_samples:
            insufficient.append("minimum_operational_samples")
        if observation_seconds < contract.minimum_observation_seconds:
            insufficient.append("minimum_observation_seconds")
    insufficient_reasons = tuple(sorted(set(insufficient)))
    status = (
        EvolutionStablePromotionInstallationObservationStatus.BREACHED
        if breach_reasons
        else EvolutionStablePromotionInstallationObservationStatus.CENSORED
        if censor_reasons
        else EvolutionStablePromotionInstallationObservationStatus.INSUFFICIENT
        if insufficient_reasons
        else EvolutionStablePromotionInstallationObservationStatus.PASSING
    )
    return {
        "first_sequence": 1,
        "last_sequence": revisions[-1].revision_sequence,
        "ledger_head_revision_sha256": revisions[-1].revision_sha256,
        "ledger_head_receipt_id": batches[-1].receipt.receipt_id,
        "ledger_head_receipt_sha256": batches[-1].receipt.receipt_sha256,
        "first_observed_at": _aware(samples[0].observed_at).isoformat(),
        "last_observed_at": last_at.isoformat(),
        "sample_count": len(samples),
        "operational_sample_count": len(operational_samples),
        "observation_seconds": observation_seconds,
        "minimum_observation_seconds": contract.minimum_observation_seconds,
        "minimum_operational_samples": contract.minimum_operational_samples,
        "maximum_gap_seconds": admission.timeout_seconds,
        "maximum_observed_gap_seconds": min(86_400, math.ceil(maximum_gap)),
        "latest_age_seconds": min(604_800, math.ceil(latest_age)),
        "insufficient_reasons": insufficient_reasons,
        "breach_reasons": breach_reasons,
        "censor_reasons": censor_reasons,
        "status": status,
        "historical_installation_health_passed": (
            status is EvolutionStablePromotionInstallationObservationStatus.PASSING
        ),
        "installation_health_alert_recorded": (
            status is EvolutionStablePromotionInstallationObservationStatus.BREACHED
        ),
    }


def _assessment_temporally_current(
    assessment: EvolutionStablePromotionInstallationObservationAssessment,
    *,
    assessed_at: str | datetime,
) -> bool:
    if assessment.status not in {
        EvolutionStablePromotionInstallationObservationStatus.INSUFFICIENT,
        EvolutionStablePromotionInstallationObservationStatus.PASSING,
    }:
        return True
    deadline = _aware(assessment.last_observed_at).timestamp() + assessment.maximum_gap_seconds
    return _aware(assessed_at).timestamp() <= deadline


async def _require_exact_sources(db, item) -> None:
    contract = await (
        await db.execute(
            "SELECT contract_json FROM evolution_stable_promotion_observation_contracts "
            "WHERE contract_id = ?",
            (item.contract.contract_id,),
        )
    ).fetchone()
    admission = await (
        await db.execute(
            "SELECT admission_json FROM evolution_stable_promotion_runtime_admissions "
            "WHERE admission_id = ?",
            (item.admission.admission_id,),
        )
    ).fetchone()
    rows = await (
        await db.execute(
            "SELECT submission_json, receipt_json FROM "
            "evolution_stable_promotion_observation_revision_receipts "
            "WHERE admission_id = ? ORDER BY first_sequence",
            (item.admission.admission_id,),
        )
    ).fetchall()
    durable_batches = tuple((str(row["submission_json"]), str(row["receipt_json"])) for row in rows)
    expected_batches = tuple(
        (batch.submission.model_dump_json(), batch.receipt.model_dump_json())
        for batch in item.batches
    )
    if not (
        contract is not None
        and hmac.compare_digest(str(contract["contract_json"]), item.contract.model_dump_json())
        and admission is not None
        and hmac.compare_digest(str(admission["admission_json"]), item.admission.model_dump_json())
        and durable_batches == expected_batches
    ):
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_source_changed",
            "Contract、Admission 或 remote revision durable source 已变化。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_promotion_installation_observation_assessments ("
        "assessment_id TEXT PRIMARY KEY, assessment_sha256 TEXT NOT NULL, "
        "contract_id TEXT NOT NULL, admission_id TEXT NOT NULL, "
        "installation_member_id TEXT NOT NULL, subject_id TEXT NOT NULL, "
        "status TEXT NOT NULL, last_sequence INTEGER NOT NULL, "
        "ledger_head_revision_sha256 TEXT NOT NULL, assessment_json TEXT NOT NULL, "
        "assessed_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_stable_promotion_installation_assessment_admission ON "
        "evolution_stable_promotion_installation_observation_assessments "
        "(admission_id, last_sequence, assessed_at)"
    )


def _row_assessment(row) -> EvolutionStablePromotionInstallationObservationAssessment:
    try:
        item = EvolutionStablePromotionInstallationObservationAssessment.model_validate_json(
            row["assessment_json"]
        )
        if not (
            row["assessment_id"] == item.assessment_id
            and row["assessment_sha256"] == item.assessment_sha256
            and row["contract_id"] == item.contract.contract_id
            and row["admission_id"] == item.admission.admission_id
            and row["installation_member_id"] == item.admission.installation_member_id
            and row["subject_id"] == item.admission.subject_id
            and row["status"] == item.status.value
            and int(row["last_sequence"]) == item.last_sequence
            and row["ledger_head_revision_sha256"] == item.ledger_head_revision_sha256
            and row["assessed_at"] == item.assessed_at
        ):
            raise ValueError("assessment row identity mismatch")
        return item
    except ValueError as exc:
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_store_corrupt",
            "Installation observation assessment durable row 已损坏。",
        ) from exc


def _assessment(value) -> EvolutionStablePromotionInstallationObservationAssessment:
    try:
        return EvolutionStablePromotionInstallationObservationAssessment.model_validate(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionInstallationObservationAssessmentError(
            "stable_promotion_installation_assessment_invalid",
            "Installation observation assessment 无效。",
        ) from exc


def _assessment_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _ASSESSMENT_RE.fullmatch(normalized) is None:
        raise ValueError("Installation observation Assessment ID 无效。")
    return normalized


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _ADMISSION_RE.fullmatch(normalized) is None:
        raise ValueError("Runtime Observation Admission ID 无效。")
    return normalized


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Installation observation timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_INSTALLATION_OBSERVATION_ASSESSMENT_POLICY",
    "EvolutionStablePromotionInstallationObservationAssessment",
    "EvolutionStablePromotionInstallationObservationAssessmentError",
    "EvolutionStablePromotionInstallationObservationAssessmentService",
    "EvolutionStablePromotionInstallationObservationAssessmentStore",
    "EvolutionStablePromotionInstallationObservationAssessmentView",
    "EvolutionStablePromotionInstallationObservationBatch",
    "EvolutionStablePromotionInstallationObservationStatus",
    "build_stable_promotion_installation_observation_assessment",
    "render_stable_promotion_installation_observation_assessment",
]
