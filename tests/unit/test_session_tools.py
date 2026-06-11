"""Session history tool tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.memory.session import Session
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.session import SessionHistoryTool, SessionLoadTool


def _session(session_id: str = "s1", title: str = "Demo") -> Session:
    return Session(
        id=session_id,
        title=title,
        model="kimi-for-coding",
        messages=[
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "可以。"},
        ],
        updated_at=datetime(2026, 6, 11, 10, 30),
        workspace_root="/tmp/naumi",
        git_branch="main",
        summary="测试摘要",
    )


@pytest.mark.asyncio
async def test_session_history_tool_lists_sessions() -> None:
    engine = MagicMock()
    engine._session = MagicMock(id="s1")
    engine.workspace_root = "/tmp/naumi"
    engine.list_sessions = AsyncMock(return_value=([_session()], 1))
    tool = SessionHistoryTool(engine)

    output = await tool.execute(query="Demo", page=1, page_size=20)

    assert tool.metadata.read_only is True
    assert "历史会话（共 1 个） · 搜索: Demo" in output
    assert "Demo *当前" in output
    assert "id: s1" in output
    engine.list_sessions.assert_awaited_once_with(page=1, page_size=20, query="Demo")


@pytest.mark.asyncio
async def test_session_history_tool_previews_session() -> None:
    engine = MagicMock()
    engine.session_store.load = AsyncMock(return_value=_session("s2", "Preview"))
    tool = SessionHistoryTool(engine)

    output = await tool.execute(action="preview", session_id="s2")

    assert "## Preview" in output
    assert "ID：`s2`" in output
    assert "测试摘要" in output
    engine.session_store.load.assert_awaited_once_with("s2")


def test_engine_registers_session_history_tool(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_history")

    assert tool is not None
    assert tool.metadata.read_only is True


@pytest.mark.asyncio
async def test_session_load_tool_loads_by_session_id() -> None:
    engine = MagicMock()
    engine.load_session = AsyncMock(return_value=True)
    engine._session = _session("s2", "Loaded")
    tool = SessionLoadTool(engine)

    output = await tool.execute(session_id="s2")

    assert "已加载会话：Loaded" in output
    assert "ID：`s2`" in output
    engine.load_session.assert_awaited_once_with("s2")


@pytest.mark.asyncio
async def test_session_load_tool_loads_by_recent_index() -> None:
    engine = MagicMock()
    engine.list_sessions = AsyncMock(return_value=([_session("s1"), _session("s2")], 2))
    engine.load_session = AsyncMock(return_value=True)
    engine._session = _session("s2", "Second")
    tool = SessionLoadTool(engine)

    output = await tool.execute(session_id="2")

    assert "已加载会话：Second" in output
    engine.list_sessions.assert_awaited_once_with(page=1, page_size=10)
    engine.load_session.assert_awaited_once_with("s2")


def test_engine_registers_session_load_tool(tmp_path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )

    tool = engine.tool_registry.get("session_load")

    assert tool is not None
    assert tool.metadata.read_only is False
