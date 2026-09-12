"""Slice 3 app-level tests: NaumiApp driving a PiTuiEngine via Textual pilot."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Markdown

from naumi_agent.pi_engine.tui_facade import PiTuiEngine
from naumi_agent.tui.app import ChatPanel, NaumiApp, UserInputMessage
from tests.unit.test_pi_web_engine import FakeSessionStore, FakeWebRpc, _FakeConfig


async def _pi_app(rpc: FakeWebRpc) -> NaumiApp:
    store = FakeSessionStore()
    engine = PiTuiEngine(_FakeConfig(), store)
    engine._rpc = rpc
    engine._events = rpc.event_queue
    return NaumiApp(engine=engine)


def _chat_text(app: NaumiApp) -> str:
    parts: list[str] = []
    for widget in app.query(Markdown):
        source = str(getattr(widget, "source", "") or "")
        parts.append(source)
    return "\n".join(parts)


@pytest.mark.timeout(60)
async def test_pi_tui_mounts_and_shows_engine_notice() -> None:
    rpc = FakeWebRpc()
    app = await _pi_app(rpc)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app._pi_engine_blocks("测试功能 ") is True
        app.post_message(UserInputMessage("/engine"))
        for _ in range(60):
            await asyncio.sleep(0.05)
            await pilot.pause()
            if "engine=pi" in _chat_text(app):
                break
        assert "engine=pi" in _chat_text(app)
        assert "zai-coding-cn" in _chat_text(app)


@pytest.mark.timeout(60)
async def test_pi_tui_blocks_naumi_only_commands() -> None:
    rpc = FakeWebRpc()
    app = await _pi_app(rpc)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.post_message(UserInputMessage("/todo"))
        for _ in range(60):
            await asyncio.sleep(0.05)
            await pilot.pause()
            if "暂不支持" in _chat_text(app):
                break
        assert "暂不支持" in _chat_text(app)


@pytest.mark.timeout(90)
async def test_pi_tui_full_conversation_roundtrip() -> None:
    rpc = FakeWebRpc()
    app = await _pi_app(rpc)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Headless runs can defer the Markdown re-render beyond the test
        # window, so assert on the authoritative pipeline record: what the
        # streaming token path actually delivered to the chat panel.
        _chat_probe = app.query_one(ChatPanel)
        _orig_tok = _chat_probe.add_response_token
        _record: dict[str, object] = {"tokens": "", "finalize": 0}
        _orig_finalize = _chat_probe.finalize

        def _tok(t: str) -> None:
            _record["tokens"] = str(_record["tokens"]) + t
            _orig_tok(t)

        def _fin(*a, **k) -> None:
            _record["finalize"] = int(_record["finalize"]) + 1
            _orig_finalize(*a, **k)

        _chat_probe.add_response_token = _tok
        _chat_probe.finalize = _fin

        async def drive() -> None:
            await asyncio.sleep(0.05)
            rpc.emit_simple_run("来自 pi 的回复")

        driver = asyncio.get_event_loop().create_task(drive())
        app.post_message(UserInputMessage("你好"))
        engine = app.engine
        assert isinstance(engine, PiTuiEngine)
        # Authoritative completion signal: the facade persists the assistant
        # reply once the pi run settles (UI rendering follows on the same loop).
        for _ in range(120):
            await asyncio.sleep(0.05)
            await pilot.pause()
            session = engine._session
            if session and any(m["role"] == "assistant" for m in session.messages):
                break
        await driver
        for _ in range(20):
            await asyncio.sleep(0.05)
            await pilot.pause()
            if int(_record["finalize"]) > 0:
                break
        assert str(_record["tokens"]) == "来自 pi 的回复"
        assert int(_record["finalize"]) == 1
        # session created with engine=pi and history mirrored
        session = engine._session
        assert session is not None and session.engine == "pi"
        assert [m["role"] for m in session.messages] == ["user", "assistant"]
