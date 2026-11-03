"""Persistent session ownership survives stale writes, duplication, and migration."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore

OWNER = "pa_" + "a" * 32
OTHER_OWNER = "pa_" + "b" * 32


def _store(tmp_path):
    return SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))


@pytest.mark.asyncio
async def test_new_store_round_trip_and_reopen_preserve_owner(tmp_path):
    store = _store(tmp_path)
    try:
        session = await store.create_session(title="我的专员", personal_agent_id=OWNER)
        session.add_message("user", "整理指定资料")
        await store.save(session)
        assert session.personal_agent_id == OWNER
        assert session.to_row()["personal_agent_id"] == OWNER
    finally:
        await store.close()
    reopened = _store(tmp_path)
    try:
        saved = await reopened.load(session.id)
        assert saved.personal_agent_id == OWNER
        assert saved.messages[-1]["content"] == "整理指定资料"
        listed, total = await reopened.list_sessions()
        assert total == 1 and listed[0].personal_agent_id == OWNER
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_legacy_database_additive_migration_preserves_data(tmp_path):
    path = tmp_path / "sessions.db"
    now = datetime.now().isoformat()
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, total_tokens INTEGER NOT NULL, total_cost_usd REAL NOT NULL
        )""")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "旧会话",
                "model",
                '[{"role":"user","content":"旧资料"}]',
                now,
                now,
                "active",
                7,
                0.2,
            ),
        )
    store = _store(tmp_path)
    try:
        old = await store.load("old")
        assert old.personal_agent_id == ""
        assert old.total_tokens == 7 and old.messages[0]["content"] == "旧资料"
        await store.save(old)
    finally:
        await store.close()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        assert "personal_agent_id" in columns
        assert db.execute("SELECT personal_agent_id FROM sessions WHERE id='old'").fetchone() == (
            "",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["", OTHER_OWNER])
async def test_existing_owner_is_sticky_on_stale_save(tmp_path, replacement):
    store = _store(tmp_path)
    try:
        session = await store.create_session(personal_agent_id=OWNER)
        stale = replace(session, personal_agent_id=replacement, title="后续消息")
        await store.save(stale)
        assert stale.personal_agent_id == OWNER
        saved = await store.load(session.id)
        assert saved.personal_agent_id == OWNER
        assert saved.title == "后续消息"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_writer_from_other_connection_cannot_clear_owner(tmp_path):
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        unowned = await first.create_session()
        stale = await second.load(unowned.id)
        unowned.personal_agent_id = OWNER
        await first.save(unowned)
        await second.save(stale)
        assert stale.personal_agent_id == OWNER
        assert (await first.load(unowned.id)).personal_agent_id == OWNER
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_concurrent_saved_owners_remain_original(tmp_path):
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        original = await first.create_session(personal_agent_id=OWNER)
        await second.load(original.id)
        stale, changed = (
            replace(original, personal_agent_id=""),
            replace(
                original,
                personal_agent_id=OTHER_OWNER,
            ),
        )
        await asyncio.gather(first.save(stale), second.save(changed))
        assert stale.personal_agent_id == changed.personal_agent_id == OWNER
        assert (await first.load(original.id)).personal_agent_id == OWNER
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_duplicate_and_archive_resume_preserve_marker(tmp_path):
    store = _store(tmp_path)
    try:
        original = await store.create_session(personal_agent_id=OWNER)
        original.add_message("user", "专员私有内容")
        await store.save(original)
        copied = await store.duplicate(original.id)
        assert copied.id != original.id and copied.personal_agent_id == OWNER
        assert (await store.load(copied.id)).personal_agent_id == OWNER
        copied.messages[0]["content"] = "副本独立编辑"
        assert (await store.load(original.id)).messages[0]["content"] == "专员私有内容"
        await store.archive(original.id)
        resumed = await store.resume(original.id)
        assert resumed.personal_agent_id == OWNER
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_plain_sessions_and_old_row_mapping_remain_compatible(tmp_path):
    original = Session(title="普通对话")
    row = original.to_row()
    row.pop("personal_agent_id")
    assert Session.from_row(row).personal_agent_id == ""
    store = _store(tmp_path)
    try:
        await store.save(original)
        assert (await store.load(original.id)).personal_agent_id == ""
        copied = await store.duplicate(original.id)
        assert copied.personal_agent_id == ""
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, 7, "has space", "tab\t", "x" * 129])
async def test_invalid_marker_is_rejected_without_writing(tmp_path, marker):
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="身份标记"):
            await store.save(Session(personal_agent_id=marker))
        assert not (tmp_path / "sessions.db").exists()
    finally:
        await store.close()
