"""Goal Pursuit Tool — autonomous long-running goal execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from naumi_agent.daemons.permission_context import current_permission_receipt
from naumi_agent.orchestrator.pursuit import GoalPursuitLoop, PursuitConfig, ToolExecutor
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    format_recovery_attempts,
    pursuit_recovery_attempt_id,
)
from naumi_agent.orchestrator.pursuit_recovery_reconcile import (
    format_pursuit_reconcile_result,
)
from naumi_agent.orchestrator.pursuit_store import format_run, format_run_list
from naumi_agent.tools.base import Tool, ToolMetadata

if TYPE_CHECKING:
    from naumi_agent.orchestrator.pursuit import PursuitInteractionPort
    from naumi_agent.orchestrator.pursuit_lease import PursuitLeasePort
    from naumi_agent.orchestrator.pursuit_reconcile import BackgroundTaskLookup
    from naumi_agent.orchestrator.pursuit_terminal_outbox import (
        PursuitTerminalOutboxRunReceipt,
    )
    from naumi_agent.runtime.ports.model import ModelPort

logger = logging.getLogger(__name__)

_global_pursuit_loop: GoalPursuitLoop | None = None
_background_pursuit_tasks: set[asyncio.Task[str]] = set()
MAX_PURSUIT_GOAL_CHARS = 8_000
PURSUIT_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
PURSUIT_RECOVERY_ATTEMPT_ID_RE = re.compile(r"^recovery-[0-9a-f]{64}$")


def set_pursuit_dependencies(
    router: ModelPort,
    tool_registry: Any,
    subagent_manager: Any,
    store: Any | None = None,
    execute_tool_call: ToolExecutor | None = None,
    lease_port: PursuitLeasePort | None = None,
    workspace_root: str | Path | None = None,
    background_reconcile_source: BackgroundTaskLookup | None = None,
    interaction_port: PursuitInteractionPort | None = None,
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
        interaction_port=interaction_port,
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


def _normalize_recovery_attempt_id(attempt_id: Any) -> str:
    text = str(attempt_id or "").strip()
    if not text:
        raise ValueError("recovery attempt_id 不能为空。")
    if not PURSUIT_RECOVERY_ATTEMPT_ID_RE.fullmatch(text):
        raise ValueError(
            "recovery attempt_id 必须是 `recovery-` 加 64 位小写十六进制摘要。"
        )
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
            interaction_port=loop._interaction_port,
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
        attempts = loop.list_recovery_attempts(normalized_run_id, limit=5)
        return format_run(run) + "\n\n" + format_recovery_attempts(attempts)


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
            requires_persistent_authorization=True,
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
        permission_receipt = current_permission_receipt()
        source_request_id = (
            permission_receipt.call_id
            if permission_receipt is not None
            else f"local-tool-{uuid.uuid4()}"
        )
        recovery_attempt_id = pursuit_recovery_attempt_id(
            run_id=normalized_run_id,
            source_request_id=source_request_id,
        )
        task = asyncio.create_task(
            loop.resume_persisted(
                normalized_run_id,
                source_request_id=source_request_id,
            ),
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
            return _with_persisted_recovery_attempt(
                task.result(),
                loop=loop,
                attempt_id=recovery_attempt_id,
            )

        admission_error = admission.result()
        if admission_error:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return _with_persisted_recovery_attempt(
                f"⚠️ {admission_error}",
                loop=loop,
                attempt_id=recovery_attempt_id,
            )
        return _with_persisted_recovery_attempt(
            (
                "✅ 目标追踪已恢复并在后台继续，当前对话不会等待长循环完成。\n\n"
                f"- run_id: `{normalized_run_id}`\n"
                f"- checkpoint: `{loop._resume_checkpoint_id}`\n"
                f"- lease epoch: {loop._resume_epoch}\n"
                f"- 查看状态: `/pursue status {normalized_run_id}`"
            ),
            loop=loop,
            attempt_id=recovery_attempt_id,
        )


class PursuitReconcileTool(Tool):
    """对因进程中断停留在 admitted 的恢复请求进行机械收口."""

    @property
    def name(self) -> str:
        return "pursuit_reconcile"

    @property
    def description(self) -> str:
        return (
            "对账一个持久恢复请求：先以更高 RunLease epoch 栅栏旧执行者，"
            "再复验准入后的 checkpoint、机械裁判与运行终态；证据不足时不修改。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            requires_persistent_authorization=True,
            user_facing_name="对账目标追踪恢复请求",
            search_hint=(
                "pursuit reconcile admitted recovery attempt fence checkpoint"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "attempt_id": {
                    "type": "string",
                    "description": "recovery- 开头的恢复请求 ID",
                    "pattern": r"^recovery-[0-9a-f]{64}$",
                },
            },
            "required": ["attempt_id"],
        }

    async def execute(self, *, attempt_id: str, **kwargs: Any) -> str:
        try:
            normalized_attempt_id = _normalize_recovery_attempt_id(attempt_id)
        except ValueError as exc:
            return f"错误：{exc}"
        loop = _global_pursuit_loop
        if loop is None:
            return "⚠️ 目标追踪工具尚未初始化。"
        result = await loop.reconcile_recovery_attempt(normalized_attempt_id)
        return format_pursuit_reconcile_result(result)


def _with_persisted_recovery_attempt(
    result: str,
    *,
    loop: GoalPursuitLoop,
    attempt_id: str,
) -> str:
    if not attempt_id or attempt_id in result:
        return result
    try:
        persisted = loop.get_recovery_attempt(attempt_id)
    except Exception:
        logger.exception(
            "Failed to verify Pursuit recovery attempt [%s]",
            attempt_id,
        )
        return result
    if persisted is None:
        return result
    return result.rstrip() + f"\n\n- recovery attempt: `{attempt_id}`"


class PursuitTerminalOutboxRunNowTool(Tool):
    """Run one explicit, bounded terminal publication recovery pass."""

    def __init__(
        self,
        runner: Callable[[str], Awaitable[PursuitTerminalOutboxRunReceipt]],
        *,
        enabled: bool,
    ) -> None:
        self._runner = runner
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "pursuit_terminal_outbox_run_now"

    @property
    def description(self) -> str:
        return (
            "立即执行一轮有界 Pursuit 终态 outbox 恢复；只处理已到期或 claim "
            "已过期的记录，不绕过退避与 fencing，并返回持久化回执。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            requires_persistent_authorization=True,
            user_facing_name="立即恢复 Pursuit 终态队列",
            search_hint="pursuit terminal outbox recovery run now receipt",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        if not self._enabled:
            raise RuntimeError("Pursuit 终态 outbox worker 当前未启用。")
        permission_receipt = current_permission_receipt()
        source_request_id = (
            permission_receipt.call_id
            if permission_receipt is not None
            else f"local-tool-{uuid.uuid4()}"
        )
        receipt = await self._runner(source_request_id)
        status_label = {
            "completed": "已完成",
            "partial": "部分完成",
            "no_due": "暂无到期记录",
            "failed": "执行失败",
        }[receipt.status.value]
        return "\n".join((
            f"{status_label}：Pursuit 终态队列有界恢复。",
            f"- 回执：`{receipt.receipt_id}`",
            f"- 积压：{receipt.pending_before} → {receipt.pending_after}",
            (
                f"- 处理：claimed {receipt.claimed} · delivered {receipt.delivered} · "
                f"retry {receipt.retry_scheduled} · failed {receipt.failures}"
            ),
        ))


def create_pursuit_tool(
    *,
    terminal_outbox_runner: (
        Callable[[str], Awaitable[PursuitTerminalOutboxRunReceipt]] | None
    ) = None,
    terminal_outbox_enabled: bool = False,
) -> list[Tool]:
    tools: list[Tool] = [
        PursueTool(),
        PursuitListTool(),
        PursuitStatusTool(),
        PursuitResumeTool(),
        PursuitReconcileTool(),
    ]
    if terminal_outbox_runner is not None:
        tools.append(PursuitTerminalOutboxRunNowTool(
            terminal_outbox_runner,
            enabled=terminal_outbox_enabled,
        ))
    return tools
