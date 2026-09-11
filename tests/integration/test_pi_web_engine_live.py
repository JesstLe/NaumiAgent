"""Live integration: web API paths driving a real pi subprocess.

Skipped unless the pi binary (and a usable Zhipu gateway key) exist, so CI
without the external engine stays green.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from naumi_agent.api.routes import engines as engines_route
from naumi_agent.api.routes import messages as messages_route
from naumi_agent.memory.session import SessionStore
from naumi_agent.pi_engine.web_facade import PiWebEngine

pytestmark = pytest.mark.integration

_PI_BIN = shutil.which("pi")


def _zai_key() -> str:
    key = os.environ.get("ZAI_CODING_CN_API_KEY")
    if not key and "bigmodel" in os.environ.get("OPENAI_BASE_URL", ""):
        key = os.environ.get("OPENAI_API_KEY")
    return key or ""


def _live_config(tmp_path: Path):
    from naumi_agent.config.settings import AppConfig

    config = AppConfig()
    config.workspace_root = str(tmp_path)
    config.engine.provider = "pi"
    config.engine.pi.provider = "zai-coding-cn"
    config.engine.pi.model = "glm-4.7"
    config.memory.session_db_path = str(tmp_path / "sessions.db")
    return config


class _StoreEngine:
    """Minimal engine stand-in exposing the store surface routes consume."""

    def __init__(self, store: SessionStore) -> None:
        self.session_store = store

    async def create_session(self, **kwargs):
        return await self.session_store.create_session(**kwargs)


def _build_app(config) -> FastAPI:
    app = FastAPI()
    store = SessionStore(config.memory)
    app.state.config = config
    app.state.engine = _StoreEngine(store)
    app.state.chat_run_store = None
    app.include_router(engines_route.router)
    app.include_router(messages_route.router)
    return app


async def test_live_web_engine_conversation(tmp_path: Path) -> None:
    if _PI_BIN is None:
        pytest.skip("本机未安装 pi")
    if not _zai_key():
        pytest.skip("未检测到可用的智谱网关 Key")
    config = _live_config(tmp_path)
    os.environ.setdefault("ZAI_CODING_CN_API_KEY", _zai_key())
    app = _build_app(config)

    # ASGITransport keeps app and pi subprocess on this test's event loop.
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        engines = (await client.get("/engines")).json()
        assert engines["default"] == "pi"
        assert any(e["id"] == "pi" and e["available"] for e in engines["engines"])

        created = await client.post(
            "/sessions", json={"engine": "pi", "title": "live pi 对话"}
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        assert created.json()["engine"] == "pi"

        body = ""
        async with client.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            json={
                "content": "用 bash 执行 echo web-live-ok 然后只回复输出",
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_text():
                body += chunk

        assert "token_delta" in body
        assert "tool_call_start" in body
        assert "web-live-ok" in body
        assert "agent_end" in body

        history = (
            await client.get(f"/sessions/{session_id}/messages")
        ).json()
        roles = [m["role"] for m in history["messages"]]
        assert roles == ["user", "assistant"]
        assert "web-live-ok" in history["messages"][-1]["content"]

        facade = app.state.pi_web_engine
        assert isinstance(facade, PiWebEngine)
        await facade.stop()
