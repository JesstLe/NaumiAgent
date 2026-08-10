"""Fenced ARC-07 slot rollback execution for exact revalidation evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollback_requests import (
    EvolutionRevalidationRollbackRequest,
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollback_sources import (
    EvolutionRevalidationRollbackSource,
    EvolutionRevalidationRollbackSourceStore,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.release.launcher import (
    ReleaseLaunchResolution,
    resolve_and_record_launch,
)
from naumi_agent.release.slots import (
    ReleaseActivePointer,
    ReleaseInstalledSlot,
    ReleaseRollbackAuthority,
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_REVALIDATION_ROLLBACK_EXECUTION_POLICY = "evolution-revalidation-rollback-execution-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRollbackExecutionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollback-execution-v1"] = (
        EVOLUTION_REVALIDATION_ROLLBACK_EXECUTION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrerollbackexec_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    source_id: str = Field(pattern=r"^evrerollbacksrc_[0-9a-f]{24}$")
    source_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    pause_event_id: str = Field(pattern=r"^evrerolloutctrl_[0-9a-f]{24}$")
    pause_event_sha256: str = Field(pattern=_SHA256_RE)
    candidate_slot: ReleaseInstalledSlot
    baseline_slot: ReleaseInstalledSlot
    expected_pointer: ReleaseActivePointer
    rollback_pointer: ReleaseActivePointer
    rollback_boot_receipt: ReleaseSlotBootReceipt
    launch_resolution: ReleaseLaunchResolution
    data_restore_required: Literal[False] = False
    exact_candidate_slot_verified: Literal[True] = True
    exact_baseline_slot_verified: Literal[True] = True
    kill_switch_paused: Literal[True] = True
    expected_pointer_cas_satisfied: Literal[True] = True
    release_pointer_switched: Literal[True] = True
    rollback_executed: Literal[True] = True
    post_rollback_launch_verified: Literal[True] = True
    process_started: Literal[False] = False
    user_process_started: Literal[False] = False
    workspace_write_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    outcome_recorded: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollback Execution workspace 必须 canonical。")
        authority = self.rollback_pointer.rollback_authority
        if not (
            self.expected_pointer.current_slot_id == self.candidate_slot.slot_id
            and self.expected_pointer.current_slot_sha256 == self.candidate_slot.slot_sha256
            and self.expected_pointer.previous_slot_id == self.baseline_slot.slot_id
            and self.expected_pointer.previous_slot_sha256 == self.baseline_slot.slot_sha256
            and self.rollback_pointer.schema_version == 3
            and self.rollback_pointer.action == "rollback"
            and self.rollback_pointer.generation == self.expected_pointer.generation + 1
            and self.rollback_pointer.previous_pointer_sha256
            == self.expected_pointer.pointer_sha256
            and self.rollback_pointer.current_slot_id == self.baseline_slot.slot_id
            and self.rollback_pointer.current_slot_sha256 == self.baseline_slot.slot_sha256
            and self.rollback_pointer.previous_slot_id == self.candidate_slot.slot_id
            and self.rollback_pointer.previous_slot_sha256 == self.candidate_slot.slot_sha256
            and authority is not None
            and authority.kind == "evolution_revalidation_rollback_source"
            and authority.authority_id == self.source_id
            and authority.authority_sha256 == self.source_sha256
        ):
            raise ValueError("Rollback Execution pointer/slot authority 不一致。")
        if not (
            self.rollback_pointer.boot_receipt_id == self.rollback_boot_receipt.receipt_id
            and self.rollback_pointer.boot_receipt_sha256
            == self.rollback_boot_receipt.receipt_sha256
            and self.rollback_boot_receipt.slot_id == self.baseline_slot.slot_id
            and self.rollback_boot_receipt.slot_sha256 == self.baseline_slot.slot_sha256
            and self.launch_resolution.pointer_id == self.rollback_pointer.pointer_id
            and self.launch_resolution.pointer_sha256 == self.rollback_pointer.pointer_sha256
            and self.launch_resolution.slot_id == self.baseline_slot.slot_id
            and self.launch_resolution.slot_sha256 == self.baseline_slot.slot_sha256
            and not self.launch_resolution.process_start_requested
            and not self.launch_resolution.process_start_authority
        ):
            raise ValueError("Rollback Execution boot/launch projection 不一致。")
        _aware(self.completed_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (f"evrerollbackexec_{digest[:24]}"):
            raise ValueError("Rollback Execution identity 不一致。")
        return self


class EvolutionRevalidationRollbackExecutionView(_StrictModel):
    receipt: EvolutionRevalidationRollbackExecutionReceipt
    durable_dependencies_valid: bool
    control_evidence_valid: bool
    release_history_valid: bool
    baseline_slot_valid: bool
    launch_resolution_valid: bool
    rollback_fact_authority: bool
    active_baseline_authority: bool
    outcome_recorded: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        fact = bool(
            self.durable_dependencies_valid
            and self.control_evidence_valid
            and self.release_history_valid
            and self.baseline_slot_valid
            and self.launch_resolution_valid
        )
        if self.rollback_fact_authority is not fact:
            raise ValueError("Rollback Execution fact authority 投影不一致。")
        if self.active_baseline_authority and not fact:
            raise ValueError("Active baseline authority 不能超出 rollback fact。")
        return self


class EvolutionRevalidationRollbackExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRollbackExecutionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_request(
        self, request_id: str
    ) -> EvolutionRevalidationRollbackExecutionReceipt | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_rollback_executions "
                    "WHERE request_id = ?",
                    (request_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def record(
        self, receipt: EvolutionRevalidationRollbackExecutionReceipt
    ) -> EvolutionRevalidationRollbackExecutionReceipt:
        item = EvolutionRevalidationRollbackExecutionReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_artifact_oversized",
                "Rollback Execution Receipt 超过 2 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            request = await (
                await db.execute(
                    "SELECT request_sha256, request_json FROM "
                    "evolution_revalidation_rollback_requests "
                    "WHERE request_id = ?",
                    (item.request_id,),
                )
            ).fetchone()
            source = await (
                await db.execute(
                    "SELECT source_sha256, request_id, source_json FROM "
                    "evolution_revalidation_rollback_sources WHERE source_id = ?",
                    (item.source_id,),
                )
            ).fetchone()
            plan = await (
                await db.execute(
                    "SELECT plan_sha256, plan_json FROM "
                    "evolution_revalidation_rollout_plans "
                    "WHERE plan_id = ?",
                    (item.plan_id,),
                )
            ).fetchone()
            typed_request = (
                None
                if request is None
                else EvolutionRevalidationRollbackRequest.model_validate_json(
                    request["request_json"]
                )
            )
            typed_source = (
                None
                if source is None
                else EvolutionRevalidationRollbackSource.model_validate_json(
                    source["source_json"]
                )
            )
            typed_plan = (
                None
                if plan is None
                else EvolutionRevalidationRolloutPlan.model_validate_json(
                    plan["plan_json"]
                )
            )
            if not (
                request is not None
                and request["request_sha256"] == item.request_sha256
                and typed_request is not None
                and typed_request.request_id == item.request_id
                and typed_request.request_sha256 == item.request_sha256
                and source is not None
                and source["source_sha256"] == item.source_sha256
                and source["request_id"] == item.request_id
                and typed_source is not None
                and typed_source.source_id == item.source_id
                and typed_source.source_sha256 == item.source_sha256
                and typed_source.request_id == item.request_id
                and plan is not None
                and plan["plan_sha256"] == item.plan_sha256
                and typed_plan is not None
                and typed_plan.plan_id == item.plan_id
                and typed_plan.plan_sha256 == item.plan_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationRollbackExecutionError(
                    "rollback_execution_dependency_mismatch",
                    "Rollback Execution durable dependency 不一致。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_rollback_executions "
                    "WHERE request_id = ?",
                    (item.request_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore_receipt(existing["receipt_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationRollbackExecutionError(
                        "rollback_execution_receipt_conflict",
                        "同一 Rollback Request 已绑定不同执行回执。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollback_executions "
                "(receipt_id, receipt_sha256, request_id, source_id, plan_id, "
                "receipt_json, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.request_id,
                    item.source_id,
                    item.plan_id,
                    encoded,
                    item.completed_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationRollbackExecutionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        request_store: EvolutionRevalidationRollbackRequestStore,
        source_store: EvolutionRevalidationRollbackSourceStore,
        plan_store: EvolutionRevalidationRolloutPlanStore,
        control_store: EvolutionRevalidationRolloutControlStore,
        release_slot_store: ReleaseSlotStore,
        store: EvolutionRevalidationRollbackExecutionStore,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.request_store = request_store
        self.source_store = source_store
        self.plan_store = plan_store
        self.control_store = control_store
        self.release_slot_store = release_slot_store
        self.store = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(self, *, request_id: str) -> EvolutionRevalidationRollbackExecutionView:
        if not re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", request_id):
            raise ValueError("request_id 格式无效。")
        lock = self._locks.setdefault(request_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_request(request_id)
            if existing is not None:
                return await self.inspect(request_id=request_id)
            request, source, plan = await self._dependencies(request_id)
            authority = ReleaseRollbackAuthority(
                kind="evolution_revalidation_rollback_source",
                authority_id=source.source_id,
                authority_sha256=source.source_sha256,
            )
            rollback = await asyncio.to_thread(
                self.release_slot_store.get_rollback_event_by_authority,
                authority,
            )
            if rollback is None:
                await self._require_current_pause(request)
                expected, candidate, baseline = await asyncio.to_thread(
                    self._preconditions, request, source, plan
                )
                await asyncio.to_thread(
                    self.release_slot_store.verify_bootable,
                    baseline.slot_id,
                    checked_at=self.now(),
                )
                refreshed_request, refreshed_source, refreshed_plan = await self._dependencies(
                    request_id
                )
                await self._require_current_pause(refreshed_request)
                if (refreshed_request, refreshed_source, refreshed_plan) != (
                    request,
                    source,
                    plan,
                ):
                    raise EvolutionRevalidationRollbackExecutionError(
                        "rollback_execution_authority_changed",
                        "Rollback authority 在 boot probe 期间变化。",
                    )
                current = await asyncio.to_thread(self.release_slot_store.active)
                if current != expected:
                    rollback = await asyncio.to_thread(
                        self.release_slot_store.get_rollback_event_by_authority,
                        authority,
                    )
                    if rollback is None:
                        raise EvolutionRevalidationRollbackExecutionError(
                            "rollback_execution_pointer_changed",
                            "Active pointer 在 rollback CAS 前被无关执行者推进。",
                        )
                else:
                    try:
                        rollback = await asyncio.to_thread(
                            self.release_slot_store.activate,
                            baseline.slot_id,
                            activated_at=self.now(),
                            action="rollback",
                            _expected_pointer_sha256=expected.pointer_sha256,
                            _rollback_authority=authority,
                        )
                    except ReleaseSlotError as exc:
                        if exc.code != "release_active_pointer_conflict":
                            raise EvolutionRevalidationRollbackExecutionError(
                                "rollback_execution_switch_failed",
                                "版本槽原子回滚失败。",
                            ) from exc
                        rollback = await asyncio.to_thread(
                            self.release_slot_store.get_rollback_event_by_authority,
                            authority,
                        )
                        if rollback is None:
                            raise EvolutionRevalidationRollbackExecutionError(
                                "rollback_execution_cas_lost",
                                "Active pointer 已被无关执行者推进。",
                            ) from exc
            expected, candidate, baseline = await asyncio.to_thread(
                self._validate_rollback_event, rollback, source, plan
            )
            booted = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                rollback.current_slot_id,
                rollback.boot_receipt_id,
            )
            try:
                launch = await asyncio.to_thread(
                    resolve_and_record_launch,
                    self.release_slot_store,
                    argument_count=0,
                    process_start_requested=False,
                    resolved_at=rollback.activated_at,
                )
            except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
                raise EvolutionRevalidationRollbackExecutionError(
                    "rollback_execution_launch_verification_failed",
                    "回滚后 active runtime 启动解析验证失败。",
                ) from exc
            if launch.pointer_sha256 != rollback.pointer_sha256:
                raise EvolutionRevalidationRollbackExecutionError(
                    "rollback_execution_pointer_advanced",
                    "回滚后 active pointer 在启动解析完成前再次变化。",
                )
            final_request, final_source, final_plan = await self._dependencies(request_id)
            if (final_request, final_source, final_plan) != (request, source, plan):
                raise EvolutionRevalidationRollbackExecutionError(
                    "rollback_execution_dependency_changed",
                    "Rollback durable dependency 在回执写入前变化。",
                )
            await self._require_control_evidence(request)
            item = _build_receipt(
                workspace_root=self.workspace_root,
                request=request,
                source=source,
                plan=plan,
                candidate=candidate,
                baseline=baseline,
                expected=expected,
                rollback=rollback,
                boot=booted.boot_receipt,
                launch=launch,
            )
            await self.store.record(item)
            return await self.inspect(request_id=request_id)

    async def inspect(self, *, request_id: str) -> EvolutionRevalidationRollbackExecutionView:
        item = await self.store.get_by_request(request_id)
        if item is None:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_receipt_missing",
                "尚未形成 Rollback Execution Receipt。",
            )
        durable = control = history = slot_valid = launch_valid = False
        try:
            request, source, plan = await self._dependencies(request_id)
            durable = bool(
                request.request_sha256 == item.request_sha256
                and source.source_id == item.source_id
                and source.source_sha256 == item.source_sha256
                and plan.plan_id == item.plan_id
                and plan.plan_sha256 == item.plan_sha256
            )
            events = await self.control_store.history(self.workspace_root)
            control = any(
                event.event_id == item.pause_event_id
                and event.event_sha256 == item.pause_event_sha256
                and event.state is EvolutionRevalidationRolloutControlState.PAUSED
                for event in events
            )
            authority = ReleaseRollbackAuthority(
                kind="evolution_revalidation_rollback_source",
                authority_id=item.source_id,
                authority_sha256=item.source_sha256,
            )
            event = await asyncio.to_thread(
                self.release_slot_store.get_rollback_event_by_authority,
                authority,
            )
            prior = await asyncio.to_thread(
                self.release_slot_store.get_activation_event,
                item.expected_pointer.generation,
            )
            history = event == item.rollback_pointer and prior == item.expected_pointer
            baseline = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                item.baseline_slot.slot_id,
                item.rollback_boot_receipt.receipt_id,
            )
            slot_valid = bool(
                baseline.slot == item.baseline_slot
                and baseline.boot_receipt == item.rollback_boot_receipt
            )
            stored_launch = await asyncio.to_thread(
                self.release_slot_store.get_launch_resolution,
                item.launch_resolution.resolution_id,
            )
            launch_valid = stored_launch == item.launch_resolution
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        fact = durable and control and history and slot_valid and launch_valid
        try:
            active = await asyncio.to_thread(self.release_slot_store.active)
        except (OSError, ValueError, ReleaseSlotError):
            active = None
        return EvolutionRevalidationRollbackExecutionView(
            receipt=item,
            durable_dependencies_valid=durable,
            control_evidence_valid=control,
            release_history_valid=history,
            baseline_slot_valid=slot_valid,
            launch_resolution_valid=launch_valid,
            rollback_fact_authority=fact,
            active_baseline_authority=bool(fact and active == item.rollback_pointer),
            outcome_recorded=False,
            promotion_authority=False,
        )

    async def _dependencies(self, request_id):
        request = await self.request_store.get(request_id)
        source = await self.source_store.get_by_request(request_id)
        if request is None or source is None:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_input_missing",
                "Rollback Request 或 immutable Source 不存在。",
            )
        plan = await self.plan_store.get(request.plan_id)
        if not (
            request.workspace_root == str(self.workspace_root)
            and source.workspace_root == str(self.workspace_root)
            and source.request_id == request.request_id
            and source.request_sha256 == request.request_sha256
            and source.rollback_plan_sha256 == request.rollback_plan_sha256
            and plan is not None
            and plan.plan_sha256 == request.plan_sha256
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_input_mismatch",
                "Rollback Request、Source 与 Rollout Plan 不一致。",
            )
        if request.data_restore_required or source.data_restore_required:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_data_restore_unavailable",
                "该回滚需要数据恢复；ARC-07.6 authority 尚未就绪，已阻止指针切换。",
            )
        await self.source_store.verify(source)
        return request, source, plan

    async def _require_current_pause(self, request) -> None:
        latest = await self.control_store.latest(self.workspace_root)
        if not (
            latest is not None
            and latest.state is EvolutionRevalidationRolloutControlState.PAUSED
            and latest.event_id == request.pause_event_id
            and latest.event_sha256 == request.pause_event_sha256
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_pause_stale",
                "Rollback Request 绑定的 kill switch 已不再 current paused。",
            )

    async def _require_control_evidence(self, request) -> None:
        events = await self.control_store.history(self.workspace_root)
        if not any(
            event.event_id == request.pause_event_id
            and event.event_sha256 == request.pause_event_sha256
            and event.state is EvolutionRevalidationRolloutControlState.PAUSED
            for event in events
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_control_evidence_missing",
                "Rollback Request 绑定的 paused control evidence 无法验证。",
            )

    def _preconditions(self, request, source, plan):
        current = self.release_slot_store.active()
        if current is None or current.previous_slot_id is None:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_slot_unavailable",
                "Active pointer 没有可回滚的 previous slot。",
            )
        candidate = self.release_slot_store.inspect_installed_slot(current.current_slot_id)
        baseline = self.release_slot_store.inspect_installed_slot(current.previous_slot_id)
        if not (
            candidate.slot_sha256 == current.current_slot_sha256
            and candidate.source_commit == plan.target_head
            and candidate.source_tree_sha256 == plan.target_tree_sha256
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_candidate_slot_mismatch",
                "Current slot 不是 Rollout Plan 的 exact candidate。",
            )
        if not (
            baseline.slot_sha256 == current.previous_slot_sha256
            and baseline.source_commit == source.baseline_commit
            and baseline.source_tree_sha256 == source.baseline_tree_sha256
            and baseline.source_commit == request.rollback_plan.baseline_commit
            and baseline.source_tree_sha256 == request.rollback_plan.baseline_tree_sha256
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_baseline_slot_mismatch",
                "Previous slot 不是 immutable Rollback Source 的 exact baseline。",
            )
        return current, candidate, baseline

    def _validate_rollback_event(self, rollback, source, plan):
        authority = ReleaseRollbackAuthority(
            kind="evolution_revalidation_rollback_source",
            authority_id=source.source_id,
            authority_sha256=source.source_sha256,
        )
        if rollback.rollback_authority != authority or rollback.generation <= 1:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_event_mismatch",
                "Rollback event 未绑定 exact Source authority。",
            )
        expected = self.release_slot_store.get_activation_event(rollback.generation - 1)
        if expected is None:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_prior_pointer_missing",
                "Rollback event 缺少 previous activation event。",
            )
        candidate = self.release_slot_store.inspect_installed_slot(expected.current_slot_id)
        baseline_id = expected.previous_slot_id
        if baseline_id is None:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_prior_baseline_missing",
                "Rollback event 的 prior pointer 没有 baseline slot。",
            )
        baseline = self.release_slot_store.inspect_installed_slot(baseline_id)
        if expected.pointer_sha256 != rollback.previous_pointer_sha256:
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_chain_mismatch",
                "Rollback event hash-chain 不一致。",
            )
        if not (
            candidate.source_commit == plan.target_head
            and candidate.source_tree_sha256 == plan.target_tree_sha256
            and baseline.source_commit == source.baseline_commit
            and baseline.source_tree_sha256 == source.baseline_tree_sha256
            and rollback.current_slot_id == baseline.slot_id
            and rollback.previous_slot_id == candidate.slot_id
        ):
            raise EvolutionRevalidationRollbackExecutionError(
                "rollback_execution_recovery_mismatch",
                "Recovered rollback event 的 candidate/baseline provenance 不一致。",
            )
        return expected, candidate, baseline


def _build_receipt(
    *,
    workspace_root,
    request: EvolutionRevalidationRollbackRequest,
    source: EvolutionRevalidationRollbackSource,
    plan: EvolutionRevalidationRolloutPlan,
    candidate,
    baseline,
    expected,
    rollback,
    boot,
    launch,
) -> EvolutionRevalidationRollbackExecutionReceipt:
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_EXECUTION_POLICY,
        "workspace_root": str(workspace_root),
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "source_id": source.source_id,
        "source_sha256": source.source_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "pause_event_id": request.pause_event_id,
        "pause_event_sha256": request.pause_event_sha256,
        "candidate_slot": candidate.model_dump(mode="json"),
        "baseline_slot": baseline.model_dump(mode="json"),
        "expected_pointer": expected.model_dump(mode="json"),
        "rollback_pointer": rollback.model_dump(mode="json"),
        "rollback_boot_receipt": boot.model_dump(mode="json"),
        "launch_resolution": launch.model_dump(mode="json"),
        "data_restore_required": False,
        "exact_candidate_slot_verified": True,
        "exact_baseline_slot_verified": True,
        "kill_switch_paused": True,
        "expected_pointer_cas_satisfied": True,
        "release_pointer_switched": True,
        "rollback_executed": True,
        "post_rollback_launch_verified": True,
        "process_started": False,
        "user_process_started": False,
        "workspace_write_executed": False,
        "git_write_executed": False,
        "outcome_recorded": False,
        "promotion_authority": False,
        "completed_at": rollback.activated_at,
    }
    digest = _digest(core)
    return EvolutionRevalidationRollbackExecutionReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrerollbackexec_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def render_revalidation_rollback_execution(
    view: EvolutionRevalidationRollbackExecutionView,
) -> str:
    item = view.receipt
    state = "可验证" if view.rollback_fact_authority else "证据已失效"
    active = "当前生效" if view.active_baseline_authority else "历史回滚"
    return "\n".join(
        (
            "## 受控版本槽回滚",
            "",
            f"- 状态：`{state}` · `{active}`",
            f"- 回执：`{item.receipt_id}`",
            f"- Request / Source：`{item.request_id}` / `{item.source_id}`",
            f"- Candidate → Baseline：`{item.candidate_slot.version}` → "
            f"`{item.baseline_slot.version}`",
            f"- Pointer generation：{item.expected_pointer.generation} → "
            f"{item.rollback_pointer.generation}",
            f"- 启动解析：`{item.launch_resolution.resolution_id}`",
            "- 用户进程：未启动；工作区与 Git：未修改",
            "- Outcome：尚未回注 HAR-09.6；本回执不授予 promotion 权限",
        )
    )


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollback Execution 时间必须包含 offset。")
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


def _restore_receipt(encoded: str) -> EvolutionRevalidationRollbackExecutionReceipt:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationRollbackExecutionError(
            "rollback_execution_artifact_oversized",
            "持久化 Rollback Execution Receipt 超过 2 MiB。",
        )
    return EvolutionRevalidationRollbackExecutionReceipt.model_validate_json(encoded)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollback_executions ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL UNIQUE, "
        "plan_id TEXT NOT NULL, receipt_json TEXT NOT NULL, completed_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLBACK_EXECUTION_POLICY",
    "EvolutionRevalidationRollbackExecutionError",
    "EvolutionRevalidationRollbackExecutionReceipt",
    "EvolutionRevalidationRollbackExecutionService",
    "EvolutionRevalidationRollbackExecutionStore",
    "EvolutionRevalidationRollbackExecutionView",
    "render_revalidation_rollback_execution",
]
