"""Shared exact candidate-worktree snapshots for evolution validation lanes."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.fingerprint import (
    TreeFingerprint,
    TreeFingerprintError,
    compute_tree_fingerprint,
)

_MAX_SOURCE_BYTES = 2_000_000
_GIT_TIMEOUT_SECONDS = 10


class EvolutionCandidateSnapshotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvolutionCandidateSourceBlob:
    path: str
    content: bytes
    sha256: str
    executable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("Candidate blob content 必须是 bytes。")
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError("Candidate blob SHA-256 与字节不一致。")
        if not isinstance(self.executable, bool):
            raise TypeError("Candidate blob executable 必须是 bool。")


@dataclass(frozen=True, slots=True)
class EvolutionCandidateWorktreeSnapshot:
    root: Path
    blobs: tuple[EvolutionCandidateSourceBlob, ...]
    fingerprint: TreeFingerprint


def capture_candidate_worktree_snapshot(
    lease: ExperimentWorktreeLease,
    plan: EvolutionValidationPlan,
    *,
    worktree_storage_dir: str | Path,
    now: datetime,
) -> EvolutionCandidateWorktreeSnapshot:
    """Capture exact candidate bytes after validating Lease, Git, and Plan state."""
    try:
        candidate_lease = ExperimentWorktreeLease.model_validate(
            lease.model_dump(mode="json")
        )
        validation_plan = EvolutionValidationPlan.model_validate(
            plan.model_dump(mode="json")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_snapshot_authority_invalid",
            "Candidate Snapshot authority 无效或已被篡改。",
        ) from exc
    if not (
        candidate_lease.lease_id == validation_plan.lease_id
        and candidate_lease.contract_id == validation_plan.contract_id
        and candidate_lease.manifest_sha256
        == validation_plan.contract_manifest_sha256
        and candidate_lease.baseline_commit == validation_plan.baseline_commit
        and candidate_lease.state is ExperimentLeaseState.ACTIVE
        and candidate_lease.worktree_ready
        and not candidate_lease.execution_ready
        and validation_plan.schema_version == 2
        and all(item.operation in {"create", "modify"} for item in validation_plan.files)
    ):
        raise EvolutionCandidateSnapshotError(
            "candidate_snapshot_authority_mismatch",
            "Candidate Snapshot 的 Lease 与 Validation Plan 不一致。",
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise EvolutionCandidateSnapshotError(
            "candidate_snapshot_clock_invalid",
            "Candidate Snapshot 时钟必须包含时区。",
        )
    if datetime.fromisoformat(candidate_lease.expires_at) <= now:
        raise EvolutionCandidateSnapshotError(
            "candidate_lease_expired",
            "Candidate Lease 已过期，不能捕获验证 Snapshot。",
        )
    storage = Path(worktree_storage_dir).expanduser().resolve()
    try:
        root = Path(candidate_lease.worktree_path).resolve(strict=True)
    except OSError as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_worktree_missing",
            "Candidate worktree 不存在或无法读取。",
        ) from exc
    if root.parent != storage or root.name != candidate_lease.worktree_name:
        raise EvolutionCandidateSnapshotError(
            "candidate_worktree_unmanaged",
            "Candidate worktree 不属于受管 Lease 存储目录。",
        )
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        head = _git(root, "rev-parse", "--verify", "HEAD").decode().strip().lower()
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    except UnicodeDecodeError as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_git_read_failed",
            "Candidate Git identity 不是有效文本。",
        ) from exc
    if top != root or head != candidate_lease.baseline_commit or branch != candidate_lease.branch:
        raise EvolutionCandidateSnapshotError(
            "candidate_git_identity_mismatch",
            "Candidate worktree HEAD 或 branch 已偏离 Lease。",
        )
    status = _parse_candidate_status(_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ))
    expected_status = {
        item.path: (b"??" if item.operation == "create" else b" M")
        for item in validation_plan.files
    }
    if status != expected_status:
        raise EvolutionCandidateSnapshotError(
            "candidate_status_mismatch",
            "Candidate worktree 含缺失、额外、暂存或类型不符的改动。",
        )
    before = _fingerprint(root, "无法读取 candidate worktree fingerprint。")
    if (
        before.head != candidate_lease.baseline_commit
        or before.dirty_paths != tuple(sorted(expected_status))
    ):
        raise EvolutionCandidateSnapshotError(
            "candidate_fingerprint_mismatch",
            "Candidate worktree fingerprint 与 Plan path 集合不一致。",
        )
    blobs: list[EvolutionCandidateSourceBlob] = []
    for item in validation_plan.files:
        path = root.joinpath(*PurePosixPath(item.path).parts)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise EvolutionCandidateSnapshotError(
                "candidate_file_missing",
                "Candidate 文件不存在或无法读取。",
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise EvolutionCandidateSnapshotError(
                "candidate_file_type_unsafe",
                "Candidate 文件必须是普通文件。",
            )
        executable = _candidate_executable(root, item.path, item.operation)
        if os.name != "nt" and bool(metadata.st_mode & stat.S_IXUSR) is not executable:
            raise EvolutionCandidateSnapshotError(
                "candidate_file_mode_mismatch",
                "Candidate 文件 executable mode 已偏离 baseline/create authority。",
            )
        if not 0 <= metadata.st_size <= _MAX_SOURCE_BYTES:
            raise EvolutionCandidateSnapshotError(
                "candidate_file_too_large",
                "Candidate 验证文件不能超过 2 MiB。",
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise EvolutionCandidateSnapshotError(
                "candidate_file_read_failed",
                "Candidate 文件无法完整读取。",
            ) from exc
        if (
            len(content) != metadata.st_size
            or hashlib.sha256(content).hexdigest() != item.candidate_sha256
        ):
            raise EvolutionCandidateSnapshotError(
                "candidate_file_digest_mismatch",
                "Candidate 文件与 Validation Plan after digest 不一致。",
            )
        blobs.append(EvolutionCandidateSourceBlob(
            path=item.path,
            content=content,
            sha256=item.candidate_sha256,
            executable=executable,
        ))
    after = _fingerprint(root, "无法在快照读取后重新读取 candidate fingerprint。")
    if after != before:
        raise EvolutionCandidateSnapshotError(
            "candidate_worktree_changed_during_capture",
            "Candidate worktree 在快照读取期间发生变化。",
        )
    return EvolutionCandidateWorktreeSnapshot(
        root=root,
        blobs=tuple(blobs),
        fingerprint=before,
    )


def revalidate_candidate_worktree_snapshot(
    snapshot: EvolutionCandidateWorktreeSnapshot,
) -> None:
    """Fail closed when candidate state changes after snapshot capture."""
    if not isinstance(snapshot, EvolutionCandidateWorktreeSnapshot):
        raise TypeError("Candidate Snapshot 类型无效。")
    current = _fingerprint(
        snapshot.root,
        "无法重新读取 candidate worktree fingerprint。",
    )
    if current != snapshot.fingerprint:
        raise EvolutionCandidateSnapshotError(
            "candidate_worktree_changed_after_snapshot",
            "Candidate worktree 在 Snapshot 捕获后发生变化。",
        )


def _parse_candidate_status(raw: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" " or b"R" in record[:2] or b"C" in record[:2]:
            raise EvolutionCandidateSnapshotError(
                "candidate_status_unrecognized",
                "Candidate Git status 包含不可识别的 rename/copy 状态。",
            )
        try:
            path = record[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvolutionCandidateSnapshotError(
                "candidate_status_unrecognized",
                "Candidate Git path 不是 UTF-8。",
            ) from exc
        if path in result:
            raise EvolutionCandidateSnapshotError(
                "candidate_status_duplicate",
                "Candidate Git status 包含重复路径。",
            )
        result[path] = record[:2]
    return result


def _candidate_executable(root: Path, path: str, operation: str) -> bool:
    raw = _git(root, "ls-files", "--stage", "-z", "--", path)
    if operation == "create":
        if raw:
            raise EvolutionCandidateSnapshotError(
                "candidate_create_baseline_conflict",
                "Create candidate path 已存在于 baseline index。",
            )
        return False
    records = tuple(item for item in raw.split(b"\0") if item)
    if len(records) != 1:
        raise EvolutionCandidateSnapshotError(
            "candidate_baseline_mode_missing",
            "Modify candidate 缺少唯一 baseline file mode。",
        )
    try:
        header, raw_path = records[0].split(b"\t", 1)
        mode, _object_id, stage = header.split(b" ", 2)
        decoded_path = raw_path.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_baseline_mode_invalid",
            "Candidate baseline index mode 无法验证。",
        ) from exc
    if decoded_path != path or stage != b"0" or mode not in {b"100644", b"100755"}:
        raise EvolutionCandidateSnapshotError(
            "candidate_baseline_mode_invalid",
            "Candidate baseline index 不是 stage-0 普通文件。",
        )
    return mode == b"100755"


def _fingerprint(root: Path, message: str) -> TreeFingerprint:
    try:
        return compute_tree_fingerprint(root)
    except (OSError, TreeFingerprintError) as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_fingerprint_read_failed",
            message,
        ) from exc


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "--literal-pathspecs",
                "-C",
                str(root),
                *args,
            ],
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvolutionCandidateSnapshotError(
            "candidate_git_read_failed",
            "无法读取 candidate Git 状态。",
        ) from exc
    if completed.returncode != 0:
        raise EvolutionCandidateSnapshotError(
            "candidate_git_read_failed",
            "无法读取 candidate Git 状态。",
        )
    return completed.stdout


__all__ = [
    "EvolutionCandidateSnapshotError",
    "EvolutionCandidateSourceBlob",
    "EvolutionCandidateWorktreeSnapshot",
    "capture_candidate_worktree_snapshot",
    "revalidate_candidate_worktree_snapshot",
]
