"""端到端测试 — 需要真实 API 调用."""

from __future__ import annotations

import os

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine

pytestmark = pytest.mark.skipif(
    not os.environ.get("NAUMI_MODELS__API_KEY"),
    reason="NAUMI_MODELS__API_KEY not set",
)


@pytest.fixture
def engine() -> AgentEngine:
    config_path = os.environ.get("NAUMI_E2E_CONFIG", "config.yaml")
    config = AppConfig.from_yaml(config_path)
    return AgentEngine(config)


@pytest.mark.asyncio
async def test_simple_qa(engine: AgentEngine) -> None:
    result = await engine.run("你好，请用一句话介绍你自己")
    assert result.status == "completed"
    assert len(result.response) > 0
    await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming(engine: AgentEngine) -> None:
    chunks: list[str] = []

    async def on_event(event_type: str, data: dict) -> None:
        if event_type == "token":
            chunks.append(data["content"])

    result = await engine.run_streaming("1+1等于几？只回答数字", on_event)
    assert result.status == "completed"
    assert len(chunks) > 0
    await engine.shutdown()


@pytest.mark.asyncio
async def test_multi_turn(engine: AgentEngine) -> None:
    """多轮对话 — 第二轮应能引用第一轮的上下文."""
    r1 = await engine.run("我的名字是 Alice，请记住。")
    assert r1.status == "completed"

    r2 = await engine.run("我刚才告诉你我叫什么名字？只回答名字。")
    assert r2.status == "completed"
    assert "alice" in r2.response.lower()
    await engine.shutdown()


@pytest.mark.asyncio
async def test_usage_tracking(engine: AgentEngine) -> None:
    """用量统计应正确累加."""
    result = await engine.run("hello, just say hi back")
    assert result.status == "completed"
    assert result.usage.total_input_tokens > 0
    assert result.usage.turns >= 1
    await engine.shutdown()


@pytest.mark.asyncio
async def test_session_persistence(engine: AgentEngine) -> None:
    """会话应能持久化并恢复."""
    session = await engine.get_or_create_session(title="test session")
    assert session.title == "test session"
    assert session.id is not None

    await engine.run("记住数字42")
    await engine._save_session()

    loaded = await engine.load_session(session.id)
    assert loaded
    assert len(engine._messages) > 0
    await engine.shutdown()
