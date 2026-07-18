"""Goal Pursuit Tool — autonomous long-running goal execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from naumi_agent.orchestrator.pursuit import GoalPursuitLoop, PursuitConfig, ToolExecutor
from naumi_agent.orchestrator.pursuit_store import format_run, format_run_list
from naumi_agent.tools.base import Tool, ToolMetadata

if TYPE_CHECKING:
    from naumi_agent.orchestrator.pursuit_lease import PursuitLeasePort
    from naumi_agent.orchestrator.pursuit_reconcile import BackgroundTaskLookup
    from naumi_agent.runtime.ports.model import ModelPort

logger = logging.getLogger(__name__)

_global_pursuit_loop: GoalPursuitLoop | None = None
_background_pursuit_tasks: set[asyncio.Task[str]] = set()
MAX_PURSUIT_GOAL_CHARS = 8_000
PURSUIT_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


def set_pursuit_dependencies(
    router: ModelPort,
    tool_registry: Any,
    subagent_manager: Any,
    store: Any | None = None,
    execute_tool_call: ToolExecutor | None = None,
    lease_port: PursuitLeasePort | None = None,
    workspace_root: str | Path | None = None,
    background_reconcile_source: BackgroundTaskLookup | None = None,
) -> None:
    """Inject dependencies needed by the pursuit tool."""
    global _global_pursuit_loop
    _global_pursuit_loop = GoalPursuitLoop(
        router=router,
        tool_registry=tool_registry,
        subagent_manager=subagent_manager,
        store=store,
        execute_tool_call=execute_tool_call,
        lease_port=lease_port,
        workspace_root=workspace_root,
        background_reconcile_source=background_reconcile_source,
    )


def _normalize_goal(goal: Any) -> str:
    text = str(goal or "").strip()
    if not text:
        raise ValueError("目标不能为空。")
    if len(text) > MAX_PURSUIT_GOAL_CHARS:
        raise ValueError(f"目标过长，最多 {MAX_PURSUIT_GOAL_CHARS} 个字符。")
    return text


def _normalize_run_id(run_id: Any) -> str:
    text = str(run_id or "").strip()
    if not text:
        raise ValueError("run_id 不能为空。")
    if not PURSUIT_RUN_ID_RE.fullmatch(text):
        raise ValueError("run_id 只能包含字母、数字、下划线、点、冒号或连字符。")
    return text


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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="追踪目标",
            search_hint="pursuit goal autonomous loop plan act verify long running",
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
        try:
            normalized_goal = _normalize_goal(goal)
        except ValueError as e:
            return f"⚠️ 目标追踪输入无效：{e}"

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
            execute_tool_call=loop._execute_tool_call,
            lease_port=loop._lease_port,
            workspace_root=loop._workspace_root,
            background_reconcile_source=loop._background_reconcile_source,
        )

        try:
            task = asyncio.create_task(
                pursuit.pursue(normalized_goal),
                name="naumi-pursuit-goal",
            )
            _background_pursuit_tasks.add(task)
            task.add_done_callback(_background_pursuit_tasks.discard)
            task.add_done_callback(_log_background_pursuit_result)
            startup_error = await pursuit.wait_until_started()
            if startup_error:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                return f"⚠️ {startup_error}"
            run_id = pursuit._run.id if pursuit._run is not None else "启动中"
            return (
                "✅ 目标追踪已在后台启动，界面不会等待长循环完成。\n\n"
                f"- run_id: `{run_id}`\n"
                f"- 查看状态: `/pursue status {run_id}`\n"
                "- 查看列表: `/pursue list`\n"
                f"- 默认上限: {_format_pursuit_limits(config)}\n\n"
                "追踪循环会持续记录证据；如果进入 waiting 或 blocked，"
                "可以用状态命令查看原因。"
            )
        except asyncio.CancelledError:
            pursuit.cancel()
            return "⚠️ 目标追踪被用户取消。"
        except Exception as e:
            logger.exception("Pursuit loop error")
            return f"⚠️ 目标追踪异常: {type(e).__name__}: {e}"


def _log_background_pursuit_result(task: asyncio.Task[str]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("Background pursuit loop failed: %s", e, exc_info=True)


def _format_pursuit_limits(config: PursuitConfig) -> str:
    def number(value: float | int, suffix: str) -> str:
        return "无限" if value == float("inf") else f"{value:.0f}{suffix}"

    budget = (
        "无限"
        if config.max_budget_usd == float("inf")
        else f"${config.max_budget_usd:.2f}"
    )
    return (
        f"{number(config.max_iterations, ' 轮')} / "
        f"{number(config.max_time_seconds, ' 秒')} / {budget}"
    )


class PursuitListTool(Tool):
    """列出持久化目标追踪运行."""

    @property
    def name(self) -> str:
        return "pursuit_list"

    @property
    def description(self) -> str:
        return "列出持久化的目标追踪运行，默认包含已完成记录。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="目标追踪列表",
            search_hint="pursuit list runs persisted active status",
        )

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="目标追踪状态",
            search_hint="pursuit status run evidence waiting",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "PursuitRun ID"}},
            "required": ["run_id"],
        }

    async def execute(self, *, run_id: str, **kwargs: Any) -> str:
        try:
            normalized_run_id = _normalize_run_id(run_id)
        except ValueError as e:
            return f"错误：{e}"

        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        run = loop.get_persisted_run(normalized_run_id)
        if run is None:
            return f"错误：目标追踪运行不存在：{normalized_run_id}"
        return format_run(run)


class PursuitResumeTool(Tool):
    """从权威 checkpoint 恢复目标追踪执行."""

    @property
    def name(self) -> str:
        return "pursuit_resume"

    @property
    def description(self) -> str:
        return (
            "校验并恢复持久化目标追踪：回收后台证据，在安全 checkpoint "
            "继续执行；存在未核对副作用时停止重放。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="恢复目标追踪",
            search_hint="pursuit resume persisted run background evidence",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "PursuitRun ID"}},
            "required": ["run_id"],
        }

    async def execute(self, *, run_id: str, **kwargs: Any) -> str:
        try:
            normalized_run_id = _normalize_run_id(run_id)
        except ValueError as e:
            return f"错误：{e}"

        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        loop.prepare_resume_admission()
        task = asyncio.create_task(
            loop.resume_persisted(normalized_run_id),
            name=f"naumi-pursuit-resume-{normalized_run_id}",
        )
        admission = asyncio.create_task(
            loop.wait_until_resume_admitted(),
            name=f"naumi-pursuit-resume-admission-{normalized_run_id}",
        )
        _background_pursuit_tasks.add(task)
        task.add_done_callback(_background_pursuit_tasks.discard)
        task.add_done_callback(_log_background_pursuit_result)
        done, _ = await asyncio.wait(
            {task, admission},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            admission.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await admission
            return task.result()

        admission_error = admission.result()
        if admission_error:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return f"⚠️ {admission_error}"
        return (
            "✅ 目标追踪已恢复并在后台继续，当前对话不会等待长循环完成。\n\n"
            f"- run_id: `{normalized_run_id}`\n"
            f"- checkpoint: `{loop._resume_checkpoint_id}`\n"
            f"- lease epoch: {loop._resume_epoch}\n"
            f"- 查看状态: `/pursue status {normalized_run_id}`"
        )


def create_pursuit_tool() -> list[Tool]:
    return [PursueTool(), PursuitListTool(), PursuitStatusTool(), PursuitResumeTool()]
