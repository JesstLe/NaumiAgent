"""Pause a breached rollout and freeze its exact rollback request."""

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

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionRollbackPlan,
)
from naumi_agent.evolution.revalidation_local_canary_runs import (
    EvolutionRevalidationLocalCanaryJournalStore,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutStageEntryService,
)
from naumi_agent.evolution.revalidation_runtime_observations import (
    EvolutionRevalidationRuntimeObservationStatus,
    EvolutionRevalidationRuntimeObservationStore,
)

EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY = (
    "evolution-revalidation-rollback-request-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRollbackRequest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollback-request-v1"] = (
        EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY
    )
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    observation_id: str = Field(pattern=r"^evreruntimeobs_[0-9a-f]{24}$")
    observation_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    baseline_id: str = Field(pattern=r"^evrerolloutbaseline_[0-9a-f]{24}$")
    baseline_sha256: str = Field(pattern=_SHA256_RE)
    entry_receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    entry_receipt_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan: EvolutionPromotionRollbackPlan
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    pause_event_id: str = Field(pattern=r"^evrerolloutctrl_[0-9a-f]{24}$")
    pause_event_sha256: str = Field(pattern=_SHA256_RE)
    pause_actor: EvolutionRevalidationRolloutControlActor
    pause_reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    breach_reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    data_restore_required: bool
    automatic_pause_satisfied: Literal[True] = True
    monitor_pause_created: bool
    rollback_request_authority: Literal[True] = True
    rollback_execution_authority: Literal[False] = False
    workspace_write_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    requested_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollback Request workspace 必须 canonical。")
        if self.rollback_plan_sha256 != self.rollback_plan.plan_sha256:
            raise ValueError("Rollback Request plan digest 不一致。")
        if self.data_restore_required is not self.rollback_plan.data_restore_required:
            raise ValueError("Rollback Request data restore 投影不一致。")
        if self.breach_reasons != tuple(sorted(set(self.breach_reasons))):
            raise ValueError("Rollback Request breach reasons 必须有序且唯一。")
        if self.monitor_pause_created and not (
            self.pause_actor is EvolutionRevalidationRolloutControlActor.MONITOR
            and self.pause_reason_code == "runtime_guardrail_breach"
        ):
            raise ValueError("Rollback Request pause projection 不一致。")
        if datetime.fromisoformat(self.requested_at).utcoffset() is None:
            raise ValueError("Rollback Request requested_at 必须包含 offset。")
        core = self.model_dump(mode="json", exclude={"request_id", "request_sha256"})
        digest = _digest(core)
        if self.request_sha256 != digest or self.request_id != (
            f"evrerollbackreq_{digest[:24]}"
        ):
            raise ValueError("Rollback Request identity 不一致。")
        return self


class EvolutionRevalidationRollbackRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRollbackRequestStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_observation(self, observation_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT request_json FROM evolution_revalidation_rollback_requests "
                "WHERE observation_id = ?", (observation_id,)
            )).fetchone()
        if row is None:
            return None
        return _restore_request(row["request_json"])

    async def record(self, request):
        item = EvolutionRevalidationRollbackRequest.model_validate_json(
            request.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRollbackRequestError(
                "rollback_request_artifact_too_large",
                "Rollback Request 超过 512 KiB 持久化上限。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            observation = await (await db.execute(
                "SELECT observation_sha256 FROM evolution_revalidation_runtime_observations "
                "WHERE observation_id = ?", (item.observation_id,)
            )).fetchone()
            promotion_input = await (await db.execute(
                "SELECT input_sha256 FROM evolution_revalidation_promotion_inputs "
                "WHERE input_id = ?", (item.promotion_input_id,)
            )).fetchone()
            pause = await (await db.execute(
                "SELECT event_sha256, state FROM "
                "evolution_revalidation_rollout_control_events WHERE event_id = ?",
                (item.pause_event_id,),
            )).fetchone()
            if not (
                observation is not None
                and observation["observation_sha256"] == item.observation_sha256
                and promotion_input is not None
                and promotion_input["input_sha256"] == item.promotion_input_sha256
                and pause is not None
                and pause["event_sha256"] == item.pause_event_sha256
                and pause["state"] == EvolutionRevalidationRolloutControlState.PAUSED.value
            ):
                await db.rollback()
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_dependency_mismatch",
                    "Rollback Request 的 Observation/Input/Pause 依赖不一致。",
                )
            existing = await (await db.execute(
                "SELECT request_json FROM evolution_revalidation_rollback_requests "
                "WHERE observation_id = ?", (item.observation_id,)
            )).fetchone()
            if existing is not None:
                restored = _restore_request(existing["request_json"])
                await db.rollback()
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollback_requests "
                "(request_id, request_sha256, observation_id, plan_id, request_json, "
                "requested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item.request_id, item.request_sha256, item.observation_id, item.plan_id,
                 encoded, item.requested_at),
            )
            await db.commit()
        return item


class EvolutionRevalidationRollbackRequestService:
    def __init__(self, *, workspace_root, observation_store, baseline_service,
                 entry_service, journal_store, control_service, store, now=None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.observation_store: EvolutionRevalidationRuntimeObservationStore = observation_store
        self.baseline_service: EvolutionRevalidationRolloutBaselineService = baseline_service
        self.entry_service: EvolutionRevalidationRolloutStageEntryService = entry_service
        self.journal_store: EvolutionRevalidationLocalCanaryJournalStore = journal_store
        self.control_service: EvolutionRevalidationRolloutControlService = control_service
        self.store: EvolutionRevalidationRollbackRequestStore = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(self, *, observation_id: str):
        lock = self._locks.setdefault(observation_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_observation(observation_id)
            if existing is not None:
                return existing
            observation = await self.observation_store.get(observation_id)
            if observation is None or observation.status is not (
                EvolutionRevalidationRuntimeObservationStatus.BREACHED
            ) or not (
                observation.pause_input_authority
                and observation.rollback_input_authority
            ):
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_observation_not_breached",
                    "只有 exact breached Observation 可以请求回滚。",
                )
            await self._validate_observation_evidence(observation)
            baseline = await self.baseline_service.inspect(plan_id=observation.plan_id)
            if not baseline.monitor_input_ready or baseline.baseline.baseline_sha256 != (
                observation.baseline_sha256
            ):
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_baseline_stale", "Rollback Baseline 已失效。"
                )
            plan_view = await self.entry_service.plan_service.inspect(
                plan_id=observation.plan_id
            )
            if not plan_view.current_rollout_eligible or plan_view.plan.plan_sha256 != (
                observation.plan_sha256
            ):
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_plan_stale", "Rollout Plan 已失效。"
                )
            promotion_input = await (
                self.entry_service.plan_service.promotion_input_service.store.get(
                    plan_view.plan.contract_id
                )
            )
            if promotion_input is None or not (
                promotion_input.input_id == plan_view.plan.promotion_input_id
                and promotion_input.input_sha256 == plan_view.plan.promotion_input_sha256
                and promotion_input.prior_input.rollback.plan_sha256
                == plan_view.plan.rollback_plan_sha256
            ):
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_plan_missing",
                    "Fresh Promotion Input 未携带 exact Rollback Plan。",
                )
            before = await self.control_service.store.latest(self.workspace_root)
            pause = await self.control_service.pause(
                reason_code="runtime_guardrail_breach",
                actor=EvolutionRevalidationRolloutControlActor.MONITOR,
                changed_at=self.now(),
            )
            if pause.state is not EvolutionRevalidationRolloutControlState.PAUSED:
                raise EvolutionRevalidationRollbackRequestError(
                    "rollback_request_pause_failed", "Rollout kill switch 未暂停。"
                )
            rollback = promotion_input.prior_input.rollback
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY,
                "workspace_root": str(self.workspace_root),
                "observation_id": observation.observation_id,
                "observation_sha256": observation.observation_sha256,
                "plan_id": observation.plan_id,
                "plan_sha256": observation.plan_sha256,
                "baseline_id": observation.baseline_id,
                "baseline_sha256": observation.baseline_sha256,
                "entry_receipt_id": observation.entry_receipt_id,
                "entry_receipt_sha256": observation.entry_receipt_sha256,
                "promotion_input_id": promotion_input.input_id,
                "promotion_input_sha256": promotion_input.input_sha256,
                "rollback_plan": rollback.model_dump(mode="json"),
                "rollback_plan_sha256": rollback.plan_sha256,
                "pause_event_id": pause.event_id,
                "pause_event_sha256": pause.event_sha256,
                "pause_actor": pause.actor.value,
                "pause_reason_code": pause.reason_code,
                "breach_reasons": list(observation.breach_reasons),
                "data_restore_required": rollback.data_restore_required,
                "automatic_pause_satisfied": True,
                "monitor_pause_created": before is None or before.event_id != pause.event_id,
                "rollback_request_authority": True,
                "rollback_execution_authority": False,
                "workspace_write_executed": False,
                "git_write_executed": False,
                "rollback_executed": False,
                "promotion_authority": False,
                "requested_at": _aware(self.now()).isoformat(),
            }
            digest = _digest(core)
            return await self.store.record(
                EvolutionRevalidationRollbackRequest.model_validate({
                    **core,
                    "request_id": f"evrerollbackreq_{digest[:24]}",
                    "request_sha256": digest,
                })
            )

    async def _validate_observation_evidence(self, observation):
        entry = await self.entry_service.store.get(observation.entry_receipt_id)
        if entry is None or entry.receipt_sha256 != observation.entry_receipt_sha256:
            raise EvolutionRevalidationRollbackRequestError(
                "rollback_request_entry_stale",
                "Breached Observation 的 stage entry 已漂移。",
            )
        terminals = await self.journal_store.terminal_for_entry(
            observation.entry_receipt_id
        )
        observed = {
            item.event_id: item.event_sha256 for item in terminals
        }
        if any(
            observed.get(event_id) != digest
            for event_id, digest in zip(
                observation.terminal_event_ids,
                observation.terminal_event_sha256,
                strict=True,
            )
        ):
            raise EvolutionRevalidationRollbackRequestError(
                "rollback_request_observation_evidence_stale",
                "Breached Observation 的 canary evidence 已漂移。",
            )
        controls = {
            item.event_id: item.event_sha256
            for item in await self.control_service.store.history(
                self.workspace_root,
                after_sequence=entry.control_sequence,
            )
        }
        if any(
            controls.get(event_id) != digest
            for event_id, digest in zip(
                observation.control_signal_event_ids,
                observation.control_signal_event_sha256,
                strict=True,
            )
        ):
            raise EvolutionRevalidationRollbackRequestError(
                "rollback_request_control_evidence_stale",
                "Breached Observation 的 control evidence 已漂移。",
            )


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollback Request 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _restore_request(encoded: str) -> EvolutionRevalidationRollbackRequest:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationRollbackRequestError(
            "rollback_request_artifact_too_large",
            "持久化的 Rollback Request 超过 512 KiB 上限。",
        )
    return EvolutionRevalidationRollbackRequest.model_validate_json(encoded)


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollback_requests ("
        "request_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL UNIQUE, "
        "observation_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, "
        "request_json TEXT NOT NULL, requested_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY",
    "EvolutionRevalidationRollbackRequest",
    "EvolutionRevalidationRollbackRequestError",
    "EvolutionRevalidationRollbackRequestService",
    "EvolutionRevalidationRollbackRequestStore",
]
