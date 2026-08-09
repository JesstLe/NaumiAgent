"""Short-lived one-time deployment intents for selected percentage members."""

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

from naumi_agent.evolution.revalidation_percentage_cohort_assignments import (
    EvolutionRevalidationPercentageCohortAssignment,
    EvolutionRevalidationPercentageCohortAssignmentError,
    EvolutionRevalidationPercentageCohortAssignmentService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanError,
)
from naumi_agent.release.archive_admission import (
    ReleaseArchiveAdmissionError,
    ReleaseArchiveAdmissionReceipt,
    ReleaseArchiveAdmissionService,
)
from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
    ReleasePopulationRegistryError,
)
from naumi_agent.release.slots import (
    ReleaseActivePointer,
    ReleaseSlotError,
    host_release_target,
)

EVOLUTION_REVALIDATION_PERCENTAGE_DEPLOYMENT_INTENT_POLICY = (
    "evolution-revalidation-percentage-deployment-intent-v1"
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


class EvolutionRevalidationPercentageDeploymentIntent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-percentage-deployment-intent-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_DEPLOYMENT_INTENT_POLICY
    intent_id: str = Field(pattern=r"^evrepercentintent_[0-9a-f]{24}$")
    intent_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    assignment: EvolutionRevalidationPercentageCohortAssignment
    plan: EvolutionRevalidationRolloutPlan
    archive_admission: ReleaseArchiveAdmissionReceipt
    installation_credential: ReleaseManagedInstallationCredential
    installation_target: str = Field(min_length=1, max_length=255)
    previous_pointer: ReleaseActivePointer
    expected_previous_pointer_sha256: str = Field(pattern=_SHA256_RE)
    expected_activation_generation: int = Field(ge=2, le=1_000_000_000)
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=_SHA256_RE)
    candidate_manifest_sha256: str = Field(pattern=_SHA256_RE)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_source_commit: str = Field(
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    candidate_source_tree_sha256: str = Field(pattern=_SHA256_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    authorization_mode: Literal[
        "current_stage_advance_selected_managed_installation"
    ] = "current_stage_advance_selected_managed_installation"
    explicit_user_prompt_required: Literal[False] = False
    one_time_issuance: Literal[True] = True
    assignment_current_at_issue: Literal[True] = True
    archive_admission_current_at_issue: Literal[True] = True
    managed_installation_credential_verified: Literal[True] = True
    installation_target_verified: Literal[True] = True
    previous_pointer_cas_frozen: Literal[True] = True
    percentage_cohort_membership_verified: Literal[True] = True
    deployment_intent_authority: Literal[True] = True
    boot_required: Literal[True] = True
    boot_executed: Literal[False] = False
    activation_intent_authority: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    deployment_receipt_authority: Literal[False] = False
    process_started: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        assignment = self.assignment
        plan = self.plan
        admission = self.archive_admission
        credential = self.installation_credential
        slot = admission.installed_slot
        build = admission.download_receipt.resolution.entry.build_attestation.payload
        canonical_workspace = str(Path(self.workspace_root).expanduser().resolve())
        if not (
            self.workspace_root == canonical_workspace == assignment.workspace_root
            and assignment.member_selected
            and assignment.percentage_cohort_membership_authority
            and assignment.plan_id == plan.plan_id
            and assignment.plan_sha256 == plan.plan_sha256
            and assignment.candidate_id == plan.candidate_id
            and assignment.candidate_revision == plan.candidate_revision
            and assignment.candidate_target == plan.target_head
        ):
            raise ValueError("Percentage Deployment Intent Assignment/Plan 投影不一致。")
        if not (
            credential.credential_id == assignment.credential_id
            and credential.credential_sha256 == assignment.credential_sha256
            and credential.payload.member_id == assignment.member_id
            and credential.payload.channel == assignment.channel
            and credential.payload.installation_public_key_sha256
            == assignment.installation_public_key_sha256
        ):
            raise ValueError("Percentage Deployment Intent Credential 投影不一致。")
        if not (
            admission.percentage_deployment_intent_input_authority
            and slot.target == self.installation_target
            and admission.download_receipt.resolution.channel == assignment.channel
            and admission.download_receipt.resolution.target == self.installation_target
            and slot.source_commit == plan.target_head == build.source_commit
            and slot.source_tree_sha256 == plan.target_tree_sha256
            == build.source_tree_sha256
            and _target_platform(self.installation_target) in plan.required_platforms
        ):
            raise ValueError("Percentage Deployment Intent archive/target 投影不一致。")
        if not (
            self.previous_pointer.current_slot_id != slot.slot_id
            and self.expected_previous_pointer_sha256
            == self.previous_pointer.pointer_sha256
            and self.expected_activation_generation
            == self.previous_pointer.generation + 1
            and self.candidate_slot_id == slot.slot_id
            and self.candidate_slot_sha256 == slot.slot_sha256
            and self.candidate_manifest_sha256 == slot.manifest_sha256
            and self.candidate_version == slot.version
            and self.candidate_source_commit == slot.source_commit
            and self.candidate_source_tree_sha256 == slot.source_tree_sha256
        ):
            raise ValueError("Percentage Deployment Intent pointer/slot 投影不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        expected_expiry = min(
            issued + timedelta(seconds=_MAX_INTENT_TTL_SECONDS),
            _aware(assignment.stage_advance.expires_at),
            _aware(credential.payload.expires_at),
        )
        if not (
            issued >= _aware(assignment.assigned_at)
            and issued >= _aware(admission.admitted_at)
            and issued < expires
            and expires == expected_expiry
        ):
            raise ValueError("Percentage Deployment Intent authority window 无效。")
        core = self.model_dump(mode="json", exclude={"intent_id", "intent_sha256"})
        digest = _digest(core)
        if not (
            self.intent_sha256 == digest
            and self.intent_id == f"evrepercentintent_{digest[:24]}"
        ):
            raise ValueError("Percentage Deployment Intent content identity 不一致。")
        return self


class EvolutionRevalidationPercentageDeploymentIntentView(_StrictModel):
    intent: EvolutionRevalidationPercentageDeploymentIntent
    intent_source_current: bool
    assignment_current: bool
    plan_current: bool
    archive_admission_current: bool
    credential_current: bool
    installation_target_current: bool
    previous_pointer_current: bool
    candidate_slot_current: bool
    candidate_slot_inactive: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    deployment_intent_authority: bool
    boot_executed: Literal[False] = False
    activation_intent_authority: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    deployment_receipt_authority: Literal[False] = False
    process_started: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.intent_source_current
            and self.assignment_current
            and self.plan_current
            and self.archive_admission_current
            and self.credential_current
            and self.installation_target_current
            and self.previous_pointer_current
            and self.candidate_slot_current
            and self.candidate_slot_inactive
            and not self.expired
        )
        if not (
            self.deployment_intent_authority is current
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Percentage Deployment Intent View authority 投影不一致。")
        return self


class EvolutionRevalidationPercentageDeploymentIntentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPercentageDeploymentIntentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        assignment_service: EvolutionRevalidationPercentageCohortAssignmentService,
        archive_service: ReleaseArchiveAdmissionService,
        installation_target_provider: Callable[[], str] = host_release_target,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            assignment_service,
            EvolutionRevalidationPercentageCohortAssignmentService,
        ):
            raise TypeError("Percentage Deployment Intent Store 需要 Assignment Service。")
        if not isinstance(archive_service, ReleaseArchiveAdmissionService):
            raise TypeError("Percentage Deployment Intent Store 需要 Archive Admission Service。")
        if not callable(installation_target_provider):
            raise TypeError(
                "Percentage Deployment Intent Store 需要 installation target provider。"
            )
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != assignment_service.store.db_path:
            raise ValueError("Percentage Deployment Intent 必须共用 Evolution evidence DB。")
        self.assignment_service = assignment_service
        self.archive_service = archive_service
        self.slot_store = archive_service.slot_store
        self.installation_target_provider = installation_target_provider
        self.clock = clock or (lambda: datetime.now(UTC))

    async def get_by_assignment(
        self,
        assignment_id: str,
    ) -> EvolutionRevalidationPercentageDeploymentIntent | None:
        _require_id(assignment_id, r"^evrepercentassign_[0-9a-f]{24}$", "Assignment")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT intent_json FROM "
                        "evolution_revalidation_percentage_deployment_intents "
                        "WHERE assignment_id = ?",
                        (assignment_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["intent_json"]))
        except EvolutionRevalidationPercentageDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_source_invalid",
                "Percentage Deployment Intent durable source 无效。",
            ) from exc

    async def record(
        self,
        intent: EvolutionRevalidationPercentageDeploymentIntent,
    ) -> EvolutionRevalidationPercentageDeploymentIntent:
        item = _validated(intent)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_oversized",
                "Percentage Deployment Intent 超过 8 MiB。",
            )
        now = _aware(self.clock())
        if not (_aware(item.issued_at) <= now < _aware(item.expires_at)):
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_not_current",
                "Percentage Deployment Intent 不在当前 authority window。",
            )
        await self._require_live_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return await self._record_transaction(item, encoded)
        except EvolutionRevalidationPercentageDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_store_error",
                "Percentage Deployment Intent 无法持久化。",
            ) from exc

    async def _record_transaction(self, item, encoded):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            assignment_row = await (
                await db.execute(
                    "SELECT assignment_json FROM "
                    "evolution_revalidation_percentage_cohort_assignments "
                    "WHERE assignment_id = ?",
                    (item.assignment.assignment_id,),
                )
            ).fetchone()
            plan_row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                    "WHERE plan_id = ?",
                    (item.plan.plan_id,),
                )
            ).fetchone()
            try:
                assignment = (
                    None
                    if assignment_row is None
                    else EvolutionRevalidationPercentageCohortAssignment.model_validate_json(
                        assignment_row["assignment_json"]
                    )
                )
                plan = (
                    None
                    if plan_row is None
                    else EvolutionRevalidationRolloutPlan.model_validate_json(
                        plan_row["plan_json"]
                    )
                )
            except (TypeError, ValueError) as exc:
                await db.rollback()
                raise EvolutionRevalidationPercentageDeploymentIntentError(
                    "percentage_deployment_intent_dependency_invalid",
                    "Percentage Deployment Intent durable dependency 无效。",
                ) from exc
            if assignment != item.assignment or plan != item.plan:
                await db.rollback()
                raise EvolutionRevalidationPercentageDeploymentIntentError(
                    "percentage_deployment_intent_dependency_changed",
                    "Percentage Assignment 或 Rollout Plan 已变化。",
                )
            existing = await (
                await db.execute(
                    "SELECT intent_json FROM "
                    "evolution_revalidation_percentage_deployment_intents "
                    "WHERE assignment_id = ? OR admission_id = ?",
                    (
                        item.assignment.assignment_id,
                        item.archive_admission.admission_id,
                    ),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore(str(existing["intent_json"]))
                await db.rollback()
                if not _same_authority_source(restored, item):
                    raise EvolutionRevalidationPercentageDeploymentIntentError(
                        "percentage_deployment_intent_conflict",
                        "Assignment 或 Archive Admission 已绑定不同 Intent。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_percentage_deployment_intents "
                "(intent_id, intent_sha256, assignment_id, admission_id, member_id, "
                "intent_json, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.intent_id,
                    item.intent_sha256,
                    item.assignment.assignment_id,
                    item.archive_admission.admission_id,
                    item.assignment.member_id,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    async def _require_live_sources(self, item) -> None:
        state = await _live_state(
            item,
            assignment_service=self.assignment_service,
            archive_service=self.archive_service,
            slot_store=self.slot_store,
            installation_target_provider=self.installation_target_provider,
        )
        if not all(state.values()):
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_source_changed",
                "Assignment、Admission、Credential、target 或 previous pointer 已变化。",
            )


class EvolutionRevalidationPercentageDeploymentIntentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        assignment_service: EvolutionRevalidationPercentageCohortAssignmentService,
        archive_service: ReleaseArchiveAdmissionService,
        store: EvolutionRevalidationPercentageDeploymentIntentStore,
        installation_target_provider: Callable[[], str] = host_release_target,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(
                assignment_service,
                EvolutionRevalidationPercentageCohortAssignmentService,
            )
            and isinstance(archive_service, ReleaseArchiveAdmissionService)
            and isinstance(store, EvolutionRevalidationPercentageDeploymentIntentStore)
            and store.assignment_service is assignment_service
            and store.archive_service is archive_service
            and store.installation_target_provider is installation_target_provider
            and (clock is None or store.clock is clock)
        ):
            raise ValueError("Percentage Deployment Intent Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != assignment_service.workspace_root:
            raise ValueError("Percentage Deployment Intent workspace 必须一致。")
        self.assignment_service = assignment_service
        self.archive_service = archive_service
        self.store = store
        self.slot_store = archive_service.slot_store
        self.installation_target_provider = installation_target_provider
        self.clock = store.clock if clock is None else clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def issue(
        self,
        *,
        advance_receipt_id: str,
        snapshot_id: str,
        member_id: str,
        download_source_id: str,
    ) -> EvolutionRevalidationPercentageDeploymentIntentView:
        _require_issue_ids(
            advance_receipt_id=advance_receipt_id,
            snapshot_id=snapshot_id,
            member_id=member_id,
            download_source_id=download_source_id,
        )
        lock_key = f"{advance_receipt_id}:{snapshot_id}:{member_id}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            sources = await self._current_sources(
                advance_receipt_id=advance_receipt_id,
                snapshot_id=snapshot_id,
                member_id=member_id,
                download_source_id=download_source_id,
            )
            assignment, plan, admission, credential, target, pointer = sources
            existing = await self.store.get_by_assignment(assignment.assignment_id)
            if existing is not None:
                if existing.archive_admission.admission_id != admission.admission_id:
                    raise EvolutionRevalidationPercentageDeploymentIntentError(
                        "percentage_deployment_intent_already_issued",
                        "该 Percentage Assignment 已签发过另一条一次性 Intent。",
                    )
                return await self._view(existing)
            now = _aware(self.clock())
            expires = min(
                now + timedelta(seconds=_MAX_INTENT_TTL_SECONDS),
                _aware(assignment.stage_advance.expires_at),
                _aware(credential.payload.expires_at),
            )
            if expires <= now:
                raise EvolutionRevalidationPercentageDeploymentIntentError(
                    "percentage_deployment_intent_window_expired",
                    "Percentage Deployment Intent authority window 已结束。",
                )
            refreshed = await self._current_sources(
                advance_receipt_id=advance_receipt_id,
                snapshot_id=snapshot_id,
                member_id=member_id,
                download_source_id=download_source_id,
            )
            if refreshed != sources:
                raise EvolutionRevalidationPercentageDeploymentIntentError(
                    "percentage_deployment_intent_context_changed",
                    "签发期间 Percentage Deployment Intent source 已变化。",
                )
            intent = _build_intent(
                workspace_root=self.workspace_root,
                assignment=assignment,
                plan=plan,
                admission=admission,
                credential=credential,
                installation_target=target,
                previous_pointer=pointer,
                issued_at=now,
                expires_at=expires,
            )
            stored = await self.store.record(intent)
            view = await self._view(stored)
            if not view.deployment_intent_authority:
                raise EvolutionRevalidationPercentageDeploymentIntentError(
                    "percentage_deployment_intent_revoked_during_issue",
                    "Intent 落盘时 source 已变化；历史 artifact 已撤权。",
                )
            return view

    async def inspect(
        self,
        *,
        assignment_id: str,
    ) -> EvolutionRevalidationPercentageDeploymentIntentView:
        intent = await self.store.get_by_assignment(assignment_id)
        if intent is None:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_missing",
                "指定 Percentage Assignment 尚无 Deployment Intent。",
            )
        return await self._view(intent)

    async def _current_sources(
        self,
        *,
        advance_receipt_id,
        snapshot_id,
        member_id,
        download_source_id,
    ):
        try:
            assignment_view = await self.assignment_service.inspect(
                advance_receipt_id=advance_receipt_id,
                snapshot_id=snapshot_id,
                member_id=member_id,
            )
            plan_view = await self.assignment_service.plan_service.inspect(
                plan_id=assignment_view.assignment.plan_id
            )
            snapshot_view = await self.assignment_service.population_store.inspect(
                snapshot_id=snapshot_id
            )
            admission_view = await self.archive_service.inspect(
                download_source_id=download_source_id
            )
            target = self.installation_target_provider()
            pointer = await asyncio.to_thread(self.slot_store.active)
        except (
            EvolutionRevalidationPercentageCohortAssignmentError,
            EvolutionRevalidationRolloutPlanError,
            ReleasePopulationRegistryError,
            ReleaseArchiveAdmissionError,
            ReleaseSlotError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_source_unavailable",
                "Percentage Deployment Intent 缺少 current authority source。",
            ) from exc
        assignment = assignment_view.assignment
        admission = admission_view.receipt
        credential = _credential(snapshot_view.snapshot.payload.credentials, member_id)
        if not assignment_view.percentage_cohort_membership_authority:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_member_not_selected",
                "当前 Installation 不属于 selected percentage cohort。",
            )
        if credential is None:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_credential_missing",
                "Current Population Snapshot 缺少 Installation Credential。",
            )
        if pointer is None:
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_previous_pointer_missing",
                "Percentage rollout 需要 existing active pointer 作为 CAS baseline。",
            )
        if not (
            plan_view.current_rollout_eligible
            and admission_view.percentage_deployment_intent_input_authority
            and _authority_sources_match(
                assignment=assignment,
                plan=plan_view.plan,
                admission=admission,
                credential=credential,
                installation_target=target,
                previous_pointer=pointer,
            )
        ):
            raise EvolutionRevalidationPercentageDeploymentIntentError(
                "percentage_deployment_intent_source_denied",
                "Assignment、Admission、Credential、target 或 pointer 不允许签发 Intent。",
            )
        return assignment, plan_view.plan, admission, credential, target, pointer

    async def _view(self, intent):
        state = await _live_state(
            intent,
            assignment_service=self.assignment_service,
            archive_service=self.archive_service,
            slot_store=self.slot_store,
            installation_target_provider=self.installation_target_provider,
        )
        intent_source_current = False
        try:
            stored = await self.store.get_by_assignment(intent.assignment.assignment_id)
            intent_source_current = stored == intent
        except EvolutionRevalidationPercentageDeploymentIntentError:
            pass
        expired = _aware(self.clock()) >= _aware(intent.expires_at)
        checks = {
            "intent_source_changed": intent_source_current,
            **state,
            "intent_expired": not expired,
        }
        reasons = tuple(sorted(reason for reason, passed in checks.items() if not passed))
        return EvolutionRevalidationPercentageDeploymentIntentView(
            intent=intent,
            intent_source_current=intent_source_current,
            assignment_current=state["assignment_changed"],
            plan_current=state["plan_changed"],
            archive_admission_current=state["archive_admission_changed"],
            credential_current=state["credential_changed"],
            installation_target_current=state["installation_target_changed"],
            previous_pointer_current=state["previous_pointer_changed"],
            candidate_slot_current=state["candidate_slot_changed"],
            candidate_slot_inactive=state["candidate_slot_no_longer_inactive"],
            expired=expired,
            invalidation_reasons=reasons,
            deployment_intent_authority=all(checks.values()),
        )


async def _live_state(
    intent,
    *,
    assignment_service,
    archive_service,
    slot_store,
    installation_target_provider,
):
    assignment_current = False
    plan_current = False
    admission_current = False
    credential_current = False
    target_current = False
    pointer_current = False
    slot_current = False
    slot_inactive = False
    try:
        view = await assignment_service.inspect(
            advance_receipt_id=intent.assignment.stage_advance.receipt_id,
            snapshot_id=intent.assignment.population_snapshot_id,
            member_id=intent.assignment.member_id,
        )
        assignment_current = bool(
            view.assignment == intent.assignment
            and view.percentage_cohort_membership_authority
        )
    except EvolutionRevalidationPercentageCohortAssignmentError:
        pass
    try:
        view = await assignment_service.plan_service.inspect(plan_id=intent.plan.plan_id)
        plan_current = bool(
            view.plan == intent.plan and view.current_rollout_eligible
        )
    except EvolutionRevalidationRolloutPlanError:
        pass
    try:
        view = await archive_service.inspect(
            download_source_id=intent.archive_admission.download_receipt.source_id
        )
        admission_current = bool(
            view.receipt == intent.archive_admission
            and view.percentage_deployment_intent_input_authority
        )
    except ReleaseArchiveAdmissionError:
        pass
    try:
        snapshot = await assignment_service.population_store.inspect(
            snapshot_id=intent.assignment.population_snapshot_id
        )
        credential = _credential(
            snapshot.snapshot.payload.credentials,
            intent.assignment.member_id,
        )
        credential_current = bool(
            snapshot.population_snapshot_authority
            and credential == intent.installation_credential
        )
    except ReleasePopulationRegistryError:
        pass
    try:
        target_current = installation_target_provider() == intent.installation_target
    except (ReleaseSlotError, OSError, TypeError, ValueError):
        pass
    try:
        pointer = await asyncio.to_thread(slot_store.active)
        pointer_current = pointer == intent.previous_pointer
        slot_inactive = bool(
            pointer is not None and pointer.current_slot_id != intent.candidate_slot_id
        )
        slot = await asyncio.to_thread(
            slot_store.inspect_installed_slot,
            intent.candidate_slot_id,
        )
        slot_current = slot == intent.archive_admission.installed_slot
    except (ReleaseSlotError, OSError, TypeError, ValueError):
        pass
    return {
        "assignment_changed": assignment_current,
        "plan_changed": plan_current,
        "archive_admission_changed": admission_current,
        "credential_changed": credential_current,
        "installation_target_changed": target_current,
        "previous_pointer_changed": pointer_current,
        "candidate_slot_changed": slot_current,
        "candidate_slot_no_longer_inactive": slot_inactive,
    }


def _authority_sources_match(
    *, assignment, plan, admission, credential, installation_target, previous_pointer
) -> bool:
    slot = admission.installed_slot
    build = admission.download_receipt.resolution.entry.build_attestation.payload
    try:
        platform = _target_platform(installation_target)
    except ValueError:
        return False
    return bool(
        assignment.member_selected
        and assignment.percentage_cohort_membership_authority
        and assignment.plan_id == plan.plan_id
        and assignment.plan_sha256 == plan.plan_sha256
        and assignment.candidate_id == plan.candidate_id
        and assignment.candidate_revision == plan.candidate_revision
        and assignment.candidate_target == plan.target_head
        and credential.credential_id == assignment.credential_id
        and credential.credential_sha256 == assignment.credential_sha256
        and credential.payload.member_id == assignment.member_id
        and credential.payload.channel == assignment.channel
        and credential.payload.installation_public_key_sha256
        == assignment.installation_public_key_sha256
        and admission.download_receipt.resolution.channel == assignment.channel
        and admission.download_receipt.resolution.target == installation_target
        and slot.target == installation_target
        and platform in plan.required_platforms
        and slot.source_commit == plan.target_head == build.source_commit
        and slot.source_tree_sha256 == plan.target_tree_sha256
        == build.source_tree_sha256
        and previous_pointer.current_slot_id != slot.slot_id
    )


def _build_intent(
    *,
    workspace_root,
    assignment,
    plan,
    admission,
    credential,
    installation_target,
    previous_pointer,
    issued_at,
    expires_at,
):
    slot = admission.installed_slot
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PERCENTAGE_DEPLOYMENT_INTENT_POLICY,
        "workspace_root": str(workspace_root),
        "assignment": assignment,
        "plan": plan,
        "archive_admission": admission,
        "installation_credential": credential,
        "installation_target": installation_target,
        "previous_pointer": previous_pointer,
        "expected_previous_pointer_sha256": previous_pointer.pointer_sha256,
        "expected_activation_generation": previous_pointer.generation + 1,
        "candidate_slot_id": slot.slot_id,
        "candidate_slot_sha256": slot.slot_sha256,
        "candidate_manifest_sha256": slot.manifest_sha256,
        "candidate_version": slot.version,
        "candidate_source_commit": slot.source_commit,
        "candidate_source_tree_sha256": slot.source_tree_sha256,
        "issued_at": _aware(issued_at).isoformat(),
        "expires_at": _aware(expires_at).isoformat(),
        "authorization_mode": "current_stage_advance_selected_managed_installation",
        "explicit_user_prompt_required": False,
        "one_time_issuance": True,
        "assignment_current_at_issue": True,
        "archive_admission_current_at_issue": True,
        "managed_installation_credential_verified": True,
        "installation_target_verified": True,
        "previous_pointer_cas_frozen": True,
        "percentage_cohort_membership_verified": True,
        "deployment_intent_authority": True,
        "boot_required": True,
        "boot_executed": False,
        "activation_intent_authority": False,
        "active_pointer_switched": False,
        "deployment_receipt_authority": False,
        "process_started": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest_core = {
        **core,
        "assignment": assignment.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "archive_admission": admission.model_dump(mode="json"),
        "installation_credential": credential.model_dump(mode="json"),
        "previous_pointer": previous_pointer.model_dump(mode="json"),
    }
    digest = _digest(digest_core)
    try:
        return EvolutionRevalidationPercentageDeploymentIntent.model_validate(
            {
                **core,
                "intent_id": f"evrepercentintent_{digest[:24]}",
                "intent_sha256": digest,
            }
        )
    except ValueError as exc:
        raise EvolutionRevalidationPercentageDeploymentIntentError(
            "percentage_deployment_intent_invalid",
            "Percentage Deployment Intent artifact 无效。",
        ) from exc


def _credential(credentials, member_id):
    return next(
        (item for item in credentials if item.payload.member_id == member_id),
        None,
    )


def _target_platform(target: str) -> Literal["linux", "macos", "windows"]:
    prefix = target.split("-", 1)[0].casefold()
    if prefix not in {"linux", "macos", "windows"}:
        raise ValueError("Installation target platform 无效。")
    return prefix  # type: ignore[return-value]


def _same_authority_source(left, right) -> bool:
    return bool(
        left.assignment == right.assignment
        and left.plan == right.plan
        and left.archive_admission == right.archive_admission
        and left.installation_credential == right.installation_credential
        and left.installation_target == right.installation_target
        and left.previous_pointer == right.previous_pointer
    )


def _validated(value) -> EvolutionRevalidationPercentageDeploymentIntent:
    try:
        return EvolutionRevalidationPercentageDeploymentIntent.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageDeploymentIntentError(
            "percentage_deployment_intent_invalid",
            "Percentage Deployment Intent artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationPercentageDeploymentIntent:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Percentage Deployment Intent durable source 超过 8 MiB。")
    return EvolutionRevalidationPercentageDeploymentIntent.model_validate_json(value)


def _require_issue_ids(**values) -> None:
    patterns = {
        "advance_receipt_id": r"^evreoptinadvance_[0-9a-f]{24}$",
        "snapshot_id": r"^relpopsnapshot_[0-9a-f]{24}$",
        "member_id": r"^relpopmember_[0-9a-f]{24}$",
        "download_source_id": r"^reldownloadsource_[0-9a-f]{24}$",
    }
    for name, value in values.items():
        _require_id(value, patterns[name], name)


def _require_id(value, pattern, label) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise EvolutionRevalidationPercentageDeploymentIntentError(
            "percentage_deployment_intent_identifier_invalid",
            f"{label} identifier 无效。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_revalidation_percentage_deployment_intents ("
        "intent_id TEXT PRIMARY KEY, intent_sha256 TEXT NOT NULL UNIQUE, "
        "assignment_id TEXT NOT NULL UNIQUE, admission_id TEXT NOT NULL UNIQUE, "
        "member_id TEXT NOT NULL, intent_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Percentage Deployment Intent timestamp 必须包含 offset。")
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
    "EVOLUTION_REVALIDATION_PERCENTAGE_DEPLOYMENT_INTENT_POLICY",
    "EvolutionRevalidationPercentageDeploymentIntent",
    "EvolutionRevalidationPercentageDeploymentIntentError",
    "EvolutionRevalidationPercentageDeploymentIntentService",
    "EvolutionRevalidationPercentageDeploymentIntentStore",
    "EvolutionRevalidationPercentageDeploymentIntentView",
]
