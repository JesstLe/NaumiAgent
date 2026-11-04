"""Compatibility port backed by the same scoped personal-memory authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from collections import Counter
from pathlib import Path

from naumi_agent.memory.long_term import MemoryEntry, MemorySearchResult, MemoryStats
from naumi_agent.personal.memory_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError


class ReadOnlyLegacyMemory:
    """Read the legacy SQLite metadata without opening a mutating Chroma client."""

    _client = None

    def __init__(self, config):
        self.path = Path(config.vector_db_path).expanduser().absolute() / "chroma.sqlite3"

    def _export(self):
        for path in [*reversed(self.path.parents), self.path]:
            if not path.exists():
                return "[]"
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PersonalAgentError("legacy_unavailable", "旧记忆路径存在链接别名")
            if path == self.path and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
                raise PersonalAgentError("legacy_unavailable", "旧记忆资料库不是独立文件")
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT c.id FROM collections c JOIN databases d ON c.database_id=d.id "
                "WHERE c.name=? AND d.name=? AND d.tenant_id=?",
                ("naumi_memory", "default_database", "default_tenant"),
            ).fetchall()
            if not rows:
                return "[]"
            if len(rows) != 1:
                raise PersonalAgentError("legacy_unavailable", "旧记忆集合身份不唯一")
            cursor = connection.execute(
                "SELECT e.id,e.embedding_id,m.key,m.string_value,m.int_value,m.float_value "
                "FROM embeddings e JOIN segments s ON e.segment_id=s.id "
                "LEFT JOIN embedding_metadata m ON m.id=e.id "
                "WHERE s.collection=? AND s.scope=? ORDER BY e.id,m.key",
                (rows[0][0], "METADATA"),
            )
            entries = {}
            for row in cursor:
                if len(entries) > 100000:
                    raise PersonalAgentError("legacy_unavailable", "旧记忆超过本次读取上限")
                value = entries.setdefault(
                    row[0], {"id": row[1], "content": "", "category": "fact"}
                )
                if row[2] == "chroma:document":
                    value["content"] = row[3] or ""
                elif row[2] == "category" and isinstance(row[3], str):
                    value["category"] = row[3]
            assert connection.total_changes == 0
            return json.dumps(list(entries.values()), ensure_ascii=False)
        except sqlite3.Error as exc:
            raise PersonalAgentError(
                "legacy_unavailable",
                "旧记忆结构不兼容或资料库不可读，原资料库未作升级",
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    async def export_memories(self):
        return await asyncio.to_thread(self._export)


class ScopedMemoryAdapter:
    """Old callers may propose/search, but never write confirmed global Chroma data."""

    def __init__(self, service, runtime):
        self.service, self.runtime = service, runtime

    def _actor(self):
        return self.runtime.actor()

    @staticmethod
    def _entry(value):
        return MemoryEntry(
            id=value["memory_id"],
            content=value["content"],
            category=value["category"],
            metadata={
                "personal_memory_ref": value["ref"],
                "scope": value["scope"],
                "source": value.get("source"),
                "confirmation": value.get("confirmation"),
            },
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            status=value["state"],
        )

    async def store(self, entry):
        if not self.runtime.enabled:
            raise PersonalAgentError("memory_disabled", "长期记忆已关闭")
        from naumi_agent.personal.runtime import current_run_id

        engine = self.service.engine
        session = getattr(engine, "_session", None)
        run = current_run_id(engine, session.id) if session is not None else ""
        if not run:
            raise PersonalAgentError("scope_denied", "记忆提取需要实际运行来源")
        request = (
            "compat-memory-"
            + digest(
                {
                    "run": run,
                    "content": entry.content,
                    "category": entry.category,
                    "source": entry.metadata.get("source", "unknown"),
                }
            )[:40]
        )
        value = await self.service.propose_compaction(
            entry.content,
            entry.category,
            request_id=request,
        )
        return value["memory_id"]

    async def recall(self, query, top_k=5, category=None):
        return await self._recall(query, top_k=top_k, category=category)

    async def recall_for_session(self, query, *, session_id, top_k=5, min_relevance=0.0):
        session = getattr(self.service.engine, "_session", None)
        if session is None or session_id != session.id:
            raise PersonalAgentError("scope_denied", "记忆查询与当前会话不一致")
        return await self._recall(query, top_k=top_k, min_relevance=min_relevance)

    async def _recall(self, query, *, top_k=5, category=None, min_relevance=0.0):
        if not self.runtime.enabled:
            return []
        result = await self.service.search(
            query,
            actor_id=self._actor(),
            item_id=self.runtime.item_id(),
            limit=min(20, max(1, top_k)),
            category=category,
        )
        # Hybrid rank is not a calibrated semantic probability.
        return [
            MemorySearchResult(self._entry(v), v["retrieval"].get("semantic_similarity") or 0.0)
            for v in result["memories"]
            if (v["retrieval"].get("semantic_similarity") or 0.0) >= min_relevance
        ]

    async def search(self, query="", *, category=None, status=None, limit=50, **kwargs):
        if not self.runtime.enabled:
            return []
        if kwargs or status not in (None, "active"):
            raise PersonalAgentError("invalid_input", "请通过记忆管理查看候选与历史状态")
        if query:
            return [
                r.entry for r in await self._recall(query, top_k=min(limit, 20), category=category)
            ]
        values, _ = await self.service._eligible(
            self._actor(), item_id=self.runtime.item_id(), category=category
        )
        return [
            self._entry(await self.service.get(v["memory_id"], actor_id=self._actor()))
            for v in values[:limit]
        ]

    async def count(self):
        if not self.runtime.enabled:
            return 0
        values, _ = await self.service._eligible(self._actor(), item_id=self.runtime.item_id())
        return len(values)

    async def stats(self):
        if not self.runtime.enabled:
            return MemoryStats()
        values, _ = await self.service._eligible(self._actor(), item_id=self.runtime.item_id())
        return MemoryStats(
            total=len(values),
            active=len(values),
            by_category=dict(Counter(v["category"] for v in values)),
        )

    async def export_memories(self):
        if not self.runtime.enabled:
            return canonical({"memories": []})
        return canonical(
            await self.service.export(actor_id=self._actor(), item_id=self.runtime.item_id())
        )

    async def delete(self, memory_id):
        raise PersonalAgentError("human_confirmation_required", "请在记忆管理中明确删除当前版本")

    async def forget_old(self, **kwargs):
        # Confirmed user facts are never deleted by an age/access heuristic.
        return 0

    async def consolidate(self):
        if not self.runtime.enabled:
            return {"merged": 0, "deleted": 0, "indexed": 0}
        result = await self.service.sync_index()
        return {"merged": 0, "deleted": 0, "indexed": result["processed"]}
