"""Trusted Harness Profile checks executed through the isolated Shell Worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from naumi_agent.daemons.shell_worker import (
    ShellCommandRequest,
    ShellCommandSpec,
    ShellJobExecutionResult,
    ShellWorkerCoordinator,
    ShellWorkerError,
    ShellWorkerStatus,
)
from naumi_agent.daemons.tool_jobs import ToolJobRequest
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerHealthReport,
)
from naumi_agent.harness.checks import ProfileRevalidator, validate_run_id
from naumi_agent.harness.fingerprint import (
    TreeFingerprintError,
    compute_tree_fingerprint,
)
from naumi_agent.harness.models import HarnessCheckSpec

_CHECK_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GIT_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_GIT_METADATA_BYTES = 64 * 1024 * 1024


class HarnessSandboxCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    RESOURCE_LIMIT = "resource_limit"
    BLOCKED = "blocked"
    STALE = "stale"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True, slots=True)
class AdmittedSandboxShellJob:
    job_id: str
    tool_job_request: ToolJobRequest
    shell_request: ShellCommandRequest
    worker_health: WorkerHealthReport
    requirements: WorkerAdmissionRequirements
    dispatch_id: str
    coordinator: ShellWorkerCoordinator


SandboxJobAdmitter = Callable[
    [ShellCommandSpec],
    Awaitable[AdmittedSandboxShellJob],
]


@dataclass(frozen=True, slots=True)
class HarnessSandboxCheckResult:
    check_id: str
    run_id: str
    status: HarnessSandboxCheckStatus
    source_revision: str | None
    source_tree_sha256: str
    snapshot_manifest_sha256: str
    profile_digest: str
    job_id: str | None
    lifecycle_receipt_sha256: str | None
    output: str
    exit_code: int | None
    duration_ms: int
    artifact_path: Path | None
    message: str


@dataclass(frozen=True, slots=True)
class _GitRevisionEntry:
    mode: str
    object_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class _GitRevisionSnapshot:
    commit: str
    tree_sha256: str
    entries: tuple[_GitRevisionEntry, ...]


class HarnessSandboxCheckRunner:
    """Snapshot one trusted workspace and execute one Profile check via ARC-04."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        sandbox_root: str | Path,
        artifact_root: str | Path,
        max_snapshot_files: int = 20_000,
        max_snapshot_bytes: int = 512 * 1024 * 1024,
        max_memory_bytes: int = 1024 * 1024 * 1024,
        max_output_bytes: int = 8 * 1024 * 1024,
        max_cpu_seconds: int = 300,
    ) -> None:
        self.workspace_root = _require_existing_absolute_directory(
            workspace_root,
            field="workspace_root",
        )
        self.sandbox_root = _resolve_owned_directory(
            sandbox_root,
            field="sandbox_root",
        )
        self.artifact_root = _resolve_owned_directory(
            artifact_root,
            field="artifact_root",
        )
        if _paths_overlap(self.sandbox_root, self.artifact_root):
            raise ValueError("sandbox_root 与 artifact_root 不得重叠。")
        self._max_snapshot_files = _bounded_int(
            max_snapshot_files,
            field="max_snapshot_files",
            minimum=1,
            maximum=100_000,
        )
        self._max_snapshot_bytes = _bounded_int(
            max_snapshot_bytes,
            field="max_snapshot_bytes",
            minimum=1,
            maximum=8 * 1024 * 1024 * 1024,
        )
        self._max_memory_bytes = _bounded_int(
            max_memory_bytes,
            field="max_memory_bytes",
            minimum=64 * 1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
        )
        self._max_output_bytes = _bounded_int(
            max_output_bytes,
            field="max_output_bytes",
            minimum=1_024,
            maximum=128 * 1024 * 1024,
        )
        self._max_cpu_seconds = _bounded_int(
            max_cpu_seconds,
            field="max_cpu_seconds",
            minimum=1,
            maximum=86_400,
        )

    async def run(
        self,
        *,
        run_id: str,
        check: HarnessCheckSpec,
        profile_digest: str,
        profile_is_current: ProfileRevalidator,
        admit_job: SandboxJobAdmitter,
        cancel_event: asyncio.Event | None = None,
        source_revision: str | None = None,
        expected_source_tree_sha256: str | None = None,
    ) -> HarnessSandboxCheckResult:
        run_id = validate_run_id(run_id)
        if not isinstance(check, HarnessCheckSpec):
            raise TypeError("check 必须是 HarnessCheckSpec。")
        if not _CHECK_ID.fullmatch(check.id):
            raise ValueError("check.id 格式无效。")
        _require_sha256(profile_digest, field="profile_digest")
        if not callable(profile_is_current) or not callable(admit_job):
            raise TypeError("profile_is_current 与 admit_job 必须可调用。")
        if (source_revision is None) != (expected_source_tree_sha256 is None):
            raise ValueError(
                "source_revision 与 expected_source_tree_sha256 必须同时提供。"
            )
        revision_snapshot: _GitRevisionSnapshot | None = None
        if not await profile_is_current():
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message="Harness Profile 在 Sandbox 快照前已失去信任，检查未执行。",
            )
        if source_revision is not None:
            revision_snapshot = await asyncio.to_thread(
                _resolve_git_revision_snapshot,
                self.workspace_root,
                source_revision,
                expected_source_tree_sha256,
            )
        _ensure_owned_directory(self.sandbox_root)
        _ensure_owned_directory(self.artifact_root)
        if revision_snapshot is None:
            try:
                source_before_sha256 = (
                    await asyncio.to_thread(
                        compute_tree_fingerprint,
                        self.workspace_root,
                    )
                ).digest
            except TreeFingerprintError as exc:
                return _blocked(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    message=str(exc),
                    status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
                )
        else:
            source_before_sha256 = revision_snapshot.tree_sha256
        snapshot = (
            self.sandbox_root
            / f"{run_id}-{check.id}-{source_before_sha256[:12]}"
        )
        if snapshot.exists():
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message="Sandbox snapshot identity 已存在；拒绝覆盖未知执行状态。",
                source_revision=(
                    revision_snapshot.commit if revision_snapshot is not None else None
                ),
                source_tree_sha256=source_before_sha256,
            )
        manifest_sha256 = ""
        try:
            manifest_sha256 = await asyncio.to_thread(
                self._materialize_snapshot,
                snapshot,
                run_id=run_id,
                check_id=check.id,
                source_tree_sha256=source_before_sha256,
                profile_digest=profile_digest,
                revision_snapshot=revision_snapshot,
            )
            executable = _resolve_profile_executable(check.argv[0])
            artifact_identity = hashlib.sha256(
                f"{run_id}\0{check.id}\0{source_before_sha256}".encode()
            ).hexdigest()[:24]
            spec = ShellCommandSpec(
                argv=(executable, *check.argv[1:]),
                workspace_root=str(snapshot),
                workspace_manifest_sha256=manifest_sha256,
                cwd_relative=".",
                artifact_root=str(self.artifact_root),
                artifact_name=f"{check.id}-{artifact_identity}.log",
                environment=(),
                timeout_seconds=check.timeout_seconds,
                max_output_bytes=self._max_output_bytes,
                max_memory_bytes=self._max_memory_bytes,
                max_cpu_seconds=min(self._max_cpu_seconds, check.timeout_seconds),
                network_disabled=True,
            )
            if not await profile_is_current():
                return _blocked(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    message=(
                        "Harness Profile 在 Sandbox admission 前失去信任，检查未执行。"
                    ),
                    source_revision=(
                        revision_snapshot.commit
                        if revision_snapshot is not None
                        else None
                    ),
                    source_tree_sha256=source_before_sha256,
                    snapshot_manifest_sha256=manifest_sha256,
                )
            admitted = await admit_job(spec)
            _validate_admitted_job(admitted, spec=spec)
            execution = await admitted.coordinator.execute(
                job_id=admitted.job_id,
                tool_job_request=admitted.tool_job_request,
                shell_request=admitted.shell_request,
                worker_health=admitted.worker_health,
                requirements=admitted.requirements,
                dispatch_id=admitted.dispatch_id,
                cancel_event=cancel_event,
            )
            return await self._finalize(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                profile_is_current=profile_is_current,
                source_before_sha256=source_before_sha256,
                revision_snapshot=revision_snapshot,
                manifest_sha256=manifest_sha256,
                execution=execution,
            )
        except (
            OSError,
            ValueError,
            ShellWorkerError,
            subprocess.SubprocessError,
        ) as exc:
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message=(
                    "Sandbox Profile check 基础设施失败："
                    f"{type(exc).__name__}：{str(exc)[:200]}"
                ),
                source_revision=(
                    revision_snapshot.commit if revision_snapshot is not None else None
                ),
                source_tree_sha256=source_before_sha256,
                snapshot_manifest_sha256=manifest_sha256,
                status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
            )
        finally:
            await asyncio.to_thread(_remove_snapshot, snapshot, self.sandbox_root)

    async def _finalize(
        self,
        *,
        check: HarnessCheckSpec,
        run_id: str,
        profile_digest: str,
        profile_is_current: ProfileRevalidator,
        source_before_sha256: str,
        revision_snapshot: _GitRevisionSnapshot | None,
        manifest_sha256: str,
        execution: ShellJobExecutionResult,
    ) -> HarnessSandboxCheckResult:
        if not await profile_is_current():
            return _from_execution(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                source_revision=(
                    revision_snapshot.commit if revision_snapshot is not None else None
                ),
                source_tree_sha256=source_before_sha256,
                manifest_sha256=manifest_sha256,
                execution=execution,
                status=HarnessSandboxCheckStatus.STALE,
                message="Harness Profile 在 Sandbox 执行期间发生变化；结果已作废。",
            )
        if revision_snapshot is None:
            try:
                source_after = await asyncio.to_thread(
                    compute_tree_fingerprint,
                    self.workspace_root,
                )
            except TreeFingerprintError as exc:
                return _from_execution(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    source_revision=None,
                    source_tree_sha256=source_before_sha256,
                    manifest_sha256=manifest_sha256,
                    execution=execution,
                    status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
                    message=str(exc),
                )
            if source_after.digest != source_before_sha256:
                return _from_execution(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    source_revision=None,
                    source_tree_sha256=source_after.digest,
                    manifest_sha256=manifest_sha256,
                    execution=execution,
                    status=HarnessSandboxCheckStatus.STALE,
                    message="源工作树在 Sandbox 执行期间发生变化；结果不能证明当前代码。",
                )
        else:
            try:
                revision_after = await asyncio.to_thread(
                    _resolve_git_revision_snapshot,
                    self.workspace_root,
                    revision_snapshot.commit,
                    revision_snapshot.tree_sha256,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return _from_execution(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    source_revision=revision_snapshot.commit,
                    source_tree_sha256=source_before_sha256,
                    manifest_sha256=manifest_sha256,
                    execution=execution,
                    status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
                    message=f"Git revision 终态复验失败：{str(exc)[:200]}",
                )
            if revision_after != revision_snapshot:
                return _from_execution(
                    check=check,
                    run_id=run_id,
                    profile_digest=profile_digest,
                    source_revision=revision_snapshot.commit,
                    source_tree_sha256=revision_after.tree_sha256,
                    manifest_sha256=manifest_sha256,
                    execution=execution,
                    status=HarnessSandboxCheckStatus.STALE,
                    message="Git revision identity 在 Sandbox 执行期间发生变化。",
                )
        status = _map_status(execution)
        return _from_execution(
            check=check,
            run_id=run_id,
            profile_digest=profile_digest,
            source_revision=(
                revision_snapshot.commit if revision_snapshot is not None else None
            ),
            source_tree_sha256=source_before_sha256,
            manifest_sha256=manifest_sha256,
            execution=execution,
            status=status,
            message=_status_message(check.id, status),
        )

    def _materialize_snapshot(
        self,
        snapshot: Path,
        *,
        run_id: str,
        check_id: str,
        source_tree_sha256: str,
        profile_digest: str,
        revision_snapshot: _GitRevisionSnapshot | None,
    ) -> str:
        snapshot.mkdir(mode=0o700)
        working_paths: tuple[Path, ...] = ()
        if revision_snapshot is None:
            working_paths = _git_snapshot_paths(self.workspace_root)
            item_count = len(working_paths)
        else:
            item_count = len(revision_snapshot.entries)
        if item_count > self._max_snapshot_files:
            raise ValueError("Sandbox snapshot 文件数量超过上限。")
        if revision_snapshot is None:
            manifest_files, total_bytes = self._materialize_working_tree(
                snapshot,
                working_paths,
            )
        else:
            manifest_files, total_bytes = self._materialize_revision(
                snapshot,
                revision_snapshot,
            )
        manifest = {
            "schema_version": 2,
            "run_id": run_id,
            "check_id": check_id,
            "source_kind": (
                "git_revision" if revision_snapshot is not None else "working_tree"
            ),
            "source_revision": (
                revision_snapshot.commit if revision_snapshot is not None else None
            ),
            "source_tree_sha256": source_tree_sha256,
            "profile_digest": profile_digest,
            "file_count": len(manifest_files),
            "total_bytes": total_bytes,
            "files": manifest_files,
        }
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("Sandbox snapshot manifest 超过上限。")
        manifest_path = snapshot / ".naumi-sandbox-manifest.json"
        manifest_path.write_bytes(encoded)
        return hashlib.sha256(encoded).hexdigest()

    def _materialize_working_tree(
        self,
        snapshot: Path,
        paths: tuple[Path, ...],
    ) -> tuple[list[dict[str, object]], int]:
        manifest_files: list[dict[str, object]] = []
        total_bytes = 0
        for relative in paths:
            _validate_snapshot_path(relative)
            source = self.workspace_root / relative
            if not source.exists():
                continue
            if source.is_symlink():
                raise ValueError(f"Sandbox snapshot 不接受 symlink：{relative}")
            if not source.is_file():
                continue
            declared_size = source.stat().st_size
            total_bytes = _accumulate_snapshot_size(
                total_bytes,
                declared_size,
                maximum=self._max_snapshot_bytes,
            )
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            size, digest = _file_identity(destination)
            if size != declared_size:
                raise ValueError("Sandbox snapshot 文件在复制期间发生变化。")
            manifest_files.append(_manifest_file(relative, size, digest))
        return manifest_files, total_bytes

    def _materialize_revision(
        self,
        snapshot: Path,
        revision: _GitRevisionSnapshot,
    ) -> tuple[list[dict[str, object]], int]:
        manifest_files: list[dict[str, object]] = []
        total_bytes = 0
        for entry in revision.entries:
            _validate_snapshot_path(entry.path)
            declared_size = _git_blob_size(self.workspace_root, entry.object_id)
            total_bytes = _accumulate_snapshot_size(
                total_bytes,
                declared_size,
                maximum=self._max_snapshot_bytes,
            )
            destination = snapshot / entry.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_git_blob(
                self.workspace_root,
                entry.object_id,
                destination,
                executable=entry.mode == "100755",
            )
            _verify_git_blob_identity(
                self.workspace_root,
                destination,
                expected_object_id=entry.object_id,
            )
            size, digest = _file_identity(destination)
            if size != declared_size:
                raise ValueError("Git blob 写入字节数与声明不一致。")
            manifest_files.append(_manifest_file(entry.path, size, digest))
        return manifest_files, total_bytes


def _git_snapshot_paths(workspace_root: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=workspace_root,
        check=True,
        capture_output=True,
        timeout=10.0,
    )
    decoded = completed.stdout.decode("utf-8", errors="strict")
    values = tuple(item for item in decoded.split("\0") if item)
    if values != tuple(sorted(set(values))):
        raise ValueError("Git snapshot path 必须唯一且稳定排序。")
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("Git snapshot path 试图逃逸工作区。")
        paths.append(path)
    return tuple(paths)


def _resolve_git_revision_snapshot(
    workspace_root: Path,
    source_revision: str,
    expected_tree_sha256: str | None,
) -> _GitRevisionSnapshot:
    if not isinstance(source_revision, str) or not _GIT_OBJECT.fullmatch(
        source_revision
    ):
        raise ValueError("source_revision 必须是完整小写 Git object id。")
    assert expected_tree_sha256 is not None
    _require_sha256(expected_tree_sha256, field="expected_source_tree_sha256")
    resolved = _git_capture(
        workspace_root,
        "rev-parse",
        "--verify",
        f"{source_revision}^{{commit}}",
    ).decode("ascii", errors="strict").strip().lower()
    if resolved != source_revision:
        raise ValueError("source_revision 无法解析为精确 commit。")
    tree_listing = _git_capture(
        workspace_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_revision,
    )
    actual_tree_sha256 = hashlib.sha256(tree_listing).hexdigest()
    if actual_tree_sha256 != expected_tree_sha256:
        raise ValueError("Git revision tree digest 与预期 authority 不一致。")
    entries: list[_GitRevisionEntry] = []
    paths: set[str] = set()
    for record in tree_listing.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_object_id = header.split(b" ", 2)
            path_text = raw_path.decode("utf-8", errors="strict")
            mode_text = mode.decode("ascii", errors="strict")
            object_id = raw_object_id.decode("ascii", errors="strict").lower()
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Git revision tree entry 编码或结构无效。") from exc
        if object_type != b"blob" or mode_text not in {"100644", "100755"}:
            raise ValueError("Git revision snapshot 只接受普通或可执行 blob。")
        if not _GIT_OBJECT.fullmatch(object_id):
            raise ValueError("Git revision blob object id 无效。")
        relative = Path(path_text)
        _validate_relative_snapshot_path(relative)
        normalized = relative.as_posix()
        if normalized in paths:
            raise ValueError("Git revision snapshot path 重复。")
        paths.add(normalized)
        entries.append(
            _GitRevisionEntry(
                mode=mode_text,
                object_id=object_id,
                path=relative,
            )
        )
    return _GitRevisionSnapshot(
        commit=resolved,
        tree_sha256=actual_tree_sha256,
        entries=tuple(entries),
    )


def _git_capture(workspace_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace_root,
        check=True,
        capture_output=True,
        timeout=15.0,
    )
    if len(completed.stdout) > _MAX_GIT_METADATA_BYTES:
        raise ValueError("Git metadata 输出超过安全上限。")
    return completed.stdout


def _git_blob_size(workspace_root: Path, object_id: str) -> int:
    raw = _git_capture(workspace_root, "cat-file", "-s", object_id)
    try:
        size = int(raw.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Git blob size 无效。") from exc
    if size < 0:
        raise ValueError("Git blob size 不能为负数。")
    return size


def _write_git_blob(
    workspace_root: Path,
    object_id: str,
    destination: Path,
    *,
    executable: bool,
) -> None:
    with destination.open("xb") as stream:
        subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=workspace_root,
            check=True,
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=60.0,
        )
    if os.name != "nt":
        destination.chmod(0o700 if executable else 0o600)


def _verify_git_blob_identity(
    workspace_root: Path,
    path: Path,
    *,
    expected_object_id: str,
) -> None:
    actual = _git_capture(
        workspace_root,
        "hash-object",
        "--no-filters",
        str(path),
    ).decode("ascii", errors="strict").strip().lower()
    if actual != expected_object_id:
        raise ValueError("Git blob 落盘字节与 tree object id 不一致。")


def _validate_relative_snapshot_path(path: Path) -> None:
    value = path.as_posix()
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "." in path.parts
        or "\x00" in value
    ):
        raise ValueError("Sandbox snapshot path 试图逃逸工作区。")


def _validate_snapshot_path(path: Path) -> None:
    _validate_relative_snapshot_path(path)
    if _is_sensitive_snapshot_path(path):
        raise ValueError(
            f"Sandbox snapshot 检测到敏感路径，拒绝复制：{path.as_posix()}"
        )


def _accumulate_snapshot_size(current: int, added: int, *, maximum: int) -> int:
    updated = current + added
    if added < 0 or updated > maximum:
        raise ValueError("Sandbox snapshot 总字节数超过上限。")
    return updated


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _manifest_file(path: Path, size: int, digest: str) -> dict[str, object]:
    return {
        "path": path.as_posix(),
        "size": size,
        "sha256": digest,
    }


def _is_sensitive_snapshot_path(path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    name = lowered_parts[-1] if lowered_parts else ""
    if lowered_parts and lowered_parts[0] == ".naumi" and lowered_parts != (
        ".naumi",
        "harness.yaml",
    ):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {"credentials.json", "service-account.json", "id_rsa", "id_ed25519"}:
        return True
    return path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def _validate_admitted_job(
    admitted: AdmittedSandboxShellJob,
    *,
    spec: ShellCommandSpec,
) -> None:
    if not isinstance(admitted, AdmittedSandboxShellJob):
        raise TypeError("admit_job 必须返回 AdmittedSandboxShellJob。")
    if admitted.shell_request.job_id != admitted.job_id:
        raise ValueError("Sandbox admitted job identity 不一致。")
    if admitted.shell_request.spec != spec:
        raise ValueError("Sandbox admitted shell spec 与快照计划不一致。")
    if admitted.tool_job_request.arguments != spec.canonical_payload():
        raise ValueError("Sandbox ToolJob 参数未绑定 shell spec。")
    if not isinstance(admitted.coordinator, ShellWorkerCoordinator):
        raise TypeError("Sandbox admitted job 缺少 ShellWorkerCoordinator。")


def _map_status(execution: ShellJobExecutionResult) -> HarnessSandboxCheckStatus:
    if execution.command is None:
        return (
            HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR
            if execution.reconcile_required
            else HarnessSandboxCheckStatus.BLOCKED
        )
    return {
        ShellWorkerStatus.PASSED: HarnessSandboxCheckStatus.PASSED,
        ShellWorkerStatus.FAILED: HarnessSandboxCheckStatus.FAILED,
        ShellWorkerStatus.TIMED_OUT: HarnessSandboxCheckStatus.TIMED_OUT,
        ShellWorkerStatus.CANCELLED: HarnessSandboxCheckStatus.CANCELLED,
        ShellWorkerStatus.OUTPUT_LIMIT: HarnessSandboxCheckStatus.RESOURCE_LIMIT,
        ShellWorkerStatus.RESOURCE_LIMIT: HarnessSandboxCheckStatus.RESOURCE_LIMIT,
        ShellWorkerStatus.INFRASTRUCTURE_ERROR: (
            HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR
        ),
    }[execution.command.status]


def _from_execution(
    *,
    check: HarnessCheckSpec,
    run_id: str,
    profile_digest: str,
    source_revision: str | None,
    source_tree_sha256: str,
    manifest_sha256: str,
    execution: ShellJobExecutionResult,
    status: HarnessSandboxCheckStatus,
    message: str,
) -> HarnessSandboxCheckResult:
    command = execution.command
    return HarnessSandboxCheckResult(
        check_id=check.id,
        run_id=run_id,
        status=status,
        source_revision=source_revision,
        source_tree_sha256=source_tree_sha256,
        snapshot_manifest_sha256=manifest_sha256,
        profile_digest=profile_digest,
        job_id=execution.job.contract.job_id,
        lifecycle_receipt_sha256=execution.job.latest_receipt.receipt_sha256,
        output=command.output_tail if command is not None else "",
        exit_code=command.exit_code if command is not None else None,
        duration_ms=command.duration_ms if command is not None else 0,
        artifact_path=command.artifact_path if command is not None else None,
        message=message,
    )


def _blocked(
    *,
    check: HarnessCheckSpec,
    run_id: str,
    profile_digest: str,
    message: str,
    source_revision: str | None = None,
    source_tree_sha256: str = "",
    snapshot_manifest_sha256: str = "",
    status: HarnessSandboxCheckStatus = HarnessSandboxCheckStatus.BLOCKED,
) -> HarnessSandboxCheckResult:
    return HarnessSandboxCheckResult(
        check_id=check.id,
        run_id=run_id,
        status=status,
        source_revision=source_revision,
        source_tree_sha256=source_tree_sha256,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        profile_digest=profile_digest,
        job_id=None,
        lifecycle_receipt_sha256=None,
        output="",
        exit_code=None,
        duration_ms=0,
        artifact_path=None,
        message=message,
    )


def _resolve_profile_executable(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=True)
    else:
        found = shutil.which(value)
        if found is None:
            raise ValueError(f"Profile check executable 不存在：{value}")
        resolved = Path(found).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Profile check executable 不可执行。")
    return str(resolved)


def _remove_snapshot(snapshot: Path, sandbox_root: Path) -> None:
    try:
        snapshot.resolve(strict=False).relative_to(sandbox_root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("拒绝清理 sandbox_root 之外的路径。") from exc
    if snapshot.exists():
        shutil.rmtree(snapshot)


def _resolve_owned_directory(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} 必须是绝对路径。")
    resolved = path.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"{field} 必须是目录。")
    return resolved


def _ensure_owned_directory(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Sandbox owned path 必须是目录。")
    if created and os.name != "nt":
        path.chmod(0o700)


def _require_existing_absolute_directory(
    value: str | Path,
    *,
    field: str,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} 必须是绝对路径。")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{field} 必须是现有目录。")
    return resolved


def _bounded_int(
    value: int,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} 必须是 SHA-256。")


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _status_message(check_id: str, status: HarnessSandboxCheckStatus) -> str:
    return {
        HarnessSandboxCheckStatus.PASSED: f"Sandbox 检查 {check_id} 已通过。",
        HarnessSandboxCheckStatus.FAILED: f"Sandbox 检查 {check_id} 未通过。",
        HarnessSandboxCheckStatus.TIMED_OUT: f"Sandbox 检查 {check_id} 已超时。",
        HarnessSandboxCheckStatus.CANCELLED: f"Sandbox 检查 {check_id} 已取消。",
        HarnessSandboxCheckStatus.RESOURCE_LIMIT: (
            f"Sandbox 检查 {check_id} 超过资源上限。"
        ),
        HarnessSandboxCheckStatus.BLOCKED: f"Sandbox 检查 {check_id} 已阻断。",
        HarnessSandboxCheckStatus.STALE: f"Sandbox 检查 {check_id} 结果已失效。",
        HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR: (
            f"Sandbox 检查 {check_id} 基础设施失败。"
        ),
    }[status]


__all__ = [
    "AdmittedSandboxShellJob",
    "HarnessSandboxCheckResult",
    "HarnessSandboxCheckRunner",
    "HarnessSandboxCheckStatus",
    "SandboxJobAdmitter",
]
