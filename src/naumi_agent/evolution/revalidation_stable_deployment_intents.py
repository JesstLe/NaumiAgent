"""Short-lived deployment intents for stable managed installations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_percentage_stage_advances import (
    EvolutionRevalidationPercentageStageAdvanceError,
    EvolutionRevalidationPercentageStageAdvanceReceipt,
    EvolutionRevalidationPercentageStageAdvanceService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.evolution.revalidation_stable_installation_proofs import (
    EvolutionRevalidationStableInstallationProof,
    EvolutionRevalidationStableInstallationProofError,
    EvolutionRevalidationStableInstallationProofPayload,
    SignStableInstallationChallenge,
    build_stable_installation_proof,
)
from naumi_agent.release.archive_admission import (
    ReleaseArchiveAdmissionError,
    ReleaseArchiveAdmissionReceipt,
    ReleaseArchiveAdmissionService,
)
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistryError,
    ReleasePopulationSnapshot,
    ReleasePopulationSnapshotStore,
)
from naumi_agent.release.slots import (
    ReleaseActivePointer,
    ReleaseSlotError,
    host_release_target,
)

EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_INTENT_POLICY = (
    "evolution-revalidation-stable-deployment-intent-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_INTENT_TTL_SECONDS = 300


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableDeploymentIntent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-stable-deployment-intent-v1"] = (
        EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_INTENT_POLICY
    )
    intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    intent_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    stage_advance: EvolutionRevalidationPercentageStageAdvanceReceipt
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=10_000)
    population_snapshot_expires_at: str = Field(min_length=1, max_length=100)
    plan: EvolutionRevalidationRolloutPlan
    archive_admission: ReleaseArchiveAdmissionReceipt
    proof: EvolutionRevalidationStableInstallationProof
    installation_target: str = Field(min_length=1, max_length=255)
    previous_pointer: ReleaseActivePointer
    expected_previous_pointer_sha256: str = Field(pattern=_SHA256_RE)
    expected_activation_generation: int = Field(ge=2, le=1_000_000_000)
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=_SHA256_RE)
    candidate_manifest_sha256: str = Field(pattern=_SHA256_RE)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidate_source_tree_sha256: str = Field(pattern=_SHA256_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    authorization_mode: Literal["current_stable_stage_entry_managed_installation_possession"] = (
        "current_stable_stage_entry_managed_installation_possession"
    )
    explicit_user_prompt_required: Literal[False] = False
    one_time_issuance: Literal[True] = True
    stable_exposure_percent: Literal[100] = 100
    population_membership_verified: Literal[True] = True
    proof_of_possession_verified: Literal[True] = True
    stable_deployment_intent_authority: Literal[True] = True
    boot_preparation_authority: Literal[True] = True
    boot_executed: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    process_started: Literal[False] = False
    stable_installation_exposure_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    publish_executed: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        canonical_workspace = str(Path(self.workspace_root).expanduser().resolve())
        advance = self.stage_advance
        plan = self.plan
        admission = self.archive_admission
        slot = admission.installed_slot
        credential = self.proof.installation_credential
        payload = self.proof.payload
        build = admission.download_receipt.resolution.entry.build_attestation.payload
        if not (
            self.workspace_root == canonical_workspace == advance.workspace_root
            and advance.decision == "advance"
            and advance.stable_stage_entry_authority
            and advance.stable_exposure_percent == 100
            and advance.plan_id == plan.plan_id
            and advance.plan_sha256 == plan.plan_sha256
            and self.population_snapshot_id == payload.population_snapshot_id
            and self.population_snapshot_sha256 == payload.population_snapshot_sha256
            and self.population_snapshot_sequence == payload.population_snapshot_sequence
            and self.population_denominator == payload.population_denominator
            and payload.stage_advance_receipt_id == advance.receipt_id
            and payload.stage_advance_receipt_sha256 == advance.receipt_sha256
            and payload.plan_id == plan.plan_id
            and payload.plan_sha256 == plan.plan_sha256
            and payload.candidate_id == plan.candidate_id
            and payload.candidate_revision == plan.candidate_revision
        ):
            raise ValueError("Stable Deployment Intent Advance/Population/Plan 投影不一致。")
        if not (
            payload.archive_admission_id == admission.admission_id
            and payload.archive_admission_sha256 == admission.admission_sha256
            and payload.candidate_slot_id == slot.slot_id
            and payload.candidate_slot_sha256 == slot.slot_sha256
            and payload.channel
            == credential.payload.channel
            == admission.download_receipt.resolution.channel
            and payload.installation_target == self.installation_target
            and slot.target == self.installation_target
            and admission.download_receipt.resolution.target == self.installation_target
            and slot.source_commit == plan.target_head == build.source_commit
            and slot.source_tree_sha256 == plan.target_tree_sha256 == build.source_tree_sha256
            and _target_platform(self.installation_target) in plan.required_platforms
        ):
            raise ValueError("Stable Deployment Intent archive/target 投影不一致。")
        if not (
            self.previous_pointer.current_slot_id != slot.slot_id
            and payload.previous_pointer_sha256 == self.previous_pointer.pointer_sha256
            and payload.previous_pointer_generation == self.previous_pointer.generation
            and self.expected_previous_pointer_sha256 == self.previous_pointer.pointer_sha256
            and self.expected_activation_generation == self.previous_pointer.generation + 1
            and self.candidate_slot_id == slot.slot_id
            and self.candidate_slot_sha256 == slot.slot_sha256
            and self.candidate_manifest_sha256 == slot.manifest_sha256
            and self.candidate_version == slot.version
            and self.candidate_source_commit == slot.source_commit
            and self.candidate_source_tree_sha256 == slot.source_tree_sha256
        ):
            raise ValueError("Stable Deployment Intent pointer/slot 投影不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        expected_expiry = min(
            issued + timedelta(seconds=_MAX_INTENT_TTL_SECONDS),
            _aware(advance.expires_at),
            _aware(self.population_snapshot_expires_at),
            _aware(credential.payload.expires_at),
        )
        if not (
            _aware(payload.signed_at) == issued
            and issued >= _aware(admission.admitted_at)
            and issued < expires
            and expires == expected_expiry
        ):
            raise ValueError("Stable Deployment Intent authority window 无效。")
        core = self.model_dump(mode="json", exclude={"intent_id", "intent_sha256"})
        digest = _digest(core)
        if self.intent_sha256 != digest or self.intent_id != (f"evrestableintent_{digest[:24]}"):
            raise ValueError("Stable Deployment Intent content identity 不一致。")
        return self


class EvolutionRevalidationStableDeploymentIntentView(_StrictModel):
    intent: EvolutionRevalidationStableDeploymentIntent
    intent_source_current: bool
    stage_advance_current: bool
    plan_current: bool
    population_snapshot_current: bool
    credential_current: bool
    archive_admission_current: bool
    installation_target_current: bool
    previous_pointer_current: bool
    candidate_slot_current: bool
    candidate_slot_inactive: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    stable_deployment_intent_authority: bool
    boot_preparation_authority: bool
    boot_executed: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    process_started: Literal[False] = False
    stable_installation_exposure_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    publish_executed: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.intent_source_current
            and self.stage_advance_current
            and self.plan_current
            and self.population_snapshot_current
            and self.credential_current
            and self.archive_admission_current
            and self.installation_target_current
            and self.previous_pointer_current
            and self.candidate_slot_current
            and self.candidate_slot_inactive
            and not self.expired
        )
        if not (
            self.stable_deployment_intent_authority is current
            and self.boot_preparation_authority is current
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Deployment Intent View authority 投影不一致。")
        return self


class EvolutionRevalidationStableDeploymentIntentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationStableDeploymentIntentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        stage_advance_service: EvolutionRevalidationPercentageStageAdvanceService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        population_store: ReleasePopulationSnapshotStore,
        archive_service: ReleaseArchiveAdmissionService,
        installation_target_provider: Callable[[], str] = host_release_target,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            isinstance(stage_advance_service, EvolutionRevalidationPercentageStageAdvanceService)
            and isinstance(plan_service, EvolutionRevalidationRolloutPlanService)
            and isinstance(population_store, ReleasePopulationSnapshotStore)
            and isinstance(archive_service, ReleaseArchiveAdmissionService)
            and self.db_path == stage_advance_service.store.db_path == plan_service.store.db_path
            and callable(installation_target_provider)
        ):
            raise ValueError("Stable Deployment Intent Store dependency 不一致。")
        self.stage_advance_service = stage_advance_service
        self.plan_service = plan_service
        self.population_store = population_store
        self.archive_service = archive_service
        self.slot_store = archive_service.slot_store
        self.installation_target_provider = installation_target_provider
        self.clock = clock or (lambda: datetime.now(UTC))

    async def get_by_source(self, *, stage_advance_receipt_id, credential_id, admission_id):
        _require_id(stage_advance_receipt_id, r"^evrepercentadvance_[0-9a-f]{24}$", "Stage Advance")
        _require_id(credential_id, r"^relpopcred_[0-9a-f]{24}$", "Credential")
        _require_id(admission_id, r"^relarchiveadmission_[0-9a-f]{24}$", "Admission")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT intent_json FROM evolution_revalidation_stable_deployment_intents "
                        "WHERE stage_advance_receipt_id = ? AND credential_id = ? "
                        "AND admission_id = ?",
                        (stage_advance_receipt_id, credential_id, admission_id),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["intent_json"]))
        except EvolutionRevalidationStableDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_source_invalid",
                "Stable Deployment Intent durable source 无效。",
            ) from exc

    async def get_by_intent(
        self,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentIntent | None:
        _require_id(intent_id, r"^evrestableintent_[0-9a-f]{24}$", "Intent")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT intent_json FROM "
                        "evolution_revalidation_stable_deployment_intents "
                        "WHERE intent_id = ?",
                        (intent_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["intent_json"]))
        except EvolutionRevalidationStableDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_source_invalid",
                "Stable Deployment Intent durable source 无效。",
            ) from exc

    async def record(self, intent):
        item = _validated(intent)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_oversized", "Stable Deployment Intent 超过 8 MiB。"
            )
        now = _aware(self.clock())
        if not (_aware(item.issued_at) <= now < _aware(item.expires_at)):
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_not_current",
                "Stable Deployment Intent 不在当前 authority window。",
            )
        if not all((await _live_state(item, store=self)).values()):
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_source_changed",
                "Stable Deployment Intent authority source 已变化。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                advance_row = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_revalidation_percentage_stage_advances "
                        "WHERE receipt_id = ?",
                        (item.stage_advance.receipt_id,),
                    )
                ).fetchone()
                plan_row = await (
                    await db.execute(
                        "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                        "WHERE plan_id = ?",
                        (item.plan.plan_id,),
                    )
                ).fetchone()
                advance = (
                    None
                    if advance_row is None
                    else (
                        EvolutionRevalidationPercentageStageAdvanceReceipt.model_validate_json(
                            advance_row["receipt_json"]
                        )
                    )
                )
                plan = (
                    None
                    if plan_row is None
                    else EvolutionRevalidationRolloutPlan.model_validate_json(plan_row["plan_json"])
                )
                if advance != item.stage_advance or plan != item.plan:
                    await db.rollback()
                    raise EvolutionRevalidationStableDeploymentIntentError(
                        "stable_deployment_intent_dependency_changed",
                        "Stable Stage Advance 或 Rollout Plan 已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT intent_json FROM evolution_revalidation_stable_deployment_intents "
                        "WHERE stage_advance_receipt_id = ? AND credential_id = ? "
                        "AND admission_id = ?",
                        (
                            item.stage_advance.receipt_id,
                            item.proof.installation_credential.credential_id,
                            item.archive_admission.admission_id,
                        ),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["intent_json"]))
                    await db.rollback()
                    if not _same_source(restored, item):
                        raise EvolutionRevalidationStableDeploymentIntentError(
                            "stable_deployment_intent_conflict",
                            "Stable authority source 已绑定不同 Intent。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_deployment_intents "
                    "(intent_id, intent_sha256, stage_advance_receipt_id, credential_id, "
                    "admission_id, member_id, intent_json, issued_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.intent_id,
                        item.intent_sha256,
                        item.stage_advance.receipt_id,
                        item.proof.installation_credential.credential_id,
                        item.archive_admission.admission_id,
                        item.proof.installation_credential.payload.member_id,
                        encoded,
                        item.issued_at,
                        item.expires_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionRevalidationStableDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_store_error",
                "Stable Deployment Intent 无法持久化。",
            ) from exc


class EvolutionRevalidationStableDeploymentIntentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        channel: str,
        store: EvolutionRevalidationStableDeploymentIntentStore,
        sign_challenge: SignStableInstallationChallenge,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(store, EvolutionRevalidationStableDeploymentIntentStore)
            and callable(sign_challenge)
            and re.fullmatch(r"^[a-z][a-z0-9._-]{0,63}$", channel)
        ):
            raise ValueError("Stable Deployment Intent Service dependency 无效。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.channel = channel
        self.store = store
        self.slot_store = store.slot_store
        self.sign_challenge = sign_challenge
        self.clock = store.clock if clock is None else clock
        if clock is not None and store.clock is not clock:
            raise ValueError("Stable Deployment Intent Service clock 必须与 Store 一致。")
        self._locks: dict[str, asyncio.Lock] = {}

    async def issue(self, *, evidence_id, snapshot_id, member_id, download_source_id):
        _require_issue_ids(
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            member_id=member_id,
            download_source_id=download_source_id,
        )
        lock_key = f"{evidence_id}:{snapshot_id}:{member_id}:{download_source_id}"
        async with self._locks.setdefault(lock_key, asyncio.Lock()):
            sources = await self._current_sources(
                evidence_id=evidence_id,
                snapshot_id=snapshot_id,
                member_id=member_id,
                download_source_id=download_source_id,
            )
            advance, snapshot, credential, plan, admission, target, pointer = sources
            now = _aware(self.clock())
            expires = min(
                now + timedelta(seconds=_MAX_INTENT_TTL_SECONDS),
                _aware(advance.expires_at),
                _aware(snapshot.payload.expires_at),
                _aware(credential.payload.expires_at),
            )
            if expires <= now:
                raise EvolutionRevalidationStableDeploymentIntentError(
                    "stable_deployment_intent_window_expired",
                    "Stable Deployment Intent authority window 已结束。",
                )
            payload = _proof_payload(
                workspace_root=self.workspace_root,
                advance=advance,
                snapshot=snapshot,
                credential=credential,
                plan=plan,
                admission=admission,
                target=target,
                pointer=pointer,
                signed_at=now,
            )
            try:
                proof = await build_stable_installation_proof(
                    payload=payload,
                    credential=credential,
                    sign_challenge=self.sign_challenge,
                )
            except EvolutionRevalidationStableInstallationProofError as exc:
                raise EvolutionRevalidationStableDeploymentIntentError(
                    exc.code,
                    "Stable Deployment Intent 未通过 installation proof-of-possession。",
                ) from exc
            refreshed = await self._current_sources(
                evidence_id=evidence_id,
                snapshot_id=snapshot_id,
                member_id=member_id,
                download_source_id=download_source_id,
            )
            if refreshed != sources:
                raise EvolutionRevalidationStableDeploymentIntentError(
                    "stable_deployment_intent_context_changed",
                    "Installation 签名期间 Stable authority source 已变化。",
                )
            intent = _build_intent(
                workspace_root=self.workspace_root,
                advance=advance,
                snapshot=snapshot,
                plan=plan,
                admission=admission,
                proof=proof,
                target=target,
                pointer=pointer,
                issued_at=now,
                expires_at=expires,
            )
            stored = await self.store.record(intent)
            view = await self._view(stored)
            if not view.stable_deployment_intent_authority:
                raise EvolutionRevalidationStableDeploymentIntentError(
                    "stable_deployment_intent_revoked_during_issue",
                    "Intent 落盘时 source 已变化；历史 artifact 已撤权。",
                )
            return view

    async def inspect(self, *, stage_advance_receipt_id, credential_id, admission_id):
        intent = await self.store.get_by_source(
            stage_advance_receipt_id=stage_advance_receipt_id,
            credential_id=credential_id,
            admission_id=admission_id,
        )
        if intent is None:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_missing", "指定 Stable Deployment Intent 不存在。"
            )
        return await self._view(intent)

    async def inspect_intent(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentIntentView:
        intent = await self.store.get_by_intent(intent_id)
        if intent is None:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_missing",
                "指定 Stable Deployment Intent 不存在。",
            )
        return await self._view(intent)

    async def _current_sources(self, *, evidence_id, snapshot_id, member_id, download_source_id):
        try:
            advance_view = await self.store.stage_advance_service.inspect(evidence_id=evidence_id)
            plan_view = await self.store.plan_service.inspect(plan_id=advance_view.receipt.plan_id)
            snapshot_view = await self.store.population_store.inspect(snapshot_id=snapshot_id)
            admission_view = await self.store.archive_service.inspect(
                download_source_id=download_source_id
            )
            target = self.store.installation_target_provider()
            pointer = await asyncio.to_thread(self.store.slot_store.active)
        except (
            EvolutionRevalidationPercentageStageAdvanceError,
            EvolutionRevalidationRolloutPlanError,
            ReleasePopulationRegistryError,
            ReleaseArchiveAdmissionError,
            ReleaseSlotError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_source_unavailable",
                "Stable Deployment Intent 缺少 current authority source。",
            ) from exc
        snapshot = snapshot_view.snapshot
        credential = _credential(snapshot, member_id)
        if credential is None:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_member_missing",
                "当前 Installation 不在 authoritative Population Snapshot 中。",
            )
        if pointer is None:
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_previous_pointer_missing",
                "Stable deployment 需要 existing active pointer 作为 CAS baseline。",
            )
        if not (
            advance_view.stable_stage_entry_authority
            and plan_view.current_rollout_eligible
            and snapshot_view.population_snapshot_authority
            and snapshot.payload.channel == self.channel
            and admission_view.stable_deployment_intent_input_authority
            and _sources_match(
                advance=advance_view.receipt,
                snapshot=snapshot,
                credential=credential,
                plan=plan_view.plan,
                admission=admission_view.receipt,
                target=target,
                pointer=pointer,
            )
        ):
            raise EvolutionRevalidationStableDeploymentIntentError(
                "stable_deployment_intent_source_denied",
                "Stable Advance、Population、Admission、target 或 pointer 不允许签发。",
            )
        return (
            advance_view.receipt,
            snapshot,
            credential,
            plan_view.plan,
            admission_view.receipt,
            target,
            pointer,
        )

    async def _view(self, intent):
        state = await _live_state(intent, store=self.store)
        source_current = False
        try:
            source_current = (
                await self.store.get_by_source(
                    stage_advance_receipt_id=intent.stage_advance.receipt_id,
                    credential_id=intent.proof.installation_credential.credential_id,
                    admission_id=intent.archive_admission.admission_id,
                )
                == intent
            )
        except EvolutionRevalidationStableDeploymentIntentError:
            pass
        expired = _aware(self.clock()) >= _aware(intent.expires_at)
        checks = {"intent_source_changed": source_current, **state, "intent_expired": not expired}
        reasons = tuple(sorted(reason for reason, passed in checks.items() if not passed))
        authority = all(checks.values())
        return EvolutionRevalidationStableDeploymentIntentView(
            intent=intent,
            intent_source_current=source_current,
            stage_advance_current=state["stage_advance_changed"],
            plan_current=state["plan_changed"],
            population_snapshot_current=state["population_snapshot_changed"],
            credential_current=state["credential_changed"],
            archive_admission_current=state["archive_admission_changed"],
            installation_target_current=state["installation_target_changed"],
            previous_pointer_current=state["previous_pointer_changed"],
            candidate_slot_current=state["candidate_slot_changed"],
            candidate_slot_inactive=state["candidate_slot_no_longer_inactive"],
            expired=expired,
            invalidation_reasons=reasons,
            stable_deployment_intent_authority=authority,
            boot_preparation_authority=authority,
        )


async def _live_state(intent, *, store):
    advance_current = plan_current = population_current = credential_current = False
    admission_current = target_current = pointer_current = slot_current = slot_inactive = False
    try:
        view = await store.stage_advance_service.inspect(
            evidence_id=intent.stage_advance.stage_completion_evidence_id
        )
        advance_current = bool(
            view.receipt == intent.stage_advance and view.stable_stage_entry_authority
        )
    except EvolutionRevalidationPercentageStageAdvanceError:
        pass
    try:
        view = await store.plan_service.inspect(plan_id=intent.plan.plan_id)
        plan_current = bool(view.plan == intent.plan and view.current_rollout_eligible)
    except EvolutionRevalidationRolloutPlanError:
        pass
    try:
        view = await store.population_store.inspect(snapshot_id=intent.population_snapshot_id)
        snapshot = view.snapshot
        population_current = bool(
            view.population_snapshot_authority
            and snapshot.snapshot_sha256 == intent.population_snapshot_sha256
            and snapshot.payload.sequence == intent.population_snapshot_sequence
            and snapshot.payload.population_denominator == intent.population_denominator
            and snapshot.payload.expires_at == intent.population_snapshot_expires_at
        )
        credential_current = bool(
            population_current
            and _credential(snapshot, intent.proof.payload.member_id)
            == intent.proof.installation_credential
        )
    except ReleasePopulationRegistryError:
        pass
    try:
        view = await store.archive_service.inspect(
            download_source_id=intent.archive_admission.download_receipt.source_id
        )
        admission_current = bool(
            view.receipt == intent.archive_admission
            and view.stable_deployment_intent_input_authority
        )
    except ReleaseArchiveAdmissionError:
        pass
    try:
        target_current = store.installation_target_provider() == intent.installation_target
    except (ReleaseSlotError, OSError, TypeError, ValueError):
        pass
    try:
        pointer = await asyncio.to_thread(store.slot_store.active)
        pointer_current = pointer == intent.previous_pointer
        slot_inactive = bool(
            pointer is not None and pointer.current_slot_id != intent.candidate_slot_id
        )
        slot = await asyncio.to_thread(
            store.slot_store.inspect_installed_slot, intent.candidate_slot_id
        )
        slot_current = slot == intent.archive_admission.installed_slot
    except (ReleaseSlotError, OSError, TypeError, ValueError):
        pass
    return {
        "stage_advance_changed": advance_current,
        "plan_changed": plan_current,
        "population_snapshot_changed": population_current,
        "credential_changed": credential_current,
        "archive_admission_changed": admission_current,
        "installation_target_changed": target_current,
        "previous_pointer_changed": pointer_current,
        "candidate_slot_changed": slot_current,
        "candidate_slot_no_longer_inactive": slot_inactive,
    }


def _sources_match(*, advance, snapshot, credential, plan, admission, target, pointer):
    slot = admission.installed_slot
    build = admission.download_receipt.resolution.entry.build_attestation.payload
    try:
        platform = _target_platform(target)
    except ValueError:
        return False
    return bool(
        advance.decision == "advance"
        and advance.stable_stage_entry_authority
        and advance.stable_exposure_percent == 100
        and advance.plan_id == plan.plan_id
        and advance.plan_sha256 == plan.plan_sha256
        and credential in snapshot.payload.credentials
        and credential.payload.channel
        == snapshot.payload.channel
        == admission.download_receipt.resolution.channel
        and slot.target == target == admission.download_receipt.resolution.target
        and platform in plan.required_platforms
        and slot.source_commit == plan.target_head == build.source_commit
        and slot.source_tree_sha256 == plan.target_tree_sha256 == build.source_tree_sha256
        and pointer.current_slot_id != slot.slot_id
    )


def _proof_payload(
    *, workspace_root, advance, snapshot, credential, plan, admission, target, pointer, signed_at
):
    slot = admission.installed_slot
    return EvolutionRevalidationStableInstallationProofPayload(
        workspace_root=str(workspace_root),
        stage_advance_receipt_id=advance.receipt_id,
        stage_advance_receipt_sha256=advance.receipt_sha256,
        population_snapshot_id=snapshot.snapshot_id,
        population_snapshot_sha256=snapshot.snapshot_sha256,
        population_snapshot_sequence=snapshot.payload.sequence,
        population_denominator=snapshot.payload.population_denominator,
        credential_id=credential.credential_id,
        credential_sha256=credential.credential_sha256,
        member_id=credential.payload.member_id,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        archive_admission_id=admission.admission_id,
        archive_admission_sha256=admission.admission_sha256,
        candidate_slot_id=slot.slot_id,
        candidate_slot_sha256=slot.slot_sha256,
        channel=snapshot.payload.channel,
        installation_target=target,
        previous_pointer_sha256=pointer.pointer_sha256,
        previous_pointer_generation=pointer.generation,
        signed_at=_aware(signed_at).isoformat(),
    )


def _build_intent(
    *,
    workspace_root,
    advance,
    snapshot,
    plan,
    admission,
    proof,
    target,
    pointer,
    issued_at,
    expires_at,
):
    slot = admission.installed_slot
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_STABLE_DEPLOYMENT_INTENT_POLICY,
        "workspace_root": str(workspace_root),
        "stage_advance": advance.model_dump(mode="json"),
        "population_snapshot_id": snapshot.snapshot_id,
        "population_snapshot_sha256": snapshot.snapshot_sha256,
        "population_snapshot_sequence": snapshot.payload.sequence,
        "population_denominator": snapshot.payload.population_denominator,
        "population_snapshot_expires_at": snapshot.payload.expires_at,
        "plan": plan.model_dump(mode="json"),
        "archive_admission": admission.model_dump(mode="json"),
        "proof": proof.model_dump(mode="json"),
        "installation_target": target,
        "previous_pointer": pointer.model_dump(mode="json"),
        "expected_previous_pointer_sha256": pointer.pointer_sha256,
        "expected_activation_generation": pointer.generation + 1,
        "candidate_slot_id": slot.slot_id,
        "candidate_slot_sha256": slot.slot_sha256,
        "candidate_manifest_sha256": slot.manifest_sha256,
        "candidate_version": slot.version,
        "candidate_source_commit": slot.source_commit,
        "candidate_source_tree_sha256": slot.source_tree_sha256,
        "issued_at": _aware(issued_at).isoformat(),
        "expires_at": _aware(expires_at).isoformat(),
        "authorization_mode": "current_stable_stage_entry_managed_installation_possession",
        "explicit_user_prompt_required": False,
        "one_time_issuance": True,
        "stable_exposure_percent": 100,
        "population_membership_verified": True,
        "proof_of_possession_verified": True,
        "stable_deployment_intent_authority": True,
        "boot_preparation_authority": True,
        "boot_executed": False,
        "active_pointer_switched": False,
        "process_started": False,
        "stable_installation_exposure_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
        "rollback_authority": False,
        "publish_executed": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationStableDeploymentIntent.model_validate(
            {
                **core,
                "stage_advance": advance,
                "plan": plan,
                "archive_admission": admission,
                "proof": proof,
                "previous_pointer": pointer,
                "intent_id": f"evrestableintent_{digest[:24]}",
                "intent_sha256": digest,
            }
        )
    except ValueError as exc:
        raise EvolutionRevalidationStableDeploymentIntentError(
            "stable_deployment_intent_invalid", "Stable Deployment Intent artifact 无效。"
        ) from exc


def _credential(snapshot: ReleasePopulationSnapshot, member_id: str):
    return next(
        (item for item in snapshot.payload.credentials if item.payload.member_id == member_id), None
    )


def _target_platform(target: str) -> Literal["linux", "macos", "windows"]:
    prefix = target.split("-", 1)[0].casefold()
    if prefix not in {"linux", "macos", "windows"}:
        raise ValueError("Installation target platform 无效。")
    return prefix  # type: ignore[return-value]


def _same_source(left, right):
    return bool(
        left.stage_advance == right.stage_advance
        and left.population_snapshot_id == right.population_snapshot_id
        and left.population_snapshot_sha256 == right.population_snapshot_sha256
        and left.proof.installation_credential == right.proof.installation_credential
        and left.plan == right.plan
        and left.archive_admission == right.archive_admission
        and left.installation_target == right.installation_target
        and left.previous_pointer == right.previous_pointer
    )


def _validated(value):
    try:
        return EvolutionRevalidationStableDeploymentIntent.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableDeploymentIntentError(
            "stable_deployment_intent_invalid", "Stable Deployment Intent artifact 无效。"
        ) from exc


def _restore(value: str):
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Stable Deployment Intent durable source 超过 8 MiB。")
    return EvolutionRevalidationStableDeploymentIntent.model_validate_json(value)


def _require_issue_ids(**values):
    patterns = {
        "evidence_id": r"^evrepercentcomplete_[0-9a-f]{24}$",
        "snapshot_id": r"^relpopsnapshot_[0-9a-f]{24}$",
        "member_id": r"^relpopmember_[0-9a-f]{24}$",
        "download_source_id": r"^reldownloadsource_[0-9a-f]{24}$",
    }
    for name, value in values.items():
        _require_id(value, patterns[name], name)


def _require_id(value, pattern, label):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise EvolutionRevalidationStableDeploymentIntentError(
            "stable_deployment_intent_identifier_invalid", f"{label} 格式无效。"
        )


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_stable_deployment_intents ("
        "intent_id TEXT PRIMARY KEY, intent_sha256 TEXT NOT NULL UNIQUE, "
        "stage_advance_receipt_id TEXT NOT NULL, credential_id TEXT NOT NULL, "
        "admission_id TEXT NOT NULL, member_id TEXT NOT NULL, intent_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "UNIQUE(stage_advance_receipt_id, credential_id, admission_id))"
    )


def _aware(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 timezone。")
    return parsed.astimezone(UTC)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
