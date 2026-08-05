"""Evaluate local-canary journals against frozen rollout guardrails."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_local_canary_runs import (
    EvolutionRevalidationLocalCanaryJournalStore,
    EvolutionRevalidationLocalCanaryState,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlAction,
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlStore,
    EvolutionRevalidationRolloutStageEntryService,
)

EVOLUTION_REVALIDATION_RUNTIME_OBSERVATION_POLICY = (
    "evolution-revalidation-runtime-observation-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class EvolutionRevalidationRuntimeObservationStatus(StrEnum):
    INSUFFICIENT = "insufficient"
    PASSING = "passing"
    BREACHED = "breached"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRuntimeObservation(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-runtime-observation-v1"] = (
        EVOLUTION_REVALIDATION_RUNTIME_OBSERVATION_POLICY
    )
    observation_id: str = Field(pattern=r"^evreruntimeobs_[0-9a-f]{24}$")
    observation_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    baseline_id: str = Field(pattern=r"^evrerolloutbaseline_[0-9a-f]{24}$")
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    entry_receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    entry_receipt_sha256: str = Field(pattern=_SHA256_RE)
    stage: Literal["local_canary"] = "local_canary"
    terminal_event_ids: tuple[str, ...] = Field(max_length=100)
    terminal_event_sha256: tuple[str, ...] = Field(max_length=100)
    control_signal_event_ids: tuple[str, ...] = Field(max_length=100)
    control_signal_event_sha256: tuple[str, ...] = Field(max_length=100)
    assessed_at: str = Field(min_length=1, max_length=100)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: int = Field(ge=300, le=604_800)
    completed_runs: int = Field(ge=0, le=100)
    minimum_completed_runs: int = Field(ge=10, le=100)
    passed_runs: int = Field(ge=0, le=100)
    failed_runs: int = Field(ge=0, le=100)
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
    mean_cost_microusd: Literal[0] = 0
    baseline_mean_cost_microusd: int = Field(ge=0)
    cost_regression_basis_points: int = Field(ge=0, le=10_000)
    max_cost_regression_basis_points: int = Field(ge=0, le=10_000)
    insufficient_reasons: tuple[str, ...] = Field(max_length=8)
    breach_reasons: tuple[str, ...] = Field(max_length=16)
    status: EvolutionRevalidationRuntimeObservationStatus
    pause_input_authority: bool
    rollback_input_authority: bool
    stage_advance_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Runtime Observation workspace 必须 canonical。")
        if len(self.terminal_event_ids) != len(self.terminal_event_sha256) or len(
            self.control_signal_event_ids
        ) != len(self.control_signal_event_sha256):
            raise ValueError("Runtime Observation evidence projection 不一致。")
        if self.completed_runs != self.passed_runs + self.failed_runs:
            raise ValueError("Runtime Observation run projection 不一致。")
        breached = self.status is EvolutionRevalidationRuntimeObservationStatus.BREACHED
        insufficient = (
            self.status is EvolutionRevalidationRuntimeObservationStatus.INSUFFICIENT
        )
        if not (
            self.pause_input_authority is breached
            and self.rollback_input_authority is breached
            and bool(self.breach_reasons) is breached
            and bool(self.insufficient_reasons) is insufficient
        ):
            raise ValueError("Runtime Observation authority/status 不一致。")
        core = self.model_dump(
            mode="json", exclude={"observation_id", "observation_sha256"}
        )
        digest = _digest(core)
        if self.observation_sha256 != digest or self.observation_id != (
            f"evreruntimeobs_{digest[:24]}"
        ):
            raise ValueError("Runtime Observation identity 不一致。")
        return self


class EvolutionRevalidationRuntimeObservationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRuntimeObservationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def latest(self, entry_receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT observation_json FROM evolution_revalidation_runtime_observations "
                "WHERE entry_receipt_id = ? ORDER BY assessed_at DESC LIMIT 1",
                (entry_receipt_id,),
            )).fetchone()
        return None if row is None else EvolutionRevalidationRuntimeObservation.model_validate_json(
            row["observation_json"]
        )

    async def record(self, observation):
        item = EvolutionRevalidationRuntimeObservation.model_validate_json(
            observation.model_dump_json()
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            entry = await (await db.execute(
                "SELECT receipt_sha256 FROM evolution_revalidation_rollout_stage_entries "
                "WHERE receipt_id = ?", (item.entry_receipt_id,)
            )).fetchone()
            baseline = await (await db.execute(
                "SELECT baseline_sha256 FROM evolution_revalidation_rollout_baselines "
                "WHERE baseline_id = ?", (item.baseline_id,)
            )).fetchone()
            if not (
                entry is not None and entry["receipt_sha256"] == item.entry_receipt_sha256
                and baseline is not None
                and baseline["baseline_sha256"] == item.baseline_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationRuntimeObservationError(
                    "runtime_observation_dependency_mismatch",
                    "Runtime Observation 的 entry/baseline 依赖不一致。",
                )
            existing = await (await db.execute(
                "SELECT observation_json FROM evolution_revalidation_runtime_observations "
                "WHERE observation_id = ?", (item.observation_id,)
            )).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationRuntimeObservation.model_validate_json(
                    existing["observation_json"]
                )
                await db.rollback()
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_runtime_observations "
                "(observation_id, observation_sha256, entry_receipt_id, status, "
                "observation_json, assessed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item.observation_id, item.observation_sha256, item.entry_receipt_id,
                 item.status.value, item.model_dump_json(), item.assessed_at),
            )
            await db.commit()
        return item


class EvolutionRevalidationRuntimeObservationService:
    def __init__(self, *, workspace_root, entry_service, baseline_service,
                 journal_store, control_store, store, now=None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.entry_service: EvolutionRevalidationRolloutStageEntryService = entry_service
        self.baseline_service: EvolutionRevalidationRolloutBaselineService = baseline_service
        self.journal_store: EvolutionRevalidationLocalCanaryJournalStore = journal_store
        self.control_store: EvolutionRevalidationRolloutControlStore = control_store
        self.store: EvolutionRevalidationRuntimeObservationStore = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def assess(self, *, entry_receipt_id: str):
        entry = await self.entry_service.store.get(entry_receipt_id)
        if entry is None:
            raise EvolutionRevalidationRuntimeObservationError(
                "runtime_observation_entry_missing", "Local-canary entry 不存在。"
            )
        baseline_view = await self.baseline_service.inspect(plan_id=entry.plan_id)
        if not baseline_view.monitor_input_ready:
            raise EvolutionRevalidationRuntimeObservationError(
                "runtime_observation_baseline_stale", "Rollout Baseline 已失效。"
            )
        baseline = baseline_view.baseline
        plan_view = await self.entry_service.plan_service.inspect(plan_id=entry.plan_id)
        if not plan_view.current_rollout_eligible or (
            plan_view.plan.plan_sha256 != entry.plan_sha256
        ):
            raise EvolutionRevalidationRuntimeObservationError(
                "runtime_observation_plan_stale", "Rollout Plan 已失效。"
            )
        stage = plan_view.plan.stages[0]
        terminals = await self.journal_store.terminal_for_entry(entry_receipt_id)
        signals = tuple(
            item for item in await self.control_store.history(
                self.workspace_root, after_sequence=entry.control_sequence
            ) if item.action is EvolutionRevalidationRolloutControlAction.PAUSE
        )
        now = _aware(self.now())
        durations = tuple(sum(check.duration_ms for check in item.checks) * 1_000
                          for item in terminals)
        passed = sum(
            item.state is EvolutionRevalidationLocalCanaryState.PASSED
            for item in terminals
        )
        count = len(terminals)
        failed = count - passed
        observation_seconds = 0 if not terminals else min(
            604_800,
            max(0, int((now - _aware(terminals[0].observed_at)).total_seconds())),
        )
        error_rate = _bps(failed, count)
        completion_rate = _bps(passed, count)
        p95 = _p95(durations)
        latency_regression = _regression(p95, baseline.p95_duration_micros)
        completion_drop = max(
            0, baseline.completion_rate_basis_points - completion_rate
        )
        cost_regression = _regression(0, baseline.mean_cost_microusd)
        insufficient = tuple(reason for condition, reason in (
            (count < stage.minimum_completed_runs, "minimum_completed_runs"),
            (observation_seconds < stage.minimum_observation_seconds,
             "minimum_observation_seconds"),
        ) if condition)
        breaches = [
            reason for condition, reason in (
                (count > 0 and error_rate > stage.max_error_rate_basis_points,
                 "error_rate"),
                (count > 0 and latency_regression > stage.max_p95_latency_regression_basis_points,
                 "p95_latency_regression"),
                (count > 0 and completion_drop > stage.max_completion_rate_drop_basis_points,
                 "completion_rate_drop"),
                (count > 0 and cost_regression > stage.max_cost_regression_basis_points,
                 "cost_regression"),
            ) if condition
        ]
        breaches.extend(_signal_reason(item) for item in signals)
        breach_reasons = tuple(sorted(set(breaches)))
        status = (
            EvolutionRevalidationRuntimeObservationStatus.BREACHED
            if breach_reasons
            else EvolutionRevalidationRuntimeObservationStatus.INSUFFICIENT
            if insufficient
            else EvolutionRevalidationRuntimeObservationStatus.PASSING
        )
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_RUNTIME_OBSERVATION_POLICY,
            "workspace_root": str(self.workspace_root),
            "plan_id": plan_view.plan.plan_id,
            "plan_sha256": plan_view.plan.plan_sha256,
            "baseline_id": baseline.baseline_id,
            "baseline_sha256": baseline.baseline_sha256,
            "entry_receipt_id": entry.receipt_id,
            "entry_receipt_sha256": entry.receipt_sha256,
            "stage": "local_canary",
            "terminal_event_ids": [item.event_id for item in terminals],
            "terminal_event_sha256": [item.event_sha256 for item in terminals],
            "control_signal_event_ids": [item.event_id for item in signals],
            "control_signal_event_sha256": [item.event_sha256 for item in signals],
            "assessed_at": now.isoformat(),
            "observation_seconds": observation_seconds,
            "minimum_observation_seconds": stage.minimum_observation_seconds,
            "completed_runs": count,
            "minimum_completed_runs": stage.minimum_completed_runs,
            "passed_runs": passed,
            "failed_runs": failed,
            "error_rate_basis_points": error_rate,
            "max_error_rate_basis_points": stage.max_error_rate_basis_points,
            "completion_rate_basis_points": completion_rate,
            "baseline_completion_rate_basis_points": baseline.completion_rate_basis_points,
            "completion_rate_drop_basis_points": completion_drop,
            "max_completion_rate_drop_basis_points": stage.max_completion_rate_drop_basis_points,
            "p95_duration_micros": p95,
            "baseline_p95_duration_micros": baseline.p95_duration_micros,
            "p95_latency_regression_basis_points": latency_regression,
            "max_p95_latency_regression_basis_points": (
                stage.max_p95_latency_regression_basis_points
            ),
            "mean_cost_microusd": 0,
            "baseline_mean_cost_microusd": baseline.mean_cost_microusd,
            "cost_regression_basis_points": cost_regression,
            "max_cost_regression_basis_points": stage.max_cost_regression_basis_points,
            "insufficient_reasons": list(() if breach_reasons else insufficient),
            "breach_reasons": list(breach_reasons),
            "status": status.value,
            "pause_input_authority": bool(breach_reasons),
            "rollback_input_authority": bool(breach_reasons),
            "stage_advance_authority": False,
            "promotion_authority": False,
        }
        digest = _digest(core)
        return await self.store.record(
            EvolutionRevalidationRuntimeObservation.model_validate({
                **core,
                "observation_id": f"evreruntimeobs_{digest[:24]}",
                "observation_sha256": digest,
            })
        )


def _signal_reason(event):
    if event.actor is EvolutionRevalidationRolloutControlActor.USER:
        return "user_withdrawal"
    if event.reason_code.startswith("security_"):
        return "security_incident"
    if event.reason_code.startswith("data_integrity_"):
        return "data_integrity_incident"
    return "rollout_control_pause"


def _bps(numerator, denominator):
    return 0 if denominator == 0 else (numerator * 10_000 + denominator // 2) // denominator


def _p95(values):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _regression(current, baseline):
    if current <= baseline:
        return 0
    if baseline == 0:
        return 10_000
    return min(10_000, ((current - baseline) * 10_000 + baseline // 2) // baseline)


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Runtime Observation 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_runtime_observations ("
        "observation_id TEXT PRIMARY KEY, observation_sha256 TEXT NOT NULL UNIQUE, "
        "entry_receipt_id TEXT NOT NULL, status TEXT NOT NULL, "
        "observation_json TEXT NOT NULL, assessed_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_RUNTIME_OBSERVATION_POLICY",
    "EvolutionRevalidationRuntimeObservation",
    "EvolutionRevalidationRuntimeObservationError",
    "EvolutionRevalidationRuntimeObservationService",
    "EvolutionRevalidationRuntimeObservationStatus",
    "EvolutionRevalidationRuntimeObservationStore",
]
