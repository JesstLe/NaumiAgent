"""记忆工具 — 让 LLM 可以存储和召回长期记忆."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from naumi_agent.memory.long_term import MemoryEntry
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


def create_memory_tools(memory: Any) -> list[Tool]:
    """创建记忆相关工具，绑定到 LongTermMemory 实例."""
    return [MemoryStoreTool(memory), MemoryRecallTool(memory)]


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
                    "description": "分类: fact | preference | experience",
                    "default": "fact",
                },
            },
            "required": ["content"],
        }

    async def execute(self, *, content: str, category: str = "fact", **kwargs: Any) -> str:
        entry = MemoryEntry(
            id="",
            content=content,
            category=category,
            created_at=datetime.now().isoformat(),
        )
        try:
            entry_id = await self._memory.store(entry)
            return f"已存储记忆 (id={entry_id}, category={category})"
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
                    "description": "限定分类: fact | preference | experience（可选）",
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
            results = await self._memory.recall(query, category=category, top_k=top_k)
        except Exception as e:
            return f"回忆失败: {type(e).__name__}: {e}"

        if not results:
            return "没有找到相关记忆。"

        parts = []
        for r in results:
            parts.append(f"- [{r.entry.category}] (相关度 {r.relevance:.0%}) {r.entry.content}")
        return "\n".join(parts)
