"""记忆工具 — 让 LLM 可以存储和召回长期记忆."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from naumi_agent.memory.long_term import MemoryEntry
from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

MEMORY_CATEGORIES = frozenset({"fact", "preference", "experience", "plan_template"})
MAX_MEMORY_CONTENT_CHARS = 8_000
MAX_MEMORY_QUERY_CHARS = 1_000
MAX_MEMORY_TOP_K = 10


def create_memory_tools(memory: Any) -> list[Tool]:
    """创建记忆相关工具，绑定到 LongTermMemory 实例."""
    return [MemoryStoreTool(memory), MemoryRecallTool(memory)]


def _normalize_memory_category(category: Any, *, required: bool) -> str | None:
    """Validate memory category values accepted by public tools."""
    if category is None:
        if required:
            return "fact"
        return None
    if not isinstance(category, str):
        raise ValueError("category 必须是字符串。")
    normalized = category.strip().lower()
    if not normalized:
        if required:
            return "fact"
        return None
    if normalized not in MEMORY_CATEGORIES:
        allowed = " | ".join(sorted(MEMORY_CATEGORIES))
        raise ValueError(f"category 只能是: {allowed}。")
    return normalized


def _normalize_memory_content(content: Any) -> str:
    """Validate and normalize memory content before storage."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 不能为空，且必须是字符串。")
    normalized = content.strip()
    if len(normalized) > MAX_MEMORY_CONTENT_CHARS:
        raise ValueError(
            "content 过长，当前上限为 "
            f"{MAX_MEMORY_CONTENT_CHARS} 个字符。"
        )
    return normalized


def _normalize_memory_query(query: Any) -> str:
    """Validate and normalize memory recall query."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空，且必须是字符串。")
    normalized = query.strip()
    if len(normalized) > MAX_MEMORY_QUERY_CHARS:
        raise ValueError(
            "query 过长，当前上限为 "
            f"{MAX_MEMORY_QUERY_CHARS} 个字符。"
        )
    return normalized


def _normalize_top_k(top_k: Any) -> int:
    """Validate recall result count."""
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ValueError("top_k 必须是整数。")
    if top_k < 1 or top_k > MAX_MEMORY_TOP_K:
        raise ValueError(f"top_k 必须在 1 到 {MAX_MEMORY_TOP_K} 之间。")
    return top_k


class MemoryStoreTool(Tool):
    """存储一条长期记忆."""

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "将一条重要信息存入长期记忆。适用于用户偏好、关键事实、重要决策等需要跨会话记住的内容。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="存储长期记忆",
            search_hint="store memory preference fact experience plan template",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的内容",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "分类: fact | preference | experience | plan_template"
                    ),
                    "default": "fact",
                },
            },
            "required": ["content"],
        }

    async def execute(self, *, content: str, category: str = "fact", **kwargs: Any) -> str:
        try:
            normalized_content = _normalize_memory_content(content)
            normalized_category = _normalize_memory_category(category, required=True)
            assert normalized_category is not None
        except ValueError as e:
            return f"存储记忆已拒绝: {e}"

        entry = MemoryEntry(
            id="",
            content=normalized_content,
            category=normalized_category,
            created_at=datetime.now().isoformat(),
        )
        try:
            entry_id = await self._memory.store(entry)
            return f"已存储记忆 (id={entry_id}, category={normalized_category})"
        except Exception as e:
            return f"存储记忆失败: {type(e).__name__}: {e}"


class MemoryRecallTool(Tool):
    """从长期记忆中召回相关内容."""

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_recall"

    @property
    def description(self) -> str:
        return "从长期记忆中搜索与当前问题相关的内容。用于回忆用户偏好、之前讨论过的事实等。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="召回长期记忆",
            search_hint="recall search memory preference fact experience",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询，描述你想回忆的内容",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "限定分类: fact | preference | experience | plan_template（可选）"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数，默认 3",
                    "default": 3,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self, *, query: str, category: str | None = None, top_k: int = 3, **kwargs: Any
    ) -> str:
        try:
            normalized_query = _normalize_memory_query(query)
            normalized_category = _normalize_memory_category(category, required=False)
            normalized_top_k = _normalize_top_k(top_k)
        except ValueError as e:
            return f"回忆已拒绝: {e}"

        try:
            results = await self._memory.recall(
                normalized_query,
                category=normalized_category,
                top_k=normalized_top_k,
            )
        except Exception as e:
            return f"回忆失败: {type(e).__name__}: {e}"

        if not results:
            return "没有找到相关记忆。"

        parts = []
        for r in results:
            parts.append(f"- [{r.entry.category}] (相关度 {r.relevance:.0%}) {r.entry.content}")
        return "\n".join(parts)
