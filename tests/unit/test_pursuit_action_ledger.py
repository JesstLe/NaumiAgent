from __future__ import annotations

import asyncio
import json
import shlex
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.background import BackgroundRunner, BackgroundTaskStore
from naumi_agent.background.tools import create_background_tools
from naumi_agent.orchestrator.pursuit import (
    GoalPursuitLoop,
    PursuitRun,
    PursuitRunStatus,
)
from naumi_agent.orchestrator.pursuit_action_ledger import (
    PursuitActionRecord,
    PursuitActionState,
    canonical_action_arguments,
    make_action_key,
)
from naumi_agent.orchestrator.pursuit_store import (
    PursuitStore,
    PursuitStoreConflictError,
    PursuitStoreError,
)
from naumi_agent.tools.base import ToolResult


def _store_with_run(tmp_path, *, run_id: str = "pursuit_action") -> PursuitStore:
    store = PursuitStore(tmp_path / "pursuit")
    store.save_run(PursuitRun(
        id=run_id,
        goal="记录外部行动",
        status=PursuitRunStatus.RUNNING,
        phase="action_inflight",
        started_at=1.0,
        updated_at=1.0,
        iteration=1,
    ))
    return store


def _record(
    *,
    run_id: str = "pursuit_action",
    iteration: int = 1,
    action_id: str = "a1",
    command: str = "echo ok",
    prepared_at: float = 1.0,
) -> PursuitActionRecord:
    arguments = {"command": command, "cwd": ""}
    _, digest, size = canonical_action_arguments(arguments)
    action_key = make_action_key(
        run_id=run_id,
        iteration=iteration,
        action_id=action_id,
        tool_name="bash_run",
        arguments_sha256=digest,
    )
    return PursuitActionRecord(
        action_key=action_key,
        run_id=run_id,
        iteration=iteration,
        action_id=action_id,
        tool_name="bash_run",
        arguments_sha256=digest,
        arguments_size_bytes=size,
        argument_summary=f"tool=bash_run; arguments_sha256={digest}",
        state=PursuitActionState.PREPARED,
        sequence=1,
        dispatch_token=action_key,
        background_task_id="",
        result_status="",
        result_summary="",
        result_sha256="",
        prepared_at=prepared_at,
        updated_at=prepared_at,
    )


def test_action_lifecycle_round_trips_as_authenticated_event_chain(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    prepared = store.prepare_action(_record())
    dispatched = store.mark_action_dispatched(prepared.action_key, updated_at=2.0)
    waiting = store.mark_action_waiting(
        prepared.action_key,
        background_task_id="bg_0042",
        updated_at=3.0,
        result_summary="后台任务已启动",
    )
    completed = store.mark_action_terminal(
        prepared.action_key,
        succeeded=True,
        result_status="completed",
        result="7 passed",
        updated_at=4.0,
    )

    reopened = PursuitStore(store.base_dir)
    assert reopened.get_action(prepared.action_key) == completed
    assert reopened.get_action_by_background_task(
        run_id=prepared.run_id,
        task_id="bg_0042",
    ) == completed
    assert reopened.list_actions(prepared.run_id) == [completed]
    assert reopened.list_actions(prepared.run_id, unresolved_only=True) == []
    assert [item.state for item in reopened.list_action_events(prepared.action_key)] == [
        PursuitActionState.PREPARED,
        PursuitActionState.DISPATCHED,
        PursuitActionState.WAITING,
        PursuitActionState.COMPLETED,
    ]
    assert dispatched.sequence == 2
    assert waiting.sequence == 3
    assert completed.sequence == 4


def test_prepare_and_same_transition_are_idempotent_but_identity_conflicts(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    first = store.prepare_action(_record())
    replay = store.prepare_action(_record(prepared_at=99.0))
    assert replay == first

    dispatched = store.mark_action_dispatched(first.action_key, updated_at=2.0)
    assert store.mark_action_dispatched(first.action_key, updated_at=99.0) == dispatched

    conflicting = first.model_copy(update={"argument_summary": "different"})
    with pytest.raises(PursuitStoreConflictError, match="不同的行动输入"):
        store.prepare_action(conflicting)


def test_state_regression_and_conflicting_terminal_receipt_fail_closed(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    record = store.prepare_action(_record())
    store.mark_action_dispatched(record.action_key, updated_at=2.0)
    terminal = store.mark_action_terminal(
        record.action_key,
        succeeded=True,
        result_status="completed",
        result="ok",
        updated_at=3.0,
    )
    assert store.mark_action_terminal(
        record.action_key,
        succeeded=True,
        result_status="completed",
        result="ok",
        updated_at=99.0,
    ) == terminal

    with pytest.raises(PursuitStoreConflictError, match="不能从 completed"):
        store.mark_action_terminal(
            record.action_key,
            succeeded=False,
            result_status="failed",
            result="different",
            updated_at=4.0,
        )
    with pytest.raises(PursuitStoreConflictError, match="不能从 completed"):
        store.mark_action_waiting(
            record.action_key,
            background_task_id="bg_late",
            updated_at=5.0,
        )


def test_concurrent_terminal_writers_cannot_overwrite_each_other(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    record = store.prepare_action(_record())
    store.mark_action_dispatched(record.action_key, updated_at=2.0)
    barrier = threading.Barrier(2)

    def finish(value: str) -> str:
        barrier.wait()
        try:
            PursuitStore(store.base_dir).mark_action_terminal(
                record.action_key,
                succeeded=value == "ok",
                result_status="completed" if value == "ok" else "failed",
                result=value,
                updated_at=3.0,
            )
        except PursuitStoreConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(finish, ("ok", "failed")))

    assert sorted(outcomes) == ["conflict", "saved"]
    assert len(store.list_action_events(record.action_key)) == 3


def test_tampering_and_broken_hash_chain_are_rejected(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    record = store.prepare_action(_record())
    store.mark_action_dispatched(record.action_key, updated_at=2.0)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_action_events SET payload_json = payload_json || ' ' "
            "WHERE action_key = ? AND sequence = 1",
            (record.action_key,),
        )

    with pytest.raises(PursuitStoreError, match="摘要校验失败"):
        store.get_action(record.action_key)


def test_ledger_never_persists_raw_arguments_or_secrets(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    secret = "sk-super-secret-value-123456"
    record = _record(command=f"curl -H 'Authorization: Bearer {secret}' example.test")
    store.prepare_action(record)
    store.mark_action_dispatched(record.action_key, updated_at=2.0)
    store.mark_action_terminal(
        record.action_key,
        succeeded=False,
        result_status="failed",
        result=f"api_key={secret}",
        updated_at=3.0,
    )

    raw_db = store.db_path.read_bytes()
    assert secret.encode() not in raw_db
    assert b"curl -H" not in raw_db
    restored = store.get_action(record.action_key)
    assert restored is not None
    assert "[REDACTED]" in restored.result_summary


def test_same_planner_action_id_is_distinct_across_iterations() -> None:
    first = _record(iteration=1)
    second = _record(iteration=2)
    assert first.action_key != second.action_key


@pytest.mark.asyncio
async def test_sync_bash_dispatch_is_journaled_before_executor_and_reused(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_action")
    assert run is not None
    observed_states: list[PursuitActionState] = []

    async def execute(call):
        arguments = json.loads(call.arguments)
        _, digest, _ = canonical_action_arguments(arguments)
        action_key = make_action_key(
            run_id=run.id,
            iteration=run.iteration,
            action_id="a1",
            tool_name="bash_run",
            arguments_sha256=digest,
        )
        persisted = store.get_action(action_key)
        assert persisted is not None
        observed_states.append(persisted.state)
        return ToolResult(
            call_id=call.id,
            status="success",
            content="ok\n[exit code: 0]",
        )

    executor = AsyncMock(side_effect=execute)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=MagicMock(),
        subagent_manager=MagicMock(),
        store=store,
        execute_tool_call=executor,
    )
    loop._run = run
    loop._llm_call = AsyncMock(return_value="echo ok")  # type: ignore[method-assign]
    bash = MagicMock()

    first = await loop._execute_via_bash(bash, "执行一次快速命令", "a1")
    replay = await loop._execute_via_bash(bash, "执行一次快速命令", "a1")

    assert first["status"] == "completed"
    assert replay["status"] == "completed"
    assert "复用持久行动回执" in replay["output"]
    assert observed_states == [PursuitActionState.DISPATCHED]
    assert executor.await_count == 1


@pytest.mark.asyncio
async def test_background_task_id_and_terminal_collection_update_same_action(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_action")
    assert run is not None
    background = MagicMock()
    background.execute = AsyncMock(
        return_value="后台任务已启动。\n\n- 任务 ID：`bg_0007`"
    )
    status = MagicMock()
    status.execute = AsyncMock(
        return_value="### 后台任务 bg_0007\n- 状态：已完成\n- 退出码：0"
    )
    output = MagicMock()
    output.execute = AsyncMock(return_value="3 passed")
    tools = MagicMock()
    tools.get = MagicMock(side_effect=lambda name: {
        "background_run": background,
        "background_status": status,
        "background_read_output": output,
    }.get(name))
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=tools,
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._run = run
    loop._llm_call = AsyncMock(  # type: ignore[method-assign]
        return_value="python3 -m pytest tests/unit/test_small.py -q"
    )

    started = await loop._execute_via_bash(
        MagicMock(), "运行耗时的小模块测试", "a1"
    )
    waiting = store.get_action_by_background_task(
        run_id=run.id,
        task_id="bg_0007",
    )
    await loop._collect_background_results()
    completed = store.get_action_by_background_task(
        run_id=run.id,
        task_id="bg_0007",
    )

    assert started["status"] == "waiting"
    assert waiting is not None and waiting.state is PursuitActionState.WAITING
    assert completed is not None and completed.state is PursuitActionState.COMPLETED
    assert loop._pending_background == []


@pytest.mark.asyncio
async def test_dispatched_background_action_retries_through_caller_idempotency_key(
    tmp_path,
) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_action")
    assert run is not None
    command = "python3 -c \"import time; time.sleep(0.1)\""
    background = MagicMock()

    async def execute_background(**arguments):
        assert arguments["idempotency_key"].startswith("pact_")
        persisted = store.get_action(arguments["idempotency_key"])
        assert persisted is not None
        assert persisted.state is PursuitActionState.DISPATCHED
        return "后台任务已启动。\n\n- 任务 ID：`bg_0091`"

    background.execute = AsyncMock(side_effect=execute_background)
    tools = MagicMock()
    tools.get = MagicMock(side_effect=lambda name: {
        "background_run": background,
    }.get(name))
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=tools,
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._run = run
    loop._llm_call = AsyncMock(return_value=command)  # type: ignore[method-assign]
    base_arguments = {"command": command, "cwd": "", "timeout_seconds": 1800}
    prepared = await loop._prepare_action_dispatch(
        action_id="retry-bg",
        tool_name="background_run",
        arguments=base_arguments,
    )
    assert prepared is not None
    store.mark_action_dispatched(prepared.action_key, updated_at=2.0)

    result = await loop._execute_via_bash(
        MagicMock(),
        "运行 sleep 后台任务",
        "retry-bg",
    )

    assert result["status"] == "waiting"
    assert result["background_task_id"] == "bg_0091"
    background.execute.assert_awaited_once()
    waiting = store.get_action(prepared.action_key)
    assert waiting is not None
    assert waiting.state is PursuitActionState.WAITING


@pytest.mark.asyncio
async def test_real_background_process_reconciles_to_terminal_ledger(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    run = store.get_run("pursuit_action")
    assert run is not None
    runner = BackgroundRunner(BackgroundTaskStore(tmp_path / "background"))
    registered = {tool.name: tool for tool in create_background_tools(runner)}
    tools = MagicMock()
    tools.get = MagicMock(side_effect=registered.get)
    loop = GoalPursuitLoop(
        router=MagicMock(),
        tool_registry=tools,
        subagent_manager=MagicMock(),
        store=store,
    )
    loop._run = run
    command = (
        f"{shlex.quote(sys.executable)} -c \"import time; "
        "time.sleep(0.1); print('ledger-real-ok')\""
    )
    loop._llm_call = AsyncMock(return_value=command)  # type: ignore[method-assign]

    try:
        started = await loop._execute_via_bash(
            MagicMock(), "运行 sleep 真实后台进程", "real-bg"
        )
        assert started["status"] == "waiting"
        task_id = started["background_task_id"]
        for _ in range(50):
            task = runner.get(task_id)
            if task is not None and task.is_finished:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("真实后台任务未在预期时间内结束")

        await loop._collect_background_results()
        terminal = store.get_action_by_background_task(
            run_id=run.id,
            task_id=task_id,
        )
        assert terminal is not None
        assert terminal.state is PursuitActionState.COMPLETED
        assert "ledger-real-ok" in terminal.result_summary
    finally:
        await runner.shutdown()
