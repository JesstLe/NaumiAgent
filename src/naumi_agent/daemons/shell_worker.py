"""Authenticated local non-PTY shell worker with fail-closed OS isolation."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import multiprocessing
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any

from naumi_agent.daemons.execution_grants import execution_arguments_sha256
from naumi_agent.daemons.tool_jobs import (
    StoredToolJob,
    ToolJobAuthority,
    ToolJobLifecycleAuthority,
    ToolJobRequest,
    ToolJobSideEffect,
    ToolJobState,
)
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerContract,
    WorkerHealthReport,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore

_PROTOCOL_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.log$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARGV_ITEMS = 256
_MAX_ARG_BYTES = 64 * 1024
_MAX_ENV_ITEMS = 64
_MAX_ENV_BYTES = 32 * 1024
_POLL_SECONDS = 0.02


class ShellWorkerStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    RESOURCE_LIMIT = "resource_limit"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class ShellSandboxBackend(StrEnum):
    DARWIN_SANDBOX_EXEC = "darwin_sandbox_exec"
    LINUX_BWRAP = "linux_bwrap"


class ShellWorkerError(RuntimeError):
    """Raised when authenticated transport or worker protocol cannot be trusted."""


class ShellSandboxUnavailableError(ShellWorkerError):
    """Raised when the host cannot prove the requested isolation contract."""


@dataclass(frozen=True, slots=True)
class ShellCommandSpec:
    argv: tuple[str, ...]
    workspace_root: str
    workspace_manifest_sha256: str
    cwd_relative: str
    artifact_root: str
    artifact_name: str
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: float
    max_output_bytes: int
    max_memory_bytes: int
    max_cpu_seconds: int
    network_disabled: bool = True

    def __post_init__(self) -> None:
        _validate_argv(self.argv)
        workspace = _require_absolute_directory(self.workspace_root, field="workspace_root")
        _require_sha256(
            self.workspace_manifest_sha256,
            field="workspace_manifest_sha256",
        )
        manifest = workspace / ".naumi-sandbox-manifest.json"
        if not manifest.is_file() or hashlib.sha256(manifest.read_bytes()).hexdigest() != (
            self.workspace_manifest_sha256
        ):
            raise ValueError("workspace sandbox manifest 缺失或摘要不匹配。")
        artifact = _require_absolute_directory(self.artifact_root, field="artifact_root")
        cwd = _resolve_relative_directory(workspace, self.cwd_relative)
        if not cwd.is_dir():
            raise ValueError("cwd_relative 必须指向现有工作区目录。")
        if _paths_overlap(workspace, artifact):
            raise ValueError("artifact_root 与 workspace_root 不得重叠。")
        if not _ARTIFACT_NAME.fullmatch(self.artifact_name):
            raise ValueError("artifact_name 必须是安全的 .log 文件名。")
        _validate_environment(self.environment)
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ):
            raise TypeError("timeout_seconds 必须是数字。")
        if not 0.05 <= float(self.timeout_seconds) <= 3_600:
            raise ValueError("timeout_seconds 必须在 0.05 到 3600 之间。")
        _require_bounded_int(
            self.max_output_bytes,
            field="max_output_bytes",
            minimum=1_024,
            maximum=128 * 1024 * 1024,
        )
        _require_bounded_int(
            self.max_memory_bytes,
            field="max_memory_bytes",
            minimum=64 * 1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
        )
        _require_bounded_int(
            self.max_cpu_seconds,
            field="max_cpu_seconds",
            minimum=1,
            maximum=86_400,
        )
        if self.network_disabled is not True:
            raise ValueError("Shell Worker v1 只允许 network_disabled=true。")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "shell_job_schema_version": 1,
            **_json_value(asdict(self)),
        }

    def digest(self) -> str:
        return _canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ShellCommandRequest:
    job_id: str
    worker_id: str
    worker_instance_id: str
    worker_epoch: int
    worker_contract_sha256: str
    spec: ShellCommandSpec

    def __post_init__(self) -> None:
        for field in ("job_id", "worker_id", "worker_instance_id"):
            _require_identifier(getattr(self, field), field=field)
        _require_positive_int(self.worker_epoch, field="worker_epoch")
        _require_sha256(self.worker_contract_sha256, field="worker_contract_sha256")
        if not isinstance(self.spec, ShellCommandSpec):
            raise TypeError("spec 必须是 ShellCommandSpec。")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "worker_instance_id": self.worker_instance_id,
            "worker_epoch": self.worker_epoch,
            "worker_contract_sha256": self.worker_contract_sha256,
            "spec": self.spec.canonical_payload(),
        }

    def digest(self) -> str:
        return _canonical_sha256(self.canonical_payload())

    def tool_arguments(self) -> dict[str, object]:
        """Return the exact payload identity that ToolJob admission must bind."""
        return self.spec.canonical_payload()

    def __getattr__(self, name: str) -> object:
        """Expose immutable spec fields to execution code without duplicating state."""
        if name in ShellCommandSpec.__dataclass_fields__:
            return getattr(self.spec, name)
        raise AttributeError(name)


@dataclass(frozen=True, slots=True)
class ShellCommandResult:
    status: ShellWorkerStatus
    exit_code: int | None
    output_tail: str
    output_bytes: int
    output_sha256: str
    artifact_path: Path
    artifact_manifest_sha256: str
    duration_ms: int
    sandbox_backend: ShellSandboxBackend

    def __post_init__(self) -> None:
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code 必须是整数或 null。")
        if not isinstance(self.output_bytes, int) or self.output_bytes < 0:
            raise ValueError("output_bytes 不能为负数。")
        _require_sha256(self.output_sha256, field="output_sha256")
        _require_sha256(
            self.artifact_manifest_sha256,
            field="artifact_manifest_sha256",
        )
        if not self.artifact_path.is_absolute() or not self.artifact_path.is_file():
            raise ValueError("artifact_path 必须是现有绝对文件。")
        if not isinstance(self.duration_ms, int) or self.duration_ms < 0:
            raise ValueError("duration_ms 不能为负数。")


StartedCallback = Callable[[], Awaitable[None]]
NowProvider = Callable[[], str]


class AuthenticatedLocalShellTransport:
    """Spawn one isolated worker and authenticate its local IPC before execution."""

    def __init__(
        self,
        *,
        runtime_dir: str | Path,
        handshake_timeout_seconds: float = 5.0,
        terminate_grace_seconds: float = 1.0,
    ) -> None:
        unresolved = Path(runtime_dir).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Shell Worker runtime_dir 必须是绝对路径。")
        if handshake_timeout_seconds <= 0:
            raise ValueError("handshake_timeout_seconds 必须大于 0。")
        if terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds 不能为负数。")
        self._runtime_dir = unresolved.resolve(strict=False)
        self._handshake_timeout_seconds = handshake_timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds

    async def execute(
        self,
        request: ShellCommandRequest,
        *,
        on_started: StartedCallback,
        cancel_event: asyncio.Event | None = None,
    ) -> ShellCommandResult:
        if not isinstance(request, ShellCommandRequest):
            raise TypeError("request 必须是 ShellCommandRequest。")
        if not callable(on_started):
            raise TypeError("on_started 必须可调用。")
        self._prepare_runtime_dir()
        address, family = _transport_address(self._runtime_dir)
        authkey = secrets.token_bytes(32)
        nonce = secrets.token_hex(16)
        listener = Listener(address=address, family=family, authkey=authkey)
        bound_address = listener.address
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_worker_client_main,
            args=(bound_address, family, authkey, nonce),
            name=f"naumi-shell-{request.job_id[:16]}",
            daemon=False,
        )
        connection: Connection | None = None
        process.start()
        try:
            connection = await asyncio.wait_for(
                asyncio.to_thread(listener.accept),
                timeout=self._handshake_timeout_seconds,
            )
            await asyncio.to_thread(
                connection.send,
                {
                    "type": "execute",
                    "nonce": nonce,
                    "request": request.canonical_payload(),
                },
            )
            ready = await asyncio.wait_for(
                asyncio.to_thread(connection.recv),
                timeout=self._handshake_timeout_seconds,
            )
            _validate_ready_message(ready, nonce=nonce, request=request)
            await on_started()
            await asyncio.to_thread(
                connection.send,
                {"type": "start", "nonce": nonce, "request_sha256": request.digest()},
            )
            result_payload = await self._receive_result(
                connection,
                nonce=nonce,
                request=request,
                cancel_event=cancel_event,
            )
            result = _result_from_message(result_payload, request=request, nonce=nonce)
            await self._join_process(process)
            if process.exitcode != 0:
                raise ShellWorkerError("Shell Worker 在结果回执后异常退出。")
            return result
        except (EOFError, OSError, TimeoutError, ValueError) as exc:
            raise ShellWorkerError("Shell Worker 本机认证传输失败。") from exc
        finally:
            if connection is not None:
                with contextlib.suppress(OSError):
                    connection.close()
            listener.close()
            await self._stop_process(process)
            if family == "AF_UNIX":
                with contextlib.suppress(OSError):
                    Path(str(address)).unlink()

    async def _receive_result(
        self,
        connection: Connection,
        *,
        nonce: str,
        request: ShellCommandRequest,
        cancel_event: asyncio.Event | None,
    ) -> object:
        receiver = asyncio.create_task(asyncio.to_thread(connection.recv))
        cancel_waiter = (
            asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        )
        try:
            if cancel_waiter is None:
                return await asyncio.wait_for(
                    receiver,
                    timeout=float(request.timeout_seconds) + 10.0,
                )
            done, _ = await asyncio.wait(
                {receiver, cancel_waiter},
                timeout=float(request.timeout_seconds) + 10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("Shell Worker 未在 deadline 后返回。")
            if cancel_waiter in done and cancel_event is not None and cancel_event.is_set():
                await asyncio.to_thread(
                    connection.send,
                    {
                        "type": "cancel",
                        "nonce": nonce,
                        "request_sha256": request.digest(),
                    },
                )
            return await asyncio.wait_for(receiver, timeout=10.0)
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
            if not receiver.done():
                receiver.cancel()

    async def _join_process(self, process: multiprocessing.Process) -> None:
        await asyncio.to_thread(
            process.join,
            max(2.0, self._terminate_grace_seconds),
        )

    async def _stop_process(self, process: multiprocessing.Process) -> None:
        if not process.is_alive():
            await asyncio.to_thread(process.join, 0)
            return
        process.terminate()
        await asyncio.to_thread(process.join, self._terminate_grace_seconds)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join)

    def _prepare_runtime_dir(self) -> None:
        created = not self._runtime_dir.exists()
        self._runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name != "nt":
            self._runtime_dir.chmod(0o700)
        if not self._runtime_dir.is_dir():
            raise ShellWorkerError("Shell Worker runtime_dir 不是目录。")


@dataclass(frozen=True, slots=True)
class ShellJobExecutionResult:
    job: StoredToolJob
    command: ShellCommandResult | None
    payload_sent: bool
    reconcile_required: bool

    def __post_init__(self) -> None:
        if self.payload_sent != (self.command is not None):
            raise ValueError("payload_sent 必须与 command result 一致。")
        if self.reconcile_required and self.job.state not in {
            ToolJobState.DISPATCHED,
            ToolJobState.RUNNING,
            ToolJobState.UNKNOWN,
        }:
            raise ValueError("reconcile_required 与 ToolJob 状态不一致。")


class ShellWorkerCoordinator:
    """Join ToolJob authority, authenticated transport, and lifecycle receipts."""

    def __init__(
        self,
        *,
        jobs: ToolJobAuthority,
        lifecycle: ToolJobLifecycleAuthority,
        worker_registry: WorkerRegistryStore,
        transport: AuthenticatedLocalShellTransport,
        now: NowProvider | None = None,
    ) -> None:
        if not isinstance(jobs, ToolJobAuthority):
            raise TypeError("jobs 必须是 ToolJobAuthority。")
        if not isinstance(lifecycle, ToolJobLifecycleAuthority):
            raise TypeError("lifecycle 必须是 ToolJobLifecycleAuthority。")
        if not isinstance(worker_registry, WorkerRegistryStore):
            raise TypeError("worker_registry 必须是 WorkerRegistryStore。")
        if not isinstance(transport, AuthenticatedLocalShellTransport):
            raise TypeError("transport 必须是 AuthenticatedLocalShellTransport。")
        self._jobs = jobs
        self._lifecycle = lifecycle
        self._worker_registry = worker_registry
        self._transport = transport
        self._now = now or _utc_now

    async def execute(
        self,
        *,
        job_id: str,
        tool_job_request: ToolJobRequest,
        shell_request: ShellCommandRequest,
        worker_health: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        dispatch_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> ShellJobExecutionResult:
        stored = await self._jobs.get(job_id)
        if stored is None:
            raise ShellWorkerError("Shell Worker 对应的 ToolJob 不存在。")
        registration = await self._worker_registry.get_active(shell_request.worker_id)
        if registration is None:
            raise ShellWorkerError("Shell Worker registration 不存在。")
        _validate_shell_job_binding(
            stored=stored,
            tool_job_request=tool_job_request,
            shell_request=shell_request,
            worker_contract=registration.contract,
        )
        if stored.state in {
            ToolJobState.SUCCEEDED,
            ToolJobState.FAILED,
            ToolJobState.CANCELLED,
            ToolJobState.UNKNOWN,
        }:
            return ShellJobExecutionResult(
                job=stored,
                command=None,
                payload_sent=False,
                reconcile_required=stored.state is ToolJobState.UNKNOWN,
            )
        if stored.state in {ToolJobState.DISPATCHED, ToolJobState.RUNNING}:
            return ShellJobExecutionResult(
                job=stored,
                command=None,
                payload_sent=False,
                reconcile_required=True,
            )
        dispatch = await self._jobs.dispatch(
            job_id=job_id,
            request=tool_job_request,
            worker_health=worker_health,
            requirements=requirements,
            dispatch_id=dispatch_id,
            now=self._now(),
        )
        if not dispatch.should_send_payload:
            return ShellJobExecutionResult(
                job=dispatch.job,
                command=None,
                payload_sent=False,
                reconcile_required=True,
            )

        async def mark_running() -> None:
            await self._lifecycle.mark_running(
                job_id=job_id,
                dispatch_id=dispatch_id,
                worker_id=shell_request.worker_id,
                worker_instance_id=shell_request.worker_instance_id,
                worker_epoch=shell_request.worker_epoch,
                now=self._now(),
            )

        try:
            command = await self._transport.execute(
                shell_request,
                on_started=mark_running,
                cancel_event=cancel_event,
            )
        except BaseException:
            current = await self._jobs.get(job_id)
            if current is not None and current.state in {
                ToolJobState.DISPATCHED,
                ToolJobState.RUNNING,
            }:
                await self._lifecycle.mark_recovery_unknown(
                    job_id=job_id,
                    expected_latest_receipt_sha256=(
                        current.latest_receipt.receipt_sha256
                    ),
                    reason_code="shell_transport_ambiguous",
                    now=self._now(),
                )
            raise
        terminal_state, side_effect, result_code = _terminal_facts(command.status)
        terminal = await self._lifecycle.finish(
            job_id=job_id,
            dispatch_id=dispatch_id,
            worker_id=shell_request.worker_id,
            worker_instance_id=shell_request.worker_instance_id,
            worker_epoch=shell_request.worker_epoch,
            state=terminal_state,
            side_effect=side_effect,
            result_code=result_code,
            now=self._now(),
            exit_code=command.exit_code,
            output_sha256=command.output_sha256,
            artifact_manifest_sha256=command.artifact_manifest_sha256,
        )
        return ShellJobExecutionResult(
            job=terminal,
            command=command,
            payload_sent=True,
            reconcile_required=terminal.state is ToolJobState.UNKNOWN,
        )


def _validate_shell_job_binding(
    *,
    stored: StoredToolJob,
    tool_job_request: ToolJobRequest,
    shell_request: ShellCommandRequest,
    worker_contract: WorkerContract,
) -> None:
    contract = stored.contract
    if contract.job_id != shell_request.job_id:
        raise ShellWorkerError("Shell request job_id 与 ToolJob 不一致。")
    if (
        contract.worker_id != shell_request.worker_id
        or contract.worker_instance_id != shell_request.worker_instance_id
        or contract.worker_epoch != shell_request.worker_epoch
        or contract.worker_contract_sha256 != shell_request.worker_contract_sha256
    ):
        raise ShellWorkerError("Shell request Worker binding 与 ToolJob 不一致。")
    expected_workspace_sha256 = hashlib.sha256(
        str(Path(shell_request.workspace_root).resolve()).encode("utf-8")
    ).hexdigest()
    if contract.workspace_sha256 != expected_workspace_sha256:
        raise ShellWorkerError("Shell request workspace 与 execution grant 不一致。")
    if execution_arguments_sha256(tool_job_request.arguments) != (
        execution_arguments_sha256(shell_request.tool_arguments())
    ):
        raise ShellWorkerError("Shell request payload 与 ToolJob 参数摘要不一致。")
    resources = worker_contract.resources
    if (
        shell_request.max_memory_bytes > resources.max_memory_bytes
        or shell_request.max_cpu_seconds > resources.max_cpu_seconds
        or shell_request.timeout_seconds > resources.max_wall_seconds
        or shell_request.max_output_bytes > resources.max_output_bytes
    ):
        raise ShellWorkerError("Shell request 资源超过 Worker contract。")
    required_capabilities = {
        WorkerCapability.SHELL_NON_PTY,
        WorkerCapability.PROCESS_TREE_CANCEL,
        WorkerCapability.WORKSPACE_EPHEMERAL,
        WorkerCapability.NETWORK_POLICY,
        WorkerCapability.ENVIRONMENT_ALLOWLIST,
        WorkerCapability.RESOURCE_LIMITS,
        WorkerCapability.ARTIFACT_DIGEST,
    }
    if not required_capabilities.issubset(worker_contract.capabilities):
        raise ShellWorkerError("Worker contract 缺少 Shell 隔离能力。")
    if not all(asdict(worker_contract.isolation).values()):
        raise ShellWorkerError("Worker contract 的 Shell 隔离声明不完整。")


def _terminal_facts(
    status: ShellWorkerStatus,
) -> tuple[ToolJobState, ToolJobSideEffect, str]:
    return {
        ShellWorkerStatus.PASSED: (
            ToolJobState.SUCCEEDED,
            ToolJobSideEffect.OBSERVED,
            "shell_exit_zero",
        ),
        ShellWorkerStatus.FAILED: (
            ToolJobState.FAILED,
            ToolJobSideEffect.OBSERVED,
            "shell_exit_nonzero",
        ),
        ShellWorkerStatus.TIMED_OUT: (
            ToolJobState.FAILED,
            ToolJobSideEffect.POSSIBLE,
            "shell_timed_out",
        ),
        ShellWorkerStatus.CANCELLED: (
            ToolJobState.CANCELLED,
            ToolJobSideEffect.POSSIBLE,
            "shell_cancelled",
        ),
        ShellWorkerStatus.OUTPUT_LIMIT: (
            ToolJobState.FAILED,
            ToolJobSideEffect.POSSIBLE,
            "shell_output_limit",
        ),
        ShellWorkerStatus.RESOURCE_LIMIT: (
            ToolJobState.FAILED,
            ToolJobSideEffect.POSSIBLE,
            "shell_resource_limit",
        ),
        ShellWorkerStatus.INFRASTRUCTURE_ERROR: (
            ToolJobState.UNKNOWN,
            ToolJobSideEffect.POSSIBLE,
            "shell_infrastructure_ambiguous",
        ),
    }[status]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def detect_shell_sandbox_backend() -> ShellSandboxBackend:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return ShellSandboxBackend.DARWIN_SANDBOX_EXEC
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return ShellSandboxBackend.LINUX_BWRAP
    if os.name == "nt":
        raise ShellSandboxUnavailableError(
            "Windows 尚无可证明默认断网的 Shell sandbox adapter；已拒绝执行。"
        )
    raise ShellSandboxUnavailableError(
        "当前主机缺少受支持的 Shell sandbox backend；已拒绝执行。"
    )


def _worker_client_main(
    address: object,
    family: str,
    authkey: bytes,
    nonce: str,
) -> None:
    connection: Connection | None = None
    try:
        connection = Client(address=address, family=family, authkey=authkey)
        message = connection.recv()
        request = _request_from_message(message, expected_nonce=nonce)
        backend, runtime_argv = _sandboxed_argv(request)
        connection.send(
            {
                "type": "ready",
                "protocol_version": _PROTOCOL_VERSION,
                "nonce": nonce,
                "request_sha256": request.digest(),
                "worker_contract_sha256": request.worker_contract_sha256,
                "sandbox_backend": backend.value,
            }
        )
        start = connection.recv()
        _validate_control_message(start, expected_type="start", nonce=nonce, request=request)
        result = _run_command(
            request,
            runtime_argv=runtime_argv,
            backend=backend,
            connection=connection,
            nonce=nonce,
        )
        connection.send(_result_message(result, request=request, nonce=nonce))
    except BaseException as exc:
        if connection is not None:
            with contextlib.suppress(BaseException):
                connection.send(
                    {
                        "type": "worker_error",
                        "protocol_version": _PROTOCOL_VERSION,
                        "nonce": nonce,
                        "error_code": _worker_error_code(exc),
                    }
                )
        raise SystemExit(70) from exc
    finally:
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()


def _run_command(
    request: ShellCommandRequest,
    *,
    runtime_argv: tuple[str, ...],
    backend: ShellSandboxBackend,
    connection: Connection,
    nonce: str,
) -> ShellCommandResult:
    started = time.monotonic()
    artifact = Path(request.artifact_root) / request.artifact_name
    if artifact.exists():
        raise ShellWorkerError("Shell Worker artifact 已存在，拒绝覆盖。")
    artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {name: value for name, value in request.environment}
    environment.setdefault("PYTHONNOUSERSITE", "1")
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    environment.setdefault("LC_ALL", "C")
    environment.setdefault("LANG", "C")
    cwd = _resolve_relative_directory(Path(request.workspace_root), request.cwd_relative)
    status = ShellWorkerStatus.INFRASTRUCTURE_ERROR
    exit_code: int | None = None
    process: subprocess.Popen[bytes] | None = None
    with artifact.open("xb") as output:
        try:
            process = subprocess.Popen(
                runtime_argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                **_target_process_kwargs(request),
            )
            deadline = time.monotonic() + float(request.timeout_seconds)
            next_memory_check = time.monotonic()
            while process.poll() is None:
                if connection.poll(0):
                    control = connection.recv()
                    _validate_control_message(
                        control,
                        expected_type="cancel",
                        nonce=nonce,
                        request=request,
                    )
                    _terminate_target(process)
                    status = ShellWorkerStatus.CANCELLED
                    break
                output.flush()
                if artifact.stat().st_size > request.max_output_bytes:
                    _terminate_target(process)
                    status = ShellWorkerStatus.OUTPUT_LIMIT
                    break
                if time.monotonic() >= next_memory_check:
                    memory_bytes = _process_group_memory_bytes(process.pid)
                    if memory_bytes is None:
                        _terminate_target(process)
                        status = ShellWorkerStatus.INFRASTRUCTURE_ERROR
                        break
                    if memory_bytes > request.max_memory_bytes:
                        _terminate_target(process)
                        status = ShellWorkerStatus.RESOURCE_LIMIT
                        break
                    next_memory_check = time.monotonic() + 0.1
                if time.monotonic() >= deadline:
                    _terminate_target(process)
                    status = ShellWorkerStatus.TIMED_OUT
                    break
                time.sleep(_POLL_SECONDS)
            if status is ShellWorkerStatus.INFRASTRUCTURE_ERROR:
                exit_code = process.wait()
                status = (
                    ShellWorkerStatus.PASSED
                    if exit_code == 0
                    else ShellWorkerStatus.FAILED
                )
            else:
                process.wait()
        except (OSError, subprocess.SubprocessError):
            if process is not None:
                _terminate_target(process)
            status = ShellWorkerStatus.INFRASTRUCTURE_ERROR
            exit_code = None
    if status is ShellWorkerStatus.OUTPUT_LIMIT and artifact.stat().st_size > (
        request.max_output_bytes
    ):
        with artifact.open("r+b") as output:
            output.truncate(request.max_output_bytes)
    payload = artifact.read_bytes()
    output_sha256 = hashlib.sha256(payload).hexdigest()
    manifest_sha256 = _canonical_sha256(
        {
            "artifact_name": request.artifact_name,
            "output_bytes": len(payload),
            "output_sha256": output_sha256,
        }
    )
    tail = payload[-min(len(payload), 64 * 1024) :].decode(
        "utf-8",
        errors="replace",
    )
    return ShellCommandResult(
        status=status,
        exit_code=exit_code,
        output_tail=tail,
        output_bytes=len(payload),
        output_sha256=output_sha256,
        artifact_path=artifact.resolve(),
        artifact_manifest_sha256=manifest_sha256,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        sandbox_backend=backend,
    )


def _sandboxed_argv(
    request: ShellCommandRequest,
) -> tuple[ShellSandboxBackend, tuple[str, ...]]:
    backend = detect_shell_sandbox_backend()
    if backend is ShellSandboxBackend.DARWIN_SANDBOX_EXEC:
        home_read_denials = _darwin_home_read_denials(
            allowed_roots=(
                Path(request.workspace_root),
                Path(request.artifact_root),
                Path(request.argv[0]),
            )
        )
        profile = "\n".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process*)",
                "(allow file-read*)",
                *home_read_denials,
                f'(allow file-write* (subpath {_sandbox_string(request.workspace_root)}))',
                f'(allow file-write* (subpath {_sandbox_string(request.artifact_root)}))',
            )
        )
        return backend, (
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            "--",
            *request.argv,
        )
    workspace = request.workspace_root
    artifact = request.artifact_root
    return backend, (
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        workspace,
        workspace,
        "--bind",
        artifact,
        artifact,
        "--chdir",
        str(_resolve_relative_directory(Path(workspace), request.cwd_relative)),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--",
        *request.argv,
    )


def _darwin_home_read_denials(*, allowed_roots: tuple[Path, ...]) -> tuple[str, ...]:
    """Hide Home outside explicit roots without blocking path traversal to them."""
    home = Path.home().resolve(strict=True)
    candidates: set[Path] = set()
    for root in allowed_roots:
        lexical = root.expanduser().absolute()
        resolved = root.resolve(strict=True)
        if _is_relative_to(lexical, home):
            candidates.add(lexical)
        if _is_relative_to(resolved, home):
            candidates.add(resolved)
    allowed = tuple(
        sorted(
            candidates,
            key=lambda path: path.parts,
        )
    )
    if not allowed:
        return (f'(deny file-read* (subpath {_sandbox_string(str(home))}))',)

    denials: list[str] = []

    def visit(parent: Path, roots: tuple[Path, ...]) -> None:
        if parent in roots:
            return
        denials.append(
            f'(deny file-read-data (literal {_sandbox_string(str(parent))}))'
        )
        allowed_children = {
            parent / root.relative_to(parent).parts[0]
            for root in roots
            if root != parent
        }
        for child in sorted(parent.iterdir(), key=lambda path: path.name):
            if child in allowed_children:
                child_roots = tuple(
                    root for root in roots if _is_relative_to(root, child)
                )
                visit(child, child_roots)
                continue
            denials.append(
                f'(deny file-read* (subpath {_sandbox_string(str(child))}))'
            )

    visit(home, allowed)
    return tuple(denials)


def _target_process_kwargs(request: ShellCommandRequest) -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def apply_limits() -> None:
        import resource

        resource.setrlimit(
            resource.RLIMIT_CPU,
            (request.max_cpu_seconds, request.max_cpu_seconds),
        )
    return {"start_new_session": True, "preexec_fn": apply_limits}


def _process_group_memory_bytes(process_group_id: int) -> int | None:
    if os.name == "nt":
        return None
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,rss="],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    total_kib = 0
    try:
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and int(fields[0]) == process_group_id:
                total_kib += int(fields[1])
    except ValueError:
        return None
    return total_kib * 1024


def _terminate_target(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


def _request_from_message(message: object, *, expected_nonce: str) -> ShellCommandRequest:
    if not isinstance(message, dict) or set(message) != {"type", "nonce", "request"}:
        raise ShellWorkerError("Shell Worker execute 消息结构无效。")
    if message["type"] != "execute" or message["nonce"] != expected_nonce:
        raise ShellWorkerError("Shell Worker execute 消息身份无效。")
    payload = message["request"]
    if not isinstance(payload, dict):
        raise ShellWorkerError("Shell Worker request payload 无效。")
    expected = {"protocol_version", *ShellCommandRequest.__dataclass_fields__}
    if set(payload) != expected or payload["protocol_version"] != _PROTOCOL_VERSION:
        raise ShellWorkerError("Shell Worker request schema 不受支持。")
    normalized = dict(payload)
    normalized.pop("protocol_version")
    normalized["spec"] = _spec_from_payload(normalized["spec"])
    return ShellCommandRequest(**normalized)


def _spec_from_payload(payload: object) -> ShellCommandSpec:
    if not isinstance(payload, dict):
        raise ShellWorkerError("Shell Worker spec payload 无效。")
    expected = {"shell_job_schema_version", *ShellCommandSpec.__dataclass_fields__}
    if set(payload) != expected or payload["shell_job_schema_version"] != 1:
        raise ShellWorkerError("Shell Worker spec schema 不受支持。")
    normalized = dict(payload)
    normalized.pop("shell_job_schema_version")
    normalized["argv"] = tuple(normalized["argv"])
    normalized["environment"] = tuple(tuple(item) for item in normalized["environment"])
    return ShellCommandSpec(**normalized)


def _validate_ready_message(
    message: object,
    *,
    nonce: str,
    request: ShellCommandRequest,
) -> None:
    if not isinstance(message, dict):
        raise ShellWorkerError("Shell Worker ready 消息无效。")
    if message.get("type") == "worker_error":
        raise ShellSandboxUnavailableError(
            f"Shell Worker 启动前拒绝执行：{message.get('error_code', 'unknown')}"
        )
    expected = {
        "type",
        "protocol_version",
        "nonce",
        "request_sha256",
        "worker_contract_sha256",
        "sandbox_backend",
    }
    if set(message) != expected:
        raise ShellWorkerError("Shell Worker ready 字段集合无效。")
    if (
        message["type"] != "ready"
        or message["protocol_version"] != _PROTOCOL_VERSION
        or message["nonce"] != nonce
        or message["request_sha256"] != request.digest()
        or message["worker_contract_sha256"] != request.worker_contract_sha256
    ):
        raise ShellWorkerError("Shell Worker ready 绑定校验失败。")
    ShellSandboxBackend(str(message["sandbox_backend"]))


def _validate_control_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    request: ShellCommandRequest,
) -> None:
    if not isinstance(message, dict) or set(message) != {
        "type",
        "nonce",
        "request_sha256",
    }:
        raise ShellWorkerError("Shell Worker control 消息结构无效。")
    if (
        message["type"] != expected_type
        or message["nonce"] != nonce
        or message["request_sha256"] != request.digest()
    ):
        raise ShellWorkerError("Shell Worker control 消息绑定无效。")


def _result_message(
    result: ShellCommandResult,
    *,
    request: ShellCommandRequest,
    nonce: str,
) -> dict[str, object]:
    return {
        "type": "result",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "request_sha256": request.digest(),
        "status": result.status.value,
        "exit_code": result.exit_code,
        "output_tail": result.output_tail,
        "output_bytes": result.output_bytes,
        "output_sha256": result.output_sha256,
        "artifact_path": str(result.artifact_path),
        "artifact_manifest_sha256": result.artifact_manifest_sha256,
        "duration_ms": result.duration_ms,
        "sandbox_backend": result.sandbox_backend.value,
    }


def _result_from_message(
    message: object,
    *,
    request: ShellCommandRequest,
    nonce: str,
) -> ShellCommandResult:
    if not isinstance(message, dict):
        raise ShellWorkerError("Shell Worker result 消息无效。")
    if message.get("type") == "worker_error":
        raise ShellWorkerError(
            f"Shell Worker 执行失败：{message.get('error_code', 'unknown')}"
        )
    expected = {
        "type",
        "protocol_version",
        "nonce",
        "request_sha256",
        "status",
        "exit_code",
        "output_tail",
        "output_bytes",
        "output_sha256",
        "artifact_path",
        "artifact_manifest_sha256",
        "duration_ms",
        "sandbox_backend",
    }
    if set(message) != expected:
        raise ShellWorkerError("Shell Worker result 字段集合无效。")
    if (
        message["type"] != "result"
        or message["protocol_version"] != _PROTOCOL_VERSION
        or message["nonce"] != nonce
        or message["request_sha256"] != request.digest()
    ):
        raise ShellWorkerError("Shell Worker result 绑定校验失败。")
    artifact = Path(str(message["artifact_path"])).resolve(strict=True)
    expected_artifact = (Path(request.artifact_root) / request.artifact_name).resolve()
    if artifact != expected_artifact:
        raise ShellWorkerError("Shell Worker result artifact 路径越界。")
    payload = artifact.read_bytes()
    output_bytes = int(message["output_bytes"])
    output_sha256 = str(message["output_sha256"])
    artifact_manifest_sha256 = str(message["artifact_manifest_sha256"])
    if len(payload) != output_bytes or hashlib.sha256(payload).hexdigest() != output_sha256:
        raise ShellWorkerError("Shell Worker result 与 artifact 内容不一致。")
    expected_tail = payload[-min(len(payload), 64 * 1024) :].decode(
        "utf-8",
        errors="replace",
    )
    if str(message["output_tail"]) != expected_tail:
        raise ShellWorkerError("Shell Worker result tail 与 artifact 内容不一致。")
    expected_manifest_sha256 = _canonical_sha256(
        {
            "artifact_name": request.artifact_name,
            "output_bytes": output_bytes,
            "output_sha256": output_sha256,
        }
    )
    if artifact_manifest_sha256 != expected_manifest_sha256:
        raise ShellWorkerError("Shell Worker artifact manifest 摘要不一致。")
    return ShellCommandResult(
        status=ShellWorkerStatus(str(message["status"])),
        exit_code=message["exit_code"],
        output_tail=str(message["output_tail"]),
        output_bytes=output_bytes,
        output_sha256=output_sha256,
        artifact_path=artifact,
        artifact_manifest_sha256=artifact_manifest_sha256,
        duration_ms=int(message["duration_ms"]),
        sandbox_backend=ShellSandboxBackend(str(message["sandbox_backend"])),
    )


def _transport_address(runtime_dir: Path) -> tuple[object, str]:
    token = secrets.token_hex(12)
    if os.name == "nt":
        return rf"\\.\pipe\naumi-shell-{token}", "AF_PIPE"
    socket_path = str(runtime_dir / f"shell-{token}.sock")
    if len(os.fsencode(socket_path)) < 100:
        return socket_path, "AF_UNIX"
    return ("127.0.0.1", 0), "AF_INET"


def _worker_error_code(exc: BaseException) -> str:
    if isinstance(exc, ShellSandboxUnavailableError):
        return "sandbox_unavailable"
    if isinstance(exc, (ValueError, TypeError, ShellWorkerError)):
        return "protocol_rejected"
    if isinstance(exc, OSError):
        return "os_error"
    return "worker_internal_error"


def _sandbox_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_argv(argv: tuple[str, ...]) -> None:
    if not isinstance(argv, tuple) or not argv or len(argv) > _MAX_ARGV_ITEMS:
        raise ValueError("argv 必须是非空且有界的字符串 tuple。")
    if any(
        not isinstance(item, str) or not item or "\x00" in item or len(item) > 8_192
        for item in argv
    ):
        raise ValueError("argv 包含无效参数。")
    if sum(len(item.encode("utf-8")) for item in argv) > _MAX_ARG_BYTES:
        raise ValueError("argv 超过总字节上限。")
    executable = Path(argv[0]).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("argv[0] 必须是现有绝对可执行文件。")


def _validate_environment(environment: tuple[tuple[str, str], ...]) -> None:
    if not isinstance(environment, tuple) or len(environment) > _MAX_ENV_ITEMS:
        raise ValueError("environment 必须是有界 tuple。")
    names: list[str] = []
    total = 0
    for item in environment:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("environment 单项必须是 name/value tuple。")
        name, value = item
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("environment name 无效。")
        if not isinstance(value, str) or "\x00" in value or len(value) > 8_192:
            raise ValueError("environment value 无效。")
        if any(token in name for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            raise ValueError("environment 不允许 secret 类变量。")
        names.append(name)
        total += len(name.encode()) + len(value.encode())
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("environment 必须按 name 排序且不能重复。")
    if total > _MAX_ENV_BYTES:
        raise ValueError("environment 超过总字节上限。")


def _require_absolute_directory(value: str, *, field: str) -> Path:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是路径字符串。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} 必须是绝对路径。")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{field} 必须是现有目录。")
    return resolved


def _resolve_relative_directory(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("cwd_relative 必须是非空相对路径。")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("cwd_relative 不得逃逸工作区。")
    resolved = (root / candidate).resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("cwd_relative 不得经 symlink 逃逸工作区。") from exc
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} 必须是安全标识符。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} 必须是 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    _require_bounded_int(value, field=field, minimum=1, maximum=2**63 - 1)


def _require_bounded_int(
    value: int,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间。")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON 对象键必须是字符串。")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"不支持的 JSON 值：{type(value).__name__}")


__all__ = [
    "AuthenticatedLocalShellTransport",
    "ShellCommandRequest",
    "ShellCommandResult",
    "ShellCommandSpec",
    "ShellJobExecutionResult",
    "ShellSandboxBackend",
    "ShellSandboxUnavailableError",
    "ShellWorkerError",
    "ShellWorkerCoordinator",
    "ShellWorkerStatus",
    "detect_shell_sandbox_backend",
]
