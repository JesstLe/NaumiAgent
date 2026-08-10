"""Authorize one exact percentage to stable stage transition."""

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

from naumi_agent.evolution.revalidation_percentage_stage_completions import (
    EvolutionRevalidationPercentageStageCompletion,
    EvolutionRevalidationPercentageStageCompletionError,
    EvolutionRevalidationPercentageStageCompletionService,
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

EVOLUTION_REVALIDATION_PERCENTAGE_STAGE_ADVANCE_POLICY = (
    "evolution-revalidation-percentage-stage-advance-v1"
)
_INTERACTION_RE = re.compile(
    r"^ask-evrepercentadvance-([0-9a-f]{24})-([1-9][0-9]{0,3})$"
)
_EVIDENCE_RE = re.compile(r"^evrepercentcomplete_[0-9a-f]{24}$")
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
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


class EvolutionRevalidationPercentageStageAdvanceReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-percentage-stage-advance-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_STAGE_ADVANCE_POLICY
    receipt_id: str = Field(pattern=r"^evrepercentadvance_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    subject_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")
    stage_completion_evidence_id: str = Field(
        pattern=r"^evrepercentcomplete_[0-9a-f]{24}$"
    )
    stage_completion_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_completion_source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignment_id: str = Field(pattern=r"^evrepercentassign_[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_stage: Literal["percentage"] = "percentage"
    next_stage: Literal["stable"] = "stable"
    stable_exposure_percent: Literal[100] = 100
    manual_advance_required: Literal[True] = True
    automatic_advance_eligible: Literal[False] = False
    manual_interaction_required: Literal[True] = True
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    decision: Literal["advance", "decline"]
    decision_source: Literal["manual"] = "manual"
    interaction: HarnessInteractionRecord
    interaction_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    stage_completion_current: Literal[True] = True
    control_active: Literal[True] = True
    advance_authorized: bool
    next_stage_entry_authority: bool
    stable_stage_entry_authority: bool
    stable_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    publish_executed: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Percentage Stage Advance workspace 必须 canonical。")
        if bool(self.control_sequence) is not bool(self.control_event_sha256):
            raise ValueError("Percentage Stage Advance control projection 不一致。")
        issued = _aware(self.issued_at)
        if not issued < _aware(self.expires_at):
            raise ValueError("Percentage Stage Advance 有效期无效。")
        authorized = self.decision == "advance"
        if not (
            self.advance_authorized is authorized
            and self.next_stage_entry_authority is authorized
            and self.stable_stage_entry_authority is authorized
        ):
            raise ValueError("Percentage Stage Advance authority projection 不一致。")
        interaction = self.interaction
        if not (
            interaction.state == "answered"
            and interaction.answer_kind == "option"
            and interaction.answer_value == self.decision
            and interaction.answered_by == "user"
            and interaction.subject_kind == "tool"
            and interaction.subject_id == self.stage_completion_evidence_id
            and _aware(interaction.answered_at) == issued
        ):
            raise ValueError("Percentage Stage Advance 未绑定结构化用户答案。")
        match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
        if match is None or match.group(1) != self.stage_completion_evidence_id.removeprefix(
            "evrepercentcomplete_"
        ):
            raise ValueError("Percentage Stage Advance interaction identity 无效。")
        request_digest = _digest(_static_request_payload(interaction.request()))
        if not hmac.compare_digest(request_digest, self.interaction_request_sha256):
            raise ValueError("Percentage Stage Advance interaction request 摘要不一致。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evrepercentadvance_{digest[:24]}"
        ):
            raise ValueError("Percentage Stage Advance identity 不一致。")
        return self


class EvolutionRevalidationPercentageStageAdvanceView(_StrictModel):
    receipt: EvolutionRevalidationPercentageStageAdvanceReceipt
    receipt_source_current: bool
    stage_completion_current: bool
    plan_source_current: bool
    control_current: bool
    interaction_current: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    next_stage_entry_authority: bool
    stable_stage_entry_authority: bool
    stable_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
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
            and self.stable_stage_entry_authority is expected
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Percentage Stage Advance View projection 不一致。")
        return self


class EvolutionRevalidationPercentageStageAdvanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPercentageStageAdvanceStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        completion_service: EvolutionRevalidationPercentageStageCompletionService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        control_store: EvolutionRevalidationRolloutControlStore,
        interaction_store: HarnessStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(
            completion_service,
            EvolutionRevalidationPercentageStageCompletionService,
        ):
            raise TypeError("Percentage Stage Advance Store 需要 Completion Service。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Percentage Stage Advance Store 需要 Rollout Plan Service。")
        if not isinstance(control_store, EvolutionRevalidationRolloutControlStore):
            raise TypeError("Percentage Stage Advance Store 需要 Rollout Control Store。")
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Percentage Stage Advance Store 需要 Harness Store。")
        if not (
            self.db_path == completion_service.store.db_path
            == plan_service.store.db_path
            == control_store.db_path
        ):
            raise ValueError("Percentage Stage Advance 必须共用 Evolution evidence DB。")
        self.completion_service = completion_service
        self.completion_store = completion_service.store
        self.plan_service = plan_service
        self.control_store = control_store
        self.interaction_store = interaction_store

    async def get_by_completion_evidence(self, evidence_id: str):
        normalized = _evidence_id(evidence_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_percentage_stage_advances "
                        "WHERE stage_completion_evidence_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(str(row["receipt_json"]))
            await self._require_interaction(item)
            return item
        except EvolutionRevalidationPercentageStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_store_corrupt",
                "Percentage Stage Advance Receipt 损坏或无法读取。",
            ) from exc

    async def record(self, receipt):
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_oversized",
                "Percentage Stage Advance Receipt 超过 512 KiB。",
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
                        "evolution_revalidation_percentage_stage_advances "
                        "WHERE stage_completion_evidence_id = ?",
                        (item.stage_completion_evidence_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["receipt_json"]))
                    await db.rollback()
                    if not _same_decision_source(restored, item):
                        raise EvolutionRevalidationPercentageStageAdvanceError(
                            "percentage_stage_advance_conflict",
                            "同一 Percentage Completion 已绑定不同推进决定。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_percentage_stage_advances "
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
        except EvolutionRevalidationPercentageStageAdvanceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_store_error",
                "Percentage Stage Advance Receipt 无法持久化。",
            ) from exc
        return item

    async def _require_live_sources(self, item) -> None:
        try:
            completion_view = await self.completion_service.inspect(
                evidence_id=item.stage_completion_evidence_id,
                subject_id=item.subject_id,
            )
            plan_view = await self.plan_service.inspect(plan_id=item.plan_id)
            control = await self.control_store.latest(item.workspace_root)
        except (
            EvolutionRevalidationPercentageStageCompletionError,
            EvolutionRevalidationRolloutPlanError,
            EvolutionRevalidationRolloutStageEntryError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_source_unavailable",
                "Percentage Stage Advance current source 当前不可用。",
            ) from exc
        if not _live_sources_match(item, completion_view, plan_view.plan, control):
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_source_changed",
                "Percentage Completion、Plan 或 rollout control 已变化。",
            )

    async def _require_durable_sources(self, db, item) -> None:
        completion_row = await (
            await db.execute(
                "SELECT evidence_sha256, evidence_json FROM "
                "evolution_revalidation_percentage_stage_completions "
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
                else EvolutionRevalidationPercentageStageCompletion.model_validate_json(
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
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_dependency_invalid",
                "Percentage Stage Advance durable source 无效。",
            ) from exc
        control_projection = (
            (0, "", "active")
            if control_row is None
            else (
                int(control_row["sequence"]),
                str(control_row["event_sha256"]),
                str(control_row["state"]),
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
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_dependency_changed",
                "Percentage Completion、Plan 或 rollout control 已变化。",
            )

    async def _require_interaction(self, item) -> None:
        try:
            authoritative = await self.interaction_store.get_interaction(
                workspace_root=item.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_read_failed",
                "无法重读 Percentage Stage Advance interaction authority。",
            ) from exc
        if authoritative != item.interaction:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_mismatch",
                "Percentage Stage Advance 未绑定 Harness interaction authority。",
            )


class EvolutionRevalidationPercentageStageAdvanceService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_service: EvolutionRevalidationPercentageStageCompletionService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        control_store: EvolutionRevalidationRolloutControlStore,
        interaction_store: HarnessStore,
        store: EvolutionRevalidationPercentageStageAdvanceStore,
        request_user_input: RequestUserInputCallback,
        validity_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(validity_seconds, bool)
            or not isinstance(validity_seconds, int)
            or not 30 <= validity_seconds <= 86_400
        ):
            raise ValueError("Percentage Stage Advance validity 必须在 30..86400 秒。")
        if not callable(request_user_input):
            raise TypeError("Percentage Stage Advance 需要用户交互 callback。")
        if not (
            isinstance(
                completion_service,
                EvolutionRevalidationPercentageStageCompletionService,
            )
            and isinstance(plan_service, EvolutionRevalidationRolloutPlanService)
            and isinstance(control_store, EvolutionRevalidationRolloutControlStore)
            and isinstance(interaction_store, HarnessStore)
            and isinstance(store, EvolutionRevalidationPercentageStageAdvanceStore)
            and store.completion_service is completion_service
            and store.plan_service is plan_service
            and store.control_store is control_store
            and store.interaction_store is interaction_store
        ):
            raise ValueError("Percentage Stage Advance durable dependency 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            self.workspace_root == completion_service.workspace_root
            == plan_service.workspace_root
        ):
            raise ValueError("Percentage Stage Advance workspace 必须一致。")
        self.completion_service = completion_service
        self.plan_service = plan_service
        self.control_store = control_store
        self.interaction_store = interaction_store
        self.store = store
        self.request_user_input = request_user_input
        self.validity_seconds = validity_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def authorize(self, *, evidence_id: str, subject_id: str):
        evidence = _evidence_id(evidence_id)
        subject = _subject(subject_id)
        lock = self._locks.setdefault(evidence, asyncio.Lock())
        async with lock:
            completion, plan, control = await self._current_context(
                evidence_id=evidence,
                subject_id=subject,
            )
            existing = await self.store.get_by_completion_evidence(evidence)
            if existing is not None:
                return await self._view(existing)
            stable_stage = plan.stages[3]
            interaction = await self._manual_decision(completion, stable_stage)
            refreshed, refreshed_plan, refreshed_control = await self._current_context(
                evidence_id=evidence,
                subject_id=subject,
            )
            if not (
                refreshed == completion
                and refreshed_plan == plan
                and _control_projection(refreshed_control)
                == _control_projection(control)
            ):
                raise EvolutionRevalidationPercentageStageAdvanceError(
                    "percentage_stage_advance_context_changed",
                    "用户回答后 Percentage Stage Advance source 已变化。",
                )
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                completion=completion,
                plan=plan,
                control=control,
                decision=interaction.answer_value,
                interaction=interaction,
                validity_seconds=self.validity_seconds,
            )
            return await self._view(await self.store.record(receipt))

    async def inspect(self, *, evidence_id: str):
        receipt = await self.store.get_by_completion_evidence(evidence_id)
        if receipt is None:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_missing",
                "尚未形成 Percentage Stage Advance Receipt。",
            )
        return await self._view(receipt)

    async def _current_context(self, *, evidence_id: str, subject_id: str):
        try:
            completion_view = await self.completion_service.inspect(
                evidence_id=evidence_id,
                subject_id=subject_id,
            )
        except EvolutionRevalidationPercentageStageCompletionError as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_completion_stale",
                "Percentage Stage Completion 已失效，不能推进 stable。",
            ) from exc
        completion = completion_view.receipt
        if not completion_view.percentage_stage_completion_authority:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_completion_denied",
                "Percentage Stage Completion 当前没有推进权限。",
            )
        try:
            plan_view = await self.plan_service.inspect(plan_id=completion.plan_id)
        except EvolutionRevalidationRolloutPlanError as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_plan_stale",
                "Rollout Plan 已失效，不能推进 stable。",
            ) from exc
        plan = plan_view.plan
        if not (
            plan_view.current_rollout_eligible
            and plan.plan_sha256 == completion.plan_sha256
            and plan.stages[2].name is EvolutionRevalidationRolloutStageName.PERCENTAGE
            and plan.stages[3].name is EvolutionRevalidationRolloutStageName.STABLE
            and plan.stages[3].manual_advance_required
        ):
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_plan_changed",
                "Rollout Plan 与 Percentage Stage Completion 不一致。",
            )
        try:
            control = await self.control_store.latest(self.workspace_root)
        except EvolutionRevalidationRolloutStageEntryError as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_control_unavailable",
                "Rollout control source 当前不可验证。",
            ) from exc
        if control is not None and (
            control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
        ):
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_control_not_active",
                "Rollout 已暂停，不能推进 stable stage。",
            )
        return completion, plan, control

    async def _manual_decision(self, completion, stable_stage):
        try:
            history = await self.interaction_store.list_interactions(
                workspace_root=self.workspace_root,
                subject_kind="tool",
                subject_ids=(completion.evidence_id,),
                limit=_MAX_INTERACTION_HISTORY,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_history_failed",
                "无法读取 Percentage Stage Advance 持久交互历史。",
            ) from exc
        if len(history) == _MAX_INTERACTION_HISTORY:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_history_limit",
                "Percentage Stage Advance 交互历史已达 100 条安全上限。",
            )
        valid = tuple(
            item for item in history if _interaction_matches(item, completion, stable_stage)
        )
        answered = tuple(item for item in valid if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_answer_ambiguous",
                "同一 Percentage Completion 存在多个已回答推进交互。",
            )
        if answered:
            return answered[0]
        pending = next((item for item in valid if item.state == "pending"), None)
        if pending is not None:
            return await self._await_concurrent_answer(
                pending,
                completion=completion,
                stable_stage=stable_stage,
            )
        request = _interaction_request(completion, stable_stage, timeout_seconds=604_800)
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
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            try:
                recovered = await self.interaction_store.get_interaction(
                    workspace_root=self.workspace_root,
                    interaction_id=interaction_id,
                )
            except (HarnessStoreError, OSError, TypeError, ValueError):
                recovered = None
            if recovered is not None and recovered.state == "answered" and (
                _interaction_matches(recovered, completion, stable_stage)
            ):
                return recovered
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_unavailable",
                "当前界面无法创建持久 Percentage Stage Advance 交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_interaction_invalid",
                "Percentage Stage Advance 交互未通过运行时协议校验。",
            ) from exc
        interaction = await self.interaction_store.get_interaction(
            workspace_root=self.workspace_root,
            interaction_id=interaction_id,
        )
        if interaction is None or interaction.state != "answered":
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_answer_not_committed",
                "用户答案尚未提交到 Harness authority。",
            )
        if not _interaction_matches(interaction, completion, stable_stage):
            raise EvolutionRevalidationPercentageStageAdvanceError(
                "percentage_stage_advance_answer_mismatch",
                "用户答案未绑定 exact Percentage Stage Completion。",
            )
        return interaction

    async def _await_concurrent_answer(self, pending, *, completion, stable_stage):
        deadline = asyncio.get_running_loop().time() + _CONCURRENT_ANSWER_WAIT_SECONDS
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
            current,
            completion,
            stable_stage,
        ):
            return current
        raise EvolutionRevalidationPercentageStageAdvanceError(
            "percentage_stage_advance_interaction_pending",
            f"推进交互 {pending.interaction_id} 仍待回答。",
        )

    async def _view(self, receipt):
        reasons: list[str] = []
        try:
            receipt_source_current = (
                await self.store.get_by_completion_evidence(
                    receipt.stage_completion_evidence_id
                )
                == receipt
            )
        except EvolutionRevalidationPercentageStageAdvanceError:
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
                and completion_view.percentage_stage_completion_authority
            )
        except EvolutionRevalidationPercentageStageCompletionError:
            stage_completion_current = False
        if not stage_completion_current:
            reasons.append("stage_completion_changed")
        try:
            plan_view = await self.plan_service.inspect(plan_id=receipt.plan_id)
            plan_source_current = bool(
                plan_view.current_rollout_eligible
                and plan_view.plan.plan_sha256 == receipt.plan_sha256
                and _receipt_matches_stage(receipt, plan_view.plan)
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
        return EvolutionRevalidationPercentageStageAdvanceView(
            receipt=receipt,
            receipt_source_current=receipt_source_current,
            stage_completion_current=stage_completion_current,
            plan_source_current=plan_source_current,
            control_current=control_current,
            interaction_current=interaction_current,
            expired=expired,
            invalidation_reasons=tuple(sorted(set(reasons))),
            next_stage_entry_authority=current,
            stable_stage_entry_authority=current,
        )

    async def _interaction_current(self, receipt) -> bool:
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


def _interaction_request(completion, stable_stage, *, timeout_seconds):
    metrics = completion.metrics
    return normalize_interaction_request(
        {
            "header": "自进化 Stable 阶段推进",
            "question": (
                f"Percentage 已观察 {metrics.observed_runs} 次真实运行，成功 "
                f"{metrics.successful_runs} 次，错误率 "
                f"{metrics.error_rate_basis_points / 100:.2f}%。是否授权候选进入 "
                f"stable 阶段（目标 {stable_stage.exposure_percent}%）？本选择只签发短期 "
                "Stage Entry authority，不会部署、扩大流量、发布或推送。"
            ),
            "options": [
                {
                    "value": "advance",
                    "label": "推进到 stable",
                    "description": (
                        "签发受 current evidence/control fencing 的短期稳定阶段入口权限。"
                    ),
                },
                {
                    "value": "decline",
                    "label": "停止推进",
                    "description": "持久记录拒绝决定，不产生 stable 阶段入口权限。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义推进决定",
            "timeout_seconds": timeout_seconds,
            "priority": "critical",
        }
    )


def _interaction_matches(interaction, completion, stable_stage) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and match.group(1)
        == completion.evidence_id.removeprefix("evrepercentcomplete_")
        and interaction.subject_kind == "tool"
        and interaction.subject_id == completion.evidence_id
        and _static_request_payload(interaction.request())
        == _static_request_payload(
            _interaction_request(completion, stable_stage, timeout_seconds=None)
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
        raise EvolutionRevalidationPercentageStageAdvanceError(
            "percentage_stage_advance_attempts_exhausted",
            "Percentage Stage Advance 交互次数已达上限。",
        )
    suffix = completion.evidence_id.removeprefix("evrepercentcomplete_")
    return f"ask-evrepercentadvance-{suffix}-{attempt}"


def _build_receipt(
    *,
    workspace_root,
    completion,
    plan,
    control,
    decision,
    interaction,
    validity_seconds,
):
    issued = _aware(interaction.answered_at)
    control_sequence, control_sha, _state = _control_projection(control)
    stable_stage = plan.stages[3]
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PERCENTAGE_STAGE_ADVANCE_POLICY,
        "workspace_root": str(workspace_root),
        "subject_id": completion.subject_id,
        "stage_completion_evidence_id": completion.evidence_id,
        "stage_completion_evidence_sha256": completion.evidence_sha256,
        "stage_completion_source_set_sha256": completion.source_set_sha256,
        "assignment_id": completion.assignment_id,
        "plan_id": completion.plan_id,
        "plan_sha256": completion.plan_sha256,
        "completed_stage": "percentage",
        "next_stage": "stable",
        "stable_exposure_percent": stable_stage.exposure_percent,
        "manual_advance_required": True,
        "automatic_advance_eligible": False,
        "manual_interaction_required": True,
        "control_sequence": control_sequence,
        "control_event_sha256": control_sha,
        "decision": decision,
        "decision_source": "manual",
        "interaction": interaction,
        "interaction_request_sha256": _digest(
            _static_request_payload(interaction.request())
        ),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=validity_seconds)).isoformat(),
        "stage_completion_current": True,
        "control_active": True,
        "advance_authorized": decision == "advance",
        "next_stage_entry_authority": decision == "advance",
        "stable_stage_entry_authority": decision == "advance",
        "stable_rollout_authority": False,
        "deployment_authority": False,
        "rollback_authority": False,
        "promotion_authority": False,
        "git_write_executed": False,
        "publish_executed": False,
        "llm_generated": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationPercentageStageAdvanceReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrepercentadvance_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _live_sources_match(item, completion_view, plan, control) -> bool:
    return bool(
        completion_view.percentage_stage_completion_authority
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
        and item.assignment_id == completion.assignment_id
        and item.plan_id == completion.plan_id == plan.plan_id
        and item.plan_sha256 == completion.plan_sha256 == plan.plan_sha256
        and _receipt_matches_stage(item, plan)
    )


def _receipt_matches_stage(item, plan) -> bool:
    completed = plan.stages[2]
    stable = plan.stages[3]
    return bool(
        completed.name is EvolutionRevalidationRolloutStageName.PERCENTAGE
        and stable.name is EvolutionRevalidationRolloutStageName.STABLE
        and stable.manual_advance_required
        and item.completed_stage == completed.name.value
        and item.next_stage == stable.name.value
        and item.stable_exposure_percent == stable.exposure_percent == 100
    )


def _same_decision_source(left, right) -> bool:
    return bool(
        left.stage_completion_evidence_id == right.stage_completion_evidence_id
        and left.stage_completion_evidence_sha256
        == right.stage_completion_evidence_sha256
        and left.decision == right.decision
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
        raise ValueError("Percentage Stage Advance timestamp 必须包含 offset。")
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


def _evidence_id(value: str) -> str:
    if not isinstance(value, str) or _EVIDENCE_RE.fullmatch(value) is None:
        raise ValueError("evidence_id 必须是稳定的 Percentage Completion 标识。")
    return value


def _subject(value: str) -> str:
    if not isinstance(value, str) or _SUBJECT_RE.fullmatch(value) is None:
        raise ValueError("subject_id 必须是稳定的 runtime 标识。")
    return value


def _validated(value):
    try:
        return EvolutionRevalidationPercentageStageAdvanceReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageStageAdvanceError(
            "percentage_stage_advance_artifact_invalid",
            "Percentage Stage Advance Receipt 无效。",
        ) from exc


def _restore(encoded: str):
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Percentage Stage Advance Receipt 超过 512 KiB。")
    return EvolutionRevalidationPercentageStageAdvanceReceipt.model_validate_json(encoded)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_percentage_stage_advances ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "stage_completion_evidence_id TEXT NOT NULL UNIQUE, "
        "stage_completion_evidence_sha256 TEXT NOT NULL, decision TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, issued_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PERCENTAGE_STAGE_ADVANCE_POLICY",
    "EvolutionRevalidationPercentageStageAdvanceError",
    "EvolutionRevalidationPercentageStageAdvanceReceipt",
    "EvolutionRevalidationPercentageStageAdvanceService",
    "EvolutionRevalidationPercentageStageAdvanceStore",
    "EvolutionRevalidationPercentageStageAdvanceView",
]
