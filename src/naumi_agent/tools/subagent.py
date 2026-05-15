"""子 Agent 工具 — 让主 LLM 可以委派任务给专用 Agent."""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


def create_subagent_tools(manager: Any) -> list[Tool]:
    return [
        DelegateTaskTool(manager),
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
            },
            "required": ["task"],
        }

    async def execute(self, *, task: str, agent: str | None = None, **kwargs: Any) -> str:
        from naumi_agent.orchestrator.subagent_manager import SubTask

        subtask = SubTask(
            id=f"sub_{task[:20]}",
            description=task,
            agent_name=agent,
        )

        try:
            result = await self._manager.delegate(subtask)
            if result.status == "completed":
                return result.response
            return f"子任务失败 ({result.status}): {result.error or result.response[:500]}"
        except Exception as e:
            return f"委派任务出错: {type(e).__name__}: {e}"


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
            lines.append(f"  - {a['name']}: {a['description']}")
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
