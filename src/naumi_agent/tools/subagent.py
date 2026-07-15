"""子 Agent 工具 — 让主 LLM 可以委派任务给专用 Agent."""

from __future__ import annotations

import logging
import re
from typing import Any

from naumi_agent.agents.team_protocol import (
    execute_team_signal,
    execute_team_status,
    format_team_signal_result,
)
from naumi_agent.runtime.ports.events import LegacyEventCallback
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

MAX_BLACKBOARD_KEY_CHARS = 120
MAX_BLACKBOARD_VALUE_CHARS = 20_000
MAX_TEAM_FIELD_CHARS = 120
MAX_TEAM_CONTENT_CHARS = 10_000
MAX_TEAM_STATUS_LIMIT = 50
MAX_AGENT_NAME_CHARS = 64
MAX_SUBAGENT_TASK_CHARS = 10_000
MAX_SUBAGENT_CONTEXT_CHARS = 20_000
MAX_SUBAGENT_FIELD_CHARS = 120
AGENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def create_subagent_tools(manager: Any) -> list[Tool]:
    return [
        DelegateTaskTool(manager),
        SpawnAgentTool(manager),
        DestroyAgentTool(manager),
        ListAgentsTool(manager),
        TeamSignalTool(manager),
        TeamStatusTool(manager),
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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="委派子任务",
            search_hint="delegate task subagent coder researcher browser",
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
        event_callback: LegacyEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        from naumi_agent.orchestrator.subagent_manager import SubTask

        try:
            task = _normalize_subagent_text(
                task,
                field_name="task",
                required=True,
                max_chars=MAX_SUBAGENT_TASK_CHARS,
            )
            agent = _normalize_agent_name(agent, field_name="agent", required=False)
            linked_task_id = _normalize_subagent_text(
                task_id,
                field_name="task_id",
                required=False,
                max_chars=MAX_SUBAGENT_FIELD_CHARS,
            )
            success_criteria = _normalize_subagent_text(
                success_criteria,
                field_name="success_criteria",
                required=False,
                max_chars=MAX_SUBAGENT_TASK_CHARS,
            )
            context = _normalize_subagent_text(
                context,
                field_name="context",
                required=False,
                max_chars=MAX_SUBAGENT_CONTEXT_CHARS,
            )
        except ValueError as e:
            return f"委派任务已拒绝: {e}"

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="创建子 Agent",
            search_hint="spawn create subagent role focus capabilities",
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
        try:
            name = _normalize_agent_name(name, field_name="name", required=True)
            task_description = _normalize_subagent_text(
                task_description,
                field_name="task_description",
                required=True,
                max_chars=MAX_SUBAGENT_TASK_CHARS,
            )
            role = _normalize_subagent_text(
                role,
                field_name="role",
                required=False,
                max_chars=MAX_SUBAGENT_FIELD_CHARS,
            ) or "expert_analyst"
            focus = _normalize_subagent_text(
                focus,
                field_name="focus",
                required=False,
                max_chars=MAX_SUBAGENT_FIELD_CHARS,
            )
        except ValueError as e:
            return f"创建 Agent 已拒绝: {e}"

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="销毁子 Agent",
            search_hint="destroy remove subagent cleanup dynamic agent",
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
        try:
            name = _normalize_agent_name(name, field_name="name", required=True)
        except ValueError as e:
            return f"销毁 Agent 已拒绝: {e}"

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="列出子 Agent",
            search_hint="list available subagents agents status lifecycle",
        )

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


class TeamSignalTool(Tool):
    """发布团队协议事件."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "team_signal"

    @property
    def description(self) -> str:
        return (
            "发布结构化团队协作事件，并同步到消息总线、共享黑板和用户界面。"
            "用于 handoff、decision、blocker、update、request、result 等团队协议。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="发布团队信号",
            search_hint="team signal handoff decision blocker update request result",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": ["handoff", "decision", "blocker", "update", "request", "result"],
                    "description": "团队事件类型。",
                },
                "sender": {
                    "type": "string",
                    "description": "发送方 Agent 名称。",
                },
                "content": {
                    "type": "string",
                    "description": "事件正文。",
                },
                "recipient": {
                    "type": "string",
                    "description": "接收方 Agent 名称；留空表示广播。",
                },
                "topic": {
                    "type": "string",
                    "description": "消息主题；留空自动使用 team.<event_type>。",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                    "description": "事件优先级。",
                    "default": "normal",
                },
                "task_id": {
                    "type": "string",
                    "description": "关联 todo 或子任务 ID（可选）。",
                },
                "blackboard_key": {
                    "type": "string",
                    "description": "写入共享黑板的 key；留空自动生成。",
                },
                "record_to_blackboard": {
                    "type": "boolean",
                    "description": "是否把团队事件写入共享黑板。",
                    "default": True,
                },
            },
            "required": ["event_type", "sender", "content"],
        }

    async def execute(
        self,
        *,
        event_type: str,
        sender: str,
        content: str,
        recipient: str = "",
        topic: str = "",
        priority: str = "normal",
        task_id: str = "",
        blackboard_key: str = "",
        record_to_blackboard: bool = True,
        event_callback: LegacyEventCallback | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            event_type = _normalize_team_text(
                event_type,
                field_name="event_type",
                required=True,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            sender = _normalize_team_text(
                sender,
                field_name="sender",
                required=True,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            content = _normalize_team_text(
                content,
                field_name="content",
                required=True,
                max_chars=MAX_TEAM_CONTENT_CHARS,
            )
            recipient = _normalize_team_text(
                recipient,
                field_name="recipient",
                required=False,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            topic = _normalize_team_text(
                topic,
                field_name="topic",
                required=False,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            priority = _normalize_team_text(
                priority,
                field_name="priority",
                required=True,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            task_id = _normalize_team_text(
                task_id,
                field_name="task_id",
                required=False,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            blackboard_key = _normalize_blackboard_key(
                blackboard_key,
                required=False,
            )
            if not isinstance(record_to_blackboard, bool):
                raise ValueError("record_to_blackboard 必须是布尔值。")
            result = await execute_team_signal(
                self._manager,
                event_type=event_type,
                sender=sender,
                content=content,
                recipient=recipient,
                topic=topic,
                priority=priority,
                task_id=task_id,
                blackboard_key=blackboard_key,
                record_to_blackboard=record_to_blackboard,
                event_callback=event_callback,
            )
        except ValueError as e:
            return f"团队信号已拒绝：{e}"
        return format_team_signal_result(result)


class TeamStatusTool(Tool):
    """读取团队协议状态."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "team_status"

    @property
    def description(self) -> str:
        return "查看团队协议状态，包括消息总览、指定 Agent 待处理消息和团队黑板。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="团队状态",
            search_hint="team status messages blackboard agents",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "要查看待处理消息的 Agent 名称（可选）。",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "最多显示多少条历史/黑板记录。",
                    "default": 10,
                },
            },
        }

    async def execute(self, *, agent: str = "", limit: int = 10, **kwargs: Any) -> str:
        try:
            agent = _normalize_team_text(
                agent,
                field_name="agent",
                required=False,
                max_chars=MAX_TEAM_FIELD_CHARS,
            )
            limit = _normalize_team_status_limit(limit)
        except ValueError as e:
            return f"团队状态已拒绝：{e}"
        return await execute_team_status(self._manager, agent=agent, limit=limit)


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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="读取共享状态板",
            search_hint="blackboard read shared state team agents",
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
        try:
            key = _normalize_blackboard_key(key, required=False)
        except ValueError as e:
            return f"读取共享状态已拒绝: {e}"

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="写入共享状态板",
            search_hint="blackboard write shared state team agents",
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
        try:
            key = _normalize_blackboard_key(key, required=True)
            value = _normalize_blackboard_value(value)
        except ValueError as e:
            return f"写入共享状态已拒绝: {e}"

        bus = self._manager.message_bus
        entry = await bus.blackboard_set(key, value, author="main_agent")
        return (
            f"✅ 已写入共享状态 '{key}' "
            f"(版本: {entry.version})"
        )


def _normalize_blackboard_key(key: Any, *, required: bool) -> str:
    """Validate blackboard key before reading or writing shared state."""
    if key is None:
        if required:
            raise ValueError("key 不能为空。")
        return ""
    if not isinstance(key, str):
        raise ValueError("key 必须是字符串。")
    normalized = key.strip()
    if not normalized:
        if required:
            raise ValueError("key 不能为空。")
        return ""
    if len(normalized) > MAX_BLACKBOARD_KEY_CHARS:
        raise ValueError(
            "key 过长，当前上限为 "
            f"{MAX_BLACKBOARD_KEY_CHARS} 个字符。"
        )
    if any(part == ".." for part in normalized.replace("\\", "/").split("/")):
        raise ValueError("key 不能包含路径越界片段。")
    return normalized


def _normalize_blackboard_value(value: Any) -> str:
    """Validate blackboard value before writing shared state."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value 不能为空，且必须是字符串。")
    normalized = value.strip()
    if len(normalized) > MAX_BLACKBOARD_VALUE_CHARS:
        raise ValueError(
            "value 过长，当前上限为 "
            f"{MAX_BLACKBOARD_VALUE_CHARS} 个字符。"
        )
    return normalized


def _normalize_team_text(
    value: Any,
    *,
    field_name: str,
    required: bool,
    max_chars: int,
) -> str:
    """Validate team protocol text fields at the tool boundary."""
    if value is None:
        if required:
            raise ValueError(f"{field_name} 不能为空。")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串。")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field_name} 不能为空。")
    if len(normalized) > max_chars:
        raise ValueError(f"{field_name} 过长，当前上限为 {max_chars} 个字符。")
    return normalized


def _normalize_team_status_limit(limit: Any) -> int:
    """Validate team status display limit."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是整数。")
    if limit < 1 or limit > MAX_TEAM_STATUS_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {MAX_TEAM_STATUS_LIMIT} 之间。")
    return limit


def _normalize_subagent_text(
    value: Any,
    *,
    field_name: str,
    required: bool,
    max_chars: int,
) -> str:
    """Validate subagent orchestration text fields at the tool boundary."""
    if value is None:
        if required:
            raise ValueError(f"{field_name} 不能为空。")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串。")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field_name} 不能为空。")
    if len(normalized) > max_chars:
        raise ValueError(f"{field_name} 过长，当前上限为 {max_chars} 个字符。")
    return normalized


def _normalize_agent_name(
    value: Any,
    *,
    field_name: str,
    required: bool,
) -> str | None:
    """Validate dynamic and routed subagent names before manager dispatch."""
    if value is None:
        if required:
            raise ValueError(f"{field_name} 不能为空。")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串。")
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError(f"{field_name} 不能为空。")
        return None
    if len(normalized) > MAX_AGENT_NAME_CHARS:
        raise ValueError(
            f"{field_name} 过长，当前上限为 {MAX_AGENT_NAME_CHARS} 个字符。"
        )
    if not AGENT_NAME_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} 只能使用字母开头，并包含字母、数字、下划线或连字符。"
        )
    return normalized


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
