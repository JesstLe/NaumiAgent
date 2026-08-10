"""Crash-reconcilable activation for selected stable installations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_boot_preparations import (
    EvolutionRevalidationStableBootPreparation,
    EvolutionRevalidationStableBootPreparationError,
    EvolutionRevalidationStableBootPreparationService,
    EvolutionRevalidationStableBootPreparationStore,
)
from naumi_agent.evolution.revalidation_stable_deployment_intents import (
    EvolutionRevalidationStableDeploymentIntent,
    EvolutionRevalidationStableDeploymentIntentError,
)
from naumi_agent.release.archive_admission import ReleaseArchiveAdmissionError
from naumi_agent.release.slots import (
    ReleaseActivationAuthority,
    ReleaseActivePointer,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_POLICY = (
    "evolution-revalidation-stable-deployment-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 12 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableDeploymentReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-stable-deployment-v1"
    ] = EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_POLICY
    receipt_id: str = Field(pattern=r"^evrestabledeployment_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    preparation: EvolutionRevalidationStableBootPreparation
    activated_pointer: ReleaseActivePointer
    activation_method: Literal["arc07_authority_bound_pointer_cas"] = (
        "arc07_authority_bound_pointer_cas"
    )
    deployment_scope: Literal["current_managed_stable_installation"] = (
        "current_managed_stable_installation"
    )
    population_membership_enforced: Literal[True] = True
    proof_of_possession_enforced: Literal[True] = True
    local_installation_deployed: Literal[True] = True
    active_pointer_switched: Literal[True] = True
    old_slot_retained: Literal[True] = True
    deployment_fact_authority: Literal[True] = True
    process_started: Literal[False] = False
    user_process_started: Literal[False] = False
    stable_installation_exposure_observed: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    activated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        preparation = self.preparation
        intent = preparation.intent
        pointer = self.activated_pointer
        if not (
            self.workspace_root == intent.workspace_root
            and self.workspace_root
            == str(Path(self.workspace_root).expanduser().resolve())
            and preparation.stable_activation_input_authority
        ):
            raise ValueError("Stable Deployment workspace/preparation 投影不一致。")
        if not _matches_activation(preparation, pointer):
            raise ValueError("Stable Deployment pointer 投影不一致。")
        activated = _aware(pointer.activated_at)
        if not (
            self.activated_at == pointer.activated_at
            and _aware(preparation.prepared_at) <= activated
            and _aware(intent.issued_at) <= activated < _aware(intent.expires_at)
        ):
            raise ValueError("Stable Deployment activation window 无效。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if not (
            self.receipt_sha256 == digest
            and self.receipt_id == f"evrestabledeployment_{digest[:24]}"
        ):
            raise ValueError("Stable Deployment Receipt identity 不一致。")
        return self


class EvolutionRevalidationStableDeploymentView(_StrictModel):
    receipt: EvolutionRevalidationStableDeploymentReceipt
    receipt_source_current: bool
    preparation_source_current: bool
    intent_source_current: bool
    activation_chain_current: bool
    active_pointer_current: bool
    candidate_slot_current: bool
    boot_receipt_current: bool
    stage_advance_current: bool
    plan_current: bool
    population_snapshot_current: bool
    credential_current: bool
    archive_admission_current: bool
    installation_target_current: bool
    intent_unexpired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=16)
    deployment_fact_authority: bool
    active_deployment_authority: bool
    stable_runtime_launch_input_authority: bool
    process_started: Literal[False] = False
    stable_installation_exposure_observed: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        fact = bool(
            self.receipt.deployment_fact_authority
            and self.receipt_source_current
            and self.preparation_source_current
            and self.intent_source_current
            and self.activation_chain_current
        )
        active = bool(
            fact
            and self.active_pointer_current
            and self.candidate_slot_current
            and self.boot_receipt_current
        )
        launch = bool(
            active
            and self.stage_advance_current
            and self.plan_current
            and self.population_snapshot_current
            and self.credential_current
            and self.archive_admission_current
            and self.installation_target_current
            and self.intent_unexpired
        )
        if not (
            self.deployment_fact_authority is fact
            and self.active_deployment_authority is active
            and self.stable_runtime_launch_input_authority is launch
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Deployment View authority 投影不一致。")
        return self


class EvolutionRevalidationStableDeploymentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationStableDeploymentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        preparation_store: EvolutionRevalidationStableBootPreparationStore,
        release_slot_store: ReleaseSlotStore,
    ) -> None:
        if not isinstance(
            preparation_store,
            EvolutionRevalidationStableBootPreparationStore,
        ):
            raise TypeError("Stable Deployment Store 需要 Boot Preparation Store。")
        if not isinstance(release_slot_store, ReleaseSlotStore):
            raise TypeError("Stable Deployment Store 需要 ReleaseSlotStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != preparation_store.db_path:
            raise ValueError("Stable Deployment 必须共用 Evolution evidence DB。")
        if release_slot_store is not preparation_store.release_slot_store:
            raise ValueError("Stable Deployment Slot Store dependency 不一致。")
        self.preparation_store = preparation_store
        self.intent_store = preparation_store.intent_store
        self.release_slot_store = release_slot_store

    async def get_by_preparation(
        self,
        preparation_id: str,
    ) -> EvolutionRevalidationStableDeploymentReceipt | None:
        _require_id(
            preparation_id,
            r"^evrestableboot_[0-9a-f]{24}$",
            "Boot Preparation",
        )
        return await self._get("preparation_id", preparation_id)

    async def get_by_intent(
        self,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentReceipt | None:
        _require_id(
            intent_id,
            r"^evrestableintent_[0-9a-f]{24}$",
            "Intent",
        )
        return await self._get("intent_id", intent_id)

    async def _get(self, column: str, value: str):
        if column not in {"preparation_id", "intent_id"}:
            raise ValueError("Stable Deployment lookup column 无效。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_stable_deployments "
                        f"WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["receipt_json"]))
        except EvolutionRevalidationStableDeploymentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_source_invalid",
                "Stable Deployment durable source 无效。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionRevalidationStableDeploymentReceipt,
    ) -> EvolutionRevalidationStableDeploymentReceipt:
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_receipt_oversized",
                "Stable Deployment Receipt 超过 12 MiB。",
            )
        await self._require_activation(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_durable_sources(db, item)
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_stable_deployments "
                        "WHERE preparation_id = ? OR intent_id = ?",
                        (
                            item.preparation.preparation_id,
                            item.preparation.intent.intent_id,
                        ),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["receipt_json"]))
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationStableDeploymentError(
                            "stable_deployment_receipt_conflict",
                            "同一 Stable authority 已绑定不同 Deployment Receipt。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_deployments "
                    "(receipt_id, receipt_sha256, preparation_id, intent_id, "
                    "pointer_sha256, pointer_generation, receipt_json, "
                    "activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.preparation.preparation_id,
                        item.preparation.intent.intent_id,
                        item.activated_pointer.pointer_sha256,
                        item.activated_pointer.generation,
                        encoded,
                        item.activated_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationStableDeploymentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_store_error",
                "Stable Deployment Receipt 无法持久化。",
            ) from exc
        return item

    async def _require_activation(self, item) -> None:
        try:
            event = await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                item.activated_pointer.generation,
            )
        except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_activation_chain_invalid",
                "无法验证 ARC-07 activation history。",
            ) from exc
        if event != item.activated_pointer:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_activation_changed",
                "Deployment Receipt 未绑定 exact ARC-07 activation event。",
            )


class EvolutionRevalidationStableDeploymentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        preparation_service: EvolutionRevalidationStableBootPreparationService,
        store: EvolutionRevalidationStableDeploymentStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(
                preparation_service,
                EvolutionRevalidationStableBootPreparationService,
            )
            and isinstance(store, EvolutionRevalidationStableDeploymentStore)
            and store.preparation_store is preparation_service.store
            and (clock is None or preparation_service.clock is clock)
        ):
            raise ValueError("Stable Deployment Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != preparation_service.workspace_root:
            raise ValueError("Stable Deployment workspace 必须一致。")
        self.preparation_service = preparation_service
        self.preparation_store = preparation_service.store
        self.intent_service = preparation_service.intent_service
        self.intent_store = self.preparation_store.intent_store
        self.archive_service = self.intent_store.archive_service
        self.release_slot_store = store.release_slot_store
        self.store = store
        self.clock = preparation_service.clock if clock is None else clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def deploy(self, *, intent_id: str):
        return await self._run(intent_id)

    async def reconcile(self, *, intent_id: str):
        return await self._run(intent_id)

    async def inspect(self, *, intent_id: str):
        receipt = await self.store.get_by_intent(intent_id)
        if receipt is None:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_receipt_missing",
                "指定 Stable Intent 尚无 Deployment Receipt。",
            )
        return await self._view(receipt)

    async def _run(self, intent_id: str):
        _require_id(
            intent_id,
            r"^evrestableintent_[0-9a-f]{24}$",
            "Intent",
        )
        lock = self._locks.setdefault(intent_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_intent(intent_id)
            if existing is not None:
                return await self._view(existing)
            intent = await self.intent_store.get_by_intent(intent_id)
            if intent is None:
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_intent_missing",
                    "缺少 Stable Deployment Intent。",
                )
            preparation = await self.preparation_store.get_by_intent(intent_id)
            if preparation is None:
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_preparation_missing",
                    "缺少 current Stable Boot Preparation。",
                )
            if preparation.intent != intent:
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_preparation_mismatch",
                    "Boot Preparation 未绑定 exact Stable Intent。",
                )
            recovered = await self._claim_existing_activation(preparation)
            if recovered is not None:
                return recovered
            try:
                prepared_view = await self.preparation_service.inspect(
                    intent_id=intent.intent_id
                )
            except EvolutionRevalidationStableBootPreparationError as exc:
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_preparation_stale",
                    "Boot Preparation 已失效，不能切换 active pointer。",
                ) from exc
            if not (
                prepared_view.preparation == preparation
                and prepared_view.stable_activation_input_authority
            ):
                recovered = await self._claim_existing_activation(preparation)
                if recovered is not None:
                    return recovered
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_preparation_denied",
                    "Boot Preparation 当前没有 activation input authority。",
                )
            current = await self._active_pointer()
            if current != intent.previous_pointer:
                recovered = await self._claim_existing_activation(preparation)
                if recovered is not None:
                    return recovered
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_pointer_conflict",
                    "Active pointer 已偏离 Stable Intent 的 CAS 前提。",
                )
            now = _aware(self.clock())
            if now >= _aware(intent.expires_at):
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_intent_expired",
                    "Stable Intent 已过期，未切换 active pointer。",
                )
            try:
                pointer = await asyncio.to_thread(
                    self.release_slot_store.activate,
                    intent.candidate_slot_id,
                    activated_at=now.isoformat(),
                    action="activate",
                    _expected_pointer_sha256=(
                        intent.expected_previous_pointer_sha256
                    ),
                    _activation_authority=_activation_authority(preparation),
                )
            except ReleaseSlotError as exc:
                if exc.code == "release_active_pointer_conflict":
                    recovered = await self._claim_existing_activation(preparation)
                    if recovered is not None:
                        return recovered
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_activation_failed",
                    "ARC-07 atomic activation 失败，未生成 Deployment Receipt。",
                ) from exc
            if not _matches_activation(preparation, pointer):
                raise EvolutionRevalidationStableDeploymentError(
                    "stable_deployment_activation_mismatch",
                    "ARC-07 activation 结果与 Boot Preparation 不一致。",
                )
            return await self._record_and_view(preparation, pointer)

    async def _record_and_view(self, preparation, pointer):
        try:
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                preparation=preparation,
                pointer=pointer,
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_activation_outside_authority",
                "Activation event 不在 exact Stable authority window 内。",
            ) from exc
        stored = await self.store.record(receipt)
        return await self._view(stored)

    async def _claim_existing_activation(self, preparation):
        event = await self._activation_event(preparation.intent)
        if event is None:
            return None
        if not _matches_activation(preparation, event):
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_generation_conflict",
                "预期 activation generation 已被其他 authority 占用。",
            )
        return await self._record_and_view(preparation, event)

    async def _activation_event(self, intent):
        try:
            return await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                intent.expected_activation_generation,
            )
        except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_activation_chain_invalid",
                "ARC-07 activation history 无法验证。",
            ) from exc

    async def _active_pointer(self):
        try:
            return await asyncio.to_thread(self.release_slot_store.active)
        except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentError(
                "stable_deployment_active_pointer_invalid",
                "ARC-07 active pointer 无法验证。",
            ) from exc

    async def _view(self, receipt):
        preparation = receipt.preparation
        intent = preparation.intent
        receipt_source_current = False
        preparation_source_current = False
        intent_source_current = False
        activation_chain_current = False
        active_pointer_current = False
        candidate_slot_current = False
        boot_receipt_current = False
        stage_advance_current = False
        plan_current = False
        population_snapshot_current = False
        credential_current = False
        archive_admission_current = False
        installation_target_current = False
        intent_unexpired = _aware(self.clock()) < _aware(intent.expires_at)
        try:
            stored = await self.store.get_by_intent(intent.intent_id)
            receipt_source_current = stored == receipt
        except EvolutionRevalidationStableDeploymentError:
            pass
        try:
            stored = await self.preparation_store.get_by_intent(intent.intent_id)
            preparation_source_current = stored == preparation
        except EvolutionRevalidationStableBootPreparationError:
            pass
        try:
            stored = await self.intent_store.get_by_intent(intent.intent_id)
            intent_source_current = stored == intent
        except EvolutionRevalidationStableDeploymentIntentError:
            pass
        try:
            intent_view = await self.intent_service.inspect_intent(
                intent_id=intent.intent_id
            )
            stage_advance_current = intent_view.stage_advance_current
            plan_current = intent_view.plan_current
            population_snapshot_current = intent_view.population_snapshot_current
            credential_current = intent_view.credential_current
            installation_target_current = intent_view.installation_target_current
        except EvolutionRevalidationStableDeploymentIntentError:
            pass
        try:
            admission_view = await self.archive_service.inspect(
                download_source_id=intent.archive_admission.download_receipt.source_id
            )
            archive_admission_current = bool(
                admission_view.receipt == intent.archive_admission
                and admission_view.archive_admission_authority
            )
        except ReleaseArchiveAdmissionError:
            pass
        try:
            event = await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                receipt.activated_pointer.generation,
            )
            activation_chain_current = event == receipt.activated_pointer
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            pass
        try:
            active = await asyncio.to_thread(self.release_slot_store.active)
            active_pointer_current = active == receipt.activated_pointer
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            pass
        try:
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                intent.candidate_slot_id,
                preparation.boot_receipt.receipt_id,
            )
            candidate_slot_current = (
                resolved.slot == intent.archive_admission.installed_slot
            )
            boot_receipt_current = resolved.boot_receipt == preparation.boot_receipt
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            pass
        checks = {
            "receipt_source_changed": receipt_source_current,
            "preparation_source_changed": preparation_source_current,
            "intent_source_changed": intent_source_current,
            "activation_chain_changed": activation_chain_current,
            "active_pointer_changed": active_pointer_current,
            "candidate_slot_changed": candidate_slot_current,
            "boot_receipt_changed": boot_receipt_current,
            "stage_advance_changed": stage_advance_current,
            "plan_changed": plan_current,
            "population_snapshot_changed": population_snapshot_current,
            "credential_changed": credential_current,
            "archive_admission_changed": archive_admission_current,
            "installation_target_changed": installation_target_current,
            "deployment_intent_expired": intent_unexpired,
        }
        reasons = tuple(sorted(reason for reason, passed in checks.items() if not passed))
        fact = bool(
            receipt_source_current
            and preparation_source_current
            and intent_source_current
            and activation_chain_current
        )
        active = bool(
            fact
            and active_pointer_current
            and candidate_slot_current
            and boot_receipt_current
        )
        launch = bool(
            active
            and stage_advance_current
            and plan_current
            and population_snapshot_current
            and credential_current
            and archive_admission_current
            and installation_target_current
            and intent_unexpired
        )
        return EvolutionRevalidationStableDeploymentView(
            receipt=receipt,
            receipt_source_current=receipt_source_current,
            preparation_source_current=preparation_source_current,
            intent_source_current=intent_source_current,
            activation_chain_current=activation_chain_current,
            active_pointer_current=active_pointer_current,
            candidate_slot_current=candidate_slot_current,
            boot_receipt_current=boot_receipt_current,
            stage_advance_current=stage_advance_current,
            plan_current=plan_current,
            population_snapshot_current=population_snapshot_current,
            credential_current=credential_current,
            archive_admission_current=archive_admission_current,
            installation_target_current=installation_target_current,
            intent_unexpired=intent_unexpired,
            invalidation_reasons=reasons,
            deployment_fact_authority=fact,
            active_deployment_authority=active,
            stable_runtime_launch_input_authority=launch,
        )


def _build_receipt(*, workspace_root, preparation, pointer):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_POLICY,
        "workspace_root": str(workspace_root),
        "preparation": preparation,
        "activated_pointer": pointer,
        "activation_method": "arc07_authority_bound_pointer_cas",
        "deployment_scope": "current_managed_stable_installation",
        "population_membership_enforced": True,
        "proof_of_possession_enforced": True,
        "local_installation_deployed": True,
        "active_pointer_switched": True,
        "old_slot_retained": True,
        "deployment_fact_authority": True,
        "process_started": False,
        "user_process_started": False,
        "stable_installation_exposure_observed": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "rollback_executed": False,
        "promotion_authority": False,
        "activated_at": pointer.activated_at,
    }
    digest = _digest(core)
    return EvolutionRevalidationStableDeploymentReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrestabledeployment_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _matches_activation(preparation, pointer) -> bool:
    intent = preparation.intent
    previous = intent.previous_pointer
    return bool(
        pointer.generation == intent.expected_activation_generation
        and pointer.schema_version == 2
        and pointer.activation_authority == _activation_authority(preparation)
        and pointer.previous_pointer_sha256
        == intent.expected_previous_pointer_sha256
        and pointer.action == "activate"
        and pointer.current_slot_id == intent.candidate_slot_id
        and pointer.current_slot_sha256 == intent.candidate_slot_sha256
        and pointer.previous_slot_id == previous.current_slot_id
        and pointer.previous_slot_sha256 == previous.current_slot_sha256
        and pointer.boot_receipt_id == preparation.boot_receipt.receipt_id
        and pointer.boot_receipt_sha256
        == preparation.boot_receipt.receipt_sha256
        and pointer.atomic_switch_satisfied
        and pointer.old_slot_retained
    )


def _activation_authority(preparation):
    return ReleaseActivationAuthority(
        kind="evolution_stable_boot_preparation",
        authority_id=preparation.preparation_id,
        authority_sha256=preparation.preparation_sha256,
    )


async def _require_durable_sources(db, receipt) -> None:
    preparation = receipt.preparation
    intent = preparation.intent
    preparation_row = await (
        await db.execute(
            "SELECT preparation_json FROM "
            "evolution_revalidation_stable_boot_preparations "
            "WHERE preparation_id = ?",
            (preparation.preparation_id,),
        )
    ).fetchone()
    intent_row = await (
        await db.execute(
            "SELECT intent_json FROM "
            "evolution_revalidation_stable_deployment_intents "
            "WHERE intent_id = ?",
            (intent.intent_id,),
        )
    ).fetchone()
    try:
        durable_preparation = (
            None
            if preparation_row is None
            else EvolutionRevalidationStableBootPreparation.model_validate_json(
                preparation_row["preparation_json"]
            )
        )
        durable_intent = (
            None
            if intent_row is None
            else EvolutionRevalidationStableDeploymentIntent.model_validate_json(
                intent_row["intent_json"]
            )
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableDeploymentError(
            "stable_deployment_dependency_invalid",
            "Stable Deployment durable dependency 无效。",
        ) from exc
    if durable_preparation != preparation or durable_intent != intent:
        raise EvolutionRevalidationStableDeploymentError(
            "stable_deployment_dependency_changed",
            "Boot Preparation 或 Deployment Intent 已变化。",
        )


def _validated(value) -> EvolutionRevalidationStableDeploymentReceipt:
    try:
        return EvolutionRevalidationStableDeploymentReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableDeploymentError(
            "stable_deployment_receipt_invalid",
            "Stable Deployment Receipt artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationStableDeploymentReceipt:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Stable Deployment Receipt 超过 12 MiB。")
    return EvolutionRevalidationStableDeploymentReceipt.model_validate_json(value)


def _require_id(value, pattern: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise EvolutionRevalidationStableDeploymentError(
            "stable_deployment_identifier_invalid",
            f"Stable {label} identifier 无效。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_stable_deployments ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "preparation_id TEXT NOT NULL UNIQUE, intent_id TEXT NOT NULL UNIQUE, "
        "pointer_sha256 TEXT NOT NULL UNIQUE, "
        "pointer_generation INTEGER NOT NULL UNIQUE, receipt_json TEXT NOT NULL, "
        "activated_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Deployment timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value) -> str:
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
    "EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_POLICY",
    "EvolutionRevalidationStableDeploymentError",
    "EvolutionRevalidationStableDeploymentReceipt",
    "EvolutionRevalidationStableDeploymentService",
    "EvolutionRevalidationStableDeploymentStore",
    "EvolutionRevalidationStableDeploymentView",
]
