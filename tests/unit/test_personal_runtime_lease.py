"""Atomic execution leases over real Engine/session persistence, without providers.

Events stop the real session port or the existing user-prompt hook. No timing
sleep, fake Engine, or replacement session store controls the tested ordering.
"""

from __future__ import annotations

import asyncio

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal import runtime
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.streaming.sinks import NullEventSink

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
_TIMEOUT = 15


@pytest.fixture
async def lease_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))

    async def no_provider(self, **kwargs):
        raise AssertionError("The execution-lease regression must not call a provider")

    async def no_stream(self, **kwargs):
        raise AssertionError("The execution-lease regression must not stream a provider")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass", "max_turns": 2},
    )
    engine = create_agent_engine(config)
    try:
        yield engine
    finally:
        await asyncio.wait_for(engine.shutdown(), _TIMEOUT)


async def _run(engine, mode, task, started=None):
    # Setting the checkpoint does not yield: the caller observes it only after
    # this task enters the real method and suspends at its first blocking await.
    if started is not None:
        started.set()
    if mode == "streaming":
        return await engine.run_streaming(task, NullEventSink())
    return await engine.run(task)


def _block_body(engine, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def gate(task, **kwargs):
        calls.append(task)
        entered.set()
        await asyncio.wait_for(release.wait(), _TIMEOUT)
        # Abort through the existing prompt-hook contract before any model call.
        return None

    monkeypatch.setattr(engine, "_fire_user_prompt_submit", gate)
    return entered, release, calls


async def _cleanup(tasks, *events):
    for event in events:
        event.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _TIMEOUT)


@pytest.mark.parametrize("mode", ("run", "streaming"))
async def test_cold_concurrent_runs_initialize_once_and_cannot_overwrite_lease(
    lease_engine, monkeypatch, mode,
):
    engine = lease_engine
    creating, allow_create = asyncio.Event(), asyncio.Event()
    second_started = asyncio.Event()
    body_entered, allow_body, body_calls = _block_body(engine, monkeypatch)
    real_create = engine._session_port.create_session
    create_calls = []

    async def gated_create(**kwargs):
        create_calls.append(kwargs)
        creating.set()
        await asyncio.wait_for(allow_create.wait(), _TIMEOUT)
        return await real_create(**kwargs)

    monkeypatch.setattr(engine._session_port, "create_session", gated_create)
    tasks = []
    try:
        first = asyncio.create_task(_run(engine, mode, "first"), name="lease-first")
        tasks.append(first)
        await asyncio.wait_for(creating.wait(), _TIMEOUT)
        second = asyncio.create_task(
            _run(engine, mode, "second", second_started), name="lease-second",
        )
        tasks.append(second)
        await asyncio.wait_for(second_started.wait(), _TIMEOUT)
        assert len(create_calls) == 1
        assert engine._session is None
        assert getattr(engine, "_personal_active_turn", None) is None

        allow_create.set()
        await asyncio.wait_for(body_entered.wait(), _TIMEOUT)
        binding = engine._personal_active_turn
        assert binding == (engine._session.id, "main", 0)
        with pytest.raises(RuntimeError, match="正在执行"):
            await asyncio.wait_for(second, _TIMEOUT)
        assert engine._personal_active_turn is binding
        assert body_calls == ["first"]
        assert len(create_calls) == 1
        sessions, total = await engine.session_store.list_sessions(page=1, page_size=20)
        assert total == 1
        assert sessions[0].id == binding[0]

        allow_body.set()
        result = await asyncio.wait_for(first, _TIMEOUT)
        assert result.status == "error"
        assert engine._personal_active_turn is None
    finally:
        await _cleanup(tasks, allow_create, allow_body)


@pytest.mark.parametrize("mode", ("run", "streaming"))
async def test_run_waits_for_started_load_and_freezes_its_completed_session(
    lease_engine, monkeypatch, mode,
):
    engine = lease_engine
    first = await engine.create_session(title="A")
    target = await engine.create_session(title="B")
    assert await engine.load_session(first.id)
    loading, allow_load = asyncio.Event(), asyncio.Event()
    run_started = asyncio.Event()
    body_entered, allow_body, body_calls = _block_body(engine, monkeypatch)
    real_load = engine._session_port.load

    async def gated_load(session_id):
        if session_id == target.id:
            loading.set()
            await asyncio.wait_for(allow_load.wait(), _TIMEOUT)
        return await real_load(session_id)

    monkeypatch.setattr(engine._session_port, "load", gated_load)
    tasks = []
    try:
        loading_task = asyncio.create_task(engine.load_session(target.id), name="load-before-run")
        tasks.append(loading_task)
        await asyncio.wait_for(loading.wait(), _TIMEOUT)
        assert engine._session_transition_lock.locked()
        running = asyncio.create_task(
            _run(engine, mode, "after-load", run_started), name="run-behind-load",
        )
        tasks.append(running)
        await asyncio.wait_for(run_started.wait(), _TIMEOUT)
        assert not body_entered.is_set()
        assert getattr(engine, "_personal_active_turn", None) is None
        assert engine._session.id == first.id

        allow_load.set()
        assert await asyncio.wait_for(loading_task, _TIMEOUT)
        await asyncio.wait_for(body_entered.wait(), _TIMEOUT)
        assert engine._session.id == target.id
        assert engine._personal_active_turn == (target.id, "main", 0)
        assert body_calls == ["after-load"]
        runtime.require_current_agent(engine)

        allow_body.set()
        await asyncio.wait_for(running, _TIMEOUT)
        assert engine._personal_active_turn is None
    finally:
        await _cleanup(tasks, allow_load, allow_body)


@pytest.mark.parametrize("mode", ("run", "streaming"))
async def test_queued_load_rechecks_idle_inside_lock_before_transition_side_effects(
    lease_engine, monkeypatch, mode,
):
    engine = lease_engine
    first = await engine.create_session(title="A")
    target = await engine.create_session(title="B")
    assert await engine.load_session(first.id)
    run_started, checked_idle = asyncio.Event(), asyncio.Event()
    body_entered, allow_body, body_calls = _block_body(engine, monkeypatch)
    real_idle = runtime.assert_idle_transition
    real_load = engine._session_port.load
    real_begin = engine._begin_active_session_transition
    idle_checks, port_reads, transition_starts = [], [], []

    def observe_idle(candidate):
        if asyncio.current_task().get_name() == "queued-load":
            idle_checks.append(getattr(candidate, "_personal_active_turn", None))
            checked_idle.set()
        return real_idle(candidate)

    async def observe_load(session_id):
        port_reads.append(session_id)
        return await real_load(session_id)

    def observe_begin(session_id):
        transition_starts.append(session_id)
        return real_begin(session_id)

    monkeypatch.setattr(runtime, "assert_idle_transition", observe_idle)
    monkeypatch.setattr(engine._session_port, "load", observe_load)
    monkeypatch.setattr(engine, "_begin_active_session_transition", observe_begin)
    generation = engine._session_authorization_generation
    tasks = []
    owns_lock = False
    try:
        await asyncio.wait_for(engine._session_transition_lock.acquire(), _TIMEOUT)
        owns_lock = True
        running = asyncio.create_task(
            _run(engine, mode, "wins-lease", run_started), name="run-before-queued-load",
        )
        tasks.append(running)
        await asyncio.wait_for(run_started.wait(), _TIMEOUT)
        assert getattr(engine, "_personal_active_turn", None) is None
        queued = asyncio.create_task(engine.load_session(target.id), name="queued-load")
        tasks.append(queued)
        await asyncio.wait_for(checked_idle.wait(), _TIMEOUT)
        assert idle_checks == [None]
        assert transition_starts == []
        assert engine._session_transition_tokens == {}

        engine._session_transition_lock.release()
        owns_lock = False
        await asyncio.wait_for(body_entered.wait(), _TIMEOUT)
        binding = engine._personal_active_turn
        with pytest.raises(RuntimeError, match="正在执行"):
            await asyncio.wait_for(queued, _TIMEOUT)
        assert len(idle_checks) == 2
        assert idle_checks[1] is binding
        assert engine._personal_active_turn is binding
        assert binding == (first.id, "main", 0)
        assert engine._session.id == first.id
        assert port_reads == []
        assert transition_starts == []
        assert engine._session_transition_tokens == {}
        assert engine._session_transition_epochs == {}
        assert engine._session_authorization_generation == generation
        assert body_calls == ["wins-lease"]

        allow_body.set()
        await asyncio.wait_for(running, _TIMEOUT)
        assert engine._personal_active_turn is None
    finally:
        if owns_lock:
            engine._session_transition_lock.release()
        await _cleanup(tasks, allow_body)
