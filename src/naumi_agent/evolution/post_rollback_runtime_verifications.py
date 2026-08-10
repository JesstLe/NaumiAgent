"""Fresh installed-runtime verification after an authority-bound rollback."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollback_executions import (
    EvolutionRevalidationRollbackExecutionError,
    EvolutionRevalidationRollbackExecutionReceipt,
    EvolutionRevalidationRollbackExecutionService,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
)
from naumi_agent.release.launcher import (
    ReleaseLaunchResolution,
    resolve_and_record_launch,
)
from naumi_agent.release.slots import (
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_POST_ROLLBACK_RUNTIME_VERIFICATION_POLICY = (
    "evolution-post-rollback-runtime-verification-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRuntimeVerification(_StrictModel):
    """Fresh boot and launcher identity evidence, not a behavioral evaluation."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-runtime-verification-v1"] = (
        EVOLUTION_POST_ROLLBACK_RUNTIME_VERIFICATION_POLICY
    )
    verification_id: str = Field(pattern=r"^evpostrollback_[0-9a-f]{24}$")
    verification_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    verification_kind: Literal["installed_runtime_recovery"] = (
        "installed_runtime_recovery"
    )
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    rollback_receipt_id: str = Field(pattern=r"^evrerollbackexec_[0-9a-f]{24}$")
    rollback_receipt_sha256: str = Field(pattern=_SHA256_RE)
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    baseline_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_manifest_sha256: str = Field(pattern=_SHA256_RE)
    baseline_version: str = Field(min_length=1, max_length=128)
    baseline_target: str = Field(min_length=1, max_length=128)
    rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    rollback_pointer_sha256: str = Field(pattern=_SHA256_RE)
    rollback_pointer_generation: int = Field(ge=1)
    runtime_identity_sha256: str = Field(pattern=_SHA256_RE)
    fresh_boot_receipt: ReleaseSlotBootReceipt
    fresh_launch_resolution: ReleaseLaunchResolution
    recovery_checks: tuple[
        Literal["active_pointer"],
        Literal["immutable_bundle"],
        Literal["backend_binary"],
        Literal["version_output"],
        Literal["launch_resolution"],
    ] = (
        "active_pointer",
        "immutable_bundle",
        "backend_binary",
        "version_output",
        "launch_resolution",
    )
    fresh_boot_probe_executed: Literal[True] = True
    active_launch_resolved: Literal[True] = True
    runtime_identity_recovered: Literal[True] = True
    post_rollback_verification_recorded: Literal[True] = True
    post_rollback_evaluation_recorded: Literal[True] = True
    behavioral_evaluation_recorded: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    verified_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self):
        if self.schema_version != 1:
            raise ValueError("Post-Rollback Verification schema_version 无效。")
        if self.policy_version != EVOLUTION_POST_ROLLBACK_RUNTIME_VERIFICATION_POLICY:
            raise ValueError("Post-Rollback Verification policy_version 无效。")
        if self.verification_kind != "installed_runtime_recovery":
            raise ValueError("Post-Rollback Verification kind 无效。")
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Post-Rollback Verification workspace 必须 canonical。")
        if any(char in self.workspace_root for char in ("\x00", "\r", "\n")):
            raise ValueError("Post-Rollback Verification workspace 包含不安全字符。")
        expected_checks = (
            "active_pointer",
            "immutable_bundle",
            "backend_binary",
            "version_output",
            "launch_resolution",
        )
        if self.recovery_checks != expected_checks:
            raise ValueError("Post-Rollback Verification checks 不完整。")
        if not all((
            self.fresh_boot_probe_executed,
            self.active_launch_resolved,
            self.runtime_identity_recovered,
            self.post_rollback_verification_recorded,
            self.post_rollback_evaluation_recorded,
        )):
            raise ValueError("Post-Rollback Verification 完成状态不完整。")
        if any((
            self.behavioral_evaluation_recorded,
            self.long_term_metrics_recorded,
            self.learning_authority,
            self.promotion_authority,
        )):
            raise ValueError("Post-Rollback Verification 不得越权。")
        boot = self.fresh_boot_receipt
        launch = self.fresh_launch_resolution
        if not (
            boot.slot_id == self.baseline_slot_id
            and boot.slot_sha256 == self.baseline_slot_sha256
            and boot.manifest_sha256 == self.baseline_manifest_sha256
            and launch.pointer_id == self.rollback_pointer_id
            and launch.pointer_sha256 == self.rollback_pointer_sha256
            and launch.pointer_generation == self.rollback_pointer_generation
            and launch.slot_id == self.baseline_slot_id
            and launch.slot_sha256 == self.baseline_slot_sha256
            and launch.version == self.baseline_version
            and launch.target == self.baseline_target
            and launch.binary_sha256 == boot.binary_sha256
            and not launch.process_start_requested
            and not launch.process_start_authority
            and not launch.process_started
        ):
            raise ValueError("Post-Rollback boot/launch runtime identity 不一致。")
        identity = _runtime_identity(
            slot_id=self.baseline_slot_id,
            slot_sha256=self.baseline_slot_sha256,
            manifest_sha256=self.baseline_manifest_sha256,
            version=self.baseline_version,
            target=self.baseline_target,
            pointer_sha256=self.rollback_pointer_sha256,
            pointer_generation=self.rollback_pointer_generation,
            binary_sha256=boot.binary_sha256,
        )
        if not hmac.compare_digest(self.runtime_identity_sha256, identity):
            raise ValueError("Post-Rollback runtime identity 摘要不一致。")
        verified = max(_aware(boot.checked_at), _aware(launch.resolved_at))
        if _aware(self.verified_at) != verified:
            raise ValueError("Post-Rollback verified_at 与 fresh probes 不一致。")
        core = self.model_dump(
            mode="json",
            exclude={"verification_id", "verification_sha256"},
        )
        digest = _digest(core)
        if not hmac.compare_digest(self.verification_sha256, digest):
            raise ValueError("Post-Rollback Verification 摘要不一致。")
        if self.verification_id != f"evpostrollback_{digest[:24]}":
            raise ValueError("Post-Rollback Verification identity 不一致。")
        return self


class EvolutionPostRollbackRuntimeVerificationView(_StrictModel):
    verification: EvolutionPostRollbackRuntimeVerification
    durable_dependencies_valid: bool
    rollback_fact_authority: bool
    fresh_boot_authority: bool
    fresh_launch_authority: bool
    verification_authority: bool
    active_baseline_authority: bool
    behavioral_evaluation_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _authority(self):
        expected = bool(
            self.durable_dependencies_valid
            and self.rollback_fact_authority
            and self.fresh_boot_authority
            and self.fresh_launch_authority
        )
        if self.verification_authority is not expected:
            raise ValueError("Post-Rollback Verification authority 投影不一致。")
        if self.active_baseline_authority and not expected:
            raise ValueError("Post-Rollback active baseline authority 越界。")
        if any((
            self.behavioral_evaluation_authority,
            self.long_term_metrics_authority,
            self.learning_authority,
            self.promotion_authority,
        )):
            raise ValueError("Post-Rollback Verification view 不得越权。")
        return self


class EvolutionPostRollbackRuntimeVerificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRuntimeVerificationBuilder:
    def build(
        self,
        *,
        outcome: EvolutionRevalidationRollbackOutcome,
        rollback: EvolutionRevalidationRollbackExecutionReceipt,
        fresh_boot: ReleaseSlotBootReceipt,
        fresh_launch: ReleaseLaunchResolution,
    ) -> EvolutionPostRollbackRuntimeVerification:
        try:
            typed_outcome = EvolutionRevalidationRollbackOutcome.model_validate_json(
                outcome.model_dump_json()
            )
            typed_rollback = (
                EvolutionRevalidationRollbackExecutionReceipt.model_validate_json(
                    rollback.model_dump_json()
                )
            )
            boot = ReleaseSlotBootReceipt.model_validate_json(fresh_boot.model_dump_json())
            launch = ReleaseLaunchResolution.model_validate_json(
                fresh_launch.model_dump_json()
            )
            _validate_lineage(typed_outcome, typed_rollback)
            if min(_aware(boot.checked_at), _aware(launch.resolved_at)) < _aware(
                typed_outcome.recorded_at
            ):
                raise ValueError("Fresh probes 早于 Rollback Outcome。")
            baseline = typed_rollback.baseline_slot
            pointer = typed_rollback.rollback_pointer
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_POST_ROLLBACK_RUNTIME_VERIFICATION_POLICY,
                "workspace_root": typed_outcome.workspace_root,
                "verification_kind": "installed_runtime_recovery",
                "outcome_id": typed_outcome.outcome_id,
                "outcome_sha256": typed_outcome.outcome_sha256,
                "request_id": typed_outcome.request_id,
                "rollback_receipt_id": typed_rollback.receipt_id,
                "rollback_receipt_sha256": typed_rollback.receipt_sha256,
                "workbench_session_id": typed_outcome.workbench_session_id,
                "workbench_proposal_id": typed_outcome.workbench_proposal_id,
                "experiment_contract_id": typed_outcome.experiment_contract_id,
                "candidate_id": typed_outcome.candidate_id,
                "candidate_revision": typed_outcome.candidate_revision,
                "baseline_slot_id": baseline.slot_id,
                "baseline_slot_sha256": baseline.slot_sha256,
                "baseline_manifest_sha256": baseline.manifest_sha256,
                "baseline_version": baseline.version,
                "baseline_target": baseline.target,
                "rollback_pointer_id": pointer.pointer_id,
                "rollback_pointer_sha256": pointer.pointer_sha256,
                "rollback_pointer_generation": pointer.generation,
                "runtime_identity_sha256": _runtime_identity(
                    slot_id=baseline.slot_id,
                    slot_sha256=baseline.slot_sha256,
                    manifest_sha256=baseline.manifest_sha256,
                    version=baseline.version,
                    target=baseline.target,
                    pointer_sha256=pointer.pointer_sha256,
                    pointer_generation=pointer.generation,
                    binary_sha256=boot.binary_sha256,
                ),
                "fresh_boot_receipt": boot.model_dump(mode="json"),
                "fresh_launch_resolution": launch.model_dump(mode="json"),
                "recovery_checks": [
                    "active_pointer",
                    "immutable_bundle",
                    "backend_binary",
                    "version_output",
                    "launch_resolution",
                ],
                "fresh_boot_probe_executed": True,
                "active_launch_resolved": True,
                "runtime_identity_recovered": True,
                "post_rollback_verification_recorded": True,
                "post_rollback_evaluation_recorded": True,
                "behavioral_evaluation_recorded": False,
                "long_term_metrics_recorded": False,
                "learning_authority": False,
                "promotion_authority": False,
                "verified_at": max(
                    _aware(boot.checked_at), _aware(launch.resolved_at)
                ).isoformat(),
            }
            digest = _digest(core)
            return EvolutionPostRollbackRuntimeVerification.model_validate({
                **core,
                "verification_id": f"evpostrollback_{digest[:24]}",
                "verification_sha256": digest,
            })
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_verification_invalid",
                "Post-Rollback Runtime Verification 无法验证。",
            ) from exc


class EvolutionPostRollbackRuntimeVerificationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        verification: EvolutionPostRollbackRuntimeVerification,
    ) -> EvolutionPostRollbackRuntimeVerification:
        try:
            item = EvolutionPostRollbackRuntimeVerification.model_validate_json(
                verification.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_verification_invalid",
                "Post-Rollback Runtime Verification artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_verification_oversized",
                "Post-Rollback Runtime Verification 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                row = await (
                    await db.execute(
                        "SELECT verification_json FROM "
                        "evolution_post_rollback_runtime_verifications "
                        "WHERE outcome_id = ?",
                        (item.outcome_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _restore(row["verification_json"])
                    await db.rollback()
                    if not _same_lineage(restored, item):
                        raise EvolutionPostRollbackRuntimeVerificationError(
                            "post_rollback_verification_conflict",
                            "同一 Outcome 已绑定不同 Post-Rollback lineage。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_post_rollback_runtime_verifications "
                    "(verification_id, verification_sha256, outcome_id, request_id, "
                    "workbench_session_id, workbench_proposal_id, verification_json, "
                    "verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.verification_id,
                        item.verification_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.workbench_session_id,
                        item.workbench_proposal_id,
                        encoded,
                        item.verified_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackRuntimeVerificationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_verification_store_error",
                "Post-Rollback Runtime Verification 无法持久化。",
            ) from exc
        restored = await self.get_by_outcome(item.outcome_id)
        assert restored is not None
        return restored

    async def get_by_outcome(
        self,
        outcome_id: str,
    ) -> EvolutionPostRollbackRuntimeVerification | None:
        if re.fullmatch(r"evrerollbackout_[0-9a-f]{24}", str(outcome_id)) is None:
            raise ValueError("Outcome ID 格式无效。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT verification_json FROM "
                        "evolution_post_rollback_runtime_verifications "
                        "WHERE outcome_id = ?",
                        (outcome_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["verification_json"])
        except EvolutionPostRollbackRuntimeVerificationError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_verification_store_corrupt",
                "Post-Rollback Runtime Verification 损坏或无法读取。",
            ) from exc


class EvolutionPostRollbackRuntimeVerificationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationRollbackOutcomeService,
        rollback_service: EvolutionRevalidationRollbackExecutionService,
        release_slot_store: ReleaseSlotStore,
        store: EvolutionPostRollbackRuntimeVerificationStore,
        builder: EvolutionPostRollbackRuntimeVerificationBuilder | None = None,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.outcome_service = outcome_service
        self.rollback_service = rollback_service
        self.release_slot_store = release_slot_store
        self.store = store
        self.builder = builder or EvolutionPostRollbackRuntimeVerificationBuilder()
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
    ) -> EvolutionPostRollbackRuntimeVerificationView:
        normalized = _request_id(request_id)
        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            outcome, rollback, _ = await self._sources(normalized, require_active=True)
            existing = await self.store.get_by_outcome(outcome.outcome_id)
            if existing is not None:
                view = await self.inspect(verification=existing)
                if not view.verification_authority:
                    raise EvolutionPostRollbackRuntimeVerificationError(
                        "post_rollback_verification_stale",
                        "既有 Post-Rollback Runtime Verification authority 已失效。",
                    )
                return view
            try:
                fresh_boot = await asyncio.to_thread(
                    self.release_slot_store.verify_bootable,
                    rollback.baseline_slot.slot_id,
                    checked_at=self.now(),
                )
                fresh_launch = await asyncio.to_thread(
                    resolve_and_record_launch,
                    self.release_slot_store,
                    argument_count=0,
                    process_start_requested=False,
                    resolved_at=self.now(),
                )
            except (OSError, RuntimeError, TypeError, ValueError, ReleaseSlotError) as exc:
                raise EvolutionPostRollbackRuntimeVerificationError(
                    "post_rollback_fresh_probe_failed",
                    "回滚后 runtime fresh boot/launch 探针失败。",
                ) from exc
            refreshed_outcome, refreshed_rollback, _ = await self._sources(
                normalized,
                require_active=True,
            )
            if (refreshed_outcome, refreshed_rollback) != (outcome, rollback):
                raise EvolutionPostRollbackRuntimeVerificationError(
                    "post_rollback_authority_changed",
                    "Post-Rollback authority 在 fresh probes 期间发生变化。",
                )
            item = self.builder.build(
                outcome=outcome,
                rollback=rollback,
                fresh_boot=fresh_boot,
                fresh_launch=fresh_launch,
            )
            recorded = await self.store.record(item)
            view = await self.inspect(verification=recorded)
            if not view.verification_authority:
                raise EvolutionPostRollbackRuntimeVerificationError(
                    "post_rollback_authority_changed",
                    "Post-Rollback authority 在持久化期间发生变化，已失败关闭。",
                )
            return view

    async def inspect(
        self,
        *,
        verification: EvolutionPostRollbackRuntimeVerification,
    ) -> EvolutionPostRollbackRuntimeVerificationView:
        item = EvolutionPostRollbackRuntimeVerification.model_validate_json(
            verification.model_dump_json()
        )
        durable = rollback_authority = boot_authority = launch_authority = False
        active = False
        try:
            outcome, rollback, active = await self._sources(
                item.request_id,
                require_active=False,
            )
            rebuilt = self.builder.build(
                outcome=outcome,
                rollback=rollback,
                fresh_boot=item.fresh_boot_receipt,
                fresh_launch=item.fresh_launch_resolution,
            )
            durable = rebuilt == item
            booted = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                item.baseline_slot_id,
                item.fresh_boot_receipt.receipt_id,
            )
            boot_authority = bool(
                booted.slot == rollback.baseline_slot
                and booted.boot_receipt == item.fresh_boot_receipt
            )
            stored_launch = await asyncio.to_thread(
                self.release_slot_store.get_launch_resolution,
                item.fresh_launch_resolution.resolution_id,
            )
            launch_authority = stored_launch == item.fresh_launch_resolution
            rollback_authority = True
        except (
            EvolutionPostRollbackRuntimeVerificationError,
            EvolutionRevalidationRollbackExecutionError,
            EvolutionRevalidationRollbackOutcomeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            ReleaseSlotError,
        ):
            pass
        authority = bool(
            durable and rollback_authority and boot_authority and launch_authority
        )
        return EvolutionPostRollbackRuntimeVerificationView(
            verification=item,
            durable_dependencies_valid=durable,
            rollback_fact_authority=rollback_authority,
            fresh_boot_authority=boot_authority,
            fresh_launch_authority=launch_authority,
            verification_authority=authority,
            active_baseline_authority=bool(authority and active),
            behavioral_evaluation_authority=False,
            long_term_metrics_authority=False,
            learning_authority=False,
            promotion_authority=False,
        )

    async def _sources(self, request_id: str, *, require_active: bool):
        try:
            outcome_view = await self.outcome_service.inspect(request_id=request_id)
            rollback_view = await self.rollback_service.inspect(request_id=request_id)
            _validate_lineage(outcome_view.outcome, rollback_view.receipt)
        except (
            EvolutionRevalidationRollbackExecutionError,
            EvolutionRevalidationRollbackOutcomeError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_source_invalid",
                "Post-Rollback durable source 不完整、越界或已损坏。",
            ) from exc
        if not outcome_view.outcome_authority or not rollback_view.rollback_fact_authority:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_source_stale",
                "Rollback Outcome 或 Execution authority 当前无效。",
            )
        active = bool(
            outcome_view.active_baseline_authority
            and rollback_view.active_baseline_authority
        )
        if require_active and not active:
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_baseline_not_active",
                "Baseline slot 当前不是 active runtime，不能执行恢复验证。",
            )
        return outcome_view.outcome, rollback_view.receipt, active


def render_post_rollback_runtime_verification(
    view: EvolutionPostRollbackRuntimeVerificationView,
) -> str:
    item = view.verification
    return "\n".join([
        f"# Post-Rollback Runtime Verification `{item.verification_id}`",
        "",
        "**已在回滚后的 active baseline 上重新执行受控启动探针并解析 launcher identity。**",
        "",
        f"- Proposal：`{item.workbench_proposal_id}`",
        f"- Outcome：`{item.outcome_id}`",
        f"- Baseline slot：`{item.baseline_slot_id}` · {item.baseline_version}",
        f"- Target：`{item.baseline_target}`",
        f"- Runtime identity：`{item.runtime_identity_sha256}`",
        f"- Fresh Boot Receipt：`{item.fresh_boot_receipt.receipt_id}` · exit 0",
        f"- Fresh Launch Resolution：`{item.fresh_launch_resolution.resolution_id}`",
        f"- Verification authority：{'有效' if view.verification_authority else '无效'}",
        f"- Baseline 当前 active：{'是' if view.active_baseline_authority else '否'}",
        "",
        "- 恢复评测：已记录（installed-runtime mechanical verification）",
        "- 行为级 Eval：尚未记录",
        "- 长期指标：尚未记录",
        "- 权限：不授予 policy learning 或 promotion authority",
    ])


def _validate_lineage(
    outcome: EvolutionRevalidationRollbackOutcome,
    rollback: EvolutionRevalidationRollbackExecutionReceipt,
) -> None:
    if not (
        outcome.workspace_root == rollback.workspace_root
        and outcome.request_id == rollback.request_id
        and outcome.request_sha256 == rollback.request_sha256
        and outcome.rollback_receipt_id == rollback.receipt_id
        and outcome.rollback_receipt_sha256 == rollback.receipt_sha256
        and outcome.plan_id == rollback.plan_id
        and outcome.plan_sha256 == rollback.plan_sha256
        and outcome.candidate_slot_id == rollback.candidate_slot.slot_id
        and outcome.candidate_slot_sha256 == rollback.candidate_slot.slot_sha256
        and outcome.baseline_slot_id == rollback.baseline_slot.slot_id
        and outcome.baseline_slot_sha256 == rollback.baseline_slot.slot_sha256
        and rollback.rollback_pointer.current_slot_id == outcome.baseline_slot_id
        and rollback.rollback_pointer.current_slot_sha256
        == outcome.baseline_slot_sha256
    ):
        raise ValueError("Post-Rollback Outcome/Execution lineage 不一致。")
    if _aware(rollback.completed_at) > _aware(outcome.recorded_at):
        raise ValueError("Post-Rollback Outcome 早于 Rollback Execution。")


def _same_lineage(
    left: EvolutionPostRollbackRuntimeVerification,
    right: EvolutionPostRollbackRuntimeVerification,
) -> bool:
    return all((
        left.workspace_root == right.workspace_root,
        left.outcome_id == right.outcome_id,
        left.outcome_sha256 == right.outcome_sha256,
        left.request_id == right.request_id,
        left.rollback_receipt_id == right.rollback_receipt_id,
        left.rollback_receipt_sha256 == right.rollback_receipt_sha256,
        left.workbench_session_id == right.workbench_session_id,
        left.workbench_proposal_id == right.workbench_proposal_id,
        left.baseline_slot_id == right.baseline_slot_id,
        left.baseline_slot_sha256 == right.baseline_slot_sha256,
        left.rollback_pointer_sha256 == right.rollback_pointer_sha256,
        left.runtime_identity_sha256 == right.runtime_identity_sha256,
    ))


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_runtime_verifications ("
        "verification_id TEXT PRIMARY KEY, verification_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
        "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL UNIQUE, "
        "verification_json TEXT NOT NULL, verified_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_post_rollback_session "
        "ON evolution_post_rollback_runtime_verifications"
        "(workbench_session_id, verified_at)"
    )
    await db.commit()


async def _require_dependencies(
    db: aiosqlite.Connection,
    item: EvolutionPostRollbackRuntimeVerification,
) -> None:
    checks = (
        (
            "SELECT outcome_sha256 FROM evolution_revalidation_rollback_outcomes "
            "WHERE outcome_id = ?",
            item.outcome_id,
            item.outcome_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_revalidation_rollback_executions "
            "WHERE receipt_id = ?",
            item.rollback_receipt_id,
            item.rollback_receipt_sha256,
        ),
    )
    for statement, identity, expected in checks:
        row = await (await db.execute(statement, (identity,))).fetchone()
        if row is None or not hmac.compare_digest(str(row[0]), expected):
            raise EvolutionPostRollbackRuntimeVerificationError(
                "post_rollback_dependency_mismatch",
                "Post-Rollback Runtime Verification dependency 缺失或不一致。",
            )


def _restore(raw: object) -> EvolutionPostRollbackRuntimeVerification:
    encoded = str(raw)
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionPostRollbackRuntimeVerificationError(
            "post_rollback_verification_oversized",
            "Post-Rollback Runtime Verification 超过 512 KiB。",
        )
    try:
        return EvolutionPostRollbackRuntimeVerification.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRuntimeVerificationError(
            "post_rollback_verification_store_corrupt",
            "Post-Rollback Runtime Verification 损坏或无法验证。",
        ) from exc


def _request_id(value: object) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", normalized) is None:
        raise EvolutionPostRollbackRuntimeVerificationError(
            "post_rollback_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _runtime_identity(**payload) -> str:
    return _digest(payload)


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_RUNTIME_VERIFICATION_POLICY",
    "EvolutionPostRollbackRuntimeVerification",
    "EvolutionPostRollbackRuntimeVerificationBuilder",
    "EvolutionPostRollbackRuntimeVerificationError",
    "EvolutionPostRollbackRuntimeVerificationService",
    "EvolutionPostRollbackRuntimeVerificationStore",
    "EvolutionPostRollbackRuntimeVerificationView",
    "render_post_rollback_runtime_verification",
]
