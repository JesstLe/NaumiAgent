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
    config = AppConfig.from_yaml("config.yaml")
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
