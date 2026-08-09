"""Authorize one exact opt-in to percentage stage transition."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_stage_completions import (
    EvolutionRevalidationOptInStageCompletion,
    EvolutionRevalidationOptInStageCompletionError,
    EvolutionRevalidationOptInStageCompletionService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutStageName,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
    EvolutionRevalidationRolloutStageEntryError,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_REVALIDATION_OPT_IN_STAGE_ADVANCE_POLICY = (
    "evolution-revalidation-opt-in-stage-advance-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_INTERACTION_RE = re.compile(r"^ask-evreoptinadvance-([0-9a-f]{24})-([1-9][0-9]{0,3})$")
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_INTERACTION_HISTORY = 100
_CONCURRENT_ANSWER_WAIT_SECONDS = 3.0

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInStageAdvanceReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-stage-advance-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_STAGE_ADVANCE_POLICY
    receipt_id: str = Field(pattern=r"^evreoptinadvance_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    subject_id: str = Field(min_length=1, max_length=128)
    stage_completion_evidence_id: str = Field(
        pattern=r"^evreoptincomplete_[0-9a-f]{24}$"
    )
    stage_completion_evidence_sha256: str = Field(pattern=_SHA256_RE)
    stage_completion_source_set_sha256: str = Field(pattern=_SHA256_RE)
    completion_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    completed_stage: Literal["opt_in"] = "opt_in"
    next_stage: Literal["percentage"] = "percentage"
    percentage_exposure_percent: int = Field(ge=1, le=100)
    manual_advance_required: bool
    automatic_advance_eligible: bool
    manual_interaction_required: bool
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    decision: Literal["advance", "decline"]
    decision_source: Literal["automatic", "manual"]
    interaction: HarnessInteractionRecord | None = None
    interaction_request_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    stage_completion_current: Literal[True] = True
    control_active: Literal[True] = True
    advance_authorized: bool
    next_stage_entry_authority: bool
    percentage_stage_entry_authority: bool
    percentage_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Opt-in Stage Advance workspace 必须 canonical。")
        if bool(self.control_sequence) is not bool(self.control_event_sha256):
            raise ValueError("Opt-in Stage Advance control projection 不一致。")
        if not (
            self.automatic_advance_eligible is (not self.manual_advance_required)
            and self.manual_interaction_required is self.manual_advance_required
            and self.decision_source
            == ("manual" if self.manual_advance_required else "automatic")
        ):
            raise ValueError("Opt-in Stage Advance policy projection 不一致。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if not issued < expires:
            raise ValueError("Opt-in Stage Advance 有效期无效。")
        authorized = self.decision == "advance"
        if not (
            self.advance_authorized is authorized
            and self.next_stage_entry_authority is authorized
            and self.percentage_stage_entry_authority is authorized
        ):
            raise ValueError("Opt-in Stage Advance authority projection 不一致。")
        if self.decision_source == "automatic":
            if self.interaction is not None or self.interaction_request_sha256:
                raise ValueError("自动 Opt-in Stage Advance 不得伪造用户交互。")
            if self.decision != "advance":
                raise ValueError("自动 Opt-in Stage Advance 只能投影 advance。")
        else:
            interaction = self.interaction
            if interaction is None or not (
                interaction.state == "answered"
                and interaction.answer_kind == "option"
                and interaction.answer_value == self.decision
                and interaction.answered_by == "user"
                and interaction.subject_kind == "tool"
                and interaction.subject_id == self.stage_completion_evidence_id
            ):
                raise ValueError("手动 Opt-in Stage Advance 未绑定结构化用户答案。")
            match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
            if match is None or match.group(1) != self.stage_completion_evidence_id.removeprefix(
                "evreoptincomplete_"
            ):
                raise ValueError("Opt-in Stage Advance interaction identity 无效。")
            request_digest = _digest(_static_request_payload(interaction.request()))
            if not hmac.compare_digest(request_digest, self.interaction_request_sha256):
                raise ValueError("Opt-in Stage Advance interaction request 摘要不一致。")
            if _aware(interaction.answered_at) != issued:
                raise ValueError("Opt-in Stage Advance issued_at 未绑定用户答案。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evreoptinadvance_{digest[:24]}"
        ):
            raise ValueError("Opt-in Stage Advance identity 不一致。")
        return self


class EvolutionRevalidationOptInStageAdvanceView(_StrictModel):
    receipt: EvolutionRevalidationOptInStageAdvanceReceipt
    receipt_source_current: bool
    stage_completion_current: bool
    plan_source_current: bool
    control_current: bool
    interaction_current: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    next_stage_entry_authority: bool
    percentage_stage_entry_authority: bool
    percentage_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.receipt_source_current
            and self.stage_completion_current
            and self.plan_source_current
            and self.control_current
            and self.interaction_current
            and not self.expired
        )
        expected = current and self.receipt.advance_authorized
        if not (
            self.next_stage_entry_authority is expected
            and self.percentage_stage_entry_authority is expected
            and tuple(sorted(set(self.invalidation_reasons)))
            == self.invalidation_reasons
        ):
            raise ValueError("Opt-in Stage Advance View authority projection 不一致。")
        return self


class EvolutionRevalidationOptInStageAdvanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOptInStageAdvanceStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        completion_service: EvolutionRevalidationOptInStageCompletionService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        control_store: EvolutionRevalidationRolloutControlStore,
        interaction_store: HarnessStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(
            completion_service, EvolutionRevalidationOptInStageCompletionService
        ):
            raise TypeError("Opt-in Stage Advance Store 需要 Stage Completion Service。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Opt-in Stage Advance Store 需要 Rollout Plan Service。")
        if not isinstance(control_store, EvolutionRevalidationRolloutControlStore):
            raise TypeError("Opt-in Stage Advance Store 需要 Rollout Control Store。")
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Opt-in Stage Advance Store 需要 Harness Store。")
        if not (
            self.db_path == completion_service.store.db_path
            == plan_service.store.db_path
            == control_store.db_path
        ):
            raise ValueError("Opt-in Stage Advance 必须共用同一 Evolution evidence DB。")
        self.completion_service = completion_service
        self.completion_store = completion_service.store
        self.plan_service = plan_service
        self.control_store = control_store
        self.interaction_store = interaction_store

    async def get_by_completion_evidence(
        self, evidence_id: str
    ) -> EvolutionRevalidationOptInStageAdvanceReceipt | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_opt_in_stage_advances "
                        "WHERE stage_completion_evidence_id = ?",
                        (evidence_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(row["receipt_json"])
            await self._require_interaction(item)
            return item
        except EvolutionRevalidationOptInStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_store_corrupt",
                "Opt-in Stage Advance Receipt 损坏或无法读取。",
            ) from exc

    async def record(
        self, receipt: EvolutionRevalidationOptInStageAdvanceReceipt
    ) -> EvolutionRevalidationOptInStageAdvanceReceipt:
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_oversized",
                "Opt-in Stage Advance Receipt 超过 512 KiB。",
            )
        await self._require_live_sources(item)
        await self._require_interaction(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await self._require_durable_sources(db, item)
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_opt_in_stage_advances "
                        "WHERE stage_completion_evidence_id = ?",
                        (item.stage_completion_evidence_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["receipt_json"])
                    await db.rollback()
                    if not _same_decision_source(restored, item):
                        raise EvolutionRevalidationOptInStageAdvanceError(
                            "opt_in_stage_advance_conflict",
                            "同一 Opt-in Stage Completion 已绑定不同推进决定。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_stage_advances "
                    "(receipt_id, receipt_sha256, stage_completion_evidence_id, "
                    "stage_completion_evidence_sha256, decision, receipt_json, issued_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.stage_completion_evidence_id,
                        item.stage_completion_evidence_sha256,
                        item.decision,
                        encoded,
                        item.issued_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationOptInStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_store_error",
                "Opt-in Stage Advance Receipt 无法持久化。",
            ) from exc
        return item

    async def _require_live_sources(
        self, item: EvolutionRevalidationOptInStageAdvanceReceipt
    ) -> None:
        try:
            completion_view = await self.completion_service.inspect(
                evidence_id=item.stage_completion_evidence_id,
                subject_id=item.subject_id,
            )
            plan_view = await self.plan_service.inspect(plan_id=item.plan_id)
            control = await self.control_store.latest(item.workspace_root)
        except (
            EvolutionRevalidationOptInStageCompletionError,
            EvolutionRevalidationRolloutPlanError,
            EvolutionRevalidationRolloutStageEntryError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_source_unavailable",
                "Opt-in Stage Advance 的 current source 当前不可用。",
            ) from exc
        if not _live_sources_match(item, completion_view, plan_view.plan, control):
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_source_changed",
                "Opt-in Stage Completion、Plan 或 rollout control 已变化。",
            )

    async def _require_durable_sources(
        self,
        db: aiosqlite.Connection,
        item: EvolutionRevalidationOptInStageAdvanceReceipt,
    ) -> None:
        completion_row = await (
            await db.execute(
                "SELECT evidence_sha256, evidence_json FROM "
                "evolution_revalidation_opt_in_stage_completions "
                "WHERE evidence_id = ?",
                (item.stage_completion_evidence_id,),
            )
        ).fetchone()
        plan_row = await (
            await db.execute(
                "SELECT plan_sha256, plan_json FROM evolution_revalidation_rollout_plans "
                "WHERE plan_id = ?",
                (item.plan_id,),
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
        try:
            completion = (
                None
                if completion_row is None
                else EvolutionRevalidationOptInStageCompletion.model_validate_json(
                    completion_row["evidence_json"]
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
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_dependency_invalid",
                "Opt-in Stage Advance durable source 无效。",
            ) from exc
        control_projection = (
            (0, "", "active")
            if control_row is None
            else (
                control_row["sequence"],
                control_row["event_sha256"],
                control_row["state"],
            )
        )
        if not (
            completion is not None
            and completion_row["evidence_sha256"]
            == item.stage_completion_evidence_sha256
            and plan is not None
            and plan_row["plan_sha256"] == item.plan_sha256
            and _receipt_matches_sources(item, completion, plan)
            and control_projection
            == (item.control_sequence, item.control_event_sha256, "active")
        ):
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_dependency_changed",
                "Opt-in Stage Completion、Plan 或 rollout control 已变化。",
            )

    async def _require_interaction(
        self, item: EvolutionRevalidationOptInStageAdvanceReceipt
    ) -> None:
        if item.interaction is None:
            return
        try:
            authoritative = await self.interaction_store.get_interaction(
                workspace_root=item.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_read_failed",
                "无法重读 Opt-in Stage Advance interaction authority。",
            ) from exc
        if authoritative != item.interaction:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_mismatch",
                "Opt-in Stage Advance 未绑定 Harness interaction authority。",
            )


class EvolutionRevalidationOptInStageAdvanceService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_service: EvolutionRevalidationOptInStageCompletionService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        control_store: EvolutionRevalidationRolloutControlStore,
        interaction_store: HarnessStore,
        store: EvolutionRevalidationOptInStageAdvanceStore,
        request_user_input: RequestUserInputCallback,
        validity_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 30 <= validity_seconds <= 86_400:
            raise ValueError("Opt-in Stage Advance validity 必须在 30..86400 秒。")
        if not callable(request_user_input):
            raise TypeError("Opt-in Stage Advance Service 需要用户交互 callback。")
        if not (
            isinstance(completion_service, EvolutionRevalidationOptInStageCompletionService)
            and isinstance(plan_service, EvolutionRevalidationRolloutPlanService)
            and isinstance(control_store, EvolutionRevalidationRolloutControlStore)
            and isinstance(interaction_store, HarnessStore)
            and isinstance(store, EvolutionRevalidationOptInStageAdvanceStore)
        ):
            raise TypeError("Opt-in Stage Advance Service durable dependency 类型无效。")
        if not (
            store.completion_service is completion_service
            and store.plan_service is plan_service
            and store.control_store is control_store
            and store.interaction_store is interaction_store
        ):
            raise ValueError("Opt-in Stage Advance Service durable dependency 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            self.workspace_root == completion_service.workspace_root
            == plan_service.workspace_root
        ):
            raise ValueError("Opt-in Stage Advance workspace 必须一致。")
        self.completion_service = completion_service
        self.plan_service = plan_service
        self.control_store = control_store
        self.interaction_store = interaction_store
        self.store = store
        self.request_user_input = request_user_input
        self.validity_seconds = validity_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def authorize(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInStageAdvanceView:
        lock = self._locks.setdefault(evidence_id, asyncio.Lock())
        async with lock:
            completion, plan, control = await self._current_context(
                evidence_id=evidence_id,
                subject_id=subject_id,
            )
            stage = plan.stages[1]
            percentage_stage = plan.stages[2]
            existing = await self.store.get_by_completion_evidence(evidence_id)
            if existing is not None:
                return await self._view(existing)
            if stage.manual_advance_required:
                interaction = await self._manual_decision(completion, percentage_stage)
                refreshed, refreshed_plan, refreshed_control = await self._current_context(
                    evidence_id=evidence_id,
                    subject_id=subject_id,
                )
                if not (
                    refreshed == completion
                    and refreshed_plan == plan
                    and _control_projection(refreshed_control)
                    == _control_projection(control)
                ):
                    raise EvolutionRevalidationOptInStageAdvanceError(
                        "opt_in_stage_advance_context_changed",
                        "用户回答后 Opt-in Stage Advance source 已变化。",
                    )
                decision = interaction.answer_value
                decision_source = "manual"
            else:
                interaction = None
                decision = "advance"
                decision_source = "automatic"
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                completion=completion,
                plan=plan,
                control=control,
                decision=decision,
                decision_source=decision_source,
                interaction=interaction,
                now=self._now(),
                validity_seconds=self.validity_seconds,
            )
            stored = await self.store.record(receipt)
            return await self._view(stored)

    async def inspect(
        self, *, evidence_id: str
    ) -> EvolutionRevalidationOptInStageAdvanceView:
        receipt = await self.store.get_by_completion_evidence(evidence_id)
        if receipt is None:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_missing",
                "尚未形成 Opt-in Stage Advance Receipt。",
            )
        return await self._view(receipt)

    async def _current_context(self, *, evidence_id: str, subject_id: str):
        try:
            completion_view = await self.completion_service.inspect(
                evidence_id=evidence_id,
                subject_id=subject_id,
            )
        except EvolutionRevalidationOptInStageCompletionError as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_completion_stale",
                "Opt-in Stage Completion 已失效，不能推进 rollout。",
            ) from exc
        completion = completion_view.receipt
        if not completion_view.opt_in_stage_completion_authority:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_completion_denied",
                "Opt-in Stage Completion 当前没有推进权限。",
            )
        try:
            plan_view = await self.plan_service.inspect(plan_id=completion.plan_id)
        except EvolutionRevalidationRolloutPlanError as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_plan_stale",
                "Rollout Plan 已失效，不能推进 percentage。",
            ) from exc
        plan = plan_view.plan
        stage = plan.stages[1]
        if not (
            plan_view.current_rollout_eligible
            and plan.plan_sha256 == completion.plan_sha256
            and stage.name is EvolutionRevalidationRolloutStageName.OPT_IN
            and plan.stages[2].name is EvolutionRevalidationRolloutStageName.PERCENTAGE
        ):
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_plan_changed",
                "Rollout Plan 与 Opt-in Stage Completion 不一致。",
            )
        try:
            control = await self.control_store.latest(self.workspace_root)
        except EvolutionRevalidationRolloutStageEntryError as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_control_unavailable",
                "Rollout control source 当前不可验证。",
            ) from exc
        if control is not None and (
            control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
        ):
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_control_not_active",
                "Rollout 已暂停，不能推进 percentage stage。",
            )
        return completion, plan, control

    async def _manual_decision(self, completion, percentage_stage):
        try:
            history = await self.interaction_store.list_interactions(
                workspace_root=self.workspace_root,
                subject_kind="tool",
                subject_ids=(completion.evidence_id,),
                limit=_MAX_INTERACTION_HISTORY,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_history_failed",
                "无法读取 Opt-in Stage Advance 持久交互历史。",
            ) from exc
        if len(history) == _MAX_INTERACTION_HISTORY:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_history_limit",
                "Opt-in Stage Advance 交互历史已达 100 条安全上限。",
            )
        valid = tuple(
            item
            for item in history
            if _interaction_matches(item, completion, percentage_stage)
        )
        answered = tuple(item for item in valid if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_answer_ambiguous",
                "同一 Opt-in Stage Completion 存在多个已回答推进交互。",
            )
        if answered:
            return answered[0]
        pending = next((item for item in valid if item.state == "pending"), None)
        if pending is not None:
            return await self._await_concurrent_answer(
                pending,
                completion=completion,
                percentage_stage=percentage_stage,
            )
        request = _interaction_request(
            completion,
            percentage_stage,
            timeout_seconds=604_800,
        )
        interaction_id = _next_interaction_id(completion, history)
        try:
            await self.request_user_input(
                {
                    **request.to_public_dict(),
                    "_interaction_id": interaction_id,
                    "_durable_subject_kind": "tool",
                    "_durable_subject_id": completion.evidence_id,
                }
            )
        except HarnessStoreError as exc:
            try:
                recovered = await self.interaction_store.get_interaction(
                    workspace_root=self.workspace_root,
                    interaction_id=interaction_id,
                )
            except (HarnessStoreError, OSError, TypeError, ValueError):
                recovered = None
            if recovered is not None and (
                recovered.state == "answered"
                and _interaction_matches(recovered, completion, percentage_stage)
            ):
                return recovered
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_unavailable",
                "当前界面无法创建持久 Opt-in Stage Advance 交互。",
            ) from exc
        except UserInteractionUnavailableError as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_unavailable",
                "当前界面无法创建持久 Opt-in Stage Advance 交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_interaction_invalid",
                "Opt-in Stage Advance 交互未通过运行时协议校验。",
            ) from exc
        interaction = await self.interaction_store.get_interaction(
            workspace_root=self.workspace_root,
            interaction_id=interaction_id,
        )
        if interaction is None or interaction.state != "answered":
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_answer_not_committed",
                "用户答案尚未提交到 Harness authority。",
            )
        if not _interaction_matches(
            interaction, completion, percentage_stage
        ):
            raise EvolutionRevalidationOptInStageAdvanceError(
                "opt_in_stage_advance_answer_mismatch",
                "用户答案没有绑定 exact Opt-in Stage Completion。",
            )
        return interaction

    async def _await_concurrent_answer(
        self,
        pending: HarnessInteractionRecord,
        *,
        completion: EvolutionRevalidationOptInStageCompletion,
        percentage_stage,
    ) -> HarnessInteractionRecord:
        deadline = (
            asyncio.get_running_loop().time() + _CONCURRENT_ANSWER_WAIT_SECONDS
        )
        current = pending
        while current.state == "pending" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
            try:
                restored = await self.interaction_store.get_interaction(
                    workspace_root=self.workspace_root,
                    interaction_id=pending.interaction_id,
                )
            except (HarnessStoreError, OSError, TypeError, ValueError):
                restored = None
            if restored is None:
                break
            current = restored
        if current.state == "answered" and _interaction_matches(
            current, completion, percentage_stage
        ):
            return current
        raise EvolutionRevalidationOptInStageAdvanceError(
            "opt_in_stage_advance_interaction_pending",
            f"推进交互 {pending.interaction_id} 仍待回答。",
        )

    async def _view(
        self, receipt: EvolutionRevalidationOptInStageAdvanceReceipt
    ) -> EvolutionRevalidationOptInStageAdvanceView:
        reasons: list[str] = []
        try:
            restored = await self.store.get_by_completion_evidence(
                receipt.stage_completion_evidence_id
            )
            receipt_source_current = restored == receipt
        except EvolutionRevalidationOptInStageAdvanceError:
            receipt_source_current = False
        if not receipt_source_current:
            reasons.append("receipt_source_changed")
        try:
            completion_view = await self.completion_service.inspect(
                evidence_id=receipt.stage_completion_evidence_id,
                subject_id=receipt.subject_id,
            )
            stage_completion_current = bool(
                completion_view.receipt.evidence_sha256
                == receipt.stage_completion_evidence_sha256
                and completion_view.opt_in_stage_completion_authority
            )
        except EvolutionRevalidationOptInStageCompletionError:
            stage_completion_current = False
        if not stage_completion_current:
            reasons.append("stage_completion_changed")
        try:
            plan_view = await self.plan_service.inspect(plan_id=receipt.plan_id)
            plan = plan_view.plan
            stage = plan.stages[1]
            plan_source_current = bool(
                plan_view.current_rollout_eligible
                and plan.plan_sha256 == receipt.plan_sha256
                and _receipt_matches_stage(receipt, stage, plan)
            )
        except (EvolutionRevalidationRolloutPlanError, TypeError, ValueError):
            plan_source_current = False
        if not plan_source_current:
            reasons.append("plan_source_changed")
        try:
            control = await self.control_store.latest(self.workspace_root)
            control_current = _control_projection(control) == (
                receipt.control_sequence,
                receipt.control_event_sha256,
                "active",
            )
        except (
            EvolutionRevalidationRolloutStageEntryError,
            OSError,
            TypeError,
            ValueError,
        ):
            control_current = False
        if not control_current:
            reasons.append("rollout_control_changed")
        interaction_current = await self._interaction_current(receipt)
        if not interaction_current:
            reasons.append("interaction_source_changed")
        expired = self._now() >= _aware(receipt.expires_at)
        if expired:
            reasons.append("authority_expired")
        current = bool(
            receipt_source_current
            and stage_completion_current
            and plan_source_current
            and control_current
            and interaction_current
            and not expired
            and receipt.advance_authorized
        )
        return EvolutionRevalidationOptInStageAdvanceView(
            receipt=receipt,
            receipt_source_current=receipt_source_current,
            stage_completion_current=stage_completion_current,
            plan_source_current=plan_source_current,
            control_current=control_current,
            interaction_current=interaction_current,
            expired=expired,
            invalidation_reasons=tuple(sorted(set(reasons))),
            next_stage_entry_authority=current,
            percentage_stage_entry_authority=current,
        )

    async def _interaction_current(self, receipt) -> bool:
        if receipt.interaction is None:
            return receipt.decision_source == "automatic"
        try:
            current = await self.interaction_store.get_interaction(
                workspace_root=receipt.workspace_root,
                interaction_id=receipt.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError):
            return False
        return current == receipt.interaction

    def _now(self) -> datetime:
        return _aware(self.clock())


def _interaction_request(completion, percentage_stage, *, timeout_seconds):
    return normalize_interaction_request(
        {
            "header": "自进化 Opt-in 阶段推进",
            "question": (
                f"Opt-in 已通过 {completion.successful_runs} 次真实运行，错误率 "
                f"{completion.error_rate_basis_points / 100:.2f}%。是否授权候选进入 "
                f"percentage 阶段（目标 {percentage_stage.exposure_percent}%）？本选择只签发短期 "
                "Stage Entry authority，不会部署、扩大流量、发布或推送。"
            ),
            "options": [
                {
                    "value": "advance",
                    "label": "推进到 percentage",
                    "description": "签发短期且受 current evidence/control fencing 的阶段入口权限。",
                },
                {
                    "value": "decline",
                    "label": "停止推进",
                    "description": "持久记录拒绝决定，不产生下一阶段入口权限。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义推进决定",
            "timeout_seconds": timeout_seconds,
            "priority": "critical",
        }
    )


def _interaction_matches(interaction, completion, percentage_stage) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and match.group(1) == completion.evidence_id.removeprefix("evreoptincomplete_")
        and interaction.subject_kind == "tool"
        and interaction.subject_id == completion.evidence_id
        and _static_request_payload(interaction.request())
        == _static_request_payload(
            _interaction_request(
                completion,
                percentage_stage,
                timeout_seconds=None,
            )
        )
    )


def _next_interaction_id(completion, history) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(2)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionRevalidationOptInStageAdvanceError(
            "opt_in_stage_advance_attempts_exhausted",
            "Opt-in Stage Advance 交互次数已达上限。",
        )
    suffix = completion.evidence_id.removeprefix("evreoptincomplete_")
    return f"ask-evreoptinadvance-{suffix}-{attempt}"


def _build_receipt(
    *,
    workspace_root,
    completion,
    plan,
    control,
    decision,
    decision_source,
    interaction,
    now,
    validity_seconds,
):
    issued = _aware(interaction.answered_at) if interaction is not None else _aware(now)
    control_sequence, control_sha, _state = _control_projection(control)
    stage = plan.stages[1]
    percentage_stage = plan.stages[2]
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_STAGE_ADVANCE_POLICY,
        "workspace_root": str(workspace_root),
        "subject_id": completion.subject_id,
        "stage_completion_evidence_id": completion.evidence_id,
        "stage_completion_evidence_sha256": completion.evidence_sha256,
        "stage_completion_source_set_sha256": completion.source_set_sha256,
        "completion_id": completion.completion_id,
        "plan_id": completion.plan_id,
        "plan_sha256": completion.plan_sha256,
        "completed_stage": "opt_in",
        "next_stage": "percentage",
        "percentage_exposure_percent": percentage_stage.exposure_percent,
        "manual_advance_required": stage.manual_advance_required,
        "automatic_advance_eligible": not stage.manual_advance_required,
        "manual_interaction_required": stage.manual_advance_required,
        "control_sequence": control_sequence,
        "control_event_sha256": control_sha,
        "decision": decision,
        "decision_source": decision_source,
        "interaction": interaction,
        "interaction_request_sha256": (
            ""
            if interaction is None
            else _digest(_static_request_payload(interaction.request()))
        ),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=validity_seconds)).isoformat(),
        "stage_completion_current": True,
        "control_active": True,
        "advance_authorized": decision == "advance",
        "next_stage_entry_authority": decision == "advance",
        "percentage_stage_entry_authority": decision == "advance",
        "percentage_rollout_authority": False,
        "deployment_authority": False,
        "stable_rollout_authority": False,
        "rollback_authority": False,
        "promotion_authority": False,
        "git_write_executed": False,
        "publish_executed": False,
        "llm_generated": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInStageAdvanceReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evreoptinadvance_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _live_sources_match(item, completion_view, plan, control) -> bool:
    return bool(
        completion_view.opt_in_stage_completion_authority
        and completion_view.receipt.evidence_sha256
        == item.stage_completion_evidence_sha256
        and _receipt_matches_sources(item, completion_view.receipt, plan)
        and _control_projection(control)
        == (item.control_sequence, item.control_event_sha256, "active")
    )


def _receipt_matches_sources(item, completion, plan) -> bool:
    return bool(
        item.workspace_root == completion.workspace_root == plan.workspace_root
        and item.subject_id == completion.subject_id
        and item.stage_completion_evidence_id == completion.evidence_id
        and item.stage_completion_evidence_sha256 == completion.evidence_sha256
        and item.stage_completion_source_set_sha256 == completion.source_set_sha256
        and item.completion_id == completion.completion_id
        and item.plan_id == completion.plan_id == plan.plan_id
        and item.plan_sha256 == completion.plan_sha256 == plan.plan_sha256
        and _receipt_matches_stage(item, plan.stages[1], plan)
    )


def _receipt_matches_stage(item, stage, plan) -> bool:
    return bool(
        stage.name is EvolutionRevalidationRolloutStageName.OPT_IN
        and plan.stages[2].name is EvolutionRevalidationRolloutStageName.PERCENTAGE
        and item.completed_stage == stage.name.value
        and item.next_stage == plan.stages[2].name.value
        and item.percentage_exposure_percent == plan.stages[2].exposure_percent
        and item.manual_advance_required is stage.manual_advance_required
        and item.automatic_advance_eligible is (not stage.manual_advance_required)
        and item.manual_interaction_required is stage.manual_advance_required
    )


def _same_decision_source(left, right) -> bool:
    return bool(
        left.stage_completion_evidence_id == right.stage_completion_evidence_id
        and left.stage_completion_evidence_sha256
        == right.stage_completion_evidence_sha256
        and left.decision == right.decision
        and left.decision_source == right.decision_source
        and left.interaction == right.interaction
    )


def _control_projection(control) -> tuple[int, str, str]:
    if control is None:
        return 0, "", "active"
    return control.sequence, control.event_sha256, control.state.value


def _static_request_payload(request: UserInteractionRequest) -> dict[str, object]:
    return {**request.to_public_dict(), "timeout_seconds": None}


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Opt-in Stage Advance timestamp 必须包含 offset。")
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


def _validated(
    value: EvolutionRevalidationOptInStageAdvanceReceipt,
) -> EvolutionRevalidationOptInStageAdvanceReceipt:
    try:
        return EvolutionRevalidationOptInStageAdvanceReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInStageAdvanceError(
            "opt_in_stage_advance_artifact_invalid",
            "Opt-in Stage Advance Receipt 无效。",
        ) from exc


def _restore(encoded: str) -> EvolutionRevalidationOptInStageAdvanceReceipt:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Opt-in Stage Advance Receipt 超过 512 KiB。")
    return EvolutionRevalidationOptInStageAdvanceReceipt.model_validate_json(encoded)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_stage_advances ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "stage_completion_evidence_id TEXT NOT NULL UNIQUE, "
        "stage_completion_evidence_sha256 TEXT NOT NULL, decision TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, issued_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_STAGE_ADVANCE_POLICY",
    "EvolutionRevalidationOptInStageAdvanceError",
    "EvolutionRevalidationOptInStageAdvanceReceipt",
    "EvolutionRevalidationOptInStageAdvanceService",
    "EvolutionRevalidationOptInStageAdvanceStore",
    "EvolutionRevalidationOptInStageAdvanceView",
]
