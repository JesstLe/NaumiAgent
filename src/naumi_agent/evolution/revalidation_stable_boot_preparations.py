"""Claim-fenced boot preparation for stable deployment intents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_deployment_intents import (
    EvolutionRevalidationStableDeploymentIntent,
    EvolutionRevalidationStableDeploymentIntentError,
    EvolutionRevalidationStableDeploymentIntentService,
)
from naumi_agent.release.slots import (
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_REVALIDATION_STABLE_BOOT_PREPARATION_POLICY = (
    "evolution-revalidation-stable-boot-preparation-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_MAX_BOOT_TIMEOUT_SECONDS = 20
_CLAIM_LEASE_SECONDS = 30
_MAX_CLAIM_WAIT_SECONDS = 35


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableBootPreparation(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-stable-boot-preparation-v1"] = (
        EVOLUTION_REVALIDATION_STABLE_BOOT_PREPARATION_POLICY
    )
    preparation_id: str = Field(pattern=r"^evrestableboot_[0-9a-f]{24}$")
    preparation_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    intent: EvolutionRevalidationStableDeploymentIntent
    boot_receipt: ReleaseSlotBootReceipt
    claim_owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    claim_epoch: int = Field(ge=1, le=1_000_000_000)
    boot_started_at: str = Field(min_length=1, max_length=100)
    prepared_at: str = Field(min_length=1, max_length=100)
    execution_method: Literal["arc07_immutable_slot_version_probe"] = (
        "arc07_immutable_slot_version_probe"
    )
    intent_current_at_prepare: Literal[True] = True
    previous_pointer_current_at_prepare: Literal[True] = True
    candidate_slot_current_at_prepare: Literal[True] = True
    candidate_slot_inactive_at_prepare: Literal[True] = True
    boot_probe_passed: Literal[True] = True
    boot_executed: Literal[True] = True
    probe_process_started: Literal[True] = True
    user_process_started: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    stable_activation_input_authority: Literal[True] = True
    activation_authority: Literal[False] = False
    deployment_receipt_authority: Literal[False] = False
    process_started: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        intent = self.intent
        boot = self.boot_receipt
        if not (
            self.workspace_root == intent.workspace_root
            and self.workspace_root == str(Path(self.workspace_root).expanduser().resolve())
            and intent.stable_deployment_intent_authority
            and intent.boot_preparation_authority
            and not intent.boot_executed
        ):
            raise ValueError("Stable Boot Preparation Intent 投影不一致。")
        if not (
            boot.slot_id == intent.candidate_slot_id
            and boot.slot_sha256 == intent.candidate_slot_sha256
            and boot.manifest_sha256 == intent.candidate_manifest_sha256
            and boot.arguments == ("--version",)
            and boot.exit_code == 0
            and boot.version_matched
            and boot.bootable
            and boot.activation_input_authority
        ):
            raise ValueError("Stable Boot Preparation Receipt 投影不一致。")
        started = _aware(self.boot_started_at)
        checked = _aware(boot.checked_at)
        prepared = _aware(self.prepared_at)
        if not (
            _aware(intent.issued_at) <= started
            and started <= checked <= prepared < _aware(intent.expires_at)
        ):
            raise ValueError("Stable Boot Preparation 时间窗口无效。")
        core = self.model_dump(
            mode="json",
            exclude={"preparation_id", "preparation_sha256"},
        )
        digest = _digest(core)
        if not (
            self.preparation_sha256 == digest
            and self.preparation_id == f"evrestableboot_{digest[:24]}"
        ):
            raise ValueError("Stable Boot Preparation content identity 不一致。")
        return self


class EvolutionRevalidationStableBootPreparationView(_StrictModel):
    preparation: EvolutionRevalidationStableBootPreparation
    preparation_source_current: bool
    intent_current: bool
    previous_pointer_current: bool
    candidate_slot_current: bool
    boot_receipt_current: bool
    candidate_slot_inactive: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=10)
    stable_activation_input_authority: bool
    active_pointer_switched: Literal[False] = False
    activation_authority: Literal[False] = False
    deployment_receipt_authority: Literal[False] = False
    process_started: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.preparation_source_current
            and self.intent_current
            and self.previous_pointer_current
            and self.candidate_slot_current
            and self.boot_receipt_current
            and self.candidate_slot_inactive
            and not self.expired
        )
        if not (
            self.stable_activation_input_authority is current
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Boot Preparation View authority 投影不一致。")
        return self


class EvolutionRevalidationStableBootPreparationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _BootPreparationClaim:
    intent_id: str
    owner_id: str
    epoch: int
    state: Literal["claimed", "completed"]
    started_at: datetime
    lease_expires_at: datetime


class EvolutionRevalidationStableBootPreparationStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        intent_service: EvolutionRevalidationStableDeploymentIntentService,
        release_slot_store: ReleaseSlotStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            intent_service,
            EvolutionRevalidationStableDeploymentIntentService,
        ):
            raise TypeError("Stable Boot Preparation Store 需要 Intent Service。")
        if not isinstance(release_slot_store, ReleaseSlotStore):
            raise TypeError("Stable Boot Preparation Store 需要 ReleaseSlotStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != intent_service.store.db_path:
            raise ValueError("Stable Boot Preparation 必须共用 Evolution evidence DB。")
        if release_slot_store is not intent_service.slot_store:
            raise ValueError("Stable Boot Preparation Slot Store dependency 不一致。")
        self.intent_service = intent_service
        self.intent_store = intent_service.store
        self.release_slot_store = release_slot_store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def get_by_intent(
        self,
        intent_id: str,
    ) -> EvolutionRevalidationStableBootPreparation | None:
        _require_intent_id(intent_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT preparation_json FROM "
                        "evolution_revalidation_stable_boot_preparations "
                        "WHERE intent_id = ?",
                        (intent_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["preparation_json"]))
        except EvolutionRevalidationStableBootPreparationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_source_invalid",
                "Stable Boot Preparation durable source 无效。",
            ) from exc

    async def acquire_claim(
        self,
        intent: EvolutionRevalidationStableDeploymentIntent,
        *,
        owner_id: str,
        lease_seconds: int = _CLAIM_LEASE_SECONDS,
    ) -> _BootPreparationClaim:
        item = _validated_intent(intent)
        _require_owner(owner_id)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("Stable Boot claim lease 必须是整数秒。")
        if not 2 <= lease_seconds <= 60:
            raise ValueError("Stable Boot claim lease 必须在 2..60 秒。")
        await self._require_intent_current(item)
        now = _aware(self.clock())
        if not (_aware(item.issued_at) <= now < _aware(item.expires_at)):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_intent_expired",
                "Stable Deployment Intent 已过期。",
            )
        lease_expires = min(
            now + timedelta(seconds=lease_seconds),
            _aware(item.expires_at),
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_intent_dependency(db, item)
                receipt_row = await (
                    await db.execute(
                        "SELECT preparation_id FROM "
                        "evolution_revalidation_stable_boot_preparations "
                        "WHERE intent_id = ?",
                        (item.intent_id,),
                    )
                ).fetchone()
                row = await (
                    await db.execute(
                        "SELECT owner_id, epoch, state, started_at, lease_expires_at "
                        "FROM evolution_revalidation_stable_boot_preparation_claims "
                        "WHERE intent_id = ?",
                        (item.intent_id,),
                    )
                ).fetchone()
                if receipt_row is not None:
                    if row is None or row["state"] != "completed":
                        await db.rollback()
                        raise EvolutionRevalidationStableBootPreparationError(
                            "stable_boot_claim_state_invalid",
                            "Completed Preparation 缺少一致 claim state。",
                        )
                    claim = _claim_from_row(item.intent_id, row)
                    await db.rollback()
                    return claim
                if row is None:
                    await db.execute(
                        "INSERT INTO "
                        "evolution_revalidation_stable_boot_preparation_claims "
                        "(intent_id, owner_id, epoch, state, started_at, "
                        "lease_expires_at, preparation_id, updated_at) "
                        "VALUES (?, ?, 1, 'claimed', ?, ?, NULL, ?)",
                        (
                            item.intent_id,
                            owner_id,
                            now.isoformat(),
                            lease_expires.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    await db.commit()
                    return _BootPreparationClaim(
                        intent_id=item.intent_id,
                        owner_id=owner_id,
                        epoch=1,
                        state="claimed",
                        started_at=now,
                        lease_expires_at=lease_expires,
                    )
                claim = _claim_from_row(item.intent_id, row)
                if claim.state == "completed":
                    await db.rollback()
                    return claim
                if claim.owner_id == owner_id and claim.lease_expires_at > now:
                    await db.rollback()
                    return claim
                if claim.lease_expires_at > now:
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_busy",
                        "另一个执行者正在进行 Stable Boot Preparation。",
                    )
                next_epoch = claim.epoch + 1
                if next_epoch > 1_000_000_000:
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_epoch_exhausted",
                        "Stable Boot claim epoch 已耗尽。",
                    )
                await db.execute(
                    "UPDATE evolution_revalidation_stable_boot_preparation_claims "
                    "SET owner_id = ?, epoch = ?, state = 'claimed', started_at = ?, "
                    "lease_expires_at = ?, preparation_id = NULL, updated_at = ? "
                    "WHERE intent_id = ? AND epoch = ? AND state = 'claimed'",
                    (
                        owner_id,
                        next_epoch,
                        now.isoformat(),
                        lease_expires.isoformat(),
                        now.isoformat(),
                        item.intent_id,
                        claim.epoch,
                    ),
                )
                if db.total_changes != 1:
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_conflict",
                        "Stable Boot claim 已被其他执行者推进。",
                    )
                await db.commit()
                return _BootPreparationClaim(
                    intent_id=item.intent_id,
                    owner_id=owner_id,
                    epoch=next_epoch,
                    state="claimed",
                    started_at=now,
                    lease_expires_at=lease_expires,
                )
        except EvolutionRevalidationStableBootPreparationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_claim_store_error",
                "Stable Boot claim 无法持久化。",
            ) from exc

    async def release_claim(self, claim: _BootPreparationClaim) -> None:
        if claim.state != "claimed" or not self.db_path.is_file():
            return
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM "
                    "evolution_revalidation_stable_boot_preparation_claims "
                    "WHERE intent_id = ? AND owner_id = ? AND epoch = ? "
                    "AND state = 'claimed'",
                    (claim.intent_id, claim.owner_id, claim.epoch),
                )
                await db.commit()
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_claim_release_failed",
                "Stable Boot claim 无法安全释放。",
            ) from exc

    async def record(
        self,
        preparation: EvolutionRevalidationStableBootPreparation,
        *,
        claim: _BootPreparationClaim,
    ) -> EvolutionRevalidationStableBootPreparation:
        item = _validated(preparation)
        if not (
            claim.state == "claimed"
            and claim.intent_id == item.intent.intent_id
            and claim.owner_id == item.claim_owner_id
            and claim.epoch == item.claim_epoch
        ):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_claim_mismatch",
                "Prepared Receipt 未绑定 exact claimed executor。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_oversized",
                "Stable Boot Preparation 超过 10 MiB。",
            )
        now = _aware(self.clock())
        if not (_aware(item.boot_started_at) <= now < _aware(item.intent.expires_at)):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_not_current",
                "Stable Boot Preparation 不在 current authority window。",
            )
        await self._require_preparation_current(item)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_intent_dependency(db, item.intent)
                claim_row = await (
                    await db.execute(
                        "SELECT owner_id, epoch, state, started_at, lease_expires_at "
                        "FROM evolution_revalidation_stable_boot_preparation_claims "
                        "WHERE intent_id = ?",
                        (item.intent.intent_id,),
                    )
                ).fetchone()
                if claim_row is None:
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_missing",
                        "Stable Boot Preparation 缺少 durable claim。",
                    )
                durable_claim = _claim_from_row(item.intent.intent_id, claim_row)
                if not (
                    durable_claim.state == "claimed"
                    and durable_claim.owner_id == claim.owner_id
                    and durable_claim.epoch == claim.epoch
                    and durable_claim.started_at == claim.started_at
                    and durable_claim.lease_expires_at == claim.lease_expires_at
                    and durable_claim.lease_expires_at > now
                ):
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_lost",
                        "Stable Boot claim 已过期或被其他执行者 fencing。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT preparation_json FROM "
                        "evolution_revalidation_stable_boot_preparations "
                        "WHERE intent_id = ?",
                        (item.intent.intent_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["preparation_json"]))
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationStableBootPreparationError(
                            "stable_boot_preparation_conflict",
                            "同一 Stable Intent 已绑定不同 Prepared Receipt。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_boot_preparations "
                    "(preparation_id, preparation_sha256, intent_id, boot_receipt_id, "
                    "preparation_json, prepared_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.preparation_id,
                        item.preparation_sha256,
                        item.intent.intent_id,
                        item.boot_receipt.receipt_id,
                        encoded,
                        item.prepared_at,
                    ),
                )
                await db.execute(
                    "UPDATE evolution_revalidation_stable_boot_preparation_claims "
                    "SET state = 'completed', preparation_id = ?, updated_at = ? "
                    "WHERE intent_id = ? AND owner_id = ? AND epoch = ? "
                    "AND state = 'claimed'",
                    (
                        item.preparation_id,
                        now.isoformat(),
                        item.intent.intent_id,
                        claim.owner_id,
                        claim.epoch,
                    ),
                )
                if db.total_changes != 2:
                    await db.rollback()
                    raise EvolutionRevalidationStableBootPreparationError(
                        "stable_boot_claim_commit_conflict",
                        "Prepared Receipt 与 claim 未能原子收口。",
                    )
                await db.commit()
            return item
        except EvolutionRevalidationStableBootPreparationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_store_error",
                "Stable Boot Preparation 无法持久化。",
            ) from exc

    async def _require_intent_current(self, intent) -> None:
        try:
            view = await self.intent_service.inspect_intent(intent_id=intent.intent_id)
        except EvolutionRevalidationStableDeploymentIntentError as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_intent_unavailable",
                "Stable Deployment Intent 当前不可用。",
            ) from exc
        if not (
            view.intent == intent
            and view.stable_deployment_intent_authority
            and view.boot_preparation_authority
        ):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_intent_denied",
                "Stable Deployment Intent 当前没有 boot preparation authority。",
            )

    async def _require_preparation_current(self, item) -> None:
        await self._require_intent_current(item.intent)
        try:
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                item.intent.candidate_slot_id,
                item.boot_receipt.receipt_id,
            )
            pointer = await asyncio.to_thread(self.release_slot_store.active)
        except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_receipt_stale",
                "Boot Receipt 或 candidate slot 当前无效。",
            ) from exc
        if not (
            resolved.slot == item.intent.archive_admission.installed_slot
            and resolved.boot_receipt == item.boot_receipt
            and pointer == item.intent.previous_pointer
            and pointer.current_slot_id != item.intent.candidate_slot_id
        ):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_context_changed",
                "Boot 后 candidate slot 或 previous pointer 已变化。",
            )


class EvolutionRevalidationStableBootPreparationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        owner_id: str,
        intent_service: EvolutionRevalidationStableDeploymentIntentService,
        store: EvolutionRevalidationStableBootPreparationStore,
        boot_timeout_seconds: int = _MAX_BOOT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_owner(owner_id)
        if (
            isinstance(boot_timeout_seconds, bool)
            or not isinstance(boot_timeout_seconds, int)
            or not 1 <= boot_timeout_seconds <= _MAX_BOOT_TIMEOUT_SECONDS
        ):
            raise ValueError("Stable Boot timeout 必须在 1..20 秒。")
        if not (
            isinstance(
                intent_service,
                EvolutionRevalidationStableDeploymentIntentService,
            )
            and isinstance(store, EvolutionRevalidationStableBootPreparationStore)
            and store.intent_service is intent_service
            and (clock is None or store.clock is clock)
        ):
            raise ValueError("Stable Boot Preparation Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != intent_service.workspace_root:
            raise ValueError("Stable Boot Preparation workspace 必须一致。")
        self.owner_id = owner_id
        self.intent_service = intent_service
        self.store = store
        self.release_slot_store = store.release_slot_store
        self.boot_timeout_seconds = boot_timeout_seconds
        self.clock = store.clock if clock is None else clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableBootPreparationView:
        _require_intent_id(intent_id)
        lock = self._locks.setdefault(intent_id, asyncio.Lock())
        async with lock:
            return await self._prepare_locked(intent_id)

    async def inspect(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableBootPreparationView:
        item = await self.store.get_by_intent(intent_id)
        if item is None:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_missing",
                "指定 Stable Intent 尚无 Boot Preparation。",
            )
        return await self._view(item)

    async def _prepare_locked(self, intent_id):
        intent_view = await self._current_intent(intent_id)
        intent = intent_view.intent
        existing = await self.store.get_by_intent(intent.intent_id)
        if existing is not None:
            return await self._view(existing)
        claim = await self._claim_or_wait(intent)
        if claim.state == "completed":
            completed = await self.store.get_by_intent(intent.intent_id)
            if completed is None:
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_completed_receipt_missing",
                    "Completed Stable Boot claim 缺少 Prepared Receipt。",
                )
            return await self._view(completed)
        try:
            refreshed = await self._current_intent(intent_id)
            if refreshed.intent != intent:
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_intent_changed",
                    "领取 claim 后 Stable Intent 已变化。",
                )
            now = _aware(self.clock())
            remaining = (_aware(intent.expires_at) - now).total_seconds()
            if remaining < 2:
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_window_too_short",
                    "Intent 剩余时间不足以完成安全 boot probe。",
                )
            timeout = min(
                self.boot_timeout_seconds,
                max(1, int(remaining) - 1),
            )
            try:
                boot = await asyncio.to_thread(
                    self.release_slot_store.verify_bootable,
                    intent.candidate_slot_id,
                    checked_at=now.isoformat(),
                    timeout_seconds=timeout,
                )
            except ReleaseSlotError as exc:
                code = (
                    "stable_boot_probe_timeout"
                    if exc.code == "release_slot_boot_timeout"
                    else "stable_boot_probe_failed"
                )
                raise EvolutionRevalidationStableBootPreparationError(
                    code,
                    "Candidate slot 未通过真实 boot probe。",
                ) from exc
            refreshed = await self._current_intent(intent_id)
            if refreshed.intent != intent:
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_context_changed",
                    "Boot probe 期间 Stable Intent authority 已变化。",
                )
            prepared_at = _aware(self.clock())
            if prepared_at >= _aware(intent.expires_at):
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_intent_expired_during_probe",
                    "Boot probe 完成时 Stable Intent 已过期。",
                )
            item = _build_preparation(
                workspace_root=self.workspace_root,
                intent=intent,
                boot=boot,
                claim=claim,
                prepared_at=prepared_at,
            )
            stored = await self.store.record(item, claim=claim)
            return await self._view(stored)
        except EvolutionRevalidationStableBootPreparationError:
            await self.store.release_claim(claim)
            raise
        except (OSError, TypeError, ValueError) as exc:
            await self.store.release_claim(claim)
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_preparation_failed",
                "Stable Boot Preparation 执行失败。",
            ) from exc

    async def _claim_or_wait(self, intent):
        deadline = time.monotonic() + _MAX_CLAIM_WAIT_SECONDS
        while True:
            existing = await self.store.get_by_intent(intent.intent_id)
            if existing is not None:
                return _BootPreparationClaim(
                    intent_id=intent.intent_id,
                    owner_id=existing.claim_owner_id,
                    epoch=existing.claim_epoch,
                    state="completed",
                    started_at=_aware(existing.boot_started_at),
                    lease_expires_at=_aware(existing.prepared_at),
                )
            try:
                return await self.store.acquire_claim(
                    intent,
                    owner_id=self.owner_id,
                )
            except EvolutionRevalidationStableBootPreparationError as exc:
                if exc.code != "stable_boot_claim_busy":
                    raise
            if time.monotonic() >= deadline:
                raise EvolutionRevalidationStableBootPreparationError(
                    "stable_boot_claim_wait_timeout",
                    "等待 Stable Boot claim 完成超时。",
                )
            await asyncio.sleep(0.05)

    async def _current_intent(self, intent_id):
        try:
            view = await self.intent_service.inspect_intent(intent_id=intent_id)
        except EvolutionRevalidationStableDeploymentIntentError as exc:
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_intent_unavailable",
                "Stable Deployment Intent 当前不可用。",
            ) from exc
        if not (view.stable_deployment_intent_authority and view.boot_preparation_authority):
            raise EvolutionRevalidationStableBootPreparationError(
                "stable_boot_intent_denied",
                "Stable Deployment Intent 已失去 boot preparation authority。",
            )
        return view

    async def _view(self, item):
        source_current = False
        try:
            stored = await self.store.get_by_intent(item.intent.intent_id)
            source_current = stored == item
        except EvolutionRevalidationStableBootPreparationError:
            pass
        intent_current = False
        try:
            view = await self.intent_service.inspect_intent(intent_id=item.intent.intent_id)
            intent_current = bool(
                view.intent == item.intent
                and view.stable_deployment_intent_authority
                and view.boot_preparation_authority
            )
        except EvolutionRevalidationStableDeploymentIntentError:
            pass
        pointer_current = False
        slot_current = False
        boot_current = False
        inactive = False
        try:
            pointer = await asyncio.to_thread(self.release_slot_store.active)
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                item.intent.candidate_slot_id,
                item.boot_receipt.receipt_id,
            )
            pointer_current = pointer == item.intent.previous_pointer
            inactive = bool(
                pointer is not None and pointer.current_slot_id != item.intent.candidate_slot_id
            )
            slot_current = resolved.slot == item.intent.archive_admission.installed_slot
            boot_current = resolved.boot_receipt == item.boot_receipt
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            pass
        expired = _aware(self.clock()) >= _aware(item.intent.expires_at)
        checks = {
            "preparation_source_changed": source_current,
            "deployment_intent_changed": intent_current,
            "previous_pointer_changed": pointer_current,
            "candidate_slot_changed": slot_current,
            "boot_receipt_changed": boot_current,
            "candidate_slot_no_longer_inactive": inactive,
            "deployment_intent_expired": not expired,
        }
        reasons = tuple(sorted(reason for reason, passed in checks.items() if not passed))
        return EvolutionRevalidationStableBootPreparationView(
            preparation=item,
            preparation_source_current=source_current,
            intent_current=intent_current,
            previous_pointer_current=pointer_current,
            candidate_slot_current=slot_current,
            boot_receipt_current=boot_current,
            candidate_slot_inactive=inactive,
            expired=expired,
            invalidation_reasons=reasons,
            stable_activation_input_authority=all(checks.values()),
        )


def _build_preparation(*, workspace_root, intent, boot, claim, prepared_at):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_STABLE_BOOT_PREPARATION_POLICY,
        "workspace_root": str(workspace_root),
        "intent": intent.model_dump(mode="json"),
        "boot_receipt": boot.model_dump(mode="json"),
        "claim_owner_id": claim.owner_id,
        "claim_epoch": claim.epoch,
        "boot_started_at": claim.started_at.isoformat(),
        "prepared_at": _aware(prepared_at).isoformat(),
        "execution_method": "arc07_immutable_slot_version_probe",
        "intent_current_at_prepare": True,
        "previous_pointer_current_at_prepare": True,
        "candidate_slot_current_at_prepare": True,
        "candidate_slot_inactive_at_prepare": True,
        "boot_probe_passed": True,
        "boot_executed": True,
        "probe_process_started": True,
        "user_process_started": False,
        "active_pointer_switched": False,
        "stable_activation_input_authority": True,
        "activation_authority": False,
        "deployment_receipt_authority": False,
        "process_started": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationStableBootPreparation.model_validate(
            {
                **core,
                "intent": intent,
                "boot_receipt": boot,
                "preparation_id": f"evrestableboot_{digest[:24]}",
                "preparation_sha256": digest,
            }
        )
    except ValueError as exc:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_preparation_invalid",
            "Stable Boot Preparation artifact 无效。",
        ) from exc


def _claim_from_row(intent_id, row) -> _BootPreparationClaim:
    state = str(row["state"])
    if state not in {"claimed", "completed"}:
        raise ValueError("Stable Boot claim state 无效。")
    return _BootPreparationClaim(
        intent_id=intent_id,
        owner_id=str(row["owner_id"]),
        epoch=int(row["epoch"]),
        state=state,
        started_at=_aware(str(row["started_at"])),
        lease_expires_at=_aware(str(row["lease_expires_at"])),
    )


async def _require_intent_dependency(db, intent) -> None:
    row = await (
        await db.execute(
            "SELECT intent_json FROM "
            "evolution_revalidation_stable_deployment_intents "
            "WHERE intent_id = ?",
            (intent.intent_id,),
        )
    ).fetchone()
    try:
        stored = (
            None
            if row is None
            else EvolutionRevalidationStableDeploymentIntent.model_validate_json(row["intent_json"])
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_intent_dependency_invalid",
            "Durable Stable Deployment Intent 无效。",
        ) from exc
    if stored != intent:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_intent_dependency_changed",
            "Durable Stable Deployment Intent 已变化。",
        )


def _validated_intent(value) -> EvolutionRevalidationStableDeploymentIntent:
    try:
        return EvolutionRevalidationStableDeploymentIntent.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_intent_invalid",
            "Stable Deployment Intent artifact 无效。",
        ) from exc


def _validated(value) -> EvolutionRevalidationStableBootPreparation:
    try:
        return EvolutionRevalidationStableBootPreparation.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_preparation_invalid",
            "Stable Boot Preparation artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationStableBootPreparation:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Stable Boot Preparation durable source 超过 10 MiB。")
    return EvolutionRevalidationStableBootPreparation.model_validate_json(value)


def _require_intent_id(value) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"^evrestableintent_[0-9a-f]{24}$",
            value,
        )
        is None
    ):
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_intent_id_invalid",
            "Stable Intent identifier 无效。",
        )


def _require_owner(value) -> None:
    if not isinstance(value, str) or _OWNER_RE.fullmatch(value) is None:
        raise EvolutionRevalidationStableBootPreparationError(
            "stable_boot_owner_invalid",
            "Stable Boot owner identifier 无效。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_revalidation_stable_boot_preparations ("
        "preparation_id TEXT PRIMARY KEY, preparation_sha256 TEXT NOT NULL UNIQUE, "
        "intent_id TEXT NOT NULL UNIQUE, boot_receipt_id TEXT NOT NULL UNIQUE, "
        "preparation_json TEXT NOT NULL, prepared_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_revalidation_stable_boot_preparation_claims ("
        "intent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('claimed', 'completed')), "
        "started_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL, "
        "preparation_id TEXT, updated_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Boot Preparation timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_STABLE_BOOT_PREPARATION_POLICY",
    "EvolutionRevalidationStableBootPreparation",
    "EvolutionRevalidationStableBootPreparationError",
    "EvolutionRevalidationStableBootPreparationService",
    "EvolutionRevalidationStableBootPreparationStore",
    "EvolutionRevalidationStableBootPreparationView",
]
