"""子 Agent 工具 — 让主 LLM 可以委派任务给专用 Agent."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def create_subagent_tools(manager: Any) -> list[Tool]:
    return [
        DelegateTaskTool(manager),
        SpawnAgentTool(manager),
        DestroyAgentTool(manager),
        ListAgentsTool(manager),
        BlackboardReadTool(manager),
        BlackboardWriteTool(manager),
    ]


class DelegateTaskTool(Tool):
    """将子任务委派给专用 Agent."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return (
            "将一个子任务委派给专用 Agent 执行。"
            "可用 Agent: coder（编程）、researcher（研究搜索）、browser（浏览器操作）。"
            "适用于需要特定能力的子任务。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要委派的任务描述",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent: coder | researcher | browser (optional)",
                },
                "task_id": {
                    "type": "string",
                    "description": "要回写状态的 todo ID（可选）。",
                },
                "success_criteria": {
                    "type": "string",
                    "description": "子任务成功标准（可选，会追加给子 Agent）。",
                },
                "context": {
                    "type": "string",
                    "description": "额外上下文（可选）。",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        agent: str | None = None,
        task_id: str | None = None,
        success_criteria: str = "",
        context: str = "",
        event_callback: EventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        from naumi_agent.orchestrator.subagent_manager import SubTask

        linked_task_id = (task_id or "").strip()
        if linked_task_id:
            status_error = await _mark_linked_task_started(self._manager, linked_task_id)
            if status_error:
                return status_error

        description = _build_delegation_prompt(task, success_criteria)
        subtask = SubTask(
            id=linked_task_id or f"sub_{task[:20]}",
            description=description,
            agent_name=agent,
            context=context,
        )

        try:
            result = await self._manager.delegate(
                subtask,
                event_callback=event_callback,
            )
            if linked_task_id:
                await _mark_linked_task_finished(self._manager, linked_task_id, result)
            if result.status == "completed":
                return result.response
            return f"子任务失败 ({result.status}): {result.error or result.response[:500]}"
        except Exception as e:
            if linked_task_id:
                await _mark_linked_task_exception(self._manager, linked_task_id, e)
            return f"委派任务出错: {type(e).__name__}: {e}"


class SpawnAgentTool(Tool):
    """自主创建一个新的专用子 Agent."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "spawn_agent"

    @property
    def description(self) -> str:
        return (
            "创建一个新的专用子 Agent。"
            "根据任务描述自动推断能力、生成 system prompt。"
            "创建后可用 delegate_task 向其委派任务。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent 唯一名称（如 security_auditor、perf_optimizer）",
                },
                "task_description": {
                    "type": "string",
                    "description": "Agent 负责的任务领域描述",
                },
                "role": {
                    "type": "string",
                    "description": "角色类型（expert_analyst、coder、researcher 等）",
                    "default": "expert_analyst",
                },
                "focus": {
                    "type": "string",
                    "description": "专注领域（如 security、performance、testing）",
                    "default": "",
                },
            },
            "required": ["name", "task_description"],
        }

    async def execute(
        self,
        *,
        name: str,
        task_description: str,
        role: str = "expert_analyst",
        focus: str = "",
        **kwargs: Any,
    ) -> str:
        if self._manager.get_agent(name):
            return f"Agent '{name}' 已存在，可直接使用 delegate_task 委派任务。"

        try:
            agent = await self._manager.spawn_for_task_with_llm(
                name=name,
                task_description=task_description,
                role=role,
                focus=focus,
            )
            return (
                f"✅ 已创建子 Agent '{agent.config.name}'\n"
                f"   描述: {agent.config.description}\n"
                f"   能力: {', '.join(c.value for c in agent.config.capabilities)}\n"
                f"   可通过 delegate_task(task='...', agent='{name}') 委派任务。"
            )
        except Exception as e:
            return f"创建 Agent 失败: {type(e).__name__}: {e}"


class DestroyAgentTool(Tool):
    """销毁一个动态子 Agent，释放资源."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "destroy_agent"

    @property
    def description(self) -> str:
        return (
            "销毁一个动态创建的子 Agent，释放资源。"
            "任务完成后不再需要的 Agent 应及时销毁。"
            "预设 Agent（coder、researcher、browser）不可销毁。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要销毁的 Agent 名称",
                },
            },
            "required": ["name"],
        }

    async def execute(self, *, name: str, **kwargs: Any) -> str:
        if not self._manager.get_agent(name):
            return f"Agent '{name}' 不存在。"

        if self._manager.destroy(name):
            return f"✅ 已销毁子 Agent '{name}'，资源已释放。"
        return f"无法销毁 '{name}'（预设 Agent 不可销毁）。"


class ListAgentsTool(Tool):
    """列出可用的子 Agent."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "list_agents"

    @property
    def description(self) -> str:
        return "列出可用的专用 Agent 及其描述。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        agents = self._manager.list_agents()
        if not agents:
            return "没有可用的子 Agent。"
        lines = ["可用 Agent:"]
        for a in agents:
            state = a.get("state", "?")
            tasks = a.get("tasks", "0")
            age = a.get("age_s", "?")
            idle = a.get("idle_s", "")
            info = f"  - {a['name']} [{state}] (任务: {tasks}, 存活: {age}s"
            if idle:
                info += f", 空闲: {idle}s"
            info += f")\n    {a['description']}"
            lines.append(info)
        return "\n".join(lines)


class BlackboardReadTool(Tool):
    """读取共享状态板上的数据."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "blackboard_read"

    @property
    def description(self) -> str:
        return (
            "读取 Agent 共享状态板（Blackboard）上的数据。"
            "多个 Agent 通过共享状态板交换中间结果和协同信息。"
            "不传 key 则返回所有条目。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "要读取的键（可选，不传则返回全部）",
                    "default": "",
                },
            },
        }

    async def execute(self, *, key: str = "", **kwargs: Any) -> str:
        bus = self._manager.message_bus
        if key:
            entry = await bus.blackboard_get(key)
            if not entry:
                return f"共享状态 '{key}' 不存在。"
            return (
                f"**{entry.key}** (作者: {entry.author}, "
                f"版本: {entry.version})\n\n"
                f"{entry.value}"
            )

        all_entries = await bus.blackboard_get_all()
        if not all_entries:
            return "共享状态板为空。"

        lines = [f"共享状态板 ({len(all_entries)} 条):"]
        for k, entry in sorted(all_entries.items()):
            val = str(entry.value)[:150]
            lines.append(
                f"  - **{k}** ({entry.author}, v{entry.version}): {val}"
            )
        return "\n".join(lines)


class BlackboardWriteTool(Tool):
    """向共享状态板写入数据."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "blackboard_write"

    @property
    def description(self) -> str:
        return (
            "向 Agent 共享状态板（Blackboard）写入数据。"
            "其他 Agent 可通过 blackboard_read 读取这些数据。"
            "适用于在多个 Agent 之间传递中间结果。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "状态键名",
                },
                "value": {
                    "type": "string",
                    "description": "要写入的值",
                },
            },
            "required": ["key", "value"],
        }

    async def execute(
        self, *, key: str, value: str, **kwargs: Any,
    ) -> str:
        bus = self._manager.message_bus
        entry = await bus.blackboard_set(key, value, author="main_agent")
        return (
            f"✅ 已写入共享状态 '{key}' "
            f"(版本: {entry.version})"
        )


def _build_delegation_prompt(task: str, success_criteria: str) -> str:
    text = task.strip()
    criteria = success_criteria.strip()
    if not criteria:
        return text
    return f"{text}\n\n成功标准：{criteria}"


async def _mark_linked_task_started(manager: Any, task_id: str) -> str:
    store = manager._engine.task_store
    task = await store.get_task(task_id)
    if task is None:
        return f"错误：todo #{task_id} 不存在，无法委派并回写。"
    if task.status == TaskStatus.COMPLETED:
        return f"错误：todo #{task_id} 已完成，不能再次委派。"
    await store.update_task(
        task_id,
        status=TaskStatus.IN_PROGRESS,
        active_form=f"子 Agent 执行中：{task.subject}",
    )
    return ""


async def _mark_linked_task_finished(manager: Any, task_id: str, result: Any) -> None:
    store = manager._engine.task_store
    if result.status == "completed":
        await store.update_task(task_id, status=TaskStatus.COMPLETED)
        return
    reason = result.error or result.response[:200] or result.status
    await store.update_task(
        task_id,
        status=TaskStatus.BLOCKED,
        active_form=f"阻塞：子 Agent {result.status} - {reason[:160]}",
    )


async def _mark_linked_task_exception(manager: Any, task_id: str, error: Exception) -> None:
    await manager._engine.task_store.update_task(
        task_id,
        status=TaskStatus.BLOCKED,
        active_form=f"阻塞：委派异常 - {type(error).__name__}: {str(error)[:140]}",
    )
