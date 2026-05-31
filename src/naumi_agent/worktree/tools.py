"""Worktree tools exposed to the agent."""

from __future__ import annotations

from typing import Any

from naumi_agent.tools.base import Tool
from naumi_agent.worktree.manager import WorktreeManager


def create_worktree_tools(manager: WorktreeManager) -> list[Tool]:
    return [
        WorktreeCreateTool(manager),
        WorktreeStatusTool(manager),
        WorktreeBindTaskTool(manager),
        WorktreeKeepTool(manager),
        WorktreeRemoveTool(manager),
    ]


class WorktreeCreateTool(Tool):
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "worktree_create"

    @property
    def description(self) -> str:
        return "创建隔离 Git worktree，可选绑定到当前会话中的任务 ID。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "worktree 名称"},
                "task_id": {"type": "string", "description": "可选任务 ID", "default": ""},
            },
            "required": ["name"],
        }

    async def execute(self, *, name: str, task_id: str = "", **kwargs: Any) -> str:
        return await self._manager.create(name=name, task_id=task_id)


class WorktreeStatusTool(Tool):
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "worktree_status"

    @property
    def description(self) -> str:
        return "查看一个或全部由 NaumiAgent 管理的隔离 worktree 状态。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "可选 worktree 名称；为空时列出全部",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(self, *, name: str = "", **kwargs: Any) -> str:
        try:
            status = await self._manager.status(name=name)
        except KeyError:
            return f"错误：worktree 不存在：{name}"
        except ValueError as e:
            return f"错误：{e}"
        return self._manager.format_status(status)


class WorktreeBindTaskTool(Tool):
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "worktree_bind_task"

    @property
    def description(self) -> str:
        return "将已有隔离 worktree 绑定到当前会话中的任务 ID。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "worktree 名称"},
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["name", "task_id"],
        }

    async def execute(self, *, name: str, task_id: str, **kwargs: Any) -> str:
        return await self._manager.bind_task(name=name, task_id=task_id)


class WorktreeKeepTool(Tool):
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "worktree_keep"

    @property
    def description(self) -> str:
        return "标记 worktree 为保留状态，用于人工审查或后续合并。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "worktree 名称"},
                "reason": {"type": "string", "description": "保留原因", "default": ""},
            },
            "required": ["name"],
        }

    async def execute(self, *, name: str, reason: str = "", **kwargs: Any) -> str:
        try:
            return await self._manager.keep(name=name, reason=reason)
        except KeyError:
            return f"错误：worktree 不存在：{name}"
        except ValueError as e:
            return f"错误：{e}"


class WorktreeRemoveTool(Tool):
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "worktree_remove"

    @property
    def description(self) -> str:
        return "删除隔离 worktree；默认拒绝删除仍有未提交文件或新提交的 worktree。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "worktree 名称"},
                "discard_changes": {
                    "type": "boolean",
                    "description": "是否强制丢弃未保存变更",
                    "default": False,
                },
            },
            "required": ["name"],
        }

    async def execute(
        self,
        *,
        name: str,
        discard_changes: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            return await self._manager.remove(name=name, discard_changes=discard_changes)
        except KeyError:
            return f"错误：worktree 不存在：{name}"
        except ValueError as e:
            return f"错误：{e}"
