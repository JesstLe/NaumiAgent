"""Human-governed post-observation decisions over stable promotion eligibility."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_outcome_eligibilities import (
    EvolutionStablePromotionOutcomeEligibility,
    EvolutionStablePromotionOutcomeEligibilityError,
    EvolutionStablePromotionOutcomeEligibilityService,
    EvolutionStablePromotionOutcomeEligibilityStore,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_STABLE_PROMOTION_OUTCOME_DECISION_POLICY = (
    "evolution-stable-promotion-outcome-decision-v1"
)
_DECISION_RE = re.compile(r"^evstablepromdecision_[0-9a-f]{24}$")
_INTERACTION_RE = re.compile(r"^ask-evstablepromdecision-([0-9a-f]{24})-([1-9][0-9]{0,3})$")
_MAX_ARTIFACT_BYTES = 1024 * 1024
_DEFER_DAYS = 7

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EvolutionStablePromotionOutcomeDecisionAction(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    DEFER = "defer"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionOutcomeDecision(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-promotion-outcome-decision-v1"] = (
        EVOLUTION_STABLE_PROMOTION_OUTCOME_DECISION_POLICY
    )
    decision_id: str = Field(pattern=r"^evstablepromdecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    eligibility_id: str = Field(pattern=r"^evstablepromeligible_[0-9a-f]{24}$")
    eligibility_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    population_assessment_id: str = Field(pattern=r"^evstableprompopobserve_[0-9a-f]{24}$")
    workbench_session_id: str = Field(min_length=1, max_length=128)
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1, le=10_000)
    previous_decision_id: str = Field(pattern=r"^(?:|evstablepromdecision_[0-9a-f]{24})$")
    previous_decision_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    action: EvolutionStablePromotionOutcomeDecisionAction
    interaction: HarnessInteractionRecord
    interaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=128)
    actor_session_id: str = Field(min_length=1, max_length=128)
    decided_at: str = Field(min_length=1, max_length=100)
    defer_until: str = Field(max_length=100)
    local_user_choice_verified: Literal[True] = True
    independent_post_observation_decision: Literal[True] = True
    outcome_decision_authority: Literal[True] = True
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Promotion Outcome Decision workspace 必须 canonical。")
        if self.interaction.state != "answered" or self.interaction.answer_kind != "option":
            raise ValueError("Promotion Outcome Decision 必须绑定已回答选项。")
        if not (
            self.interaction.subject_kind == "tool"
            and self.interaction.subject_id == self.eligibility_id
            and self.interaction.answer_value == self.action.value
            and self.interaction.answered_by == self.actor
            and self.interaction.session_id == self.actor_session_id
            and self.interaction.answered_at == self.decided_at
            and self.interaction.digest() == self.interaction_sha256
        ):
            raise ValueError("Promotion Outcome Decision 用户交互绑定不一致。")
        if self.revision == 1:
            if self.previous_decision_id or self.previous_decision_sha256:
                raise ValueError("首个 Outcome Decision 不能携带前序。")
        elif not self.previous_decision_id or not self.previous_decision_sha256:
            raise ValueError("后续 Outcome Decision 必须链接前序。")
        decided = _aware(self.decided_at)
        if self.action is EvolutionStablePromotionOutcomeDecisionAction.DEFER:
            if _aware(self.defer_until) != decided + timedelta(days=_DEFER_DAYS):
                raise ValueError("defer 必须使用精确 7 天治理冷却。")
        elif self.defer_until:
            raise ValueError("非 defer Decision 不得携带 defer_until。")
        expected_source = _source(
            eligibility_id=self.eligibility_id,
            eligibility_sha256=self.eligibility_sha256,
            revision=self.revision,
            previous_decision_sha256=self.previous_decision_sha256,
            interaction_sha256=self.interaction_sha256,
        )
        if not hmac.compare_digest(self.source_sha256, expected_source):
            raise ValueError("Promotion Outcome Decision source identity 不一致。")
        digest = _digest(self.model_dump(mode="json", exclude={"decision_id", "decision_sha256"}))
        if not (
            hmac.compare_digest(self.decision_sha256, digest)
            and self.decision_id == f"evstablepromdecision_{digest[:24]}"
        ):
            raise ValueError("Promotion Outcome Decision content identity 不一致。")
        return self


class EvolutionStablePromotionOutcomeDecisionView(_StrictModel):
    decision: EvolutionStablePromotionOutcomeDecision
    current_decision: EvolutionStablePromotionOutcomeDecision | None
    durable_decision_valid: bool
    latest_decision_current: bool
    eligibility_authority: bool
    interaction_authority: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    outcome_decision_authority: bool
    promoted_outcome_ready_authority: bool
    rejected_outcome_authority: bool
    deferred_outcome_authority: bool
    revision_allowed: bool
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        authority = bool(
            self.durable_decision_valid
            and self.latest_decision_current
            and self.eligibility_authority
            and self.interaction_authority
            and self.current_decision is not None
        )
        action = self.decision.action
        if not (
            (self.current_decision == self.decision if authority else self.current_decision is None)
            and self.outcome_decision_authority is authority
            and self.promoted_outcome_ready_authority
            is (authority and action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE)
            and self.rejected_outcome_authority
            is (authority and action is EvolutionStablePromotionOutcomeDecisionAction.REJECT)
            and self.deferred_outcome_authority
            is (authority and action is EvolutionStablePromotionOutcomeDecisionAction.DEFER)
            and (not self.revision_allowed or self.deferred_outcome_authority)
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Promotion Outcome Decision authority projection 不一致。")
        return self


class EvolutionStablePromotionOutcomeDecisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionOutcomeDecisionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        eligibility_store: EvolutionStablePromotionOutcomeEligibilityStore,
        interaction_store: HarnessStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != eligibility_store.db_path:
            raise ValueError("Promotion Outcome Decision 与 Eligibility 必须共享 session SQLite。")
        self.eligibility_store = eligibility_store
        self.interaction_store = interaction_store

    async def get(self, decision_id: str) -> EvolutionStablePromotionOutcomeDecision | None:
        identifier = _decision_id(decision_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_decisions "
                        "WHERE decision_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_decision(row)
        except EvolutionStablePromotionOutcomeDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_store_corrupt",
                "Promotion Outcome Decision 损坏或无法读取。",
            ) from exc

    async def latest(
        self, *, eligibility_id: str
    ) -> EvolutionStablePromotionOutcomeDecision | None:
        identifier = str(eligibility_id or "").strip()
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_decisions "
                        "WHERE eligibility_id = ? ORDER BY revision DESC LIMIT 1",
                        (identifier,),
                    )
                ).fetchone()
            return None if row is None else _row_decision(row)
        except EvolutionStablePromotionOutcomeDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_store_corrupt",
                "Promotion Outcome Decision 损坏或无法读取。",
            ) from exc

    async def record(
        self, decision: EvolutionStablePromotionOutcomeDecision
    ) -> EvolutionStablePromotionOutcomeDecision:
        item = EvolutionStablePromotionOutcomeDecision.model_validate(decision)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_oversized",
                "Promotion Outcome Decision 超过 1 MiB。",
            )
        try:
            interaction = await self.interaction_store.get_interaction(
                workspace_root=item.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_interaction_read_failed",
                "无法重读 Harness interaction authority。",
            ) from exc
        if interaction != item.interaction or interaction.digest() != item.interaction_sha256:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_source_changed",
                "Harness interaction durable source 已变化。",
            )
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_exact_sources(db, item)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_decisions "
                        "WHERE source_sha256 = ?",
                        (item.source_sha256,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _row_decision(row)
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionOutcomeDecisionError(
                        "stable_promotion_outcome_decision_source_conflict",
                        "同一 Outcome Decision source 已绑定不同内容。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_outcome_decisions "
                    "(decision_id, decision_sha256, source_sha256, eligibility_id, "
                    "revision, action, interaction_id, decision_json, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.decision_id,
                        item.decision_sha256,
                        item.source_sha256,
                        item.eligibility_id,
                        item.revision,
                        item.action.value,
                        item.interaction.interaction_id,
                        encoded,
                        item.decided_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionOutcomeDecisionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_store_failed",
                "Promotion Outcome Decision 无法持久化。",
            ) from exc


class EvolutionStablePromotionOutcomeDecisionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        eligibility_service: EvolutionStablePromotionOutcomeEligibilityService,
        interaction_store: HarnessStore,
        store: EvolutionStablePromotionOutcomeDecisionStore,
        request_user_input: RequestUserInputCallback,
        clock: Callable[[], datetime] | None = None,
        interaction_timeout_seconds: int = 3_600,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            store.eligibility_store is eligibility_service.store
            and store.interaction_store is interaction_store
        ):
            raise ValueError("Promotion Outcome Decision Service dependency 不一致。")
        if not callable(request_user_input):
            raise TypeError("Promotion Outcome Decision Service 需要用户交互 callback。")
        if not 3 <= interaction_timeout_seconds <= 86_400:
            raise ValueError("Outcome Decision interaction timeout 必须在 3..86400 秒。")
        self.workspace_root = root
        self.eligibility_service = eligibility_service
        self.interaction_store = interaction_store
        self.store = store
        self.request_user_input = request_user_input
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interaction_timeout_seconds = interaction_timeout_seconds
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def decide(self, *, eligibility_id: str) -> EvolutionStablePromotionOutcomeDecisionView:
        identifier = str(eligibility_id or "").strip()
        lock = self._locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            eligibility = await self._current_eligibility(identifier)
            previous = await self.store.latest(eligibility_id=identifier)
            if previous is not None:
                view = await self.inspect(decision_id=previous.decision_id)
                if previous.action is not EvolutionStablePromotionOutcomeDecisionAction.DEFER:
                    return view
                if self._now() < _aware(previous.defer_until):
                    return view
            revision = 1 if previous is None else previous.revision + 1
            request = _decision_request(
                eligibility, revision, timeout_seconds=self.interaction_timeout_seconds
            )
            history = await self._interaction_history(eligibility, revision, request)
            answered = tuple(item for item in history if item.state == "answered")
            if len(answered) > 1:
                raise EvolutionStablePromotionOutcomeDecisionError(
                    "stable_promotion_outcome_decision_answer_ambiguous",
                    "同一 Outcome Decision revision 存在多个已回答交互。",
                )
            if answered:
                interaction = answered[0]
            else:
                pending = next((item for item in history if item.state == "pending"), None)
                if pending is not None:
                    raise EvolutionStablePromotionOutcomeDecisionError(
                        "stable_promotion_outcome_decision_interaction_pending",
                        f"Outcome Decision 交互 {pending.interaction_id} 仍待回答。",
                    )
                interaction_id = _interaction_id(eligibility.eligibility_id, revision)
                payload = {
                    **request.to_public_dict(),
                    "_interaction_id": interaction_id,
                    "_durable_subject_kind": "tool",
                    "_durable_subject_id": eligibility.eligibility_id,
                }
                try:
                    await self.request_user_input(payload)
                except (HarnessStoreError, UserInteractionUnavailableError) as exc:
                    raise EvolutionStablePromotionOutcomeDecisionError(
                        "stable_promotion_outcome_decision_interaction_unavailable",
                        "当前界面无法创建持久 Outcome Decision 交互。",
                    ) from exc
                except ValueError as exc:
                    raise EvolutionStablePromotionOutcomeDecisionError(
                        "stable_promotion_outcome_decision_interaction_invalid",
                        "Outcome Decision 交互未通过运行时协议校验。",
                    ) from exc
                interaction = await self.interaction_store.get_interaction(
                    workspace_root=self.workspace_root,
                    interaction_id=interaction_id,
                )
                if interaction is None or interaction.state != "answered":
                    raise EvolutionStablePromotionOutcomeDecisionError(
                        "stable_promotion_outcome_decision_answer_not_committed",
                        "用户答案尚未提交到 Harness authority。",
                    )
            refreshed = await self._current_eligibility(identifier)
            latest = await self.store.latest(eligibility_id=identifier)
            if refreshed != eligibility or latest != previous:
                raise EvolutionStablePromotionOutcomeDecisionError(
                    "stable_promotion_outcome_decision_authority_changed",
                    "Outcome authority 在用户回答期间已变化，请重新发起决策。",
                )
            decision = build_stable_promotion_outcome_decision(
                eligibility=eligibility,
                interaction=interaction,
                revision=revision,
                previous=previous,
            )
            stored = await self.store.record(decision)
        return await self.inspect(decision_id=stored.decision_id)

    async def inspect(self, *, decision_id: str) -> EvolutionStablePromotionOutcomeDecisionView:
        decision = await self.store.get(decision_id)
        if decision is None:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_missing",
                "Promotion Outcome Decision 不存在。",
            )
        durable = await self.store.get(decision.decision_id) == decision
        latest = await self.store.latest(eligibility_id=decision.eligibility_id) == decision
        eligibility_current = interaction_current = False
        reasons: list[str] = []
        try:
            eligibility = await self._current_eligibility(decision.eligibility_id)
            eligibility_current = bool(
                eligibility.eligibility_sha256 == decision.eligibility_sha256
            )
        except (EvolutionStablePromotionOutcomeEligibilityError, OSError, RuntimeError, ValueError):
            reasons.append("eligibility_authority_unavailable")
        try:
            interaction = await self.interaction_store.get_interaction(
                workspace_root=self.workspace_root,
                interaction_id=decision.interaction.interaction_id,
            )
            interaction_current = bool(
                interaction is not None
                and interaction.digest() == decision.interaction_sha256
                and interaction == decision.interaction
            )
        except (HarnessStoreError, OSError, RuntimeError, ValueError):
            reasons.append("interaction_authority_unavailable")
        checks = {
            "decision_source_changed": durable,
            "newer_decision_exists": latest,
            "eligibility_stale": eligibility_current,
            "interaction_stale": interaction_current,
        }
        reasons.extend(reason for reason, passed in checks.items() if not passed)
        authority = bool(all(checks.values()))
        current = decision if authority else None
        revision_allowed = bool(
            authority
            and decision.action is EvolutionStablePromotionOutcomeDecisionAction.DEFER
            and self._now() >= _aware(decision.defer_until)
        )
        return EvolutionStablePromotionOutcomeDecisionView(
            decision=decision,
            current_decision=current,
            durable_decision_valid=durable,
            latest_decision_current=latest,
            eligibility_authority=eligibility_current,
            interaction_authority=interaction_current,
            invalidation_reasons=tuple(sorted(set(reasons))),
            outcome_decision_authority=authority,
            promoted_outcome_ready_authority=bool(
                authority
                and decision.action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE
            ),
            rejected_outcome_authority=bool(
                authority
                and decision.action is EvolutionStablePromotionOutcomeDecisionAction.REJECT
            ),
            deferred_outcome_authority=bool(
                authority and decision.action is EvolutionStablePromotionOutcomeDecisionAction.DEFER
            ),
            revision_allowed=revision_allowed,
        )

    async def _current_eligibility(
        self, eligibility_id: str
    ) -> EvolutionStablePromotionOutcomeEligibility:
        view = await self.eligibility_service.inspect(eligibility_id=eligibility_id)
        if not view.outcome_review_ready_authority or view.current_eligibility is None:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_eligibility_stale",
                "只有 current Outcome Eligibility 才能发起独立决策。",
            )
        return view.current_eligibility

    async def _interaction_history(
        self,
        eligibility: EvolutionStablePromotionOutcomeEligibility,
        revision: int,
        request: UserInteractionRequest,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            records = await self.interaction_store.list_interactions(
                workspace_root=self.workspace_root,
                subject_kind="tool",
                subject_ids=(eligibility.eligibility_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_interaction_history_failed",
                "无法读取 Outcome Decision 持久交互历史。",
            ) from exc
        return tuple(
            item for item in records if _interaction_matches(item, eligibility, revision, request)
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.utcoffset() is None:
            raise EvolutionStablePromotionOutcomeDecisionError(
                "stable_promotion_outcome_decision_clock_invalid",
                "Outcome Decision clock 必须包含时区。",
            )
        return value.astimezone(UTC)


def build_stable_promotion_outcome_decision(
    *,
    eligibility: EvolutionStablePromotionOutcomeEligibility,
    interaction: HarnessInteractionRecord,
    revision: int,
    previous: EvolutionStablePromotionOutcomeDecision | None,
) -> EvolutionStablePromotionOutcomeDecision:
    item = EvolutionStablePromotionOutcomeEligibility.model_validate(eligibility)
    answer = HarnessInteractionRecord.model_validate_json(interaction.model_dump_json())
    request = _decision_request(item, revision, timeout_seconds=None)
    if not _interaction_matches(answer, item, revision, request):
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_interaction_mismatch",
            "已回答 interaction 未绑定 exact Outcome Eligibility/revision。",
        )
    if answer.state != "answered" or answer.answer_kind != "option" or not answer.session_id:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_answer_invalid",
            "Outcome Decision 必须来自已认证的本地用户选项。",
        )
    try:
        action = EvolutionStablePromotionOutcomeDecisionAction(answer.answer_value)
    except ValueError as exc:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_action_invalid",
            "Outcome Decision action 无效。",
        ) from exc
    if revision == 1 and previous is not None:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_previous_invalid",
            "首个 Outcome Decision 不能绑定前序。",
        )
    if revision > 1 and not (
        previous is not None
        and previous.eligibility_id == item.eligibility_id
        and previous.revision == revision - 1
        and previous.action is EvolutionStablePromotionOutcomeDecisionAction.DEFER
        and _aware(answer.answered_at) >= _aware(previous.defer_until)
    ):
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_previous_invalid",
            "只有已到期 defer Decision 才能形成下一 revision。",
        )
    interaction_sha = answer.digest()
    previous_id = "" if previous is None else previous.decision_id
    previous_sha = "" if previous is None else previous.decision_sha256
    source = _source(
        eligibility_id=item.eligibility_id,
        eligibility_sha256=item.eligibility_sha256,
        revision=revision,
        previous_decision_sha256=previous_sha,
        interaction_sha256=interaction_sha,
    )
    decided_at = answer.answered_at
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OUTCOME_DECISION_POLICY,
        "source_sha256": source,
        "workspace_root": item.workspace_root,
        "eligibility_id": item.eligibility_id,
        "eligibility_sha256": item.eligibility_sha256,
        "contract_id": item.contract_id,
        "population_assessment_id": item.population_assessment_id,
        "workbench_session_id": item.workbench_session_id,
        "workbench_proposal_id": item.workbench_proposal_id,
        "proposal_id": item.proposal_id,
        "candidate_id": item.candidate_id,
        "candidate_revision": item.candidate_revision,
        "candidate_sha256": item.candidate_sha256,
        "revision": revision,
        "previous_decision_id": previous_id,
        "previous_decision_sha256": previous_sha,
        "action": action.value,
        "interaction": answer,
        "interaction_sha256": interaction_sha,
        "actor": answer.answered_by,
        "actor_session_id": answer.session_id,
        "decided_at": decided_at,
        "defer_until": (
            (_aware(decided_at) + timedelta(days=_DEFER_DAYS)).isoformat()
            if action is EvolutionStablePromotionOutcomeDecisionAction.DEFER
            else ""
        ),
        "local_user_choice_verified": True,
        "independent_post_observation_decision": True,
        "outcome_decision_authority": True,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "llm_generated": False,
    }
    digest = _digest(
        {
            **core,
            "interaction": answer.model_dump(mode="json"),
        }
    )
    return EvolutionStablePromotionOutcomeDecision.model_validate(
        {
            **core,
            "decision_id": f"evstablepromdecision_{digest[:24]}",
            "decision_sha256": digest,
        }
    )


def render_stable_promotion_outcome_decision(
    view: EvolutionStablePromotionOutcomeDecisionView,
) -> str:
    item = view.current_decision or view.decision
    return "\n".join(
        (
            "## 稳定推广 Outcome 独立决策",
            "",
            f"- Decision：`{item.decision_id}` revision `{item.revision}`",
            f"- Eligibility：`{item.eligibility_id}`",
            f"- 用户选择：`{item.action.value}`",
            f"- 决策主体：`{item.actor}` / session `{item.actor_session_id}`",
            f"- 当前 Decision authority：`{str(view.outcome_decision_authority).lower()}`",
            f"- 7b4c 可消费 promote：`{str(view.promoted_outcome_ready_authority).lower()}`",
            f"- 可形成下一 revision：`{str(view.revision_allowed).lower()}`",
            f"- defer_until：`{item.defer_until or 'none'}`",
            f"- 撤权原因：`{', '.join(view.invalidation_reasons) or 'none'}`",
            "- Promoted Outcome / Learning / Promotion / Execution authority：`false`",
        )
    )


def _decision_request(
    eligibility: EvolutionStablePromotionOutcomeEligibility,
    revision: int,
    *,
    timeout_seconds: int | None,
) -> UserInteractionRequest:
    return normalize_interaction_request(
        {
            "header": "稳定推广 Outcome 决策",
            "question": (
                f"请审查 Eligibility {eligibility.eligibility_id} revision {revision}："
                f"Candidate {eligibility.candidate_id} revision {eligibility.candidate_revision}，"
                "Population "
                f"{eligibility.passing_count}/{eligibility.population_denominator} passing。"
                "本选择只形成 post-observation Decision，不会发布、改代码或写 promoted Outcome。"
            ),
            "options": [
                {
                    "value": "promote",
                    "label": "确认观察结果",
                    "description": "允许 7b4c 写 promoted Outcome；本步不执行推广。",
                },
                {
                    "value": "reject",
                    "label": "拒绝观察结果",
                    "description": "记录终态拒绝；不执行回滚或代码变更。",
                },
                {
                    "value": "defer",
                    "label": "延后 7 天复核",
                    "description": "记录 7 天冷却；到期后可形成下一版决策。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义治理动作",
            "timeout_seconds": timeout_seconds,
            "priority": "critical",
        }
    )


def _interaction_matches(
    interaction: HarnessInteractionRecord,
    eligibility: EvolutionStablePromotionOutcomeEligibility,
    revision: int,
    request: UserInteractionRequest,
) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and interaction.subject_kind == "tool"
        and interaction.subject_id == eligibility.eligibility_id
        and match.group(1) == eligibility.eligibility_id.removeprefix("evstablepromeligible_")
        and int(match.group(2)) == revision
        and _static_request(interaction.request()) == _static_request(request)
        and interaction.request().timeout_seconds is not None
        and interaction.request().timeout_seconds <= 86_400
    )


async def _require_exact_sources(
    db: aiosqlite.Connection,
    item: EvolutionStablePromotionOutcomeDecision,
) -> None:
    eligibility_row = await (
        await db.execute(
            "SELECT eligibility_json FROM evolution_stable_promotion_outcome_eligibilities "
            "WHERE eligibility_id = ?",
            (item.eligibility_id,),
        )
    ).fetchone()
    previous_row = await (
        await db.execute(
            "SELECT decision_json FROM evolution_stable_promotion_outcome_decisions "
            "WHERE eligibility_id = ? AND revision = ?",
            (item.eligibility_id, item.revision - 1),
        )
    ).fetchone()
    if eligibility_row is None:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_source_changed",
            "Eligibility durable source 已变化。",
        )
    eligibility = EvolutionStablePromotionOutcomeEligibility.model_validate_json(
        eligibility_row["eligibility_json"]
    )
    previous = (
        None
        if previous_row is None
        else EvolutionStablePromotionOutcomeDecision.model_validate_json(
            previous_row["decision_json"]
        )
    )
    expected = build_stable_promotion_outcome_decision(
        eligibility=eligibility,
        interaction=item.interaction,
        revision=item.revision,
        previous=previous,
    )
    if expected != item:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_source_changed",
            "Outcome Decision 与 durable sources 不一致。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_outcome_decisions ("
        "decision_id TEXT PRIMARY KEY, decision_sha256 TEXT NOT NULL UNIQUE, "
        "source_sha256 TEXT NOT NULL UNIQUE, eligibility_id TEXT NOT NULL, "
        "revision INTEGER NOT NULL, action TEXT NOT NULL, interaction_id TEXT NOT NULL UNIQUE, "
        "decision_json TEXT NOT NULL, decided_at TEXT NOT NULL, "
        "UNIQUE(eligibility_id, revision))"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_outcome_decisions_latest "
        "ON evolution_stable_promotion_outcome_decisions(eligibility_id, revision DESC)"
    )


def _row_decision(row: aiosqlite.Row) -> EvolutionStablePromotionOutcomeDecision:
    item = EvolutionStablePromotionOutcomeDecision.model_validate_json(row["decision_json"])
    indexed = (
        row["decision_id"],
        row["decision_sha256"],
        row["source_sha256"],
        row["eligibility_id"],
        row["revision"],
        row["action"],
        row["interaction_id"],
        row["decided_at"],
    )
    expected = (
        item.decision_id,
        item.decision_sha256,
        item.source_sha256,
        item.eligibility_id,
        item.revision,
        item.action.value,
        item.interaction.interaction_id,
        item.decided_at,
    )
    if indexed != expected:
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_store_corrupt",
            "Promotion Outcome Decision 索引与 durable JSON 不一致。",
        )
    return item


def _source(**payload: Any) -> str:
    return _digest(
        {"policy_version": EVOLUTION_STABLE_PROMOTION_OUTCOME_DECISION_POLICY, **payload}
    )


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _static_request(request: UserInteractionRequest) -> dict[str, Any]:
    value = request.to_public_dict()
    value.pop("timeout_seconds", None)
    return value


def _interaction_id(eligibility_id: str, revision: int) -> str:
    suffix = eligibility_id.removeprefix("evstablepromeligible_")
    return f"ask-evstablepromdecision-{suffix}-{revision}"


def _decision_id(value: str) -> str:
    identifier = str(value or "").strip()
    if not _DECISION_RE.fullmatch(identifier):
        raise EvolutionStablePromotionOutcomeDecisionError(
            "stable_promotion_outcome_decision_id_invalid",
            "Promotion Outcome Decision ID 无效。",
        )
    return identifier


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OUTCOME_DECISION_POLICY",
    "EvolutionStablePromotionOutcomeDecision",
    "EvolutionStablePromotionOutcomeDecisionAction",
    "EvolutionStablePromotionOutcomeDecisionError",
    "EvolutionStablePromotionOutcomeDecisionService",
    "EvolutionStablePromotionOutcomeDecisionStore",
    "EvolutionStablePromotionOutcomeDecisionView",
    "build_stable_promotion_outcome_decision",
    "render_stable_promotion_outcome_decision",
]
