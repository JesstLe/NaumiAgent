"""Guard-bound writer for an ordered multi-file evolution write-set."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.evolution.patch_journals import EvolutionPatchJournalStore
from naumi_agent.evolution.patch_sets import (
    EvolutionPatchSetStore,
    EvolutionPatchSetTransaction,
    PatchSetState,
)
from naumi_agent.evolution.patch_writers import (
    EvolutionPatchWriteError,
    _acquire_lock,
    _atomic_replace,
    _git_status,
    _managed_worktree_root,
    _release_lock,
    _rollback,
    _target_matches_baseline,
    _target_matches_digest,
    _verify_baseline,
    _verify_target_path,
)
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuard,
    EvolutionStaticGuardReceipt,
    StaticGuardChangeFact,
)

PATCH_SET_WRITER_POLICY = "evolution-multi-file-patch-writer-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPatchSetWriteReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-multi-file-patch-writer-v1"] = (
        PATCH_SET_WRITER_POLICY
    )
    write_id: str = Field(pattern=r"^evsw_[0-9a-f]{24}$")
    write_sha256: str = Field(pattern=_SHA256_RE)
    transaction_id: str = Field(pattern=r"^evset_[0-9a-f]{24}$")
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    guard_receipt_sha256: str = Field(pattern=_SHA256_RE)
    changes: tuple[StaticGuardChangeFact, ...] = Field(min_length=2, max_length=16)
    worktree_status_sha256: str = Field(pattern=_SHA256_RE)
    postflight_passed: Literal[True] = True
    rollback_performed: Literal[False] = False
    write_completed: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_tamper_evident(self) -> Self:
        paths = tuple(change.path for change in self.changes)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Patch Set Write Receipt changes 顺序无效。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"write_id", "write_sha256"})
        )
        if not hmac.compare_digest(self.write_sha256, expected):
            raise ValueError("write_sha256 与 Patch Set Write Receipt 不一致。")
        if self.write_id != f"evsw_{expected[:24]}":
            raise ValueError("write_id 与 Patch Set Write Receipt 不一致。")
        return self


class EvolutionPatchSetWriter:
    """Apply one Guard-approved write-set in deterministic order."""

    def __init__(
        self,
        *,
        static_guard: EvolutionStaticGuard,
        patch_set_store: EvolutionPatchSetStore,
        journal_store: EvolutionPatchJournalStore,
    ) -> None:
        self._static_guard = static_guard
        self._patch_set_store = patch_set_store
        self._journal_store = journal_store

    async def apply(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        guard_receipt: EvolutionStaticGuardReceipt,
        proposed_contents: Mapping[str, str | bytes],
    ) -> EvolutionPatchSetWriteReceipt:
        if not 2 <= len(proposed_contents) <= 16 or len(guard_receipt.changes) != len(
            proposed_contents
        ):
            raise EvolutionPatchWriteError(
                "multi_file_required",
                "Multi-file Patch Writer 需要一次提交 2..16 个完整文件。",
            )
        root = _managed_worktree_root(lease)
        lock_path = root.parent / f".{lease.worktree_name}.{lease.lease_id}.patch.lock"
        lock_token = await asyncio.to_thread(_acquire_lock, lock_path, lease)
        try:
            single = await asyncio.to_thread(
                self._journal_store.get_by_lease, lease.lease_id
            )
            if single is not None:
                raise EvolutionPatchWriteError(
                    "single_journal_conflict",
                    "该 Lease 已绑定单文件 Patch Journal，拒绝建立平行事务。",
                )
            existing = await asyncio.to_thread(
                self._patch_set_store.get_by_lease, lease.lease_id
            )
            if existing is not None:
                if existing.state is PatchSetState.COMMITTED:
                    return await asyncio.to_thread(
                        self._load_committed_receipt, root, existing, guard_receipt
                    )
                if existing.state in {
                    PatchSetState.PREPARED,
                    PatchSetState.APPLYING,
                    PatchSetState.APPLIED,
                    PatchSetState.ROLLING_BACK,
                    PatchSetState.RECOVERY_FAILED,
                }:
                    raise EvolutionPatchWriteError(
                        "write_set_recovery_required",
                        "发现未完成 Patch Set，必须先执行多文件恢复。",
                    )
            fresh = await self._static_guard.preflight(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                proposed_contents=proposed_contents,
            )
            if not fresh.preflight_passed:
                raise EvolutionPatchWriteError(
                    "guard_rejected", "Static Guard 已拒绝整个 write-set。"
                )
            if fresh != guard_receipt:
                raise EvolutionPatchWriteError(
                    "guard_receipt_mismatch",
                    "提议内容或运行事实已偏离 Guard Receipt。",
                )
            return await asyncio.to_thread(
                self._apply_sync,
                root,
                contract,
                lease,
                source_snapshot,
                mutation_plan,
                guard_receipt,
                proposed_contents,
            )
        finally:
            await asyncio.to_thread(_release_lock, lock_path, lock_token)

    def _apply_sync(
        self,
        root: Path,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        guard_receipt: EvolutionStaticGuardReceipt,
        proposed_contents: Mapping[str, str | bytes],
    ) -> EvolutionPatchSetWriteReceipt:
        contents = _bind_contents(guard_receipt, proposed_contents)
        baselines: dict[str, bytes | None] = {}
        modes: dict[str, int] = {}
        targets: dict[str, Path] = {}
        for change in guard_receipt.changes:
            target = root / change.path
            _verify_target_path(root, target)
            before, mode = _verify_baseline(target, change)
            targets[change.path] = target
            baselines[change.path] = before
            modes[change.path] = mode
        transaction: EvolutionPatchSetTransaction | None = None
        replaced: set[int] = set()
        try:
            transaction = self._patch_set_store.prepare(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                guard_receipt=guard_receipt,
                before_contents=baselines,
                file_modes=modes,
            )
            if transaction.state is PatchSetState.ROLLED_BACK:
                raise EvolutionPatchWriteError(
                    "attempt_budget_exhausted",
                    "Patch Set 已达到 Mutation Plan 尝试上限。",
                )
            if transaction.state is not PatchSetState.PREPARED:
                raise EvolutionPatchWriteError(
                    "write_set_state_mismatch", "Patch Set 未处于 prepared 状态。"
                )
            for item in transaction.files:
                _atomic_replace(
                    targets[item.path], contents[item.path], mode=item.file_mode
                )
                replaced.add(item.index)
                self._patch_set_store.mark_file_replaced(
                    transaction.transaction_id, file_index=item.index
                )
            status = _postflight(root, guard_receipt.changes)
            receipt = _build_receipt(
                transaction=transaction,
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                guard_receipt=guard_receipt,
                status=status,
            )
            self._patch_set_store.mark_committed(
                transaction.transaction_id,
                receipt_json=receipt.model_dump_json(),
            )
            return receipt
        except Exception as exc:
            rollback_completed = False
            if transaction is not None:
                rollback_completed = self._rollback_set(
                    transaction, targets, baselines, modes, replaced, exc
                )
            if isinstance(exc, EvolutionPatchWriteError):
                raise EvolutionPatchWriteError(
                    exc.code, str(exc), rollback_completed=rollback_completed
                ) from exc
            raise EvolutionPatchWriteError(
                "write_set_failed",
                "多文件原子写入或 postflight 失败。",
                rollback_completed=rollback_completed,
            ) from exc

    def _rollback_set(
        self,
        transaction: EvolutionPatchSetTransaction,
        targets: Mapping[str, Path],
        baselines: Mapping[str, bytes | None],
        modes: Mapping[str, int],
        replaced: set[int],
        failure: Exception,
    ) -> bool:
        try:
            current = self._patch_set_store.get_by_lease(transaction.lease_id)
            if current is None:
                return False
            if current.state is not PatchSetState.ROLLING_BACK:
                current = self._patch_set_store.begin_rollback(
                    transaction.transaction_id
                )
            for item in reversed(current.files):
                target = targets[item.path]
                if item.index in replaced or _target_matches_digest(
                    target, item.after_sha256
                ):
                    _rollback(target, baselines[item.path], modes[item.path])
                if not _target_matches_baseline(target, item):
                    self._patch_set_store.mark_recovery_failed(
                        transaction.transaction_id,
                        failure_code="rollback_baseline_mismatch",
                    )
                    return False
                self._patch_set_store.mark_file_rolled_back(
                    transaction.transaction_id, file_index=item.index
                )
            self._patch_set_store.mark_rolled_back(
                transaction.transaction_id,
                failure_code=(
                    failure.code
                    if isinstance(failure, EvolutionPatchWriteError)
                    else "write_set_failed"
                ),
            )
            return True
        except (KeyError, OSError, RuntimeError, ValueError):
            try:
                self._patch_set_store.mark_recovery_failed(
                    transaction.transaction_id,
                    failure_code="rollback_failed",
                )
            except (KeyError, RuntimeError, ValueError):
                pass
            return False

    def _load_committed_receipt(
        self,
        root: Path,
        transaction: EvolutionPatchSetTransaction,
        guard_receipt: EvolutionStaticGuardReceipt,
    ) -> EvolutionPatchSetWriteReceipt:
        if (
            transaction.guard_id != guard_receipt.guard_id
            or transaction.guard_receipt_sha256 != guard_receipt.receipt_sha256
            or tuple(item.path for item in transaction.files)
            != tuple(item.path for item in guard_receipt.changes)
        ):
            raise EvolutionPatchWriteError(
                "committed_write_set_mismatch",
                "已提交 Patch Set 与本次 Guard Receipt 不一致。",
            )
        receipt_json = self._patch_set_store.load_receipt_json(
            transaction.transaction_id
        )
        if receipt_json is None:
            raise EvolutionPatchWriteError(
                "committed_receipt_missing", "已提交 Patch Set 缺少写入回执。"
            )
        receipt = EvolutionPatchSetWriteReceipt.model_validate_json(receipt_json)
        _postflight(root, guard_receipt.changes)
        return receipt


def _bind_contents(
    guard: EvolutionStaticGuardReceipt,
    proposed: Mapping[str, str | bytes],
) -> dict[str, bytes]:
    if set(proposed) != {item.path for item in guard.changes}:
        raise EvolutionPatchWriteError(
            "content_scope_mismatch", "提议内容与 Guard write-set 范围不一致。"
        )
    contents: dict[str, bytes] = {}
    for change in guard.changes:
        raw = proposed[change.path]
        content = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), change.after_sha256
        ):
            raise EvolutionPatchWriteError(
                "content_digest_mismatch", "提议内容摘要与 Guard fact 不一致。"
            )
        contents[change.path] = content
    return contents


def _postflight(root: Path, changes: tuple[StaticGuardChangeFact, ...]) -> bytes:
    for change in changes:
        if not _target_matches_digest(root / change.path, change.after_sha256):
            raise EvolutionPatchWriteError(
                "postflight_digest_mismatch", "write-set 文件摘要与 Guard 不一致。"
            )
    status = _git_status(root)
    expected = b"".join(
        (
            f" M {change.path}\0"
            if change.operation == "modify"
            else f"?? {change.path}\0"
        ).encode("utf-8")
        for change in changes
    )
    if status != expected:
        raise EvolutionPatchWriteError(
            "postflight_scope_mismatch", "Git 变更范围与完整 write-set 不一致。"
        )
    return status


def _build_receipt(
    *,
    transaction: EvolutionPatchSetTransaction,
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    source_snapshot: EvolutionExperimentSourceSnapshot,
    mutation_plan: EvolutionMutationPlan,
    guard_receipt: EvolutionStaticGuardReceipt,
    status: bytes,
) -> EvolutionPatchSetWriteReceipt:
    payload = {
        "schema_version": 1,
        "policy_version": PATCH_SET_WRITER_POLICY,
        "transaction_id": transaction.transaction_id,
        "contract_id": contract.contract_id,
        "lease_id": lease.lease_id,
        "source_snapshot_id": source_snapshot.snapshot_id,
        "mutation_plan_id": mutation_plan.plan_id,
        "guard_id": guard_receipt.guard_id,
        "guard_receipt_sha256": guard_receipt.receipt_sha256,
        "changes": tuple(guard_receipt.changes),
        "worktree_status_sha256": hashlib.sha256(status).hexdigest(),
        "postflight_passed": True,
        "rollback_performed": False,
        "write_completed": True,
        "execution_ready": False,
    }
    digest = _sha256_payload(payload)
    return EvolutionPatchSetWriteReceipt.model_validate(
        {**payload, "write_id": f"evsw_{digest[:24]}", "write_sha256": digest}
    )


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported canonical payload type: {type(value).__name__}")


__all__ = ["EvolutionPatchSetWriteReceipt", "EvolutionPatchSetWriter"]
