"""长期记忆 — ChromaDB 向量存储."""

from __future__ import annotations

import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from naumi_agent.config.settings import MemoryConfig

logger = logging.getLogger(__name__)

# Deduplication: cosine similarity threshold above which memories are merged.
_DEDUP_SIMILARITY_THRESHOLD = 0.92

# Scoring: half-life in days for recency decay.
_RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass(frozen=True)
class MemoryEntry:
    """一条记忆记录."""

    id: str
    content: str
    category: str  # "fact" | "preference" | "experience" | "plan_template"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    access_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
        """存储一条记忆.

        自动去重：如果已存在高度相似（>0.92）的记忆，合并而非新增。
        合并策略：保留更长的内容，更新 updated_at，累加 metadata。
        """
        self._ensure_initialized()

        now = datetime.now().isoformat()

        # Dedup check: find similar existing memory
        existing = self._find_similar(entry.content, category=entry.category)
        if existing is not None:
            merged = self._merge_entries(existing, entry, now=now)
            self._upsert_entry(merged)
            logger.info(
                "Merged memory with existing (id=%s, similarity=%.2f)",
                merged.id,
                existing.get("distance", 0),
            )
            return merged.id

        # New entry
        if not entry.id:
            entry = MemoryEntry(
                id=uuid.uuid4().hex[:12],
                content=entry.content,
                category=entry.category,
                metadata=entry.metadata,
                created_at=entry.created_at or now,
                updated_at=now,
                access_count=entry.access_count,
            )

        self._upsert_entry(entry)
        return entry.id

    def _find_similar(
        self, content: str, *, category: str | None = None
    ) -> dict[str, Any] | None:
        """Check for a highly similar existing memory. Returns ChromaDB result dict or None."""
        if self._collection.count() == 0:
            return None

        where_filter = {"category": category} if category else None
        n_results = min(1, self._collection.count())

        results = self._collection.query(
            query_texts=[content],
            n_results=n_results,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return None

        distance = results["distances"][0][0]
        similarity = 1.0 - distance

        if similarity >= _DEDUP_SIMILARITY_THRESHOLD:
            return {
                "id": results["ids"][0][0],
                "document": results["documents"][0][0],
                "metadata": results["metadatas"][0][0],
                "distance": distance,
            }
        return None

    def _merge_entries(
        self, existing: dict[str, Any], incoming: MemoryEntry, *, now: str
    ) -> MemoryEntry:
        """Merge incoming entry into existing. Keeps longer content."""
        old_content = existing.get("document", "")
        new_content = incoming.content
        merged_content = new_content if len(new_content) > len(old_content) else old_content

        old_meta = existing.get("metadata", {})
        clean_old_meta = {
            k: v for k, v in old_meta.items()
            if k not in ("category", "created_at", "updated_at", "access_count")
        }
        merged_metadata = {**clean_old_meta, **incoming.metadata}

        old_access = int(old_meta.get("access_count", "0"))

        return MemoryEntry(
            id=existing["id"],
            content=merged_content,
            category=incoming.category,
            metadata=merged_metadata,
            created_at=old_meta.get("created_at", incoming.created_at or now),
            updated_at=now,
            access_count=old_access,
        )

    def _upsert_entry(self, entry: MemoryEntry) -> None:
        """Write a MemoryEntry to ChromaDB."""
        meta = {
            "category": entry.category,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "access_count": str(entry.access_count),
        }
        meta.update({k: str(v) for k, v in entry.metadata.items()})

        self._collection.upsert(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[meta],
        )

    async def recall(
        self,
        query: str,
        *,
        category: str | None = None,
        top_k: int = 5,
        min_relevance: float = 0.5,
    ) -> list[MemorySearchResult]:
        """召回相关记忆.

        使用综合评分：relevance * recency_weight * access_weight.
        """
        self._ensure_initialized()

        count = self._collection.count()
        if count == 0:
            return []

        where_filter = {"category": category} if category else None
        # Over-fetch for scoring/reranking, then trim to top_k
        fetch_k = min(top_k * 3, count)

        results = self._collection.query(
            query_texts=[query],
            n_results=fetch_k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        now = datetime.now()
        scored: list[tuple[float, MemorySearchResult]] = []

        for i, doc_id in enumerate(results["ids"][0]):
            doc = results["documents"][0][i] if results["documents"] else ""
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            distance = results["distances"][0][i] if results["distances"] else 1.0

            relevance = round(1.0 - distance, 4)
            if relevance < min_relevance:
                continue

            recency_w = _recency_score(
                meta.get("updated_at", "") or meta.get("created_at", ""), now,
            )
            access_w = _access_score(int(meta.get("access_count", "0")))
            composite = relevance * recency_w * access_w

            clean_meta = {
                k: v for k, v in meta.items()
                if k not in ("category", "created_at", "updated_at", "access_count")
            }

            entry = MemoryEntry(
                id=doc_id,
                content=doc,
                category=meta.get("category", "unknown"),
                metadata=clean_meta,
                created_at=meta.get("created_at", ""),
                updated_at=meta.get("updated_at", ""),
                access_count=int(meta.get("access_count", "0")),
            )
            result = MemorySearchResult(entry=entry, relevance=relevance)
            scored.append((composite, result))

        scored.sort(key=lambda t: t[0], reverse=True)
        entries = [r for _, r in scored[:top_k]]

        # Increment access count for returned entries
        for r in entries:
            new_count = r.entry.access_count + 1
            updated_meta = {
                "category": r.entry.category,
                "created_at": r.entry.created_at,
                "updated_at": r.entry.updated_at,
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


def _recency_score(timestamp: str, now: datetime) -> float:
    """Exponential decay: weight 1.0 for fresh, decays with 30-day half-life."""
    if not timestamp:
        return 0.5
    try:
        ts = datetime.fromisoformat(timestamp)
        age_days = max(0.0, (now - ts).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 0.5
    # exp(-age * ln(2) / half_life) — 1.0 at day 0, 0.5 at day 30, 0.25 at day 60
    return math.exp(-age_days * math.log(2) / _RECENCY_HALF_LIFE_DAYS)


def _access_score(access_count: int) -> float:
    """Logarithmic boost: 1.0 at 0 accesses, grows slowly."""
    # 1.0 + log2(1 + count) / 5 — gives 1.0, 1.2, 1.4, 1.6 for 0,1,3,7 accesses
    return 1.0 + math.log2(1 + access_count) / 5.0
