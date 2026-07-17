"""Session history tools."""

from __future__ import annotations

from typing import Any

from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.history_screen import (
    build_history_snapshot,
    render_history_preview,
    render_history_screen,
    render_session_delete_preview,
    render_session_retention_preview,
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
            "查看历史会话列表、预览单个会话或只读评估删除影响。"
            "用于 Agent 自主检索上下文和删除前风险判断，"
            "对应用户斜杠命令 /history。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "preview", "delete_preview", "retention_preview"],
                    "default": "list",
                    "description": (
                        "操作类型：list 列出历史会话，preview 预览指定会话，"
                        "delete_preview 只读预览删除影响，retention_preview "
                        "只读预览归档保留策略。"
                    ),
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
                    "description": "preview 或 delete_preview 操作要查看的会话 ID。",
                },
            },
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="历史会话",
            search_hint="history session delete preview /history 会话 历史 删除影响",
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
        if normalized_action == "delete_preview":
            return await self._delete_preview(session_id)
        if normalized_action == "retention_preview":
            return render_session_retention_preview(
                await self._engine.preview_session_retention()
            )
        if normalized_action != "list":
            return (
                "不支持的历史会话操作。可用操作：list、preview、"
                "delete_preview、retention_preview。"
            )
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

    async def _delete_preview(self, session_id: str) -> str:
        clean_id = (session_id or "").strip()
        if not clean_id:
            return (
                "用法：session_history(action='delete_preview', "
                "session_id='<会话ID>')"
            )
        preview = await self._engine.preview_session_delete(clean_id)
        if preview is None:
            return f"会话 {clean_id} 不存在。"
        return render_session_delete_preview(preview)

    def _current_session_id(self) -> str | None:
        session = getattr(self._engine, "_session", None)
        if session is None:
            return None
        return str(getattr(session, "id", "") or "") or None


class SessionLoadTool(Tool):
    """Load a persisted session into the active agent context."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "session_load"

    @property
    def description(self) -> str:
        return (
            "加载历史会话并恢复上下文，对应用户斜杠命令 /load。"
            "可传会话 ID，也可传最近列表中的数字编号。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "要加载的会话 ID，或最近 10 个会话列表中的数字编号。",
                },
            },
            "required": ["session_id"],
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=False,
            user_facing_name="加载会话",
            search_hint="load session /load 会话 恢复 上下文",
        )

    async def execute(self, session_id: str) -> str:
        """Load a session by id or recent-list index."""
        target = (session_id or "").strip()
        if not target:
            return "用法：session_load(session_id='<会话ID或编号>')"

        resolved_id = await self._resolve_session_id(target)
        if resolved_id is None:
            return f"没有找到编号为 {target} 的历史会话。"

        loaded = await self._engine.load_session(resolved_id)
        if not loaded:
            return f"会话 {resolved_id} 不存在。"

        active = getattr(self._engine, "_session", None)
        title = str(getattr(active, "title", "") or "新会话")
        active_id = str(getattr(active, "id", resolved_id) or resolved_id)
        message_count = len(list(getattr(active, "messages", []) or []))
        return (
            f"已加载会话：{title}\n"
            f"- ID：`{active_id}`\n"
            f"- 消息数：{message_count}\n"
            "上下文已恢复，可继续对话。"
        )

    async def _resolve_session_id(self, session_id: str) -> str | None:
        if not session_id.isdigit():
            return session_id

        sessions, _ = await self._engine.list_sessions(page=1, page_size=10)
        index = int(session_id) - 1
        if index < 0 or index >= len(sessions):
            return None
        return str(getattr(sessions[index], "id", "") or "") or None


class SessionDeleteTool(Tool):
    """Delete a persisted session by explicit id."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "session_delete"

    @property
    def description(self) -> str:
        return (
            "删除指定历史会话，对应用户斜杠命令 /delete。"
            "这是不可逆的破坏性操作，必须提供明确会话 ID。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "要删除的明确会话 ID。不支持数字编号，避免误删。",
                },
            },
            "required": ["session_id"],
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            concurrency_safe=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="删除会话",
            search_hint="delete session /delete 删除 会话",
        )

    async def execute(self, session_id: str) -> str:
        """Delete a session by explicit id."""
        target = (session_id or "").strip()
        if not target:
            return "用法：session_delete(session_id='<会话ID>')"
        if target.isdigit():
            return "为避免误删，session_delete 只接受明确会话 ID，不接受数字编号。"

        result = await self._engine.delete_session_detailed(target)
        if result.outcome is ReconciliationCoordinatorOutcome.NOT_FOUND:
            return f"会话 {target} 不存在。"
        if result.outcome is ReconciliationCoordinatorOutcome.COMPLETED:
            return f"{result.message}\n- Session：`{target}`"
        if result.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED:
            return (
                f"会话删除协调尚未完成，已安排安全重试：{target}\n"
                f"- Request ID：`{result.request_id}`"
            )
        if result.outcome is ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED:
            return (
                f"会话删除协调重试已耗尽，需要人工检查：{target}\n"
                f"- Request ID：`{result.request_id}`"
            )
        return f"会话 {target} 的生命周期策略阻止删除。"


def create_session_tools(engine: Any) -> list[Tool]:
    """Create session-related tools."""
    return [SessionHistoryTool(engine), SessionLoadTool(engine), SessionDeleteTool(engine)]
