"""Regression tests use bounded real processes, never user runtime data."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import psutil
import pytest

from naumi_agent.tools import sandbox


async def _wait_file(path: Path) -> None:
    async with asyncio.timeout(8):
        while not path.exists():
            await asyncio.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _cleanup(pids: list[int]) -> None:
    processes = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            processes.extend(process.children(recursive=True))
            processes.append(process)
        except psutil.NoSuchProcess:
            pass
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(processes, timeout=3)


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    monkeypatch.setattr(sandbox, "_docker_available_cache", False)


@pytest.mark.asyncio
async def test_cancel_stops_real_code_before_return_and_preserves_cancellation(tmp_path):
    ready, marker = tmp_path / "ready", tmp_path / "late"
    code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('late'); time.sleep(5)"
    )
    task = asyncio.create_task(sandbox.CodeExecuteTool().execute(code=code, timeout=10))
    pids = []
    timer = None
    try:
        await _wait_file(ready)
        pids = [int(ready.read_text())]
        task.cancel()
        timer = asyncio.get_running_loop().call_later(0.01, task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert not any(_alive(pid) for pid in pids), "cancel returned with live code"
        await asyncio.sleep(1.1)
        assert not marker.exists(), "code wrote after cancellation completed"
    finally:
        if timer:
            timer.cancel()
        _cleanup(pids)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("root_exits", [False, True])
@pytest.mark.asyncio
async def test_timeout_stops_descendants_even_after_root_exits(tmp_path, root_exits):
    ready, marker = tmp_path / "pids", tmp_path / "late"
    child = (
        "import pathlib,time; time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('late'); time.sleep(5)"
    )
    code = (
        "import os,subprocess,sys,pathlib,json,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],cwd={str(tmp_path)!r}); "
        f"pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpid(),p.pid])); "
        + ("pass" if root_exits else "time.sleep(7)")
    )
    pids = []
    try:
        start = time.monotonic()
        result = await asyncio.wait_for(
            sandbox.CodeExecuteTool().execute(code=code, timeout=1), timeout=8,
        )
        pids = json.loads(ready.read_text())
        assert "timed out" in result or "超时" in result, result
        assert time.monotonic() - start < 7
        assert not any(_alive(pid) for pid in pids), "timeout left live descendants"
        await asyncio.sleep(1.2)
        assert not marker.exists()
    finally:
        if ready.exists():
            pids = json.loads(ready.read_text())
        _cleanup(pids)


@pytest.mark.asyncio
async def test_success_also_reaps_detached_output_descendants(tmp_path):
    ready, marker = tmp_path / "pids", tmp_path / "late"
    child = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('late'); time.sleep(5)"
    )
    code = (
        "import os,subprocess,sys,pathlib,json; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],cwd={str(tmp_path)!r},"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpid(),p.pid])); "
        "print('finished')"
    )
    pids = []
    try:
        result = await asyncio.wait_for(
            sandbox.CodeExecuteTool().execute(code=code, timeout=3), timeout=8,
        )
        pids = json.loads(ready.read_text())
        assert result.strip() == "finished"
        assert not any(_alive(pid) for pid in pids), "success left live descendants"
        await asyncio.sleep(1.1)
        assert not marker.exists()
    finally:
        if ready.exists():
            pids = json.loads(ready.read_text())
        _cleanup(pids)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable argument contract")
@pytest.mark.asyncio
async def test_real_code_preserves_unicode_and_space_workdir(tmp_path):
    code = "import sys; print('中文 spaces'); print(sys.argv[0].endswith('code.py'))"
    result = await sandbox.CodeExecuteTool().execute(code=code, timeout=3)
    assert "中文 spaces" in result
    assert "True" in result
