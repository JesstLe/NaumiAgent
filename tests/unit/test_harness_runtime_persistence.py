from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _service(tmp_path: Path, *, store_path: Path) -> HarnessService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Harness Runtime Tests")
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    argv = json.dumps(
        [
            sys.executable,
            "-c",
            "print(bytes.fromhex('72756e74696d6520756e6974206f6b').decode())",
        ]
    )
    profile.write_text(
        "schema_version: 1\n"
        "completion:\n"
        "  correction_attempts: 1\n"
        "checks:\n"
        "  - id: unit\n"
        f"    argv: {argv}\n"
        "    timeout_seconds: 10\n"
        "    when_changed: ['**/*.py']\n"
        "    required_for: [change]\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture")
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store_path),
    )


@pytest.mark.asyncio
async def test_service_persists_live_run_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "harness.db"
    service = _service(tmp_path, store_path=db_path)
    await service.trust(source="test")

    state = await service.begin_completion_run(
        task="修改 source.py",
        run_id="runtime-run",
        session_id="runtime-session",
    )
    assert state is not None
    (service.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    correction = await service.evaluate_completion_run(state)
    check = await service.run_check(check_id="unit", run_id="runtime-run")
    completed = await service.evaluate_completion_run(state)

    restored = await HarnessStore(db_path).get_run("runtime-run")
    assert correction.status == "needs_correction"
    assert check.status.value == "passed"
    assert completed.status == "completed_verified"
    assert restored is not None
    assert restored.status == "completed_verified"
    assert restored.session_id == "runtime-session"
    assert restored.contract.task_kind.value == "change"
    assert restored.contract.required_checks == ("unit",)
    assert restored.receipt == completed.receipt
    assert len(restored.checks) == 1
    assert restored.checks[0].check_key == "unit"
    assert restored.checks[0].status == "passed"
    assert b"runtime unit ok" not in db_path.read_bytes()

    with sqlite3.connect(db_path) as db:
        profile_count = db.execute(
            "SELECT COUNT(*) FROM harness_profiles WHERE workspace_root = ?",
            (str(service.workspace_root),),
        ).fetchone()[0]
    assert profile_count == 1


@pytest.mark.asyncio
async def test_unavailable_store_warns_without_losing_completion(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "blocked-state"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    service = _service(tmp_path, store_path=blocked_parent / "harness.db")
    await service.trust(source="test")

    state = await service.begin_completion_run(
        task="只分析当前代码",
        run_id="store-unavailable",
        session_id="runtime-session",
    )
    assert state is not None

    result = await service.evaluate_completion_run(state)

    assert result.status == "completed_verified"
    assert result.receipt is not None
    assert len(result.receipt.warnings) == 1
    assert result.receipt.warnings[0].startswith("infrastructure_error:")
    assert "主任务结果仍会返回" in result.receipt.warnings[0]
    assert "FileExistsError" not in result.receipt.warnings[0]
    assert "SQLite" not in result.receipt.warnings[0]


@pytest.mark.asyncio
async def test_finish_failure_is_added_to_the_returned_receipt(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    db_path = state_dir / "harness.db"
    service = _service(tmp_path, store_path=db_path)
    await service.trust(source="test")
    state = await service.begin_completion_run(
        task="只分析当前代码",
        run_id="finish-failure",
        session_id="runtime-session",
    )
    assert state is not None

    for path in state_dir.iterdir():
        path.unlink()
    state_dir.rmdir()
    state_dir.write_text("store parent became a file", encoding="utf-8")

    result = await service.evaluate_completion_run(state)

    assert result.status == "completed_verified"
    assert result.receipt is not None
    assert len(result.receipt.warnings) == 1
    assert result.receipt.warnings[0].startswith("infrastructure_error:")
    assert state.receipt == result.receipt
