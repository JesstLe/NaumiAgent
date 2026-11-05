"""Child-only Git Bash environment and local execution regression contracts."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import naumi_agent.runtime.shell as shell_runtime
import naumi_agent.tools.sandbox as sandbox_module
from naumi_agent.background.models import BackgroundStatus
from naumi_agent.background.runner import BackgroundRunner
from naumi_agent.background.store import BackgroundTaskStore
from naumi_agent.tools.builtin import BashRunTool
from naumi_agent.tools.sandbox import CodeExecuteTool


def _windows_path_key(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").casefold()


def _path_entries(environment: dict[str, str]) -> list[str]:
    keys = [key for key in environment if key.casefold() == "path"]
    assert len(keys) == 1
    return environment[keys[0]].split(";")


@pytest.fixture
def git_layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "Portable Git With Spaces"
    wrapper = root / "bin" / "bash.exe"
    raw = root / "usr" / "bin" / "bash.exe"
    for path in (wrapper, raw, raw.with_name("msys-2.0.dll"), root / "cmd" / "git.exe"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return wrapper, raw


@pytest.mark.parametrize("path_key", ["PATH", "Path", "pAtH"])
@pytest.mark.parametrize("use_raw", [False, True], ids=["wrapper", "raw"])
def test_windows_child_path_contains_only_selected_installation_prefixes(
    git_layout: tuple[Path, Path], path_key: str, use_raw: bool
) -> None:
    wrapper, raw = git_layout
    selected = raw if use_raw else wrapper
    original = {path_key: r"C:\fixture-native;C:\fixture-tail", "SENTINEL": "unchanged"}
    snapshot = dict(original)
    parent = dict(os.environ)

    result = shell_runtime.build_shell_environment(selected, platform="win32", env=original)

    assert result is not None and result is not original
    entries = _path_entries(result)
    normalized = [_windows_path_key(entry) for entry in entries]
    assert normalized.count(_windows_path_key(str(raw.parent))) == 1
    assert normalized.count(_windows_path_key(str(selected.parent))) == 1
    assert entries[-2:] == [r"C:\fixture-native", r"C:\fixture-tail"]
    assert result["SENTINEL"] == "unchanged"
    assert original == snapshot
    assert dict(os.environ) == parent


@pytest.mark.parametrize("original", [{}, {"PATH": ""}, {"Path": ""}])
def test_windows_child_path_handles_absent_and_empty_path(
    git_layout: tuple[Path, Path], original: dict[str, str]
) -> None:
    _, raw = git_layout
    snapshot = dict(original)

    result = shell_runtime.build_shell_environment(raw, platform="win32", env=original)

    assert result is not None
    entries = _path_entries(result)
    assert _windows_path_key(str(raw.parent)) in {_windows_path_key(p) for p in entries}
    if not original:
        assert "" not in entries
    else:
        assert entries[-1] == ""
    assert original == snapshot


def test_windows_child_path_normalizes_case_and_slashes_without_duplicate_prefixes(
    git_layout: tuple[Path, Path],
) -> None:
    wrapper, raw = git_layout
    original = {
        "Path": ";".join(
            (str(raw.parent).replace("\\", "/").upper(), str(wrapper.parent).upper(), r"C:\tail")
        ),
        "MARKER": "preserved",
    }
    snapshot = dict(original)

    once = shell_runtime.build_shell_environment(wrapper, platform="win32", env=original)
    assert once is not None
    twice = shell_runtime.build_shell_environment(wrapper, platform="win32", env=once)

    assert twice is not None
    assert once == twice
    entries = [_windows_path_key(p) for p in _path_entries(twice)]
    assert entries.count(_windows_path_key(str(raw.parent))) == 1
    assert entries.count(_windows_path_key(str(wrapper.parent))) == 1
    assert original == snapshot
    assert twice["MARKER"] == "preserved"


def test_windows_child_environment_inherits_without_mutating_parent(
    git_layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw = git_layout
    monkeypatch.setenv("PATH", r"C:\native-only")
    monkeypatch.setenv("NAUMI_CHILD_ENV_SENTINEL", "parent-value")
    original = dict(os.environ)

    result = shell_runtime.build_shell_environment(raw, platform="win32")

    assert result is not None
    assert result["NAUMI_CHILD_ENV_SENTINEL"] == "parent-value"
    assert r"C:\native-only" in _path_entries(result)
    assert dict(os.environ) == original


def test_explicit_environment_does_not_import_ambient_variables(
    git_layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper, _ = git_layout
    monkeypatch.setenv("NAUMI_AMBIENT_NOT_FOR_CHILD", "must-not-inherit")
    original = {"PATH": r"C:\explicit", "EXPLICIT_MARKER": "kept"}

    result = shell_runtime.build_shell_environment(wrapper, platform="win32", env=original)

    assert result is not None
    assert "NAUMI_AMBIENT_NOT_FOR_CHILD" not in result
    assert result["EXPLICIT_MARKER"] == "kept"
    assert set(result) == set(original)


def test_non_git_bash_does_not_gain_an_invented_git_usr_directory(tmp_path: Path) -> None:
    bash = tmp_path / "custom tools" / "bash.exe"
    bash.parent.mkdir()
    bash.touch()
    original = {"Path": r"C:\original", "CUSTOM_MARKER": "kept"}

    result = shell_runtime.build_shell_environment(bash, platform="win32", env=original)

    assert result is not None
    entries = [_windows_path_key(p) for p in _path_entries(result)]
    assert _windows_path_key(str(bash.parent / "usr" / "bin")) not in entries
    assert _windows_path_key(str(tmp_path / "usr" / "bin")) not in entries
    assert result["CUSTOM_MARKER"] == "kept"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_environment_preserves_inheritance_and_explicit_mapping(platform: str) -> None:
    assert shell_runtime.build_shell_environment("/bin/bash", platform=platform) is None
    original = {"Path": "one:two", "PATH": "explicit:case-sensitive", "MARKER": "kept"}

    result = shell_runtime.build_shell_environment("/bin/bash", platform=platform, env=original)

    assert result == original and result is not original


def test_windows_script_argv_preserves_filename_as_one_argument(
    git_layout: tuple[Path, Path], tmp_path: Path
) -> None:
    wrapper, _ = git_layout
    script = tmp_path / "中文 path" / "script's file;literal.sh"

    argv = shell_runtime.build_bash_script_argv(
        script,
        platform="win32",
        env={"NAUMI_GIT_BASH": str(wrapper)},
        which=lambda _: None,
        is_file=lambda path: path == wrapper,
    )

    assert argv == (str(wrapper), "--noprofile", "--norc", str(script))


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_script_argv_keeps_native_bash_contract(platform: str) -> None:
    script = "path with spaces/script's file.sh"

    argv = shell_runtime.build_bash_script_argv(
        script,
        platform=platform,
        env={"NAUMI_GIT_BASH": "invalid-ignored-on-posix"},
        which=lambda _: pytest.fail("POSIX script must not discover Windows Bash"),
        is_file=lambda _: pytest.fail("POSIX script must not inspect Windows Bash"),
    )

    assert argv == ("bash", script)


@pytest.mark.asyncio
async def test_async_and_sync_spawns_share_child_environment_and_keep_process_contract(
    git_layout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw = git_layout
    original = {"Path": r"C:\native", "NAUMI_GIT_BASH": str(raw), "MARKER": "kept"}
    snapshot = dict(original)
    command = "printf '%s' 'one argument; not reinterpreted'"
    argv = (str(raw), "--noprofile", "--norc", "-lc", command)
    monkeypatch.setattr(shell_runtime.sys, "platform", "win32")
    monkeypatch.setattr(shell_runtime.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(shell_runtime, "build_shell_argv", Mock(return_value=argv))
    async_proc = object()
    async_spawn = AsyncMock(return_value=async_proc)
    sync_proc = Mock(returncode=7)
    sync_proc.communicate.return_value = ("OUT", "ERR")
    sync_spawn = Mock(return_value=sync_proc)
    monkeypatch.setattr(shell_runtime.asyncio, "create_subprocess_exec", async_spawn)
    monkeypatch.setattr(shell_runtime.subprocess, "Popen", sync_spawn)

    started = await shell_runtime.create_shell_process(
        command, cwd=tmp_path, stdin=-1, stdout=-1, stderr=-1, env=original
    )
    completed = shell_runtime.run_shell_command(command, cwd=tmp_path, timeout=4, env=original)

    assert started is async_proc
    assert async_spawn.call_args.args == argv
    assert sync_spawn.call_args.args == (argv,)
    assert async_spawn.call_args.kwargs["env"] == sync_spawn.call_args.kwargs["env"]
    assert async_spawn.call_args.kwargs["env"] == shell_runtime.build_shell_environment(
        raw, platform="win32", env=original
    )
    assert async_spawn.call_args.kwargs["cwd"] == str(tmp_path)
    assert sync_spawn.call_args.kwargs["cwd"] == str(tmp_path)
    assert async_spawn.call_args.kwargs["stdin"] == -1
    assert async_spawn.call_args.kwargs["stdout"] == -1
    assert async_spawn.call_args.kwargs["stderr"] == -1
    assert async_spawn.call_args.kwargs["creationflags"] == (
        shell_runtime.subprocess.CREATE_NEW_PROCESS_GROUP
    )
    assert sync_spawn.call_args.kwargs["creationflags"] == (
        shell_runtime.subprocess.CREATE_NEW_PROCESS_GROUP
    )
    assert (completed.returncode, completed.stdout, completed.stderr) == (7, "OUT", "ERR")
    sync_proc.communicate.assert_called_once_with(timeout=4)
    assert original == snapshot


@pytest.mark.asyncio
async def test_bash_script_process_uses_shared_child_env_and_preserves_stdio(
    git_layout: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw = git_layout
    script = tmp_path / "script with spaces.sh"
    original = {"Path": r"C:\native", "NAUMI_GIT_BASH": str(raw)}
    snapshot = dict(original)
    argv = (str(raw), "--noprofile", "--norc", str(script))
    spawn = AsyncMock(return_value=object())
    monkeypatch.setattr(shell_runtime.sys, "platform", "win32")
    monkeypatch.setattr(shell_runtime.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(shell_runtime, "build_bash_script_argv", Mock(return_value=argv))
    monkeypatch.setattr(shell_runtime.asyncio, "create_subprocess_exec", spawn)

    await shell_runtime.create_bash_script_process(
        script, cwd=tmp_path, stdin=-1, stdout=-1, stderr=-1, env=original
    )

    assert spawn.call_args.args == argv
    assert spawn.call_args.kwargs["env"] == shell_runtime.build_shell_environment(
        raw, platform="win32", env=original
    )
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)
    for stream in ("stdin", "stdout", "stderr"):
        assert spawn.call_args.kwargs[stream] == -1
    assert spawn.call_args.kwargs["creationflags"] == (
        shell_runtime.subprocess.CREATE_NEW_PROCESS_GROUP
    )
    assert original == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [None, {"Path": "explicit:kept", "MARKER": "value"}])
async def test_non_windows_spawners_keep_original_env_and_process_groups(
    environment: dict[str, str] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ("/bin/sh", "-c", "fixture command")
    monkeypatch.setattr(shell_runtime.sys, "platform", "linux")
    monkeypatch.setattr(shell_runtime, "build_shell_argv", Mock(return_value=argv))
    async_spawn = AsyncMock(return_value=object())
    sync_proc = Mock(returncode=0)
    sync_proc.communicate.return_value = ("OUT", "")
    sync_spawn = Mock(return_value=sync_proc)
    monkeypatch.setattr(shell_runtime.asyncio, "create_subprocess_exec", async_spawn)
    monkeypatch.setattr(shell_runtime.subprocess, "Popen", sync_spawn)

    await shell_runtime.create_shell_process("fixture command", cwd=tmp_path, env=environment)
    shell_runtime.run_shell_command("fixture command", cwd=tmp_path, env=environment)

    for spawn in (async_spawn, sync_spawn):
        actual = spawn.call_args.kwargs["env"]
        assert actual == environment
        if environment is not None:
            assert actual is not environment
        assert spawn.call_args.kwargs["start_new_session"] is True
        assert "creationflags" not in spawn.call_args.kwargs


@pytest.fixture
def isolated_windows_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], Path]:
    if sys.platform != "win32":
        pytest.skip("Real Git for Windows execution contract")
    selected = shell_runtime.resolve_git_bash()
    if selected.parent.name.casefold() == "bin" and selected.parent.parent.name.casefold() == "usr":
        raw = selected
    else:
        raw = selected.parent.parent / "usr" / "bin" / "bash.exe"
    if not raw.is_file():
        pytest.skip("Installed Git does not expose a raw usr/bin Bash")
    workdir = tmp_path / "work space"
    home = tmp_path / "isolated home"
    temp = tmp_path / "temporary space"
    for directory in (workdir, home, temp):
        directory.mkdir()
    (workdir / "input.txt").write_text("UTILITY_PAYLOAD\n", encoding="utf-8")
    (home / ".bash_profile").write_text("printf PROFILE_MUST_NOT_RUN\n", encoding="utf-8")
    (home / ".bashrc").write_text("printf RC_MUST_NOT_RUN\n", encoding="utf-8")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    environment = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": str(Path(system_root) / "System32"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": str(temp),
        "TMP": str(temp),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "NAUMI_GIT_BASH": str(raw),
        "NAUMI_CHILD_ENV_SENTINEL": "isolated-value",
    }
    for key in list(os.environ):
        if key not in environment:
            monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sandbox_module.tempfile, "tempdir", str(temp))
    return environment, workdir


_EXTERNAL_UTILITY_COMMAND = (
    "uname -s || exit 91; cat ./input.txt || exit 92; "
    "printf 'CWD:%s\\n' \"$PWD\"; printf 'ERR_SENTINEL\\n' >&2; exit 7"
)


@pytest.mark.asyncio
async def test_real_windows_async_repairs_restricted_path_and_preserves_results(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
) -> None:
    environment, cwd = isolated_windows_git_environment
    parent = dict(os.environ)
    supplied = dict(environment)
    proc = await shell_runtime.create_shell_process(
        _EXTERNAL_UTILITY_COMMAND, cwd=cwd, stdout=-1, stderr=-1, env=supplied
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

    assert proc.returncode == 7
    assert b"UTILITY_PAYLOAD" in stdout
    assert b"work space" in stdout
    assert stderr == b"ERR_SENTINEL\n"
    assert b"MUST_NOT_RUN" not in stdout
    assert supplied == environment
    assert dict(os.environ) == parent


def test_real_windows_sync_repairs_restricted_path_and_preserves_results(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
) -> None:
    environment, cwd = isolated_windows_git_environment
    parent = dict(os.environ)
    supplied = dict(environment)

    completed = shell_runtime.run_shell_command(
        _EXTERNAL_UTILITY_COMMAND, cwd=cwd, timeout=10, env=supplied
    )

    assert completed.returncode == 7
    assert "UTILITY_PAYLOAD" in completed.stdout
    assert "work space" in completed.stdout
    assert completed.stderr == "ERR_SENTINEL\n"
    assert "MUST_NOT_RUN" not in completed.stdout
    assert supplied == environment
    assert dict(os.environ) == parent


@pytest.mark.asyncio
async def test_real_windows_bash_tool_keeps_output_artifact_and_nonzero_status(
    isolated_windows_git_environment: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    _, cwd = isolated_windows_git_environment
    parent = dict(os.environ)
    output_dir = tmp_path / "shell-output"
    tool = BashRunTool(workspace_root=cwd, output_dir=output_dir)
    tool._output_store.inline_limit_bytes = 1

    result = await tool.execute(command=_EXTERNAL_UTILITY_COMMAND, timeout=10)

    assert "UTILITY_PAYLOAD" in result
    assert "ERR_SENTINEL" in result
    assert "exit code: 7" in result
    assert "MUST_NOT_RUN" not in result
    logs = [path for path in output_dir.iterdir() if path.is_file()]
    assert any(b"UTILITY_PAYLOAD" in path.read_bytes() for path in logs)
    assert dict(os.environ) == parent


@pytest.mark.asyncio
async def test_real_windows_background_execution_persists_external_utility_output(
    isolated_windows_git_environment: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    _, cwd = isolated_windows_git_environment
    parent = dict(os.environ)
    runner = BackgroundRunner(BackgroundTaskStore(tmp_path / "background-store"))
    try:
        task = await runner.run(
            "uname -s >/dev/null && cat ./input.txt",
            cwd=str(cwd),
            timeout_seconds=10,
            idempotency_key="git-bash-env-fixture",
        )
        watchers = list(runner._watchers.values())
        if watchers:
            await asyncio.wait_for(asyncio.gather(*watchers), timeout=10)
        completed = runner.get(task.id)
        assert completed is not None
        assert completed.status is BackgroundStatus.COMPLETED
        assert completed.exit_code == 0
        assert runner.read_output(task.id) == "UTILITY_PAYLOAD\n"
        assert Path(completed.output_path).read_text(encoding="utf-8") == "UTILITY_PAYLOAD\n"
        assert Path(completed.output_path).read_bytes() == b"UTILITY_PAYLOAD\r\n"
        assert not runner.is_managed_active(task.id)
        assert dict(os.environ) == parent
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_real_windows_local_code_execute_bash_uses_shared_runtime_without_docker(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = dict(os.environ)
    tool = CodeExecuteTool()
    docker_check = AsyncMock(return_value=False)
    monkeypatch.setattr(tool, "_check_docker", docker_check)

    result = await tool.execute(
        code="uname -s || exit 97\nprintf 'CODE_OK\\n'\nprintf 'CODE_ERR\\n' >&2\nexit 9\n",
        language="bash",
        timeout=10,
    )

    docker_check.assert_awaited_once_with()
    assert "CODE_OK" in result
    assert "CODE_ERR" in result
    assert "[exit code: 9]" in result
    assert "not found" not in result
    assert "MUST_NOT_RUN" not in result
    assert dict(os.environ) == parent


def _delayed_child_fixture(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    child = tmp_path / "delayed fixture child.py"
    ready = tmp_path / "child-ready.txt"
    pid = tmp_path / "child-native-pid.txt"
    late = tmp_path / "child-must-not-write.txt"
    child.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"Path({str(ready)!r}).write_text('READY', encoding='utf-8')\n"
        "time.sleep(2)\n"
        f"Path({str(late)!r}).write_text('LATE', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = (
        f"{shlex.quote(Path(sys.executable).as_posix())} "
        f"{shlex.quote(child.as_posix())} &\nwait\n"
    )
    return command, ready, pid, late


@pytest.mark.asyncio
async def test_real_windows_local_bash_multiline_uses_posix_line_endings(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = CodeExecuteTool()
    monkeypatch.setattr(tool, "_check_docker", AsyncMock(return_value=False))
    code = "if true; then\n  printf 'MULTILINE_OK\\n' > output.txt\nfi\ncat output.txt\n"
    result = await tool.execute(code=code, language="bash", timeout=10)
    assert result == "MULTILINE_OK\n"


def _native_windows_pid_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        assert ctypes.get_last_error() in (87, 1168)
        return False
    try:
        status = wintypes.DWORD()
        assert kernel32.GetExitCodeProcess(handle, ctypes.byref(status))
        return status.value == 259
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.asyncio
async def test_real_windows_local_bash_timeout_terminates_only_its_fixture_child(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = CodeExecuteTool()
    monkeypatch.setattr(tool, "_check_docker", AsyncMock(return_value=False))
    code, ready, pid_path, late = _delayed_child_fixture(tmp_path)
    parent = dict(os.environ)

    result = await tool.execute(code=code, language="bash", timeout=1)

    assert "timed out after 1s" in result
    assert ready.read_text(encoding="utf-8") == "READY"
    pid = int(pid_path.read_text(encoding="utf-8"))
    assert not _native_windows_pid_is_running(pid)
    await asyncio.sleep(1.3)
    assert not late.exists()
    assert dict(os.environ) == parent


@pytest.mark.asyncio
async def test_real_windows_local_bash_cancel_terminates_only_its_fixture_child(
    isolated_windows_git_environment: tuple[dict[str, str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = CodeExecuteTool()
    monkeypatch.setattr(tool, "_check_docker", AsyncMock(return_value=False))
    code, ready, pid_path, late = _delayed_child_fixture(tmp_path)
    parent = dict(os.environ)
    execution = asyncio.create_task(tool.execute(code=code, language="bash", timeout=10))
    try:
        async def wait_ready() -> None:
            while not ready.exists():
                if execution.done():
                    pytest.fail(
                        f"Bash exited before fixture child became ready: {execution.result()}"
                    )
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_ready(), timeout=5)
        pid = int(pid_path.read_text(encoding="utf-8"))
        assert _native_windows_pid_is_running(pid)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert not _native_windows_pid_is_running(pid)
        await asyncio.sleep(2.2)
        assert not late.exists()
        assert dict(os.environ) == parent
    finally:
        if not execution.done():
            execution.cancel()
            try:
                await execution
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["python", "javascript"])
async def test_non_bash_local_code_keeps_original_native_spawn_without_env_rewrite(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = Mock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"NATIVE_OK\n", b""))
    spawn = AsyncMock(return_value=proc)
    bash_spawn = AsyncMock(side_effect=AssertionError("Non-Bash must not use Bash runtime"))
    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sandbox_module, "create_bash_script_process", bash_spawn)

    result = await CodeExecuteTool()._run_local("fixture", language, 10)

    assert result == "NATIVE_OK\n"
    assert spawn.call_args.args[0] == (sys.executable if language == "python" else "node")
    assert "env" not in spawn.call_args.kwargs
    assert "creationflags" not in spawn.call_args.kwargs
    assert "start_new_session" not in spawn.call_args.kwargs
    bash_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_code_keeps_existing_spawn_without_git_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = Mock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"DOCKER_FIXTURE\n", b""))
    spawn = AsyncMock(return_value=proc)
    bash_spawn = AsyncMock(side_effect=AssertionError("Docker must not use host Bash runtime"))
    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sandbox_module, "create_bash_script_process", bash_spawn)

    result = await CodeExecuteTool()._run_in_docker("fixture", "bash", 10)

    assert result == "DOCKER_FIXTURE\n"
    assert spawn.call_args.args[:3] == ("docker", "run", "--rm")
    assert spawn.call_args.args[-2:] == ("bash", "/tmp/code.sh")
    assert "env" not in spawn.call_args.kwargs
    bash_spawn.assert_not_awaited()
