from __future__ import annotations

import pytest

from naumi_agent.workbench.models import FailureKind
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationCommand, ValidationRunner


@pytest.mark.asyncio
async def test_validation_runner_records_success(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["python3", "-c"]])

    result = await runner.run(
        session_id="s",
        task_id="1",
        actor="Test-Agent",
        command=ValidationCommand(argv=["python3", "-c", "print('ok')"], cwd=str(tmp_path)),
    )

    assert result.status == "passed"
    assert "ok" in result.output


@pytest.mark.asyncio
async def test_validation_runner_rejects_unapproved_command(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["pytest"]])

    with pytest.raises(ValueError, match="不在允许列表"):
        await runner.run(
            session_id="s",
            task_id="1",
            actor="Test-Agent",
            command=ValidationCommand(argv=["rm", "-rf", "x"], cwd=str(tmp_path)),
        )


@pytest.mark.asyncio
async def test_failed_validation_creates_failure_card(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    runner = ValidationRunner(store=store, allowed_commands=[["python3", "-c"]])

    result = await runner.run(
        session_id="s",
        task_id="1",
        actor="Test-Agent",
        command=ValidationCommand(argv=["python3", "-c", "raise SystemExit(3)"], cwd=str(tmp_path)),
    )

    failures = await store.list_failures("s")
    assert result.status == "failed"
    assert failures[0]["kind"] == FailureKind.TEST_FAILED.value
    assert failures[0]["task_id"] == "1"
