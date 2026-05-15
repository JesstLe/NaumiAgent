"""Goal Pursuit Tool — autonomous long-running goal execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from naumi_agent.orchestrator.pursuit import GoalPursuitLoop, PursuitConfig
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

_global_pursuit_loop: GoalPursuitLoop | None = None


def set_pursuit_dependencies(
    router: Any,
    tool_registry: Any,
    subagent_manager: Any,
) -> None:
    """Inject dependencies needed by the pursuit tool."""
    global _global_pursuit_loop
    _global_pursuit_loop = GoalPursuitLoop(
        router=router,
        tool_registry=tool_registry,
        subagent_manager=subagent_manager,
    )


class PursueTool(Tool):
    """目标追踪循环 — 自主运行直至目标真正达成."""

    @property
    def name(self) -> str:
        return "pursue_goal"

    @property
    def description(self) -> str:
        return (
            "目标追踪：给定一个目标，自主循环执行（规划→行动→验证→评估）"
            "直到目标真正达成。适合需要长时间迭代、反复验证的复杂任务。"
            "Agent 会反复调用工具、评估进度、发现不足、调整策略。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "要达成的目标（自然语言描述）",
                },
            },
            "required": ["goal"],
        }

    async def execute(
        self,
        *,
        goal: str,
        **kwargs: Any,
    ) -> str:
        loop = _global_pursuit_loop
        if loop is None:
            return (
                "⚠️ 目标追踪工具尚未初始化。"
                "请在 Agent 启动后使用。"
            )

        config = PursuitConfig()

        # Create a fresh loop instance for this goal
        pursuit = GoalPursuitLoop(
            router=loop._router,
            tool_registry=loop._tools,
            subagent_manager=loop._manager,
            config=config,
        )

        try:
            report = await pursuit.pursue(goal)
            return report
        except asyncio.CancelledError:
            pursuit.cancel()
            return "⚠️ 目标追踪被用户取消。"
        except Exception as e:
            logger.exception("Pursuit loop error")
            return f"⚠️ 目标追踪异常: {type(e).__name__}: {e}"


def create_pursuit_tool() -> list[Tool]:
    return [PursueTool()]
