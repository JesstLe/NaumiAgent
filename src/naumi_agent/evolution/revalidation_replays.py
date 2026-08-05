"""Isolated exact-target source replay for approved Evolution candidates."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputStore,
    EvolutionPromotionPatchFile,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestService,
    EvolutionRevalidationRequestView,
)

EVOLUTION_REVALIDATION_REPLAY_POLICY = "evolution-revalidation-replay-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SOURCE_BYTES = 2 * 1_024 * 1_024
_MAX_RECEIPT_BYTES = 256 * 1_024
_GIT_TIMEOUT_SECONDS = 20


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationReplayFile(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    baseline_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    replay_sha256: str = Field(pattern=_SHA256_RE)
    unified_diff_sha256: str = Field(pattern=_SHA256_RE)
    executable: bool

    @model_validator(mode="after")
    def _file_is_exact(self) -> Self:
        if (self.operation == "modify") is not (self.baseline_sha256 is not None):
            raise ValueError("Replay file operation/baseline digest 不一致。")
        if not hmac.compare_digest(self.candidate_sha256, self.replay_sha256):
            raise ValueError("Exact-target replay 必须逐字节复现 Candidate。")
        return self


class EvolutionRevalidationReplayReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-replay-v1"] = (
        EVOLUTION_REVALIDATION_REPLAY_POLICY
    )
    replay_id: str = Field(pattern=r"^evreplay_[0-9a-f]{24}$")
    replay_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_before: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    replay_tree_sha256: str = Field(pattern=_SHA256_RE)
    files: tuple[EvolutionRevalidationReplayFile, ...] = Field(
        min_length=1,
        max_length=16,
    )
    source_worktree_unchanged: Literal[True] = True
    main_worktree_unchanged: Literal[True] = True
    target_branch_unchanged: Literal[True] = True
    detached_worktree_removed: Literal[True] = True
    network_used: Literal[False] = False
    dependency_install_used: Literal[False] = False
    validation_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        if tuple(item.order for item in self.files) != tuple(
            range(1, len(self.files) + 1)
        ):
            raise ValueError("Replay files 顺序不连续。")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Replay files 必须排序且不得重复。")
        parsed = datetime.fromisoformat(self.created_at)
        if parsed.utcoffset() is None:
            raise ValueError("Replay created_at 必须包含 UTC offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"replay_id", "replay_sha256"})
        )
        if not hmac.compare_digest(self.replay_sha256, digest):
            raise ValueError("Replay Receipt 摘要不一致。")
        if self.replay_id != f"evreplay_{digest[:24]}":
            raise ValueError("Replay Receipt identity 不一致。")
        return self


class EvolutionRevalidationReplayError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationReplayStore:
    """Immutable receipt store; one exact replay result per request."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionRevalidationReplayReceipt,
    ) -> EvolutionRevalidationReplayReceipt:
        item = _validated_receipt(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_receipt_oversized",
                "Revalidation Replay Receipt 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_revalidation_replays "
                        "WHERE request_id = ?",
                        (item.request_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _receipt_from_json(row["receipt_json"])
                    await db.rollback()
                    if existing != item:
                        raise EvolutionRevalidationReplayError(
                            "revalidation_replay_conflict",
                            "同一 Revalidation Request 已绑定不同 Replay Receipt。",
                        )
                    return existing
                await db.execute(
                    "INSERT INTO evolution_revalidation_replays "
                    "(replay_id, replay_sha256, request_id, request_sha256, "
                    "receipt_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.replay_id,
                        item.replay_sha256,
                        item.request_id,
                        item.request_sha256,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationReplayError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_store_error",
                "Revalidation Replay Receipt 无法持久化。",
            ) from exc
        return item

    async def get_by_request(
        self,
        request_id: str,
    ) -> EvolutionRevalidationReplayReceipt | None:
        if re.fullmatch(r"evrevalidation_[0-9a-f]{24}", str(request_id)) is None:
            raise ValueError("Revalidation Request ID 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_revalidation_replays "
                        "WHERE request_id = ?",
                        (request_id,),
                    )
                ).fetchone()
            return None if row is None else _receipt_from_json(row["receipt_json"])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_store_corrupt",
                "Revalidation Replay Receipt 损坏或无法读取。",
            ) from exc


class EvolutionRevalidationReplayExecutor:
    """Replay approved exact candidate bytes in a disposable detached worktree."""

    def __init__(
        self,
        *,
        store: EvolutionRevalidationReplayStore,
        worktree_storage_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, EvolutionRevalidationReplayStore):
            raise TypeError("Replay Executor 需要 EvolutionRevalidationReplayStore。")
        self._store = store
        self._storage = Path(worktree_storage_dir).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        *,
        request_view: EvolutionRevalidationRequestView,
        package_input: EvolutionPromotionPackageInput,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionRevalidationReplayReceipt:
        request_view = EvolutionRevalidationRequestView.model_validate(
            request_view.model_dump(mode="json")
        )
        request = request_view.request
        lock = self._locks.setdefault(request.request_id, asyncio.Lock())
        async with lock:
            existing = await self._store.get_by_request(request.request_id)
            if existing is not None:
                return existing
            return await self._execute_locked(
                request_view=request_view,
                package_input=package_input,
                lease=lease,
            )

    async def _execute_locked(
        self,
        *,
        request_view: EvolutionRevalidationRequestView,
        package_input: EvolutionPromotionPackageInput,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionRevalidationReplayReceipt:
        request = request_view.request
        package = EvolutionPromotionPackageInput.model_validate(
            package_input.model_dump(mode="json")
        )
        candidate_lease = ExperimentWorktreeLease.model_validate(
            lease.model_dump(mode="json")
        )
        _require_authority(request_view, package, candidate_lease, self._storage, self._clock())
        workspace = Path(request.workspace_root).resolve(strict=True)
        source = Path(candidate_lease.worktree_path).resolve(strict=True)
        source_before = _identity(source)
        main_before = _identity(workspace)
        target_before = _git_text(workspace, "rev-parse", request.target_branch)
        if target_before != request.target_head:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_target_moved",
                "目标分支已变化，必须重新签发 Revalidation Request。",
            )
        blobs = _capture_candidate(source, package.patch.files, request.baseline_commit)
        replay_path = self._storage / f"revalidation-{request.request_id[-24:]}"
        if replay_path.exists():
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_path_exists",
                "Replay worktree 路径已存在，需要先执行恢复清理。",
            )
        added = False
        try:
            self._storage.mkdir(parents=True, exist_ok=True)
            _git(workspace, "worktree", "add", "--detach", str(replay_path), request.target_head)
            added = True
            files = _apply_exact(replay_path, package.patch.files, blobs)
            replay_tree = _replay_tree_digest(files)
        except EvolutionRevalidationReplayError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_execution_failed",
                "隔离 Replay 执行失败，未产生 promotion authority。",
            ) from exc
        finally:
            if added:
                _git(workspace, "worktree", "remove", "--force", str(replay_path))
            _git(workspace, "worktree", "prune")
        if replay_path.exists():
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_cleanup_failed",
                "Replay worktree 未能清理，结果拒绝入库。",
            )
        if _identity(source) != source_before:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_source_changed",
                "Candidate source worktree 在 Replay 期间发生变化。",
            )
        if _identity(workspace) != main_before:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_main_changed",
                "主工作区在 Replay 期间发生变化。",
            )
        if _git_text(workspace, "rev-parse", request.target_branch) != target_before:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_target_changed",
                "目标分支在 Replay 期间发生变化。",
            )
        created_at = _aware_time(self._clock()).isoformat()
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_REPLAY_POLICY,
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "promotion_input_id": package.input_id,
            "promotion_input_sha256": package.input_sha256,
            "lease_id": candidate_lease.lease_id,
            "workspace_root": request.workspace_root,
            "target_head": request.target_head,
            "target_tree_before": request.target_tree,
            "replay_tree_sha256": replay_tree,
            "files": [item.model_dump(mode="json") for item in files],
            "source_worktree_unchanged": True,
            "main_worktree_unchanged": True,
            "target_branch_unchanged": True,
            "detached_worktree_removed": True,
            "network_used": False,
            "dependency_install_used": False,
            "validation_executed": False,
            "promotion_authority": False,
            "created_at": created_at,
        }
        digest = _sha256_payload(payload)
        receipt = EvolutionRevalidationReplayReceipt.model_validate(
            {**payload, "replay_id": f"evreplay_{digest[:24]}", "replay_sha256": digest}
        )
        return await self._store.record(receipt)


class EvolutionRevalidationReplayService:
    """Resolve current durable authorities before invoking the replay kernel."""

    def __init__(
        self,
        *,
        request_service: EvolutionRevalidationRequestService,
        package_input_store: EvolutionPromotionPackageInputStore,
        lease_store: EvolutionExperimentLeaseStore,
        replay_executor: EvolutionRevalidationReplayExecutor,
    ) -> None:
        if not isinstance(request_service, EvolutionRevalidationRequestService):
            raise TypeError("Replay Service 需要 Revalidation Request Service。")
        if not isinstance(package_input_store, EvolutionPromotionPackageInputStore):
            raise TypeError("Replay Service 需要 Promotion Package Input Store。")
        if not isinstance(lease_store, EvolutionExperimentLeaseStore):
            raise TypeError("Replay Service 需要 Experiment Lease Store。")
        if not isinstance(replay_executor, EvolutionRevalidationReplayExecutor):
            raise TypeError("Replay Service 需要 Replay Executor。")
        self._request_service = request_service
        self._package_input_store = package_input_store
        self._lease_store = lease_store
        self._replay_executor = replay_executor

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> EvolutionRevalidationReplayReceipt:
        view = await self._request_service.inspect(
            workspace_root=workspace_root,
            request_id=request_id,
        )
        package_view = await self._package_input_store.get(
            view.request.promotion_input_id
        )
        if package_view is None or not package_view.promotion_review_eligible:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_package_input_ineligible",
                "Promotion Package Input 不存在、已撤销或不可读取。",
            )
        lease = await self._lease_store.get(
            package_view.package_input.experiment_contract_id
        )
        if lease is None:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_lease_missing",
                "Experiment Lease 不存在，无法恢复 Candidate 源码。",
            )
        return await self._replay_executor.execute(
            request_view=view,
            package_input=package_view.package_input,
            lease=lease,
        )


def render_evolution_revalidation_replay(
    receipt: EvolutionRevalidationReplayReceipt,
) -> str:
    item = _validated_receipt(receipt)
    return "\n".join(
        [
            f"# Evolution Revalidation Replay `{item.replay_id}`",
            "",
            "**已在 detached worktree 逐字节重放 Candidate；尚未执行验证或 Promotion。**",
            "",
            f"- Request：`{item.request_id}`",
            f"- Target：`{item.target_head}`",
            f"- Files：{len(item.files)}",
            "- 隔离保证：主工作区、Candidate source 与 target branch 均未变化",
            "- Cleanup：detached worktree 已移除",
            "- Validation / Promotion authority：`false` / `false`",
        ]
    )


def _require_authority(
    view: EvolutionRevalidationRequestView,
    package: EvolutionPromotionPackageInput,
    lease: ExperimentWorktreeLease,
    storage: Path,
    now: datetime,
) -> None:
    request = view.request
    if not (view.current_status == "ready" and view.execution_eligible):
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_request_ineligible",
            "Revalidation Request 已失效或不具备执行资格。",
        )
    if request.operation != "validate_exact_tree":
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_rebase_not_implemented",
            "目标已前进；当前切片拒绝伪重放，需由后续三方 rebase executor 处理。",
        )
    if not (
        request.promotion_input_id == package.input_id
        and request.promotion_input_sha256 == package.input_sha256
        and request.patch_manifest_sha256 == package.patch.manifest_sha256
        and request.baseline_commit == package.baseline.baseline_commit
        and lease.contract_id == package.experiment_contract_id
        and lease.manifest_sha256 == package.experiment_contract_sha256
        and lease.baseline_commit == request.baseline_commit
        and lease.state is ExperimentLeaseState.ACTIVE
        and lease.worktree_ready
        and not lease.execution_ready
    ):
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_authority_mismatch",
            "Replay Request、Promotion Input 与 Experiment Lease 绑定不一致。",
        )
    aware_now = _aware_time(now)
    if datetime.fromisoformat(lease.expires_at) <= aware_now:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_lease_expired",
            "Experiment Lease 已过期，不能读取 Candidate 源码。",
        )
    source = Path(lease.worktree_path).resolve()
    if source.parent != storage or source.name != lease.worktree_name:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_worktree_unmanaged",
            "Candidate worktree 不属于受管存储目录。",
        )


def _capture_candidate(
    source: Path,
    patch_files: tuple[EvolutionPromotionPatchFile, ...],
    baseline: str,
) -> dict[str, tuple[bytes | None, bytes, bool]]:
    if _git_text(source, "rev-parse", "HEAD") != baseline:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_source_head_mismatch",
            "Candidate worktree HEAD 已偏离批准 baseline。",
        )
    status = _git_bytes(source, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    expected = b"".join(
        (b"?? " if item.operation == "create" else b" M ")
        + item.path.encode("utf-8")
        + b"\x00"
        for item in patch_files
    )
    if status != expected:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_source_status_mismatch",
            "Candidate worktree 含缺失、额外、暂存或类型不符的改动。",
        )
    result: dict[str, tuple[bytes | None, bytes, bool]] = {}
    for item in patch_files:
        path = source.joinpath(*PurePosixPath(item.path).parts)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOURCE_BYTES:
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_source_unsafe",
                "Candidate 文件类型或大小不符合 Replay 安全策略。",
            )
        candidate = path.read_bytes()
        baseline_bytes = (
            None
            if item.operation == "create"
            else _git_bytes(source, "show", f"{baseline}:{item.path}")
        )
        if not (
            hashlib.sha256(candidate).hexdigest() == item.after_sha256
            and (
                baseline_bytes is None
                or hashlib.sha256(baseline_bytes).hexdigest() == item.before_sha256
            )
            and _unified_diff_sha256(item.path, baseline_bytes, candidate)
            == item.unified_diff_sha256
        ):
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_source_digest_mismatch",
                "Candidate 源码与批准 Patch Manifest 不一致。",
            )
        result[item.path] = (
            baseline_bytes,
            candidate,
            bool(metadata.st_mode & stat.S_IXUSR),
        )
    return result


def _apply_exact(
    root: Path,
    patch_files: tuple[EvolutionPromotionPatchFile, ...],
    blobs: dict[str, tuple[bytes | None, bytes, bool]],
) -> tuple[EvolutionRevalidationReplayFile, ...]:
    receipts: list[EvolutionRevalidationReplayFile] = []
    for order, item in enumerate(patch_files, start=1):
        baseline, candidate, executable = blobs[item.path]
        target = root.joinpath(*PurePosixPath(item.path).parts)
        if item.operation == "modify":
            current = target.read_bytes()
            if hashlib.sha256(current).hexdigest() != item.before_sha256:
                raise EvolutionRevalidationReplayError(
                    "revalidation_replay_target_blob_mismatch",
                    "Exact-target 文件已偏离批准 baseline。",
                )
        elif target.exists():
            raise EvolutionRevalidationReplayError(
                "revalidation_replay_create_conflict",
                "待创建文件已存在于 exact target。",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, candidate, executable=executable)
        replay = target.read_bytes()
        receipts.append(
            EvolutionRevalidationReplayFile(
                order=order,
                path=item.path,
                operation=item.operation,
                baseline_sha256=item.before_sha256,
                candidate_sha256=item.after_sha256,
                replay_sha256=hashlib.sha256(replay).hexdigest(),
                unified_diff_sha256=_unified_diff_sha256(item.path, baseline, replay),
                executable=executable,
            )
        )
    status_paths = tuple(sorted(_status_paths(root)))
    expected_paths = tuple(item.path for item in patch_files)
    if status_paths != expected_paths:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_result_scope_mismatch",
            "Replay 结果包含批准范围外的文件变化。",
        )
    return tuple(receipts)


def _status_paths(root: Path) -> list[str]:
    payload = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [item for item in payload.split(b"\x00") if item]
    try:
        return [item[3:].decode("utf-8") for item in entries]
    except UnicodeDecodeError as exc:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_status_invalid",
            "Replay Git status 含无效路径编码。",
        ) from exc


def _atomic_write(path: Path, content: bytes, *, executable: bool) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755 if executable else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(root: Path) -> tuple[str, bytes]:
    return (
        _git_text(root, "rev-parse", "HEAD"),
        _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
    )


def _unified_diff_sha256(path: str, before: bytes | None, after: bytes) -> str:
    before_lines = [] if before is None else before.decode("utf-8").splitlines()
    after_lines = after.decode("utf-8").splitlines()
    unified = tuple(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return _sha256_payload(unified)


def _replay_tree_digest(files: tuple[EvolutionRevalidationReplayFile, ...]) -> str:
    return _sha256_payload([item.model_dump(mode="json") for item in files])


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_git_failed", "Replay Git 操作无法执行。"
        ) from exc
    if completed.returncode != 0:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_git_failed", "Replay Git 操作失败。"
        )
    return completed.stdout


def _git_bytes(root: Path, *args: str) -> bytes:
    return _git(root, *args)


def _git_text(root: Path, *args: str) -> str:
    try:
        return _git(root, *args).decode("utf-8").strip().lower()
    except UnicodeDecodeError as exc:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_git_output_invalid", "Replay Git 输出不是有效文本。"
        ) from exc


def _aware_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_clock_invalid", "Replay 时钟必须包含 UTC offset。"
        )
    return value


def _validated_receipt(
    receipt: EvolutionRevalidationReplayReceipt,
) -> EvolutionRevalidationReplayReceipt:
    try:
        return EvolutionRevalidationReplayReceipt.model_validate_json(
            receipt.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationReplayError(
            "revalidation_replay_receipt_invalid", "Replay Receipt 无法验证。"
        ) from exc


def _receipt_from_json(payload: object) -> EvolutionRevalidationReplayReceipt:
    if not isinstance(payload, str):
        raise ValueError("Replay receipt_json 类型无效。")
    return EvolutionRevalidationReplayReceipt.model_validate_json(payload)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_replays ("
        "replay_id TEXT PRIMARY KEY, replay_sha256 TEXT NOT NULL, "
        "request_id TEXT NOT NULL UNIQUE, request_sha256 TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_REPLAY_POLICY",
    "EvolutionRevalidationReplayError",
    "EvolutionRevalidationReplayExecutor",
    "EvolutionRevalidationReplayFile",
    "EvolutionRevalidationReplayReceipt",
    "EvolutionRevalidationReplayService",
    "EvolutionRevalidationReplayStore",
    "render_evolution_revalidation_replay",
]
