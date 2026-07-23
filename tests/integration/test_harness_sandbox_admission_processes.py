from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from naumi_agent.harness.store import HarnessStore

_CHILD = """
import asyncio
import json
import sys
import time
from pathlib import Path

from naumi_agent.harness.sandbox_batch import HarnessSandboxBatchAdmission
from naumi_agent.harness.store import HarnessStore


async def main():
    db_path, workspace, token, marker, hold = sys.argv[1:]
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=1,
        store=HarnessStore(db_path),
        workspace_root=workspace,
        owner_id=f"process-{token}",
        lease_seconds=2,
        poll_interval_seconds=0.02,
        token=lambda: token * 32,
    )
    async with admission.admit(
        authority_key=token * 64,
        lane="sandbox",
        requested_samples=5,
    ):
        entered = time.monotonic()
        Path(marker).write_text(
            json.dumps({"entered": entered}),
            encoding="utf-8",
        )
        await asyncio.sleep(float(hold))
        Path(marker).write_text(
            json.dumps({"entered": entered, "exited": time.monotonic()}),
            encoding="utf-8",
        )


asyncio.run(main())
"""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_durable_sandbox_admission_serializes_independent_processes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    first_marker = tmp_path / "first.json"
    second_marker = tmp_path / "second.json"

    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(db_path),
            str(workspace),
            "a",
            str(first_marker),
            "0.35",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not first_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not first_marker.exists():
        stdout, stderr = first.communicate(timeout=2)
        pytest.fail(f"首个独立进程未取得 admission：{stdout}\n{stderr}")

    second = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(db_path),
            str(workspace),
            "b",
            str(second_marker),
            "0.01",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_stdout, first_stderr = first.communicate(timeout=5)
    second_stdout, second_stderr = second.communicate(timeout=5)

    assert first.returncode == 0, f"{first_stdout}\n{first_stderr}"
    assert second.returncode == 0, f"{second_stdout}\n{second_stderr}"
    first_times = json.loads(first_marker.read_text(encoding="utf-8"))
    second_times = json.loads(second_marker.read_text(encoding="utf-8"))
    assert second_times["entered"] >= first_times["exited"]

    snapshot = await HarnessStore(db_path).sandbox_admission_snapshot(
        workspace_root=workspace,
        now="2099-01-01T00:00:00+00:00",
    )
    assert snapshot is not None
    assert snapshot.active_count == 0
    assert snapshot.queued_count == 0
