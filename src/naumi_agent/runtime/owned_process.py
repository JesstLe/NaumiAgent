"""Bounded execution that owns its process tree until all cleanup has finished."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Sequence
from pathlib import Path

from naumi_agent.runtime.shell import terminate_pid_tree

_CLEANUP_SECONDS = 3.0
# This trusted gate runs no target code before the parent assigns it to a Job Object.
# Inline stdlib-only code also works from wheels without a separately packaged script.
_WINDOWS_GATE = (
    "import subprocess,sys; "
    "allowed=sys.stdin.buffer.read(1)==b'\\n'; "
    "sys.exit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL) if allowed else 125)"
)


async def finish_cleanup[T](operation: Awaitable[T]) -> T:
    """Finish cleanup before propagating any repeated cancellation to the caller."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                # An operation's own cancellation is not a request to retry it forever.
                if task.cancelled():
                    raise
                result = task.result()
                cancelled = True
                break
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def resolve_python_executable() -> str:
    if not getattr(sys, "frozen", False):
        return sys.executable
    # A frozen app executable is not a Python interpreter. Fail before user code starts.
    interpreter = shutil.which("python") or shutil.which("python3")
    if interpreter is None or Path(interpreter).resolve() == Path(sys.executable).resolve():
        raise RuntimeError("当前打包环境缺少独立 Python 解释器，已停止本地代码执行。")
    return interpreter


async def run_owned_process(
    argv: Sequence[str], *, timeout: float, cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run argv without a shell and retain ownership after the root process exits."""
    command = list(argv)
    job = None
    kwargs: dict[str, object] = {"cwd": cwd, "stdout": -1, "stderr": -1, "stdin": -3}
    try:
        if sys.platform == "win32":
            from naumi_agent.runtime.windows_job import WindowsProcessJob

            interpreter = resolve_python_executable()
            job = WindowsProcessJob()
            command = [interpreter, "-I", "-S", "-c", _WINDOWS_GATE, *command]
            kwargs.update(stdin=-1, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command, **kwargs))
    except BaseException:
        if job is not None:
            job.close()
        raise
    communication = None
    try:
        # Shield the launch so cancellation cannot lose an already-created child.
        process = await asyncio.shield(spawn)
        if job is not None:
            job.assign(process.pid)
        communication = asyncio.create_task(process.communicate(b"\n" if job is not None else None))
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
        return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
    finally:
        await finish_cleanup(_cleanup(spawn, communication, job))


async def _cleanup(spawn, communication, job) -> None:
    process = None
    try:
        try:
            process = await spawn
        except (OSError, ValueError):
            return
        if job is not None:
            job.terminate()
            # A launch cancelled before assignment leaves a blocked, childless gate.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            async with asyncio.timeout(_CLEANUP_SECONDS):
                while job.active_processes():
                    await asyncio.sleep(0.01)
        else:
            # Do not return early for an exited root: its process group can still exist.
            terminate_pid_tree(process.pid, force=True)
        if communication is None:
            communication = asyncio.create_task(process.communicate())
        try:
            await asyncio.wait_for(asyncio.shield(communication), _CLEANUP_SECONDS)
        except TimeoutError as exc:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise RuntimeError("代码进程输出清理超时，执行未被确认为已完整结束。") from exc
    finally:
        if job is not None:
            job.close()
