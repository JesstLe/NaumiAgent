from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runtime.browser_heartbeat import BrowserExecutionHeartbeatFactory
from naumi_agent.tools.browser.orchestrator.task_run_store import TaskRunStore
from naumi_agent.tools.browser.orchestrator.task_runner import TaskRunner


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.replay_recording_enabled = False
    runtime.record_event = MagicMock()
    runtime.stop = AsyncMock(return_value={})
    runtime.enter_manual_control = AsyncMock(return_value={})
    runtime.exit_manual_control = AsyncMock(return_value={})
    return runtime


def _result(status: str = "completed") -> dict:
    return {
        "status": status,
        "step": 1,
        "summary": f"browser {status}",
        "history": [],
        "artifacts": None,
        "reports": None,
        "pendingInput": None,
    }


def _factory(tmp_path, *, store: HarnessStore | None = None):
    return BrowserExecutionHeartbeatFactory(
        store=store or HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        interval_seconds=1,
        timeout_seconds=3,
        auto_pulse=False,
    )


async def _wait_for_status(run: dict, status: str) -> None:
    for _ in range(100):
        if run.get("status") == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach {status}: {run.get('status')}")


@pytest.mark.asyncio
async def test_task_runner_persists_completed_browser_heartbeat(tmp_path) -> None:
    runtime = _runtime()
    factory = _factory(tmp_path)
    subagent = MagicMock()
    subagent.planner = MagicMock()
    subagent.delegate_task = AsyncMock(return_value=_result())
    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": runtime,
            "planner": MagicMock(),
            "subagent_factory": lambda _: subagent,
            "heartbeat_factory": factory,
        },
    )

    run = runner.create_run("Open the dashboard")
    await _wait_for_status(run, "completed")

    assert run["heartbeatSubjectId"].startswith("browser-execution-")
    assert run["heartbeatEpoch"] == 1
    assert run["heartbeatPhase"] == "stopped"
    assert run["heartbeatFailureCode"] == ""
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=run["heartbeatSubjectId"],
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.STOPPED
    assert heartbeat.detail_code == "browser_completed"


@pytest.mark.asyncio
async def test_waiting_resume_and_abort_keep_heartbeat_truthful(tmp_path) -> None:
    runtime = _runtime()
    factory = _factory(tmp_path)

    class WaitingSubagent:
        planner = MagicMock()

        async def delegate_task(self, instruction, options):
            reply = await options["onNeedsInput"]({
                "mode": "instruction",
                "question": "Choose the account",
            })
            return _result("aborted" if reply.get("abort") else "completed")

    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": runtime,
            "planner": MagicMock(),
            "subagent_factory": lambda _: WaitingSubagent(),
            "heartbeat_factory": factory,
        },
    )
    resumed = runner.create_run("Resume case")
    await _wait_for_status(resumed, "waiting_for_instruction")
    assert resumed["heartbeatPhase"] == "waiting"

    await runner.request_manual_control(
        resumed["id"],
        "Operator needs to inspect the page",
    )
    assert resumed["status"] == "manual_control"
    assert resumed["heartbeatPhase"] == "waiting"
    manual = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=resumed["heartbeatSubjectId"],
    )
    assert manual is not None
    assert manual.detail_code == "browser_manual_control"

    await runner.resume_run(resumed["id"], "Use account B")
    assert resumed["heartbeatPhase"] == "running"
    await _wait_for_status(resumed, "completed")
    assert resumed["heartbeatPhase"] == "stopped"

    aborted = runner.create_run("Abort case")
    await _wait_for_status(aborted, "waiting_for_instruction")
    runner.abort_run(aborted["id"], "Operator cancelled")
    await _wait_for_status(aborted, "aborted")
    assert aborted["heartbeatPhase"] == "stopped"
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=aborted["heartbeatSubjectId"],
    )
    assert heartbeat is not None
    assert heartbeat.detail_code == "browser_aborted"


@pytest.mark.asyncio
async def test_parallel_runs_get_distinct_heartbeat_subjects(tmp_path) -> None:
    shared = _runtime()
    release = asyncio.Event()
    started = asyncio.Event()
    active = 0

    class ParallelSubagent:
        planner = MagicMock()

        async def delegate_task(self, instruction, options):
            nonlocal active
            active += 1
            if active == 2:
                started.set()
            await release.wait()
            return _result()

    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": shared,
            "planner": MagicMock(),
            "runtime_factory": lambda _: _runtime(),
            "subagent_factory": lambda _: ParallelSubagent(),
            "max_concurrent_runs": 2,
            "heartbeat_factory": _factory(tmp_path),
        },
    )
    first = runner.create_run("First")
    second = runner.create_run("Second")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert first["heartbeatPhase"] == "running"
    assert second["heartbeatPhase"] == "running"
    assert first["heartbeatSubjectId"] != second["heartbeatSubjectId"]
    release.set()
    await _wait_for_status(first, "completed")
    await _wait_for_status(second, "completed")


@pytest.mark.asyncio
async def test_heartbeat_outage_does_not_change_browser_result(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    factory = _factory(tmp_path, store=store)
    store.record_heartbeat = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("/private/browser-profile")
    )
    subagent = MagicMock()
    subagent.planner = MagicMock()
    subagent.delegate_task = AsyncMock(return_value=_result())
    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": _runtime(),
            "planner": MagicMock(),
            "subagent_factory": lambda _: subagent,
            "heartbeat_factory": factory,
        },
    )

    run = runner.create_run("Still complete")
    await _wait_for_status(run, "completed")

    assert run["status"] == "completed"
    assert run["heartbeatFailureCode"] == "browser_heartbeat_start_failed"
    assert "private" not in run["heartbeatFailureCode"]


@pytest.mark.asyncio
async def test_terminal_heartbeat_failure_keeps_completed_result(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    original_record = store.record_heartbeat
    calls = 0

    async def fail_during_terminal(**kwargs):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise OSError("/private/terminal-write")
        return await original_record(**kwargs)

    store.record_heartbeat = fail_during_terminal  # type: ignore[method-assign]
    subagent = MagicMock()
    subagent.planner = MagicMock()
    subagent.delegate_task = AsyncMock(return_value=_result())
    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": _runtime(),
            "planner": MagicMock(),
            "subagent_factory": lambda _: subagent,
            "heartbeat_factory": _factory(tmp_path, store=store),
        },
    )

    run = runner.create_run("Complete despite terminal outage")
    await _wait_for_status(run, "completed")

    assert run["status"] == "completed"
    assert run["heartbeatFailureCode"] == "browser_heartbeat_terminal_failed"
    assert "private" not in run["heartbeatFailureCode"]


@pytest.mark.asyncio
async def test_restart_reconciles_interrupted_run_with_new_epoch(tmp_path) -> None:
    factory = _factory(tmp_path)
    original = await factory.create(run_id="run-interrupted")
    await original.start()
    TaskRunStore(tmp_path / "browser").persist([{
        "id": "run-interrupted",
        "status": "running",
        "summary": "working",
        "result": {"status": "running"},
    }])

    runner = TaskRunner(
        str(tmp_path / "browser"),
        options={
            "runtime": _runtime(),
            "planner": MagicMock(),
            "heartbeat_factory": factory,
        },
    )
    recovered = runner.get_run("run-interrupted")
    assert recovered is not None
    await _wait_for_status(recovered, "failed")
    for _ in range(100):
        if recovered.get("heartbeatPhase") == "failed":
            break
        await asyncio.sleep(0.01)

    assert recovered["heartbeatEpoch"] == 2
    assert recovered["heartbeatPhase"] == "failed"
    assert recovered["heartbeatFailureCode"] == ""
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="browser",
        subject_id=recovered["heartbeatSubjectId"],
    )
    assert heartbeat is not None
    assert heartbeat.detail_code == "browser_runtime_interrupted"


def test_task_runner_rejects_untyped_heartbeat_factory(tmp_path) -> None:
    with pytest.raises(ValueError, match="BrowserExecutionHeartbeatFactory"):
        TaskRunner(
            str(tmp_path),
            options={
                "runtime": _runtime(),
                "planner": MagicMock(),
                "heartbeat_factory": object(),
            },
        )
