"""Freeze exact passing evidence before any rollout stage advancement."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_local_canary_runs import (
    EvolutionRevalidationLocalCanaryJournalStore,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutStageName,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlStore,
    EvolutionRevalidationRolloutStageEntryStore,
)
from naumi_agent.evolution.revalidation_runtime_observations import (
    EvolutionRevalidationRuntimeObservationStatus,
    EvolutionRevalidationRuntimeObservationStore,
)

EVOLUTION_REVALIDATION_ROLLOUT_STAGE_COMPLETION_POLICY = (
    "evolution-revalidation-rollout-stage-completion-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRolloutStageCompletion(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-stage-completion-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_STAGE_COMPLETION_POLICY
    )
    completion_id: str = Field(pattern=r"^evrerolloutcomplete_[0-9a-f]{24}$")
    completion_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    observation_id: str = Field(pattern=r"^evreruntimeobs_[0-9a-f]{24}$")
    observation_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    entry_receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    entry_receipt_sha256: str = Field(pattern=_SHA256_RE)
    completed_stage: Literal["local_canary"] = "local_canary"
    next_stage: Literal["opt_in"] = "opt_in"
    terminal_event_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    terminal_event_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    completed_runs: int = Field(ge=1, le=100)
    minimum_completed_runs: int = Field(ge=1, le=100)
    observation_seconds: int = Field(ge=0, le=604_800)
    minimum_observation_seconds: int = Field(ge=300, le=604_800)
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    manual_advance_required: bool
    passing_observation_verified: Literal[True] = True
    terminal_prefix_current: Literal[True] = True
    control_quiet: Literal[True] = True
    stage_completed: Literal[True] = True
    automatic_advance_eligible: bool
    manual_interaction_required: bool
    next_stage_entry_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollout Stage Completion workspace 必须 canonical。")
        if len(self.terminal_event_ids) != len(self.terminal_event_sha256):
            raise ValueError("Rollout Stage Completion terminal evidence 不完整。")
        if len(set(self.terminal_event_ids)) != len(self.terminal_event_ids):
            raise ValueError("Rollout Stage Completion terminal ID 不得重复。")
        if not (
            self.completed_runs == len(self.terminal_event_ids)
            and self.completed_runs >= self.minimum_completed_runs
            and self.observation_seconds >= self.minimum_observation_seconds
        ):
            raise ValueError("Rollout Stage Completion 门槛投影不一致。")
        if bool(self.control_sequence) is not bool(self.control_event_sha256):
            raise ValueError("Rollout Stage Completion control projection 不一致。")
        if not (
            self.automatic_advance_eligible is (not self.manual_advance_required)
            and self.manual_interaction_required is self.manual_advance_required
        ):
            raise ValueError("Rollout Stage Completion advance projection 不一致。")
        if datetime.fromisoformat(self.completed_at).utcoffset() is None:
            raise ValueError("Rollout Stage Completion completed_at 必须包含 offset。")
        core = self.model_dump(mode="json", exclude={"completion_id", "completion_sha256"})
        digest = _digest(core)
        if self.completion_sha256 != digest or self.completion_id != (
            f"evrerolloutcomplete_{digest[:24]}"
        ):
            raise ValueError("Rollout Stage Completion identity 不一致。")
        return self


class EvolutionRevalidationRolloutStageCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRolloutStageCompletionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_observation(self, observation_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT completion_json FROM "
                    "evolution_revalidation_rollout_stage_completions "
                    "WHERE observation_id = ?",
                    (observation_id,),
                )
            ).fetchone()
        return None if row is None else _restore(row["completion_json"])

    async def record(self, completion):
        item = EvolutionRevalidationRolloutStageCompletion.model_validate_json(
            completion.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutStageCompletionError(
                "rollout_stage_completion_oversized",
                "Rollout Stage Completion 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            observation = await (
                await db.execute(
                    "SELECT observation_sha256, status FROM "
                    "evolution_revalidation_runtime_observations "
                    "WHERE observation_id = ?",
                    (item.observation_id,),
                )
            ).fetchone()
            plan = await (
                await db.execute(
                    "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans "
                    "WHERE plan_id = ?",
                    (item.plan_id,),
                )
            ).fetchone()
            entry = await (
                await db.execute(
                    "SELECT receipt_sha256 FROM "
                    "evolution_revalidation_rollout_stage_entries WHERE receipt_id = ?",
                    (item.entry_receipt_id,),
                )
            ).fetchone()
            latest_control = await (
                await db.execute(
                    "SELECT sequence, event_sha256, state FROM "
                    "evolution_revalidation_rollout_control_events "
                    "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                    (item.workspace_root,),
                )
            ).fetchone()
            observed_control = (
                (0, "", "active")
                if latest_control is None
                else (
                    latest_control["sequence"],
                    latest_control["event_sha256"],
                    latest_control["state"],
                )
            )
            terminal_rows = await (
                await db.execute(
                    "SELECT event_id, event_sha256 FROM "
                    "evolution_revalidation_local_canary_events "
                    "WHERE entry_receipt_id = ? AND terminal = 1 ORDER BY run_index",
                    (item.entry_receipt_id,),
                )
            ).fetchall()
            terminal_projection = (
                tuple(row["event_id"] for row in terminal_rows),
                tuple(row["event_sha256"] for row in terminal_rows),
            )
            if not (
                observation is not None
                and observation["observation_sha256"] == item.observation_sha256
                and observation["status"]
                == EvolutionRevalidationRuntimeObservationStatus.PASSING.value
                and plan is not None
                and plan["plan_sha256"] == item.plan_sha256
                and entry is not None
                and entry["receipt_sha256"] == item.entry_receipt_sha256
                and observed_control == (item.control_sequence, item.control_event_sha256, "active")
                and terminal_projection == (item.terminal_event_ids, item.terminal_event_sha256)
            ):
                await db.rollback()
                raise EvolutionRevalidationRolloutStageCompletionError(
                    "rollout_stage_completion_dependency_changed",
                    "Rollout Stage Completion 的 Observation/Plan/Entry/Control 已变化。",
                )
            existing = await (
                await db.execute(
                    "SELECT completion_json FROM "
                    "evolution_revalidation_rollout_stage_completions "
                    "WHERE observation_id = ?",
                    (item.observation_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore(existing["completion_json"])
                await db.rollback()
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollout_stage_completions "
                "(completion_id, completion_sha256, observation_id, plan_id, "
                "completion_json, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.completion_id,
                    item.completion_sha256,
                    item.observation_id,
                    item.plan_id,
                    encoded,
                    item.completed_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationRolloutStageCompletionService:
    def __init__(
        self,
        *,
        workspace_root,
        observation_store,
        plan_service,
        entry_store,
        journal_store,
        control_store,
        store,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.observation_store: EvolutionRevalidationRuntimeObservationStore = observation_store
        self.plan_service: EvolutionRevalidationRolloutPlanService = plan_service
        self.entry_store: EvolutionRevalidationRolloutStageEntryStore = entry_store
        self.journal_store: EvolutionRevalidationLocalCanaryJournalStore = journal_store
        self.control_store: EvolutionRevalidationRolloutControlStore = control_store
        self.store: EvolutionRevalidationRolloutStageCompletionStore = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def complete(self, *, observation_id: str):
        lock = self._locks.setdefault(observation_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_observation(observation_id)
            if existing is not None:
                await self._require_current(existing)
                return existing
            observation = await self.observation_store.get(observation_id)
            if observation is None or not (
                observation.workspace_root == str(self.workspace_root)
                and observation.status is EvolutionRevalidationRuntimeObservationStatus.PASSING
            ):
                raise EvolutionRevalidationRolloutStageCompletionError(
                    "rollout_stage_completion_observation_not_passing",
                    "只有当前 passing Observation 可以完成 rollout stage。",
                )
            plan_view = await self.plan_service.inspect(plan_id=observation.plan_id)
            entry = await self.entry_store.get(observation.entry_receipt_id)
            if not (
                plan_view.current_rollout_eligible
                and plan_view.plan.plan_sha256 == observation.plan_sha256
                and entry is not None
                and entry.receipt_sha256 == observation.entry_receipt_sha256
                and entry.plan_id == observation.plan_id
            ):
                raise EvolutionRevalidationRolloutStageCompletionError(
                    "rollout_stage_completion_authority_stale",
                    "Rollout Plan 或 Stage Entry 已失效。",
                )
            stage = plan_view.plan.stages[0]
            if not (
                stage.name is EvolutionRevalidationRolloutStageName.LOCAL_CANARY
                and plan_view.plan.stages[1].name is EvolutionRevalidationRolloutStageName.OPT_IN
            ):
                raise EvolutionRevalidationRolloutStageCompletionError(
                    "rollout_stage_completion_transition_invalid",
                    "当前 rollout stage transition 不是 local_canary → opt_in。",
                )
            terminals, control = await self._current_evidence(observation, entry)
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_STAGE_COMPLETION_POLICY,
                "workspace_root": str(self.workspace_root),
                "observation_id": observation.observation_id,
                "observation_sha256": observation.observation_sha256,
                "plan_id": observation.plan_id,
                "plan_sha256": observation.plan_sha256,
                "entry_receipt_id": observation.entry_receipt_id,
                "entry_receipt_sha256": observation.entry_receipt_sha256,
                "completed_stage": "local_canary",
                "next_stage": "opt_in",
                "terminal_event_ids": [item.event_id for item in terminals],
                "terminal_event_sha256": [item.event_sha256 for item in terminals],
                "completed_runs": observation.completed_runs,
                "minimum_completed_runs": observation.minimum_completed_runs,
                "observation_seconds": observation.observation_seconds,
                "minimum_observation_seconds": observation.minimum_observation_seconds,
                "control_sequence": 0 if control is None else control.sequence,
                "control_event_sha256": "" if control is None else control.event_sha256,
                "manual_advance_required": stage.manual_advance_required,
                "passing_observation_verified": True,
                "terminal_prefix_current": True,
                "control_quiet": True,
                "stage_completed": True,
                "automatic_advance_eligible": not stage.manual_advance_required,
                "manual_interaction_required": stage.manual_advance_required,
                "next_stage_entry_authority": False,
                "deployment_authority": False,
                "rollback_authority": False,
                "promotion_authority": False,
                "completed_at": _aware(self.now()).isoformat(),
            }
            digest = _digest(core)
            item = EvolutionRevalidationRolloutStageCompletion.model_validate(
                {
                    **core,
                    "completion_id": f"evrerolloutcomplete_{digest[:24]}",
                    "completion_sha256": digest,
                }
            )
            await self._require_current(item)
            stored = await self.store.record(item)
            await self._require_current(stored)
            return stored

    async def _require_current(self, item) -> None:
        observation = await self.observation_store.get(item.observation_id)
        if observation is None or observation.observation_sha256 != (item.observation_sha256):
            raise EvolutionRevalidationRolloutStageCompletionError(
                "rollout_stage_completion_observation_stale",
                "Rollout Stage Completion 的 Observation 已失效。",
            )
        plan_view = await self.plan_service.inspect(plan_id=item.plan_id)
        entry = await self.entry_store.get(item.entry_receipt_id)
        if not (
            plan_view.current_rollout_eligible
            and plan_view.plan.plan_sha256 == item.plan_sha256
            and entry is not None
            and entry.receipt_sha256 == item.entry_receipt_sha256
        ):
            raise EvolutionRevalidationRolloutStageCompletionError(
                "rollout_stage_completion_authority_stale",
                "Rollout Stage Completion 的 Plan/Entry authority 已失效。",
            )
        await self._current_evidence(observation, entry)

    async def _current_evidence(self, observation, entry):
        terminals = await self.journal_store.terminal_for_entry(entry.receipt_id)
        terminal_ids = tuple(item.event_id for item in terminals)
        terminal_sha = tuple(item.event_sha256 for item in terminals)
        if not (
            terminal_ids == observation.terminal_event_ids
            and terminal_sha == observation.terminal_event_sha256
            and len(terminals) == observation.completed_runs
        ):
            raise EvolutionRevalidationRolloutStageCompletionError(
                "rollout_stage_completion_terminal_prefix_changed",
                "Passing Observation 后 terminal run prefix 已变化，必须重新评估。",
            )
        changes = await self.control_store.history(
            self.workspace_root, after_sequence=entry.control_sequence
        )
        if changes:
            raise EvolutionRevalidationRolloutStageCompletionError(
                "rollout_stage_completion_control_changed",
                "Passing Observation 后 rollout control 已变化。",
            )
        return terminals, await self.control_store.latest(self.workspace_root)


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollout Stage Completion 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _restore(encoded: str) -> EvolutionRevalidationRolloutStageCompletion:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationRolloutStageCompletionError(
            "rollout_stage_completion_oversized",
            "持久化 Rollout Stage Completion 超过 512 KiB。",
        )
    return EvolutionRevalidationRolloutStageCompletion.model_validate_json(encoded)


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_stage_completions ("
        "completion_id TEXT PRIMARY KEY, completion_sha256 TEXT NOT NULL UNIQUE, "
        "observation_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, "
        "completion_json TEXT NOT NULL, completed_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLOUT_STAGE_COMPLETION_POLICY",
    "EvolutionRevalidationRolloutStageCompletion",
    "EvolutionRevalidationRolloutStageCompletionError",
    "EvolutionRevalidationRolloutStageCompletionService",
    "EvolutionRevalidationRolloutStageCompletionStore",
]
