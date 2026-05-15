"""长期记忆 — ChromaDB 向量存储."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from naumi_agent.config.settings import MemoryConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryEntry:
    """一条记忆记录."""

    id: str
    content: str
    category: str  # "fact" | "preference" | "experience" | "plan_template"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    access_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "access_count": self.access_count,
        }


@dataclass(frozen=True)
class MemorySearchResult:
    """记忆搜索结果."""

    entry: MemoryEntry
    relevance: float  # 0.0 - 1.0


class LongTermMemory:
    """基于 ChromaDB 的长期向量记忆."""

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config
        self._client: Any = None
        self._collection: Any = None

    def _ensure_initialized(self) -> None:
        if self._client is not None:
            return

        import chromadb

        os.makedirs(os.path.dirname(self._config.vector_db_path) or ".", exist_ok=True)
        self._client = chromadb.PersistentClient(path=self._config.vector_db_path)
        self._collection = self._client.get_or_create_collection(
            name="naumi_memory",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB initialized at %s", self._config.vector_db_path)

    async def store(self, entry: MemoryEntry) -> str:
        """存储一条记忆."""
        self._ensure_initialized()

        if not entry.id:
            entry = MemoryEntry(
                id=uuid.uuid4().hex[:12],
                content=entry.content,
                category=entry.category,
                metadata=entry.metadata,
                created_at=entry.created_at or datetime.now().isoformat(),
                access_count=entry.access_count,
            )

        meta = {
            "category": entry.category,
            "created_at": entry.created_at or datetime.now().isoformat(),
            "access_count": str(entry.access_count),
        }
        meta.update({k: str(v) for k, v in entry.metadata.items()})

        self._collection.upsert(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[meta],
        )
        return entry.id

    async def recall(
        self,
        query: str,
        *,
        category: str | None = None,
        top_k: int = 5,
        min_relevance: float = 0.5,
    ) -> list[MemorySearchResult]:
        """召回相关记忆."""
        self._ensure_initialized()

        where_filter = {"category": category} if category else None

        results = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, self._collection.count() or 1),
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        entries: list[MemorySearchResult] = []
        for i, doc_id in enumerate(results["ids"][0]):
            doc = results["documents"][0][i] if results["documents"] else ""
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            distance = results["distances"][0][i] if results["distances"] else 1.0

            # cosine distance → relevance (1 - distance)
            relevance = round(1.0 - distance, 4)
            if relevance < min_relevance:
                continue

            clean_meta = {
                k: v for k, v in meta.items() if k not in ("category", "created_at", "access_count")
            }

            entry = MemoryEntry(
                id=doc_id,
                content=doc,
                category=meta.get("category", "unknown"),
                metadata=clean_meta,
                created_at=meta.get("created_at", ""),
                access_count=int(meta.get("access_count", "0")),
            )
            entries.append(MemorySearchResult(entry=entry, relevance=relevance))

        # 增加访问计数
        for r in entries:
            new_count = r.entry.access_count + 1
            updated_meta = {
                "category": r.entry.category,
                "created_at": r.entry.created_at,
                "access_count": str(new_count),
            }
            updated_meta.update({k: str(v) for k, v in r.entry.metadata.items()})
            self._collection.update(
                ids=[r.entry.id],
                metadatas=[updated_meta],
            )

        return entries

    async def delete(self, memory_id: str) -> None:
        """删除一条记忆."""
        self._ensure_initialized()
        self._collection.delete(ids=[memory_id])

    async def count(self) -> int:
        """返回记忆总数."""
        self._ensure_initialized()
        return self._collection.count() or 0

    async def forget_old(self, max_age_days: int = 90, min_access_count: int = 1) -> int:
        """遗忘长期未访问且低频的记忆."""
        self._ensure_initialized()

        all_data = self._collection.get(include=["metadatas"])
        if not all_data["ids"]:
            return 0

        now = datetime.now()
        to_delete: list[str] = []

        for i, doc_id in enumerate(all_data["ids"]):
            meta = all_data["metadatas"][i]
            created = meta.get("created_at", "")
            access = int(meta.get("access_count", "0"))

            if access >= min_access_count:
                continue

            try:
                created_dt = datetime.fromisoformat(created)
                age_days = (now - created_dt).days
                if age_days > max_age_days:
                    to_delete.append(doc_id)
            except (ValueError, TypeError):
                continue

        if to_delete:
            self._collection.delete(ids=to_delete)
            logger.info("Forgot %d old memories", len(to_delete))

        return len(to_delete)
