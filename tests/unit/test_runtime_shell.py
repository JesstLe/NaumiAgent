"""Cross-platform shell runtime tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from naumi_agent.runtime.shell import (
    ShellRuntimeError,
    build_shell_argv,
    create_shell_process,
    pid_exists,
    resolve_git_bash,
    run_shell_command,
    terminate_pid_tree,
    terminate_process_tree,
)


def _exists(*paths: Path):
    normalized = {str(path) for path in paths}
    return lambda path: str(path) in normalized


def test_windows_prefers_explicit_git_bash_override(tmp_path: Path) -> None:
    explicit = tmp_path / "Portable Git" / "bin" / "bash.exe"

    result = resolve_git_bash(
        env={"NAUMI_GIT_BASH": str(explicit)},
        which=lambda _name: None,
        is_file=_exists(explicit),
    )

    assert result == explicit


def test_windows_discovers_bash_next_to_git_executable(tmp_path: Path) -> None:
    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"

    result = resolve_git_bash(
        env={},
        which=lambda name: str(git) if name == "git" else None,
        is_file=_exists(bash),
    )

    assert result == bash


def test_windows_rejects_wsl_launcher_from_path(tmp_path: Path) -> None:
    wsl_bash = Path("C:/Windows/System32/bash.exe")

    with pytest.raises(ShellRuntimeError, match="Git Bash"):
        resolve_git_bash(
            env={},
            which=lambda name: str(wsl_bash) if name == "bash" else None,
            is_file=_exists(wsl_bash),
        )


def test_explicit_override_reports_invalid_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "bash.exe"

    with pytest.raises(ShellRuntimeError, match="NAUMI_GIT_BASH"):
        resolve_git_bash(
            env={"NAUMI_GIT_BASH": str(missing)},
            which=lambda _name: None,
            is_file=lambda _path: False,
        )


def test_build_windows_shell_argv_preserves_command_as_one_argument(tmp_path: Path) -> None:
    bash = tmp_path / "Git With Spaces" / "bin" / "bash.exe"
    command = "printf '%s' '中文 path C:\\work\\file.txt'"

    argv = build_shell_argv(
        command,
        platform="win32",
        env={"NAUMI_GIT_BASH": str(bash)},
        which=lambda _name: None,
        is_file=_exists(bash),
    )

    assert argv == (str(bash), "--noprofile", "--norc", "-lc", command)


def test_build_posix_shell_argv_keeps_existing_sh_contract() -> None:
    assert build_shell_argv("printf ok", platform="darwin") == (
        "/bin/sh",
        "-c",
        "printf ok",
    )


def test_build_windows_shell_quotes_leading_native_executable(tmp_path: Path) -> None:
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    command = 'C:\\work\\.venv\\Scripts\\python.exe -c "print(1)"'

    argv = build_shell_argv(
        command,
        platform="win32",
        env={"NAUMI_GIT_BASH": str(bash)},
        which=lambda _name: None,
        is_file=_exists(bash),
    )

    assert argv[-1] == '\'C:\\work\\.venv\\Scripts\\python.exe\' -c "print(1)"'


def test_sync_shell_capture_is_bounded_while_both_pipes_are_drained(
    tmp_path: Path,
) -> None:
    script = tmp_path / "large_output.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'a' * 300_000)\n"
        "sys.stderr.buffer.write(b'b' * 300_000)\n",
        encoding="utf-8",
    )

    result = run_shell_command(
        f'{sys.executable} "{script}"',
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("a" * 1024)
    assert result.stderr.startswith("b" * 1024)
    assert "输出已截断，原始大小 300000 字节" in result.stdout
    assert "输出已截断，原始大小 300000 字节" in result.stderr
    assert len(result.stdout) < 70_000
    assert len(result.stderr) < 70_000


def test_sync_shell_timeout_returns_bounded_partial_output(tmp_path: Path) -> None:
    script = tmp_path / "timeout_output.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "sys.stdout.buffer.write(b'a' * 300_000)\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'b' * 300_000)\n"
        "sys.stderr.buffer.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        run_shell_command(
            f'{sys.executable} "{script}"',
            timeout=1,
        )

    assert "输出已截断，原始大小 300000 字节" in exc_info.value.output
    assert "输出已截断，原始大小 300000 字节" in exc_info.value.stderr
    assert len(exc_info.value.output) < 70_000
    assert len(exc_info.value.stderr) < 70_000
    child_pid = int(pid_file.read_text())
    for _ in range(100):
        if not pid_exists(child_pid):
            break
        time.sleep(0.01)
    assert not pid_exists(child_pid)


@pytest.mark.asyncio
async def test_real_shell_preserves_unicode_working_directory(tmp_path: Path) -> None:
    workdir = tmp_path / "中文 workspace"
    workdir.mkdir()

    proc = await create_shell_process(
        "printf '%s' \"$PWD\"",
        cwd=workdir,
        stdout=-1,
        stderr=-1,
    )
    stdout, stderr = await proc.communicate()

    assert proc.returncode == 0, stderr.decode(errors="replace")
    assert "中文 workspace" in stdout.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_real_shell_preserves_nonzero_exit_and_stderr() -> None:
    proc = await create_shell_process(
        "printf failure >&2; exit 7",
        stdout=-1,
        stderr=-1,
    )
    stdout, stderr = await proc.communicate()

    assert stdout == b""
    assert stderr == b"failure"
    assert proc.returncode == 7


@pytest.mark.asyncio
async def test_real_shell_process_tree_can_be_terminated() -> None:
    proc = await create_shell_process(
        "sleep 60",
        stdout=-1,
        stderr=-1,
    )

    await terminate_process_tree(proc, grace_seconds=2)

    assert proc.returncode is not None


def test_windows_persisted_tree_uses_taskkill_with_force(monkeypatch) -> None:
    run = Mock()
    monkeypatch.setattr("naumi_agent.runtime.shell.sys.platform", "win32")
    monkeypatch.setattr("naumi_agent.runtime.shell.subprocess.run", run)

    terminate_pid_tree(4321, force=True)

    run.assert_called_once_with(
        ["taskkill", "/PID", "4321", "/T", "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_pid_exists_treats_platform_oserror_as_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "naumi_agent.runtime.shell.os.kill",
        Mock(side_effect=OSError("process is no longer addressable")),
    )

    assert not pid_exists(4321)


def test_current_windows_runtime_uses_git_for_windows_not_wsl() -> None:
    if os.name != "nt":
        pytest.skip("Windows-only discovery assertion")

    bash = resolve_git_bash()

    assert bash.name.lower() == "bash.exe"
    assert "system32" not in str(bash).lower()
