"""Memory tool boundary tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from naumi_agent.memory.long_term import MemoryEntry, MemorySearchResult
from naumi_agent.tools.memory import (
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_QUERY_CHARS,
    MAX_MEMORY_TOP_K,
    MEMORY_CATEGORIES,
    MemoryRecallTool,
    MemoryStoreTool,
)


@dataclass
class FakeMemory:
    stored: list[MemoryEntry] = field(default_factory=list)
    recalls: list[dict[str, Any]] = field(default_factory=list)
    recall_results: list[MemorySearchResult] = field(default_factory=list)

    async def store(self, entry: MemoryEntry) -> str:
        self.stored.append(entry)
        return "mem123"

    async def recall(
        self,
        query: str,
        *,
        category: str | None = None,
        top_k: int = 3,
    ) -> list[MemorySearchResult]:
        self.recalls.append({"query": query, "category": category, "top_k": top_k})
        return self.recall_results


class TestMemoryStoreTool:
    def test_metadata_marks_state_change_without_confirmation(self) -> None:
        metadata = MemoryStoreTool(FakeMemory()).metadata
        assert metadata.read_only is False
        assert metadata.requires_confirmation is False
        assert metadata.user_facing_name == "存储长期记忆"

    def test_schema_includes_all_supported_categories(self) -> None:
        description = MemoryStoreTool(FakeMemory()).parameters_schema["properties"][
            "category"
        ]["description"]
        for category in MEMORY_CATEGORIES:
            assert category in description

    @pytest.mark.asyncio
    async def test_store_normalizes_content_and_category(self) -> None:
        memory = FakeMemory()

        result = await MemoryStoreTool(memory).execute(
            content="  用户喜欢紧凑的技术总结  ",
            category=" Preference ",
        )

        assert "已存储记忆" in result
        assert "category=preference" in result
        assert memory.stored[0].content == "用户喜欢紧凑的技术总结"
        assert memory.stored[0].category == "preference"

    @pytest.mark.parametrize(
        ("content", "category", "expected"),
        [
            ("", "fact", "content 不能为空"),
            ("x" * (MAX_MEMORY_CONTENT_CHARS + 1), "fact", "content 过长"),
            ("valid", "secret", "category 只能是"),
            ("valid", 123, "category 必须是字符串"),
        ],
    )
    @pytest.mark.asyncio
    async def test_store_rejects_invalid_inputs(
        self,
        content: Any,
        category: Any,
        expected: str,
    ) -> None:
        memory = FakeMemory()

        result = await MemoryStoreTool(memory).execute(
            content=content,
            category=category,
        )

        assert "已拒绝" in result
        assert expected in result
        assert memory.stored == []


class TestMemoryRecallTool:
    def test_metadata_marks_recall_as_read_only(self) -> None:
        metadata = MemoryRecallTool(FakeMemory()).metadata
        assert metadata.read_only is True
        assert metadata.concurrency_safe is True
        assert metadata.user_facing_name == "召回长期记忆"

    def test_schema_includes_all_supported_categories(self) -> None:
        description = MemoryRecallTool(FakeMemory()).parameters_schema["properties"][
            "category"
        ]["description"]
        for category in MEMORY_CATEGORIES:
            assert category in description

    @pytest.mark.asyncio
    async def test_recall_normalizes_inputs_and_formats_results(self) -> None:
        memory = FakeMemory(
            recall_results=[
                MemorySearchResult(
                    entry=MemoryEntry(
                        id="mem123",
                        content="用户喜欢 Python",
                        category="preference",
                    ),
                    relevance=0.875,
                )
            ]
        )

        result = await MemoryRecallTool(memory).execute(
            query=" Python ",
            category=" Preference ",
            top_k=2,
        )

        assert "用户喜欢 Python" in result
        assert "相关度 88%" in result
        assert memory.recalls == [
            {"query": "Python", "category": "preference", "top_k": 2}
        ]

    @pytest.mark.asyncio
    async def test_recall_returns_empty_message(self) -> None:
        result = await MemoryRecallTool(FakeMemory()).execute(query="不存在")

        assert result == "没有找到相关记忆。"

    @pytest.mark.parametrize(
        ("query", "category", "top_k", "expected"),
        [
            ("", None, 3, "query 不能为空"),
            ("x" * (MAX_MEMORY_QUERY_CHARS + 1), None, 3, "query 过长"),
            ("valid", "secret", 3, "category 只能是"),
            ("valid", None, 0, f"top_k 必须在 1 到 {MAX_MEMORY_TOP_K} 之间"),
            ("valid", None, MAX_MEMORY_TOP_K + 1, "top_k 必须在 1 到"),
            ("valid", None, True, "top_k 必须是整数"),
        ],
    )
    @pytest.mark.asyncio
    async def test_recall_rejects_invalid_inputs(
        self,
        query: Any,
        category: Any,
        top_k: Any,
        expected: str,
    ) -> None:
        memory = FakeMemory()

        result = await MemoryRecallTool(memory).execute(
            query=query,
            category=category,
            top_k=top_k,
        )

        assert "已拒绝" in result
        assert expected in result
        assert memory.recalls == []
