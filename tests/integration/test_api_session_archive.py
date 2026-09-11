"""Archive route regression with a real engine and SQLite persistence."""

import asyncio

import httpx
import pytest

from naumi_agent.api.app import create_app
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.runtime.composition import create_agent_engine


@pytest.mark.asyncio
async def test_archive_route_persistence_missing_and_busy(tmp_path):
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
    )
    engine = create_agent_engine(config)
    app = create_app()
    app.state.engine = engine
    app.state.config = config
    app.state.engine_lock = asyncio.Lock()
    reopened = SessionStore(config.memory)
    try:
        current = await engine.get_or_create_session(title="归档回归验收")
        current.add_message("user", "归档后仍需保留的原始内容")
        await engine.session_store.save(current)
        original_messages = list(current.messages)
        other = await engine.session_store.create_session(title="保留会话")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            endpoint = f"/api/v1/sessions/{current.id}/archive"
            async with app.state.engine_lock:
                response = await client.post(endpoint)
                assert response.status_code == 409
                assert (await reopened.load(current.id)).status == "active"
            response = await client.post(endpoint)
            assert response.status_code == 204
            assert response.content == b""
            assert (await client.post(endpoint)).status_code == 204
            missing = await client.post("/api/v1/sessions/missing/archive")
            assert missing.status_code == 404
            assert missing.json()["detail"] == "Session not found"
            listed = (await client.get("/api/v1/sessions")).json()
            assert [s["id"] for s in listed["sessions"]] == [other.id]
            persisted = await reopened.load(current.id)
            assert persisted.status == "archived"
            assert persisted.archived_at is not None
            assert persisted.messages == original_messages
            assert engine._session.status == "archived"
    finally:
        await reopened.close()
        await engine.shutdown()
