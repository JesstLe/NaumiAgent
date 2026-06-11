"""Session history tools."""

from __future__ import annotations

from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.history_screen import (
    build_history_snapshot,
    render_history_preview,
    render_history_screen,
)


class SessionHistoryTool(Tool):
    """Expose session history to autonomous agent tool use."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "session_history"

    @property
    def description(self) -> str:
        return (
            "查看历史会话列表或预览单个会话。用于 Agent 自主检索上下文，"
            "对应用户斜杠命令 /history。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "preview"],
                    "default": "list",
                    "description": "操作类型：list 列出历史会话，preview 预览指定会话。",
                },
                "query": {
                    "type": "string",
                    "description": "列表搜索关键词，可匹配标题、模型、工作区、分支或摘要。",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "列表页码，从 1 开始。",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": "每页数量，最大 50。",
                },
                "session_id": {
                    "type": "string",
                    "description": "preview 操作要查看的会话 ID。",
                },
            },
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="历史会话",
            search_hint="history session /history 会话 历史",
        )

    async def execute(
        self,
        action: str = "list",
        query: str = "",
        page: int = 1,
        page_size: int = 20,
        session_id: str = "",
    ) -> str:
        """Render session history list or preview text."""
        normalized_action = (action or "list").strip().lower()
        if normalized_action == "preview":
            return await self._preview(session_id)
        if normalized_action != "list":
            return "不支持的历史会话操作。可用操作：list、preview。"
        return await self._list(query=query, page=page, page_size=page_size)

    async def _list(self, *, query: str, page: int, page_size: int) -> str:
        safe_page = max(1, int(page or 1))
        safe_page_size = min(50, max(1, int(page_size or 20)))
        sessions, total = await self._engine.list_sessions(
            page=safe_page,
            page_size=safe_page_size,
            query=(query or "").strip(),
        )
        snapshot = build_history_snapshot(
            sessions,
            total=total,
            query=(query or "").strip(),
            current_session_id=self._current_session_id(),
            fallback_workspace=str(getattr(self._engine, "workspace_root", "") or ""),
        )
        return render_history_screen(snapshot)

    async def _preview(self, session_id: str) -> str:
        clean_id = (session_id or "").strip()
        if not clean_id:
            return "用法：session_history(action='preview', session_id='<会话ID>')"
        session = await self._engine.session_store.load(clean_id)
        if session is None:
            return f"会话 {clean_id} 不存在。"
        return render_history_preview(session)

    def _current_session_id(self) -> str | None:
        session = getattr(self._engine, "_session", None)
        if session is None:
            return None
        return str(getattr(session, "id", "") or "") or None


def create_session_tools(engine: Any) -> list[Tool]:
    """Create session-related tools."""
    return [SessionHistoryTool(engine)]
