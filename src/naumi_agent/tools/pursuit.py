"""Goal Pursuit Tool — autonomous long-running goal execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from naumi_agent.orchestrator.pursuit import GoalPursuitLoop, PursuitConfig
from naumi_agent.orchestrator.pursuit_store import format_run, format_run_list
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

_global_pursuit_loop: GoalPursuitLoop | None = None


def set_pursuit_dependencies(
    router: Any,
    tool_registry: Any,
    subagent_manager: Any,
    store: Any | None = None,
) -> None:
    """Inject dependencies needed by the pursuit tool."""
    global _global_pursuit_loop
    _global_pursuit_loop = GoalPursuitLoop(
        router=router,
        tool_registry=tool_registry,
        subagent_manager=subagent_manager,
        store=store,
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
            store=loop._store,
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


class PursuitListTool(Tool):
    """列出持久化目标追踪运行."""

    @property
    def name(self) -> str:
        return "pursuit_list"

    @property
    def description(self) -> str:
        return "列出持久化的目标追踪运行，默认包含已完成记录。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "active_only": {
                    "type": "boolean",
                    "description": "是否只列出运行中/等待中的记录",
                    "default": False,
                }
            },
            "required": [],
        }

    async def execute(self, *, active_only: bool = False, **kwargs: Any) -> str:
        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        return format_run_list(loop.list_persisted_runs(include_finished=not active_only))


class PursuitStatusTool(Tool):
    """查看持久化目标追踪运行状态."""

    @property
    def name(self) -> str:
        return "pursuit_status"

    @property
    def description(self) -> str:
        return "查看一个目标追踪运行的状态、等待任务和最近证据。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "PursuitRun ID"}},
            "required": ["run_id"],
        }

    async def execute(self, *, run_id: str, **kwargs: Any) -> str:
        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        run = loop.get_persisted_run(run_id)
        if run is None:
            return f"错误：目标追踪运行不存在：{run_id}"
        return format_run(run)


class PursuitResumeTool(Tool):
    """恢复等待中的目标追踪状态."""

    @property
    def name(self) -> str:
        return "pursuit_resume"

    @property
    def description(self) -> str:
        return "恢复一个持久化目标追踪运行，并回收已完成后台任务的输出证据。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "PursuitRun ID"}},
            "required": ["run_id"],
        }

    async def execute(self, *, run_id: str, **kwargs: Any) -> str:
        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        return await loop.resume_persisted(run_id)


def create_pursuit_tool() -> list[Tool]:
    return [PursueTool(), PursuitListTool(), PursuitStatusTool(), PursuitResumeTool()]
