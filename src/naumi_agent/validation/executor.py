"""Cross-platform argv-only subprocess execution for validation checks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


class CommandExecutionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True)
class CommandExecutionResult:
    status: CommandExecutionStatus
    exit_code: int | None
    output: str
    output_bytes: int
    output_truncated: bool
    duration_ms: int
    artifact_path: Path | None = None


class ValidationExecutor:
    """Execute one approved command and always reap its process group."""

    def __init__(
        self,
        *,
        terminate_grace_seconds: float = 2.0,
        output_limit_bytes: int = 2_000_000,
    ) -> None:
        if terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds 不能为负数。")
        if output_limit_bytes < 1:
            raise ValueError("output_limit_bytes 必须大于 0。")
        self._terminate_grace_seconds = terminate_grace_seconds
        self._output_limit_bytes = output_limit_bytes

    async def run(
        self,
        *,
        argv: Sequence[str],
        cwd: str | Path,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None = None,
        artifact_path: str | Path | None = None,
    ) -> CommandExecutionResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0。")
        started = time.monotonic()
        runtime_argv = _runtime_argv(tuple(argv))
        destination = Path(artifact_path).resolve() if artifact_path is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *runtime_argv,
                cwd=str(Path(cwd)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **_process_group_kwargs(),
            )
        except (OSError, ValueError) as exc:
            return CommandExecutionResult(
                status=CommandExecutionStatus.INFRASTRUCTURE_ERROR,
                exit_code=None,
                output=f"无法启动验证命令：{type(exc).__name__}",
                output_bytes=0,
                output_truncated=False,
                duration_ms=_elapsed_ms(started),
            )

        assert proc.stdout is not None
        reader = asyncio.create_task(
            _collect_output(
                proc.stdout,
                limit_bytes=self._output_limit_bytes,
                artifact_path=destination,
            )
        )
        waiter = asyncio.create_task(proc.wait())
        cancel_waiter = (
            asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        )
        status: CommandExecutionStatus
        exit_code: int | None
        try:
            waiters = {waiter, reader}
            if cancel_waiter is not None:
                waiters.add(cancel_waiter)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            reader_failure: BaseException | None = None
            while True:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    await self._terminate_process_group(proc)
                    status = CommandExecutionStatus.TIMED_OUT
                    exit_code = None
                    break
                if cancel_waiter is not None and cancel_waiter in done:
                    await self._terminate_process_group(proc)
                    status = CommandExecutionStatus.CANCELLED
                    exit_code = None
                    break
                if reader in done:
                    reader_failure = reader.exception()
                    if reader_failure is not None:
                        await self._terminate_process_group(proc)
                        status = CommandExecutionStatus.INFRASTRUCTURE_ERROR
                        exit_code = None
                        break
                    waiters.discard(reader)
                if waiter in done:
                    exit_code = await waiter
                    status = (
                        CommandExecutionStatus.PASSED
                        if exit_code == 0
                        else CommandExecutionStatus.FAILED
                    )
                    break
            if reader_failure is not None:
                output = f"无法保存验证输出：{type(reader_failure).__name__}"
                output_bytes = 0
                truncated = False
            else:
                output, output_bytes, truncated = await reader
        except asyncio.CancelledError:
            await self._terminate_process_group(proc)
            with contextlib.suppress(Exception):
                await asyncio.shield(reader)
            raise
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
            if not waiter.done():
                waiter.cancel()

        return CommandExecutionResult(
            status=status,
            exit_code=exit_code,
            output=output,
            output_bytes=output_bytes,
            output_truncated=truncated,
            duration_ms=_elapsed_ms(started),
            artifact_path=destination,
        )

    async def _terminate_process_group(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        if proc.returncode is not None:
            return
        _signal_process_group(proc, force=False)
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=self._terminate_grace_seconds,
            )
            return
        except TimeoutError:
            _signal_process_group(proc, force=True)
        await proc.wait()


async def _collect_output(
    stream: asyncio.StreamReader,
    *,
    limit_bytes: int,
    artifact_path: Path | None,
) -> tuple[str, int, bool]:
    output = bytearray()
    total = 0
    artifact: BinaryIO | None = None
    try:
        if artifact_path is not None:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact = artifact_path.open("wb")
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                break
            total += len(chunk)
            if artifact is not None:
                artifact.write(chunk)
            output.extend(chunk)
            overflow = len(output) - limit_bytes
            if overflow > 0:
                del output[:overflow]
    finally:
        if artifact is not None:
            artifact.close()
    return output.decode("utf-8", errors="replace"), total, total > limit_bytes


def _runtime_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if os.name == "nt" and argv and argv[0] == "python3":
        import sys

        return (sys.executable, *argv[1:])
    return argv


def _process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _signal_process_group(
    proc: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.name == "nt":
            if force:
                proc.kill()
            else:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            return
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
