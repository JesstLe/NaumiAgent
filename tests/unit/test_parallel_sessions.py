from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.parallel_sessions import (
    MAX_PARALLEL_SESSIONS,
    ParallelSessionLaunchError,
    launch_parallel_sessions,
    parse_parallel_request,
)


def test_parse_parallel_request_supports_count_and_quoted_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()

    count, resolved = parse_parallel_request(
        f'10 "{workspace}"',
        default_workspace=tmp_path,
    )

    assert count == MAX_PARALLEL_SESSIONS
    assert resolved == workspace.resolve()


def test_parse_parallel_request_rejects_out_of_range_count(tmp_path: Path) -> None:
    with pytest.raises(ParallelSessionLaunchError, match="1 到 10"):
        parse_parallel_request("11", default_workspace=tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NEW_CONSOLE 仅存在于 Windows")
def test_launcher_creates_independent_processes_with_slots(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(pid=9000 + len(calls), terminate=lambda: None)

    result = launch_parallel_sessions(
        count=3,
        workspace=tmp_path,
        config_path=config,
        popen_factory=fake_popen,
        platform="nt",
    )

    assert [item.pid for item in result.processes] == [9001, 9002, 9003]
    assert [call["env"]["NAUMI_PARALLEL_SLOT"] for call in calls] == ["1", "2", "3"]
    assert all(call["cwd"] == str(tmp_path.resolve()) for call in calls)
    assert all(call["creationflags"] & subprocess.CREATE_NEW_CONSOLE for call in calls)
    assert all(call["command"][:3] == [sys.executable, "-m", "naumi_agent"] for call in calls)


def test_launcher_rolls_back_partial_creation(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    terminated: list[int] = []
    attempts = 0

    def fake_popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("launch failed")
        return SimpleNamespace(pid=7001, terminate=lambda: terminated.append(7001))

    with pytest.raises(ParallelSessionLaunchError, match="1/3"):
        launch_parallel_sessions(
            count=3,
            workspace=tmp_path,
            config_path=config,
            popen_factory=fake_popen,
            platform="nt",
        )

    assert terminated == [7001]
