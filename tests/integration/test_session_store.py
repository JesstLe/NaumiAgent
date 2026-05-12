"""会话存储集成测试."""

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

    async def test_delete_session(self, store: SessionStore) -> None:
        session = Session(title="待删除")
        await store.save(session)

        deleted = await store.delete(session.id)
        assert deleted is True

        loaded = await store.load(session.id)
        assert loaded is None

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
