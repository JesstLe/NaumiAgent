"""Aggregate current opt-in execution outcomes into stage-completion evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_execution_outcome_ledger import (
    EvolutionRevalidationOptInExecutionOutcomeLedgerService,
)
from naumi_agent.evolution.revalidation_opt_in_observation_window_assessments import (
    EvolutionRevalidationOptInObservationAssessmentError,
    EvolutionRevalidationOptInObservationWindowService,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineError,
    EvolutionRevalidationRolloutBaselineService,
    EvolutionRevalidationRolloutCostSource,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutStageName,
)

EVOLUTION_REVALIDATION_OPT_IN_STAGE_COMPLETION_POLICY = (
    "evolution-revalidation-opt-in-stage-completion-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_OUTCOMES = 100
_MAX_ARTIFACT_BYTES = 512 * 1024


class EvolutionRevalidationOptInStageCompletionStatus(StrEnum):
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInStageCompletion(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-stage-completion-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_STAGE_COMPLETION_POLICY
    evidence_id: str = Field(pattern=r"^evreoptincomplete_[0-9a-f]{24}$")
    evidence_sha256: str = Field(pattern=_SHA256_RE)
    source_set_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    completion_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    baseline_id: str = Field(pattern=r"^evrerolloutbaseline_[0-9a-f]{24}$")
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    liveness_window_id: str = Field(pattern=r"^evreoptinwindow_[0-9a-f]{24}$")
    liveness_window_sha256: str = Field(pattern=_SHA256_RE)
    stage_order: Literal[2] = 2
    completed_stage: Literal["opt_in"] = "opt_in"
    next_stage: Literal["percentage"] = "percentage"
    outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    outcome_sha256: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    run_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    authoritative_outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    successful_outcome_ids: tuple[str, ...] = Field(max_length=_MAX_OUTCOMES)
    duration_micros: tuple[int, ...] = Field(max_length=_MAX_OUTCOMES)
    reported_cost_microusd: tuple[int, ...] = Field(max_length=_MAX_OUTCOMES)
    observed_runs: int = Field(ge=0, le=_MAX_OUTCOMES)
    minimum_completed_runs: int = Field(ge=10, le=_MAX_OUTCOMES)
    successful_runs: int = Field(ge=0, le=_MAX_OUTCOMES)
    unsuccessful_runs: int = Field(ge=0, le=_MAX_OUTCOMES)
    error_rate_basis_points: int = Field(ge=0, le=10_000)
    max_error_rate_basis_points: int = Field(ge=0, le=10_000)
    completion_rate_basis_points: int = Field(ge=0, le=10_000)
    baseline_completion_rate_basis_points: int = Field(ge=0, le=10_000)
    completion_rate_drop_basis_points: int = Field(ge=0, le=10_000)
    max_completion_rate_drop_basis_points: int = Field(ge=0, le=10_000)
    p95_duration_micros: int = Field(ge=0)
    baseline_p95_duration_micros: int = Field(ge=0)
    p95_latency_regression_basis_points: int = Field(ge=0, le=10_000)
    max_p95_latency_regression_basis_points: int = Field(ge=0, le=10_000)
    mean_reported_cost_microusd: int = Field(ge=0)
    baseline_mean_cost_microusd: int = Field(ge=0)
    baseline_cost_source: EvolutionRevalidationRolloutCostSource
    cost_comparable: bool
    cost_regression_basis_points: int = Field(ge=0, le=10_000)
    max_cost_regression_basis_points: int = Field(ge=0, le=10_000)
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=8)
    status: EvolutionRevalidationOptInStageCompletionStatus
    assessed_at: str = Field(min_length=1, max_length=100)
    liveness_verified: Literal[True] = True
    outcomes_revalidated: Literal[True] = True
    reported_cost_source_authority: Literal[True] = True
    billing_authority: Literal[False] = False
    opt_in_stage_completion_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    next_stage_entry_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Opt-in Stage Completion workspace 必须 canonical。")
        if not (
            len(self.outcome_ids)
            == len(self.outcome_sha256)
            == len(self.run_ids)
            and len(set(self.outcome_ids)) == len(self.outcome_ids)
            and len(set(self.run_ids)) == len(self.run_ids)
        ):
            raise ValueError("Opt-in Stage Completion outcome source projection 不一致。")
        authoritative = set(self.authoritative_outcome_ids)
        successful = set(self.successful_outcome_ids)
        if not (
            len(authoritative) == len(self.authoritative_outcome_ids)
            and len(successful) == len(self.successful_outcome_ids)
            and authoritative.issubset(self.outcome_ids)
            and successful.issubset(authoritative)
            and self.observed_runs == len(self.authoritative_outcome_ids)
            and self.successful_runs == len(self.successful_outcome_ids)
            and self.unsuccessful_runs == self.observed_runs - self.successful_runs
            and len(self.duration_micros) == self.observed_runs
            and len(self.reported_cost_microusd) == self.observed_runs
        ):
            raise ValueError("Opt-in Stage Completion current outcome projection 不一致。")
        if not (
            self.error_rate_basis_points
            == _basis_points(self.unsuccessful_runs, self.observed_runs)
            and self.completion_rate_basis_points
            == _basis_points(self.successful_runs, self.observed_runs)
            and self.completion_rate_drop_basis_points
            == max(
                0,
                self.baseline_completion_rate_basis_points
                - self.completion_rate_basis_points,
            )
            and self.p95_duration_micros == _p95(self.duration_micros)
            and self.p95_latency_regression_basis_points
            == _regression(
                self.p95_duration_micros,
                self.baseline_p95_duration_micros,
            )
            and self.mean_reported_cost_microusd
            == _rounded_div(sum(self.reported_cost_microusd), self.observed_runs)
            and self.cost_regression_basis_points
            == (
                _regression(
                    self.mean_reported_cost_microusd,
                    self.baseline_mean_cost_microusd,
                )
                if self.cost_comparable
                else 0
            )
            and self.cost_comparable
            is (self.baseline_cost_source is EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE)
        ):
            raise ValueError("Opt-in Stage Completion metric projection 不一致。")
        expected_insufficient, expected_breaches = _reasons(
            observed_runs=self.observed_runs,
            minimum_completed_runs=self.minimum_completed_runs,
            cost_comparable=self.cost_comparable,
            error_rate_basis_points=self.error_rate_basis_points,
            max_error_rate_basis_points=self.max_error_rate_basis_points,
            completion_rate_drop_basis_points=self.completion_rate_drop_basis_points,
            max_completion_rate_drop_basis_points=(
                self.max_completion_rate_drop_basis_points
            ),
            p95_latency_regression_basis_points=(
                self.p95_latency_regression_basis_points
            ),
            max_p95_latency_regression_basis_points=(
                self.max_p95_latency_regression_basis_points
            ),
            cost_regression_basis_points=self.cost_regression_basis_points,
            max_cost_regression_basis_points=self.max_cost_regression_basis_points,
        )
        expected_status = _status(expected_insufficient, expected_breaches)
        if not (
            self.insufficient_reasons
            == (() if expected_breaches else expected_insufficient)
            and self.breach_reasons == expected_breaches
            and self.status is expected_status
            and self.opt_in_stage_completion_authority
            is (expected_status is EvolutionRevalidationOptInStageCompletionStatus.PASSING)
            and self.pause_input_authority
            is (expected_status is EvolutionRevalidationOptInStageCompletionStatus.BREACHED)
            and self.rollback_input_authority is self.pause_input_authority
        ):
            raise ValueError("Opt-in Stage Completion status/authority projection 不一致。")
        if datetime.fromisoformat(self.assessed_at).utcoffset() is None:
            raise ValueError("Opt-in Stage Completion assessed_at 必须包含 offset。")
        expected_source_set = _source_set_digest(
            plan_id=self.plan_id,
            plan_sha256=self.plan_sha256,
            baseline_id=self.baseline_id,
            baseline_sha256=self.baseline_sha256,
            liveness_window_id=self.liveness_window_id,
            liveness_window_sha256=self.liveness_window_sha256,
            outcome_ids=self.outcome_ids,
            outcome_sha256=self.outcome_sha256,
            authoritative_outcome_ids=self.authoritative_outcome_ids,
            successful_outcome_ids=self.successful_outcome_ids,
        )
        if self.source_set_sha256 != expected_source_set:
            raise ValueError("Opt-in Stage Completion source set digest 不一致。")
        core = self.model_dump(mode="json", exclude={"evidence_id", "evidence_sha256"})
        digest = _digest(core)
        if self.evidence_sha256 != digest or self.evidence_id != (
            f"evreoptincomplete_{digest[:24]}"
        ):
            raise ValueError("Opt-in Stage Completion identity 不一致。")
        return self


class EvolutionRevalidationOptInStageCompletionView(_StrictModel):
    receipt: EvolutionRevalidationOptInStageCompletion
    evidence_source_current: bool
    latest_assessment: bool
    plan_source_current: bool
    baseline_source_current: bool
    liveness_source_current: bool
    outcome_set_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    opt_in_stage_completion_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    next_stage_entry_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.evidence_source_current
            and self.latest_assessment
            and self.plan_source_current
            and self.baseline_source_current
            and self.liveness_source_current
            and self.outcome_set_current
        )
        if not (
            self.opt_in_stage_completion_authority
            is (
                current
                and self.receipt.status
                is EvolutionRevalidationOptInStageCompletionStatus.PASSING
            )
            and self.pause_input_authority
            is (
                current
                and self.receipt.status
                is EvolutionRevalidationOptInStageCompletionStatus.BREACHED
            )
            and self.rollback_input_authority is self.pause_input_authority
            and tuple(sorted(set(self.invalidation_reasons)))
            == self.invalidation_reasons
        ):
            raise ValueError("Opt-in Stage Completion View authority projection 不一致。")
        return self


class EvolutionRevalidationOptInStageCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOptInStageCompletionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        outcome_service: EvolutionRevalidationOptInExecutionOutcomeLedgerService,
        window_service: EvolutionRevalidationOptInObservationWindowService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        baseline_service: EvolutionRevalidationRolloutBaselineService,
    ) -> None:
        if not isinstance(
            outcome_service,
            EvolutionRevalidationOptInExecutionOutcomeLedgerService,
        ):
            raise TypeError("Opt-in Stage Completion Store 需要 Outcome Service。")
        if not isinstance(window_service, EvolutionRevalidationOptInObservationWindowService):
            raise TypeError("Opt-in Stage Completion Store 需要 Window Service。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Opt-in Stage Completion Store 需要 Plan Service。")
        if not isinstance(baseline_service, EvolutionRevalidationRolloutBaselineService):
            raise TypeError("Opt-in Stage Completion Store 需要 Baseline Service。")
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            self.db_path == outcome_service.store.db_path
            == window_service.store.db_path
            == plan_service.store.db_path
            == baseline_service.store.db_path
        ):
            raise ValueError("Opt-in Stage Completion 必须共用同一证据数据库。")
        self.outcome_service = outcome_service
        self.outcome_store = outcome_service.store
        self.window_service = window_service
        self.plan_service = plan_service
        self.baseline_service = baseline_service

    async def get(
        self,
        evidence_id: str,
    ) -> EvolutionRevalidationOptInStageCompletion | None:
        return await self._read(
            "WHERE evidence_id = ?",
            (evidence_id,),
        )

    async def latest(
        self,
        completion_id: str,
    ) -> EvolutionRevalidationOptInStageCompletion | None:
        return await self._read(
            "WHERE completion_id = ? ORDER BY rowid DESC LIMIT 1",
            (completion_id,),
        )

    async def _read(
        self,
        where: str,
        parameters: tuple[object, ...],
    ) -> EvolutionRevalidationOptInStageCompletion | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_opt_in_stage_completions " + where,
                        parameters,
                    )
                ).fetchone()
            return None if row is None else _restore(row["evidence_json"])
        except EvolutionRevalidationOptInStageCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_source_unavailable",
                "Opt-in Stage Completion durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionRevalidationOptInStageCompletion,
    ) -> EvolutionRevalidationOptInStageCompletion:
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_oversized",
                "Opt-in Stage Completion 超过 512 KiB。",
            )
        await self._require_live_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await self._require_sources(db, item)
                same_source = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_opt_in_stage_completions "
                        "WHERE completion_id = ? AND source_set_sha256 = ?",
                        (item.completion_id, item.source_set_sha256),
                    )
                ).fetchone()
                if same_source is not None:
                    restored = _restore(same_source["evidence_json"])
                    await db.rollback()
                    return restored
                existing = await (
                    await db.execute(
                        "SELECT evidence_json FROM "
                        "evolution_revalidation_opt_in_stage_completions "
                        "WHERE evidence_id = ?",
                        (item.evidence_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["evidence_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationOptInStageCompletionError(
                            "opt_in_stage_completion_identity_conflict",
                            "同一 Opt-in Stage Completion ID 已绑定不同内容。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_stage_completions "
                    "(evidence_id, evidence_sha256, completion_id, status, "
                    "source_set_sha256, evidence_json, assessed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.evidence_id,
                        item.evidence_sha256,
                        item.completion_id,
                        item.status.value,
                        item.source_set_sha256,
                        encoded,
                        item.assessed_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationOptInStageCompletionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_store_error",
                "Opt-in Stage Completion 无法持久化。",
            ) from exc
        return item

    async def _require_live_sources(
        self,
        item: EvolutionRevalidationOptInStageCompletion,
    ) -> None:
        plan_view = await self.plan_service.inspect(plan_id=item.plan_id)
        baseline_view = await self.baseline_service.inspect(plan_id=item.plan_id)
        window_view = await self.window_service.inspect(
            completion_id=item.completion_id,
            subject_id=item.subject_id,
        )
        plan = plan_view.plan
        stage = plan.stages[1]
        baseline = baseline_view.baseline
        window = window_view.receipt
        if not (
            plan_view.current_rollout_eligible
            and plan.plan_sha256 == item.plan_sha256
            and stage.name is EvolutionRevalidationRolloutStageName.OPT_IN
            and baseline_view.monitor_input_ready
            and baseline.baseline_id == item.baseline_id
            and baseline.baseline_sha256 == item.baseline_sha256
            and window_view.runtime_liveness_window_authority
            and window.window_id == item.liveness_window_id
            and window.window_sha256 == item.liveness_window_sha256
            and window.binding.subject_id == item.subject_id
            and stage.minimum_completed_runs == item.minimum_completed_runs
            and stage.max_error_rate_basis_points
            == item.max_error_rate_basis_points
            and stage.max_completion_rate_drop_basis_points
            == item.max_completion_rate_drop_basis_points
            and stage.max_p95_latency_regression_basis_points
            == item.max_p95_latency_regression_basis_points
            and stage.max_cost_regression_basis_points
            == item.max_cost_regression_basis_points
            and baseline.completion_rate_basis_points
            == item.baseline_completion_rate_basis_points
            and baseline.p95_duration_micros
            == item.baseline_p95_duration_micros
            and baseline.mean_cost_microusd == item.baseline_mean_cost_microusd
            and baseline.cost_source is item.baseline_cost_source
        ):
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_live_source_changed",
                "Opt-in Stage Completion 的 current Plan/Baseline/Window 已变化。",
            )
        outcomes = await self.outcome_store.list_for_completion(
            item.completion_id,
            limit=_MAX_OUTCOMES,
        )
        if (
            tuple(outcome.outcome_id for outcome in outcomes) != item.outcome_ids
            or tuple(outcome.outcome_sha256 for outcome in outcomes)
            != item.outcome_sha256
            or tuple(outcome.run_id for outcome in outcomes) != item.run_ids
        ):
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_outcome_set_changed",
                "Opt-in Stage Completion 的 Outcome source set 已变化。",
            )
        authoritative = []
        successful = []
        for outcome in outcomes:
            view = await self.outcome_service.inspect(outcome_id=outcome.outcome_id)
            if view.execution_outcome_authority:
                authoritative.append(outcome)
                if view.successful_completed_run_authority:
                    successful.append(outcome.outcome_id)
        if not (
            tuple(outcome.outcome_id for outcome in authoritative)
            == item.authoritative_outcome_ids
            and tuple(successful) == item.successful_outcome_ids
            and tuple(outcome.duration_ms * 1_000 for outcome in authoritative)
            == item.duration_micros
            and tuple(
                _reported_microusd(outcome.usage.reported_cost_usd)
                for outcome in authoritative
            )
            == item.reported_cost_microusd
        ):
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_outcome_authority_changed",
                "Opt-in Stage Completion 的 Outcome authority 已变化。",
            )

    async def _require_sources(
        self,
        db: aiosqlite.Connection,
        item: EvolutionRevalidationOptInStageCompletion,
    ) -> None:
        plan = await (
            await db.execute(
                "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans "
                "WHERE plan_id = ?",
                (item.plan_id,),
            )
        ).fetchone()
        baseline = await (
            await db.execute(
                "SELECT baseline_sha256 FROM evolution_revalidation_rollout_baselines "
                "WHERE baseline_id = ?",
                (item.baseline_id,),
            )
        ).fetchone()
        window = await (
            await db.execute(
                "SELECT window_sha256 FROM "
                "evolution_revalidation_opt_in_observation_windows "
                "WHERE window_id = ?",
                (item.liveness_window_id,),
            )
        ).fetchone()
        outcomes = await (
            await db.execute(
                "SELECT outcome_id, outcome_sha256 FROM "
                "evolution_revalidation_opt_in_execution_outcomes "
                "WHERE completion_id = ? "
                "ORDER BY execution_completed_at, outcome_id LIMIT ?",
                (item.completion_id, _MAX_OUTCOMES + 1),
            )
        ).fetchall()
        projection = (
            tuple(row["outcome_id"] for row in outcomes),
            tuple(row["outcome_sha256"] for row in outcomes),
        )
        if not (
            plan is not None
            and plan["plan_sha256"] == item.plan_sha256
            and baseline is not None
            and baseline["baseline_sha256"] == item.baseline_sha256
            and window is not None
            and window["window_sha256"] == item.liveness_window_sha256
            and len(outcomes) <= _MAX_OUTCOMES
            and projection == (item.outcome_ids, item.outcome_sha256)
        ):
            await db.rollback()
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_dependency_changed",
                "Opt-in Stage Completion 的 Plan/Baseline/Window/Outcome 已变化。",
            )


class EvolutionRevalidationOptInStageCompletionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationOptInExecutionOutcomeLedgerService,
        window_service: EvolutionRevalidationOptInObservationWindowService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        baseline_service: EvolutionRevalidationRolloutBaselineService,
        store: EvolutionRevalidationOptInStageCompletionStore,
        clock=None,
    ) -> None:
        if not isinstance(
            outcome_service,
            EvolutionRevalidationOptInExecutionOutcomeLedgerService,
        ):
            raise TypeError("Opt-in Stage Completion 需要 Outcome Service。")
        if not isinstance(window_service, EvolutionRevalidationOptInObservationWindowService):
            raise TypeError("Opt-in Stage Completion 需要 Window Service。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Opt-in Stage Completion 需要 Rollout Plan Service。")
        if not isinstance(baseline_service, EvolutionRevalidationRolloutBaselineService):
            raise TypeError("Opt-in Stage Completion 需要 Rollout Baseline Service。")
        if not isinstance(store, EvolutionRevalidationOptInStageCompletionStore):
            raise TypeError("Opt-in Stage Completion 需要 Stage Completion Store。")
        if not (
            outcome_service is store.outcome_service
            and outcome_service.window_service is window_service
            and store.window_service is window_service
            and store.plan_service is plan_service
            and store.baseline_service is baseline_service
            and store.db_path == baseline_service.store.db_path
            and store.db_path == plan_service.store.db_path
        ):
            raise ValueError("Opt-in Stage Completion 的 durable source 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            self.workspace_root == outcome_service.workspace_root
            == window_service.workspace_root
            == plan_service.workspace_root
            == baseline_service.workspace_root
        ):
            raise ValueError("Opt-in Stage Completion workspace 必须一致。")
        self.outcome_service = outcome_service
        self.window_service = window_service
        self.plan_service = plan_service
        self.baseline_service = baseline_service
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def assess(
        self,
        *,
        completion_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInStageCompletionView:
        lock = self._locks.setdefault(completion_id, asyncio.Lock())
        async with lock:
            snapshot = await self._snapshot(
                completion_id=completion_id,
                subject_id=subject_id,
            )
            latest = await self.store.latest(completion_id)
            if latest is not None and _snapshot_signature(latest) == snapshot:
                return await self._view(latest, subject_id=subject_id)
            receipt = _build_receipt(
                snapshot=snapshot,
                assessed_at=_aware(self.clock()).isoformat(),
            )
            stored = await self.store.record(receipt)
            return await self._view(stored, subject_id=subject_id)

    async def inspect(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInStageCompletionView:
        receipt = await self.store.get(evidence_id)
        if receipt is None:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_missing",
                "指定的 Opt-in Stage Completion 不存在。",
            )
        return await self._view(receipt, subject_id=subject_id)

    async def _view(
        self,
        receipt: EvolutionRevalidationOptInStageCompletion,
        *,
        subject_id: str,
    ) -> EvolutionRevalidationOptInStageCompletionView:
        reasons: list[str] = []
        try:
            source = await self.store.get(receipt.evidence_id)
            evidence_source_current = source == receipt
        except EvolutionRevalidationOptInStageCompletionError:
            evidence_source_current = False
        if not evidence_source_current:
            reasons.append("evidence_source_changed")
        try:
            latest = await self.store.latest(receipt.completion_id)
            latest_assessment = latest == receipt
        except EvolutionRevalidationOptInStageCompletionError:
            latest_assessment = False
        if not latest_assessment:
            reasons.append("newer_assessment_exists")
        plan_source_current = False
        baseline_source_current = False
        liveness_source_current = False
        outcome_set_current = False
        try:
            plan_view = await self.plan_service.inspect(plan_id=receipt.plan_id)
            plan_source_current = bool(
                plan_view.current_rollout_eligible
                and plan_view.plan.plan_sha256 == receipt.plan_sha256
            )
        except (EvolutionRevalidationRolloutPlanError, OSError, TypeError, ValueError):
            pass
        if not plan_source_current:
            reasons.append("plan_source_changed")
        try:
            baseline_view = await self.baseline_service.inspect(plan_id=receipt.plan_id)
            baseline_source_current = bool(
                baseline_view.monitor_input_ready
                and baseline_view.baseline.baseline_id == receipt.baseline_id
                and baseline_view.baseline.baseline_sha256 == receipt.baseline_sha256
            )
        except (EvolutionRevalidationRolloutBaselineError, OSError, TypeError, ValueError):
            pass
        if not baseline_source_current:
            reasons.append("baseline_source_changed")
        try:
            window_view = await self.window_service.inspect(
                completion_id=receipt.completion_id,
                subject_id=subject_id,
            )
            liveness_source_current = bool(
                window_view.runtime_liveness_window_authority
                and window_view.receipt.window_id == receipt.liveness_window_id
                and window_view.receipt.window_sha256 == receipt.liveness_window_sha256
            )
        except (
            EvolutionRevalidationOptInObservationAssessmentError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        if not liveness_source_current:
            reasons.append("liveness_source_changed")
        try:
            current = await self._snapshot(
                completion_id=receipt.completion_id,
                subject_id=subject_id,
            )
            outcome_set_current = current == _snapshot_signature(receipt)
        except (
            EvolutionRevalidationOptInStageCompletionError,
            EvolutionRevalidationOptInObservationAssessmentError,
            EvolutionRevalidationRolloutBaselineError,
            EvolutionRevalidationRolloutPlanError,
            OSError,
            TypeError,
            ValueError,
        ):
            outcome_set_current = False
        if not outcome_set_current:
            reasons.append("outcome_set_changed")
        current_authority = bool(
            evidence_source_current
            and latest_assessment
            and plan_source_current
            and baseline_source_current
            and liveness_source_current
            and outcome_set_current
        )
        passing = (
            receipt.status is EvolutionRevalidationOptInStageCompletionStatus.PASSING
        )
        breached = (
            receipt.status is EvolutionRevalidationOptInStageCompletionStatus.BREACHED
        )
        return EvolutionRevalidationOptInStageCompletionView(
            receipt=receipt,
            evidence_source_current=evidence_source_current,
            latest_assessment=latest_assessment,
            plan_source_current=plan_source_current,
            baseline_source_current=baseline_source_current,
            liveness_source_current=liveness_source_current,
            outcome_set_current=outcome_set_current,
            invalidation_reasons=tuple(sorted(set(reasons))),
            opt_in_stage_completion_authority=current_authority and passing,
            pause_input_authority=current_authority and breached,
            rollback_input_authority=current_authority and breached,
        )

    async def _snapshot(self, *, completion_id: str, subject_id: str) -> dict:
        try:
            window_view = await self.window_service.inspect(
                completion_id=completion_id,
                subject_id=subject_id,
            )
        except EvolutionRevalidationOptInObservationAssessmentError as exc:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_liveness_unavailable",
                "缺少当前可用的 Opt-in liveness window。",
            ) from exc
        if not window_view.runtime_liveness_window_authority:
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_liveness_not_authoritative",
                "当前 Opt-in liveness window 不具备 Stage Completion 准入权限。",
            )
        window = window_view.receipt
        cohort = window.runtime_health.deployment.intent.cohort
        plan_view = await self.plan_service.inspect(plan_id=cohort.plan_id)
        if not (
            plan_view.current_rollout_eligible
            and plan_view.plan.plan_sha256 == cohort.plan_sha256
            and plan_view.plan.stages[1] == cohort.stage
            and cohort.stage.name is EvolutionRevalidationRolloutStageName.OPT_IN
        ):
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_plan_stale",
                "Opt-in Rollout Plan 或冻结 Stage 已失效。",
            )
        baseline_view = await self.baseline_service.inspect(plan_id=cohort.plan_id)
        baseline = baseline_view.baseline
        if not (
            baseline_view.monitor_input_ready
            and baseline.plan_sha256 == cohort.plan_sha256
            and baseline.workspace_root == str(self.workspace_root)
        ):
            raise EvolutionRevalidationOptInStageCompletionError(
                "opt_in_stage_completion_baseline_stale",
                "Opt-in Stage Completion 的 GREEN Baseline 已失效。",
            )
        outcomes = await self.store.outcome_store.list_for_completion(
            completion_id,
            limit=_MAX_OUTCOMES,
        )
        if len(outcomes) == _MAX_OUTCOMES:
            count = await _outcome_count(self.store.db_path, completion_id)
            if count > _MAX_OUTCOMES:
                raise EvolutionRevalidationOptInStageCompletionError(
                    "opt_in_stage_completion_outcome_limit_exceeded",
                    "Opt-in Stage Completion outcome 超过 100 条证据上限。",
                )
        authoritative = []
        successful_ids = []
        for outcome in outcomes:
            source = outcome.liveness_source
            if not (
                source.completion_id == completion_id
                and outcome.workspace_root == str(self.workspace_root)
                and source.binding_id == window.binding.binding_id
                and source.binding_sha256 == window.binding.binding_sha256
                and source.candidate_version == cohort.candidate_version
                and source.candidate_target == cohort.candidate_target
            ):
                raise EvolutionRevalidationOptInStageCompletionError(
                    "opt_in_stage_completion_outcome_scope_mismatch",
                    "Execution Outcome 与当前 completion/release scope 不一致。",
                )
            view = await self.outcome_service.inspect(outcome_id=outcome.outcome_id)
            if view.execution_outcome_authority:
                authoritative.append(outcome)
                if view.successful_completed_run_authority:
                    successful_ids.append(outcome.outcome_id)
        stage = cohort.stage
        authoritative_ids = tuple(item.outcome_id for item in authoritative)
        durations = tuple(item.duration_ms * 1_000 for item in authoritative)
        costs = tuple(_reported_microusd(item.usage.reported_cost_usd) for item in authoritative)
        observed = len(authoritative)
        successful = len(successful_ids)
        unsuccessful = observed - successful
        error_rate = _basis_points(unsuccessful, observed)
        completion_rate = _basis_points(successful, observed)
        completion_drop = max(
            0,
            baseline.completion_rate_basis_points - completion_rate,
        )
        p95 = _p95(durations)
        latency_regression = _regression(p95, baseline.p95_duration_micros)
        mean_cost = _rounded_div(sum(costs), observed)
        cost_comparable = (
            baseline.cost_source is EvolutionRevalidationRolloutCostSource.LIVE_EVIDENCE
        )
        cost_regression = (
            _regression(mean_cost, baseline.mean_cost_microusd)
            if cost_comparable
            else 0
        )
        insufficient, breaches = _reasons(
            observed_runs=observed,
            minimum_completed_runs=stage.minimum_completed_runs,
            cost_comparable=cost_comparable,
            error_rate_basis_points=error_rate,
            max_error_rate_basis_points=stage.max_error_rate_basis_points,
            completion_rate_drop_basis_points=completion_drop,
            max_completion_rate_drop_basis_points=(
                stage.max_completion_rate_drop_basis_points
            ),
            p95_latency_regression_basis_points=latency_regression,
            max_p95_latency_regression_basis_points=(
                stage.max_p95_latency_regression_basis_points
            ),
            cost_regression_basis_points=cost_regression,
            max_cost_regression_basis_points=stage.max_cost_regression_basis_points,
        )
        status = _status(insufficient, breaches)
        return {
            "workspace_root": str(self.workspace_root),
            "completion_id": completion_id,
            "subject_id": subject_id,
            "plan_id": plan_view.plan.plan_id,
            "plan_sha256": plan_view.plan.plan_sha256,
            "baseline_id": baseline.baseline_id,
            "baseline_sha256": baseline.baseline_sha256,
            "liveness_window_id": window.window_id,
            "liveness_window_sha256": window.window_sha256,
            "outcome_ids": tuple(item.outcome_id for item in outcomes),
            "outcome_sha256": tuple(item.outcome_sha256 for item in outcomes),
            "run_ids": tuple(item.run_id for item in outcomes),
            "authoritative_outcome_ids": authoritative_ids,
            "successful_outcome_ids": tuple(successful_ids),
            "duration_micros": durations,
            "reported_cost_microusd": costs,
            "observed_runs": observed,
            "minimum_completed_runs": stage.minimum_completed_runs,
            "successful_runs": successful,
            "unsuccessful_runs": unsuccessful,
            "error_rate_basis_points": error_rate,
            "max_error_rate_basis_points": stage.max_error_rate_basis_points,
            "completion_rate_basis_points": completion_rate,
            "baseline_completion_rate_basis_points": (
                baseline.completion_rate_basis_points
            ),
            "completion_rate_drop_basis_points": completion_drop,
            "max_completion_rate_drop_basis_points": (
                stage.max_completion_rate_drop_basis_points
            ),
            "p95_duration_micros": p95,
            "baseline_p95_duration_micros": baseline.p95_duration_micros,
            "p95_latency_regression_basis_points": latency_regression,
            "max_p95_latency_regression_basis_points": (
                stage.max_p95_latency_regression_basis_points
            ),
            "mean_reported_cost_microusd": mean_cost,
            "baseline_mean_cost_microusd": baseline.mean_cost_microusd,
            "baseline_cost_source": baseline.cost_source,
            "cost_comparable": cost_comparable,
            "cost_regression_basis_points": cost_regression,
            "max_cost_regression_basis_points": stage.max_cost_regression_basis_points,
            "insufficient_reasons": () if breaches else insufficient,
            "breach_reasons": breaches,
            "status": status,
        }


def _build_receipt(
    *,
    snapshot: dict,
    assessed_at: str,
) -> EvolutionRevalidationOptInStageCompletion:
    source_set_sha256 = _source_set_digest(
        plan_id=snapshot["plan_id"],
        plan_sha256=snapshot["plan_sha256"],
        baseline_id=snapshot["baseline_id"],
        baseline_sha256=snapshot["baseline_sha256"],
        liveness_window_id=snapshot["liveness_window_id"],
        liveness_window_sha256=snapshot["liveness_window_sha256"],
        outcome_ids=snapshot["outcome_ids"],
        outcome_sha256=snapshot["outcome_sha256"],
        authoritative_outcome_ids=snapshot["authoritative_outcome_ids"],
        successful_outcome_ids=snapshot["successful_outcome_ids"],
    )
    status = snapshot["status"]
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_OPT_IN_STAGE_COMPLETION_POLICY,
        "source_set_sha256": source_set_sha256,
        **snapshot,
        "stage_order": 2,
        "completed_stage": "opt_in",
        "next_stage": "percentage",
        "assessed_at": _aware(assessed_at).isoformat(),
        "liveness_verified": True,
        "outcomes_revalidated": True,
        "reported_cost_source_authority": True,
        "billing_authority": False,
        "opt_in_stage_completion_authority": (
            status is EvolutionRevalidationOptInStageCompletionStatus.PASSING
        ),
        "pause_input_authority": (
            status is EvolutionRevalidationOptInStageCompletionStatus.BREACHED
        ),
        "rollback_input_authority": (
            status is EvolutionRevalidationOptInStageCompletionStatus.BREACHED
        ),
        "next_stage_entry_authority": False,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationOptInStageCompletion.model_validate(
        {
            **core,
            "evidence_id": f"evreoptincomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


def _snapshot_signature(receipt: EvolutionRevalidationOptInStageCompletion) -> dict:
    return receipt.model_dump(
        mode="python",
        include={
            "workspace_root",
            "completion_id",
            "subject_id",
            "plan_id",
            "plan_sha256",
            "baseline_id",
            "baseline_sha256",
            "liveness_window_id",
            "liveness_window_sha256",
            "outcome_ids",
            "outcome_sha256",
            "run_ids",
            "authoritative_outcome_ids",
            "successful_outcome_ids",
            "duration_micros",
            "reported_cost_microusd",
            "observed_runs",
            "minimum_completed_runs",
            "successful_runs",
            "unsuccessful_runs",
            "error_rate_basis_points",
            "max_error_rate_basis_points",
            "completion_rate_basis_points",
            "baseline_completion_rate_basis_points",
            "completion_rate_drop_basis_points",
            "max_completion_rate_drop_basis_points",
            "p95_duration_micros",
            "baseline_p95_duration_micros",
            "p95_latency_regression_basis_points",
            "max_p95_latency_regression_basis_points",
            "mean_reported_cost_microusd",
            "baseline_mean_cost_microusd",
            "baseline_cost_source",
            "cost_comparable",
            "cost_regression_basis_points",
            "max_cost_regression_basis_points",
            "insufficient_reasons",
            "breach_reasons",
            "status",
        },
    )


def _source_set_digest(**payload) -> str:
    return _digest(payload)


def _reasons(
    *,
    observed_runs: int,
    minimum_completed_runs: int,
    cost_comparable: bool,
    error_rate_basis_points: int,
    max_error_rate_basis_points: int,
    completion_rate_drop_basis_points: int,
    max_completion_rate_drop_basis_points: int,
    p95_latency_regression_basis_points: int,
    max_p95_latency_regression_basis_points: int,
    cost_regression_basis_points: int,
    max_cost_regression_basis_points: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    insufficient = tuple(
        reason
        for condition, reason in (
            (observed_runs < minimum_completed_runs, "minimum_completed_runs"),
            (not cost_comparable, "cost_baseline_not_comparable"),
        )
        if condition
    )
    breaches = tuple(
        reason
        for condition, reason in (
            (
                observed_runs > 0
                and error_rate_basis_points > max_error_rate_basis_points,
                "error_rate",
            ),
            (
                observed_runs > 0
                and completion_rate_drop_basis_points
                > max_completion_rate_drop_basis_points,
                "completion_rate_drop",
            ),
            (
                observed_runs > 0
                and p95_latency_regression_basis_points
                > max_p95_latency_regression_basis_points,
                "p95_latency_regression",
            ),
            (
                observed_runs > 0
                and cost_comparable
                and cost_regression_basis_points > max_cost_regression_basis_points,
                "cost_regression",
            ),
        )
        if condition
    )
    return insufficient, breaches


def _status(
    insufficient: tuple[str, ...],
    breaches: tuple[str, ...],
) -> EvolutionRevalidationOptInStageCompletionStatus:
    if breaches:
        return EvolutionRevalidationOptInStageCompletionStatus.BREACHED
    if insufficient:
        return EvolutionRevalidationOptInStageCompletionStatus.INSUFFICIENT
    return EvolutionRevalidationOptInStageCompletionStatus.PASSING


def _basis_points(numerator: int, denominator: int) -> int:
    return (
        0
        if denominator == 0
        else (numerator * 10_000 + denominator // 2) // denominator
    )


def _p95(values: tuple[int, ...]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _regression(current: int, baseline: int) -> int:
    if current <= baseline:
        return 0
    if baseline == 0:
        return 10_000
    return min(10_000, ((current - baseline) * 10_000 + baseline // 2) // baseline)


def _rounded_div(total: int, count: int) -> int:
    return 0 if count == 0 else (total + count // 2) // count


def _reported_microusd(value) -> int:
    scaled = value * 1_000_000
    return int(scaled.to_integral_value())


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Opt-in Stage Completion timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.value if isinstance(value, StrEnum) else str(value),
        ).encode()
    ).hexdigest()


def _validated(
    value: EvolutionRevalidationOptInStageCompletion,
) -> EvolutionRevalidationOptInStageCompletion:
    try:
        return EvolutionRevalidationOptInStageCompletion.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInStageCompletionError(
            "opt_in_stage_completion_invalid",
            "Opt-in Stage Completion artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationOptInStageCompletion:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationOptInStageCompletionError(
            "opt_in_stage_completion_source_oversized",
            "Opt-in Stage Completion durable source 超过上限。",
        )
    try:
        return EvolutionRevalidationOptInStageCompletion.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInStageCompletionError(
            "opt_in_stage_completion_source_invalid",
            "Opt-in Stage Completion durable source 无效。",
        ) from exc


async def _outcome_count(db_path: Path, completion_id: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM "
                "evolution_revalidation_opt_in_execution_outcomes "
                "WHERE completion_id = ?",
                (completion_id,),
            )
        ).fetchone()
    return 0 if row is None else int(row[0])


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_stage_completions (
            evidence_id TEXT PRIMARY KEY,
            evidence_sha256 TEXT NOT NULL,
            completion_id TEXT NOT NULL,
            status TEXT NOT NULL,
            source_set_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            assessed_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_opt_in_stage_completions_latest "
        "ON evolution_revalidation_opt_in_stage_completions "
        "(completion_id, assessed_at DESC, evidence_id DESC)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_opt_in_stage_completions_source "
        "ON evolution_revalidation_opt_in_stage_completions "
        "(completion_id, source_set_sha256)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_STAGE_COMPLETION_POLICY",
    "EvolutionRevalidationOptInStageCompletion",
    "EvolutionRevalidationOptInStageCompletionError",
    "EvolutionRevalidationOptInStageCompletionService",
    "EvolutionRevalidationOptInStageCompletionStatus",
    "EvolutionRevalidationOptInStageCompletionStore",
    "EvolutionRevalidationOptInStageCompletionView",
]
