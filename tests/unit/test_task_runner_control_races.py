"""Deterministic control races in the real TaskRunner, without a live browser."""
from __future__ import annotations

import asyncio

import pytest

from naumi_agent.tools.browser.orchestrator.task_runner import TaskRunner


class Runtime:
    def __init__(self):
        self.manual = False
        self.enter_count = self.exit_count = self.stop_count = 0
        self.enter_gate = self.exit_gate = None
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()
        self.stopped = asyncio.Event()
        self.switching = False
        self.stop_while_switching = False

    def record_event(self, *args, **kwargs):
        pass

    async def enter_manual_control(self):
        self.enter_count += 1
        self.entered.set()
        if self.enter_gate:
            await self.enter_gate.wait()
        self.manual = True

    async def exit_manual_control(self):
        self.exit_count += 1
        self.switching = True
        self.exited.set()
        if self.exit_gate:
            await self.exit_gate.wait()
        self.switching = False
        self.manual = False

    async def stop(self):
        self.stop_count += 1
        self.stop_while_switching = self.switching
        self.stopped.set()
        return {}


def make_runner(path, runtime, **options):
    return TaskRunner(str(path), {
        "runtime": runtime, "planner": object(),
        "subagent_factory": lambda _: object(), **options,
    })


def run_record(status="manual_control"):
    return {"id": "audit-run", "status": status, "summary": "", "result": None,
            "pendingInput": {"mode": "manual_control"}, "browserSource": "managed",
            "taskInstruction": "audit", "maxSteps": 2, "template": None,
            "artifacts": None, "createdAt": "2026-11-04T00:00:00Z"}


def pending(runner, status="manual_control"):
    run = run_record(status)
    runner.runs = [run]
    future = asyncio.get_running_loop().create_future()
    runner._pending_replies[run["id"]] = future
    return run, future


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("phase", ["browser", "heartbeat"])
@pytest.mark.asyncio
async def test_abort_during_resume_preserves_abort_state_and_reply(tmp_path, phase):
    runtime = Runtime()
    runner = make_runner(tmp_path, runtime)
    run, future = pending(runner)
    gate, entered = asyncio.Event(), asyncio.Event()
    if phase == "browser":
        runtime.exit_gate = gate
        entered = runtime.exited
    else:
        async def heartbeat(_run):
            entered.set()
            await gate.wait()
        runner._resume_run_heartbeat = heartbeat
    task = asyncio.create_task(runner.resume_run(run["id"]))
    await entered.wait()
    runner.abort_run(run["id"], "operator abort")
    gate.set()
    with pytest.raises(ValueError):
        await task
    assert run["status"] == "aborting"
    assert future.result()["abort"] is True
    assert run["id"] not in runner._pending_replies


@pytest.mark.asyncio
async def test_two_resumes_switch_browser_once(tmp_path):
    runtime = Runtime()
    runtime.exit_gate = asyncio.Event()
    runner = make_runner(tmp_path, runtime)
    run, future = pending(runner)
    first = asyncio.create_task(runner.resume_run(run["id"], "first"))
    await runtime.exited.wait()
    second = asyncio.create_task(runner.resume_run(run["id"], "second"))
    await asyncio.sleep(0.01)
    runtime.exit_gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert runtime.exit_count == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert future.result()["instruction"] == "first"


def execution_runner(path, shared, actual, *, mode="manual_control", concurrency=2):
    class Subagent:
        async def delegate_task(self, instruction, options):
            reply = await options["onNeedsInput"]({"mode": mode, "question": "audit"})
            return {"status": "aborted" if reply.get("abort") else "completed",
                    "summary": "finished", "pendingInput": None}

    runner = make_runner(path, shared, max_concurrent_runs=concurrency,
                         runtime_factory=lambda _: actual, subagent_factory=lambda _: Subagent())
    run = run_record("queued")
    run["pendingInput"] = None
    runner.runs = [run]
    runner._active_slots = 1
    task = asyncio.create_task(runner._execute_run(run))
    return runner, run, task


@pytest.mark.asyncio
async def test_isolated_manual_controls_use_owned_runtime_and_cleanup_map(tmp_path):
    shared, actual = Runtime(), Runtime()
    runner, run, task = execution_runner(tmp_path, shared, actual, mode="instruction")
    await wait_for(lambda: run["status"] == "waiting_for_instruction")
    await runner.request_manual_control(run["id"])
    assert actual.manual
    assert not shared.manual
    await runner.resume_run(run["id"])
    await asyncio.wait_for(task, 2)
    assert not actual.manual
    assert actual.exit_count == 1
    assert shared.enter_count == shared.exit_count == shared.stop_count == 0
    assert actual.stop_count == 1
    assert not runner._run_runtimes
    assert runner.active_slots == 0


@pytest.mark.asyncio
async def test_abort_during_enter_manual_does_not_create_unresolvable_wait(tmp_path):
    runtime = Runtime()
    runtime.enter_gate = asyncio.Event()
    runner, run, task = execution_runner(tmp_path, runtime, runtime, concurrency=1)
    await runtime.entered.wait()
    runner.abort_run(run["id"], "aborted while entering")
    runtime.enter_gate.set()
    await asyncio.wait_for(task, 0.5)
    assert run["status"] == "aborted"
    assert not runner._pending_replies
    assert runner.active_slots == 0


@pytest.mark.asyncio
async def test_abort_cleanup_waits_for_inflight_browser_switch(tmp_path):
    runtime = Runtime()
    runner, run, task = execution_runner(tmp_path, runtime, runtime, concurrency=1)
    await wait_for(lambda: run["status"] == "manual_control")
    runtime.exit_gate = asyncio.Event()
    resume = asyncio.create_task(runner.resume_run(run["id"]))
    await runtime.exited.wait()
    runner.abort_run(run["id"])
    await asyncio.sleep(0.01)
    was_stopped = runtime.stopped.is_set()
    runtime.exit_gate.set()
    outcome = await asyncio.gather(resume, task, return_exceptions=True)
    assert isinstance(outcome[0], ValueError)
    assert not was_stopped
    assert not runtime.stop_while_switching
    assert runtime.stopped.is_set()
    assert run["status"] == "aborted"


@pytest.mark.asyncio
async def test_manual_request_aborted_during_switch_does_not_overwrite_abort(tmp_path):
    runtime = Runtime()
    runtime.enter_gate = asyncio.Event()
    runner, run, task = execution_runner(
        tmp_path, runtime, runtime, mode="instruction", concurrency=1,
    )
    await wait_for(lambda: run["status"] == "waiting_for_instruction")
    request = asyncio.create_task(runner.request_manual_control(run["id"]))
    await runtime.entered.wait()
    runner.abort_run(run["id"])
    runtime.enter_gate.set()
    with pytest.raises(ValueError):
        await request
    await asyncio.wait_for(task, 2)
    assert run["status"] == "aborted"
    assert not runner._pending_replies


@pytest.mark.asyncio
async def test_isolated_missing_runtime_never_falls_back_to_shared_browser(tmp_path):
    shared = Runtime()
    runner = make_runner(tmp_path, shared, max_concurrent_runs=2)
    run, future = pending(runner)
    try:
        with pytest.raises(ValueError, match="浏览器实例"):
            await runner.resume_run(run["id"])
        assert shared.exit_count == 0
        assert not future.done()
    finally:
        future.cancel()


@pytest.mark.asyncio
async def test_waiting_state_is_published_only_after_channel_and_heartbeat_are_ready(tmp_path):
    runtime = Runtime()
    runner = make_runner(tmp_path, runtime)
    run = run_record("running")
    run["pendingInput"] = None
    runner.runs = [run]
    gate, entered = asyncio.Event(), asyncio.Event()

    async def heartbeat(waiting, **kwargs):
        entered.set()
        await gate.wait()
        waiting["heartbeatPhase"] = "waiting"

    runner._set_run_heartbeat_waiting = heartbeat
    task = asyncio.create_task(runner._wait_for_run_input(
        run, runtime, {"mode": "instruction", "question": "audit"}, 1000,
    ))
    await entered.wait()
    assert run["status"] == "running"
    assert run["id"] in runner._pending_replies
    runner.abort_run(run["id"])
    gate.set()
    result = await task
    assert result["abort"]
    assert run["status"] == "aborting"
    assert not runner._pending_replies


@pytest.mark.asyncio
async def test_late_abort_is_rejected_while_final_result_is_being_cleaned(tmp_path):
    runtime = Runtime()
    gate, stopping = asyncio.Event(), asyncio.Event()

    async def stop():
        stopping.set()
        await gate.wait()
        return {}

    runtime.stop = stop
    runner, run, task = execution_runner(tmp_path, runtime, runtime, concurrency=1)
    await wait_for(lambda: run["status"] == "manual_control")
    await runner.resume_run(run["id"])
    await stopping.wait()
    assert run["status"] != "completed"
    with pytest.raises(ValueError, match="正在清理"):
        runner.abort_run(run["id"])
    gate.set()
    await task
    assert run["status"] == "completed"
    assert not runner._run_runtimes
    assert not runner._control_locks


@pytest.mark.asyncio
async def test_two_owned_runtimes_receive_independent_manual_controls(tmp_path):
    shared = Runtime()
    runtimes = {"one": Runtime(), "two": Runtime()}

    class Subagent:
        async def delegate_task(self, instruction, options):
            reply = await options["onNeedsInput"]({"mode": "instruction"})
            return {"status": "aborted" if reply.get("abort") else "completed"}

    runner = make_runner(tmp_path, shared, max_concurrent_runs=2,
                         runtime_factory=lambda key: runtimes[key],
                         subagent_factory=lambda _: Subagent())
    runs = [{**run_record("queued"), "id": key, "pendingInput": None} for key in runtimes]
    runner.runs = runs
    runner._active_slots = 2
    tasks = [asyncio.create_task(runner._execute_run(run)) for run in runs]
    await wait_for(lambda: all(run["status"] == "waiting_for_instruction" for run in runs))
    await runner.request_manual_control("one")
    assert runtimes["one"].manual and not runtimes["two"].manual
    await runner.resume_run("one")
    runner.abort_run("two")
    await asyncio.gather(*tasks)
    assert runtimes["one"].exit_count == 1
    assert runtimes["two"].enter_count == runtimes["two"].exit_count == 0
    assert shared.enter_count == shared.exit_count == shared.stop_count == 0
    assert runner.active_slots == 0
    assert not runner._run_runtimes
