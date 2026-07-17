"""会话存储集成测试."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore


@pytest.fixture
async def store(tmp_path) -> SessionStore:
    config = MemoryConfig(session_db_path=str(tmp_path / "test_sessions.db"))
    s = SessionStore(config)
    yield s
    await s.close()


class TestSessionStore:
    async def test_save_and_load(self, store: SessionStore) -> None:
        session = Session(title="测试会话", model="claude-sonnet-4-6")
        session.add_message("user", "你好")
        session.add_message("assistant", "你好！有什么可以帮你？")

        await store.save(session)

        loaded = await store.load(session.id)
        assert loaded is not None
        assert loaded.title == "测试会话"
        assert len(loaded.messages) == 2
        assert loaded.messages[0]["role"] == "user"
        assert loaded.messages[1]["content"] == "你好！有什么可以帮你？"

    async def test_load_nonexistent(self, store: SessionStore) -> None:
        result = await store.load("nonexistent")
        assert result is None

    async def test_list_sessions(self, store: SessionStore) -> None:
        for i in range(5):
            s = Session(title=f"会话 {i}")
            await store.save(s)

        sessions, total = await store.list_sessions(page=1, page_size=3)
        assert total == 5
        assert len(sessions) == 3

        sessions2, _ = await store.list_sessions(page=2, page_size=3)
        assert len(sessions2) == 2

    async def test_search_and_archive_sessions(self, store: SessionStore) -> None:
        keep = Session(title="保留会话", workspace_root="/workspace/keep", git_branch="main")
        keep.summary = "包含关键搜索词"
        archive = Session(title="归档会话", workspace_root="/workspace/archive")
        await store.save(keep)
        await store.save(archive)

        matches, total = await store.list_sessions(query="关键搜索词")
        assert total == 1
        assert matches[0].id == keep.id
        assert matches[0].workspace_root == "/workspace/keep"
        assert matches[0].git_branch == "main"

        archived = await store.archive(keep.id)
        matches_after_archive, total_after_archive = await store.list_sessions(query="关键搜索词")

        assert archived is True
        assert matches_after_archive == []
        assert total_after_archive == 0

    async def test_delete_session(self, store: SessionStore) -> None:
        session = Session(title="待删除")
        await store.save(session)

        deleted = await store.delete(session.id)
        assert deleted is True

        loaded = await store.load(session.id)
        assert loaded is None

    async def test_retention_delete_is_atomic_and_rejects_active_session(
        self, store: SessionStore
    ) -> None:
        active = Session(id="active")
        archived = Session(id="archived", status="archived")
        await store.save(active)
        await store.save(archived)

        assert await store.delete_if_archived(active.id) is False
        assert await store.delete_if_archived(archived.id) is True
        assert await store.load(active.id) is not None
        assert await store.load(archived.id) is None

    async def test_update_session(self, store: SessionStore) -> None:
        session = Session(title="原始")
        await store.save(session)

        session.title = "更新后"
        session.add_message("user", "新消息")
        await store.save(session)

        loaded = await store.load(session.id)
        assert loaded is not None
        assert loaded.title == "更新后"
        assert len(loaded.messages) == 1

    async def test_session_immutability(self) -> None:
        session = Session(title="原始")
        old_messages = session.messages
        session.add_message("user", "新消息")

        assert len(old_messages) == 0  # 旧列表未被修改
        assert len(session.messages) == 1

    async def test_legacy_schema_adds_retention_timestamps_without_data_loss(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "legacy.db"
        old = (datetime.now() - timedelta(days=40)).isoformat()
        connection = sqlite3.connect(db_path)
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
                messages TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, status TEXT NOT NULL,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "旧会话", "test", "[]", old, old, "archived", 0, 0.0),
        )
        connection.commit()
        connection.close()

        legacy_store = SessionStore(MemoryConfig(session_db_path=str(db_path)))
        try:
            loaded = await legacy_store.load("legacy")
            assert loaded is not None
            assert loaded.last_accessed_at.isoformat() == old
            assert loaded.archived_at is not None
            assert loaded.archived_at.isoformat() == old
        finally:
            await legacy_store.close()

    async def test_resume_reactivates_and_updates_access_atomically(
        self, store: SessionStore
    ) -> None:
        old = datetime.now() - timedelta(days=60)
        session = Session(
            title="恢复会话",
            status="archived",
            updated_at=old,
            last_accessed_at=old,
            archived_at=old,
        )
        await store.save(session)

        resumed = await store.resume(session.id, accessed_at=datetime.now())
        persisted = await store.load(session.id)

        assert resumed is not None
        assert persisted is not None
        assert resumed.status == persisted.status == "active"
        assert resumed.archived_at is None
        assert resumed.last_accessed_at > old

    async def test_stale_active_save_cannot_undo_an_archive(
        self, store: SessionStore
    ) -> None:
        session = Session(title="并发归档保护")
        await store.save(session)
        stale = await store.load(session.id)
        assert stale is not None
        assert await store.archive(session.id)

        stale.title = "归档后迟到的保存"
        await store.save(stale)
        persisted = await store.load(session.id)

        assert persisted is not None
        assert persisted.status == "archived"
        assert persisted.archived_at is not None

    async def test_retention_scan_is_bounded_stable_and_does_not_decode_messages(
        self, store: SessionStore
    ) -> None:
        old = datetime.now() - timedelta(days=90)
        for session_id in ("b", "a", "c"):
            session = Session(
                id=session_id,
                title=session_id,
                status="archived",
                updated_at=old,
                last_accessed_at=old,
                archived_at=old,
            )
            await store.save(session)
        db = await store._get_db()
        await db.execute("UPDATE sessions SET messages = '{broken-json}' WHERE id = 'a'")
        await db.commit()

        scan = await store.scan_retention_candidates(limit=2)

        assert [item.session_id for item in scan.candidates] == ["a", "b"]
        assert scan.total_archived_count == 3
        assert scan.scanned_count == 2
        assert scan.scan_truncated is True
        assert all(item.payload_bytes > 0 for item in scan.candidates)

    async def test_retention_scan_is_read_only(self, store: SessionStore) -> None:
        old = datetime.now() - timedelta(days=90)
        session = Session(
            title="只读预览",
            status="archived",
            updated_at=old,
            last_accessed_at=old,
            archived_at=old,
        )
        await store.save(session)
        before = await store.load(session.id)

        await store.scan_retention_candidates(limit=10)
        after = await store.load(session.id)

        assert before is not None and after is not None
        assert after.to_row() == before.to_row()

    async def test_retention_scan_orders_by_more_recent_archive_timestamp(
        self, store: SessionStore
    ) -> None:
        old = datetime.now() - timedelta(days=90)
        recent = datetime.now() - timedelta(days=1)
        recently_archived = Session(
            id="recent-archive",
            status="archived",
            updated_at=old,
            last_accessed_at=old,
            archived_at=recent,
        )
        genuinely_old = Session(
            id="genuinely-old",
            status="archived",
            updated_at=old + timedelta(days=1),
            last_accessed_at=old + timedelta(days=1),
            archived_at=old + timedelta(days=1),
        )
        await store.save(recently_archived)
        await store.save(genuinely_old)

        scan = await store.scan_retention_candidates(limit=1)

        assert [item.session_id for item in scan.candidates] == ["genuinely-old"]
