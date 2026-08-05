"""Durable fenced three-way replay for linearly advanced Evolution targets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPatchFile,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayError,
    _atomic_write,
    _capture_candidate,
    _git,
    _git_text,
    _identity,
    _status_paths,
)
from naumi_agent.evolution.revalidation_requests import EvolutionRevalidationRequestView

EVOLUTION_REVALIDATION_REBASE_POLICY = "evolution-revalidation-rebase-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_OUTCOME_BYTES = 256 * 1_024
_MAX_SOURCE_BYTES = 2 * 1_024 * 1_024
_CLAIM_SECONDS = 120
_GIT_TIMEOUT_SECONDS = 20


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationRebaseStatus(StrEnum):
    SUCCEEDED = "succeeded"
    CONFLICTED = "conflicted"
    FAILED = "failed"


class EvolutionRevalidationRebaseFile(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    baseline_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    target_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    result_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    merge_strategy: Literal[
        "candidate_on_unchanged_target",
        "already_equivalent",
        "three_way_clean",
        "create_on_absent_target",
        "conflict",
    ]
    executable: bool | None = None

    @model_validator(mode="after")
    def _file_is_exact(self) -> Self:
        if (self.operation == "modify") is not (self.baseline_sha256 is not None):
            raise ValueError("Rebase file operation/baseline digest 不一致。")
        conflict = self.merge_strategy == "conflict"
        if conflict is not (self.result_sha256 is None and self.executable is None):
            raise ValueError("Rebase conflict/result 投影不一致。")
        return self


class EvolutionRevalidationRebaseOutcome(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rebase-v1"] = (
        EVOLUTION_REVALIDATION_REBASE_POLICY
    )
    outcome_id: str = Field(pattern=r"^evrebase_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    original_target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    current_target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    epoch: int = Field(ge=1)
    status: EvolutionRevalidationRebaseStatus
    files: tuple[EvolutionRevalidationRebaseFile, ...] = Field(max_length=16)
    conflict_paths: tuple[str, ...] = Field(max_length=16)
    result_tree_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    failure_code: str = Field(default="", pattern=r"^(?:|[a-z][a-z0-9_.-]{0,127})$")
    source_worktree_unchanged: bool
    main_worktree_unchanged: bool
    target_branch_unchanged: bool
    detached_worktree_removed: bool
    validation_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _outcome_is_exact(self) -> Self:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Rebase files 必须排序且不得重复。")
        if tuple(item.order for item in self.files) != tuple(range(1, len(self.files) + 1)):
            raise ValueError("Rebase files 顺序不连续。")
        conflicts = tuple(item.path for item in self.files if item.merge_strategy == "conflict")
        if self.conflict_paths != conflicts:
            raise ValueError("Rebase conflict paths 投影不一致。")
        if self.status is EvolutionRevalidationRebaseStatus.SUCCEEDED:
            if (
                conflicts
                or not self.files
                or self.result_tree_sha256 is None
                or self.failure_code
                or not all(
                    (
                        self.source_worktree_unchanged,
                        self.main_worktree_unchanged,
                        self.target_branch_unchanged,
                        self.detached_worktree_removed,
                    )
                )
            ):
                raise ValueError("Succeeded Rebase Outcome 不完整。")
        elif self.status is EvolutionRevalidationRebaseStatus.CONFLICTED:
            if (
                not conflicts
                or self.result_tree_sha256 is not None
                or self.failure_code
                or not all(
                    (
                        self.source_worktree_unchanged,
                        self.main_worktree_unchanged,
                        self.target_branch_unchanged,
                        self.detached_worktree_removed,
                    )
                )
            ):
                raise ValueError("Conflicted Rebase Outcome 不完整。")
        elif not self.failure_code or self.files or self.result_tree_sha256 is not None:
            raise ValueError("Failed Rebase Outcome 不完整。")
        parsed = datetime.fromisoformat(self.created_at)
        if parsed.utcoffset() is None:
            raise ValueError("Rebase Outcome created_at 必须包含 UTC offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"outcome_id", "outcome_sha256"})
        )
        if not hmac.compare_digest(self.outcome_sha256, digest):
            raise ValueError("Rebase Outcome 摘要不一致。")
        if self.outcome_id != f"evrebase_{digest[:24]}":
            raise ValueError("Rebase Outcome identity 不一致。")
        return self


class EvolutionRevalidationRebaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Claim(_StrictModel):
    request_id: str
    target_head: str
    epoch: int
    owner_token: str
    worktree_path: str
    expires_at: str


class EvolutionRevalidationRebaseStore:
    """Cross-process claim, fencing, and immutable terminal outcome store."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def claim(
        self,
        *,
        request_id: str,
        request_sha256: str,
        target_head: str,
        worktree_path: str,
        now: datetime,
    ) -> tuple[_Claim | None, EvolutionRevalidationRebaseOutcome | None]:
        current = _aware(now)
        owner = secrets.token_hex(32)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_rebase_attempts "
                    "WHERE request_id = ? AND target_head = ?",
                    (request_id, target_head),
                )
            ).fetchone()
            if row is not None and row["outcome_json"]:
                outcome = _outcome_from_json(row["outcome_json"])
                await db.rollback()
                return None, outcome
            if row is not None and datetime.fromisoformat(row["expires_at"]) > current:
                await db.rollback()
                raise EvolutionRevalidationRebaseError(
                    "revalidation_rebase_claim_busy",
                    "同一 Revalidation Request 正由另一个执行者处理。",
                )
            epoch = 1 if row is None else int(row["epoch"]) + 1
            expires = (current + timedelta(seconds=_CLAIM_SECONDS)).isoformat()
            owner_sha = hashlib.sha256(owner.encode()).hexdigest()
            if row is None:
                await db.execute(
                    "INSERT INTO evolution_revalidation_rebase_attempts "
                    "(request_id, request_sha256, target_head, epoch, owner_sha256, state, "
                    "worktree_path, expires_at, outcome_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, '', ?)",
                    (
                        request_id,
                        request_sha256,
                        target_head,
                        epoch,
                        owner_sha,
                        worktree_path,
                        expires,
                        current.isoformat(),
                    ),
                )
            else:
                if not (
                    row["request_sha256"] == request_sha256
                    and row["target_head"] == target_head
                ):
                    await db.rollback()
                    raise EvolutionRevalidationRebaseError(
                        "revalidation_rebase_claim_conflict",
                        "Rebase claim 已绑定不同 Request 或 target。",
                    )
                await db.execute(
                    "UPDATE evolution_revalidation_rebase_attempts SET epoch = ?, "
                    "owner_sha256 = ?, state = 'active', worktree_path = ?, expires_at = ?, "
                    "updated_at = ? WHERE request_id = ? AND target_head = ?",
                    (
                        epoch,
                        owner_sha,
                        worktree_path,
                        expires,
                        current.isoformat(),
                        request_id,
                        target_head,
                    ),
                )
            await db.commit()
        return (
            _Claim(
                request_id=request_id,
                target_head=target_head,
                epoch=epoch,
                owner_token=owner,
                worktree_path=worktree_path,
                expires_at=expires,
            ),
            None,
        )

    async def bind_worktree_path(
        self,
        *,
        claim: _Claim,
        worktree_path: str,
        now: datetime,
    ) -> _Claim:
        """Bind the epoch-specific isolation path while fencing stale executors."""
        owner_sha = hashlib.sha256(claim.owner_token.encode()).hexdigest()
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE evolution_revalidation_rebase_attempts SET worktree_path = ?, "
                "updated_at = ? WHERE request_id = ? AND target_head = ? AND epoch = ? "
                "AND owner_sha256 = ? AND state = 'active' AND outcome_json = ''",
                (
                    worktree_path,
                    _aware(now).isoformat(),
                    claim.request_id,
                    claim.target_head,
                    claim.epoch,
                    owner_sha,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise EvolutionRevalidationRebaseError(
                    "revalidation_rebase_fenced",
                    "Rebase 执行者 epoch 已失效，禁止绑定隔离 worktree。",
                )
            await db.commit()
        return claim.model_copy(update={"worktree_path": worktree_path})

    async def finish(
        self,
        *,
        claim: _Claim,
        outcome: EvolutionRevalidationRebaseOutcome,
        now: datetime,
    ) -> EvolutionRevalidationRebaseOutcome:
        item = _validated_outcome(outcome)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_OUTCOME_BYTES:
            raise EvolutionRevalidationRebaseError(
                "revalidation_rebase_outcome_oversized",
                "Rebase Outcome 超过 256 KiB 上限。",
            )
        owner_sha = hashlib.sha256(claim.owner_token.encode()).hexdigest()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE evolution_revalidation_rebase_attempts SET state = ?, "
                "outcome_json = ?, updated_at = ? WHERE request_id = ? AND epoch = ? "
                "AND target_head = ? AND owner_sha256 = ? AND state = 'active' "
                "AND outcome_json = ''",
                (
                    item.status.value,
                    encoded,
                    _aware(now).isoformat(),
                    claim.request_id,
                    claim.epoch,
                    claim.target_head,
                    owner_sha,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise EvolutionRevalidationRebaseError(
                    "revalidation_rebase_fenced",
                    "Rebase 执行者 epoch 已失效，禁止提交结果。",
                )
            await db.commit()
        return item


class EvolutionRevalidationRebaseExecutor:
    def __init__(
        self,
        *,
        store: EvolutionRevalidationRebaseStore,
        worktree_storage_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._storage = Path(worktree_storage_dir).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        request_view: EvolutionRevalidationRequestView,
        package_input: EvolutionPromotionPackageInput,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionRevalidationRebaseOutcome:
        view = EvolutionRevalidationRequestView.model_validate_json(
            request_view.model_dump_json()
        )
        package = EvolutionPromotionPackageInput.model_validate_json(
            package_input.model_dump_json()
        )
        source_lease = ExperimentWorktreeLease.model_validate_json(lease.model_dump_json())
        _require_rebase_authority(view, package, source_lease, self._storage, self._clock())
        request = view.request
        assert view.current_target_head is not None
        prefix = f"rebase-{request.request_id[-12:]}-{view.current_target_head[:8]}-"
        claim_path = self._storage / f"{prefix}pending"
        claim, terminal = await self._store.claim(
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            target_head=view.current_target_head,
            worktree_path=str(claim_path),
            now=self._clock(),
        )
        if terminal is not None:
            return terminal
        assert claim is not None
        actual_path = self._storage / f"{prefix}{claim.epoch}"
        claim = await self._store.bind_worktree_path(
            claim=claim,
            worktree_path=str(actual_path),
            now=self._clock(),
        )
        workspace: Path | None = None
        source: Path | None = None
        source_before: tuple[str, bytes] | None = None
        main_before: tuple[str, bytes] | None = None
        target_before: str | None = None
        added = False
        files: tuple[EvolutionRevalidationRebaseFile, ...] = ()
        status = EvolutionRevalidationRebaseStatus.FAILED
        failure_code = ""
        result_tree: str | None = None
        detached_removed = True
        try:
            workspace = Path(request.workspace_root).resolve(strict=True)
            main_before = _identity(workspace)
            target_before = _git_text(workspace, "rev-parse", request.target_branch)
            if target_before != view.current_target_head:
                raise EvolutionRevalidationRebaseError(
                    "target_moved_after_claim",
                    "Target 在 claim 后发生变化，禁止继续 rebase。",
                )
            source = Path(source_lease.worktree_path).resolve(strict=True)
            await self._cleanup_stale(workspace, prefix=prefix, keep=actual_path)
            main_before = _identity(workspace)
            source_before = _identity(source)
            blobs = _capture_candidate(source, package.patch.files, request.baseline_commit)
            self._storage.mkdir(parents=True, exist_ok=True)
            _git(workspace, "worktree", "add", "--detach", str(actual_path), target_before)
            added = True
            files = _apply_three_way(actual_path, package.patch.files, blobs)
            if any(item.merge_strategy == "conflict" for item in files):
                status = EvolutionRevalidationRebaseStatus.CONFLICTED
            else:
                status = EvolutionRevalidationRebaseStatus.SUCCEEDED
                result_tree = _sha256_payload(
                    [item.model_dump(mode="json") for item in files]
                )
        except (EvolutionRevalidationReplayError, EvolutionRevalidationRebaseError) as exc:
            failure_code = exc.code
        except (OSError, subprocess.SubprocessError):
            failure_code = "rebase_execution_failed"
        finally:
            try:
                if added and workspace is not None:
                    _git(workspace, "worktree", "remove", "--force", str(actual_path))
                if workspace is not None:
                    _git(workspace, "worktree", "prune")
            except EvolutionRevalidationReplayError:
                failure_code = "rebase_cleanup_failed"
                status = EvolutionRevalidationRebaseStatus.FAILED
                files = ()
                result_tree = None
                detached_removed = False
        if actual_path.exists():
            status = EvolutionRevalidationRebaseStatus.FAILED
            failure_code = "rebase_cleanup_failed"
            files = ()
            result_tree = None
            detached_removed = False
        source_unchanged = _identity_unchanged(source, source_before)
        main_unchanged = _identity_unchanged(workspace, main_before)
        target_unchanged = _target_unchanged(
            workspace,
            branch=request.target_branch,
            expected=target_before,
        )
        if not source_unchanged:
            status, failure_code, files, result_tree = (
                EvolutionRevalidationRebaseStatus.FAILED,
                "source_changed_during_rebase",
                (),
                None,
            )
        if not main_unchanged:
            status, failure_code, files, result_tree = (
                EvolutionRevalidationRebaseStatus.FAILED,
                "main_changed_during_rebase",
                (),
                None,
            )
        if not target_unchanged:
            status, failure_code, files, result_tree = (
                EvolutionRevalidationRebaseStatus.FAILED,
                "target_changed_during_rebase",
                (),
                None,
            )
        if status is EvolutionRevalidationRebaseStatus.FAILED and not failure_code:
            failure_code = "rebase_execution_failed"
        outcome = _build_outcome(
            view=view,
            package=package,
            lease=source_lease,
            epoch=claim.epoch,
            status=status,
            files=files,
            result_tree=result_tree,
            failure_code=failure_code,
            source_worktree_unchanged=source_unchanged,
            main_worktree_unchanged=main_unchanged,
            target_branch_unchanged=target_unchanged,
            detached_worktree_removed=detached_removed,
            now=self._clock(),
        )
        return await self._store.finish(claim=claim, outcome=outcome, now=self._clock())

    async def _cleanup_stale(self, workspace: Path, *, prefix: str, keep: Path) -> None:
        self._storage.mkdir(parents=True, exist_ok=True)
        for path in self._storage.glob(f"{prefix}*"):
            if path == keep:
                continue
            try:
                _git(workspace, "worktree", "remove", "--force", str(path))
            except EvolutionRevalidationReplayError:
                if path.exists():
                    raise EvolutionRevalidationRebaseError(
                        "revalidation_rebase_recovery_failed",
                        "旧 Rebase worktree 无法安全清理。",
                    )
        _git(workspace, "worktree", "prune")


def _require_rebase_authority(
    view: EvolutionRevalidationRequestView,
    package: EvolutionPromotionPackageInput,
    lease: ExperimentWorktreeLease,
    storage: Path,
    now: datetime,
) -> None:
    request = view.request
    if not (
        view.execution_eligible
        and view.current_status == "stale"
        and view.decision_rebase_eligible
        and view.package_current
        and not view.target_current
        and view.current_target_relation == "advanced"
        and view.current_target_head is not None
    ):
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_authority_ineligible",
            "Revalidation Request 不具备线性 target rebase authority。",
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
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_authority_mismatch",
            "Rebase Request、Promotion Input 与 Experiment Lease 绑定不一致。",
        )
    if datetime.fromisoformat(lease.expires_at) <= _aware(now):
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_lease_expired",
            "Experiment Lease 已过期，不能读取 Candidate 源码。",
        )
    source = Path(lease.worktree_path).resolve()
    if source.parent != storage or source.name != lease.worktree_name:
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_worktree_unmanaged",
            "Candidate worktree 不属于受管存储目录。",
        )


def _identity_unchanged(
    root: Path | None,
    before: tuple[str, bytes] | None,
) -> bool:
    if root is None or before is None:
        return False
    try:
        return _identity(root) == before
    except (OSError, EvolutionRevalidationReplayError):
        return False


def _target_unchanged(
    root: Path | None,
    *,
    branch: str,
    expected: str | None,
) -> bool:
    if root is None or expected is None:
        return False
    try:
        return _git_text(root, "rev-parse", branch) == expected
    except (OSError, EvolutionRevalidationReplayError):
        return False


def _apply_three_way(
    root: Path,
    patch_files: tuple[EvolutionPromotionPatchFile, ...],
    blobs: dict[str, tuple[bytes | None, bytes, bool]],
) -> tuple[EvolutionRevalidationRebaseFile, ...]:
    planned: list[tuple[EvolutionPromotionPatchFile, bytes | None, bool, str, bytes | None]] = []
    for item in patch_files:
        baseline, candidate, candidate_executable = blobs[item.path]
        relative = PurePosixPath(item.path)
        target = root.joinpath(*relative.parts)
        current: bytes | None = None
        current_executable = candidate_executable
        if _path_is_unsafe(root, relative):
            strategy, merged = "conflict", None
        elif target.exists():
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOURCE_BYTES:
                strategy, merged = "conflict", None
            else:
                current = target.read_bytes()
                current_executable = bool(metadata.st_mode & stat.S_IXUSR)
                strategy, merged = _merge_file(
                    operation=item.operation,
                    baseline=baseline,
                    current=current,
                    candidate=candidate,
                    mode_compatible=current_executable == candidate_executable,
                )
        else:
            strategy, merged = _merge_file(
                operation=item.operation,
                baseline=baseline,
                current=None,
                candidate=candidate,
                mode_compatible=True,
            )
        planned.append((item, current, current_executable, strategy, merged))
    if any(strategy == "conflict" for _, _, _, strategy, _ in planned):
        return tuple(
            _file_outcome(
                order=index,
                item=item,
                target=current,
                result=None,
                strategy="conflict" if strategy == "conflict" else strategy,
                executable=None if strategy == "conflict" else executable,
            )
            for index, (item, current, executable, strategy, _merged) in enumerate(
                planned, start=1
            )
        )
    results: list[EvolutionRevalidationRebaseFile] = []
    for index, (item, current, executable, strategy, merged) in enumerate(planned, start=1):
        assert merged is not None
        target = root.joinpath(*PurePosixPath(item.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, merged, executable=executable)
        result = target.read_bytes()
        results.append(
            _file_outcome(
                order=index,
                item=item,
                target=current,
                result=result,
                strategy=strategy,
                executable=executable,
            )
        )
    if tuple(sorted(_status_paths(root))) != tuple(item.path for item in patch_files):
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_scope_mismatch",
            "Rebase 结果包含批准范围外的变化。",
        )
    return tuple(results)


def _path_is_unsafe(root: Path, relative: PurePosixPath) -> bool:
    """Reject symlink traversal and non-directory ancestors before any write."""
    cursor = root
    last = len(relative.parts) - 1
    for index, part in enumerate(relative.parts):
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if index != last and not stat.S_ISDIR(metadata.st_mode):
            return True
    return False


def _merge_file(
    *,
    operation: Literal["modify", "create"],
    baseline: bytes | None,
    current: bytes | None,
    candidate: bytes,
    mode_compatible: bool,
) -> tuple[str, bytes | None]:
    if not mode_compatible:
        return "conflict", None
    if operation == "create":
        if current is None:
            return "create_on_absent_target", candidate
        if current == candidate:
            return "already_equivalent", current
        return "conflict", None
    assert baseline is not None
    if current is None:
        return "conflict", None
    if current == baseline:
        return "candidate_on_unchanged_target", candidate
    if current == candidate:
        return "already_equivalent", current
    merged = _git_merge_file(current=current, baseline=baseline, candidate=candidate)
    if merged is None:
        return "conflict", None
    return "three_way_clean", merged


def _git_merge_file(*, current: bytes, baseline: bytes, candidate: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="naumi-rebase-") as directory:
        root = Path(directory)
        paths = (root / "current", root / "baseline", root / "candidate")
        for path, content in zip(paths, (current, baseline, candidate), strict=True):
            path.write_bytes(content)
        completed = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", *(str(path) for path in paths)],
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    if completed.returncode == 0:
        return completed.stdout
    if completed.returncode == 1:
        return None
    raise EvolutionRevalidationRebaseError(
        "revalidation_rebase_merge_failed",
        "Git three-way merge 无法执行。",
    )


def _file_outcome(
    *,
    order: int,
    item: EvolutionPromotionPatchFile,
    target: bytes | None,
    result: bytes | None,
    strategy: str,
    executable: bool | None,
) -> EvolutionRevalidationRebaseFile:
    return EvolutionRevalidationRebaseFile(
        order=order,
        path=item.path,
        operation=item.operation,
        baseline_sha256=item.before_sha256,
        target_sha256=None if target is None else hashlib.sha256(target).hexdigest(),
        candidate_sha256=item.after_sha256,
        result_sha256=None if result is None else hashlib.sha256(result).hexdigest(),
        merge_strategy=strategy,
        executable=executable,
    )


def _build_outcome(
    *,
    view: EvolutionRevalidationRequestView,
    package: EvolutionPromotionPackageInput,
    lease: ExperimentWorktreeLease,
    epoch: int,
    status: EvolutionRevalidationRebaseStatus,
    files: tuple[EvolutionRevalidationRebaseFile, ...],
    now: datetime,
    result_tree: str | None = None,
    failure_code: str = "",
    source_worktree_unchanged: bool = True,
    main_worktree_unchanged: bool = True,
    target_branch_unchanged: bool = True,
    detached_worktree_removed: bool = True,
) -> EvolutionRevalidationRebaseOutcome:
    request = view.request
    assert view.current_target_head is not None
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_REBASE_POLICY,
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "promotion_input_id": package.input_id,
        "promotion_input_sha256": package.input_sha256,
        "lease_id": lease.lease_id,
        "workspace_root": request.workspace_root,
        "original_target_head": request.target_head,
        "current_target_head": view.current_target_head,
        "epoch": epoch,
        "status": status.value,
        "files": [item.model_dump(mode="json") for item in files],
        "conflict_paths": tuple(
            item.path for item in files if item.merge_strategy == "conflict"
        ),
        "result_tree_sha256": result_tree,
        "failure_code": failure_code,
        "source_worktree_unchanged": source_worktree_unchanged,
        "main_worktree_unchanged": main_worktree_unchanged,
        "target_branch_unchanged": target_branch_unchanged,
        "detached_worktree_removed": detached_worktree_removed,
        "validation_executed": False,
        "promotion_authority": False,
        "created_at": _aware(now).isoformat(),
    }
    digest = _sha256_payload(payload)
    return EvolutionRevalidationRebaseOutcome.model_validate(
        {**payload, "outcome_id": f"evrebase_{digest[:24]}", "outcome_sha256": digest}
    )


def render_evolution_revalidation_rebase(
    outcome: EvolutionRevalidationRebaseOutcome,
) -> str:
    item = _validated_outcome(outcome)
    lines = [
        f"# Evolution Revalidation Rebase `{item.outcome_id}`",
        "",
        f"- Status：`{item.status.value}`",
        f"- Target：`{item.original_target_head}` → `{item.current_target_head}`",
        f"- Epoch：{item.epoch}",
        f"- Files：{len(item.files)}",
        "- Conflicts：" + (", ".join(item.conflict_paths) or "none"),
        "- Detached worktree：已清理",
        "- Validation / Promotion authority：`false` / `false`",
    ]
    if item.failure_code:
        lines.append(f"- Failure：`{item.failure_code}`")
    return "\n".join(lines)


def _validated_outcome(value: object) -> EvolutionRevalidationRebaseOutcome:
    try:
        if not isinstance(value, EvolutionRevalidationRebaseOutcome):
            raise TypeError("outcome type invalid")
        return EvolutionRevalidationRebaseOutcome.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_outcome_invalid",
            "Rebase Outcome 无法验证。",
        ) from exc


def _outcome_from_json(value: object) -> EvolutionRevalidationRebaseOutcome:
    if not isinstance(value, str):
        raise ValueError("Rebase outcome_json 类型无效。")
    return EvolutionRevalidationRebaseOutcome.model_validate_json(value)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvolutionRevalidationRebaseError(
            "revalidation_rebase_clock_invalid",
            "Rebase 时钟必须包含 UTC offset。",
        )
    return value


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rebase_attempts ("
        "request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, "
        "target_head TEXT NOT NULL, epoch INTEGER NOT NULL, owner_sha256 TEXT NOT NULL, "
        "state TEXT NOT NULL, worktree_path TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "outcome_json TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, "
        "PRIMARY KEY (request_id, target_head))"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_REBASE_POLICY",
    "EvolutionRevalidationRebaseError",
    "EvolutionRevalidationRebaseExecutor",
    "EvolutionRevalidationRebaseFile",
    "EvolutionRevalidationRebaseOutcome",
    "EvolutionRevalidationRebaseStatus",
    "EvolutionRevalidationRebaseStore",
    "render_evolution_revalidation_rebase",
]
