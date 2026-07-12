"""Cross-platform Bash command execution and process-tree management."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

Which = Callable[[str], str | None]
IsFile = Callable[[Path], bool]


class ShellRuntimeError(RuntimeError):
    """Raised when the configured platform shell cannot be used safely."""


def resolve_git_bash(
    *,
    env: Mapping[str, str] | None = None,
    which: Which | None = None,
    is_file: IsFile | None = None,
) -> Path:
    """Resolve Git for Windows Bash without mistaking the WSL launcher for it."""
    runtime_env = os.environ if env is None else env
    find_executable = shutil.which if which is None else which
    file_exists = Path.is_file if is_file is None else is_file

    explicit = runtime_env.get("NAUMI_GIT_BASH", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not file_exists(candidate) or not _is_acceptable_git_bash(candidate):
            raise ShellRuntimeError(
                "环境变量 NAUMI_GIT_BASH 未指向可用的 Git Bash bash.exe。"
            )
        return candidate

    candidates: list[Path] = []
    git_executable = find_executable("git")
    if git_executable:
        git_path = Path(git_executable)
        candidates.append(git_path.parent.parent / "bin" / "bash.exe")

    program_files = runtime_env.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    program_files_x86 = runtime_env.get("ProgramFiles(x86)")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "Git" / "bin" / "bash.exe")
    local_app_data = runtime_env.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Git" / "bin" / "bash.exe")

    path_bash = find_executable("bash")
    if path_bash:
        candidates.append(Path(path_bash))

    for candidate in _unique_paths(candidates):
        if file_exists(candidate) and _is_acceptable_git_bash(candidate):
            return candidate

    raise ShellRuntimeError(
        "未找到 Git Bash。请安装 Git for Windows，或将 NAUMI_GIT_BASH 设置为 "
        "Git 安装目录中的 bin\\bash.exe；不能使用 C:\\Windows\\System32\\bash.exe。"
    )


def build_shell_argv(
    command: str,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Which | None = None,
    is_file: IsFile | None = None,
) -> tuple[str, ...]:
    """Build an argv that preserves the project's Bash command contract."""
    active_platform = sys.platform if platform is None else platform
    if active_platform == "win32":
        bash = resolve_git_bash(env=env, which=which, is_file=is_file)
        normalized_command = _quote_leading_windows_executable(command)
        return (str(bash), "--noprofile", "--norc", "-lc", normalized_command)
    return ("/bin/sh", "-c", command)


async def create_shell_process(
    command: str,
    *,
    cwd: str | Path | None = None,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Start a shell command in its own process group for reliable cleanup."""
    argv = build_shell_argv(command, env=env)
    kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd is not None else None,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "env": dict(env) if env is not None else None,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


async def terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    force: bool = False,
    grace_seconds: float = 3.0,
) -> None:
    """Terminate a launched shell and all descendants on the active platform."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is None:
        _terminate_direct_process(proc, force=force)
    elif sys.platform == "win32":
        await _taskkill(pid, force=force)
    else:
        _kill_posix_group(pid, force=force)

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        if force:
            _terminate_direct_process(proc, force=True)
        elif pid is not None and sys.platform == "win32":
            await _taskkill(pid, force=True)
        elif pid is not None:
            _kill_posix_group(pid, force=True)
        else:
            _terminate_direct_process(proc, force=True)
    try:
        await proc.wait()
    except ProcessLookupError:
        return


def terminate_pid_tree(pid: int, *, force: bool = False) -> None:
    """Synchronously terminate a persisted process tree by root PID."""
    if sys.platform == "win32":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    _kill_posix_group(pid, force=force)


def pid_exists(pid: int) -> bool:
    """Return whether a PID is still addressable by the current user."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_acceptable_git_bash(path: Path) -> bool:
    normalized = str(path).replace("/", "\\").lower()
    if normalized.endswith("\\windows\\system32\\bash.exe"):
        return False
    return path.name.lower() in {"bash", "bash.exe"}


def _quote_leading_windows_executable(command: str) -> str:
    """Protect a leading native executable path from Bash backslash escaping."""
    quoted = re.match(r'^(\s*)"([A-Za-z]:\\[^"\r\n]+)"', command)
    if quoted:
        prefix, path = quoted.groups()
        return f"{prefix}{_shell_single_quote(path)}{command[quoted.end():]}"

    unquoted = re.match(r"^(\s*)([A-Za-z]:\\[^\s\r\n]+)", command)
    if unquoted:
        prefix, path = unquoted.groups()
        return f"{prefix}{_shell_single_quote(path)}{command[unquoted.end():]}"
    return command


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


async def _taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        killer = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except (FileNotFoundError, ProcessLookupError):
        return


def _kill_posix_group(pid: int, *, force: bool) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _terminate_direct_process(
    proc: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        return
