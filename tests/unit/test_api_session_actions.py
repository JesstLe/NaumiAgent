"""Verify Web session actions against the durable SessionStore."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.messages import router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore


class _Engine:
    def __init__(self, store: SessionStore) -> None:
        self.session_store = store
        self._session = None

    async def archive_session(self, session_id: str) -> bool:
        return await self.session_store.archive(session_id)


@pytest.fixture
async def session_client(tmp_path):
    store = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    source = await store.create_session(title="原会话")
    source.add_message("user", "需要复制的历史")
    await store.save(source)
    app = FastAPI()
    app.include_router(router)
    app.state.engine = _Engine(store)
    app.state.engine_lock = asyncio.Lock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, store, source.id
    await store.close()


async def test_rename_pin_duplicate_and_archive_are_persistent(session_client):
    client, store, session_id = session_client

    renamed = await client.patch(f"/sessions/{session_id}", json={"title": "新名称"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新名称"

    pinned = await client.post(f"/sessions/{session_id}/pin", json={"pinned": True})
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True

    copied = await client.post(f"/sessions/{session_id}/duplicate")
    assert copied.status_code == 201
    duplicate = await store.load(copied.json()["id"])
    assert duplicate is not None
    assert duplicate.title == "新名称 副本"
    assert duplicate.messages[0]["content"] == "需要复制的历史"

    archived = await client.post(f"/sessions/{session_id}/archive")
    assert archived.status_code == 204
    restored = await store.load(session_id)
    assert restored is not None and restored.status == "archived"
    listing = (await client.get("/sessions")).json()
    assert session_id not in {item["id"] for item in listing["sessions"]}


async def test_archive_rejects_the_running_session(session_client):
    client, store, session_id = session_client
    active = await store.load(session_id)
    app = client._transport.app
    app.state.engine._session = active
    async with app.state.engine_lock:
        response = await client.post(f"/sessions/{session_id}/archive")
    assert response.status_code == 409
    assert "正在执行" in response.json()["detail"]
