"""Admit one finalized stable member runtime into promotion observation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_deployments import (
    EvolutionRevalidationStableDeploymentReceipt,
)
from naumi_agent.evolution.revalidation_stable_runtime_exposures import (
    stable_runtime_identity_matches_deployment,
)
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationReceipt,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationMember,
    EvolutionStableRemotePopulationFinalizationService,
)
from naumi_agent.evolution.stable_rollback_readiness import (
    EvolutionStableDeploymentInspectionPort,
)
from naumi_agent.harness.runtime_release_binding import HarnessRuntimeReleaseBinding
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore

EVOLUTION_STABLE_PROMOTION_RUNTIME_OBSERVATION_ADMISSION_POLICY = (
    "evolution-stable-promotion-runtime-observation-admission-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTITY_RE = r"^[a-z][a-z0-9_-]{0,95}$"
_FINALIZATION_RE = re.compile(r"^evstableremotepopfinal_[0-9a-f]{24}$")
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")
_MAX_ARTIFACT_BYTES = 512 * 1024
_INSPECTION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionRuntimeObservationAdmission(_StrictModel):
    """Exact member, deployment, runtime binding, and startup origin."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-runtime-observation-admission-v1"
    ] = EVOLUTION_STABLE_PROMOTION_RUNTIME_OBSERVATION_ADMISSION_POLICY
    admission_id: str = Field(pattern=r"^evstablepromadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    observation_contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    population_finalization_receipt_id: str = Field(
        pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$"
    )
    population_finalization_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    member_source_sha256: str = Field(pattern=_SHA256_RE)
    member_receipt_id: str = Field(pattern=r"^evstableremotefinalreceipt_[0-9a-f]{24}$")
    member_receipt_sha256: str = Field(pattern=_SHA256_RE)
    release_finalization_id: str = Field(pattern=r"^relstablefinal_[0-9a-f]{24}$")
    release_finalization_sha256: str = Field(pattern=_SHA256_RE)
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    stable_intent_sha256: str = Field(pattern=_SHA256_RE)
    deployment_receipt_id: str = Field(pattern=r"^evrestabledeployment_[0-9a-f]{24}$")
    deployment_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    runtime_identity_id: str = Field(pattern=r"^relruntimeidentity_[0-9a-f]{24}$")
    runtime_identity_sha256: str = Field(pattern=_SHA256_RE)
    surface: Literal["new_ui", "tui"]
    subject_id: str = Field(pattern=_IDENTITY_RE)
    instance_id: str = Field(pattern=_IDENTITY_RE)
    epoch: int = Field(ge=1)
    pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    pointer_sha256: str = Field(pattern=_SHA256_RE)
    pointer_generation: int = Field(ge=2)
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    boot_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    version: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=255)
    origin_sample_id: str = Field(pattern=r"^hrreleaseobservation_[0-9a-f]{24}$")
    origin_sample_sha256: str = Field(pattern=_SHA256_RE)
    origin_sequence: Literal[1] = 1
    origin_kind: Literal["startup"] = "startup"
    origin_phase: Literal["starting"] = "starting"
    origin_observed_at: str = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(ge=3, le=86_400)
    population_member_verified: Literal[True] = True
    exact_stable_release_verified: Literal[True] = True
    startup_origin_verified: Literal[True] = True
    runtime_observation_input_recorded: Literal[True] = True
    observation_window_authority: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    admitted_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("稳定推广 Runtime Admission workspace 必须 canonical。")
        if _aware(self.origin_observed_at) > _aware(self.admitted_at):
            raise ValueError("稳定推广 Runtime Admission 不能早于 startup origin。")
        if any(
            (
                self.observation_window_authority,
                self.long_term_metrics_recorded,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("稳定推广 Runtime Admission 不得越权。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"admission_id", "admission_sha256"})
        )
        if not (
            hmac.compare_digest(self.admission_sha256, digest)
            and self.admission_id == f"evstablepromadmit_{digest[:24]}"
        ):
            raise ValueError("稳定推广 Runtime Admission content identity 不一致。")
        return self


class EvolutionStablePromotionRuntimeObservationAdmissionView(_StrictModel):
    admission: EvolutionStablePromotionRuntimeObservationAdmission
    status: Literal["admitted", "stale"]
    durable_admission_valid: bool
    observation_contract_authority: bool
    population_member_authority: bool
    active_deployment_authority: bool
    runtime_binding_authority: bool
    startup_origin_authority: bool
    exact_stable_release_authority: bool
    runtime_observation_input_authority: bool
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_admission_valid
            and self.observation_contract_authority
            and self.population_member_authority
            and self.active_deployment_authority
            and self.runtime_binding_authority
            and self.startup_origin_authority
            and self.exact_stable_release_authority
        )
        if not (
            self.runtime_observation_input_authority is expected
            and (self.status == "admitted") is expected
        ):
            raise ValueError("稳定推广 Runtime Admission authority projection 不一致。")
        return self


class EvolutionStablePromotionRuntimeObservationAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionRuntimeObservationAdmissionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        contract_id: str,
        installation_member_id: str,
        subject_id: str,
    ) -> EvolutionStablePromotionRuntimeObservationAdmission | None:
        contract = _contract_id(contract_id)
        member = _member_id(installation_member_id)
        subject = _subject_id(subject_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_runtime_admissions "
                        "WHERE observation_contract_id = ? AND "
                        "installation_member_id = ? AND subject_id = ?",
                        (contract, member, subject),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(str(row["admission_json"]))
            if not (
                item.admission_id == row["admission_id"]
                and item.admission_sha256 == row["admission_sha256"]
                and item.observation_contract_id == row["observation_contract_id"]
                and item.installation_member_id == row["installation_member_id"]
                and item.stable_intent_id == row["stable_intent_id"]
                and item.subject_id == row["subject_id"]
                and item.binding_id == row["binding_id"]
                and item.origin_sample_id == row["origin_sample_id"]
            ):
                raise ValueError("stable promotion runtime admission row mismatch")
            if not await _dependencies_current(self.db_path, item):
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_dependency_changed",
                    "稳定推广 Runtime Admission 的 durable dependency 已变化。",
                )
            return item
        except EvolutionStablePromotionRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_store_corrupt",
                "稳定推广 Runtime Admission 损坏或无法读取。",
            ) from exc

    async def get_by_id(
        self, admission_id: str
    ) -> EvolutionStablePromotionRuntimeObservationAdmission | None:
        item_id = _admission_id(admission_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_runtime_admissions "
                        "WHERE admission_id = ?",
                        (item_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(str(row["admission_json"]))
            if not (
                item.admission_id == row["admission_id"]
                and item.admission_sha256 == row["admission_sha256"]
                and item.observation_contract_id == row["observation_contract_id"]
                and item.installation_member_id == row["installation_member_id"]
                and item.stable_intent_id == row["stable_intent_id"]
                and item.subject_id == row["subject_id"]
                and item.binding_id == row["binding_id"]
                and item.origin_sample_id == row["origin_sample_id"]
            ):
                raise ValueError("stable promotion runtime admission row mismatch")
            if not await _dependencies_current(self.db_path, item):
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_dependency_changed",
                    "稳定推广 Runtime Admission 的 durable dependency 已变化。",
                )
            return item
        except EvolutionStablePromotionRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_store_corrupt",
                "稳定推广 Runtime Admission 损坏或无法读取。",
            ) from exc

    async def list_for_contract(
        self,
        contract_id: str,
    ) -> tuple[EvolutionStablePromotionRuntimeObservationAdmission, ...]:
        contract = _contract_id(contract_id)
        if not self.db_path.is_file():
            return ()
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                rows = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_runtime_admissions "
                        "WHERE observation_contract_id = ? ORDER BY "
                        "installation_member_id, admitted_at, admission_id LIMIT 10001",
                        (contract,),
                    )
                ).fetchall()
            if len(rows) > 10_000:
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_population_oversized",
                    "稳定推广 Runtime Admission 超过 10000 个 Population 成员上限。",
                )
            items = tuple(_restore(str(row["admission_json"])) for row in rows)
            if any(
                not (
                    item.admission_id == row["admission_id"]
                    and item.admission_sha256 == row["admission_sha256"]
                    and item.observation_contract_id == row["observation_contract_id"]
                    and item.installation_member_id == row["installation_member_id"]
                    and item.stable_intent_id == row["stable_intent_id"]
                    and item.subject_id == row["subject_id"]
                    and item.binding_id == row["binding_id"]
                    and item.origin_sample_id == row["origin_sample_id"]
                )
                for item, row in zip(items, rows, strict=True)
            ):
                raise ValueError("stable promotion runtime admission row mismatch")
            return items
        except EvolutionStablePromotionRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_store_corrupt",
                "稳定推广 Runtime Admission 集合损坏或无法读取。",
            ) from exc

    async def record(
        self, admission: EvolutionStablePromotionRuntimeObservationAdmission
    ) -> EvolutionStablePromotionRuntimeObservationAdmission:
        item = _admission(admission)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_oversized",
                "稳定推广 Runtime Admission 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                existing = await (
                    await db.execute(
                        "SELECT admission_json FROM "
                        "evolution_stable_promotion_runtime_admissions WHERE "
                        "observation_contract_id = ? AND installation_member_id = ? "
                        "AND subject_id = ?",
                        (
                            item.observation_contract_id,
                            item.installation_member_id,
                            item.subject_id,
                        ),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["admission_json"]))
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                        "stable_promotion_runtime_admission_conflict",
                        "同一 Contract/member/runtime subject 已绑定不同 Admission。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_runtime_admissions "
                    "(admission_id, admission_sha256, observation_contract_id, "
                    "population_finalization_receipt_id, installation_member_id, "
                    "stable_intent_id, subject_id, binding_id, origin_sample_id, "
                    "admission_json, admitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.admission_id,
                        item.admission_sha256,
                        item.observation_contract_id,
                        item.population_finalization_receipt_id,
                        item.installation_member_id,
                        item.stable_intent_id,
                        item.subject_id,
                        item.binding_id,
                        item.origin_sample_id,
                        encoded,
                        item.admitted_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_store_failed",
                "稳定推广 Runtime Admission 无法持久化。",
            ) from exc


class EvolutionStablePromotionRuntimeObservationAdmissionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionStablePromotionObservationContractStore,
        contract_service: EvolutionStablePromotionObservationContractService,
        finalization_service: EvolutionStableRemotePopulationFinalizationService,
        deployment_inspector: EvolutionStableDeploymentInspectionPort,
        evolution_db_path: str | Path,
        harness_store: HarnessStore,
        store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        db_path = Path(evolution_db_path).expanduser().resolve()
        paths = {
            contract_store.db_path,
            finalization_service.store.db_path,
            finalization_service.member_store.db_path,
            db_path,
            store.db_path,
        }
        if len(paths) != 1:
            raise ValueError("稳定推广 Runtime Admission sources 必须共享 session SQLite。")
        if not (
            contract_service.store is contract_store
            and contract_service.finalization_service is finalization_service
            and finalization_service.workspace_root == root
            and isinstance(
                deployment_inspector,
                EvolutionStableDeploymentInspectionPort,
            )
        ):
            raise ValueError("稳定推广 Runtime Admission authority composition 不一致。")
        self.workspace_root = root
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.finalization_service = finalization_service
        self.deployment_inspector = deployment_inspector
        self.harness_store = harness_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        finalization_receipt_id: str,
        stable_intent_id: str,
        subject_id: str,
    ) -> EvolutionStablePromotionRuntimeObservationAdmissionView:
        receipt_id = _finalization_id(finalization_receipt_id)
        intent_id = _intent_id(stable_intent_id)
        subject = _subject_id(subject_id)
        lock_key = f"{receipt_id}:{intent_id}:{subject}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            sources = await self._sources(
                receipt_id,
                intent_id,
                subject,
            )
            (
                contract_view,
                finalization_view,
                member,
                member_receipt,
                deployment_view,
                binding,
                origin,
            ) = sources
            if not (
                contract_view.observation_contract_authority
                and finalization_view.stable_population_finalization_authority
                and deployment_view.deployment_fact_authority
                and deployment_view.active_deployment_authority
            ):
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_source_stale",
                    "观察契约、Population member 或 Stable Deployment authority 已失效。",
                )
            proposed = _build_admission(
                contract_view.contract,
                member,
                member_receipt,
                deployment_view.receipt,
                binding,
                origin,
            )
            existing = await self.store.get(
                proposed.observation_contract_id,
                proposed.installation_member_id,
                proposed.subject_id,
            )
            if existing is None:
                existing = await self.store.record(proposed)
            elif existing != proposed:
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_conflict",
                    "既有 Admission 与当前 exact sources 不一致。",
                )
            view = await self.inspect(admission=existing)
            if not view.runtime_observation_input_authority:
                raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                    "stable_promotion_runtime_admission_authority_changed",
                    "Admission 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self, *, admission: EvolutionStablePromotionRuntimeObservationAdmission
    ) -> EvolutionStablePromotionRuntimeObservationAdmissionView:
        item = _admission(admission)
        durable = contract_authority = member_authority = False
        deployment_authority = binding_authority = origin_authority = False
        release_authority = False
        contract = member = member_receipt = deployment = binding = origin = None
        try:
            contract = await self.contract_store.get_by_finalization(
                item.population_finalization_receipt_id
            )
            if contract is not None:
                contract_view = await self.contract_service.inspect(contract=contract)
                contract_authority = bool(
                    contract_view.observation_contract_authority
                    and contract.contract_id == item.observation_contract_id
                    and contract.contract_sha256 == item.observation_contract_sha256
                )
        except _INSPECTION_ERRORS:
            contract = None
        try:
            finalization_view = await self.finalization_service.inspect(
                receipt_id=item.population_finalization_receipt_id
            )
            member = next(
                (
                    source
                    for source in finalization_view.receipt.members
                    if source.installation_member_id == item.installation_member_id
                ),
                None,
            )
            if member is not None:
                member_receipt = await self.finalization_service.member_store.get_receipt(
                    member.member_receipt_id
                )
            member_authority = bool(
                finalization_view.stable_population_finalization_authority
                and finalization_view.receipt.receipt_id
                == item.population_finalization_receipt_id
                and finalization_view.receipt.receipt_sha256
                == item.population_finalization_receipt_sha256
                and member is not None
                and member.member_source_sha256 == item.member_source_sha256
                and member.member_receipt_id == item.member_receipt_id
                and member.member_receipt_sha256 == item.member_receipt_sha256
                and member_receipt is not None
                and member_receipt.receipt_id == item.member_receipt_id
                and member_receipt.receipt_sha256 == item.member_receipt_sha256
            )
        except _INSPECTION_ERRORS:
            member = member_receipt = None
        try:
            deployment_view = await self.deployment_inspector.inspect_stable_deployment(
                intent_id=item.stable_intent_id
            )
            deployment = deployment_view.receipt
            deployment_authority = bool(
                deployment_view.deployment_fact_authority
                and deployment_view.active_deployment_authority
                and deployment.receipt_id == item.deployment_receipt_id
                and deployment.receipt_sha256 == item.deployment_receipt_sha256
                and deployment.preparation.intent.intent_id == item.stable_intent_id
                and deployment.preparation.intent.intent_sha256
                == item.stable_intent_sha256
            )
        except _INSPECTION_ERRORS:
            deployment = None
        try:
            binding, origin = await self._runtime_source(item.subject_id)
            binding_authority = bool(
                binding.binding_id == item.binding_id
                and binding.binding_sha256 == item.binding_sha256
                and binding.runtime_identity.identity_id == item.runtime_identity_id
                and binding.runtime_identity.identity_sha256
                == item.runtime_identity_sha256
            )
            origin_authority = bool(
                origin.sample_id == item.origin_sample_id
                and origin.sample_sha256 == item.origin_sample_sha256
            )
        except _INSPECTION_ERRORS:
            binding = origin = None
        try:
            if any(
                source is None
                for source in (
                    contract,
                    member,
                    member_receipt,
                    deployment,
                    binding,
                    origin,
                )
            ):
                raise ValueError("stable promotion runtime admission source missing")
            release_authority = _sources_match(
                contract,
                member,
                member_receipt,
                deployment,
                binding,
                origin,
            )
            rebuilt = _build_admission(
                contract,
                member,
                member_receipt,
                deployment,
                binding,
                origin,
            )
            stored = await self.store.get(
                item.observation_contract_id,
                item.installation_member_id,
                item.subject_id,
            )
            durable = stored == item and rebuilt == item
        except _INSPECTION_ERRORS:
            pass
        authority = bool(
            durable
            and contract_authority
            and member_authority
            and deployment_authority
            and binding_authority
            and origin_authority
            and release_authority
        )
        return EvolutionStablePromotionRuntimeObservationAdmissionView(
            admission=item,
            status="admitted" if authority else "stale",
            durable_admission_valid=durable,
            observation_contract_authority=contract_authority,
            population_member_authority=member_authority,
            active_deployment_authority=deployment_authority,
            runtime_binding_authority=binding_authority,
            startup_origin_authority=origin_authority,
            exact_stable_release_authority=release_authority,
            runtime_observation_input_authority=authority,
        )

    async def _sources(
        self,
        receipt_id: str,
        intent_id: str,
        subject_id: str,
    ):
        contract_view = await self.contract_service.record(
            finalization_receipt_id=receipt_id
        )
        finalization_view = await self.finalization_service.inspect(receipt_id=receipt_id)
        deployment_view = await self.deployment_inspector.inspect_stable_deployment(
            intent_id=intent_id
        )
        member_id = deployment_view.receipt.preparation.intent.proof.payload.member_id
        member = next(
            (
                item
                for item in finalization_view.receipt.members
                if item.installation_member_id == member_id
            ),
            None,
        )
        if member is None:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_member_missing",
                "Stable Intent 对应的 installation member 不在 Population Finalization 中。",
            )
        member_receipt = await self.finalization_service.member_store.get_receipt(
            member.member_receipt_id
        )
        if member_receipt is None:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_member_receipt_missing",
                "Population member 的 Remote Finalization Receipt 不存在。",
            )
        binding, origin = await self._runtime_source(subject_id)
        return (
            contract_view,
            finalization_view,
            member,
            member_receipt,
            deployment_view,
            binding,
            origin,
        )

    async def _runtime_source(
        self, subject_id: str
    ) -> tuple[HarnessRuntimeReleaseBinding, HarnessRuntimeReleaseObservation]:
        binding = await self.harness_store.get_runtime_release_binding(
            workspace_root=self.workspace_root,
            subject_id=subject_id,
        )
        page = await self.harness_store.list_runtime_release_observations(
            workspace_root=self.workspace_root,
            subject_id=subject_id,
            after_sequence=0,
            limit=1,
        )
        if binding is None or page is None or len(page.items) != 1:
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_source_missing",
                "找不到 exact managed runtime binding/startup origin。",
            )
        origin = page.items[0]
        if not _origin_matches(binding, origin):
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_origin_invalid",
                "Managed runtime origin 不是 exact startup sequence-1 sample。",
            )
        return binding, origin


def render_stable_promotion_runtime_observation_admission(
    view: EvolutionStablePromotionRuntimeObservationAdmissionView,
) -> str:
    item = view.admission
    return "\n".join(
        [
            f"# Stable Promotion Runtime Observation Admission `{item.admission_id}`",
            "",
            f"- 状态：`{view.status}`",
            f"- Observation Contract：`{item.observation_contract_id}`",
            f"- Population Member：`{item.installation_member_id}`",
            "- Stable Intent / Deployment："
            f"`{item.stable_intent_id}` / `{item.deployment_receipt_id}`",
            f"- Runtime Binding：`{item.binding_id}`",
            f"- Surface / Subject：`{item.surface}` / `{item.subject_id}`",
            f"- Instance / Epoch：`{item.instance_id}` / `{item.epoch}`",
            f"- Release：`{item.version}` · `{item.target}` · `{item.slot_id}`",
            f"- Startup Origin：`{item.origin_sample_id}` · sequence `1`",
            f"- Heartbeat timeout：`{item.timeout_seconds}s`",
            "- Contract / Member / Deployment authority："
            f"`{str(view.observation_contract_authority).lower()} / "
            f"{str(view.population_member_authority).lower()} / "
            f"{str(view.active_deployment_authority).lower()}`",
            "- Runtime observation input authority："
            f"`{str(view.runtime_observation_input_authority).lower()}`",
            "- Window / Long-term metrics / Promoted Outcome authority："
            "`false / false / false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_admission(
    contract: EvolutionStablePromotionObservationContract,
    member: EvolutionStableRemotePopulationFinalizationMember,
    member_receipt: EvolutionStableRemoteFinalizationReceipt,
    deployment: EvolutionRevalidationStableDeploymentReceipt,
    binding: HarnessRuntimeReleaseBinding,
    origin: HarnessRuntimeReleaseObservation,
) -> EvolutionStablePromotionRuntimeObservationAdmission:
    if not _sources_match(contract, member, member_receipt, deployment, binding, origin):
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_release_mismatch",
            "Population member、Stable Deployment 与 managed runtime release 不一致。",
        )
    if _aware(origin.observed_at) < _aware(contract.window_not_before_at):
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_origin_predates_finalization",
            "当前 runtime 早于 Population Finalization；请在 Finalization 后重启 Naumi，"
            "再准入新的 managed runtime subject。",
        )
    finalization = member_receipt.submission.result.release_finalization
    intent = deployment.preparation.intent
    identity = binding.runtime_identity
    admitted_at = max(
        _aware(contract.window_not_before_at),
        _aware(deployment.activated_at),
        _aware(binding.bound_at),
        _aware(origin.observed_at),
    ).isoformat()
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_RUNTIME_OBSERVATION_ADMISSION_POLICY,
        "workspace_root": contract.workspace_root,
        "observation_contract_id": contract.contract_id,
        "observation_contract_sha256": contract.contract_sha256,
        "population_finalization_receipt_id": contract.population_finalization_receipt_id,
        "population_finalization_receipt_sha256": contract.population_finalization_receipt_sha256,
        "population_snapshot_id": contract.population_snapshot_id,
        "population_snapshot_sha256": contract.population_snapshot_sha256,
        "installation_member_id": member.installation_member_id,
        "member_source_sha256": member.member_source_sha256,
        "member_receipt_id": member.member_receipt_id,
        "member_receipt_sha256": member.member_receipt_sha256,
        "release_finalization_id": finalization.finalization_id,
        "release_finalization_sha256": finalization.finalization_sha256,
        "stable_intent_id": intent.intent_id,
        "stable_intent_sha256": intent.intent_sha256,
        "deployment_receipt_id": deployment.receipt_id,
        "deployment_receipt_sha256": deployment.receipt_sha256,
        "binding_id": binding.binding_id,
        "binding_sha256": binding.binding_sha256,
        "runtime_identity_id": identity.identity_id,
        "runtime_identity_sha256": identity.identity_sha256,
        "surface": binding.surface,
        "subject_id": binding.subject_id,
        "instance_id": binding.instance_id,
        "epoch": binding.epoch,
        "pointer_id": identity.pointer_id,
        "pointer_sha256": identity.pointer_sha256,
        "pointer_generation": identity.pointer_generation,
        "slot_id": identity.slot_id,
        "slot_sha256": identity.slot_sha256,
        "boot_receipt_id": identity.boot_receipt_id,
        "boot_receipt_sha256": identity.boot_receipt_sha256,
        "binary_sha256": identity.binary_sha256,
        "version": identity.version,
        "target": identity.target,
        "origin_sample_id": origin.sample_id,
        "origin_sample_sha256": origin.sample_sha256,
        "origin_sequence": 1,
        "origin_kind": "startup",
        "origin_phase": "starting",
        "origin_observed_at": _aware(origin.observed_at).isoformat(),
        "timeout_seconds": origin.timeout_seconds,
        "population_member_verified": True,
        "exact_stable_release_verified": True,
        "startup_origin_verified": True,
        "runtime_observation_input_recorded": True,
        "observation_window_authority": False,
        "long_term_metrics_recorded": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "admitted_at": admitted_at,
    }
    digest = _digest(core)
    return EvolutionStablePromotionRuntimeObservationAdmission.model_validate(
        {
            **core,
            "admission_id": f"evstablepromadmit_{digest[:24]}",
            "admission_sha256": digest,
        }
    )


def _sources_match(contract, member, member_receipt, deployment, binding, origin) -> bool:
    intent = deployment.preparation.intent
    proof = intent.proof.payload
    auth = member_receipt.execution_package.authorization.authorization
    finalization = member_receipt.submission.result.release_finalization
    pointer = deployment.activated_pointer
    identity = binding.runtime_identity
    return bool(
        contract.workspace_root
        == deployment.workspace_root
        == binding.workspace_root
        == origin.workspace_root
        and contract.population_snapshot_id
        == intent.population_snapshot_id
        == proof.population_snapshot_id
        == auth.population_snapshot_id
        and contract.population_snapshot_sha256
        == intent.population_snapshot_sha256
        == proof.population_snapshot_sha256
        == auth.population_snapshot_sha256
        and contract.population_completion_receipt_id == auth.completion_receipt_id
        and contract.population_completion_receipt_sha256 == auth.completion_receipt_sha256
        and member.installation_member_id
        == proof.member_id
        == auth.installation_member_id
        == finalization.authority.installation_member_id
        and member.member_receipt_id == member_receipt.receipt_id
        and member.member_receipt_sha256 == member_receipt.receipt_sha256
        and member.authorization_id == auth.authorization_id
        and member.authorization_sha256 == auth.authorization_sha256
        and member.release_finalization_id == finalization.finalization_id
        and member.release_finalization_sha256 == finalization.finalization_sha256
        and auth.stable_intent_id == intent.intent_id
        and contract.candidate_version == auth.candidate_version == intent.candidate_version
        and contract.candidate_target == auth.candidate_target == intent.installation_target
        and auth.expected_active_pointer_id
        == member.expected_pointer_id
        == finalization.active_pointer.pointer_id
        == pointer.pointer_id
        == identity.pointer_id
        and auth.expected_active_pointer_sha256
        == member.expected_pointer_sha256
        == finalization.active_pointer.pointer_sha256
        == pointer.pointer_sha256
        == identity.pointer_sha256
        and auth.expected_active_pointer_generation
        == member.expected_pointer_generation
        == finalization.active_pointer.generation
        == pointer.generation
        == identity.pointer_generation
        and auth.expected_candidate_slot_id
        == finalization.active_pointer.current_slot_id
        == intent.candidate_slot_id
        == identity.slot_id
        and auth.expected_candidate_slot_sha256
        == finalization.active_pointer.current_slot_sha256
        == intent.candidate_slot_sha256
        == identity.slot_sha256
        and auth.expected_candidate_boot_receipt_id
        == finalization.active_pointer.boot_receipt_id
        == deployment.preparation.boot_receipt.receipt_id
        == identity.boot_receipt_id
        and auth.expected_candidate_boot_receipt_sha256
        == finalization.active_pointer.boot_receipt_sha256
        == deployment.preparation.boot_receipt.receipt_sha256
        == identity.boot_receipt_sha256
        and stable_runtime_identity_matches_deployment(identity, deployment)
        and binding.surface in contract.eligible_surfaces
        and _origin_matches(binding, origin)
    )


def _origin_matches(binding, origin) -> bool:
    return bool(
        origin.binding_id == binding.binding_id
        and origin.binding_sha256 == binding.binding_sha256
        and origin.runtime_identity_id == binding.runtime_identity.identity_id
        and origin.runtime_identity_sha256 == binding.runtime_identity.identity_sha256
        and origin.surface == binding.surface
        and origin.subject_id == binding.subject_id
        and origin.instance_id == binding.instance_id
        and origin.epoch == binding.epoch
        and origin.chain_origin_kind == "startup"
        and origin.chain_origin_sequence == 1
        and origin.heartbeat_sequence == 1
        and origin.phase == "starting"
        and origin.previous_sample_sha256 == ""
    )


def _dependency_checks(item):
    return (
        (
            "SELECT contract_sha256 FROM evolution_stable_promotion_observation_contracts "
            "WHERE contract_id = ?",
            (item.observation_contract_id,),
            item.observation_contract_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_population_finalizations "
            "WHERE receipt_id = ?",
            (item.population_finalization_receipt_id,),
            item.population_finalization_receipt_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_finalization_receipts "
            "WHERE receipt_id = ?",
            (item.member_receipt_id,),
            item.member_receipt_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_revalidation_stable_deployments "
            "WHERE receipt_id = ? AND intent_id = ?",
            (item.deployment_receipt_id, item.stable_intent_id),
            item.deployment_receipt_sha256,
        ),
    )


async def _require_dependencies(db, item) -> None:
    for query, params, expected in _dependency_checks(item):
        row = await (await db.execute(query, params)).fetchone()
        if row is None or row[0] != expected:
            await db.rollback()
            raise EvolutionStablePromotionRuntimeObservationAdmissionError(
                "stable_promotion_runtime_admission_dependency_mismatch",
                "稳定推广 Runtime Admission 缺少 exact durable dependency。",
            )


async def _dependencies_current(db_path: Path, item) -> bool:
    try:
        async with aiosqlite.connect(db_path, timeout=5.0) as db:
            for query, params, expected in _dependency_checks(item):
                row = await (await db.execute(query, params)).fetchone()
                if row is None or row[0] != expected:
                    return False
    except (aiosqlite.Error, OSError, TypeError, ValueError):
        return False
    return True


def _admission(value) -> EvolutionStablePromotionRuntimeObservationAdmission:
    try:
        return EvolutionStablePromotionRuntimeObservationAdmission.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_invalid",
            "稳定推广 Runtime Admission 无效。",
        ) from exc


def _restore(raw: str) -> EvolutionStablePromotionRuntimeObservationAdmission:
    if len(raw.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("stable promotion runtime admission oversized")
    return EvolutionStablePromotionRuntimeObservationAdmission.model_validate_json(raw)


def _finalization_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _FINALIZATION_RE.fullmatch(normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_finalization_id_invalid",
            "Population Finalization Receipt ID 格式无效。",
        )
    return normalized


def _contract_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evstablepromobserve_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_contract_id_invalid",
            "Stable Promotion Observation Contract ID 格式无效。",
        )
    return normalized


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evstablepromadmit_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_id_invalid",
            "Stable Promotion Runtime Admission ID 格式无效。",
        )
    return normalized


def _intent_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _INTENT_RE.fullmatch(normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_intent_id_invalid",
            "Stable Intent ID 格式无效。",
        )
    return normalized


def _member_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^relpopmember_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_member_id_invalid",
            "Installation Member ID 格式无效。",
        )
    return normalized


def _subject_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_IDENTITY_RE, normalized) is None:
        raise EvolutionStablePromotionRuntimeObservationAdmissionError(
            "stable_promotion_runtime_admission_subject_id_invalid",
            "Runtime subject ID 格式无效。",
        )
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("稳定推广 Runtime Admission 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda item: item.model_dump(mode="json"),
        ).encode("utf-8")
    ).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_runtime_admissions ("
        "admission_id TEXT PRIMARY KEY, admission_sha256 TEXT NOT NULL UNIQUE, "
        "observation_contract_id TEXT NOT NULL, "
        "population_finalization_receipt_id TEXT NOT NULL, "
        "installation_member_id TEXT NOT NULL, stable_intent_id TEXT NOT NULL, "
        "subject_id TEXT NOT NULL, binding_id TEXT NOT NULL UNIQUE, "
        "origin_sample_id TEXT NOT NULL UNIQUE, admission_json TEXT NOT NULL, "
        "admitted_at TEXT NOT NULL, "
        "UNIQUE(observation_contract_id, installation_member_id, subject_id));"
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_runtime_admission_member "
        "ON evolution_stable_promotion_runtime_admissions("
        "observation_contract_id, installation_member_id, admitted_at);"
    )


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_RUNTIME_OBSERVATION_ADMISSION_POLICY",
    "EvolutionStablePromotionRuntimeObservationAdmission",
    "EvolutionStablePromotionRuntimeObservationAdmissionError",
    "EvolutionStablePromotionRuntimeObservationAdmissionService",
    "EvolutionStablePromotionRuntimeObservationAdmissionStore",
    "EvolutionStablePromotionRuntimeObservationAdmissionView",
    "render_stable_promotion_runtime_observation_admission",
]
