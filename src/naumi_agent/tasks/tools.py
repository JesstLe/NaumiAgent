"""任务管理工具 — 让 LLM 自主管理执行进度."""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore, format_task_list
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


def create_task_tools(task_store: TaskStore) -> list[Tool]:
    """创建任务管理工具，绑定到 TaskStore 实例."""
    return [
        TaskCreateTool(task_store),
        TaskUpdateTool(task_store),
        TaskListTool(task_store),
        TaskDeleteTool(task_store),
    ]


class TaskCreateTool(Tool):
    """创建一个新任务."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_create"

    @property
    def description(self) -> str:
        return (
            "创建一个新任务到当前任务列表。用于将复杂工作拆解为可追踪的步骤。"
            "每个任务有唯一 ID，可以指定依赖关系（blocked_by）。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "任务标题（简短描述，如「读取配置文件」「编写单元测试」）",
                },
                "description": {
                    "type": "string",
                    "description": "任务详细描述（可选，包含更多上下文）",
                    "default": "",
                },
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "依赖的任务 ID 列表（如 [\"1\", \"3\"] 表示"
                        "依赖任务 1 和 3 完成后才能开始）"
                    ),
                    "default": [],
                },
            },
            "required": ["subject"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        if not self._store.session_id:
            return "错误：当前没有活跃会话，无法创建任务。"

        subject = subject.strip()
        if not subject:
            return "错误：任务标题不能为空。"

        blocked_by = blocked_by or []

        try:
            task = await self._store.create_task(
                subject=subject,
                description=description,
                blocked_by=blocked_by,
            )
        except ValueError as e:
            return f"错误：{e}"

        block_info = f"（依赖 #{', #'.join(blocked_by)}）" if blocked_by else ""
        return f"已创建任务 #{task.id}：{subject} {block_info}"


class TaskUpdateTool(Tool):
    """更新任务状态."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_update"

    @property
    def description(self) -> str:
        return (
            "更新任务状态。开始执行时设为 in_progress，完成时设为 completed。"
            "也可以更新 active_form 来显示当前正在做什么。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "要更新的任务 ID",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": (
                        "新状态：pending（待处理）、"
                        "in_progress（进行中）、completed（已完成）"
                    ),
                },
                "active_form": {
                    "type": "string",
                    "description": (
                        "进行时描述（如「正在运行测试」「正在编写代码」），"
                        "仅在 in_progress 时有意义"
                    ),
                },
            },
            "required": ["task_id", "status"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        task_id: str,
        status: str,
        active_form: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self._store.session_id:
            return "错误：当前没有活跃会话。"

        try:
            new_status = TaskStatus(status)
        except ValueError:
            return f"错误：无效状态 '{status}'。有效值：pending, in_progress, completed"

        task = await self._store.update_task(
            task_id=task_id,
            status=new_status,
            active_form=active_form,
        )
        if task is None:
            return f"错误：任务 #{task_id} 不存在。"

        status_text = {
            TaskStatus.PENDING: "待处理",
            TaskStatus.IN_PROGRESS: "进行中",
            TaskStatus.COMPLETED: "已完成",
        }[new_status]
        active_text = f"（{active_form}）" if active_form else ""
        return f"任务 #{task_id}「{task.subject}」→ {status_text} {active_text}"


class TaskListTool(Tool):
    """列出所有任务."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "列出当前会话的所有任务及其状态。用于查看整体进度和规划下一步工作。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._store.session_id:
            return "错误：当前没有活跃会话。"

        tasks = await self._store.list_tasks()
        return format_task_list(tasks)


class TaskDeleteTool(Tool):
    """删除一个任务."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_delete"

    @property
    def description(self) -> str:
        return (
            "删除指定任务并清理其依赖关系。"
            "仅在任务不再需要时使用（如重复创建、计划变更）。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "要删除的任务 ID",
                },
            },
            "required": ["task_id"],
        }

    async def execute(self, *, task_id: str, **kwargs: Any) -> str:  # type: ignore[override]
        if not self._store.session_id:
            return "错误：当前没有活跃会话。"

        task = await self._store.get_task(task_id)
        if task is None:
            return f"错误：任务 #{task_id} 不存在。"

        deleted = await self._store.delete_task(task_id)
        if deleted:
            return f"已删除任务 #{task_id}「{task.subject}」"
        return f"错误：删除任务 #{task_id} 失败。"
