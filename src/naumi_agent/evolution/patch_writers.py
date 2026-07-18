"""Guard-bound atomic writer for one isolated evolution source file."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.evolution.patch_journals import (
    EvolutionPatchJournal,
    EvolutionPatchJournalStore,
    PatchJournalState,
)
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuard,
    EvolutionStaticGuardReceipt,
    StaticGuardChangeFact,
)

PATCH_WRITER_POLICY = "evolution-single-file-patch-writer-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_GIT_STATUS_BYTES = 64 * 1024
_MAX_LOCK_BYTES = 4_096


class _LockBinding(Protocol):
    lease_id: str
    worktree_name: str


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPatchWriteReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-single-file-patch-writer-v1"] = PATCH_WRITER_POLICY
    write_id: str = Field(pattern=r"^evw_[0-9a-f]{24}$")
    write_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    guard_receipt_sha256: str = Field(pattern=_SHA256_RE)
    change: StaticGuardChangeFact
    worktree_status_sha256: str = Field(pattern=_SHA256_RE)
    postflight_passed: Literal[True] = True
    rollback_performed: Literal[False] = False
    write_completed: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_tamper_evident(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"write_id", "write_sha256"})
        )
        if not hmac.compare_digest(self.write_sha256, expected):
            raise ValueError("write_sha256 与 Patch Write Receipt 不一致。")
        if self.write_id != f"evw_{expected[:24]}":
            raise ValueError("write_id 与 Patch Write Receipt 不一致。")
        return self


class EvolutionPatchWriteError(RuntimeError):
    """Typed failure that never includes proposed source content."""

    def __init__(self, code: str, message: str, *, rollback_completed: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.rollback_completed = rollback_completed


class EvolutionPatchWriter:
    """Atomically apply exactly one preflight-approved file in a leased worktree."""

    def __init__(
        self,
        *,
        static_guard: EvolutionStaticGuard,
        journal_store: EvolutionPatchJournalStore,
    ) -> None:
        self._static_guard = static_guard
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
    ) -> EvolutionPatchWriteReceipt:
        if len(proposed_contents) != 1 or len(guard_receipt.changes) != 1:
            raise EvolutionPatchWriteError(
                "single_file_required",
                "EVO-02.5a 仅允许一次原子写入一个文件。",
            )
        root = _managed_worktree_root(lease)
        lock_path = root.parent / f".{lease.worktree_name}.{lease.lease_id}.patch.lock"
        lock_token = await asyncio.to_thread(_acquire_lock, lock_path, lease)
        try:
            existing = await asyncio.to_thread(
                self._journal_store.get_by_lease,
                lease.lease_id,
            )
            if existing is not None:
                if existing.state is PatchJournalState.COMMITTED:
                    return await asyncio.to_thread(
                        self._load_committed_receipt,
                        root,
                        existing,
                        guard_receipt,
                    )
                if existing.state in {
                    PatchJournalState.PREPARED,
                    PatchJournalState.REPLACED,
                    PatchJournalState.RECOVERY_FAILED,
                }:
                    raise EvolutionPatchWriteError(
                        "journal_recovery_required",
                        "发现未完成 Patch Journal，必须先执行恢复。",
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
                    "guard_rejected",
                    "Static Guard 已拒绝写入，隔离 worktree 未变更。",
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
    ) -> EvolutionPatchWriteReceipt:
        change = guard_receipt.changes[0]
        raw_content = next(iter(proposed_contents.values()))
        content = (
            raw_content.encode("utf-8")
            if isinstance(raw_content, str)
            else bytes(raw_content)
        )
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), change.after_sha256):
            raise EvolutionPatchWriteError(
                "content_digest_mismatch",
                "提议内容摘要与 Guard change fact 不一致。",
            )
        target = root / change.path
        _verify_target_path(root, target)
        before, mode = _verify_baseline(target, change)
        journal: EvolutionPatchJournal | None = None
        replaced = False
        try:
            journal = self._journal_store.prepare(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                guard_receipt=guard_receipt,
                change=change,
                before=before,
                file_mode=mode,
            )
            if journal.state is PatchJournalState.ROLLED_BACK:
                raise EvolutionPatchWriteError(
                    "attempt_budget_exhausted",
                    "Patch Journal 已达到 Mutation Plan 尝试上限。",
                )
            if journal.state is not PatchJournalState.PREPARED:
                raise EvolutionPatchWriteError(
                    "journal_state_mismatch",
                    "Patch Journal 未处于 prepared 状态。",
                )
            _atomic_replace(target, content, mode=mode)
            replaced = True
            self._journal_store.mark_replaced(journal.journal_id)
            status = self._postflight(root, target, change)
            receipt = _build_write_receipt(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                guard_receipt=guard_receipt,
                change=change,
                status=status,
            )
            self._journal_store.mark_committed(
                journal.journal_id,
                receipt_json=receipt.model_dump_json(),
            )
            return receipt
        except Exception as exc:
            if not replaced:
                replaced = _target_matches_digest(target, change.after_sha256)
            rollback_completed = (
                not replaced and _target_matches_baseline(target, change)
            )
            if replaced:
                try:
                    _rollback(target, before, mode)
                    rollback_completed = True
                except OSError:
                    rollback_completed = False
            if journal is not None and rollback_completed:
                try:
                    self._journal_store.mark_rolled_back(
                        journal.journal_id,
                        failure_code=(
                            exc.code
                            if isinstance(exc, EvolutionPatchWriteError)
                            else "write_failed"
                        ),
                    )
                except (KeyError, RuntimeError, ValueError):
                    pass
            if isinstance(exc, EvolutionPatchWriteError):
                raise EvolutionPatchWriteError(
                    exc.code,
                    str(exc),
                    rollback_completed=rollback_completed,
                ) from exc
            raise EvolutionPatchWriteError(
                "write_failed",
                "原子写入或 postflight 失败。",
                rollback_completed=rollback_completed,
            ) from exc

    def _load_committed_receipt(
        self,
        root: Path,
        journal: EvolutionPatchJournal,
        guard_receipt: EvolutionStaticGuardReceipt,
    ) -> EvolutionPatchWriteReceipt:
        if (
            journal.guard_id != guard_receipt.guard_id
            or journal.guard_receipt_sha256 != guard_receipt.receipt_sha256
            or len(guard_receipt.changes) != 1
            or journal.target_path != guard_receipt.changes[0].path
        ):
            raise EvolutionPatchWriteError(
                "committed_journal_mismatch",
                "已提交 Patch Journal 与本次 Guard Receipt 不一致。",
            )
        receipt_json = self._journal_store.load_receipt_json(journal.journal_id)
        if receipt_json is None:
            raise EvolutionPatchWriteError(
                "committed_receipt_missing",
                "已提交 Patch Journal 缺少写入回执。",
            )
        receipt = EvolutionPatchWriteReceipt.model_validate_json(receipt_json)
        self._postflight(
            root,
            root / journal.target_path,
            guard_receipt.changes[0],
        )
        return receipt

    def _postflight(
        self,
        root: Path,
        target: Path,
        change: StaticGuardChangeFact,
    ) -> bytes:
        try:
            metadata = target.lstat()
            actual = target.read_bytes()
        except OSError as exc:
            raise EvolutionPatchWriteError(
                "postflight_read_failed",
                "写入后无法读取目标文件。",
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
            raise EvolutionPatchWriteError(
                "postflight_file_type",
                "写入后目标不是普通文件。",
            )
        if not hmac.compare_digest(hashlib.sha256(actual).hexdigest(), change.after_sha256):
            raise EvolutionPatchWriteError(
                "postflight_digest_mismatch",
                "写入后文件摘要与 Guard Receipt 不一致。",
            )
        status = _git_status(root)
        expected = (
            f" M {change.path}\0" if change.operation == "modify" else f"?? {change.path}\0"
        ).encode("utf-8")
        if status != expected:
            raise EvolutionPatchWriteError(
                "postflight_scope_mismatch",
                "写入后 Git 变更范围与 Guard Receipt 不一致。",
            )
        return status


def _build_write_receipt(
    *,
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    source_snapshot: EvolutionExperimentSourceSnapshot,
    mutation_plan: EvolutionMutationPlan,
    guard_receipt: EvolutionStaticGuardReceipt,
    change: StaticGuardChangeFact,
    status: bytes,
) -> EvolutionPatchWriteReceipt:
    payload = {
        "schema_version": 1,
        "policy_version": PATCH_WRITER_POLICY,
        "contract_id": contract.contract_id,
        "lease_id": lease.lease_id,
        "source_snapshot_id": source_snapshot.snapshot_id,
        "mutation_plan_id": mutation_plan.plan_id,
        "guard_id": guard_receipt.guard_id,
        "guard_receipt_sha256": guard_receipt.receipt_sha256,
        "change": change.model_dump(mode="json"),
        "worktree_status_sha256": hashlib.sha256(status).hexdigest(),
        "postflight_passed": True,
        "rollback_performed": False,
        "write_completed": True,
        "execution_ready": False,
    }
    digest = _sha256_payload(payload)
    return EvolutionPatchWriteReceipt.model_validate(
        {**payload, "write_id": f"evw_{digest[:24]}", "write_sha256": digest}
    )


def _managed_worktree_root(lease: ExperimentWorktreeLease) -> Path:
    if not lease.worktree_ready:
        raise EvolutionPatchWriteError("lease_inactive", "Patch Writer 需要 active Lease。")
    try:
        root = Path(lease.worktree_path).resolve(strict=True)
    except OSError as exc:
        raise EvolutionPatchWriteError(
            "worktree_unavailable",
            "实验 worktree 不存在或无法读取。",
        ) from exc
    if not root.is_dir() or root.name != lease.worktree_name:
        raise EvolutionPatchWriteError("worktree_binding", "实验 worktree 路径绑定无效。")
    return root


def _acquire_lock(path: Path, lease: _LockBinding) -> str:
    token = secrets.token_hex(24)
    payload = {
        "schema_version": 1,
        "policy_version": PATCH_WRITER_POLICY,
        "token": token,
        "lease_id": lease.lease_id,
        "worktree_name": lease.worktree_name,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise EvolutionPatchWriteError(
            "writer_locked",
            "该 Experiment Lease 已有 Patch Writer 正在工作。",
        ) from exc
    except OSError as exc:
        raise EvolutionPatchWriteError("lock_failed", "无法获取 Patch Writer 互斥锁。") from exc
    try:
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short lock write")
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    return token


def _release_lock(path: Path, token: str) -> None:
    try:
        payload = _read_lock(path)
    except FileNotFoundError:
        return
    if payload["token"] != token:
        raise EvolutionPatchWriteError(
            "lock_ownership_lost",
            "Patch Writer 锁所有权已变化，拒绝删除未知锁。",
        )
    path.unlink()


def _reclaim_stale_lock(path: Path, lease: _LockBinding) -> str:
    try:
        return _acquire_lock(path, lease)
    except EvolutionPatchWriteError as exc:
        if exc.code != "writer_locked":
            raise
    payload = _read_lock(path)
    if payload["lease_id"] != lease.lease_id or payload["worktree_name"] != lease.worktree_name:
        raise EvolutionPatchWriteError("stale_lock_binding", "残留锁与 Lease binding 不一致。")
    if payload["hostname"] != socket.gethostname():
        raise EvolutionPatchWriteError("remote_lock_owner", "残留锁属于其他主机，禁止自动接管。")
    if _pid_alive(payload["pid"]):
        raise EvolutionPatchWriteError("writer_locked", "Patch Writer 进程仍存活。")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return _acquire_lock(path, lease)


def _read_lock(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) > _MAX_LOCK_BYTES:
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁超过安全上限。")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁格式损坏。") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "policy_version",
        "token",
        "lease_id",
        "worktree_name",
        "pid",
        "hostname",
        "created_at",
    }:
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁字段不完整。")
    if (
        payload["schema_version"] != 1
        or payload["policy_version"] != PATCH_WRITER_POLICY
        or not isinstance(payload["token"], str)
        or len(payload["token"]) != 48
        or any(char not in "0123456789abcdef" for char in payload["token"])
        or not isinstance(payload["lease_id"], str)
        or not re.fullmatch(r"evl_[0-9a-f]{24}", payload["lease_id"])
        or not isinstance(payload["worktree_name"], str)
        or not re.fullmatch(r"experiment-[0-9a-f]{16}", payload["worktree_name"])
        or not isinstance(payload["pid"], int)
        or payload["pid"] < 1
        or not isinstance(payload["hostname"], str)
        or not payload["hostname"]
        or not isinstance(payload["created_at"], str)
    ):
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁字段格式无效。")
    try:
        parsed = datetime.fromisoformat(payload["created_at"])
    except ValueError as exc:
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁时间无效。") from exc
    if parsed.tzinfo is None:
        raise EvolutionPatchWriteError("lock_corrupt", "Patch Writer 锁时间缺少时区。")
    return payload


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _verify_baseline(target: Path, change: StaticGuardChangeFact) -> tuple[bytes | None, int]:
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if change.operation == "modify":
        if metadata is None or not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
            raise EvolutionPatchWriteError("baseline_file_type", "目标文件类型已偏离 Guard。")
        before = target.read_bytes()
        if change.before_sha256 is None or not hmac.compare_digest(
            hashlib.sha256(before).hexdigest(), change.before_sha256
        ):
            raise EvolutionPatchWriteError(
                "baseline_digest_mismatch",
                "目标文件已偏离 Guard baseline。",
            )
        return before, stat.S_IMODE(metadata.st_mode) & 0o777
    if change.operation == "create":
        if metadata is not None:
            raise EvolutionPatchWriteError("baseline_create_conflict", "计划创建的目标已经存在。")
        return None, 0o644
    raise EvolutionPatchWriteError("invalid_operation", "Guard Receipt 不允许该写入操作。")


def _verify_target_path(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise EvolutionPatchWriteError("path_escape", "写入目标越过实验 worktree。") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EvolutionPatchWriteError("path_unreadable", "写入目标路径无法安全复核。") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise EvolutionPatchWriteError("path_symlink", "写入目标路径出现符号链接。")
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise EvolutionPatchWriteError("parent_unavailable", "写入目标父目录不可用。") from exc
    if not resolved_parent.is_relative_to(root):
        raise EvolutionPatchWriteError("path_escape", "写入目标父目录越过实验 worktree。")


def _atomic_replace(target: Path, content: bytes, *, mode: int) -> None:
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise OSError("unsafe parent")
    temporary = parent / f".naumi-evolution-{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        _fsync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback(target: Path, before: bytes | None, mode: int) -> None:
    if before is None:
        target.unlink(missing_ok=False)
        _fsync_directory(target.parent)
        return
    _atomic_replace(target, before, mode=mode)


def _target_matches_digest(target: Path, expected_sha256: str) -> bool:
    try:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
            return False
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return False
    return hmac.compare_digest(actual, expected_sha256)


def _target_matches_baseline(target: Path, change: StaticGuardChangeFact) -> bool:
    if change.operation == "create":
        return not target.exists() and not target.is_symlink()
    if change.before_sha256 is None:
        return False
    return _target_matches_digest(target, change.before_sha256)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git_status(root: Path) -> bytes:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=False,
            capture_output=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvolutionPatchWriteError(
            "postflight_git_failed",
            "无法读取 postflight Git 状态。",
        ) from exc
    if completed.returncode != 0:
        raise EvolutionPatchWriteError("postflight_git_failed", "无法读取 postflight Git 状态。")
    if len(completed.stdout) > _MAX_GIT_STATUS_BYTES:
        raise EvolutionPatchWriteError(
            "postflight_git_oversized",
            "postflight Git 状态超过安全上限。",
        )
    return completed.stdout


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionPatchWriteError",
    "EvolutionPatchWriteReceipt",
    "EvolutionPatchWriter",
]
