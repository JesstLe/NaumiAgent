"""Durable long-term runtime windows after an exact rollback admission."""

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

from naumi_agent.evolution.post_rollback_long_term_observation_contracts import (
    EvolutionPostRollbackLongTermObservationContract,
    EvolutionPostRollbackLongTermObservationContractError,
    EvolutionPostRollbackLongTermObservationContractService,
    EvolutionPostRollbackLongTermObservationContractStore,
)
from naumi_agent.evolution.post_rollback_runtime_observation_admissions import (
    EvolutionPostRollbackRuntimeObservationAdmission,
    EvolutionPostRollbackRuntimeObservationAdmissionError,
    EvolutionPostRollbackRuntimeObservationAdmissionService,
    EvolutionPostRollbackRuntimeObservationAdmissionStore,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import HarnessRuntimeReleaseBinding
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_ASSESSMENT_POLICY = (
    "evolution-post-rollback-long-term-observation-assessment-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SAMPLES = 5_000
_PAGE_LIMIT = 500
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class EvolutionPostRollbackLongTermObservationStatus(StrEnum):
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


class EvolutionPostRollbackLongTermObservationAssessment(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-post-rollback-long-term-observation-assessment-v1"
    ] = EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_ASSESSMENT_POLICY
    assessment_id: str = Field(pattern=r"^evpostobservewindow_[0-9a-f]{24}$")
    assessment_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract: EvolutionPostRollbackLongTermObservationContract
    admission: EvolutionPostRollbackRuntimeObservationAdmission
    samples: tuple[HarnessRuntimeReleaseObservation, ...] = Field(
        min_length=1,
        max_length=_MAX_SAMPLES,
    )
    assessed_at: str = Field(min_length=1, max_length=100)
    sample_scope: Literal["origin", "suffix"]
    slice_anchor_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    ledger_head_sequence: int = Field(ge=1)
    ledger_head_sha256: str = Field(pattern=_SHA256_RE)
    first_observed_at: str = Field(min_length=1, max_length=100)
    last_observed_at: str = Field(min_length=1, max_length=100)
    sample_count: int = Field(ge=1, le=_MAX_SAMPLES)
    operational_sample_count: int = Field(ge=0, le=_MAX_SAMPLES)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: Literal[3600] = 3600
    minimum_operational_samples: Literal[12] = 12
    maximum_gap_seconds: int = Field(ge=3, le=86_400)
    maximum_observed_gap_seconds: int = Field(ge=0, le=86_400)
    latest_age_seconds: int = Field(ge=0, le=604_800)
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    censor_reasons: tuple[str, ...] = Field(max_length=4)
    status: EvolutionPostRollbackLongTermObservationStatus
    observation_window_evaluated: Literal[True] = True
    long_term_metrics_recorded: Literal[True] = True
    historical_long_term_health_passed: bool
    health_alert_recorded: bool
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("长期观察评估 workspace 必须 canonical。")
        projection = _evaluate(
            contract=self.contract,
            admission=self.admission,
            samples=self.samples,
            assessed_at=self.assessed_at,
        )
        fields = (
            "sample_scope",
            "slice_anchor_sha256",
            "first_sequence",
            "last_sequence",
            "ledger_head_sequence",
            "ledger_head_sha256",
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
            "historical_long_term_health_passed",
            "health_alert_recorded",
        )
        if any(getattr(self, name) != projection[name] for name in fields):
            raise ValueError("长期观察评估 evidence projection 不一致。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"assessment_id", "assessment_sha256"},
            )
        )
        if not (
            hmac.compare_digest(self.assessment_sha256, digest)
            and self.assessment_id == f"evpostobservewindow_{digest[:24]}"
        ):
            raise ValueError("长期观察评估 content identity 不一致。")
        return self


class EvolutionPostRollbackLongTermObservationAssessmentView(_StrictModel):
    receipt: EvolutionPostRollbackLongTermObservationAssessment
    current_assessment: EvolutionPostRollbackLongTermObservationAssessment | None
    receipt_source_current: bool
    latest_receipt_current: bool
    contract_authority: bool
    admission_authority: bool
    release_binding_current: bool
    observation_ledger_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    long_term_health_authority: bool
    health_alert_authority: bool
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
            and self.release_binding_current
            and self.observation_ledger_current
            and self.current_assessment is not None
        )
        passing = bool(
            durable
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionPostRollbackLongTermObservationStatus.PASSING
        )
        breached = bool(
            durable
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionPostRollbackLongTermObservationStatus.BREACHED
        )
        if not (
            self.long_term_health_authority is passing
            and self.health_alert_authority is breached
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("长期观察评估 authority projection 不一致。")
        return self


class EvolutionPostRollbackLongTermObservationAssessmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackLongTermObservationAssessmentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        contract_store: EvolutionPostRollbackLongTermObservationContractStore,
        admission_store: EvolutionPostRollbackRuntimeObservationAdmissionStore,
        harness_store: HarnessStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        if not (
            self.db_path == contract_store.db_path == admission_store.db_path
        ):
            raise ValueError("长期观察评估 sources 必须共享 session SQLite。")
        self.contract_store = contract_store
        self.admission_store = admission_store
        self.harness_store = harness_store

    async def get(
        self,
        assessment_id: str,
    ) -> EvolutionPostRollbackLongTermObservationAssessment | None:
        identifier = _assessment_id(assessment_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_assessments "
                        "WHERE assessment_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionPostRollbackLongTermObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_store_corrupt",
                "Post-Rollback 长期观察评估损坏或无法读取。",
            ) from exc

    async def latest(
        self,
        *,
        admission_id: str,
    ) -> EvolutionPostRollbackLongTermObservationAssessment | None:
        identifier = _admission_id(admission_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_assessments "
                        "WHERE admission_id = ? "
                        "ORDER BY ledger_head_sequence DESC, assessed_at DESC, "
                        "assessment_id DESC LIMIT 1",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_assessment(row)
        except EvolutionPostRollbackLongTermObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_store_corrupt",
                "Post-Rollback 长期观察评估损坏或无法读取。",
            ) from exc

    async def record(
        self,
        assessment: EvolutionPostRollbackLongTermObservationAssessment,
    ) -> EvolutionPostRollbackLongTermObservationAssessment:
        item = _assessment(assessment)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_oversized",
                "Post-Rollback 长期观察评估超过 8 MiB。",
            )
        await self._require_harness_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                contract = await (
                    await db.execute(
                        "SELECT contract_json FROM "
                        "evolution_post_rollback_observation_contracts "
                        "WHERE contract_id = ?",
                        (item.contract.contract_id,),
                    )
                ).fetchone()
                admission = await (
                    await db.execute(
                        "SELECT admission_json FROM "
                        "evolution_post_rollback_runtime_admissions "
                        "WHERE admission_id = ?",
                        (item.admission.admission_id,),
                    )
                ).fetchone()
                if not (
                    contract is not None
                    and hmac.compare_digest(
                        str(contract["contract_json"]),
                        item.contract.model_dump_json(),
                    )
                    and admission is not None
                    and hmac.compare_digest(
                        str(admission["admission_json"]),
                        item.admission.model_dump_json(),
                    )
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackLongTermObservationAssessmentError(
                        "post_rollback_long_term_assessment_source_changed",
                        "长期观察 Contract/Admission durable source 已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_assessments "
                        "WHERE assessment_id = ?",
                        (item.assessment_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _row_assessment(existing)
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionPostRollbackLongTermObservationAssessmentError(
                        "post_rollback_long_term_assessment_identity_conflict",
                        "同一长期观察评估 identity 已绑定不同内容。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_long_term_assessments "
                    "(assessment_id, assessment_sha256, outcome_id, request_id, "
                    "contract_id, admission_id, subject_id, status, "
                    "ledger_head_sequence, assessment_json, assessed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.assessment_id,
                        item.assessment_sha256,
                        item.contract.outcome_id,
                        item.contract.request_id,
                        item.contract.contract_id,
                        item.admission.admission_id,
                        item.admission.subject_id,
                        item.status.value,
                        item.ledger_head_sequence,
                        encoded,
                        item.assessed_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackLongTermObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_store_failed",
                "Post-Rollback 长期观察评估无法持久化。",
            ) from exc

    async def _require_harness_sources(
        self,
        item: EvolutionPostRollbackLongTermObservationAssessment,
    ) -> None:
        try:
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=item.workspace_root,
                subject_id=item.admission.subject_id,
            )
            actual = await _read_exact_slice(
                harness_store=self.harness_store,
                assessment=item,
            )
            if not (
                binding is not None
                and binding.binding_id == item.admission.binding_id
                and binding.binding_sha256 == item.admission.binding_sha256
                and actual == item.samples
            ):
                raise ValueError("assessment harness source changed")
        except (
            EvolutionPostRollbackLongTermObservationAssessmentError,
            HarnessStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_harness_source_changed",
                "长期观察 Harness ledger source 已变化或无法验证。",
            ) from exc


class EvolutionPostRollbackLongTermObservationAssessmentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionPostRollbackLongTermObservationContractStore,
        contract_service: EvolutionPostRollbackLongTermObservationContractService,
        admission_store: EvolutionPostRollbackRuntimeObservationAdmissionStore,
        admission_service: EvolutionPostRollbackRuntimeObservationAdmissionService,
        harness_store: HarnessStore,
        store: EvolutionPostRollbackLongTermObservationAssessmentStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            store.contract_store is contract_store
            and store.admission_store is admission_store
            and store.harness_store is harness_store
        ):
            raise ValueError("长期观察评估 Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.admission_store = admission_store
        self.admission_service = admission_service
        self.harness_store = harness_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def assess(
        self,
        *,
        request_id: str,
        subject_id: str,
    ) -> EvolutionPostRollbackLongTermObservationAssessmentView:
        request = _request_id(request_id)
        subject = _subject_id(subject_id)
        key = f"{request}:{subject}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            admission_view = await self.admission_service.record(
                request_id=request,
                subject_id=subject,
            )
            if not admission_view.runtime_observation_input_authority:
                raise EvolutionPostRollbackLongTermObservationAssessmentError(
                    "post_rollback_long_term_admission_stale",
                    "Runtime Observation Admission authority 已失效。",
                )
            admission = admission_view.admission
            contract = await self.contract_store.get_by_outcome(admission.outcome_id)
            if contract is None:
                raise EvolutionPostRollbackLongTermObservationAssessmentError(
                    "post_rollback_long_term_contract_missing",
                    "缺少长期观察契约。",
                )
            binding, samples = await _read_current_ledger(
                harness_store=self.harness_store,
                workspace_root=self.workspace_root,
                admission=admission,
            )
            if not (
                binding.binding_id == admission.binding_id
                and binding.binding_sha256 == admission.binding_sha256
            ):
                raise EvolutionPostRollbackLongTermObservationAssessmentError(
                    "post_rollback_long_term_binding_changed",
                    "Runtime Release Binding 已变化。",
                )
            assessed_at = self._now().isoformat()
            artifact = build_post_rollback_long_term_observation_assessment(
                contract=contract,
                admission=admission,
                samples=samples,
                assessed_at=assessed_at,
            )
            latest = await self.store.latest(admission_id=admission.admission_id)
            recorded = (
                latest
                if latest is not None and _same_material_assessment(latest, artifact)
                else await self.store.record(artifact)
            )
            return await self._view(recorded, assessed_at=assessed_at)

    async def inspect(
        self,
        *,
        admission_id: str,
    ) -> EvolutionPostRollbackLongTermObservationAssessmentView:
        latest = await self.store.latest(admission_id=admission_id)
        if latest is None:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_assessment_missing",
                "尚未形成 Post-Rollback 长期观察评估。",
            )
        return await self._view(latest, assessed_at=self._now().isoformat())

    async def _view(
        self,
        receipt: EvolutionPostRollbackLongTermObservationAssessment,
        *,
        assessed_at: str,
    ) -> EvolutionPostRollbackLongTermObservationAssessmentView:
        source = await self.store.get(receipt.assessment_id)
        latest = await self.store.latest(admission_id=receipt.admission.admission_id)
        receipt_current = source == receipt
        latest_current = latest == receipt
        contract_authority = admission_authority = False
        binding_current = ledger_current = False
        current = None
        invalidation: list[str] = []
        try:
            contract_view = await self.contract_service.inspect(
                contract=receipt.contract
            )
            contract_authority = contract_view.observation_contract_authority
        except EvolutionPostRollbackLongTermObservationContractError:
            invalidation.append("observation_contract_unavailable")
        try:
            admission_view = await self.admission_service.inspect(
                admission=receipt.admission
            )
            admission_authority = (
                admission_view.runtime_observation_input_authority
            )
        except EvolutionPostRollbackRuntimeObservationAdmissionError:
            invalidation.append("runtime_admission_unavailable")
        try:
            binding, samples = await _read_current_ledger(
                harness_store=self.harness_store,
                workspace_root=self.workspace_root,
                admission=receipt.admission,
            )
            binding_current = bool(
                binding.binding_id == receipt.admission.binding_id
                and binding.binding_sha256 == receipt.admission.binding_sha256
            )
            if binding_current:
                current = build_post_rollback_long_term_observation_assessment(
                    contract=receipt.contract,
                    admission=receipt.admission,
                    samples=samples,
                    assessed_at=assessed_at,
                )
                ledger_current = True
        except (
            EvolutionPostRollbackLongTermObservationAssessmentError,
            HarnessStoreError,
        ):
            invalidation.append("observation_ledger_invalid")
        checks = {
            "assessment_source_changed": receipt_current,
            "newer_assessment_exists": latest_current,
            "observation_contract_stale": contract_authority,
            "runtime_admission_stale": admission_authority,
            "release_binding_changed": binding_current,
            "observation_ledger_stale": ledger_current,
        }
        invalidation.extend(reason for reason, passed in checks.items() if not passed)
        durable = all(checks.values()) and current is not None
        passing = bool(
            durable
            and current is not None
            and current.status
            is EvolutionPostRollbackLongTermObservationStatus.PASSING
        )
        breached = bool(
            durable
            and current is not None
            and current.status
            is EvolutionPostRollbackLongTermObservationStatus.BREACHED
        )
        return EvolutionPostRollbackLongTermObservationAssessmentView(
            receipt=receipt,
            current_assessment=current,
            receipt_source_current=receipt_current,
            latest_receipt_current=latest_current,
            contract_authority=contract_authority,
            admission_authority=admission_authority,
            release_binding_current=binding_current,
            observation_ledger_current=ledger_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            long_term_health_authority=passing,
            health_alert_authority=breached,
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


def build_post_rollback_long_term_observation_assessment(
    *,
    contract: EvolutionPostRollbackLongTermObservationContract,
    admission: EvolutionPostRollbackRuntimeObservationAdmission,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> EvolutionPostRollbackLongTermObservationAssessment:
    try:
        typed_contract = EvolutionPostRollbackLongTermObservationContract.model_validate_json(
            contract.model_dump_json()
        )
        typed_admission = EvolutionPostRollbackRuntimeObservationAdmission.model_validate_json(
            admission.model_dump_json()
        )
        typed_samples = tuple(
            HarnessRuntimeReleaseObservation.model_validate_json(item.model_dump_json())
            for item in samples
        )
        assessed = _aware(assessed_at).isoformat()
        projection = _evaluate(
            contract=typed_contract,
            admission=typed_admission,
            samples=typed_samples,
            assessed_at=assessed,
        )
    except EvolutionPostRollbackLongTermObservationAssessmentError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_assessment_input_invalid",
            "Post-Rollback 长期观察评估输入无效。",
        ) from exc
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_ASSESSMENT_POLICY,
        "workspace_root": typed_contract.workspace_root,
        "contract": typed_contract.model_dump(mode="json"),
        "admission": typed_admission.model_dump(mode="json"),
        "samples": [item.model_dump(mode="json") for item in typed_samples],
        "assessed_at": assessed,
        **{
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in projection.items()
        },
        "observation_window_evaluated": True,
        "long_term_metrics_recorded": True,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackLongTermObservationAssessment.model_validate(
        {
            **core,
            "contract": typed_contract,
            "admission": typed_admission,
            "samples": typed_samples,
            "assessment_id": f"evpostobservewindow_{digest[:24]}",
            "assessment_sha256": digest,
        }
    )


def _evaluate(
    *,
    contract: EvolutionPostRollbackLongTermObservationContract,
    admission: EvolutionPostRollbackRuntimeObservationAdmission,
    samples: tuple[HarnessRuntimeReleaseObservation, ...],
    assessed_at: str,
) -> dict[str, object]:
    if not 1 <= len(samples) <= contract.maximum_sample_count == _MAX_SAMPLES:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_sample_count_invalid",
            "长期观察样本数必须在 1 到 5000 之间。",
        )
    if not (
        admission.workspace_root == contract.workspace_root
        and admission.outcome_id == contract.outcome_id
        and admission.outcome_sha256 == contract.outcome_sha256
        and admission.request_id == contract.request_id
        and admission.observation_contract_id == contract.contract_id
        and admission.observation_contract_sha256 == contract.contract_sha256
        and admission.runtime_observation_input_recorded
        and not admission.observation_window_authority
    ):
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_admission_mismatch",
            "长期观察 Admission 未绑定 exact Contract。",
        )
    first = samples[0]
    origin_scope = first.sample_id == admission.origin_sample_id
    suffix_scope = bool(
        first.heartbeat_sequence > admission.origin_sequence
        and first.previous_sample_sha256
        and first.chain_origin_kind == "startup"
        and first.chain_origin_sequence == admission.origin_sequence
    )
    if not (origin_scope or suffix_scope):
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_slice_origin_invalid",
            "长期观察 slice 未从 exact Admission origin 或可验证 suffix 开始。",
        )
    previous = None
    maximum_gap = 0.0
    for item in samples:
        if not (
            item.workspace_root == contract.workspace_root
            and item.binding_id == admission.binding_id
            and item.binding_sha256 == admission.binding_sha256
            and item.runtime_identity_id == admission.runtime_identity_id
            and item.runtime_identity_sha256 == admission.runtime_identity_sha256
            and item.surface == admission.surface
            and item.subject_id == admission.subject_id
            and item.instance_id == admission.instance_id
            and item.epoch == admission.epoch
            and item.timeout_seconds == admission.timeout_seconds
            and item.chain_origin_kind == "startup"
            and item.chain_origin_sequence == admission.origin_sequence
        ):
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_sample_binding_mismatch",
                "长期观察 sample 未绑定 exact admitted runtime。",
            )
        if _aware(item.observed_at) < _aware(contract.window_not_before_at):
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_sample_predates_contract",
                "长期观察 sample 早于 Contract not-before time。",
            )
        if previous is not None:
            if not (
                item.heartbeat_sequence == previous.heartbeat_sequence + 1
                and item.previous_sample_sha256 == previous.sample_sha256
            ):
                raise EvolutionPostRollbackLongTermObservationAssessmentError(
                    "post_rollback_long_term_sample_chain_broken",
                    "长期观察 sample sequence/hash chain 不连续。",
                )
            gap = (_aware(item.observed_at) - _aware(previous.observed_at)).total_seconds()
            if gap < 0:
                raise EvolutionPostRollbackLongTermObservationAssessmentError(
                    "post_rollback_long_term_clock_regression",
                    "长期观察 sample 时间发生倒退。",
                )
            maximum_gap = max(maximum_gap, gap)
        previous = item
    assessed = _aware(assessed_at)
    last_at = _aware(samples[-1].observed_at)
    if assessed < last_at:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_assessment_predates_head",
            "长期观察 assessed_at 不能早于 ledger head。",
        )
    operational = set(contract.operational_phases)
    last_phase = str(samples[-1].phase)
    end = len(samples) if last_phase in operational else len(samples) - 1
    start = end
    while start > 0 and str(samples[start - 1].phase) in operational:
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
    latest_age_exact = max(0.0, (assessed - last_at).total_seconds())
    breaches: list[str] = []
    if any(str(item.phase) in contract.breach_phases for item in samples):
        breaches.append("runtime_failed")
    if maximum_gap > admission.timeout_seconds:
        breaches.append("heartbeat_gap")
    terminal = last_phase in contract.censor_phases
    if not terminal and latest_age_exact > admission.timeout_seconds:
        breaches.append("heartbeat_stale")
    breach_reasons = tuple(sorted(set(breaches)))
    censor_reasons = (
        ()
        if breach_reasons or not terminal
        else (f"runtime_{last_phase}",)
    )
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
        EvolutionPostRollbackLongTermObservationStatus.BREACHED
        if breach_reasons
        else EvolutionPostRollbackLongTermObservationStatus.CENSORED
        if censor_reasons
        else EvolutionPostRollbackLongTermObservationStatus.INSUFFICIENT
        if insufficient_reasons
        else EvolutionPostRollbackLongTermObservationStatus.PASSING
    )
    return {
        "sample_scope": "origin" if origin_scope else "suffix",
        "slice_anchor_sha256": "" if origin_scope else first.previous_sample_sha256,
        "first_sequence": first.heartbeat_sequence,
        "last_sequence": samples[-1].heartbeat_sequence,
        "ledger_head_sequence": samples[-1].heartbeat_sequence,
        "ledger_head_sha256": samples[-1].sample_sha256,
        "first_observed_at": _aware(first.observed_at).isoformat(),
        "last_observed_at": last_at.isoformat(),
        "sample_count": len(samples),
        "operational_sample_count": len(operational_samples),
        "observation_seconds": observation_seconds,
        "minimum_observation_seconds": contract.minimum_observation_seconds,
        "minimum_operational_samples": contract.minimum_operational_samples,
        "maximum_gap_seconds": admission.timeout_seconds,
        "maximum_observed_gap_seconds": min(86_400, math.ceil(maximum_gap)),
        "latest_age_seconds": min(604_800, math.ceil(latest_age_exact)),
        "insufficient_reasons": insufficient_reasons,
        "breach_reasons": breach_reasons,
        "censor_reasons": censor_reasons,
        "status": status,
        "historical_long_term_health_passed": (
            status is EvolutionPostRollbackLongTermObservationStatus.PASSING
        ),
        "health_alert_recorded": (
            status is EvolutionPostRollbackLongTermObservationStatus.BREACHED
        ),
    }


async def _read_current_ledger(
    *,
    harness_store: HarnessStore,
    workspace_root: str | Path,
    admission: EvolutionPostRollbackRuntimeObservationAdmission,
) -> tuple[HarnessRuntimeReleaseBinding, tuple[HarnessRuntimeReleaseObservation, ...]]:
    binding = await harness_store.get_runtime_release_binding(
        workspace_root=workspace_root,
        subject_id=admission.subject_id,
    )
    heartbeat = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=admission.subject_id,
    )
    if binding is None or heartbeat is None:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_ledger_head_missing",
            "缺少 admitted runtime Binding 或 heartbeat head。",
        )
    first_sequence = max(admission.origin_sequence, heartbeat.sequence - _MAX_SAMPLES + 1)
    after_sequence = 0 if first_sequence == admission.origin_sequence else first_sequence - 1
    samples: list[HarnessRuntimeReleaseObservation] = []
    while after_sequence < heartbeat.sequence:
        page = await harness_store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=admission.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, heartbeat.sequence - after_sequence),
        )
        if page is None or not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
        ):
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_ledger_binding_mismatch",
                "Observation page 未绑定 exact Runtime Binding。",
            )
        if not page.items or page.items[-1].heartbeat_sequence <= after_sequence:
            raise EvolutionPostRollbackLongTermObservationAssessmentError(
                "post_rollback_long_term_ledger_cursor_invalid",
                "Observation page cursor 未前进。",
            )
        samples.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
        if after_sequence == heartbeat.sequence:
            break
    if not samples or len(samples) > _MAX_SAMPLES:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_samples_missing",
            "Observation ledger 没有可评估的 bounded samples。",
        )
    heartbeat_after = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=admission.subject_id,
    )
    binding_after = await harness_store.get_runtime_release_binding(
        workspace_root=workspace_root,
        subject_id=admission.subject_id,
    )
    if not (
        heartbeat_after == heartbeat
        and binding_after == binding
        and _heartbeat_matches_sample(heartbeat, samples[-1])
    ):
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_ledger_head_changed",
            "Observation ledger 分页期间 head 或 Binding 已变化。",
        )
    return binding, tuple(samples)


async def _read_exact_slice(
    *,
    harness_store: HarnessStore,
    assessment: EvolutionPostRollbackLongTermObservationAssessment,
) -> tuple[HarnessRuntimeReleaseObservation, ...]:
    first = assessment.samples[0]
    after_sequence = (
        0
        if assessment.sample_scope == "origin"
        else first.heartbeat_sequence - 1
    )
    actual: list[HarnessRuntimeReleaseObservation] = []
    while len(actual) < len(assessment.samples):
        page = await harness_store.list_runtime_release_observations(
            workspace_root=assessment.workspace_root,
            subject_id=assessment.admission.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(assessment.samples) - len(actual)),
        )
        if page is None or not page.items:
            break
        actual.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
    return tuple(actual)


def render_post_rollback_long_term_observation_assessment(
    view: EvolutionPostRollbackLongTermObservationAssessmentView,
) -> str:
    current = view.current_assessment or view.receipt
    return "\n".join(
        [
            f"# Post-Rollback Long-Term Observation `{view.receipt.assessment_id}`",
            "",
            f"- 当前判定：`{current.status.value}`",
            f"- Runtime：`{current.admission.surface}` / `{current.admission.subject_id}`",
            f"- 样本：`{current.sample_count}`（operational `{current.operational_sample_count}`）",
            "- 持续时间："
            f"`{current.observation_seconds}s / {current.minimum_observation_seconds}s`",
            "- 最大间隙："
            f"`{current.maximum_observed_gap_seconds}s / {current.maximum_gap_seconds}s`",
            f"- 最新样本年龄：`{current.latest_age_seconds}s`",
            f"- Insufficient：`{', '.join(current.insufficient_reasons) or '-'}`",
            f"- Breach：`{', '.join(current.breach_reasons) or '-'}`",
            f"- Censor：`{', '.join(current.censor_reasons) or '-'}`",
            "- Long-term health authority："
            f"`{str(view.long_term_health_authority).lower()}`",
            f"- Health alert authority：`{str(view.health_alert_authority).lower()}`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _heartbeat_matches_sample(heartbeat, sample) -> bool:
    return bool(
        heartbeat.workspace_root == sample.workspace_root
        and heartbeat.subject_kind is HarnessRunKind.RUNTIME
        and heartbeat.subject_id == sample.subject_id
        and heartbeat.instance_id == sample.instance_id
        and heartbeat.epoch == sample.epoch
        and heartbeat.sequence == sample.heartbeat_sequence
        and str(heartbeat.phase) == str(sample.phase)
        and _aware(heartbeat.observed_at) == _aware(sample.observed_at)
        and heartbeat.timeout_seconds == sample.timeout_seconds
        and heartbeat.detail_code == sample.detail_code
    )


def _same_material_assessment(left, right) -> bool:
    return bool(
        left.contract == right.contract
        and left.admission == right.admission
        and left.samples == right.samples
        and left.status is right.status
        and left.insufficient_reasons == right.insufficient_reasons
        and left.breach_reasons == right.breach_reasons
        and left.censor_reasons == right.censor_reasons
    )


def _assessment(value) -> EvolutionPostRollbackLongTermObservationAssessment:
    try:
        return EvolutionPostRollbackLongTermObservationAssessment.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_assessment_invalid",
            "Post-Rollback 长期观察评估无效。",
        ) from exc


def _row_assessment(row) -> EvolutionPostRollbackLongTermObservationAssessment:
    raw = str(row["assessment_json"])
    if len(raw.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("assessment oversized")
    item = EvolutionPostRollbackLongTermObservationAssessment.model_validate_json(raw)
    if not (
        item.assessment_id == row["assessment_id"]
        and item.assessment_sha256 == row["assessment_sha256"]
        and item.admission.admission_id == row["admission_id"]
        and item.status.value == row["status"]
        and item.ledger_head_sequence == row["ledger_head_sequence"]
    ):
        raise ValueError("assessment row mismatch")
    return item


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackreq_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _subject_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^[a-z][a-z0-9_-]{0,95}$", normalized) is None:
        raise EvolutionPostRollbackLongTermObservationAssessmentError(
            "post_rollback_long_term_subject_id_invalid",
            "Runtime Subject ID 格式无效。",
        )
    return normalized


def _assessment_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evpostobservewindow_[0-9a-f]{24}$", normalized) is None:
        raise ValueError("Long-Term Assessment ID 格式无效。")
    return normalized


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evpostobserveadmit_[0-9a-f]{24}$", normalized) is None:
        raise ValueError("Runtime Admission ID 格式无效。")
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("长期观察评估时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_long_term_assessments ("
        "assessment_id TEXT PRIMARY KEY, assessment_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL, request_id TEXT NOT NULL, "
        "contract_id TEXT NOT NULL, admission_id TEXT NOT NULL, "
        "subject_id TEXT NOT NULL, status TEXT NOT NULL, "
        "ledger_head_sequence INTEGER NOT NULL, assessment_json TEXT NOT NULL, "
        "assessed_at TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_long_term_latest "
        "ON evolution_post_rollback_long_term_assessments("
        "admission_id, ledger_head_sequence DESC, assessed_at DESC, "
        "assessment_id DESC);"
    )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_ASSESSMENT_POLICY",
    "EvolutionPostRollbackLongTermObservationAssessment",
    "EvolutionPostRollbackLongTermObservationAssessmentError",
    "EvolutionPostRollbackLongTermObservationAssessmentService",
    "EvolutionPostRollbackLongTermObservationAssessmentStore",
    "EvolutionPostRollbackLongTermObservationAssessmentView",
    "EvolutionPostRollbackLongTermObservationStatus",
    "build_post_rollback_long_term_observation_assessment",
    "render_post_rollback_long_term_observation_assessment",
]
