"""Process ownership tests with no external Docker or model service."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.runtime import owned_process
from naumi_agent.tools import sandbox


def test_frozen_runtime_never_reexecutes_the_application(monkeypatch):
    monkeypatch.setattr(owned_process.sys, "frozen", True, raising=False)
    monkeypatch.setattr(owned_process.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="独立 Python"):
        owned_process.resolve_python_executable()


@pytest.mark.asyncio
async def test_cleanup_survives_repeated_cancel_and_waits_until_finished():
    entered, release = asyncio.Event(), asyncio.Event()
    complete = False

    async def clean():
        nonlocal complete
        entered.set()
        await release.wait()
        complete = True

    task = asyncio.create_task(owned_process.finish_cleanup(clean()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not complete
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert complete


@pytest.mark.asyncio
async def test_cleanup_own_cancellation_does_not_loop_forever():
    async def clean():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owned_process.finish_cleanup(clean()), 1)


@pytest.mark.asyncio
async def test_launch_cancellation_retains_and_cleans_created_process(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    captured = []

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        captured.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(owned_process.asyncio, "create_subprocess_exec", delayed)
    marker = tmp_path / "launched"
    code = f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).touch()"
    task = asyncio.create_task(owned_process.run_owned_process(
        [sys.executable, "-c", code], timeout=5,
    ))
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert captured[0].returncode is not None
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object fail-closed contract")
@pytest.mark.asyncio
async def test_failed_job_assignment_never_runs_target(tmp_path, monkeypatch):
    from naumi_agent.runtime.windows_job import WindowsProcessJob

    def refuse(_self, _pid):
        raise OSError("synthetic assignment failure")

    monkeypatch.setattr(WindowsProcessJob, "assign", refuse)
    marker = tmp_path / "never"
    with pytest.raises(OSError, match="synthetic assignment failure"):
        await owned_process.run_owned_process(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            timeout=5,
        )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_posix_process_group_contract_and_exited_root_cleanup(monkeypatch):
    monkeypatch.setattr(owned_process.sys, "platform", "linux")
    process = MagicMock(pid=4242, returncode=0)
    process.communicate = AsyncMock(return_value=(b"out", b"err"))
    spawn = AsyncMock(return_value=process)
    terminate = MagicMock()
    monkeypatch.setattr(owned_process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(owned_process, "terminate_pid_tree", terminate)
    result = await owned_process.run_owned_process(["python", "file with spaces.py"], timeout=1)
    assert result.stdout == b"out"
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.args == ("python", "file with spaces.py")
    terminate.assert_called_once_with(4242, force=True)


@pytest.mark.asyncio
async def test_nonzero_exit_and_stderr_are_preserved():
    result = await owned_process.run_owned_process(
        [sys.executable, "-c", "import sys; print('problem',file=sys.stderr);sys.exit(7)"],
        timeout=5,
    )
    assert result.returncode == 7
    assert b"problem" in result.stderr


@pytest.mark.asyncio
async def test_docker_cancel_removes_only_named_invocation_container(monkeypatch):
    calls, host_paths = [], []
    entered = asyncio.Event()

    async def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            host = argv[argv.index("-v") + 1].rsplit(":/tmp/", 1)[0]
            host_paths.append(Path(host))
            assert host_paths[0].exists()
            entered.set()
            await asyncio.Future()
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sandbox, "run_owned_process", run)
    task = asyncio.create_task(sandbox.CodeExecuteTool()._run_in_docker("print(1)", "python", 5))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("naumi-code-")
    assert calls[1] == ["docker", "rm", "--force", name]
    assert not host_paths[0].exists()


@pytest.mark.asyncio
async def test_docker_timeout_removes_named_container_before_return(monkeypatch):
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            raise TimeoutError
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sandbox, "run_owned_process", run)
    result = await sandbox.CodeExecuteTool()._run_in_docker("print(1)", "python", 1)
    assert "timed out" in result
    assert calls[1][1:3] == ["rm", "--force"]
    assert calls[1][-1] == calls[0][calls[0].index("--name") + 1]


@pytest.mark.asyncio
async def test_docker_cleanup_failure_is_not_reported_as_success(monkeypatch):
    async def run(argv, **kwargs):
        if argv[1] == "run":
            return subprocess.CompletedProcess(argv, 0, b"success", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"daemon unavailable")

    monkeypatch.setattr(sandbox, "run_owned_process", run)
    with pytest.raises(RuntimeError, match="容器清理失败"):
        await sandbox.CodeExecuteTool()._run_in_docker("print(1)", "python", 1)


@pytest.mark.asyncio
async def test_absent_auto_removed_container_is_idempotent(monkeypatch):
    run = AsyncMock(return_value=subprocess.CompletedProcess([], 1, b"", b"No such container"))
    monkeypatch.setattr(sandbox, "run_owned_process", run)
    await sandbox._remove_container("naumi-code-test")
