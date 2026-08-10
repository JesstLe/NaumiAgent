"""Append-only long-term Outcome revisions after a verified rollback recovery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.post_rollback_long_term_observation_assessments import (
    EvolutionPostRollbackLongTermObservationAssessment,
    EvolutionPostRollbackLongTermObservationAssessmentError,
    EvolutionPostRollbackLongTermObservationAssessmentService,
    EvolutionPostRollbackLongTermObservationAssessmentStore,
    EvolutionPostRollbackLongTermObservationStatus,
)
from naumi_agent.evolution.post_rollback_long_term_observation_contracts import (
    EvolutionPostRollbackLongTermObservationContract,
    EvolutionPostRollbackLongTermObservationContractError,
    EvolutionPostRollbackLongTermObservationContractService,
    EvolutionPostRollbackLongTermObservationContractStore,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
    EvolutionRevalidationRollbackOutcomeStore,
)

EVOLUTION_POST_ROLLBACK_LONG_TERM_OUTCOME_POLICY = (
    "evolution-post-rollback-long-term-outcome-v1"
)
EVOLUTION_POST_ROLLBACK_OUTCOME_SUPERSEDE_POLICY = (
    "evolution-post-rollback-outcome-supersede-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_CHAIN_LENGTH = 10_000


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackLongTermOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-long-term-outcome-v1"] = (
        EVOLUTION_POST_ROLLBACK_LONG_TERM_OUTCOME_POLICY
    )
    outcome_id: str = Field(pattern=r"^evpostlongout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    status: Literal["rollback_recovery_observed"] = "rollback_recovery_observed"
    revision_sequence: int = Field(ge=1)
    root_rollback_outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    root_rollback_outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    prior_outcome_kind: Literal["rollback_outcome", "long_term_outcome"]
    prior_outcome_id: str = Field(
        pattern=r"^(?:evrerollbackout|evpostlongout)_[0-9a-f]{24}$"
    )
    prior_outcome_sha256: str = Field(pattern=_SHA256_RE)
    observation_contract_id: str = Field(
        pattern=r"^evpostobservecontract_[0-9a-f]{24}$"
    )
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    behavioral_matrix_id: str = Field(pattern=r"^evpostmatrix_[0-9a-f]{24}$")
    behavioral_matrix_sha256: str = Field(pattern=_SHA256_RE)
    assessment_id: str = Field(pattern=r"^evpostobservewindow_[0-9a-f]{24}$")
    assessment_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=r"^evpostobserveadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    proposal_id: str = Field(pattern=r"^evp_[0-9a-f]{24}$")
    proposal_kind: Literal["knowledge", "profile", "prompt", "tool", "test", "code"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    baseline_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_version: str = Field(min_length=1, max_length=128)
    baseline_target: str = Field(min_length=1, max_length=128)
    runtime_subject_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")
    runtime_binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    runtime_identity_id: str = Field(pattern=r"^relruntimeidentity_[0-9a-f]{24}$")
    assessment_head_sequence: int = Field(ge=1)
    observation_seconds: int = Field(ge=3600, le=604_800)
    operational_sample_count: int = Field(ge=12, le=5000)
    rollback_fact_preserved: Literal[True] = True
    behavioral_recovery_verified: Literal[True] = True
    long_term_metrics_recorded: Literal[True] = True
    baseline_sustained_health_verified: Literal[True] = True
    candidate_promoted: Literal[False] = False
    promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Long-Term Outcome workspace 必须 canonical。")
        expected_prefix = (
            "evrerollbackout_"
            if self.prior_outcome_kind == "rollback_outcome"
            else "evpostlongout_"
        )
        if not self.prior_outcome_id.startswith(expected_prefix):
            raise ValueError("Long-Term Outcome prior kind/ID 不一致。")
        _aware(self.recorded_at)
        digest = _digest(
            self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        )
        if not (
            hmac.compare_digest(self.outcome_sha256, digest)
            and self.outcome_id == f"evpostlongout_{digest[:24]}"
        ):
            raise ValueError("Long-Term Outcome content identity 不一致。")
        return self


class EvolutionPostRollbackOutcomeSupersedeEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-outcome-supersede-v1"] = (
        EVOLUTION_POST_ROLLBACK_OUTCOME_SUPERSEDE_POLICY
    )
    event_id: str = Field(pattern=r"^evpostoutsup_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    root_rollback_outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    root_rollback_outcome_sha256: str = Field(pattern=_SHA256_RE)
    sequence: int = Field(ge=1)
    previous_event_id: str = Field(pattern=r"^(|evpostoutsup_[0-9a-f]{24})$")
    previous_event_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    prior_outcome_kind: Literal["rollback_outcome", "long_term_outcome"]
    prior_outcome_id: str = Field(
        pattern=r"^(?:evrerollbackout|evpostlongout)_[0-9a-f]{24}$"
    )
    prior_outcome_sha256: str = Field(pattern=_SHA256_RE)
    successor_outcome_id: str = Field(pattern=r"^evpostlongout_[0-9a-f]{24}$")
    successor_outcome_sha256: str = Field(pattern=_SHA256_RE)
    transition: Literal["rollback_recovery_observed"] = "rollback_recovery_observed"
    reason: Literal["behavioral_and_long_term_baseline_recovery_verified"] = (
        "behavioral_and_long_term_baseline_recovery_verified"
    )
    projection_head_changed: Literal[True] = True
    rollback_fact_deleted: Literal[False] = False
    candidate_promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Outcome Supersede Event workspace 必须 canonical。")
        if (self.sequence == 1) is not (
            not self.previous_event_id and not self.previous_event_sha256
        ):
            raise ValueError("Outcome Supersede Event previous event chain 无效。")
        _aware(self.recorded_at)
        digest = _digest(
            self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        )
        if not (
            hmac.compare_digest(self.event_sha256, digest)
            and self.event_id == f"evpostoutsup_{digest[:24]}"
        ):
            raise ValueError("Outcome Supersede Event content identity 不一致。")
        return self


class EvolutionPostRollbackLongTermOutcomeView(_StrictModel):
    outcome: EvolutionPostRollbackLongTermOutcome
    supersede_event: EvolutionPostRollbackOutcomeSupersedeEvent
    durable_pair_valid: bool
    root_rollback_outcome_authority: bool
    observation_contract_authority: bool
    bound_assessment_source_valid: bool
    current_long_term_health_authority: bool
    projection_head_authority: bool
    outcome_authority: bool
    promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_pair_valid
            and self.root_rollback_outcome_authority
            and self.observation_contract_authority
            and self.bound_assessment_source_valid
            and self.current_long_term_health_authority
            and self.projection_head_authority
        )
        if self.outcome_authority is not expected:
            raise ValueError("Long-Term Outcome authority projection 不一致。")
        return self


class EvolutionPostRollbackLongTermOutcomeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackLongTermOutcomeStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        rollback_outcome_store: EvolutionRevalidationRollbackOutcomeStore,
        contract_store: EvolutionPostRollbackLongTermObservationContractStore,
        assessment_store: EvolutionPostRollbackLongTermObservationAssessmentStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        if not (
            self.db_path
            == rollback_outcome_store.db_path
            == contract_store.db_path
            == assessment_store.db_path
        ):
            raise ValueError("Long-Term Outcome sources 必须共享 session SQLite。")
        self.rollback_outcome_store = rollback_outcome_store
        self.contract_store = contract_store
        self.assessment_store = assessment_store

    async def head(
        self,
        request_id: str,
    ) -> EvolutionPostRollbackLongTermOutcome | None:
        request = _request_id(request_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_outcomes "
                        "WHERE request_id = ? ORDER BY revision_sequence DESC LIMIT 1",
                        (request,),
                    )
                ).fetchone()
            return None if row is None else _row_outcome(row)
        except EvolutionPostRollbackLongTermOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_store_corrupt",
                "Long-Term Outcome durable source 损坏或无法读取。",
            ) from exc

    async def latest_event(
        self,
        request_id: str,
    ) -> EvolutionPostRollbackOutcomeSupersedeEvent | None:
        request = _request_id(request_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_outcome_supersede_events "
                        "WHERE request_id = ? ORDER BY sequence DESC LIMIT 1",
                        (request,),
                    )
                ).fetchone()
            return None if row is None else _row_event(row)
        except EvolutionPostRollbackLongTermOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_store_corrupt",
                "Outcome Supersede ledger 损坏或无法读取。",
            ) from exc

    async def get_by_assessment(
        self,
        assessment_id: str,
    ) -> tuple[
        EvolutionPostRollbackLongTermOutcome,
        EvolutionPostRollbackOutcomeSupersedeEvent,
    ] | None:
        identifier = _assessment_id(assessment_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_outcomes "
                        "WHERE assessment_id = ?",
                        (identifier,),
                    )
                ).fetchone()
                if row is None:
                    return None
                event = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_outcome_supersede_events "
                        "WHERE successor_outcome_id = ?",
                        (row["outcome_id"],),
                    )
                ).fetchone()
            if event is None:
                raise ValueError("supersede event missing")
            return _row_outcome(row), _row_event(event)
        except EvolutionPostRollbackLongTermOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_store_corrupt",
                "Long-Term Outcome durable pair 损坏或无法读取。",
            ) from exc

    async def chain_valid(
        self,
        request_id: str,
        root: EvolutionRevalidationRollbackOutcome,
    ) -> bool:
        request = _request_id(request_id)
        root_item = _root(root)
        if not self.db_path.is_file():
            return False
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                outcome_rows = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_outcomes "
                        "WHERE request_id = ? ORDER BY revision_sequence ASC "
                        "LIMIT ?",
                        (request, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
                event_rows = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_outcome_supersede_events "
                        "WHERE request_id = ? ORDER BY sequence ASC LIMIT ?",
                        (request, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
            return _chain_matches(outcome_rows, event_rows, root_item)
        except EvolutionPostRollbackLongTermOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_store_corrupt",
                "Outcome supersede ledger 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        *,
        outcome: EvolutionPostRollbackLongTermOutcome,
        event: EvolutionPostRollbackOutcomeSupersedeEvent,
        root: EvolutionRevalidationRollbackOutcome,
        contract: EvolutionPostRollbackLongTermObservationContract,
        assessment: EvolutionPostRollbackLongTermObservationAssessment,
    ) -> tuple[
        EvolutionPostRollbackLongTermOutcome,
        EvolutionPostRollbackOutcomeSupersedeEvent,
    ]:
        item = _outcome(outcome)
        transition = _event(event)
        root_item = _root(root)
        contract_item = _contract(contract)
        assessment_item = _assessment(assessment)
        if not (
            _pair_matches(item, transition, root_item)
            and _lineage_matches(item, root_item, contract_item, assessment_item)
        ):
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_pair_invalid",
                "Long-Term Outcome、Supersede Event 与 source lineage 不一致。",
            )
        outcome_json = item.model_dump_json()
        event_json = transition.model_dump_json()
        if max(len(outcome_json.encode()), len(event_json.encode())) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_oversized",
                "Long-Term Outcome 或 Supersede Event 超过 1 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                outcome_rows = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_outcomes "
                        "WHERE request_id = ? ORDER BY revision_sequence ASC LIMIT ?",
                        (item.request_id, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
                event_rows = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_post_rollback_outcome_supersede_events "
                        "WHERE request_id = ? ORDER BY sequence ASC LIMIT ?",
                        (item.request_id, _MAX_CHAIN_LENGTH + 1),
                    )
                ).fetchall()
                if not _chain_matches(outcome_rows, event_rows, root_item):
                    await db.rollback()
                    raise EvolutionPostRollbackLongTermOutcomeError(
                        "post_rollback_long_term_outcome_chain_corrupt",
                        "既有 Outcome supersede ledger 不连续。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_long_term_outcomes "
                        "WHERE assessment_id = ?",
                        (item.assessment_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _row_outcome(existing)
                    event_row = await (
                        await db.execute(
                            "SELECT * FROM "
                            "evolution_post_rollback_outcome_supersede_events "
                            "WHERE successor_outcome_id = ?",
                            (restored.outcome_id,),
                        )
                    ).fetchone()
                    await db.rollback()
                    if event_row is None:
                        raise ValueError("existing event missing")
                    restored_event = _row_event(event_row)
                    if restored == item and restored_event == transition:
                        return restored, restored_event
                    raise EvolutionPostRollbackLongTermOutcomeError(
                        "post_rollback_long_term_outcome_assessment_conflict",
                        "同一 Assessment 已绑定不同 Long-Term Outcome。",
                    )
                await _require_sources(
                    db,
                    item=item,
                    root=root_item,
                    contract=contract_item,
                    assessment=assessment_item,
                )
                head_row = outcome_rows[-1] if outcome_rows else None
                event_row = event_rows[-1] if event_rows else None
                actual_prior = root_item if head_row is None else _row_outcome(head_row)
                if not (
                    item.revision_sequence
                    == (1 if head_row is None else actual_prior.revision_sequence + 1)
                    and item.prior_outcome_id == actual_prior.outcome_id
                    and item.prior_outcome_sha256 == actual_prior.outcome_sha256
                    and transition.sequence == item.revision_sequence
                    and transition.previous_event_id
                    == ("" if event_row is None else str(event_row["event_id"]))
                    and transition.previous_event_sha256
                    == ("" if event_row is None else str(event_row["event_sha256"]))
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackLongTermOutcomeError(
                        "post_rollback_long_term_outcome_head_changed",
                        "Outcome projection head 在写入期间发生变化。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_long_term_outcomes "
                    "(outcome_id, outcome_sha256, request_id, root_outcome_id, "
                    "revision_sequence, prior_outcome_id, contract_id, assessment_id, "
                    "admission_id, status, outcome_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.outcome_id,
                        item.outcome_sha256,
                        item.request_id,
                        item.root_rollback_outcome_id,
                        item.revision_sequence,
                        item.prior_outcome_id,
                        item.observation_contract_id,
                        item.assessment_id,
                        item.admission_id,
                        item.status,
                        outcome_json,
                        item.recorded_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_outcome_supersede_events "
                    "(event_id, event_sha256, request_id, sequence, previous_event_id, "
                    "prior_outcome_id, successor_outcome_id, event_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        transition.event_id,
                        transition.event_sha256,
                        transition.request_id,
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
        except EvolutionPostRollbackLongTermOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_store_failed",
                "Long-Term Outcome 与 Supersede Event 无法持久化。",
            ) from exc


class EvolutionPostRollbackLongTermOutcomeService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        rollback_outcome_store: EvolutionRevalidationRollbackOutcomeStore,
        rollback_outcome_service: EvolutionRevalidationRollbackOutcomeService,
        contract_store: EvolutionPostRollbackLongTermObservationContractStore,
        contract_service: EvolutionPostRollbackLongTermObservationContractService,
        assessment_store: EvolutionPostRollbackLongTermObservationAssessmentStore,
        assessment_service: EvolutionPostRollbackLongTermObservationAssessmentService,
        store: EvolutionPostRollbackLongTermOutcomeStore,
    ) -> None:
        if not (
            store.rollback_outcome_store is rollback_outcome_store
            and store.contract_store is contract_store
            and store.assessment_store is assessment_store
        ):
            raise ValueError("Long-Term Outcome Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.rollback_outcome_store = rollback_outcome_store
        self.rollback_outcome_service = rollback_outcome_service
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.assessment_store = assessment_store
        self.assessment_service = assessment_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
        subject_id: str,
    ) -> EvolutionPostRollbackLongTermOutcomeView:
        request = _request_id(request_id)
        subject = _subject_id(subject_id)
        lock = self._locks.setdefault(request, asyncio.Lock())
        async with lock:
            root = await self.rollback_outcome_store.get_by_request(request)
            if root is None:
                raise EvolutionPostRollbackLongTermOutcomeError(
                    "post_rollback_long_term_root_missing",
                    "缺少 rolled_back Outcome root。",
                )
            root_view = await self.rollback_outcome_service.inspect(request_id=request)
            if not root_view.outcome_authority:
                raise EvolutionPostRollbackLongTermOutcomeError(
                    "post_rollback_long_term_root_stale",
                    "rolled_back Outcome authority 已失效。",
                )
            assessment_view = await self.assessment_service.assess(
                request_id=request,
                subject_id=subject,
            )
            if not assessment_view.long_term_health_authority:
                status = (
                    assessment_view.current_assessment.status.value
                    if assessment_view.current_assessment is not None
                    else "unavailable"
                )
                raise EvolutionPostRollbackLongTermOutcomeError(
                    "post_rollback_long_term_health_not_passing",
                    f"当前长期观察状态为 {status}，不能签发 Long-Term Outcome。",
                )
            assessment = assessment_view.receipt
            contract = assessment.contract
            if not (
                root.outcome_id == contract.outcome_id
                and root.outcome_sha256 == contract.outcome_sha256
                and root.request_id == contract.request_id
            ):
                raise EvolutionPostRollbackLongTermOutcomeError(
                    "post_rollback_long_term_lineage_invalid",
                    "Long-Term Assessment 未绑定 exact rolled_back Outcome。",
                )
            existing = await self.store.get_by_assessment(assessment.assessment_id)
            if existing is not None:
                return await self.inspect(outcome=existing[0], event=existing[1])
            for _attempt in range(3):
                head = await self.store.head(request)
                previous_event = await self.store.latest_event(request)
                outcome, event = _build_pair(
                    root=root,
                    contract=contract,
                    assessment=assessment,
                    head=head,
                    previous_event=previous_event,
                )
                try:
                    recorded = await self.store.record(
                        outcome=outcome,
                        event=event,
                        root=root,
                        contract=contract,
                        assessment=assessment,
                    )
                    view = await self.inspect(outcome=recorded[0], event=recorded[1])
                    if not view.outcome_authority:
                        raise EvolutionPostRollbackLongTermOutcomeError(
                            "post_rollback_long_term_outcome_authority_changed",
                            "Long-Term Outcome 持久化期间 authority 已变化。",
                        )
                    return view
                except EvolutionPostRollbackLongTermOutcomeError as exc:
                    if exc.code != "post_rollback_long_term_outcome_head_changed":
                        raise
            raise EvolutionPostRollbackLongTermOutcomeError(
                "post_rollback_long_term_outcome_contention",
                "Outcome projection head 持续竞争，请重试。",
            )

    async def inspect(
        self,
        *,
        outcome: EvolutionPostRollbackLongTermOutcome,
        event: EvolutionPostRollbackOutcomeSupersedeEvent,
    ) -> EvolutionPostRollbackLongTermOutcomeView:
        item = _outcome(outcome)
        transition = _event(event)
        durable = root_authority = contract_authority = False
        assessment_source = current_health = head_authority = False
        try:
            pair = await self.store.get_by_assessment(item.assessment_id)
            root = await self.rollback_outcome_store.get_by_request(item.request_id)
            contract = await self.contract_store.get_by_outcome(
                item.root_rollback_outcome_id
            )
            bound_assessment = await self.assessment_store.get(item.assessment_id)
            head = await self.store.head(item.request_id)
            if root is None or contract is None or bound_assessment is None:
                raise ValueError("long-term outcome source missing")
            chain_valid = await self.store.chain_valid(item.request_id, root)
            root_view = await self.rollback_outcome_service.inspect(
                request_id=item.request_id
            )
            contract_view = await self.contract_service.inspect(contract=contract)
            assessment_view = await self.assessment_service.inspect(
                admission_id=item.admission_id
            )
            durable = chain_valid and pair == (item, transition)
            root_authority = root_view.outcome_authority and root == root_view.outcome
            contract_authority = bool(
                contract_view.observation_contract_authority
                and contract.contract_id == item.observation_contract_id
                and contract.contract_sha256 == item.observation_contract_sha256
            )
            assessment_source = bool(
                bound_assessment == assessment_view.receipt
                or (
                    bound_assessment.status
                    is EvolutionPostRollbackLongTermObservationStatus.PASSING
                    and bound_assessment.assessment_id == item.assessment_id
                    and bound_assessment.assessment_sha256 == item.assessment_sha256
                )
            )
            current_health = bool(
                assessment_view.long_term_health_authority
                and assessment_view.current_assessment is not None
                and assessment_view.current_assessment.admission.admission_id
                == item.admission_id
            )
            head_authority = head == item
        except (
            EvolutionPostRollbackLongTermOutcomeError,
            EvolutionRevalidationRollbackOutcomeError,
            EvolutionPostRollbackLongTermObservationContractError,
            EvolutionPostRollbackLongTermObservationAssessmentError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            pass
        authority = bool(
            durable
            and root_authority
            and contract_authority
            and assessment_source
            and current_health
            and head_authority
        )
        return EvolutionPostRollbackLongTermOutcomeView(
            outcome=item,
            supersede_event=transition,
            durable_pair_valid=durable,
            root_rollback_outcome_authority=root_authority,
            observation_contract_authority=contract_authority,
            bound_assessment_source_valid=assessment_source,
            current_long_term_health_authority=current_health,
            projection_head_authority=head_authority,
            outcome_authority=authority,
        )


def render_post_rollback_long_term_outcome(
    view: EvolutionPostRollbackLongTermOutcomeView,
) -> str:
    item = view.outcome
    event = view.supersede_event
    return "\n".join(
        [
            f"# Post-Rollback Long-Term Outcome `{item.outcome_id}`",
            "",
            f"- 状态：`{item.status}`",
            f"- Revision：`{item.revision_sequence}`",
            f"- Prior Outcome：`{item.prior_outcome_kind}` / `{item.prior_outcome_id}`",
            f"- Assessment：`{item.assessment_id}`",
            f"- Runtime：`{item.runtime_subject_id}` / `{item.runtime_binding_id}`",
            f"- 持续健康：`{item.observation_seconds}s` / "
            f"`{item.operational_sample_count}` samples",
            f"- Supersede Event：`{event.event_id}`",
            f"- Projection head authority：`{str(view.projection_head_authority).lower()}`",
            f"- Outcome authority：`{str(view.outcome_authority).lower()}`",
            "- 原 rolled_back fact：保留",
            "- Candidate promoted：`false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_pair(
    *,
    root: EvolutionRevalidationRollbackOutcome,
    contract,
    assessment: EvolutionPostRollbackLongTermObservationAssessment,
    head: EvolutionPostRollbackLongTermOutcome | None,
    previous_event: EvolutionPostRollbackOutcomeSupersedeEvent | None,
) -> tuple[
    EvolutionPostRollbackLongTermOutcome,
    EvolutionPostRollbackOutcomeSupersedeEvent,
]:
    if not (
        assessment.status is EvolutionPostRollbackLongTermObservationStatus.PASSING
        and assessment.historical_long_term_health_passed
        and assessment.contract == contract
        and contract.outcome_id == root.outcome_id
        and contract.outcome_sha256 == root.outcome_sha256
        and assessment.admission.outcome_id == root.outcome_id
    ):
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_outcome_input_invalid",
            "Long-Term Outcome 需要 exact passing Assessment lineage。",
        )
    sequence = 1 if head is None else head.revision_sequence + 1
    prior_kind = "rollback_outcome" if head is None else "long_term_outcome"
    prior = root if head is None else head
    admission = assessment.admission
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_LONG_TERM_OUTCOME_POLICY,
        "workspace_root": root.workspace_root,
        "status": "rollback_recovery_observed",
        "revision_sequence": sequence,
        "root_rollback_outcome_id": root.outcome_id,
        "root_rollback_outcome_sha256": root.outcome_sha256,
        "request_id": root.request_id,
        "prior_outcome_kind": prior_kind,
        "prior_outcome_id": prior.outcome_id,
        "prior_outcome_sha256": prior.outcome_sha256,
        "observation_contract_id": contract.contract_id,
        "observation_contract_sha256": contract.contract_sha256,
        "behavioral_matrix_id": contract.behavioral_matrix_id,
        "behavioral_matrix_sha256": contract.behavioral_matrix_sha256,
        "assessment_id": assessment.assessment_id,
        "assessment_sha256": assessment.assessment_sha256,
        "admission_id": admission.admission_id,
        "admission_sha256": admission.admission_sha256,
        "workbench_session_id": root.workbench_session_id,
        "workbench_proposal_id": root.workbench_proposal_id,
        "proposal_id": root.proposal_id,
        "proposal_kind": root.proposal_kind,
        "candidate_id": root.candidate_id,
        "candidate_revision": root.candidate_revision,
        "candidate_sha256": root.candidate_sha256,
        "baseline_slot_id": contract.baseline_slot_id,
        "baseline_slot_sha256": contract.baseline_slot_sha256,
        "baseline_version": contract.baseline_version,
        "baseline_target": contract.baseline_target,
        "runtime_subject_id": admission.subject_id,
        "runtime_binding_id": admission.binding_id,
        "runtime_identity_id": admission.runtime_identity_id,
        "assessment_head_sequence": assessment.ledger_head_sequence,
        "observation_seconds": assessment.observation_seconds,
        "operational_sample_count": assessment.operational_sample_count,
        "rollback_fact_preserved": True,
        "behavioral_recovery_verified": True,
        "long_term_metrics_recorded": True,
        "baseline_sustained_health_verified": True,
        "candidate_promoted": False,
        "promoted": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "recorded_at": assessment.assessed_at,
    }
    digest = _digest(core)
    outcome = EvolutionPostRollbackLongTermOutcome.model_validate(
        {
            **core,
            "outcome_id": f"evpostlongout_{digest[:24]}",
            "outcome_sha256": digest,
        }
    )
    event_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_OUTCOME_SUPERSEDE_POLICY,
        "workspace_root": root.workspace_root,
        "request_id": root.request_id,
        "root_rollback_outcome_id": root.outcome_id,
        "root_rollback_outcome_sha256": root.outcome_sha256,
        "sequence": sequence,
        "previous_event_id": "" if previous_event is None else previous_event.event_id,
        "previous_event_sha256": (
            "" if previous_event is None else previous_event.event_sha256
        ),
        "prior_outcome_kind": prior_kind,
        "prior_outcome_id": prior.outcome_id,
        "prior_outcome_sha256": prior.outcome_sha256,
        "successor_outcome_id": outcome.outcome_id,
        "successor_outcome_sha256": outcome.outcome_sha256,
        "transition": "rollback_recovery_observed",
        "reason": "behavioral_and_long_term_baseline_recovery_verified",
        "projection_head_changed": True,
        "rollback_fact_deleted": False,
        "candidate_promoted": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "recorded_at": outcome.recorded_at,
    }
    event_digest = _digest(event_core)
    event = EvolutionPostRollbackOutcomeSupersedeEvent.model_validate(
        {
            **event_core,
            "event_id": f"evpostoutsup_{event_digest[:24]}",
            "event_sha256": event_digest,
        }
    )
    return outcome, event


async def _require_sources(db, *, item, root, contract, assessment) -> None:
    root_row = await (
        await db.execute(
            "SELECT outcome_json FROM evolution_revalidation_rollback_outcomes "
            "WHERE outcome_id = ? AND request_id = ?",
            (root.outcome_id, root.request_id),
        )
    ).fetchone()
    contract_row = await (
        await db.execute(
            "SELECT contract_json FROM evolution_post_rollback_observation_contracts "
            "WHERE contract_id = ? AND outcome_id = ?",
            (item.observation_contract_id, root.outcome_id),
        )
    ).fetchone()
    assessment_row = await (
        await db.execute(
            "SELECT assessment_json FROM evolution_post_rollback_long_term_assessments "
            "WHERE assessment_id = ? AND admission_id = ?",
            (item.assessment_id, item.admission_id),
        )
    ).fetchone()
    if not (
        root_row is not None
        and hmac.compare_digest(str(root_row["outcome_json"]), root.model_dump_json())
        and contract_row is not None
        and hmac.compare_digest(
            str(contract_row["contract_json"]), contract.model_dump_json()
        )
        and assessment_row is not None
        and hmac.compare_digest(
            str(assessment_row["assessment_json"]),
            assessment.model_dump_json(),
        )
    ):
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_outcome_source_changed",
            "Long-Term Outcome durable dependencies 已变化或不存在。",
        )


def _pair_matches(outcome, event, root) -> bool:
    return bool(
        outcome.workspace_root == event.workspace_root == root.workspace_root
        and outcome.request_id == event.request_id == root.request_id
        and outcome.root_rollback_outcome_id
        == event.root_rollback_outcome_id
        == root.outcome_id
        and outcome.root_rollback_outcome_sha256
        == event.root_rollback_outcome_sha256
        == root.outcome_sha256
        and outcome.revision_sequence == event.sequence
        and outcome.prior_outcome_kind == event.prior_outcome_kind
        and outcome.prior_outcome_id == event.prior_outcome_id
        and outcome.prior_outcome_sha256 == event.prior_outcome_sha256
        and outcome.outcome_id == event.successor_outcome_id
        and outcome.outcome_sha256 == event.successor_outcome_sha256
        and outcome.recorded_at == event.recorded_at
        and outcome.workbench_session_id == root.workbench_session_id
        and outcome.workbench_proposal_id == root.workbench_proposal_id
        and outcome.proposal_id == root.proposal_id
        and outcome.proposal_kind == root.proposal_kind
        and outcome.candidate_id == root.candidate_id
        and outcome.candidate_revision == root.candidate_revision
        and outcome.candidate_sha256 == root.candidate_sha256
    )


def _lineage_matches(outcome, root, contract, assessment) -> bool:
    admission = assessment.admission
    return bool(
        contract.workspace_root == outcome.workspace_root == root.workspace_root
        and contract.outcome_id
        == outcome.root_rollback_outcome_id
        == root.outcome_id
        and contract.outcome_sha256
        == outcome.root_rollback_outcome_sha256
        == root.outcome_sha256
        and contract.request_id == outcome.request_id == root.request_id
        and outcome.observation_contract_id == contract.contract_id
        and outcome.observation_contract_sha256 == contract.contract_sha256
        and outcome.behavioral_matrix_id == contract.behavioral_matrix_id
        and outcome.behavioral_matrix_sha256 == contract.behavioral_matrix_sha256
        and assessment.status
        is EvolutionPostRollbackLongTermObservationStatus.PASSING
        and assessment.historical_long_term_health_passed
        and assessment.contract == contract
        and outcome.assessment_id == assessment.assessment_id
        and outcome.assessment_sha256 == assessment.assessment_sha256
        and outcome.admission_id == admission.admission_id
        and outcome.admission_sha256 == admission.admission_sha256
        and outcome.baseline_slot_id == contract.baseline_slot_id
        and outcome.baseline_slot_sha256 == contract.baseline_slot_sha256
        and outcome.baseline_version == contract.baseline_version
        and outcome.baseline_target == contract.baseline_target
        and outcome.runtime_subject_id == admission.subject_id
        and outcome.runtime_binding_id == admission.binding_id
        and outcome.runtime_identity_id == admission.runtime_identity_id
        and outcome.assessment_head_sequence == assessment.ledger_head_sequence
        and outcome.observation_seconds == assessment.observation_seconds
        and outcome.operational_sample_count == assessment.operational_sample_count
        and outcome.recorded_at == assessment.assessed_at
    )


def _chain_matches(outcome_rows, event_rows, root) -> bool:
    if not (
        len(outcome_rows) == len(event_rows)
        and len(outcome_rows) <= _MAX_CHAIN_LENGTH
    ):
        return False
    prior = root
    previous_event = None
    for sequence, (outcome_row, event_row) in enumerate(
        zip(outcome_rows, event_rows, strict=True),
        start=1,
    ):
        outcome = _row_outcome(outcome_row)
        event = _row_event(event_row)
        prior_kind = "rollback_outcome" if sequence == 1 else "long_term_outcome"
        if not (
            outcome.revision_sequence == event.sequence == sequence
            and outcome.prior_outcome_kind == event.prior_outcome_kind == prior_kind
            and outcome.prior_outcome_id == event.prior_outcome_id == prior.outcome_id
            and outcome.prior_outcome_sha256
            == event.prior_outcome_sha256
            == prior.outcome_sha256
            and event.previous_event_id
            == ("" if previous_event is None else previous_event.event_id)
            and event.previous_event_sha256
            == ("" if previous_event is None else previous_event.event_sha256)
            and _pair_matches(outcome, event, root)
        ):
            return False
        prior = outcome
        previous_event = event
    return True


def _outcome(value) -> EvolutionPostRollbackLongTermOutcome:
    try:
        return EvolutionPostRollbackLongTermOutcome.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_outcome_invalid",
            "Post-Rollback Long-Term Outcome 无效。",
        ) from exc


def _event(value) -> EvolutionPostRollbackOutcomeSupersedeEvent:
    try:
        return EvolutionPostRollbackOutcomeSupersedeEvent.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_event_invalid",
            "Post-Rollback Outcome Supersede Event 无效。",
        ) from exc


def _root(value) -> EvolutionRevalidationRollbackOutcome:
    try:
        return EvolutionRevalidationRollbackOutcome.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_root_invalid",
            "rolled_back Outcome root 无效。",
        ) from exc


def _contract(value) -> EvolutionPostRollbackLongTermObservationContract:
    try:
        return EvolutionPostRollbackLongTermObservationContract.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_contract_invalid",
            "Post-Rollback Observation Contract 无效。",
        ) from exc


def _assessment(value) -> EvolutionPostRollbackLongTermObservationAssessment:
    try:
        return EvolutionPostRollbackLongTermObservationAssessment.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_assessment_invalid",
            "Post-Rollback Long-Term Assessment 无效。",
        ) from exc


def _row_outcome(row) -> EvolutionPostRollbackLongTermOutcome:
    raw = str(row["outcome_json"])
    if len(raw.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("long-term outcome oversized")
    item = EvolutionPostRollbackLongTermOutcome.model_validate_json(raw)
    if not (
        item.outcome_id == row["outcome_id"]
        and item.outcome_sha256 == row["outcome_sha256"]
        and item.request_id == row["request_id"]
        and item.root_rollback_outcome_id == row["root_outcome_id"]
        and item.revision_sequence == row["revision_sequence"]
        and item.prior_outcome_id == row["prior_outcome_id"]
        and item.observation_contract_id == row["contract_id"]
        and item.assessment_id == row["assessment_id"]
        and item.admission_id == row["admission_id"]
        and item.status == row["status"]
        and item.recorded_at == row["recorded_at"]
    ):
        raise ValueError("long-term outcome row mismatch")
    return item


def _row_event(row) -> EvolutionPostRollbackOutcomeSupersedeEvent:
    raw = str(row["event_json"])
    if len(raw.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("supersede event oversized")
    item = EvolutionPostRollbackOutcomeSupersedeEvent.model_validate_json(raw)
    if not (
        item.event_id == row["event_id"]
        and item.event_sha256 == row["event_sha256"]
        and item.request_id == row["request_id"]
        and item.sequence == row["sequence"]
        and item.previous_event_id == row["previous_event_id"]
        and item.prior_outcome_id == row["prior_outcome_id"]
        and item.successor_outcome_id == row["successor_outcome_id"]
        and item.recorded_at == row["recorded_at"]
    ):
        raise ValueError("supersede event row mismatch")
    return item


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackreq_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _subject_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^[a-z][a-z0-9_-]{0,95}$", normalized) is None:
        raise EvolutionPostRollbackLongTermOutcomeError(
            "post_rollback_long_term_subject_id_invalid",
            "Runtime Subject ID 格式无效。",
        )
    return normalized


def _assessment_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evpostobservewindow_[0-9a-f]{24}$", normalized) is None:
        raise ValueError("Long-Term Assessment ID 格式无效。")
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Long-Term Outcome 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_long_term_outcomes ("
        "outcome_id TEXT PRIMARY KEY, outcome_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL, root_outcome_id TEXT NOT NULL, "
        "revision_sequence INTEGER NOT NULL, prior_outcome_id TEXT NOT NULL, "
        "contract_id TEXT NOT NULL, assessment_id TEXT NOT NULL UNIQUE, "
        "admission_id TEXT NOT NULL, status TEXT NOT NULL, "
        "outcome_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
        "UNIQUE(request_id, revision_sequence));"
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_outcome_supersede_events ("
        "event_id TEXT PRIMARY KEY, event_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
        "previous_event_id TEXT NOT NULL, prior_outcome_id TEXT NOT NULL, "
        "successor_outcome_id TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL, UNIQUE(request_id, sequence));"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_long_outcome_head "
        "ON evolution_post_rollback_long_term_outcomes("
        "request_id, revision_sequence DESC);"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_outcome_supersede_head "
        "ON evolution_post_rollback_outcome_supersede_events("
        "request_id, sequence DESC);"
    )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_LONG_TERM_OUTCOME_POLICY",
    "EVOLUTION_POST_ROLLBACK_OUTCOME_SUPERSEDE_POLICY",
    "EvolutionPostRollbackLongTermOutcome",
    "EvolutionPostRollbackLongTermOutcomeError",
    "EvolutionPostRollbackLongTermOutcomeService",
    "EvolutionPostRollbackLongTermOutcomeStore",
    "EvolutionPostRollbackLongTermOutcomeView",
    "EvolutionPostRollbackOutcomeSupersedeEvent",
    "render_post_rollback_long_term_outcome",
]
