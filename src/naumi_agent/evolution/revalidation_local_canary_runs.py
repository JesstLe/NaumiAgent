"""Execute one real local-canary run with a crash-recoverable state journal."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutStageEntryReceipt,
    EvolutionRevalidationRolloutStageEntryService,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionError,
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_REVALIDATION_LOCAL_CANARY_RUN_POLICY = (
    "evolution-revalidation-local-canary-run-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class EvolutionRevalidationLocalCanaryState(StrEnum):
    ADMITTED = "admitted"
    SOURCE_VERIFIED = "source_verified"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationLocalCanaryCheckEvidence(_StrictModel):
    check_id: str = Field(min_length=1, max_length=128)
    status: HarnessSandboxCheckStatus
    run_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    lifecycle_receipt_sha256: str = Field(pattern=_SHA256_RE)
    source_tree_sha256: str = Field(pattern=_SHA256_RE)
    snapshot_manifest_sha256: str = Field(pattern=_SHA256_RE)
    profile_digest: str = Field(pattern=_SHA256_RE)
    exit_code: int
    duration_ms: int = Field(ge=0)


class EvolutionRevalidationLocalCanaryEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-local-canary-run-v1"] = (
        EVOLUTION_REVALIDATION_LOCAL_CANARY_RUN_POLICY
    )
    event_id: str = Field(pattern=r"^evrecanaryevent_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    run_id: str = Field(pattern=r"^evrecanaryrun_[0-9a-f]{24}$")
    sequence: int = Field(ge=1, le=20)
    previous_event_id: str = Field(
        default="", pattern=r"^(?:|evrecanaryevent_[0-9a-f]{24})$"
    )
    previous_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    entry_receipt_id: str = Field(pattern=r"^evrerolloutentry_[0-9a-f]{24}$")
    entry_receipt_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    run_index: int = Field(ge=0, le=99)
    state: EvolutionRevalidationLocalCanaryState
    source_snapshot_id: str = Field(
        default="", pattern=r"^(?:|evrevalsrc_[0-9a-f]{24})$"
    )
    source_snapshot_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_grant_id: str = Field(default="", max_length=128)
    run_grant_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    checks: tuple[EvolutionRevalidationLocalCanaryCheckEvidence, ...] = Field(
        max_length=80
    )
    error_code: str = Field(default="", max_length=128)
    error_message: str = Field(default="", max_length=300)
    observed_at: str = Field(min_length=1, max_length=100)
    terminal: bool
    canary_passed: bool
    monitor_authority: bool
    opt_in_authority: Literal[False] = False
    percentage_authority: Literal[False] = False
    stable_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Local canary workspace 必须 canonical。")
        if (self.sequence == 1) is bool(self.previous_event_id):
            raise ValueError("Local canary event chain 不一致。")
        if bool(self.previous_event_id) is not bool(self.previous_event_sha256):
            raise ValueError("Local canary previous digest 不一致。")
        _aware(self.observed_at)
        has_source = bool(self.source_snapshot_id)
        if has_source is not bool(self.source_snapshot_sha256 and self.source_tree_sha256):
            raise ValueError("Local canary source evidence 不完整。")
        has_grant = bool(self.run_grant_id)
        if has_grant is not bool(self.run_grant_sha256):
            raise ValueError("Local canary Run Grant evidence 不完整。")
        terminal = self.state in {
            EvolutionRevalidationLocalCanaryState.PASSED,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        }
        if not (
            self.terminal is terminal
            and self.canary_passed
            is (self.state is EvolutionRevalidationLocalCanaryState.PASSED)
            and self.monitor_authority is terminal
        ):
            raise ValueError("Local canary terminal projection 不一致。")
        if self.state is EvolutionRevalidationLocalCanaryState.ADMITTED and (
            has_source or has_grant or self.checks or self.error_code
        ):
            raise ValueError("Admitted canary 不得携带执行证据。")
        if self.state is EvolutionRevalidationLocalCanaryState.SOURCE_VERIFIED and (
            not has_source or has_grant or self.checks or self.error_code
        ):
            raise ValueError("Source-verified canary evidence 无效。")
        if self.state is EvolutionRevalidationLocalCanaryState.RUNNING and (
            not has_source or not has_grant or self.checks or self.error_code
        ):
            raise ValueError("Running canary evidence 无效。")
        if self.state is EvolutionRevalidationLocalCanaryState.PASSED and (
            not self.checks
            or any(
                item.status is not HarnessSandboxCheckStatus.PASSED
                for item in self.checks
            )
            or self.error_code
        ):
            raise ValueError("Passed canary evidence 无效。")
        if self.state is EvolutionRevalidationLocalCanaryState.FAILED and not (
            self.error_code or any(item.status != "passed" for item in self.checks)
        ):
            raise ValueError("Failed canary 缺少失败证据。")
        if self.state is EvolutionRevalidationLocalCanaryState.CANCELLED and not self.error_code:
            raise ValueError("Cancelled canary 缺少原因。")
        core = self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        digest = _digest(core)
        if self.event_sha256 != digest or self.event_id != f"evrecanaryevent_{digest[:24]}":
            raise ValueError("Local canary event identity 不一致。")
        return self


class EvolutionRevalidationLocalCanaryRunView(_StrictModel):
    event: EvolutionRevalidationLocalCanaryEvent
    run_complete: bool
    passed: bool
    monitor_authority: bool
    later_stage_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if not (
            self.run_complete is self.event.terminal
            and self.passed is self.event.canary_passed
            and self.monitor_authority is self.event.monitor_authority
        ):
            raise ValueError("Local canary view 投影不一致。")
        return self


class EvolutionRevalidationLocalCanaryRunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationLocalCanaryJournalStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def latest_for_run(self, run_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_local_canary_events "
                    "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                    (run_id,),
                )
            ).fetchone()
        return None if row is None else _event(row["event_json"])

    async def latest_for_entry(self, entry_receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_local_canary_events "
                    "WHERE entry_receipt_id = ? ORDER BY run_index DESC, sequence DESC LIMIT 1",
                    (entry_receipt_id,),
                )
            ).fetchone()
        return None if row is None else _event(row["event_json"])

    async def admit(self, entry, *, observed_at: str):
        receipt = EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
            entry.model_dump_json()
        )
        timestamp = _aware(observed_at).isoformat()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_entry(db, receipt)
            active_row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_local_canary_events e "
                    "WHERE e.entry_receipt_id = ? AND e.sequence = ("
                    "SELECT MAX(x.sequence) FROM evolution_revalidation_local_canary_events x "
                    "WHERE x.run_id = e.run_id) AND e.terminal = 0 "
                    "ORDER BY e.run_index LIMIT 1",
                    (receipt.receipt_id,),
                )
            ).fetchone()
            if active_row is not None:
                await db.rollback()
                return _event(active_row["event_json"])
            row = await (
                await db.execute(
                    "SELECT MAX(run_index) AS maximum FROM "
                    "evolution_revalidation_local_canary_events "
                    "WHERE entry_receipt_id = ?",
                    (receipt.receipt_id,),
                )
            ).fetchone()
            run_index = 0 if row["maximum"] is None else int(row["maximum"]) + 1
            if run_index >= receipt.max_completed_runs or run_index > 99:
                await db.rollback()
                raise EvolutionRevalidationLocalCanaryRunError(
                    "local_canary_run_budget_exhausted",
                    "Local canary 已达到 stage-entry run 上限。",
                )
            run_id = _run_id(receipt.receipt_sha256, run_index)
            item = _build_event(
                entry=receipt,
                run_id=run_id,
                run_index=run_index,
                state=EvolutionRevalidationLocalCanaryState.ADMITTED,
                previous=None,
                observed_at=timestamp,
            )
            await _insert(db, item)
            await db.commit()
        return item

    async def transition(self, event):
        item = EvolutionRevalidationLocalCanaryEvent.model_validate_json(
            event.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationLocalCanaryRunError(
                "local_canary_event_oversized", "Local canary event 过大。"
            )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            entry = await _require_entry_id(db, item.entry_receipt_id)
            if not (
                entry.receipt_sha256 == item.entry_receipt_sha256
                and entry.plan_id == item.plan_id
                and entry.plan_sha256 == item.plan_sha256
                and entry.contract_id == item.contract_id
            ):
                await db.rollback()
                raise EvolutionRevalidationLocalCanaryRunError(
                    "local_canary_entry_mismatch", "Local canary entry 依赖不一致。"
                )
            latest_row = await (
                await db.execute(
                    "SELECT event_json FROM evolution_revalidation_local_canary_events "
                    "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                    (item.run_id,),
                )
            ).fetchone()
            if latest_row is None:
                await db.rollback()
                raise EvolutionRevalidationLocalCanaryRunError(
                    "local_canary_journal_missing", "Local canary journal 缺少 admission。"
                )
            latest = _event(latest_row["event_json"])
            if latest.event_id == item.event_id:
                await db.rollback()
                return latest
            if not (
                item.sequence == latest.sequence + 1
                and item.previous_event_id == latest.event_id
                and item.previous_event_sha256 == latest.event_sha256
                and _transition_allowed(latest.state, item.state)
            ):
                await db.rollback()
                raise EvolutionRevalidationLocalCanaryRunError(
                    "local_canary_journal_race", "Local canary journal chain 已变化。"
                )
            await _insert(db, item)
            await db.commit()
        return item


class EvolutionRevalidationLocalCanaryExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        entry_service: EvolutionRevalidationRolloutStageEntryService,
        contract_service: EvolutionRevalidationRuntimeContractService,
        source_service: EvolutionRevalidationRuntimeSourceService,
        profile_service: HarnessService,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        harness_store: HarnessStore,
        sandbox_eval_kernel: HarnessSandboxEvalExecutionKernel,
        journal_store: EvolutionRevalidationLocalCanaryJournalStore,
        now=None,
        runner_id=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.entry_service = entry_service
        self.contract_service = contract_service
        self.source_service = source_service
        self.profile_service = profile_service
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.harness_store = harness_store
        self.sandbox_eval_kernel = sandbox_eval_kernel
        self.journal_store = journal_store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.runner_id = runner_id or f"canary-runner-{uuid4().hex[:16]}"
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(self, *, entry_receipt_id: str, parent_receipt_id: str):
        lock = self._locks.setdefault(entry_receipt_id, asyncio.Lock())
        async with lock:
            entry_view = await self.entry_service.inspect(
                receipt_id=entry_receipt_id,
                assessed_at=self.now(),
            )
            if not entry_view.can_execute_local_canary:
                raise EvolutionRevalidationLocalCanaryRunError(
                    "local_canary_entry_not_current",
                    f"Local canary entry 当前为 {entry_view.status.value}。",
                )
            entry = entry_view.receipt
            current = await self.journal_store.admit(entry, observed_at=self.now())
            if current.terminal:
                return _view(current)
            return await self._resume(current, entry, parent_receipt_id)

    async def _resume(self, current, entry, parent_receipt_id):
        contract_view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=entry.contract_id,
        )
        plan_view = await self.entry_service.plan_service.inspect(plan_id=entry.plan_id)
        if not (
            contract_view.execution_eligible
            and contract_view.contract.contract_sha256
            == plan_view.plan.contract_sha256
        ):
            return _view(await self._terminal(
                current, entry, EvolutionRevalidationLocalCanaryState.CANCELLED,
                error_code="runtime_contract_stale",
                error_message="Runtime Contract 已失效或与 Rollout Plan 不一致。",
            ))
        contract = contract_view.contract
        pair = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=contract.validation_plan_id,
        )
        if not (
            pair.validation_plan.validation_plan_sha256 == contract.validation_plan_sha256
            and pair.source_snapshot.snapshot_sha256 == contract.source_snapshot_sha256
        ):
            return _view(await self._terminal(
                current, entry, EvolutionRevalidationLocalCanaryState.FAILED,
                error_code="source_contract_mismatch",
                error_message="Immutable GREEN 与 Runtime Contract 不一致。",
            ))
        if current.state is EvolutionRevalidationLocalCanaryState.ADMITTED:
            current = await self.journal_store.transition(_build_event(
                entry=entry,
                run_id=current.run_id,
                run_index=current.run_index,
                state=EvolutionRevalidationLocalCanaryState.SOURCE_VERIFIED,
                previous=current,
                observed_at=self.now(),
                source=pair,
            ))
        entry_view = await self.entry_service.inspect(
            receipt_id=entry.receipt_id,
            assessed_at=self.now(),
        )
        if not entry_view.can_execute_local_canary:
            return _view(await self._terminal(
                current, entry, EvolutionRevalidationLocalCanaryState.CANCELLED,
                error_code="entry_fenced_before_run",
                error_message="Stage entry 在执行前失效。",
            ))
        try:
            checks = await self._checks(pair, contract)
        except EvolutionRevalidationLocalCanaryRunError as exc:
            return _view(await self._terminal(
                current,
                entry,
                EvolutionRevalidationLocalCanaryState.FAILED,
                source=pair,
                error_code=exc.code,
                error_message=str(exc),
            ))
        parent = await self.permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id or (
            "bash_run" not in parent.delegated_tool_names
        ):
            return _view(await self._terminal(
                current, entry, EvolutionRevalidationLocalCanaryState.FAILED,
                error_code="parent_permission_invalid",
                error_message="Local canary 缺少 bash_run 父权限。",
            ))
        lease_seconds = min(
            86_400,
            max(30, contract.profile_timeout_seconds_per_sample + 30),
        )
        lease = await self.harness_store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=parent.run_id,
            owner_id=f"{self.runner_id}-{current.run_id}",
            now=self.now(),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            raise EvolutionRevalidationLocalCanaryRunError(
                "local_canary_executor_busy", "Local canary run 已由其他执行者持有。"
            )
        grant = None
        results = ()
        error = None
        cancellation = None
        try:
            grant = await self.run_grant_authority.issue(
                RunDelegationGrantRequest(
                    idempotency_key=f"{current.run_id}-{lease.epoch}",
                    parent_receipt_id=parent_receipt_id,
                    run_kind=HarnessRunKind.RUNTIME,
                    lease_owner_id=lease.owner_id,
                    lease_epoch=lease.epoch,
                    delegated_tool_names=("bash_run",),
                ),
                now=self.now(),
                ttl_seconds=lease_seconds,
            )
            authority = HarnessSandboxEvalRunAuthority(
                parent_receipt_id=parent_receipt_id,
                run_id=parent.run_id,
                grant_id=grant.contract.grant_id,
                grant_sha256=grant.contract.grant_sha256,
            )
            current = await self.journal_store.transition(_build_event(
                entry=entry,
                run_id=current.run_id,
                run_index=current.run_index,
                state=EvolutionRevalidationLocalCanaryState.RUNNING,
                previous=current,
                observed_at=self.now(),
                source=pair,
                grant=grant.contract,
            ))
            results = await self.sandbox_eval_kernel.execute(
                lane="sandbox",
                authority_key=entry.receipt_sha256,
                parent_receipt_id=parent_receipt_id,
                sample_index=current.run_index,
                checks=checks,
                profile_digest=pair.validation_plan.profile_sha256,
                profile_is_current=lambda: self._profile_current(pair, contract),
                source=pair.green,
                run_authority=authority,
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
            error = ("local_canary_cancelled", "Local canary 执行被取消。")
        except HarnessSandboxEvalExecutionError as exc:
            error = (exc.code, str(exc))
        except Exception as exc:
            error = ("local_canary_execution_failed", str(exc))
        cleanup_errors = []
        if grant is not None:
            try:
                await self.run_grant_authority.revoke(
                    grant_id=grant.contract.grant_id,
                    reason="local_canary_run_finished",
                    revoked_at=self.now(),
                )
            except Exception as exc:
                cleanup_errors.append(str(exc))
        try:
            released = await self.harness_store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=parent.run_id,
                owner_id=lease.owner_id,
                epoch=lease.epoch,
                now=self.now(),
            )
            if released is None:
                cleanup_errors.append("Runtime lease 未释放。")
        except (HarnessStoreError, OSError, ValueError) as exc:
            cleanup_errors.append(str(exc))
        if cleanup_errors:
            error = (
                "local_canary_cleanup_failed",
                "; ".join(cleanup_errors)[:300],
            )
        entry_view = await self.entry_service.inspect(
            receipt_id=entry.receipt_id,
            assessed_at=self.now(),
        )
        try:
            evidence = tuple(_check_evidence(item) for item in results)
        except EvolutionRevalidationLocalCanaryRunError as exc:
            evidence = ()
            error = (exc.code, str(exc))
        if not entry_view.can_execute_local_canary:
            state = EvolutionRevalidationLocalCanaryState.CANCELLED
            error = ("entry_fenced_after_run", "Stage entry 在执行后失效。")
        elif cancellation is not None:
            state = EvolutionRevalidationLocalCanaryState.CANCELLED
        elif error is not None or any(
            item.status is not HarnessSandboxCheckStatus.PASSED
            for item in evidence
        ):
            state = EvolutionRevalidationLocalCanaryState.FAILED
            error = error or ("local_canary_check_failed", "Local canary check 未通过。")
        else:
            state = EvolutionRevalidationLocalCanaryState.PASSED
        terminal = await self._terminal(
            current,
            entry,
            state,
            source=pair,
            grant=None if grant is None else grant.contract,
            checks=evidence,
            error_code="" if error is None else error[0],
            error_message="" if error is None else error[1],
        )
        if cancellation is not None:
            raise cancellation
        return _view(terminal)

    async def _terminal(self, current, entry, state, **kwargs):
        return await self.journal_store.transition(_build_event(
            entry=entry,
            run_id=current.run_id,
            run_index=current.run_index,
            state=state,
            previous=current,
            observed_at=self.now(),
            **kwargs,
        ))

    async def _checks(self, pair, contract):
        status = await self.profile_service.status()
        if not status.trusted or status.snapshot.profile is None or (
            status.profile_digest != pair.validation_plan.profile_sha256
        ):
            raise EvolutionRevalidationLocalCanaryRunError(
                "local_canary_profile_untrusted", "Harness Profile 当前不受信任。"
            )
        current = {item.id: item for item in status.snapshot.profile.checks}
        checks = tuple(current.get(item.id) for item in pair.validation_plan.checks)
        if any(item is None for item in checks) or tuple(
            _digest(item.model_dump(mode="json")) for item in checks
        ) != tuple(
            _digest(item.model_dump(mode="json")) for item in pair.validation_plan.checks
        ) or sum(item.timeout_seconds for item in checks) != (
            contract.profile_timeout_seconds_per_sample
        ):
            raise EvolutionRevalidationLocalCanaryRunError(
                "local_canary_profile_drifted", "Harness Profile checks 已漂移。"
            )
        return checks

    async def _profile_current(self, pair, contract):
        try:
            await self._checks(pair, contract)
            return bool(await pair.green.source_is_current())
        except (OSError, TypeError, ValueError, RuntimeError):
            return False


def _build_event(
    *, entry, run_id, run_index, state, previous, observed_at,
    source=None, grant=None, checks=(), error_code="", error_message="",
):
    source_snapshot_id = (
        source.source_snapshot.snapshot_id
        if source is not None
        else "" if previous is None else previous.source_snapshot_id
    )
    source_snapshot_sha256 = (
        source.source_snapshot.snapshot_sha256
        if source is not None
        else "" if previous is None else previous.source_snapshot_sha256
    )
    source_tree_sha256 = (
        source.green_materialized_tree_sha256
        if source is not None
        else "" if previous is None else previous.source_tree_sha256
    )
    run_grant_id = (
        grant.grant_id
        if grant is not None
        else "" if previous is None else previous.run_grant_id
    )
    run_grant_sha256 = (
        grant.grant_sha256
        if grant is not None
        else "" if previous is None else previous.run_grant_sha256
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_LOCAL_CANARY_RUN_POLICY,
        "run_id": run_id,
        "sequence": 1 if previous is None else previous.sequence + 1,
        "previous_event_id": "" if previous is None else previous.event_id,
        "previous_event_sha256": "" if previous is None else previous.event_sha256,
        "workspace_root": entry.workspace_root,
        "entry_receipt_id": entry.receipt_id,
        "entry_receipt_sha256": entry.receipt_sha256,
        "plan_id": entry.plan_id,
        "plan_sha256": entry.plan_sha256,
        "contract_id": entry.contract_id,
        "run_index": run_index,
        "state": state.value,
        "source_snapshot_id": source_snapshot_id,
        "source_snapshot_sha256": source_snapshot_sha256,
        "source_tree_sha256": source_tree_sha256,
        "run_grant_id": run_grant_id,
        "run_grant_sha256": run_grant_sha256,
        "checks": [item.model_dump(mode="json") for item in checks],
        "error_code": error_code,
        "error_message": error_message[:300],
        "observed_at": _aware(observed_at).isoformat(),
        "terminal": state in {
            EvolutionRevalidationLocalCanaryState.PASSED,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        },
        "canary_passed": state is EvolutionRevalidationLocalCanaryState.PASSED,
        "monitor_authority": state in {
            EvolutionRevalidationLocalCanaryState.PASSED,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        },
        "opt_in_authority": False,
        "percentage_authority": False,
        "stable_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationLocalCanaryEvent.model_validate({
        **core,
        "event_id": f"evrecanaryevent_{digest[:24]}",
        "event_sha256": digest,
    })


def _check_evidence(result):
    if result.job_id is None or result.lifecycle_receipt_sha256 is None or (
        result.exit_code is None
    ):
        raise EvolutionRevalidationLocalCanaryRunError(
            "local_canary_execution_evidence_incomplete",
            "Local canary 缺少 ARC-04 lifecycle evidence。",
        )
    return EvolutionRevalidationLocalCanaryCheckEvidence(
        check_id=result.check_id,
        status=result.status.value,
        run_id=result.run_id,
        job_id=result.job_id,
        lifecycle_receipt_sha256=result.lifecycle_receipt_sha256,
        source_tree_sha256=result.source_tree_sha256.removeprefix("sha256:"),
        snapshot_manifest_sha256=result.snapshot_manifest_sha256,
        profile_digest=result.profile_digest,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
    )


def _transition_allowed(previous, current):
    allowed = {
        EvolutionRevalidationLocalCanaryState.ADMITTED: {
            EvolutionRevalidationLocalCanaryState.SOURCE_VERIFIED,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        },
        EvolutionRevalidationLocalCanaryState.SOURCE_VERIFIED: {
            EvolutionRevalidationLocalCanaryState.RUNNING,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        },
        EvolutionRevalidationLocalCanaryState.RUNNING: {
            EvolutionRevalidationLocalCanaryState.RUNNING,
            EvolutionRevalidationLocalCanaryState.PASSED,
            EvolutionRevalidationLocalCanaryState.FAILED,
            EvolutionRevalidationLocalCanaryState.CANCELLED,
        },
    }
    return current in allowed.get(previous, set())


def _run_id(entry_sha256, run_index):
    digest = hashlib.sha256(f"{entry_sha256}:{run_index}".encode()).hexdigest()
    return f"evrecanaryrun_{digest[:24]}"


def _view(event):
    return EvolutionRevalidationLocalCanaryRunView(
        event=event,
        run_complete=event.terminal,
        passed=event.canary_passed,
        monitor_authority=event.monitor_authority,
    )


async def _require_entry(db, entry):
    observed = await _require_entry_id(db, entry.receipt_id)
    if observed != entry:
        raise EvolutionRevalidationLocalCanaryRunError(
            "local_canary_entry_changed", "Local canary entry 已变化。"
        )


async def _require_entry_id(db, receipt_id):
    row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_revalidation_rollout_stage_entries "
            "WHERE receipt_id = ?",
            (receipt_id,),
        )
    ).fetchone()
    if row is None:
        raise EvolutionRevalidationLocalCanaryRunError(
            "local_canary_entry_missing", "Local canary entry 不存在。"
        )
    return EvolutionRevalidationRolloutStageEntryReceipt.model_validate_json(
        row["receipt_json"]
    )


async def _insert(db, item):
    await db.execute(
        "INSERT INTO evolution_revalidation_local_canary_events "
        "(event_id, event_sha256, run_id, sequence, entry_receipt_id, run_index, "
        "state, terminal, event_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.event_id,
            item.event_sha256,
            item.run_id,
            item.sequence,
            item.entry_receipt_id,
            item.run_index,
            item.state.value,
            int(item.terminal),
            item.model_dump_json(),
            item.observed_at,
        ),
    )


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_local_canary_events ("
        "event_id TEXT PRIMARY KEY, event_sha256 TEXT NOT NULL UNIQUE, "
        "run_id TEXT NOT NULL, sequence INTEGER NOT NULL, entry_receipt_id TEXT NOT NULL, "
        "run_index INTEGER NOT NULL, state TEXT NOT NULL, terminal INTEGER NOT NULL, "
        "event_json TEXT NOT NULL, observed_at TEXT NOT NULL, "
        "UNIQUE(run_id, sequence), UNIQUE(entry_receipt_id, run_index, sequence))"
    )
    await db.commit()


def _event(value):
    return EvolutionRevalidationLocalCanaryEvent.model_validate_json(value)


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Local canary 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_LOCAL_CANARY_RUN_POLICY",
    "EvolutionRevalidationLocalCanaryCheckEvidence",
    "EvolutionRevalidationLocalCanaryEvent",
    "EvolutionRevalidationLocalCanaryExecutor",
    "EvolutionRevalidationLocalCanaryJournalStore",
    "EvolutionRevalidationLocalCanaryRunError",
    "EvolutionRevalidationLocalCanaryRunView",
    "EvolutionRevalidationLocalCanaryState",
]
