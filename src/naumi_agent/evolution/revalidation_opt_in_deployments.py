"""Crash-reconcilable local opt-in activation and deployment facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import CredentialStoreError
from naumi_agent.evolution.revalidation_opt_in_deployment_intents import (
    EvolutionRevalidationOptInDeploymentIntent,
    EvolutionRevalidationOptInDeploymentIntentError,
    EvolutionRevalidationOptInDeploymentIntentService,
    EvolutionRevalidationOptInDeploymentIntentStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutStageEntryError,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.release.build_attestations import (
    ReleaseBuildAttestationError,
    ReleaseBuildTrustPolicyDocument,
    verify_release_build_attestation,
)
from naumi_agent.release.slots import (
    ReleaseActivationAuthority,
    ReleaseActivePointer,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_POLICY = (
    "evolution-revalidation-opt-in-deployment-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInDeploymentReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-opt-in-deployment-v1"] = (
        EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evredeployment_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    intent: EvolutionRevalidationOptInDeploymentIntent
    activated_pointer: ReleaseActivePointer
    activation_method: Literal["arc07_atomic_pointer_cas"] = (
        "arc07_atomic_pointer_cas"
    )
    deployment_scope: Literal["local_installation_opt_in"] = (
        "local_installation_opt_in"
    )
    local_installation_deployed: Literal[True] = True
    active_pointer_switched: Literal[True] = True
    old_slot_retained: Literal[True] = True
    deployment_fact_authority: Literal[True] = True
    process_started: Literal[False] = False
    population_assignment_enforced: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    activated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        intent = self.intent
        pointer = self.activated_pointer
        if self.workspace_root != intent.workspace_root or self.workspace_root != str(
            Path(self.workspace_root).expanduser().resolve()
        ):
            raise ValueError("Deployment Receipt workspace projection 不一致。")
        if not _matches_activation(intent, pointer):
            raise ValueError("Deployment Receipt pointer projection 不一致。")
        activated = _aware(pointer.activated_at)
        if not (
            self.activated_at == pointer.activated_at
            and _aware(intent.issued_at) <= activated < _aware(intent.expires_at)
        ):
            raise ValueError("Deployment Receipt activation window 无效。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evredeployment_{digest[:24]}"
        ):
            raise ValueError("Deployment Receipt identity 不一致。")
        return self


class EvolutionRevalidationOptInDeploymentView(_StrictModel):
    receipt: EvolutionRevalidationOptInDeploymentReceipt
    intent_source_current: bool
    activation_chain_current: bool
    active_pointer_current: bool
    candidate_slot_current: bool
    boot_receipt_current: bool
    build_trust_current: bool
    opt_in_enrollment_current: bool
    rollout_control_current: bool
    deployment_fact_authority: bool
    active_deployment_authority: bool

    @model_validator(mode="after")
    def _project(self) -> Self:
        fact = bool(
            self.receipt.deployment_fact_authority
            and self.intent_source_current
            and self.activation_chain_current
        )
        active = bool(
            fact
            and self.active_pointer_current
            and self.candidate_slot_current
            and self.boot_receipt_current
            and self.build_trust_current
            and self.opt_in_enrollment_current
            and self.rollout_control_current
        )
        if self.deployment_fact_authority is not fact:
            raise ValueError("Deployment fact authority projection 不一致。")
        if self.active_deployment_authority is not active:
            raise ValueError("Active deployment authority projection 不一致。")
        return self


class EvolutionRevalidationOptInDeploymentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOptInDeploymentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        intent_store: EvolutionRevalidationOptInDeploymentIntentStore,
        release_slot_store: ReleaseSlotStore,
    ) -> None:
        if not isinstance(intent_store, EvolutionRevalidationOptInDeploymentIntentStore):
            raise TypeError("Deployment Store 需要 Deployment Intent Store。")
        if not isinstance(release_slot_store, ReleaseSlotStore):
            raise TypeError("Deployment Store 需要 ReleaseSlotStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        self.intent_store = intent_store
        self.release_slot_store = release_slot_store

    async def get_by_intent(
        self, intent_id: str
    ) -> EvolutionRevalidationOptInDeploymentReceipt | None:
        return await self._get("intent_id", intent_id)

    async def get_by_completion(
        self, completion_id: str
    ) -> EvolutionRevalidationOptInDeploymentReceipt | None:
        return await self._get("completion_id", completion_id)

    async def _get(self, column: str, value: str):
        if column not in {"intent_id", "completion_id"}:
            raise ValueError("Deployment Receipt lookup column 无效。")
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_opt_in_deployments "
                    f"WHERE {column} = ?",
                    (value,),
                )
            ).fetchone()
        return None if row is None else _restore(row["receipt_json"])

    async def record(
        self, receipt: EvolutionRevalidationOptInDeploymentReceipt
    ) -> EvolutionRevalidationOptInDeploymentReceipt:
        try:
            item = EvolutionRevalidationOptInDeploymentReceipt.model_validate_json(
                receipt.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_receipt_invalid",
                "Opt-in Deployment Receipt artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_receipt_oversized",
                "Opt-in Deployment Receipt 超过 4 MiB。",
            )
        try:
            activation = await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                item.activated_pointer.generation,
            )
        except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_activation_chain_invalid",
                "无法验证 ARC-07 activation history。",
            ) from exc
        if activation != item.activated_pointer:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_activation_changed",
                "Deployment Receipt 未绑定 exact ARC-07 activation event。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                intent_row = await (
                    await db.execute(
                        "SELECT intent_json FROM "
                        "evolution_revalidation_opt_in_deployment_intents "
                        "WHERE intent_id = ?",
                        (item.intent.intent_id,),
                    )
                ).fetchone()
                source_intent = (
                    None
                    if intent_row is None
                    else EvolutionRevalidationOptInDeploymentIntent.model_validate_json(
                        intent_row["intent_json"]
                    )
                )
                if source_intent != item.intent:
                    await db.rollback()
                    raise EvolutionRevalidationOptInDeploymentError(
                        "opt_in_deployment_intent_changed",
                        "Deployment Intent 已变化或不存在。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_opt_in_deployments WHERE intent_id = ?",
                        (item.intent.intent_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["receipt_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationOptInDeploymentError(
                            "opt_in_deployment_receipt_conflict",
                            "同一 Deployment Intent 已绑定不同 Receipt。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_deployments "
                    "(receipt_id, receipt_sha256, intent_id, completion_id, "
                    "pointer_sha256, pointer_generation, receipt_json, activated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.intent.intent_id,
                        item.intent.admission.completion_id,
                        item.activated_pointer.pointer_sha256,
                        item.activated_pointer.generation,
                        encoded,
                        item.activated_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationOptInDeploymentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_store_error",
                "Opt-in Deployment Receipt 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationOptInDeploymentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        intent_service: EvolutionRevalidationOptInDeploymentIntentService,
        interaction_store: HarnessStore,
        store: EvolutionRevalidationOptInDeploymentStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Deployment Service 需要 HarnessStore。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.intent_service = intent_service
        self.interaction_store = interaction_store
        self.store = store
        self.release_slot_store = store.release_slot_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def deploy(self, *, completion_id: str):
        return await self._run(completion_id)

    async def reconcile(self, *, completion_id: str):
        return await self._run(completion_id)

    async def inspect(self, *, completion_id: str):
        receipt = await self.store.get_by_completion(completion_id)
        if receipt is None:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_receipt_missing",
                "尚未形成 Opt-in Deployment Receipt。",
            )
        return await self._view(receipt)

    async def _run(self, completion_id: str):
        lock = self._locks.setdefault(completion_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_completion(completion_id)
            if existing is not None:
                return await self._view(existing)
            intent = await self.intent_service.store.get_by_completion(completion_id)
            if intent is None:
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_intent_missing",
                    "缺少可供部署的 Opt-in Deployment Intent。",
                )
            recovered = await self._claim_existing_activation(intent)
            if recovered is not None:
                return recovered
            try:
                intent_view = await self.intent_service.inspect(
                    completion_id=completion_id
                )
            except EvolutionRevalidationOptInDeploymentIntentError as exc:
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_intent_stale",
                    "Deployment Intent 已失效，不能切换 active runtime。",
                ) from exc
            if intent_view.intent != intent or not intent_view.activation_intent_authority:
                recovered = await self._claim_existing_activation(intent)
                if recovered is not None:
                    return recovered
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_intent_denied",
                    "Deployment Intent 当前没有 activation authority。",
                )
            current = await self._active_pointer()
            if current != intent.admission.previous_pointer:
                recovered = await self._claim_existing_activation(intent)
                if recovered is not None:
                    return recovered
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_pointer_conflict",
                    "Active pointer 已偏离 Deployment Intent 的 CAS 前提。",
                )
            now = self._now()
            activated_at = now.isoformat()
            if now >= _aware(intent.expires_at):
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_intent_expired",
                    "Deployment Intent 已过期，未切换 active runtime。",
                )
            try:
                pointer = await asyncio.to_thread(
                    self.release_slot_store.activate,
                    intent.activation_target_slot_id,
                    activated_at=activated_at,
                    action="activate",
                    _expected_pointer_sha256=(
                        intent.expected_previous_pointer_sha256
                    ),
                    _activation_authority=ReleaseActivationAuthority(
                        kind="evolution_opt_in_deployment_intent",
                        authority_id=intent.intent_id,
                        authority_sha256=intent.intent_sha256,
                    ),
                )
            except ReleaseSlotError as exc:
                if exc.code == "release_active_pointer_conflict":
                    recovered = await self._claim_existing_activation(intent)
                    if recovered is not None:
                        return recovered
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_activation_failed",
                    "ARC-07 atomic activation 失败，未生成 Deployment Receipt。",
                ) from exc
            if not _matches_activation(intent, pointer):
                raise EvolutionRevalidationOptInDeploymentError(
                    "opt_in_deployment_activation_mismatch",
                    "ARC-07 activation 结果与 Deployment Intent 不一致。",
                )
            return await self._record_and_view(intent, pointer)

    async def _record_and_view(self, intent, pointer):
        try:
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                intent=intent,
                pointer=pointer,
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_activation_outside_authority",
                "Activation event 不在 exact Deployment Intent authority window 内。",
            ) from exc
        stored = await self.store.record(receipt)
        return await self._view(stored)

    async def _claim_existing_activation(self, intent):
        activation = await self._activation_event(intent)
        if activation is None:
            return None
        if not _matches_activation(intent, activation):
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_generation_conflict",
                "预期 activation generation 已被其他目标占用。",
            )
        return await self._record_and_view(intent, activation)

    async def _activation_event(self, intent):
        try:
            return await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                intent.expected_activation_generation,
            )
        except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_activation_chain_invalid",
                "ARC-07 activation history 无法验证。",
            ) from exc

    async def _active_pointer(self):
        try:
            return await asyncio.to_thread(self.release_slot_store.active)
        except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
            raise EvolutionRevalidationOptInDeploymentError(
                "opt_in_deployment_active_pointer_invalid",
                "ARC-07 active pointer 无法验证。",
            ) from exc

    async def _view(self, receipt):
        intent = receipt.intent
        admission = intent.admission
        try:
            source_intent = await self.intent_service.store.get_by_completion(
                admission.completion_id
            )
            intent_source_current = source_intent == intent
        except (aiosqlite.Error, OSError, TypeError, ValueError):
            intent_source_current = False
        try:
            event = await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                receipt.activated_pointer.generation,
            )
            activation_chain_current = event == receipt.activated_pointer
        except (OSError, TypeError, ValueError, ReleaseSlotError):
            activation_chain_current = False
        try:
            active = await asyncio.to_thread(self.release_slot_store.active)
            active_pointer_current = active == receipt.activated_pointer
        except (OSError, TypeError, ValueError, ReleaseSlotError):
            active_pointer_current = False
        resolved = None
        try:
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                admission.candidate_slot.slot_id,
                admission.boot_receipt.receipt_id,
            )
            candidate_slot_current = resolved.slot == admission.candidate_slot
            boot_receipt_current = resolved.boot_receipt == admission.boot_receipt
        except (OSError, TypeError, ValueError, ReleaseSlotError):
            candidate_slot_current = False
            boot_receipt_current = False
        build_trust_current = _build_trust_current(
            intent,
            resolved,
            self.intent_service.candidate_service.trust_policy_provider,
        )
        try:
            interaction = await self.interaction_store.get_interaction(
                workspace_root=self.workspace_root,
                interaction_id=intent.cohort.interaction.interaction_id,
            )
            opt_in_enrollment_current = interaction == intent.cohort.interaction
        except (HarnessStoreError, OSError, TypeError, ValueError):
            opt_in_enrollment_current = False
        try:
            control = await (
                self.intent_service.candidate_service.stage_advance_service.control_store.latest(
                    self.workspace_root
                )
            )
            advance = intent.stage_advance
            rollout_control_current = bool(
                (
                    control is None
                    and advance.control_sequence == 0
                    and advance.control_event_sha256 == ""
                )
                or (
                    control is not None
                    and control.sequence == advance.control_sequence
                    and control.event_sha256 == advance.control_event_sha256
                    and control.state.value == "active"
                )
            )
        except (
            CredentialStoreError,
            EvolutionRevalidationRolloutStageEntryError,
            OSError,
            TypeError,
            ValueError,
        ):
            rollout_control_current = False
        fact = bool(intent_source_current and activation_chain_current)
        return EvolutionRevalidationOptInDeploymentView(
            receipt=receipt,
            intent_source_current=intent_source_current,
            activation_chain_current=activation_chain_current,
            active_pointer_current=active_pointer_current,
            candidate_slot_current=candidate_slot_current,
            boot_receipt_current=boot_receipt_current,
            build_trust_current=build_trust_current,
            opt_in_enrollment_current=opt_in_enrollment_current,
            rollout_control_current=rollout_control_current,
            deployment_fact_authority=fact,
            active_deployment_authority=(
                fact
                and active_pointer_current
                and candidate_slot_current
                and boot_receipt_current
                and build_trust_current
                and opt_in_enrollment_current
                and rollout_control_current
            ),
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


def _build_receipt(*, workspace_root, intent, pointer):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_POLICY,
        "workspace_root": str(workspace_root),
        "intent": intent,
        "activated_pointer": pointer,
        "activation_method": "arc07_atomic_pointer_cas",
        "deployment_scope": "local_installation_opt_in",
        "local_installation_deployed": True,
        "active_pointer_switched": True,
        "old_slot_retained": True,
        "deployment_fact_authority": True,
        "process_started": False,
        "population_assignment_enforced": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "rollback_executed": False,
        "promotion_authority": False,
        "activated_at": pointer.activated_at,
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInDeploymentReceipt.model_validate(
        {**core, "receipt_id": f"evredeployment_{digest[:24]}", "receipt_sha256": digest}
    )


def _matches_activation(intent, pointer) -> bool:
    admission = intent.admission
    return bool(
        pointer.generation == intent.expected_activation_generation
        and pointer.schema_version == 2
        and pointer.activation_authority
        == ReleaseActivationAuthority(
            kind="evolution_opt_in_deployment_intent",
            authority_id=intent.intent_id,
            authority_sha256=intent.intent_sha256,
        )
        and pointer.previous_pointer_sha256
        == intent.expected_previous_pointer_sha256
        and pointer.action == "activate"
        and pointer.current_slot_id == intent.activation_target_slot_id
        and pointer.current_slot_sha256 == intent.activation_target_slot_sha256
        and pointer.previous_slot_id == admission.previous_slot.slot_id
        and pointer.previous_slot_sha256 == admission.previous_slot.slot_sha256
        and pointer.boot_receipt_id == intent.activation_boot_receipt_id
        and pointer.boot_receipt_sha256 == intent.activation_boot_receipt_sha256
        and pointer.atomic_switch_satisfied
        and pointer.old_slot_retained
    )


def _build_trust_current(intent, resolved, provider) -> bool:
    if resolved is None:
        return False
    admission = intent.admission
    try:
        policy = provider()
        if not isinstance(policy, ReleaseBuildTrustPolicyDocument):
            return False
        trusted = verify_release_build_attestation(
            admission.build_attestation,
            trust_policy=policy,
            manifest_path=Path(resolved.slot.bundle_dir) / "manifest.json",
        )
    except (OSError, TypeError, ValueError, ReleaseBuildAttestationError):
        return False
    return bool(
        policy.policy_id == admission.build_trust_policy_id
        and policy.policy_sha256 == admission.build_trust_policy_sha256
        and trusted == admission.trusted_builder_key
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Deployment timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def _restore(encoded: str) -> EvolutionRevalidationOptInDeploymentReceipt:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Opt-in Deployment Receipt 超过 4 MiB。")
    return EvolutionRevalidationOptInDeploymentReceipt.model_validate_json(encoded)


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_deployments ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "intent_id TEXT NOT NULL UNIQUE, completion_id TEXT NOT NULL UNIQUE, "
        "pointer_sha256 TEXT NOT NULL UNIQUE, pointer_generation INTEGER NOT NULL UNIQUE, "
        "receipt_json TEXT NOT NULL, activated_at TEXT NOT NULL)"
    )


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_POLICY",
    "EvolutionRevalidationOptInDeploymentError",
    "EvolutionRevalidationOptInDeploymentReceipt",
    "EvolutionRevalidationOptInDeploymentService",
    "EvolutionRevalidationOptInDeploymentStore",
    "EvolutionRevalidationOptInDeploymentView",
]
