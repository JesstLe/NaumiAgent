"""Fenced local-canary stage entry authority and emergency rollout control."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutStageName,
)

EVOLUTION_REVALIDATION_ROLLOUT_CONTROL_POLICY = (
    "evolution-revalidation-rollout-control-v1"
)
EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ENTRY_POLICY = (
    "evolution-revalidation-rollout-stage-entry-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class EvolutionRevalidationRolloutControlState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class EvolutionRevalidationRolloutControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"


class EvolutionRevalidationRolloutControlActor(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    RUNTIME = "runtime"
    MONITOR = "monitor"


class EvolutionRevalidationRolloutEntryStatus(StrEnum):
    CURRENT = "current"
    EXPIRED = "expired"
    FENCED = "fenced"
    PLAN_STALE = "plan_stale"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRolloutControlEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-control-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_CONTROL_POLICY
    )
    event_id: str = Field(pattern=r"^evrerolloutctrl_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    sequence: int = Field(ge=1, le=1_000_000)
    previous_event_id: str = Field(
        default="", pattern=r"^(?:|evrerolloutctrl_[0-9a-f]{24})$"
    )
    previous_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    action: EvolutionRevalidationRolloutControlAction
    state: EvolutionRevalidationRolloutControlState
    actor: EvolutionRevalidationRolloutControlActor
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    changed_at: str = Field(min_length=1, max_length=100)
    control_plane_attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    control_plane_attestation_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollout control workspace 必须 canonical。")
        if (self.sequence == 1) is bool(self.previous_event_id):
            raise ValueError("Rollout control event chain 不一致。")
        if bool(self.previous_event_id) is not bool(self.previous_event_sha256):
            raise ValueError("Rollout control previous digest 不一致。")
        expected_state = (
            EvolutionRevalidationRolloutControlState.PAUSED
            if self.action is EvolutionRevalidationRolloutControlAction.PAUSE
            else EvolutionRevalidationRolloutControlState.ACTIVE
        )
        if self.state is not expected_state:
            raise ValueError("Rollout control action/state 不一致。")
        _aware(self.changed_at)
        core = self.model_dump(
            mode="json",
            exclude={
                "event_id",
                "event_sha256",
                "control_plane_attestation_sha256",
            },
        )
        digest = _digest(core)
        if self.event_sha256 != digest or self.event_id != f"evrerolloutctrl_{digest[:24]}":
            raise ValueError("Rollout control event identity 不一致。")
        return self


class EvolutionRevalidationRolloutStageEntryReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollout-stage-entry-v1"] = (
        EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ENTRY_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    decision_id: str = Field(pattern=r"^evreapprovaldecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    stage: Literal["local_canary"] = "local_canary"
    stage_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=10_000)
    previous_receipt_id: str = Field(
        default="", pattern=r"^(?:|evrerolloutentry_[0-9a-f]{24})$"
    )
    previous_receipt_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    control_state: Literal["active"] = "active"
    allowed_operations: tuple[
        Literal[
            "materialize_immutable_green",
            "run_local_canary",
            "record_canary_observation",
        ],
        ...,
    ] = Field(min_length=3, max_length=3)
    workspace_mode: Literal["read_only"] = "read_only"
    ephemeral_root_required: Literal[True] = True
    network_mode: Literal["deny"] = "deny"
    process_tree_cancel_required: Literal[True] = True
    max_wall_seconds: int = Field(ge=300, le=604_800)
    max_completed_runs: int = Field(ge=10, le=100)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    control_plane_attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    control_plane_attestation_sha256: str = Field(pattern=_SHA256_RE)
    stage_entry_authority: Literal[True] = True
    local_canary_execution_authority: Literal[True] = True
    opt_in_authority: Literal[False] = False
    percentage_authority: Literal[False] = False
    stable_authority: Literal[False] = False
    workspace_write_authority: Literal[False] = False
    git_write_authority: Literal[False] = False
    merge_authority: Literal[False] = False
    push_authority: Literal[False] = False
    publish_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollout stage entry workspace 必须 canonical。")
        if (self.attempt == 1) is bool(self.previous_receipt_id):
            raise ValueError("Rollout stage entry chain 不一致。")
        if bool(self.previous_receipt_id) is not bool(self.previous_receipt_sha256):
            raise ValueError("Rollout stage entry previous digest 不一致。")
        if (self.control_sequence == 0) is bool(self.control_event_sha256):
            raise ValueError("Rollout stage entry control binding 不一致。")
        expected_operations = (
            "materialize_immutable_green",
            "run_local_canary",
            "record_canary_observation",
        )
        if self.allowed_operations != expected_operations:
            raise ValueError("Rollout stage entry operation scope 无效。")
        issued = _aware(self.issued_at)
        if _aware(self.expires_at) != issued + timedelta(seconds=self.max_wall_seconds):
            raise ValueError("Rollout stage entry expiry 投影不一致。")
        core = self.model_dump(
            mode="json",
            exclude={
                "receipt_id",
                "receipt_sha256",
                "control_plane_attestation_sha256",
            },
        )
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != f"evrerolloutentry_{digest[:24]}":
            raise ValueError("Rollout stage entry identity 不一致。")
        return self


class EvolutionRevalidationRolloutStageEntryView(_StrictModel):
    receipt: EvolutionRevalidationRolloutStageEntryReceipt
    status: EvolutionRevalidationRolloutEntryStatus
    plan_current: bool
    control_current: bool
    can_execute_local_canary: bool
    fenced_reason: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = self.status is EvolutionRevalidationRolloutEntryStatus.CURRENT
        if self.can_execute_local_canary is not current:
            raise ValueError("Rollout stage entry execution 投影不一致。")
        if current and not (self.plan_current and self.control_current):
            raise ValueError("Rollout stage entry current authority 投影不一致。")
        if current is bool(self.fenced_reason):
            raise ValueError("Rollout stage entry fenced reason 投影不一致。")
        return self


class EvolutionRevalidationRolloutStageEntryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRolloutControlStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._key_provider = control_plane_key_provider

    async def latest(self, workspace_root: str | Path):
        workspace = str(Path(workspace_root).expanduser().resolve())
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                    "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                    (workspace,),
                )
            ).fetchone()
        if row is None:
            return None
        item = EvolutionRevalidationRolloutControlEvent.model_validate_json(
            row["event_json"]
        )
        _verify_attestation(item, self._key())
        return item

    async def history(self, workspace_root: str | Path, *, after_sequence: int = 0):
        workspace = str(Path(workspace_root).expanduser().resolve())
        if not 0 <= after_sequence <= 1_000_000:
            raise ValueError("Rollout control after_sequence 无效。")
        if not self.db_path.is_file():
            return ()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            start_sequence = max(1, after_sequence)
            rows = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                    "WHERE workspace_root = ? AND sequence >= ? ORDER BY sequence ASC",
                    (workspace, start_sequence),
                )
            ).fetchall()
        events = tuple(
            EvolutionRevalidationRolloutControlEvent.model_validate_json(
                row["event_json"]
            )
            for row in rows
        )
        previous = None
        if after_sequence > 0 and not events:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_control_history_broken",
                "Rollout control history 缺少 entry 绑定的 event。",
            )
        if events and events[0].sequence != start_sequence:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_control_history_broken",
                "Rollout control history 缺少起始 event。",
            )
        for item in events:
            _verify_attestation(item, self._key())
            if previous is not None and not (
                item.sequence == previous.sequence + 1
                and item.previous_event_id == previous.event_id
                and item.previous_event_sha256 == previous.event_sha256
            ):
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_control_history_broken",
                    "Rollout control history chain 不连续。",
                )
            previous = item
        return tuple(item for item in events if item.sequence > after_sequence)

    async def record(self, event):
        item = EvolutionRevalidationRolloutControlEvent.model_validate_json(
            event.model_dump_json()
        )
        _verify_attestation(item, self._key())
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_control_oversized", "Rollout control event 过大。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            latest_row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                    "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                    (item.workspace_root,),
                )
            ).fetchone()
            latest = (
                None
                if latest_row is None
                else EvolutionRevalidationRolloutControlEvent.model_validate_json(
                    latest_row["event_json"]
                )
            )
            if latest is not None:
                _verify_attestation(latest, self._key())
            if latest is not None and latest.state is item.state:
                await db.rollback()
                return latest
            expected = (
                (1, "", "")
                if latest is None
                else (latest.sequence + 1, latest.event_id, latest.event_sha256)
            )
            if (
                item.sequence,
                item.previous_event_id,
                item.previous_event_sha256,
            ) != expected:
                await db.rollback()
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_control_chain_race", "Rollout control chain 已变化。"
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_rollout_control_events "
                "(event_id, event_sha256, workspace_root, sequence, state, "
                "event_json, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.event_id,
                    item.event_sha256,
                    item.workspace_root,
                    item.sequence,
                    item.state.value,
                    encoded,
                    item.changed_at,
                ),
            )
            await db.commit()
        return item

    def _key(self):
        return _key(self._key_provider)


class EvolutionRevalidationRolloutControlService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: EvolutionRevalidationRolloutControlStore,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.store = store
        self._key_provider = control_plane_key_provider
        self._lock = asyncio.Lock()

    async def pause(
        self,
        *,
        reason_code: str,
        actor: EvolutionRevalidationRolloutControlActor,
        changed_at: str | None = None,
    ):
        return await self._set(
            state=EvolutionRevalidationRolloutControlState.PAUSED,
            action=EvolutionRevalidationRolloutControlAction.PAUSE,
            reason_code=reason_code,
            actor=actor,
            changed_at=changed_at,
        )

    async def resume(
        self,
        *,
        reason_code: str,
        actor: EvolutionRevalidationRolloutControlActor,
        changed_at: str | None = None,
    ):
        if actor not in {
            EvolutionRevalidationRolloutControlActor.USER,
            EvolutionRevalidationRolloutControlActor.OPERATOR,
        }:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_control_resume_actor_forbidden",
                "只有 user/operator control-plane actor 可以恢复 Rollout。",
            )
        return await self._set(
            state=EvolutionRevalidationRolloutControlState.ACTIVE,
            action=EvolutionRevalidationRolloutControlAction.RESUME,
            reason_code=reason_code,
            actor=actor,
            changed_at=changed_at,
        )

    async def _set(self, *, state, action, reason_code, actor, changed_at):
        async with self._lock:
            latest = await self.store.latest(self.workspace_root)
            if latest is not None and latest.state is state:
                return latest
            now = _aware(changed_at or datetime.now(UTC).isoformat())
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_CONTROL_POLICY,
                "workspace_root": str(self.workspace_root),
                "sequence": 1 if latest is None else latest.sequence + 1,
                "previous_event_id": "" if latest is None else latest.event_id,
                "previous_event_sha256": "" if latest is None else latest.event_sha256,
                "action": action.value,
                "state": state.value,
                "actor": actor.value,
                "reason_code": reason_code,
                "changed_at": now.isoformat(),
                "control_plane_attestation_algorithm": "hmac-sha256",
            }
            digest = _digest(core)
            attestation = _attestation(core, _key(self._key_provider))
            return await self.store.record(
                EvolutionRevalidationRolloutControlEvent.model_validate({
                    **core,
                    "event_id": f"evrerolloutctrl_{digest[:24]}",
                    "event_sha256": digest,
                    "control_plane_attestation_sha256": attestation,
                })
            )


class EvolutionRevalidationRolloutStageEntryStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._key_provider = control_plane_key_provider

    async def get(self, receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_rollout_stage_entries "
                    "WHERE receipt_id = ?",
                    (receipt_id,),
                )
            ).fetchone()
        if row is None:
            return None
        item = EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
            row["receipt_json"]
        )
        _verify_attestation(item, self._key())
        return item

    async def latest(self, plan_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_rollout_stage_entries "
                    "WHERE plan_id = ? ORDER BY attempt DESC LIMIT 1",
                    (plan_id,),
                )
            ).fetchone()
        if row is None:
            return None
        item = EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
            row["receipt_json"]
        )
        _verify_attestation(item, self._key())
        return item

    async def record(self, receipt):
        item = EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        _verify_attestation(item, self._key())
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_stage_entry_oversized", "Rollout stage entry 过大。"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            plan_row = await (
                await db.execute(
                    "SELECT plan_json FROM evolution_revalidation_rollout_plans "
                    "WHERE plan_id = ?",
                    (item.plan_id,),
                )
            ).fetchone()
            if plan_row is None:
                await db.rollback()
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_plan_missing", "Rollout Plan 不存在。"
                )
            plan = EvolutionRevalidationRolloutPlan.model_validate_json(
                plan_row["plan_json"]
            )
            control_row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                    "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                    (item.workspace_root,),
                )
            ).fetchone()
            control = (
                None
                if control_row is None
                else EvolutionRevalidationRolloutControlEvent.model_validate_json(
                    control_row["event_json"]
                )
            )
            if control is not None:
                _verify_attestation(control, self._key())
            sequence = 0 if control is None else control.sequence
            control_sha = "" if control is None else control.event_sha256
            active = control is None or (
                control.state is EvolutionRevalidationRolloutControlState.ACTIVE
            )
            if not (
                plan.plan_sha256 == item.plan_sha256
                and plan.contract_id == item.contract_id
                and plan.decision_id == item.decision_id
                and plan.decision_sha256 == item.decision_sha256
                and _stage_digest(plan) == item.stage_sha256
                and active
                and sequence == item.control_sequence
                and control_sha == item.control_event_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_atomic_authority_changed",
                    "Rollout Plan 或 kill switch 在 stage entry 持久化前变化。",
                )
            latest_row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_rollout_stage_entries "
                    "WHERE plan_id = ? ORDER BY attempt DESC LIMIT 1",
                    (item.plan_id,),
                )
            ).fetchone()
            latest = (
                None
                if latest_row is None
                else EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
                    latest_row["receipt_json"]
                )
            )
            if latest is not None:
                _verify_attestation(latest, self._key())
            if (
                latest is not None
                and latest.control_sequence == item.control_sequence
                and latest.control_event_sha256 == item.control_event_sha256
                and _aware(latest.expires_at) > _aware(item.issued_at)
            ):
                await db.rollback()
                return latest
            expected = (
                (1, "", "")
                if latest is None
                else (latest.attempt + 1, latest.receipt_id, latest.receipt_sha256)
            )
            if (
                item.attempt,
                item.previous_receipt_id,
                item.previous_receipt_sha256,
            ) != expected:
                await db.rollback()
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_chain_race", "Rollout stage entry chain 已变化。"
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_rollout_stage_entries "
                "(receipt_id, receipt_sha256, plan_id, attempt, control_sequence, "
                "receipt_json, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.plan_id,
                    item.attempt,
                    item.control_sequence,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    def _key(self):
        return _key(self._key_provider)


class EvolutionRevalidationRolloutStageEntryService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        plan_service: EvolutionRevalidationRolloutPlanService,
        control_store: EvolutionRevalidationRolloutControlStore,
        store: EvolutionRevalidationRolloutStageEntryStore,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.plan_service = plan_service
        self.control_store = control_store
        self.store = store
        self._key_provider = control_plane_key_provider
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(self, *, plan_id: str):
        lock = self._locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            now = _aware(self.now())
            latest = await self.store.latest(plan_id)
            if latest is not None:
                existing = await self.inspect(
                    receipt_id=latest.receipt_id, assessed_at=now.isoformat()
                )
                if existing.can_execute_local_canary:
                    return existing
            plan_view = await self.plan_service.inspect(plan_id=plan_id)
            if not plan_view.current_rollout_eligible:
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_plan_not_current",
                    "Rollout Plan 当前不可进入 local canary。",
                )
            control = await self.control_store.latest(self.workspace_root)
            if control is not None and (
                control.state is EvolutionRevalidationRolloutControlState.PAUSED
            ):
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_kill_switch_paused",
                    f"Rollout kill switch 已暂停：{control.reason_code}。",
                )
            plan = plan_view.plan
            stage = plan.stages[0]
            if stage.name is not EvolutionRevalidationRolloutStageName.LOCAL_CANARY:
                raise EvolutionRevalidationRolloutStageEntryError(
                    "rollout_stage_entry_first_stage_invalid",
                    "Rollout Plan 第一阶段不是 local canary。",
                )
            latest = await self.store.latest(plan_id)
            max_wall_seconds = min(
                stage.minimum_observation_seconds + 3_600,
                604_800,
            )
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ENTRY_POLICY,
                "workspace_root": str(self.workspace_root),
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "contract_id": plan.contract_id,
                "decision_id": plan.decision_id,
                "decision_sha256": plan.decision_sha256,
                "stage": "local_canary",
                "stage_sha256": _stage_digest(plan),
                "attempt": 1 if latest is None else latest.attempt + 1,
                "previous_receipt_id": "" if latest is None else latest.receipt_id,
                "previous_receipt_sha256": (
                    "" if latest is None else latest.receipt_sha256
                ),
                "control_sequence": 0 if control is None else control.sequence,
                "control_event_sha256": "" if control is None else control.event_sha256,
                "control_state": "active",
                "allowed_operations": [
                    "materialize_immutable_green",
                    "run_local_canary",
                    "record_canary_observation",
                ],
                "workspace_mode": "read_only",
                "ephemeral_root_required": True,
                "network_mode": "deny",
                "process_tree_cancel_required": True,
                "max_wall_seconds": max_wall_seconds,
                "max_completed_runs": stage.minimum_completed_runs,
                "issued_at": now.isoformat(),
                "expires_at": (
                    now + timedelta(seconds=max_wall_seconds)
                ).isoformat(),
                "control_plane_attestation_algorithm": "hmac-sha256",
                "stage_entry_authority": True,
                "local_canary_execution_authority": True,
                "opt_in_authority": False,
                "percentage_authority": False,
                "stable_authority": False,
                "workspace_write_authority": False,
                "git_write_authority": False,
                "merge_authority": False,
                "push_authority": False,
                "publish_authority": False,
            }
            digest = _digest(core)
            receipt = EvolutionRevalidationRolloutStageEntryReceipt.model_validate({
                **core,
                "receipt_id": f"evrerolloutentry_{digest[:24]}",
                "receipt_sha256": digest,
                "control_plane_attestation_sha256": _attestation(
                    core, _key(self._key_provider)
                ),
            })
            stored = await self.store.record(receipt)
            return await self.inspect(
                receipt_id=stored.receipt_id,
                assessed_at=now.isoformat(),
            )

    async def inspect(self, *, receipt_id: str, assessed_at: str | None = None):
        now = _aware(assessed_at or self.now())
        receipt = await self.store.get(receipt_id)
        if receipt is None:
            raise EvolutionRevalidationRolloutStageEntryError(
                "rollout_stage_entry_missing", "Rollout stage entry 不存在。"
            )
        plan_view = await self.plan_service.inspect(plan_id=receipt.plan_id)
        plan_current = bool(
            plan_view.current_rollout_eligible
            and plan_view.plan.plan_sha256 == receipt.plan_sha256
        )
        control = await self.control_store.latest(self.workspace_root)
        control_sequence = 0 if control is None else control.sequence
        control_sha = "" if control is None else control.event_sha256
        control_current = bool(
            (control is None or control.state is EvolutionRevalidationRolloutControlState.ACTIVE)
            and control_sequence == receipt.control_sequence
            and control_sha == receipt.control_event_sha256
        )
        if not plan_current:
            status = EvolutionRevalidationRolloutEntryStatus.PLAN_STALE
            reason = "plan_stale"
        elif not control_current:
            status = EvolutionRevalidationRolloutEntryStatus.FENCED
            reason = "kill_switch_generation_changed"
        elif now >= _aware(receipt.expires_at):
            status = EvolutionRevalidationRolloutEntryStatus.EXPIRED
            reason = "entry_expired"
        else:
            status = EvolutionRevalidationRolloutEntryStatus.CURRENT
            reason = ""
        return EvolutionRevalidationRolloutStageEntryView(
            receipt=receipt,
            status=status,
            plan_current=plan_current,
            control_current=control_current,
            can_execute_local_canary=(
                status is EvolutionRevalidationRolloutEntryStatus.CURRENT
            ),
            fenced_reason=reason,
        )


def _stage_digest(plan):
    return _digest(plan.stages[0].model_dump(mode="json"))


def _key(provider):
    key = provider()
    if not isinstance(key, bytes) or len(key) < 32:
        raise EvolutionRevalidationRolloutStageEntryError(
            "rollout_control_plane_key_invalid",
            "Rollout control-plane key 不可用。",
        )
    return key


def _verify_attestation(item, key):
    core = item.model_dump(
        mode="json",
        exclude={
            "event_id",
            "event_sha256",
            "receipt_id",
            "receipt_sha256",
            "control_plane_attestation_sha256",
        },
    )
    expected = _attestation(core, key)
    if not hmac.compare_digest(expected, item.control_plane_attestation_sha256):
        raise EvolutionRevalidationRolloutStageEntryError(
            "rollout_control_plane_attestation_invalid",
            "Rollout control-plane attestation 无效。",
        )


def _attestation(payload, key):
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollout stage entry 时间必须包含 offset。")
    return parsed.astimezone(UTC)


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_control_events ("
        "event_id TEXT PRIMARY KEY, event_sha256 TEXT NOT NULL UNIQUE, "
        "workspace_root TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, "
        "event_json TEXT NOT NULL, changed_at TEXT NOT NULL, "
        "UNIQUE(workspace_root, sequence))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollout_stage_entries ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "plan_id TEXT NOT NULL, attempt INTEGER NOT NULL, control_sequence INTEGER NOT NULL, "
        "receipt_json TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "UNIQUE(plan_id, attempt))"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLOUT_CONTROL_POLICY",
    "EVOLUTION_REVALIDATION_ROLLOUT_STAGE_ENTRY_POLICY",
    "EvolutionRevalidationRolloutControlAction",
    "EvolutionRevalidationRolloutControlActor",
    "EvolutionRevalidationRolloutControlEvent",
    "EvolutionRevalidationRolloutControlService",
    "EvolutionRevalidationRolloutControlState",
    "EvolutionRevalidationRolloutControlStore",
    "EvolutionRevalidationRolloutEntryStatus",
    "EvolutionRevalidationRolloutStageEntryError",
    "EvolutionRevalidationRolloutStageEntryReceipt",
    "EvolutionRevalidationRolloutStageEntryService",
    "EvolutionRevalidationRolloutStageEntryStore",
    "EvolutionRevalidationRolloutStageEntryView",
]
