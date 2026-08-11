"""Append-only promoted Outcome and supersession ledger for stable promotion."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_outcome_decisions import (
    EvolutionStablePromotionOutcomeDecision,
    EvolutionStablePromotionOutcomeDecisionAction,
    EvolutionStablePromotionOutcomeDecisionError,
    EvolutionStablePromotionOutcomeDecisionService,
    EvolutionStablePromotionOutcomeDecisionStore,
)
from naumi_agent.evolution.stable_promotion_outcome_eligibilities import (
    EvolutionStablePromotionOutcomeEligibility,
    EvolutionStablePromotionOutcomeEligibilityError,
    EvolutionStablePromotionOutcomeEligibilityService,
    EvolutionStablePromotionOutcomeEligibilityStore,
)

EVOLUTION_STABLE_PROMOTION_OUTCOME_POLICY = "evolution-stable-promotion-outcome-v1"
EVOLUTION_STABLE_PROMOTION_OUTCOME_SUPERSEDE_POLICY = (
    "evolution-stable-promotion-outcome-supersede-v1"
)
_OUTCOME_RE = re.compile(r"^evstablepromout_[0-9a-f]{24}$")
_SAFE_BINDING_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_CHAIN_LENGTH = 10_000
_MAX_SESSION_PROJECTIONS = 100


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-promotion-outcome-v1"] = (
        EVOLUTION_STABLE_PROMOTION_OUTCOME_POLICY
    )
    outcome_id: str = Field(pattern=r"^evstablepromout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    status: Literal["promoted"] = "promoted"
    sequence: int = Field(ge=1, le=_MAX_CHAIN_LENGTH)
    previous_outcome_id: str = Field(pattern=r"^(?:|evstablepromout_[0-9a-f]{24})$")
    previous_outcome_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    decision_id: str = Field(pattern=r"^evstablepromdecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligibility_id: str = Field(pattern=r"^evstablepromeligible_[0-9a-f]{24}$")
    eligibility_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    population_assessment_id: str = Field(pattern=r"^evstableprompopobserve_[0-9a-f]{24}$")
    population_denominator: int = Field(ge=1, le=10_000)
    passing_count: int = Field(ge=1, le=10_000)
    assessment_coverage_bps: Literal[10000] = 10_000
    duration_coverage_bps: Literal[10000] = 10_000
    workbench_session_id: str = Field(min_length=1, max_length=128)
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(min_length=1, max_length=255)
    promoted_at: str = Field(min_length=1, max_length=100)
    post_observation_decision_verified: Literal[True] = True
    population_sustained_health_verified: Literal[True] = True
    promoted: Literal[True] = True
    superseded: Literal[False] = False
    long_term_metrics_recorded: Literal[True] = True
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Promotion Outcome workspace 必须 canonical。")
        first = self.sequence == 1
        empty_previous = not self.previous_outcome_id and not self.previous_outcome_sha256
        complete_previous = bool(self.previous_outcome_id and self.previous_outcome_sha256)
        if not ((first and empty_previous) or (not first and complete_previous)):
            raise ValueError("Stable Promotion Outcome previous chain 无效。")
        if self.passing_count != self.population_denominator:
            raise ValueError("Stable Promotion Outcome 必须覆盖 exact Population。")
        _aware(self.promoted_at)
        digest = _digest(self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"}))
        if not (
            hmac.compare_digest(self.outcome_sha256, digest)
            and self.outcome_id == f"evstablepromout_{digest[:24]}"
        ):
            raise ValueError("Stable Promotion Outcome content identity 不一致。")
        return self


class EvolutionStablePromotionOutcomeSupersedeEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-promotion-outcome-supersede-v1"] = (
        EVOLUTION_STABLE_PROMOTION_OUTCOME_SUPERSEDE_POLICY
    )
    event_id: str = Field(pattern=r"^evstablepromoutsup_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    workbench_session_id: str = Field(min_length=1, max_length=128)
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1, le=_MAX_CHAIN_LENGTH)
    previous_event_id: str = Field(pattern=r"^(?:|evstablepromoutsup_[0-9a-f]{24})$")
    previous_event_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    prior_outcome_id: str = Field(pattern=r"^(?:|evstablepromout_[0-9a-f]{24})$")
    prior_outcome_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    successor_outcome_id: str = Field(pattern=r"^evstablepromout_[0-9a-f]{24}$")
    successor_outcome_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(pattern=r"^evstablepromdecision_[0-9a-f]{24}$")
    transition: Literal["promoted"] = "promoted"
    reason: Literal["post_observation_promote_decision"] = "post_observation_promote_decision"
    prior_outcome_superseded: bool
    projection_head_changed: Literal[True] = True
    historical_outcome_deleted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Promotion Supersede Event workspace 必须 canonical。")
        first = self.sequence == 1
        empty_previous = bool(
            not self.previous_event_id
            and not self.previous_event_sha256
            and not self.prior_outcome_id
            and not self.prior_outcome_sha256
            and not self.prior_outcome_superseded
        )
        complete_previous = bool(
            self.previous_event_id
            and self.previous_event_sha256
            and self.prior_outcome_id
            and self.prior_outcome_sha256
            and self.prior_outcome_superseded
        )
        if not ((first and empty_previous) or (not first and complete_previous)):
            raise ValueError("Stable Promotion Supersede Event previous chain 无效。")
        _aware(self.recorded_at)
        digest = _digest(self.model_dump(mode="json", exclude={"event_id", "event_sha256"}))
        if not (
            hmac.compare_digest(self.event_sha256, digest)
            and self.event_id == f"evstablepromoutsup_{digest[:24]}"
        ):
            raise ValueError("Stable Promotion Supersede Event identity 不一致。")
        return self


class EvolutionStablePromotionOutcomeView(_StrictModel):
    outcome: EvolutionStablePromotionOutcome
    supersede_event: EvolutionStablePromotionOutcomeSupersedeEvent
    current_outcome: EvolutionStablePromotionOutcome | None
    durable_pair_valid: bool
    chain_valid: bool
    decision_authority: bool
    eligibility_authority: bool
    projection_head_authority: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    promoted_outcome_authority: bool
    promoted: Literal[True] = True
    superseded: bool
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        authority = bool(
            self.durable_pair_valid
            and self.chain_valid
            and self.decision_authority
            and self.eligibility_authority
            and self.projection_head_authority
            and self.current_outcome is not None
        )
        if not (
            (self.current_outcome == self.outcome if authority else self.current_outcome is None)
            and self.promoted_outcome_authority is authority
            and not (self.superseded and self.projection_head_authority)
            and not (self.superseded and self.promoted_outcome_authority)
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Promotion Outcome authority projection 不一致。")
        return self


class EvolutionStablePromotionOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionOutcomeStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        decision_store: EvolutionStablePromotionOutcomeDecisionStore,
        eligibility_store: EvolutionStablePromotionOutcomeEligibilityStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            self.db_path == decision_store.db_path == eligibility_store.db_path
            and decision_store.eligibility_store is eligibility_store
        ):
            raise ValueError("Stable Promotion Outcome sources 必须共享 session SQLite。")
        self.decision_store = decision_store
        self.eligibility_store = eligibility_store

    async def get_pair(
        self, outcome_id: str
    ) -> (
        tuple[
            EvolutionStablePromotionOutcome,
            EvolutionStablePromotionOutcomeSupersedeEvent,
        ]
        | None
    ):
        identifier = _outcome_id(outcome_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                outcome_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcomes WHERE outcome_id = ?",
                        (identifier,),
                    )
                ).fetchone()
                if outcome_row is None:
                    return None
                event_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_supersede_events "
                        "WHERE successor_outcome_id = ?",
                        (identifier,),
                    )
                ).fetchone()
            if event_row is None:
                raise ValueError("Stable Promotion Outcome event missing")
            return _row_outcome(outcome_row), _row_event(event_row)
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_corrupt",
                "Stable Promotion Outcome durable pair 损坏或无法读取。",
            ) from exc

    async def head(
        self, *, workbench_session_id: str, workbench_proposal_id: str
    ) -> EvolutionStablePromotionOutcome | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcomes "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence DESC LIMIT 1",
                        (workbench_session_id, workbench_proposal_id),
                    )
                ).fetchone()
            return None if row is None else _row_outcome(row)
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_corrupt",
                "Stable Promotion Outcome head 损坏或无法读取。",
            ) from exc

    async def latest_event(
        self, *, workbench_session_id: str, workbench_proposal_id: str
    ) -> EvolutionStablePromotionOutcomeSupersedeEvent | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_supersede_events "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence DESC LIMIT 1",
                        (workbench_session_id, workbench_proposal_id),
                    )
                ).fetchone()
            return None if row is None else _row_event(row)
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_corrupt",
                "Stable Promotion Supersede Event head 损坏或无法读取。",
            ) from exc

    async def list_heads_by_session(
        self,
        session_id: str,
        *,
        limit: int = _MAX_SESSION_PROJECTIONS,
    ) -> tuple[EvolutionStablePromotionOutcome, ...]:
        normalized = str(session_id or "").strip()
        if _SAFE_BINDING_RE.fullmatch(normalized) is None:
            raise ValueError("Workbench Session ID 格式无效。")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("Stable Promotion Outcome 查询上限必须为 1..100。")
        if not self.db_path.is_file():
            return ()
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                rows = await (
                    await db.execute(
                        "SELECT outcome.* FROM evolution_stable_promotion_outcomes outcome "
                        "JOIN (SELECT workbench_proposal_id, MAX(sequence) AS head_sequence "
                        "FROM evolution_stable_promotion_outcomes "
                        "WHERE workbench_session_id = ? GROUP BY workbench_proposal_id) head "
                        "ON head.workbench_proposal_id = outcome.workbench_proposal_id "
                        "AND head.head_sequence = outcome.sequence "
                        "WHERE outcome.workbench_session_id = ? "
                        "ORDER BY outcome.promoted_at DESC, outcome.outcome_id DESC LIMIT ?",
                        (normalized, normalized, limit + 1),
                    )
                ).fetchall()
            if len(rows) > limit:
                raise EvolutionStablePromotionOutcomeError(
                    "stable_promotion_outcome_projection_limit_exceeded",
                    "Stable Promotion Outcome 数量超过单次投影上限。",
                )
            return tuple(_row_outcome(row) for row in rows)
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_corrupt",
                "Stable Promotion Outcome projection head 损坏或无法读取。",
            ) from exc

    async def chain_valid(self, *, workbench_session_id: str, workbench_proposal_id: str) -> bool:
        if not self.db_path.is_file():
            return False
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                outcomes = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcomes "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence ASC LIMIT ?",
                        (workbench_session_id, workbench_proposal_id, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
                events = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_supersede_events "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence ASC LIMIT ?",
                        (workbench_session_id, workbench_proposal_id, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
            return _chain_matches(outcomes, events)
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_corrupt",
                "Stable Promotion Outcome chain 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        *,
        outcome: EvolutionStablePromotionOutcome,
        event: EvolutionStablePromotionOutcomeSupersedeEvent,
    ) -> tuple[
        EvolutionStablePromotionOutcome,
        EvolutionStablePromotionOutcomeSupersedeEvent,
    ]:
        item = EvolutionStablePromotionOutcome.model_validate(outcome)
        transition = EvolutionStablePromotionOutcomeSupersedeEvent.model_validate(event)
        if not _pair_matches(item, transition):
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_pair_invalid",
                "Stable Promotion Outcome 与 Supersede Event 不一致。",
            )
        outcome_json = item.model_dump_json()
        event_json = transition.model_dump_json()
        if max(len(outcome_json.encode()), len(event_json.encode())) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_oversized",
                "Stable Promotion Outcome 或 Event 超过 1 MiB。",
            )
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                outcomes = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcomes "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence ASC LIMIT ?",
                        (
                            item.workbench_session_id,
                            item.workbench_proposal_id,
                            _MAX_CHAIN_LENGTH + 1,
                        ),
                    )
                ).fetchall()
                events = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcome_supersede_events "
                        "WHERE workbench_session_id = ? AND workbench_proposal_id = ? "
                        "ORDER BY sequence ASC LIMIT ?",
                        (
                            item.workbench_session_id,
                            item.workbench_proposal_id,
                            _MAX_CHAIN_LENGTH + 1,
                        ),
                    )
                ).fetchall()
                if not _chain_matches(outcomes, events):
                    raise EvolutionStablePromotionOutcomeError(
                        "stable_promotion_outcome_chain_corrupt",
                        "既有 Stable Promotion Outcome chain 不连续。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_outcomes WHERE decision_id = ?",
                        (item.decision_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _row_outcome(existing)
                    event_row = await (
                        await db.execute(
                            "SELECT * FROM evolution_stable_promotion_outcome_supersede_events "
                            "WHERE successor_outcome_id = ?",
                            (restored.outcome_id,),
                        )
                    ).fetchone()
                    await db.rollback()
                    if event_row is not None and restored == item:
                        restored_event = _row_event(event_row)
                        if restored_event == transition:
                            return restored, restored_event
                    raise EvolutionStablePromotionOutcomeError(
                        "stable_promotion_outcome_decision_conflict",
                        "同一 promote Decision 已绑定不同 Outcome。",
                    )
                decision, eligibility = await _exact_sources(db, item)
                head = None if not outcomes else _row_outcome(outcomes[-1])
                previous_event = None if not events else _row_event(events[-1])
                expected_outcome, expected_event = build_stable_promotion_outcome_pair(
                    decision=decision,
                    eligibility=eligibility,
                    head=head,
                    previous_event=previous_event,
                )
                if expected_outcome != item or expected_event != transition:
                    raise EvolutionStablePromotionOutcomeError(
                        "stable_promotion_outcome_head_changed",
                        "Stable Promotion Outcome projection head 已变化。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_outcomes "
                    "(outcome_id, outcome_sha256, workbench_session_id, "
                    "workbench_proposal_id, sequence, decision_id, eligibility_id, "
                    "outcome_json, promoted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.outcome_id,
                        item.outcome_sha256,
                        item.workbench_session_id,
                        item.workbench_proposal_id,
                        item.sequence,
                        item.decision_id,
                        item.eligibility_id,
                        outcome_json,
                        item.promoted_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_outcome_supersede_events "
                    "(event_id, event_sha256, workbench_session_id, "
                    "workbench_proposal_id, sequence, previous_event_id, "
                    "prior_outcome_id, successor_outcome_id, event_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        transition.event_id,
                        transition.event_sha256,
                        transition.workbench_session_id,
                        transition.workbench_proposal_id,
                        transition.sequence,
                        transition.previous_event_id,
                        transition.prior_outcome_id,
                        transition.successor_outcome_id,
                        event_json,
                        transition.recorded_at,
                    ),
                )
                await db.commit()
            return item, transition
        except EvolutionStablePromotionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_store_failed",
                "Stable Promotion Outcome 与 Event 无法持久化。",
            ) from exc


class EvolutionStablePromotionOutcomeService:
    def __init__(
        self,
        *,
        decision_service: EvolutionStablePromotionOutcomeDecisionService,
        eligibility_service: EvolutionStablePromotionOutcomeEligibilityService,
        store: EvolutionStablePromotionOutcomeStore,
    ) -> None:
        if not (
            store.decision_store is decision_service.store
            and store.eligibility_store is eligibility_service.store
            and decision_service.eligibility_service is eligibility_service
        ):
            raise ValueError("Stable Promotion Outcome Service dependency 不一致。")
        self.decision_service = decision_service
        self.eligibility_service = eligibility_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(self, *, decision_id: str) -> EvolutionStablePromotionOutcomeView:
        decision, eligibility = await self._current_sources(decision_id)
        key = f"{decision.workbench_session_id}::{decision.workbench_proposal_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            decision, eligibility = await self._current_sources(decision_id)
            head = await self.store.head(
                workbench_session_id=decision.workbench_session_id,
                workbench_proposal_id=decision.workbench_proposal_id,
            )
            previous_event = await self.store.latest_event(
                workbench_session_id=decision.workbench_session_id,
                workbench_proposal_id=decision.workbench_proposal_id,
            )
            outcome, event = build_stable_promotion_outcome_pair(
                decision=decision,
                eligibility=eligibility,
                head=head,
                previous_event=previous_event,
            )
            stored, _stored_event = await self.store.record(outcome=outcome, event=event)
        return await self.inspect(outcome_id=stored.outcome_id)

    async def inspect(self, *, outcome_id: str) -> EvolutionStablePromotionOutcomeView:
        pair = await self.store.get_pair(outcome_id)
        if pair is None:
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_missing",
                "Stable Promotion Outcome 不存在。",
            )
        outcome, event = pair
        durable = chain = decision_authority = eligibility_authority = False
        head_authority = superseded = False
        reasons: list[str] = []
        try:
            durable = await self.store.get_pair(outcome.outcome_id) == pair
            chain = await self.store.chain_valid(
                workbench_session_id=outcome.workbench_session_id,
                workbench_proposal_id=outcome.workbench_proposal_id,
            )
            head = await self.store.head(
                workbench_session_id=outcome.workbench_session_id,
                workbench_proposal_id=outcome.workbench_proposal_id,
            )
            head_authority = head == outcome
            superseded = bool(
                head is not None and head != outcome and head.sequence > outcome.sequence
            )
        except (EvolutionStablePromotionOutcomeError, OSError, RuntimeError, ValueError):
            reasons.append("durable_chain_unavailable")
        try:
            decision_view = await self.decision_service.inspect(decision_id=outcome.decision_id)
            decision_authority = bool(
                decision_view.promoted_outcome_ready_authority
                and decision_view.current_decision == decision_view.decision
                and decision_view.decision.decision_sha256 == outcome.decision_sha256
            )
        except (EvolutionStablePromotionOutcomeDecisionError, OSError, RuntimeError, ValueError):
            reasons.append("decision_authority_unavailable")
        try:
            eligibility_view = await self.eligibility_service.inspect(
                eligibility_id=outcome.eligibility_id
            )
            eligibility_authority = bool(
                eligibility_view.outcome_review_ready_authority
                and eligibility_view.current_eligibility is not None
                and eligibility_view.current_eligibility.eligibility_sha256
                == outcome.eligibility_sha256
            )
        except (EvolutionStablePromotionOutcomeEligibilityError, OSError, RuntimeError, ValueError):
            reasons.append("eligibility_authority_unavailable")
        checks = {
            "outcome_pair_changed": durable,
            "outcome_chain_invalid": chain,
            "promote_decision_stale": decision_authority,
            "outcome_eligibility_stale": eligibility_authority,
            "newer_promoted_outcome_exists": head_authority,
        }
        reasons.extend(reason for reason, passed in checks.items() if not passed)
        authority = bool(all(checks.values()))
        return EvolutionStablePromotionOutcomeView(
            outcome=outcome,
            supersede_event=event,
            current_outcome=outcome if authority else None,
            durable_pair_valid=durable,
            chain_valid=chain,
            decision_authority=decision_authority,
            eligibility_authority=eligibility_authority,
            projection_head_authority=head_authority,
            invalidation_reasons=tuple(sorted(set(reasons))),
            promoted_outcome_authority=authority,
            superseded=superseded,
        )

    async def _current_sources(
        self, decision_id: str
    ) -> tuple[
        EvolutionStablePromotionOutcomeDecision,
        EvolutionStablePromotionOutcomeEligibility,
    ]:
        decision_view = await self.decision_service.inspect(decision_id=decision_id)
        decision = decision_view.current_decision
        if not (
            decision_view.promoted_outcome_ready_authority
            and decision is not None
            and decision.action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE
        ):
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_decision_not_promote",
                "只有 current promote Decision 才能写 Stable Promotion Outcome。",
            )
        eligibility_view = await self.eligibility_service.inspect(
            eligibility_id=decision.eligibility_id
        )
        eligibility = eligibility_view.current_eligibility
        if not (
            eligibility_view.outcome_review_ready_authority
            and eligibility is not None
            and eligibility.eligibility_sha256 == decision.eligibility_sha256
        ):
            raise EvolutionStablePromotionOutcomeError(
                "stable_promotion_outcome_eligibility_stale",
                "Promote Decision 绑定的 Eligibility 已失效。",
            )
        return decision, eligibility


def build_stable_promotion_outcome_pair(
    *,
    decision: EvolutionStablePromotionOutcomeDecision,
    eligibility: EvolutionStablePromotionOutcomeEligibility,
    head: EvolutionStablePromotionOutcome | None,
    previous_event: EvolutionStablePromotionOutcomeSupersedeEvent | None,
) -> tuple[
    EvolutionStablePromotionOutcome,
    EvolutionStablePromotionOutcomeSupersedeEvent,
]:
    decision_item = EvolutionStablePromotionOutcomeDecision.model_validate(decision)
    eligibility_item = EvolutionStablePromotionOutcomeEligibility.model_validate(eligibility)
    if not (
        decision_item.action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE
        and decision_item.eligibility_id == eligibility_item.eligibility_id
        and decision_item.eligibility_sha256 == eligibility_item.eligibility_sha256
        and decision_item.workbench_session_id == eligibility_item.workbench_session_id
        and decision_item.workbench_proposal_id == eligibility_item.workbench_proposal_id
        and decision_item.candidate_id == eligibility_item.candidate_id
        and decision_item.candidate_revision == eligibility_item.candidate_revision
        and decision_item.candidate_sha256 == eligibility_item.candidate_sha256
    ):
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_lineage_mismatch",
            "Promote Decision 与 Outcome Eligibility lineage 不一致。",
        )
    if (head is None) is not (previous_event is None):
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_head_incomplete",
            "Outcome 与 Supersede Event head 不完整。",
        )
    if head is not None and not (
        head.workbench_session_id == decision_item.workbench_session_id
        and head.workbench_proposal_id == decision_item.workbench_proposal_id
        and previous_event is not None
        and previous_event.successor_outcome_id == head.outcome_id
        and previous_event.successor_outcome_sha256 == head.outcome_sha256
        and previous_event.sequence == head.sequence
    ):
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_head_mismatch",
            "Outcome 与 Supersede Event head 不一致。",
        )
    sequence = 1 if head is None else head.sequence + 1
    previous_outcome_id = "" if head is None else head.outcome_id
    previous_outcome_sha = "" if head is None else head.outcome_sha256
    outcome_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OUTCOME_POLICY,
        "workspace_root": eligibility_item.workspace_root,
        "status": "promoted",
        "sequence": sequence,
        "previous_outcome_id": previous_outcome_id,
        "previous_outcome_sha256": previous_outcome_sha,
        "decision_id": decision_item.decision_id,
        "decision_sha256": decision_item.decision_sha256,
        "eligibility_id": eligibility_item.eligibility_id,
        "eligibility_sha256": eligibility_item.eligibility_sha256,
        "contract_id": eligibility_item.contract_id,
        "population_assessment_id": eligibility_item.population_assessment_id,
        "population_denominator": eligibility_item.population_denominator,
        "passing_count": eligibility_item.passing_count,
        "assessment_coverage_bps": 10_000,
        "duration_coverage_bps": 10_000,
        "workbench_session_id": eligibility_item.workbench_session_id,
        "workbench_proposal_id": eligibility_item.workbench_proposal_id,
        "proposal_id": eligibility_item.proposal_id,
        "candidate_id": eligibility_item.candidate_id,
        "candidate_revision": eligibility_item.candidate_revision,
        "candidate_sha256": eligibility_item.candidate_sha256,
        "candidate_version": eligibility_item.candidate_version,
        "candidate_target": eligibility_item.candidate_target,
        "promoted_at": decision_item.decided_at,
        "post_observation_decision_verified": True,
        "population_sustained_health_verified": True,
        "promoted": True,
        "superseded": False,
        "long_term_metrics_recorded": True,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    outcome_digest = _digest(outcome_core)
    outcome = EvolutionStablePromotionOutcome.model_validate(
        {
            **outcome_core,
            "outcome_id": f"evstablepromout_{outcome_digest[:24]}",
            "outcome_sha256": outcome_digest,
        }
    )
    previous_event_id = "" if previous_event is None else previous_event.event_id
    previous_event_sha = "" if previous_event is None else previous_event.event_sha256
    event_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OUTCOME_SUPERSEDE_POLICY,
        "workspace_root": eligibility_item.workspace_root,
        "workbench_session_id": eligibility_item.workbench_session_id,
        "workbench_proposal_id": eligibility_item.workbench_proposal_id,
        "sequence": sequence,
        "previous_event_id": previous_event_id,
        "previous_event_sha256": previous_event_sha,
        "prior_outcome_id": previous_outcome_id,
        "prior_outcome_sha256": previous_outcome_sha,
        "successor_outcome_id": outcome.outcome_id,
        "successor_outcome_sha256": outcome.outcome_sha256,
        "decision_id": decision_item.decision_id,
        "transition": "promoted",
        "reason": "post_observation_promote_decision",
        "prior_outcome_superseded": head is not None,
        "projection_head_changed": True,
        "historical_outcome_deleted": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "recorded_at": decision_item.decided_at,
    }
    event_digest = _digest(event_core)
    event = EvolutionStablePromotionOutcomeSupersedeEvent.model_validate(
        {
            **event_core,
            "event_id": f"evstablepromoutsup_{event_digest[:24]}",
            "event_sha256": event_digest,
        }
    )
    return outcome, event


def render_stable_promotion_outcome(view: EvolutionStablePromotionOutcomeView) -> str:
    item = view.outcome
    event = view.supersede_event
    return "\n".join(
        (
            "## 稳定推广 Outcome Ledger",
            "",
            f"- Outcome：`{item.outcome_id}` sequence `{item.sequence}`",
            f"- 状态：`{item.status}`",
            f"- Decision：`{item.decision_id}`",
            f"- Eligibility：`{item.eligibility_id}`",
            f"- Candidate：`{item.candidate_id}` revision `{item.candidate_revision}`",
            f"- Population：`{item.passing_count}/{item.population_denominator}` passing",
            f"- Supersede Event：`{event.event_id}`",
            f"- Prior Outcome：`{event.prior_outcome_id or 'none'}`",
            f"- 当前 promoted authority：`{str(view.promoted_outcome_authority).lower()}`",
            f"- 已被后继替代：`{str(view.superseded).lower()}`",
            f"- 撤权原因：`{', '.join(view.invalidation_reasons) or 'none'}`",
            "- Learning / Promotion / Execution authority：`false`",
        )
    )


async def _exact_sources(
    db: aiosqlite.Connection,
    item: EvolutionStablePromotionOutcome,
) -> tuple[
    EvolutionStablePromotionOutcomeDecision,
    EvolutionStablePromotionOutcomeEligibility,
]:
    decision_row = await (
        await db.execute(
            "SELECT decision_json FROM evolution_stable_promotion_outcome_decisions "
            "WHERE decision_id = ?",
            (item.decision_id,),
        )
    ).fetchone()
    eligibility_row = await (
        await db.execute(
            "SELECT eligibility_json FROM evolution_stable_promotion_outcome_eligibilities "
            "WHERE eligibility_id = ?",
            (item.eligibility_id,),
        )
    ).fetchone()
    if decision_row is None or eligibility_row is None:
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_source_missing",
            "Promote Decision 或 Eligibility durable source 不存在。",
        )
    decision = EvolutionStablePromotionOutcomeDecision.model_validate_json(
        decision_row["decision_json"]
    )
    eligibility = EvolutionStablePromotionOutcomeEligibility.model_validate_json(
        eligibility_row["eligibility_json"]
    )
    latest_decision = await (
        await db.execute(
            "SELECT decision_id FROM evolution_stable_promotion_outcome_decisions "
            "WHERE eligibility_id = ? ORDER BY revision DESC LIMIT 1",
            (item.eligibility_id,),
        )
    ).fetchone()
    latest_eligibility = await (
        await db.execute(
            "SELECT eligibility_id FROM evolution_stable_promotion_outcome_eligibilities "
            "WHERE contract_id = ? ORDER BY rowid DESC LIMIT 1",
            (item.contract_id,),
        )
    ).fetchone()
    if not (
        latest_decision is not None
        and latest_decision["decision_id"] == item.decision_id
        and latest_eligibility is not None
        and latest_eligibility["eligibility_id"] == item.eligibility_id
        and decision.action is EvolutionStablePromotionOutcomeDecisionAction.PROMOTE
    ):
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_source_stale",
            "Promote Decision 或 Eligibility 已不是 durable current source。",
        )
    return decision, eligibility


def _pair_matches(
    outcome: EvolutionStablePromotionOutcome,
    event: EvolutionStablePromotionOutcomeSupersedeEvent,
) -> bool:
    return bool(
        outcome.workspace_root == event.workspace_root
        and outcome.workbench_session_id == event.workbench_session_id
        and outcome.workbench_proposal_id == event.workbench_proposal_id
        and outcome.sequence == event.sequence
        and outcome.previous_outcome_id == event.prior_outcome_id
        and outcome.previous_outcome_sha256 == event.prior_outcome_sha256
        and outcome.outcome_id == event.successor_outcome_id
        and outcome.outcome_sha256 == event.successor_outcome_sha256
        and outcome.decision_id == event.decision_id
        and outcome.promoted_at == event.recorded_at
    )


def _chain_matches(
    outcome_rows: list[aiosqlite.Row],
    event_rows: list[aiosqlite.Row],
) -> bool:
    if len(outcome_rows) != len(event_rows) or len(outcome_rows) > _MAX_CHAIN_LENGTH:
        return False
    prior_outcome = None
    prior_event = None
    for sequence, (outcome_row, event_row) in enumerate(
        zip(outcome_rows, event_rows, strict=True), start=1
    ):
        outcome = _row_outcome(outcome_row)
        event = _row_event(event_row)
        if not (
            outcome.sequence == event.sequence == sequence
            and _pair_matches(outcome, event)
            and outcome.previous_outcome_id
            == ("" if prior_outcome is None else prior_outcome.outcome_id)
            and outcome.previous_outcome_sha256
            == ("" if prior_outcome is None else prior_outcome.outcome_sha256)
            and event.previous_event_id == ("" if prior_event is None else prior_event.event_id)
            and event.previous_event_sha256
            == ("" if prior_event is None else prior_event.event_sha256)
            and event.prior_outcome_superseded is (prior_outcome is not None)
        ):
            return False
        prior_outcome = outcome
        prior_event = event
    return True


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_outcomes ("
        "outcome_id TEXT PRIMARY KEY, outcome_sha256 TEXT NOT NULL UNIQUE, "
        "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL, "
        "sequence INTEGER NOT NULL, decision_id TEXT NOT NULL UNIQUE, "
        "eligibility_id TEXT NOT NULL, outcome_json TEXT NOT NULL, "
        "promoted_at TEXT NOT NULL, "
        "UNIQUE(workbench_session_id, workbench_proposal_id, sequence));"
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_outcome_supersede_events ("
        "event_id TEXT PRIMARY KEY, event_sha256 TEXT NOT NULL UNIQUE, "
        "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL, "
        "sequence INTEGER NOT NULL, previous_event_id TEXT NOT NULL, "
        "prior_outcome_id TEXT NOT NULL, successor_outcome_id TEXT NOT NULL UNIQUE, "
        "event_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
        "UNIQUE(workbench_session_id, workbench_proposal_id, sequence));"
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_outcome_head "
        "ON evolution_stable_promotion_outcomes("
        "workbench_session_id, workbench_proposal_id, sequence DESC);"
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_outcome_event_head "
        "ON evolution_stable_promotion_outcome_supersede_events("
        "workbench_session_id, workbench_proposal_id, sequence DESC);"
    )


def _row_outcome(row: aiosqlite.Row) -> EvolutionStablePromotionOutcome:
    item = EvolutionStablePromotionOutcome.model_validate_json(row["outcome_json"])
    indexed = (
        row["outcome_id"],
        row["outcome_sha256"],
        row["workbench_session_id"],
        row["workbench_proposal_id"],
        row["sequence"],
        row["decision_id"],
        row["eligibility_id"],
        row["promoted_at"],
    )
    expected = (
        item.outcome_id,
        item.outcome_sha256,
        item.workbench_session_id,
        item.workbench_proposal_id,
        item.sequence,
        item.decision_id,
        item.eligibility_id,
        item.promoted_at,
    )
    if indexed != expected:
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_store_corrupt",
            "Stable Promotion Outcome 索引与 JSON 不一致。",
        )
    return item


def _row_event(row: aiosqlite.Row) -> EvolutionStablePromotionOutcomeSupersedeEvent:
    item = EvolutionStablePromotionOutcomeSupersedeEvent.model_validate_json(row["event_json"])
    indexed = (
        row["event_id"],
        row["event_sha256"],
        row["workbench_session_id"],
        row["workbench_proposal_id"],
        row["sequence"],
        row["previous_event_id"],
        row["prior_outcome_id"],
        row["successor_outcome_id"],
        row["recorded_at"],
    )
    expected = (
        item.event_id,
        item.event_sha256,
        item.workbench_session_id,
        item.workbench_proposal_id,
        item.sequence,
        item.previous_event_id,
        item.prior_outcome_id,
        item.successor_outcome_id,
        item.recorded_at,
    )
    if indexed != expected:
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_store_corrupt",
            "Stable Promotion Supersede Event 索引与 JSON 不一致。",
        )
    return item


def _outcome_id(value: str) -> str:
    identifier = str(value or "").strip()
    if not _OUTCOME_RE.fullmatch(identifier):
        raise EvolutionStablePromotionOutcomeError(
            "stable_promotion_outcome_id_invalid",
            "Stable Promotion Outcome ID 无效。",
        )
    return identifier


def _aware(value: str):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Stable Promotion Outcome 时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OUTCOME_POLICY",
    "EVOLUTION_STABLE_PROMOTION_OUTCOME_SUPERSEDE_POLICY",
    "EvolutionStablePromotionOutcome",
    "EvolutionStablePromotionOutcomeError",
    "EvolutionStablePromotionOutcomeService",
    "EvolutionStablePromotionOutcomeStore",
    "EvolutionStablePromotionOutcomeSupersedeEvent",
    "EvolutionStablePromotionOutcomeView",
    "build_stable_promotion_outcome_pair",
    "render_stable_promotion_outcome",
]
