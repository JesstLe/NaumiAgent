"""Tests for main CLI analysis dispatch semantics."""

from __future__ import annotations

from typing import Any

import pytest

from naumi_agent.main import _run_analysis


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "# 分析完成\n\n测试输出。"


class _FakeEngine:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self.tool_registry = {tool_name: tool}


@pytest.mark.asyncio
async def test_run_analysis_passes_page_session_context() -> None:
    tool = _FakeTool()
    engine = _FakeEngine("analysis_page", tool)

    await _run_analysis(engine, "page", "用户消息很多，需要分页")

    assert tool.calls == [{"session_context": "用户消息很多，需要分页"}]
