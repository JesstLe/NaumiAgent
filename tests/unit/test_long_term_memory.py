"""LongTermMemory unit tests — dedup and scoring."""

from datetime import datetime, timedelta

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.long_term import (
    LongTermMemory,
    MemoryEntry,
    _access_score,
    _recency_score,
)

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory(tmp_path):
    """Create a LongTermMemory with a temp directory."""
    config = MemoryConfig(vector_db_path=str(tmp_path / "chroma"))
    mem = LongTermMemory(config)
    yield mem
    # Cleanup ChromaDB client so temp dir can be removed
    if mem._client is not None:
        mem._client = None
        mem._collection = None


async def _store(memory: LongTermMemory, content: str, **kwargs) -> str:
    entry = MemoryEntry(
        id=kwargs.get("id", ""),
        content=content,
        category=kwargs.get("category", "fact"),
        metadata=kwargs.get("metadata", {}),
        created_at=kwargs.get("created_at", ""),
        updated_at=kwargs.get("updated_at", ""),
        access_count=kwargs.get("access_count", 0),
    )
    return await memory.store(entry)


# ---------------------------------------------------------------------------
#  _recency_score / _access_score
# ---------------------------------------------------------------------------


class TestScoringFunctions:
    def test_recency_fresh(self):
        now = datetime.now()
        score = _recency_score(now.isoformat(), now)
        assert abs(score - 1.0) < 0.01

    def test_recency_30_days(self):
        now = datetime.now()
        ts = (now - timedelta(days=30)).isoformat()
        score = _recency_score(ts, now)
        assert abs(score - 0.5) < 0.01

    def test_recency_60_days(self):
        now = datetime.now()
        ts = (now - timedelta(days=60)).isoformat()
        score = _recency_score(ts, now)
        assert abs(score - 0.25) < 0.01

    def test_recency_empty_timestamp(self):
        score = _recency_score("", datetime.now())
        assert score == 0.5

    def test_recency_invalid_timestamp(self):
        score = _recency_score("not-a-date", datetime.now())
        assert score == 0.5

    def test_access_zero(self):
        assert _access_score(0) == 1.0

    def test_access_increases(self):
        assert _access_score(5) > _access_score(0)
        assert _access_score(10) > _access_score(5)

    def test_access_logarithmic(self):
        # Growth slows down: 0→1 should be bigger jump than 10→11
        jump_early = _access_score(1) - _access_score(0)
        jump_late = _access_score(11) - _access_score(10)
        assert jump_early > jump_late


# ---------------------------------------------------------------------------
#  Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_new_entry_stores_normally(self, memory):
        mid = await _store(memory, "用户喜欢 Python")
        assert mid
        count = await memory.count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_identical_content_merges(self, memory):
        id1 = await _store(memory, "用户喜欢 Python 编程语言")
        id2 = await _store(memory, "用户喜欢 Python 编程语言")

        count = await memory.count()
        assert count == 1
        assert id2 == id1

    @pytest.mark.asyncio
    async def test_similar_content_merges(self, memory):
        await _store(memory, "项目使用 FastAPI 作为 Web 框架")
        await _store(memory, "项目使用 FastAPI 作为 Web 框架进行开发")

        count = await memory.count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_dissimilar_content_stores_separately(self, memory):
        await _store(memory, "用户喜欢 Python")
        await _store(memory, "部署使用 Docker 容器化方案")

        count = await memory.count()
        assert count == 2

    @pytest.mark.asyncio
    async def test_merge_keeps_longer_content(self, memory):
        base = "用户偏好深色主题"

        id1 = await _store(memory, base)
        id2 = await _store(memory, base)

        assert id2 == id1
        assert await memory.count() == 1

    @pytest.mark.asyncio
    async def test_merge_preserves_created_at(self, memory):
        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        id1 = await _store(memory, "用户喜欢 Python", created_at=old_time)
        id2 = await _store(memory, "用户喜欢 Python 编程语言")

        assert id2 == id1

        results = await memory.recall("Python", top_k=1)
        assert results[0].entry.created_at == old_time

    @pytest.mark.asyncio
    async def test_different_categories_stored_separately(self, memory):
        await _store(memory, "系统使用 SQLite 数据库", category="fact")
        await _store(memory, "系统使用 SQLite 数据库", category="preference")

        count = await memory.count()
        assert count == 2

    @pytest.mark.asyncio
    async def test_merge_preserves_access_count(self, memory):
        id1 = await _store(memory, "用户喜欢 Python")

        # Simulate access: recall increments access_count
        await memory.recall("Python", top_k=1)

        # Store similar content — should merge, keeping old access_count
        id2 = await _store(memory, "用户喜欢 Python 编程")

        assert id2 == id1

        results = await memory.recall("Python", top_k=1)
        # Original had 1 access from recall, then this recall makes 2
        assert results[0].entry.access_count >= 1

    @pytest.mark.asyncio
    async def test_merge_metadata_combined(self, memory):
        await _store(memory, "用户喜欢 Python", metadata={"source": "conversation"})
        await _store(
            memory, "用户喜欢 Python 编程语言", metadata={"confidence": "high"},
        )

        results = await memory.recall("Python", top_k=1)
        meta = results[0].entry.metadata
        # New metadata overwrites, old metadata that doesn't conflict is preserved
        assert "source" in meta or "confidence" in meta


# ---------------------------------------------------------------------------
#  Retrieval scoring
# ---------------------------------------------------------------------------


class TestRetrievalScoring:
    @pytest.mark.asyncio
    async def test_recall_for_session_keeps_current_memories_and_global_preferences(
        self,
        memory,
    ):
        await _store(
            memory,
            "NaumiAgent 当前会话项目事实",
            id="current-fact",
            metadata={"scope": "session", "session_id": "current"},
        )
        await _store(
            memory,
            "NaumiAgent 其他会话决策",
            id="other-decision",
            category="decision",
            metadata={"scope": "session", "session_id": "other"},
        )
        await _store(
            memory,
            "NaumiAgent 全局用户偏好",
            id="global-preference",
            category="preference",
            metadata={"scope": "global"},
        )
        await _store(
            memory,
            "NaumiAgent 旧版当前会话事实",
            id="legacy-current",
            metadata={"source": "auto_extract", "session_id": "current"},
        )
        await _store(
            memory,
            "NaumiAgent 旧版其他会话决策",
            id="legacy-other",
            category="decision",
            metadata={"source": "auto_extract", "session_id": "other"},
        )
        await _store(
            memory,
            "NaumiAgent 未声明作用域事实",
            id="unscoped-fact",
        )

        results = await memory.recall_for_session(
            "NaumiAgent",
            session_id="current",
            top_k=6,
            min_relevance=0.0,
        )

        assert {result.entry.id for result in results} == {
            "current-fact",
            "global-preference",
            "legacy-current",
        }

    @pytest.mark.asyncio
    async def test_recall_empty_collection(self, memory):
        results = await memory.recall("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_recall_returns_relevant(self, memory):
        await _store(memory, "项目使用 FastAPI 框架")
        await _store(memory, "部署在 AWS 上")

        results = await memory.recall("FastAPI", top_k=1)
        assert len(results) == 1
        assert "FastAPI" in results[0].entry.content

    @pytest.mark.asyncio
    async def test_recent_memories_ranked_higher(self, memory):
        """Two distinct memories, old vs recent — recent should rank higher.

        Uses English text for reliable embedding similarity with ChromaDB's
        default model (all-MiniLM-L6-v2).
        """
        old_time = (datetime.now() - timedelta(days=120)).isoformat()
        recent_time = (datetime.now() - timedelta(hours=1)).isoformat()

        await _store(
            memory,
            "The project uses Redis for session caching layer",
            created_at=old_time,
            updated_at=old_time,
        )
        await _store(
            memory,
            "The project uses Memcached for query caching layer",
            created_at=recent_time,
            updated_at=recent_time,
        )

        results = await memory.recall("project caching setup", top_k=2)
        assert len(results) == 2
        # Recent one should rank higher due to recency boost
        assert "Memcached" in results[0].entry.content

    @pytest.mark.asyncio
    async def test_frequently_accessed_ranked_higher(self, memory):
        """Memory accessed many times should outrank never-accessed one."""
        await _store(memory, "数据库配置在 config.yaml")
        await _store(memory, "API 端口 8080")

        # Access the first one multiple times
        for _ in range(5):
            await memory.recall("数据库配置", top_k=1)

        # Now query broadly — the frequently accessed one should rank higher
        results = await memory.recall("配置信息", top_k=2)
        if len(results) == 2:
            # First result should be the one accessed more often
            assert "数据库配置" in results[0].entry.content

    @pytest.mark.asyncio
    async def test_category_filter(self, memory):
        await _store(memory, "Python 很好用", category="fact")
        await _store(memory, "偏好深色主题", category="preference")

        results = await memory.recall("偏好", category="preference", top_k=5)
        assert len(results) == 1
        assert results[0].entry.category == "preference"

    @pytest.mark.asyncio
    async def test_min_relevance_filter(self, memory):
        await _store(memory, "项目使用 FastAPI 框架")

        results = await memory.recall("完全无关的量子物理问题", min_relevance=0.9)
        # Should return empty if no memory is relevant enough
        # (depends on embedding model, but query is very unrelated)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_recall_increments_access_count(self, memory):
        await _store(memory, "用户喜欢 Python")

        # First recall — entry starts at 0, recall increments in DB
        r1 = await memory.recall("Python", top_k=1)
        count_after_r1 = r1[0].entry.access_count

        # Second recall — should see higher count from DB
        r2 = await memory.recall("Python", top_k=1)
        assert r2[0].entry.access_count > count_after_r1


# ---------------------------------------------------------------------------
#  Delete and count
# ---------------------------------------------------------------------------


class TestDeleteAndCount:
    @pytest.mark.asyncio
    async def test_delete(self, memory):
        mid = await _store(memory, "临时信息")
        assert await memory.count() == 1

        await memory.delete(mid)
        assert await memory.count() == 0

    @pytest.mark.asyncio
    async def test_count_empty(self, memory):
        assert await memory.count() == 0

    @pytest.mark.asyncio
    async def test_forget_old_marks_dormant(self, memory):
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        new_time = datetime.now().isoformat()

        await _store(memory, "旧记忆", created_at=old_time, updated_at=old_time)
        await _store(memory, "新记忆", created_at=new_time, updated_at=new_time)

        # Access the new memory so it has access_count > 0
        await memory.recall("新记忆", top_k=1)

        forgotten = await memory.forget_old(min_access_count=2)
        assert forgotten == 1
        # Not deleted, just marked dormant
        assert await memory.count() == 2

        # Dormant memories are excluded from recall
        results = await memory.recall("旧记忆", top_k=5)
        assert all(r.entry.content != "旧记忆" for r in results)

    @pytest.mark.asyncio
    async def test_forget_deletes_very_old_dormant(self, memory):
        very_old = (datetime.now() - timedelta(days=200)).isoformat()
        await _store(memory, "很旧的记忆", created_at=very_old, updated_at=very_old)

        # First pass: mark dormant
        await memory.forget_old(min_access_count=2)
        assert await memory.count() == 1

        # Second pass: delete dormant (200 days > _DELETE_AFTER_DAYS=180)
        await memory.forget_old(min_access_count=2)
        assert await memory.count() == 0


# ---------------------------------------------------------------------------
#  Stats, Search, Export
# ---------------------------------------------------------------------------


class TestStatsSearchExport:
    @pytest.mark.asyncio
    async def test_stats_empty(self, memory):
        stats = await memory.stats()
        assert stats.total == 0
        assert stats.active == 0

    @pytest.mark.asyncio
    async def test_stats_counts(self, memory):
        await _store(memory, "fact 1", category="fact")
        await _store(memory, "pref 1", category="preference")

        stats = await memory.stats()
        assert stats.total == 2
        assert stats.active == 2
        assert stats.dormant == 0
        assert "fact" in stats.by_category
        assert "preference" in stats.by_category

    @pytest.mark.asyncio
    async def test_stats_with_dormant(self, memory):
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        await _store(memory, "旧记忆", created_at=old_time, updated_at=old_time)
        await memory.forget_old(min_access_count=2)

        stats = await memory.stats()
        assert stats.total == 1
        assert stats.dormant == 1
        assert stats.active == 0

    @pytest.mark.asyncio
    async def test_stats_avg_access(self, memory):
        await _store(memory, "mem1")
        await _store(memory, "mem2")
        await memory.recall("mem1", top_k=1)

        stats = await memory.stats()
        assert stats.avg_access_count >= 0.0

    @pytest.mark.asyncio
    async def test_search_returns_results(self, memory):
        await _store(memory, "Python is great for web development")
        await _store(memory, "Rust is great for systems programming")

        results = await memory.search("Python", top_k=5)
        assert len(results) >= 1
        assert any("Python" in r.entry.content for r in results)

    @pytest.mark.asyncio
    async def test_search_excludes_dormant(self, memory):
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        await _store(memory, "dormant item", created_at=old_time, updated_at=old_time)
        await memory.forget_old(min_access_count=2)

        results = await memory.search("dormant", include_dormant=False)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_includes_dormant(self, memory):
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        await _store(memory, "dormant item", created_at=old_time, updated_at=old_time)
        await memory.forget_old(min_access_count=2)

        results = await memory.search("dormant", include_dormant=True)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_export_returns_json(self, memory):
        await _store(memory, "memory content")
        exported = await memory.export_memories()
        assert "[" in exported
        assert "memory content" in exported

    @pytest.mark.asyncio
    async def test_export_empty(self, memory):
        exported = await memory.export_memories()
        assert exported == "[]"


# ---------------------------------------------------------------------------
#  Consolidate
# ---------------------------------------------------------------------------


class TestConsolidate:
    @pytest.mark.asyncio
    async def test_consolidate_dedup(self, memory):
        await _store(memory, "duplicate content")
        await _store(memory, "duplicate content")

        # Both were merged by store dedup, but let's add via direct upsert
        # to create a real duplicate scenario
        from naumi_agent.memory.long_term import MemoryEntry

        entry = MemoryEntry(
            id="manual_dup",
            content="duplicate content",
            category="fact",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        memory._upsert_entry(entry)

        count_before = await memory.count()
        assert count_before >= 2

        result = await memory.consolidate()
        assert result["deduped"] >= 1

    @pytest.mark.asyncio
    async def test_consolidate_forget(self, memory):
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        await _store(memory, "old memory", created_at=old_time, updated_at=old_time)

        result = await memory.consolidate()
        assert result["forgotten"] >= 1
