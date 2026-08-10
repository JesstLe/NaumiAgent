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
from naumi_agent.orchestrator.pursuit_terminal_retention import (
    render_terminal_outbox_retention_preview,
)
from naumi_agent.tools.base import Tool, ToolMetadata

if TYPE_CHECKING:
    from naumi_agent.orchestrator.pursuit import PursuitInteractionPort
    from naumi_agent.orchestrator.pursuit_lease import PursuitLeasePort
    from naumi_agent.orchestrator.pursuit_reconcile import BackgroundTaskLookup
    from naumi_agent.orchestrator.pursuit_terminal_dead_letter_abandon import (
        PursuitTerminalDeadLetterAbandonReceipt,
    )
    from naumi_agent.orchestrator.pursuit_terminal_dead_letter_action import (
        PursuitTerminalDeadLetterRequeueReceipt,
    )
    from naumi_agent.orchestrator.pursuit_terminal_outbox import (
        PursuitTerminalOutboxRunReceipt,
    )
    from naumi_agent.orchestrator.pursuit_terminal_retention import (
        PursuitTerminalOutboxRetentionPreview,
    )
    from naumi_agent.runtime.ports.model import ModelPort

logger = logging.getLogger(__name__)

_global_pursuit_loop: GoalPursuitLoop | None = None
_background_pursuit_tasks: set[asyncio.Task[str]] = set()
MAX_PURSUIT_GOAL_CHARS = 8_000
PURSUIT_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
PURSUIT_RECOVERY_ATTEMPT_ID_RE = re.compile(r"^recovery-[0-9a-f]{64}$")


def parse_terminal_outbox_retention_preview_args(
    tokens: list[str],
) -> dict[str, Any]:
    """Parse the shared CLI/TUI retention-preview option grammar."""
    option_names = {
        "--retention-days": "retention_days",
        "--limit": "limit",
        "--scan-limit": "scan_limit",
        "--assessed-at": "assessed_at",
    }
    parsed: dict[str, Any] = {
        "retention_days": 30,
        "limit": 20,
        "scan_limit": 100,
    }
    seen: set[str] = set()
    index = 0
    while index < len(tokens):
        option = tokens[index]
        field = option_names.get(option)
        if field is None:
            raise ValueError(f"未知 retention-preview 参数：{option}")
        if option in seen:
            raise ValueError(f"retention-preview 参数重复：{option}")
        if index + 1 >= len(tokens):
            raise ValueError(f"retention-preview 参数缺少值：{option}")
        value = tokens[index + 1]
        if value.startswith("--"):
            raise ValueError(f"retention-preview 参数缺少值：{option}")
        if field == "assessed_at":
            if not value or len(value) > 64:
                raise ValueError("--assessed-at 必须是最长 64 字符的 ISO 时间。")
            parsed[field] = value
        else:
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError(f"{option} 必须是十进制整数。")
            parsed[field] = int(value)
        seen.add(option)
        index += 2
    if not 1 <= parsed["retention_days"] <= 3_650:
        raise ValueError("--retention-days 必须在 1..3650 之间。")
    if not 1 <= parsed["limit"] <= 20:
        raise ValueError("--limit 必须在 1..20 之间。")
    if not parsed["limit"] <= parsed["scan_limit"] <= 100:
        raise ValueError("--scan-limit 必须在 limit..100 之间。")
    return parsed


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


class PursuitTerminalDeadLetterRequeueTool(Tool):
    """Requeue one exact authenticated terminal outbox dead letter."""

    def __init__(
        self,
        runner: Callable[
            [str, str],
            Awaitable[PursuitTerminalDeadLetterRequeueReceipt],
        ],
    ) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "pursuit_terminal_dead_letter_requeue"

    @property
    def description(self) -> str:
        return (
            "将一个审查目录中的精确 Pursuit 终态死信重新加入自动恢复队列；"
            "保留旧失败链，重置该记录的失败预算段，并返回不可变回执。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            requires_persistent_authorization=True,
            user_facing_name="重入队 Pursuit 终态死信",
            search_hint="pursuit terminal dead letter exact requeue receipt",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dead_letter_id": {
                    "type": "string",
                    "pattern": r"^ptfail_[0-9a-f]{24}$",
                    "description": "Goal 死信审查目录公开的精确目标 ID。",
                },
            },
            "required": ["dead_letter_id"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        dead_letter_id = str(kwargs.get("dead_letter_id") or "").strip()
        if not re.fullmatch(r"ptfail_[0-9a-f]{24}", dead_letter_id):
            raise ValueError("dead_letter_id 格式无效。")
        permission_receipt = current_permission_receipt()
        source_request_id = (
            permission_receipt.call_id
            if permission_receipt is not None
            else f"local-tool-{uuid.uuid4()}"
        )
        receipt = await self._runner(dead_letter_id, source_request_id)
        return "\n".join((
            "已将 Pursuit 终态死信重新加入自动恢复队列。",
            f"- 死信：`{receipt.dead_letter_id}`",
            f"- 回执：`{receipt.receipt_id}`",
            f"- 原失败序号：{receipt.failure_sequence}",
            "- 旧失败证据已保留；新的失败预算段从下一次领取开始。",
        ))


class PursuitTerminalDeadLetterAbandonTool(Tool):
    """Permanently abandon one exact authenticated terminal dead letter."""

    _REASONS = {
        "no_longer_required",
        "superseded",
        "external_resolution",
        "invalid_target",
    }

    def __init__(
        self,
        runner: Callable[
            [str, str, str],
            Awaitable[PursuitTerminalDeadLetterAbandonReceipt],
        ],
    ) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "pursuit_terminal_dead_letter_abandon"

    @property
    def description(self) -> str:
        return (
            "永久停止一个精确 Pursuit 终态死信的后续投递；保留失败链并签发"
            "不可变放弃回执，不会将其伪装为 delivered。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            requires_persistent_authorization=True,
            user_facing_name="放弃 Pursuit 终态死信",
            search_hint="pursuit terminal dead letter exact abandon receipt",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dead_letter_id": {
                    "type": "string",
                    "pattern": r"^ptfail_[0-9a-f]{24}$",
                    "description": "Goal 死信审查目录公开的精确目标 ID。",
                },
                "reason": {
                    "type": "string",
                    "enum": sorted(self._REASONS),
                    "description": "受控放弃原因，不接收可能泄密的自由文本。",
                },
            },
            "required": ["dead_letter_id", "reason"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        dead_letter_id = str(kwargs.get("dead_letter_id") or "").strip()
        reason = str(kwargs.get("reason") or "").strip()
        if not re.fullmatch(r"ptfail_[0-9a-f]{24}", dead_letter_id):
            raise ValueError("dead_letter_id 格式无效。")
        if reason not in self._REASONS:
            raise ValueError("dead-letter abandon reason 无效。")
        permission_receipt = current_permission_receipt()
        source_request_id = (
            permission_receipt.call_id
            if permission_receipt is not None
            else f"local-tool-{uuid.uuid4()}"
        )
        receipt = await self._runner(dead_letter_id, source_request_id, reason)
        return "\n".join((
            "已永久停止该 Pursuit 终态死信的后续投递。",
            f"- 死信：`{receipt.dead_letter_id}`",
            f"- 回执：`{receipt.receipt_id}`",
            f"- 原因：`{receipt.reason.value}`",
            "- 旧失败证据已保留；该动作未声明 delivered。",
        ))


class PursuitTerminalOutboxRetentionPreviewTool(Tool):
    """Preview old authenticated abandoned outboxes without mutating them."""

    def __init__(
        self,
        runner: Callable[
            [int, int, int, str | None],
            Awaitable[PursuitTerminalOutboxRetentionPreview],
        ],
    ) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "pursuit_terminal_outbox_retention_preview"

    @property
    def description(self) -> str:
        return (
            "只读预演超过保留期的 authenticated abandoned Pursuit 终态 outbox；"
            "认证每条保护引用并签发防篡改 preview，不删除或归档任何记录。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="预演 Pursuit 终态 Outbox 保留策略",
            search_hint=(
                "pursuit terminal outbox retention preview abandoned protection refs"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "retention_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                    "default": 30,
                    "description": "仅选择已处置时间不晚于该保留期截止点的记录。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 20,
                    "description": "本轮最多返回的认证候选数。",
                },
                "scan_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                    "description": "本轮最多扫描的年龄合格记录数，不能小于 limit。",
                },
                "assessed_at": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "可选的带时区 ISO 时间，仅用于可复现审计。",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        retention_days = kwargs.get("retention_days", 30)
        limit = kwargs.get("limit", 20)
        scan_limit = kwargs.get("scan_limit", 100)
        assessed_at = kwargs.get("assessed_at")
        for value, name, lower, upper in (
            (retention_days, "retention_days", 1, 3_650),
            (limit, "limit", 1, 20),
            (scan_limit, "scan_limit", 1, 100),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数。")
            if not lower <= value <= upper:
                raise ValueError(f"{name} 必须在 {lower}..{upper} 之间。")
        if scan_limit < limit:
            raise ValueError("scan_limit 不能小于 limit。")
        if assessed_at is not None:
            if not isinstance(assessed_at, str) or not assessed_at.strip():
                raise ValueError("assessed_at 必须是带时区 ISO 时间。")
            assessed_at = assessed_at.strip()
            if len(assessed_at) > 64:
                raise ValueError("assessed_at 最长 64 个字符。")
        preview = await self._runner(
            retention_days,
            limit,
            scan_limit,
            assessed_at,
        )
        return render_terminal_outbox_retention_preview(preview)


def create_pursuit_tool(
    *,
    terminal_outbox_runner: (
        Callable[[str], Awaitable[PursuitTerminalOutboxRunReceipt]] | None
    ) = None,
    terminal_outbox_enabled: bool = False,
    terminal_dead_letter_requeue: (
        Callable[
            [str, str],
            Awaitable[PursuitTerminalDeadLetterRequeueReceipt],
        ]
        | None
    ) = None,
    terminal_dead_letter_abandon: (
        Callable[
            [str, str, str],
            Awaitable[PursuitTerminalDeadLetterAbandonReceipt],
        ]
        | None
    ) = None,
    terminal_outbox_retention_preview: (
        Callable[
            [int, int, int, str | None],
            Awaitable[PursuitTerminalOutboxRetentionPreview],
        ]
        | None
    ) = None,
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
    if terminal_dead_letter_requeue is not None:
        tools.append(PursuitTerminalDeadLetterRequeueTool(
            terminal_dead_letter_requeue,
        ))
    if terminal_dead_letter_abandon is not None:
        tools.append(PursuitTerminalDeadLetterAbandonTool(
            terminal_dead_letter_abandon,
        ))
    if terminal_outbox_retention_preview is not None:
        tools.append(PursuitTerminalOutboxRetentionPreviewTool(
            terminal_outbox_retention_preview,
        ))
    return tools
