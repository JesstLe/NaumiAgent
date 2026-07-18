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
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


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
    ) -> HarnessSandboxCheckResult:
        run_id = validate_run_id(run_id)
        if not isinstance(check, HarnessCheckSpec):
            raise TypeError("check 必须是 HarnessCheckSpec。")
        if not _CHECK_ID.fullmatch(check.id):
            raise ValueError("check.id 格式无效。")
        _require_sha256(profile_digest, field="profile_digest")
        if not callable(profile_is_current) or not callable(admit_job):
            raise TypeError("profile_is_current 与 admit_job 必须可调用。")
        if not await profile_is_current():
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message="Harness Profile 在 Sandbox 快照前已失去信任，检查未执行。",
            )
        _ensure_owned_directory(self.sandbox_root)
        _ensure_owned_directory(self.artifact_root)
        try:
            source_before = await asyncio.to_thread(
                compute_tree_fingerprint,
                self.workspace_root,
            )
        except TreeFingerprintError as exc:
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message=str(exc),
                status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
            )
        snapshot = self.sandbox_root / f"{run_id}-{check.id}-{source_before.digest[:12]}"
        if snapshot.exists():
            return _blocked(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                message="Sandbox snapshot identity 已存在；拒绝覆盖未知执行状态。",
                source_tree_sha256=source_before.digest,
            )
        manifest_sha256 = ""
        try:
            manifest_sha256 = await asyncio.to_thread(
                self._materialize_snapshot,
                snapshot,
                run_id=run_id,
                check_id=check.id,
                source_tree_sha256=source_before.digest,
                profile_digest=profile_digest,
            )
            executable = _resolve_profile_executable(check.argv[0])
            artifact_identity = hashlib.sha256(
                f"{run_id}\0{check.id}\0{source_before.digest}".encode()
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
                    source_tree_sha256=source_before.digest,
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
                source_before_sha256=source_before.digest,
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
                source_tree_sha256=source_before.digest,
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
        manifest_sha256: str,
        execution: ShellJobExecutionResult,
    ) -> HarnessSandboxCheckResult:
        if not await profile_is_current():
            return _from_execution(
                check=check,
                run_id=run_id,
                profile_digest=profile_digest,
                source_tree_sha256=source_before_sha256,
                manifest_sha256=manifest_sha256,
                execution=execution,
                status=HarnessSandboxCheckStatus.STALE,
                message="Harness Profile 在 Sandbox 执行期间发生变化；结果已作废。",
            )
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
                source_tree_sha256=source_after.digest,
                manifest_sha256=manifest_sha256,
                execution=execution,
                status=HarnessSandboxCheckStatus.STALE,
                message="源工作树在 Sandbox 执行期间发生变化；结果不能证明当前代码。",
            )
        status = _map_status(execution)
        return _from_execution(
            check=check,
            run_id=run_id,
            profile_digest=profile_digest,
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
    ) -> str:
        snapshot.mkdir(mode=0o700)
        paths = _git_snapshot_paths(self.workspace_root)
        if len(paths) > self._max_snapshot_files:
            raise ValueError("Sandbox snapshot 文件数量超过上限。")
        manifest_files: list[dict[str, object]] = []
        total_bytes = 0
        for relative in paths:
            if _is_sensitive_snapshot_path(relative):
                raise ValueError(
                    f"Sandbox snapshot 检测到敏感路径，拒绝复制：{relative.as_posix()}"
                )
            source = self.workspace_root / relative
            if not source.exists():
                continue
            if source.is_symlink():
                raise ValueError(f"Sandbox snapshot 不接受 symlink：{relative}")
            if not source.is_file():
                continue
            declared_size = source.stat().st_size
            if total_bytes + declared_size > self._max_snapshot_bytes:
                raise ValueError("Sandbox snapshot 总字节数超过上限。")
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            payload = destination.read_bytes()
            total_bytes += len(payload)
            if total_bytes > self._max_snapshot_bytes:
                raise ValueError("Sandbox snapshot 总字节数超过上限。")
            manifest_files.append(
                {
                    "path": relative.as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "check_id": check_id,
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
    source_tree_sha256: str = "",
    snapshot_manifest_sha256: str = "",
    status: HarnessSandboxCheckStatus = HarnessSandboxCheckStatus.BLOCKED,
) -> HarnessSandboxCheckResult:
    return HarnessSandboxCheckResult(
        check_id=check.id,
        run_id=run_id,
        status=status,
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
