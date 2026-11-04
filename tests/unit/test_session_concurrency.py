"""Regression coverage for atomic session metadata and concurrent persistence."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from naumi_agent.api.routes.messages import update_session
from naumi_agent.api.schemas import SessionUpdate
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore


@pytest.fixture
async def store(tmp_path):
    instance = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    yield instance
    await instance.close()


async def test_parallel_first_use_creates_one_connection(store):
    connections = await asyncio.gather(*(store._get_db() for _ in range(8)))
    assert len({id(connection) for connection in connections}) == 1


async def test_parallel_save_resume_and_pin_are_atomic(store):
    first = await store.create_session(title="first")
    second = await store.create_session(title="second")
    for index in range(20):
        second.add_message("assistant", str(index))
        await asyncio.gather(store.save(second), store.resume(first.id),
                             store.set_pinned(first.id, bool(index % 2)))
    loaded = await store.load(second.id)
    assert len(loaded.messages) == 20


async def test_metadata_update_preserves_latest_reply_and_usage(store):
    session = await store.create_session(title="before", engine="pi")
    session.add_message("assistant", "durable reply")
    session.total_tokens = 123
    await store.save(session)
    updated = await store.update_metadata(session.id, title="renamed")
    assert updated.title == "renamed"
    assert updated.messages[-1]["content"] == "durable reply"
    assert updated.total_tokens == 123
    assert updated.engine == "pi"
    assert await store.update_metadata("missing", title="no insert") is None


async def test_metadata_system_prompt_keeps_latest_messages(store):
    session = await store.create_session(system_prompt="old")
    session.add_message("assistant", "answer")
    await store.save(session)
    updated = await store.update_metadata(session.id, system_prompt="new", model="new-model")
    assert [m["content"] for m in updated.messages] == ["new", "answer"]
    assert updated.model == "new-model"


async def test_api_rename_cannot_overwrite_reply_read_after_load(store, monkeypatch):
    session = await store.create_session(title="before")
    engine = SimpleNamespace(session_store=store, _session=session)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
    original_load = store.load
    injected = False

    async def load_then_complete(session_id):
        nonlocal injected
        snapshot = await original_load(session_id)
        if not injected:
            injected = True
            session.add_message("assistant", "completed while rename was loading")
            await store.save(session)
        return snapshot

    monkeypatch.setattr(store, "load", load_then_complete)
    await update_session(session.id, SessionUpdate(title="renamed"), request, "test")
    saved = await original_load(session.id)
    assert saved.title == "renamed"
    assert saved.messages[-1]["content"] == "completed while rename was loading"


@pytest.mark.parametrize("engine", ["pi", "naumi"])
async def test_duplicate_preserves_engine_and_independent_messages(store, engine):
    original = await store.create_session(engine=engine)
    original.add_message("assistant", "first")
    await store.save(original)
    duplicate = await store.duplicate(original.id)
    assert duplicate.engine == engine
    assert duplicate.id != original.id
    duplicate.add_message("user", "independent")
    await store.save(duplicate)
    assert len((await store.load(original.id)).messages) == 1


async def test_cancelled_save_rolls_back_before_next_operation(store, monkeypatch):
    session = await store.create_session(title="old")
    db = await store._get_db()
    original_commit = db.commit

    async def cancel_commit():
        raise asyncio.CancelledError

    session.title = "must roll back"
    monkeypatch.setattr(db, "commit", cancel_commit)
    with pytest.raises(asyncio.CancelledError):
        await store.save(session)
    monkeypatch.setattr(db, "commit", original_commit)
    loaded = await store.resume(session.id)
    assert loaded.title == "old"


async def test_separate_stores_migrate_one_legacy_database_atomically(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, total_tokens INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0
        )""")
    stores = [SessionStore(MemoryConfig(session_db_path=str(database))) for _ in range(6)]
    try:
        sessions = await asyncio.gather(*(s.create_session(title=str(i))
                                          for i, s in enumerate(stores)))
        for s, session in zip(stores, sessions, strict=True):
            assert (await s.load(session.id)).title == session.title
        assert (await stores[0].list_sessions())[1] == 6
    finally:
        await asyncio.gather(*(s.close() for s in stores))
