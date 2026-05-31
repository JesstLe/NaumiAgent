"""任务管理工具 — 让 LLM 自主管理执行进度."""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore, TaskWriteItem, format_task_list
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


def create_task_tools(task_store: TaskStore) -> list[Tool]:
    """创建任务管理工具，绑定到 TaskStore 实例."""
    return [
        TodoWriteTool(task_store),
        TaskCreateTool(task_store),
        TaskUpdateTool(task_store),
        TaskListTool(task_store),
        TaskDeleteTool(task_store),
    ]


class TodoWriteTool(Tool):
    """批量同步 todo 清单."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "批量写入当前会话的 todo 清单。适合在复杂任务开始前创建完整计划，"
            "以及在每个步骤完成后同步状态。一次最多只能有一个 in_progress。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "要同步的 todo 列表。已有任务带 id；新任务省略 id。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "已有任务 ID。新增任务请省略。",
                            },
                            "content": {
                                "type": "string",
                                "description": "任务标题，必须具体且可验证。",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态。",
                            },
                            "active_form": {
                                "type": "string",
                                "description": "进行中展示文案，仅 in_progress 使用。",
                            },
                            "description": {
                                "type": "string",
                                "description": "任务补充说明。",
                            },
                            "owner": {
                                "type": "string",
                                "description": "负责人或执行者标识，可选。",
                            },
                            "blocked_by": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "依赖任务 ID 列表。",
                                "default": [],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
                "mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "description": "merge 只创建/更新传入项；replace 会删除未传入的旧任务。",
                    "default": "merge",
                },
            },
            "required": ["todos"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        todos: list[dict[str, Any]],
        mode: str = "merge",
        **kwargs: Any,
    ) -> str:
        if not self._store.session_id:
            return "错误：当前没有活跃会话，无法同步 todo。"
        if mode not in {"merge", "replace"}:
            return "错误：mode 必须是 merge 或 replace。"
        if not isinstance(todos, list):
            return "错误：todos 必须是数组。"

        try:
            items = _normalize_todo_items(todos)
        except ValueError as e:
            return f"错误：{e}"

        running = [item for item in items if item.status == TaskStatus.IN_PROGRESS]
        if len(running) > 1:
            return "错误：同一时间最多只能有一个 in_progress todo。"

        try:
            result = await self._store.write_tasks(items, replace=(mode == "replace"))
        except ValueError as e:
            return f"错误：{e}"

        changes: list[str] = []
        if result.created:
            changes.append(f"新增 {len(result.created)} 项")
        if result.updated:
            changes.append(f"更新 {len(result.updated)} 项")
        if result.deleted:
            changes.append(f"删除 {len(result.deleted)} 项")
        if not changes:
            changes.append("没有变更")
        return "todo 已同步：" + "，".join(changes) + "\n\n" + format_task_list(result.tasks)


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

    # 允许的状态转换（completed 为终止态，不可回退）
    _VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
        TaskStatus.PENDING: {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED},
        TaskStatus.IN_PROGRESS: {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.PENDING},
        TaskStatus.COMPLETED: {TaskStatus.COMPLETED},
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

        existing = await self._store.get_task(task_id)
        if existing is None:
            return f"错误：任务 #{task_id} 不存在。"

        if new_status not in self._VALID_TRANSITIONS.get(existing.status, set()):
            return (
                f"错误：无效的状态转换 {existing.status.value} → {new_status.value}。"
                f"已完成任务不可回退。"
            )

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


def _normalize_todo_items(raw_items: list[dict[str, Any]]) -> list[TaskWriteItem]:
    items: list[TaskWriteItem] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 项不是对象")
        content = str(raw.get("content", "")).strip()
        if not content:
            raise ValueError(f"第 {index} 项 content 不能为空")
        try:
            status = TaskStatus(str(raw.get("status", "")).strip())
        except ValueError as e:
            raise ValueError(
                f"第 {index} 项 status 无效，必须是 pending、in_progress 或 completed"
            ) from e
        blocked_by = raw.get("blocked_by", [])
        if blocked_by is None:
            blocked_by = []
        if not isinstance(blocked_by, list):
            raise ValueError(f"第 {index} 项 blocked_by 必须是数组")
        items.append(TaskWriteItem(
            id=str(raw["id"]).strip() if raw.get("id") is not None else None,
            subject=content,
            description=str(raw.get("description", "") or ""),
            status=status,
            active_form=str(raw.get("active_form", "") or "").strip() or None,
            owner=str(raw.get("owner", "") or "").strip() or None,
            blocked_by=[str(item).strip() for item in blocked_by if str(item).strip()],
        ))
    return items
