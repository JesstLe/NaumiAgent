"""子 Agent 工具 — 让主 LLM 可以委派任务给专用 Agent."""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


def create_subagent_tools(manager: Any) -> list[Tool]:
    return [DelegateTaskTool(manager), ListAgentsTool(manager)]


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
