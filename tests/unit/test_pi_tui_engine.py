"""Slice 3 tests: pi engine inside the Textual TUI."""

from __future__ import annotations

import asyncio

from naumi_agent.pi_engine.tui_facade import PiTuiEngine, _PiRouterShim
from naumi_agent.runtime.ports.events import RuntimeEventType
from tests.unit.test_pi_web_engine import FakeSessionStore, FakeWebRpc, _FakeConfig


async def _tui_engine(rpc: FakeWebRpc) -> tuple[PiTuiEngine, FakeSessionStore]:
    store = FakeSessionStore()
    facade = PiTuiEngine(_FakeConfig(), store)
    facade._rpc = rpc
    facade._events = rpc.event_queue
    return facade, store


async def test_tui_engine_creates_pi_sessions_and_streams() -> None:
    rpc = FakeWebRpc()
    engine, store = await _tui_engine(rpc)
    session = await engine.get_or_create_session("对话")
    assert session.engine == "pi"
    assert engine._session is session

    received: list[tuple[str, dict]] = []

    class Sink:
        async def emit(self, event) -> None:
            received.append((str(event.type), dict(event.data)))

    async def drive() -> None:
        await asyncio.sleep(0.03)
        rpc.emit_simple_run("TUI 回复")

    driver = asyncio.get_event_loop().create_task(drive())
    result = await engine.run_streaming("你好", Sink())
    await driver

    assert result.status == "completed"
    assert result.response == "TUI 回复"
    assert received[0][0] == RuntimeEventType.RESPONSE_START.value
    assert any(t == RuntimeEventType.TOKEN.value and d["content"] == "TUI 回复"
               for t, d in received)
    assert received[-1][0] == RuntimeEventType.RESPONSE_END.value
    # history mirrored into the session store
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


async def test_tui_engine_reset_starts_fresh_pi_conversation() -> None:
    rpc = FakeWebRpc()
    engine, _ = await _tui_engine(rpc)
    await engine.get_or_create_session()
    engine.reset()
    assert engine._session is None
    assert engine._current_web_session == ""


async def test_tui_engine_status_shapes() -> None:
    rpc = FakeWebRpc()
    engine, _ = await _tui_engine(rpc)
    session = await engine.get_or_create_session()
    session.total_cost_usd = 0.5
    engine._state_cache = {
        "model": {"id": "glm-4.7", "provider": "zai-coding-cn",
                  "contextWindow": 128000},
        "messageCount": 4,
    }
    ctx = engine.get_context_info()
    assert ctx["window"] == 128000
    assert 0 <= ctx["percentage"] <= 100
    budget = engine.get_budget_info()
    assert budget == {"used_usd": 0.5, "max_usd": None, "enabled": False}
    assert engine.router.resolve_model("capable") == "pi/zai-coding-cn/glm-4.7"
    identity = engine.router.get_runtime_identity("x")
    assert identity.provider == "zai-coding-cn"
    # lifecycle no-ops stay silent
    await engine.start_long_running_services()
    engine.start_session_retention_worker()
    engine.set_permission_confirmer(lambda: None)


async def test_tui_engine_pi_helpers() -> None:
    rpc = FakeWebRpc()
    engine, _ = await _tui_engine(rpc)
    models = await engine.list_models()
    assert {m["id"] for m in models} == {"glm-4.7", "glm-5.2"}
    await engine.set_pi_model("zai-coding-cn", "glm-5.2")
    await engine.new_pi_conversation()
    await engine.compact_conversation()


def test_router_shim_reasoning_notice() -> None:
    shim = _PiRouterShim.__new__(_PiRouterShim)
    shim._facade = type("F", (), {"_state_cache": {}})()
    payload = shim.get_reasoning_effort_status().to_dict()
    assert "pi" in payload["warning"]
