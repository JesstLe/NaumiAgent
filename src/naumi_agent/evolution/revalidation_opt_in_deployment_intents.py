"""Durable explicit opt-in enrollment and activation intent for one installation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_candidate_bundle_admissions import (
    EvolutionRevalidationCandidateBundleAdmission,
    EvolutionRevalidationCandidateBundleAdmissionError,
    EvolutionRevalidationCandidateBundleAdmissionService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutStage,
    EvolutionRevalidationRolloutStageName,
)
from naumi_agent.evolution.revalidation_rollout_stage_advances import (
    EvolutionRevalidationRolloutStageAdvanceError,
    EvolutionRevalidationRolloutStageAdvanceReceipt,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_INTENT_POLICY = (
    "evolution-revalidation-opt-in-deployment-intent-v1"
)
EVOLUTION_REVALIDATION_OPT_IN_COHORT_POLICY = (
    "evolution-revalidation-opt-in-cohort-v1"
)
_INTERACTION_RE = re.compile(r"^ask-evredeploy-([0-9a-f]{24})-([1-9][0-9]{0,3})$")
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInCohort(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-opt-in-cohort-v1"] = (
        EVOLUTION_REVALIDATION_OPT_IN_COHORT_POLICY
    )
    cohort_id: str = Field(pattern=r"^evreoptincohort_[0-9a-f]{24}$")
    cohort_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    installation_id: str = Field(pattern=r"^evreinstall_[0-9a-f]{24}$")
    admission_id: str = Field(pattern=r"^evrecandidatebundle_[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=128)
    candidate_source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    stage: EvolutionRevalidationRolloutStage
    stage_sha256: str = Field(pattern=_SHA256_RE)
    interaction: HarnessInteractionRecord
    interaction_request_sha256: str = Field(pattern=_SHA256_RE)
    assignment_mode: Literal["explicit_local_installation_opt_in"] = (
        "explicit_local_installation_opt_in"
    )
    local_installation_member: Literal[True] = True
    explicit_user_opt_in: Literal[True] = True
    population_assignment_enforced: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    enrolled_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Opt-in Cohort workspace 必须 canonical。")
        if not (
            self.stage.order == 2
            and self.stage.name is EvolutionRevalidationRolloutStageName.OPT_IN
            and self.stage.exposure.value == "opt_in"
            and self.stage.exposure_percent == 1
            and self.stage.predecessor is EvolutionRevalidationRolloutStageName.LOCAL_CANARY
        ):
            raise ValueError("Opt-in Cohort stage projection 无效。")
        if self.stage_sha256 != _digest(self.stage.model_dump(mode="json")):
            raise ValueError("Opt-in Cohort stage digest 不一致。")
        interaction = self.interaction
        if not (
            interaction.state == "answered"
            and interaction.answer_kind == "option"
            and interaction.answer_value == "enroll"
            and interaction.answered_by == "user"
            and interaction.subject_kind == "tool"
            and interaction.subject_id == self.admission_id
            and interaction.answer_label
        ):
            raise ValueError("Opt-in Cohort 未绑定明确用户 enrollment。")
        if self.interaction_request_sha256 != _digest(
            _static_request_payload(interaction.request())
        ):
            raise ValueError("Opt-in Cohort interaction request 摘要不一致。")
        if _aware(self.enrolled_at) != _aware(interaction.answered_at):
            raise ValueError("Opt-in Cohort enrolled_at 未绑定用户答案。")
        core = self.model_dump(mode="json", exclude={"cohort_id", "cohort_sha256"})
        digest = _digest(core)
        if self.cohort_sha256 != digest or self.cohort_id != f"evreoptincohort_{digest[:24]}":
            raise ValueError("Opt-in Cohort identity 不一致。")
        return self


class EvolutionRevalidationOptInDeploymentIntent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-opt-in-deployment-intent-v1"] = (
        EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_INTENT_POLICY
    )
    intent_id: str = Field(pattern=r"^evredeployintent_[0-9a-f]{24}$")
    intent_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    admission: EvolutionRevalidationCandidateBundleAdmission
    stage_advance: EvolutionRevalidationRolloutStageAdvanceReceipt
    cohort: EvolutionRevalidationOptInCohort
    expected_previous_pointer_sha256: str = Field(pattern=_SHA256_RE)
    expected_activation_generation: int = Field(ge=2)
    activation_target_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    activation_target_slot_sha256: str = Field(pattern=_SHA256_RE)
    activation_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    activation_boot_receipt_sha256: str = Field(pattern=_SHA256_RE)
    build_trust_policy_sha256: str = Field(pattern=_SHA256_RE)
    build_attestation_sha256: str = Field(pattern=_SHA256_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    admission_current_at_issue: Literal[True] = True
    stage_advance_current_at_issue: Literal[True] = True
    build_trust_current_at_issue: Literal[True] = True
    explicit_opt_in_verified: Literal[True] = True
    activation_intent_authority: Literal[True] = True
    active_pointer_switched: Literal[False] = False
    deployment_receipt_authority: Literal[False] = False
    process_started: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        admission = self.admission
        advance = self.stage_advance
        cohort = self.cohort
        if self.workspace_root != admission.workspace_root or self.workspace_root != str(
            Path(self.workspace_root).expanduser().resolve()
        ):
            raise ValueError("Deployment Intent workspace projection 不一致。")
        if not (
            advance.receipt_id == admission.stage_advance_receipt_id
            and advance.receipt_sha256 == admission.stage_advance_receipt_sha256
            and advance.completion_id == admission.completion_id
            and advance.plan_id == admission.plan_id
            and advance.plan_sha256 == admission.plan_sha256
            and advance.decision == "advance"
            and advance.next_stage == "opt_in"
        ):
            raise ValueError("Deployment Intent Stage Advance projection 不一致。")
        if not (
            cohort.workspace_root == admission.workspace_root
            and cohort.admission_id == admission.admission_id
            and cohort.plan_id == admission.plan_id
            and cohort.plan_sha256 == admission.plan_sha256
            and cohort.candidate_id == admission.candidate_id
            and cohort.candidate_revision == admission.candidate_revision
            and cohort.candidate_version == admission.candidate_slot.version
            and cohort.candidate_target == admission.candidate_slot.target
            and cohort.candidate_source_commit == admission.target_commit
        ):
            raise ValueError("Deployment Intent Cohort projection 不一致。")
        if not (
            self.expected_previous_pointer_sha256
            == admission.previous_pointer.pointer_sha256
            and self.expected_activation_generation
            == admission.previous_pointer.generation + 1
            and self.activation_target_slot_id == admission.candidate_slot.slot_id
            and self.activation_target_slot_sha256 == admission.candidate_slot.slot_sha256
            and self.activation_boot_receipt_id == admission.boot_receipt.receipt_id
            and self.activation_boot_receipt_sha256
            == admission.boot_receipt.receipt_sha256
            and self.build_trust_policy_sha256
            == admission.build_trust_policy_sha256
            and self.build_attestation_sha256
            == admission.build_attestation.attestation_sha256
        ):
            raise ValueError("Deployment Intent activation projection 不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if not (
            issued == _aware(cohort.enrolled_at)
            and _aware(advance.issued_at) <= issued < expires
            and expires == _aware(advance.expires_at)
        ):
            raise ValueError("Deployment Intent authority window 无效。")
        core = self.model_dump(mode="json", exclude={"intent_id", "intent_sha256"})
        digest = _digest(core)
        if self.intent_sha256 != digest or self.intent_id != f"evredeployintent_{digest[:24]}":
            raise ValueError("Deployment Intent identity 不一致。")
        return self


class EvolutionRevalidationOptInDeploymentIntentView(_StrictModel):
    intent: EvolutionRevalidationOptInDeploymentIntent
    admission_current: bool
    stage_advance_current: bool
    active_pointer_current: bool
    candidate_slot_current: bool
    boot_receipt_current: bool
    build_trust_current: bool
    opt_in_enrollment_current: bool
    expired: bool
    activation_intent_authority: bool

    @model_validator(mode="after")
    def _project(self) -> Self:
        expected = bool(
            self.intent.activation_intent_authority
            and self.admission_current
            and self.stage_advance_current
            and self.active_pointer_current
            and self.candidate_slot_current
            and self.boot_receipt_current
            and self.build_trust_current
            and self.opt_in_enrollment_current
            and not self.expired
        )
        if self.activation_intent_authority is not expected:
            raise ValueError("Deployment Intent view authority projection 不一致。")
        return self


class EvolutionRevalidationOptInDeploymentIntentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOptInDeploymentIntentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        interaction_store: HarnessStore,
        release_root: str | Path,
    ) -> None:
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Deployment Intent Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        self.interaction_store = interaction_store
        self.release_root = Path(release_root).expanduser().resolve()

    async def get_by_admission(
        self, admission_id: str
    ) -> EvolutionRevalidationOptInDeploymentIntent | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT intent_json FROM "
                    "evolution_revalidation_opt_in_deployment_intents "
                    "WHERE admission_id = ?",
                    (admission_id,),
                )
            ).fetchone()
        return None if row is None else _restore(row["intent_json"])

    async def get_by_completion(
        self, completion_id: str
    ) -> EvolutionRevalidationOptInDeploymentIntent | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT intent_json FROM "
                    "evolution_revalidation_opt_in_deployment_intents "
                    "WHERE completion_id = ?",
                    (completion_id,),
                )
            ).fetchone()
        return None if row is None else _restore(row["intent_json"])

    async def record(
        self, intent: EvolutionRevalidationOptInDeploymentIntent
    ) -> EvolutionRevalidationOptInDeploymentIntent:
        try:
            item = EvolutionRevalidationOptInDeploymentIntent.model_validate_json(
                intent.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_invalid",
                "Opt-in Deployment Intent artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_oversized",
                "Opt-in Deployment Intent 超过 4 MiB。",
            )
        await self._require_interaction(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                admission_row = await (
                    await db.execute(
                        "SELECT admission_json FROM "
                        "evolution_revalidation_candidate_bundle_admissions_v2 "
                        "WHERE admission_id = ?",
                        (item.admission.admission_id,),
                    )
                ).fetchone()
                advance_row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_rollout_stage_advances "
                        "WHERE receipt_id = ?",
                        (item.stage_advance.receipt_id,),
                    )
                ).fetchone()
                plan_row = await (
                    await db.execute(
                        "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                        "WHERE plan_id = ?",
                        (item.admission.plan_id,),
                    )
                ).fetchone()
                control_row = await (
                    await db.execute(
                        "SELECT sequence, event_sha256, state FROM "
                        "evolution_revalidation_rollout_control_events "
                        "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                        (item.workspace_root,),
                    )
                ).fetchone()
                control = (
                    (0, "", "active")
                    if control_row is None
                    else (
                        control_row["sequence"],
                        control_row["event_sha256"],
                        control_row["state"],
                    )
                )
                source_admission = (
                    None
                    if admission_row is None
                    else EvolutionRevalidationCandidateBundleAdmission.model_validate_json(
                        admission_row["admission_json"]
                    )
                )
                source_advance = (
                    None
                    if advance_row is None
                    else EvolutionRevalidationRolloutStageAdvanceReceipt.model_validate_json(
                        advance_row["receipt_json"]
                    )
                )
                source_plan = (
                    None
                    if plan_row is None
                    else EvolutionRevalidationRolloutPlan.model_validate_json(
                        plan_row["plan_json"]
                    )
                )
                if not (
                    source_admission == item.admission
                    and source_advance == item.stage_advance
                    and source_plan is not None
                    and _matches_plan(item, source_plan, self.release_root)
                    and control
                    == (
                        item.stage_advance.control_sequence,
                        item.stage_advance.control_event_sha256,
                        "active",
                    )
                ):
                    await db.rollback()
                    raise EvolutionRevalidationOptInDeploymentIntentError(
                        "deployment_intent_dependency_changed",
                        "Candidate Admission、Stage Advance 或 Rollout Plan 已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT intent_json FROM "
                        "evolution_revalidation_opt_in_deployment_intents "
                        "WHERE admission_id = ?",
                        (item.admission.admission_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["intent_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationOptInDeploymentIntentError(
                            "deployment_intent_conflict",
                            "同一 Candidate Admission 已绑定不同 Deployment Intent。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_deployment_intents "
                    "(intent_id, intent_sha256, admission_id, completion_id, "
                    "interaction_id, intent_json, issued_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.intent_id,
                        item.intent_sha256,
                        item.admission.admission_id,
                        item.admission.completion_id,
                        item.cohort.interaction.interaction_id,
                        encoded,
                        item.issued_at,
                        item.expires_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationOptInDeploymentIntentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_store_error",
                "Opt-in Deployment Intent 无法持久化。",
            ) from exc
        return item

    async def _require_interaction(
        self, intent: EvolutionRevalidationOptInDeploymentIntent
    ) -> None:
        interaction = intent.cohort.interaction
        try:
            authoritative = await self.interaction_store.get_interaction(
                workspace_root=intent.workspace_root,
                interaction_id=interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_interaction_read_failed",
                "无法重读 opt-in interaction authority。",
            ) from exc
        if authoritative != interaction:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_interaction_mismatch",
                "Deployment Intent 未绑定 authoritative Harness interaction。",
            )


class EvolutionRevalidationOptInDeploymentIntentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        candidate_service: EvolutionRevalidationCandidateBundleAdmissionService,
        interaction_store: HarnessStore,
        store: EvolutionRevalidationOptInDeploymentIntentStore,
        request_user_input: RequestUserInputCallback,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(request_user_input):
            raise TypeError("Deployment Intent Service 需要用户交互 callback。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.candidate_service = candidate_service
        self.interaction_store = interaction_store
        self.store = store
        self.request_user_input = request_user_input
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def authorize(self, *, completion_id: str):
        lock = self._locks.setdefault(completion_id, asyncio.Lock())
        async with lock:
            admission_view = await self._current_admission(completion_id)
            admission = admission_view.admission
            existing = await self.store.get_by_admission(admission.admission_id)
            if existing is not None:
                return await self._view(existing)
            plan_view = await self.candidate_service.plan_service.inspect(
                plan_id=admission.plan_id
            )
            if not plan_view.current_rollout_eligible or plan_view.plan.plan_sha256 != (
                admission.plan_sha256
            ):
                raise EvolutionRevalidationOptInDeploymentIntentError(
                    "deployment_intent_plan_stale",
                    "Rollout Plan 已失效，不能创建部署意图。",
                )
            stage = plan_view.plan.stages[1]
            interaction = await self._manual_enrollment(admission, stage)
            if interaction.answer_value != "enroll":
                raise EvolutionRevalidationOptInDeploymentIntentError(
                    "deployment_opt_in_declined",
                    "用户未同意将当前安装加入 opt-in cohort。",
                )
            refreshed = await self._current_admission(completion_id)
            if refreshed.admission != admission:
                raise EvolutionRevalidationOptInDeploymentIntentError(
                    "deployment_intent_admission_changed",
                    "用户回答后 Candidate Admission 已变化。",
                )
            try:
                advance_view = await self.candidate_service.stage_advance_service.inspect(
                    completion_id=completion_id
                )
            except EvolutionRevalidationRolloutStageAdvanceError as exc:
                raise EvolutionRevalidationOptInDeploymentIntentError(
                    "deployment_intent_stage_advance_stale",
                    "Stage Advance 已失效，不能创建部署意图。",
                ) from exc
            if not advance_view.next_stage_entry_authority:
                raise EvolutionRevalidationOptInDeploymentIntentError(
                    "deployment_intent_stage_advance_denied",
                    "Stage Advance 当前不允许进入 opt-in。",
                )
            cohort = _build_cohort(
                workspace_root=self.workspace_root,
                release_root=self.candidate_service.release_slot_store.release_root,
                admission=admission,
                plan=plan_view.plan,
                stage=stage,
                interaction=interaction,
            )
            intent = _build_intent(
                workspace_root=self.workspace_root,
                admission=admission,
                advance=advance_view.receipt,
                cohort=cohort,
            )
            stored = await self.store.record(intent)
            return await self._view(stored)

    async def inspect(self, *, completion_id: str):
        intent = await self.store.get_by_completion(completion_id)
        if intent is None:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_missing",
                "尚未形成 Opt-in Deployment Intent。",
            )
        return await self._view(intent)

    async def _current_admission(self, completion_id):
        try:
            view = await self.candidate_service.inspect(completion_id=completion_id)
        except EvolutionRevalidationCandidateBundleAdmissionError as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_admission_stale",
                "Candidate Bundle Admission 已失效。",
            ) from exc
        if not view.activation_input_authority:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_admission_denied",
                "Candidate Bundle Admission 当前没有 activation input authority。",
            )
        return view

    async def _manual_enrollment(self, admission, stage):
        try:
            history = await self.interaction_store.list_interactions(
                workspace_root=self.workspace_root,
                subject_kind="tool",
                subject_ids=(admission.admission_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_history_failed",
                "无法读取本机 opt-in 持久交互历史。",
            ) from exc
        valid = tuple(item for item in history if _interaction_matches(item, admission, stage))
        answered = tuple(item for item in valid if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_answer_ambiguous",
                "同一 Candidate Admission 存在多个 opt-in 答案。",
            )
        if answered:
            return answered[0]
        pending = next((item for item in valid if item.state == "pending"), None)
        if pending is not None:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_interaction_pending",
                f"Opt-in 交互 {pending.interaction_id} 仍待回答。",
            )
        try:
            advance_view = await self.candidate_service.stage_advance_service.inspect(
                completion_id=admission.completion_id
            )
        except EvolutionRevalidationRolloutStageAdvanceError as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_intent_stage_advance_stale",
                "Stage Advance 已失效，不能创建 opt-in enrollment。",
            ) from exc
        remaining = int(
            (
                _aware(advance_view.receipt.expires_at)
                - self._now()
            ).total_seconds()
        )
        if remaining < 3:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_window_too_short",
                "Stage Advance 剩余时间不足以完成 opt-in enrollment。",
            )
        request = _interaction_request(
            admission,
            stage,
            timeout_seconds=min(remaining, 604_800),
        )
        interaction_id = _next_interaction_id(admission, history)
        try:
            await self.request_user_input(
                {
                    **request.to_public_dict(),
                    "_interaction_id": interaction_id,
                    "_durable_subject_kind": "tool",
                    "_durable_subject_id": admission.admission_id,
                }
            )
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_interaction_unavailable",
                "当前界面无法创建持久 opt-in enrollment。",
            ) from exc
        except ValueError as exc:
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_interaction_invalid",
                "Opt-in enrollment 未通过运行时协议校验。",
            ) from exc
        interaction = await self.interaction_store.get_interaction(
            workspace_root=self.workspace_root,
            interaction_id=interaction_id,
        )
        if interaction is None or interaction.state != "answered":
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_answer_not_committed",
                "用户 opt-in 答案尚未提交到 Harness authority。",
            )
        if not _interaction_matches(interaction, admission, stage):
            raise EvolutionRevalidationOptInDeploymentIntentError(
                "deployment_opt_in_answer_mismatch",
                "用户答案没有绑定 exact Candidate Admission。",
            )
        return interaction

    async def _view(self, intent):
        try:
            admission_view = await self.candidate_service.inspect(
                completion_id=intent.admission.completion_id
            )
            admission_current = admission_view.admission == intent.admission
            active_pointer_current = admission_view.active_pointer_current
            candidate_slot_current = admission_view.slot_current
            boot_receipt_current = admission_view.boot_receipt_current
            build_trust_current = bool(
                admission_view.trust_policy_current
                and admission_view.build_attestation_current
            )
        except EvolutionRevalidationCandidateBundleAdmissionError:
            admission_current = False
            active_pointer_current = False
            candidate_slot_current = False
            boot_receipt_current = False
            build_trust_current = False
        try:
            advance_view = await self.candidate_service.stage_advance_service.inspect(
                completion_id=intent.admission.completion_id
            )
            stage_advance_current = bool(
                advance_view.receipt == intent.stage_advance
                and advance_view.next_stage_entry_authority
            )
        except EvolutionRevalidationRolloutStageAdvanceError:
            stage_advance_current = False
        try:
            authoritative = await self.interaction_store.get_interaction(
                workspace_root=self.workspace_root,
                interaction_id=intent.cohort.interaction.interaction_id,
            )
            opt_in_enrollment_current = authoritative == intent.cohort.interaction
        except (HarnessStoreError, OSError, TypeError, ValueError):
            opt_in_enrollment_current = False
        expired = self._now() >= _aware(intent.expires_at)
        return EvolutionRevalidationOptInDeploymentIntentView(
            intent=intent,
            admission_current=admission_current,
            stage_advance_current=stage_advance_current,
            active_pointer_current=active_pointer_current,
            candidate_slot_current=candidate_slot_current,
            boot_receipt_current=boot_receipt_current,
            build_trust_current=build_trust_current,
            opt_in_enrollment_current=opt_in_enrollment_current,
            expired=expired,
            activation_intent_authority=(
                admission_current
                and stage_advance_current
                and active_pointer_current
                and candidate_slot_current
                and boot_receipt_current
                and build_trust_current
                and opt_in_enrollment_current
                and not expired
            ),
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


def _build_cohort(*, workspace_root, release_root, admission, plan, stage, interaction):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_COHORT_POLICY,
        "workspace_root": str(workspace_root),
        "installation_id": _installation_id(release_root),
        "admission_id": admission.admission_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "candidate_id": admission.candidate_id,
        "candidate_revision": admission.candidate_revision,
        "candidate_version": admission.candidate_slot.version,
        "candidate_target": admission.candidate_slot.target,
        "candidate_source_commit": admission.target_commit,
        "stage": stage,
        "stage_sha256": _digest(stage.model_dump(mode="json")),
        "interaction": interaction,
        "interaction_request_sha256": _digest(
            _static_request_payload(interaction.request())
        ),
        "assignment_mode": "explicit_local_installation_opt_in",
        "local_installation_member": True,
        "explicit_user_opt_in": True,
        "population_assignment_enforced": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "enrolled_at": _aware(interaction.answered_at).isoformat(),
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInCohort.model_validate(
        {**core, "cohort_id": f"evreoptincohort_{digest[:24]}", "cohort_sha256": digest}
    )


def _build_intent(*, workspace_root, admission, advance, cohort):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_INTENT_POLICY,
        "workspace_root": str(workspace_root),
        "admission": admission,
        "stage_advance": advance,
        "cohort": cohort,
        "expected_previous_pointer_sha256": admission.previous_pointer.pointer_sha256,
        "expected_activation_generation": admission.previous_pointer.generation + 1,
        "activation_target_slot_id": admission.candidate_slot.slot_id,
        "activation_target_slot_sha256": admission.candidate_slot.slot_sha256,
        "activation_boot_receipt_id": admission.boot_receipt.receipt_id,
        "activation_boot_receipt_sha256": admission.boot_receipt.receipt_sha256,
        "build_trust_policy_sha256": admission.build_trust_policy_sha256,
        "build_attestation_sha256": admission.build_attestation.attestation_sha256,
        "issued_at": cohort.enrolled_at,
        "expires_at": advance.expires_at,
        "admission_current_at_issue": True,
        "stage_advance_current_at_issue": True,
        "build_trust_current_at_issue": True,
        "explicit_opt_in_verified": True,
        "activation_intent_authority": True,
        "active_pointer_switched": False,
        "deployment_receipt_authority": False,
        "process_started": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "rollback_executed": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInDeploymentIntent.model_validate(
        {**core, "intent_id": f"evredeployintent_{digest[:24]}", "intent_sha256": digest}
    )


def _interaction_request(admission, stage, *, timeout_seconds):
    return normalize_interaction_request(
        {
            "header": "候选版本本机 Opt-in",
            "question": (
                f"是否将当前 Naumi 安装加入 opt-in cohort，并准备把 active runtime 从 "
                f"{admission.previous_slot.version} 切换到已验签候选 "
                f"{admission.candidate_slot.version}？该操作仅影响本机安装，保留旧版本槽供回滚；"
                "不会启动新进程，也不授权 percentage、stable、发布或推送。"
            ),
            "options": [
                {
                    "value": "enroll",
                    "label": "加入并准备切换",
                    "description": (
                        f"登记本机为 {stage.exposure_percent}% opt-in 阶段成员，"
                        "并生成短期 CAS 激活意图。"
                    ),
                },
                {
                    "value": "cancel",
                    "label": "保持当前版本",
                    "description": "记录拒绝；不生成激活意图，不改变 active runtime。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义部署决定",
            "timeout_seconds": timeout_seconds,
            "priority": "critical",
        }
    )


def _interaction_matches(interaction, admission, stage) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and match.group(1) == admission.admission_id.removeprefix("evrecandidatebundle_")
        and interaction.subject_kind == "tool"
        and interaction.subject_id == admission.admission_id
        and _static_request_payload(interaction.request())
        == _static_request_payload(
            _interaction_request(admission, stage, timeout_seconds=None)
        )
    )


def _next_interaction_id(admission, history) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(2)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionRevalidationOptInDeploymentIntentError(
            "deployment_opt_in_attempts_exhausted",
            "Opt-in enrollment 交互次数已达上限。",
        )
    suffix = admission.admission_id.removeprefix("evrecandidatebundle_")
    return f"ask-evredeploy-{suffix}-{attempt}"


def _static_request_payload(request: UserInteractionRequest) -> dict[str, object]:
    return {**request.to_public_dict(), "timeout_seconds": None}


def _matches_plan(intent, plan, release_root) -> bool:
    return bool(
        intent.admission.plan_id == plan.plan_id
        and intent.admission.plan_sha256 == plan.plan_sha256
        and intent.cohort.plan_id == plan.plan_id
        and intent.cohort.plan_sha256 == plan.plan_sha256
        and intent.cohort.stage == plan.stages[1]
        and intent.cohort.stage_sha256 == _digest(plan.stages[1].model_dump(mode="json"))
        and plan.stages[1].name is EvolutionRevalidationRolloutStageName.OPT_IN
        and intent.cohort.installation_id == _installation_id(release_root)
        and _interaction_matches(
            intent.cohort.interaction,
            intent.admission,
            plan.stages[1],
        )
    )


def _installation_id(release_root) -> str:
    digest = hashlib.sha256(
        str(Path(release_root).expanduser().resolve()).encode("utf-8")
    ).hexdigest()
    return f"evreinstall_{digest[:24]}"


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Deployment Intent timestamp 必须包含时区。")
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


def _restore(encoded: str) -> EvolutionRevalidationOptInDeploymentIntent:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Opt-in Deployment Intent 超过 4 MiB。")
    return EvolutionRevalidationOptInDeploymentIntent.model_validate_json(encoded)


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_deployment_intents ("
        "intent_id TEXT PRIMARY KEY, intent_sha256 TEXT NOT NULL UNIQUE, "
        "admission_id TEXT NOT NULL UNIQUE, completion_id TEXT NOT NULL UNIQUE, "
        "interaction_id TEXT NOT NULL UNIQUE, intent_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_COHORT_POLICY",
    "EVOLUTION_REVALIDATION_OPT_IN_DEPLOYMENT_INTENT_POLICY",
    "EvolutionRevalidationOptInCohort",
    "EvolutionRevalidationOptInDeploymentIntent",
    "EvolutionRevalidationOptInDeploymentIntentError",
    "EvolutionRevalidationOptInDeploymentIntentService",
    "EvolutionRevalidationOptInDeploymentIntentStore",
    "EvolutionRevalidationOptInDeploymentIntentView",
]
