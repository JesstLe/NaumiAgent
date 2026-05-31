"""Harness context assembly for each agent turn."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from naumi_agent.background.models import BackgroundStatus
from naumi_agent.scheduler.models import ScheduleStatus
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.worktree.models import WorktreeRecord

if TYPE_CHECKING:
    from naumi_agent.background.runner import BackgroundRunner
    from naumi_agent.mcp.client import MCPClientManager
    from naumi_agent.orchestrator.pursuit_store import PursuitStore
    from naumi_agent.scheduler.runner import SchedulerRunner
    from naumi_agent.skills.loader import SkillLoader
    from naumi_agent.tasks.store import TaskStore
    from naumi_agent.tools.base import ToolRegistry
    from naumi_agent.worktree.manager import WorktreeManager

logger = logging.getLogger(__name__)

HARNESS_CONTEXT_MARKER = "<naumi_harness_context>"
_MAX_LINES_PER_SECTION = 8


@dataclass(frozen=True)
class HarnessContextInput:
    """Dependencies used to assemble one turn-scoped harness snapshot."""

    tool_registry: ToolRegistry
    skill_loader: SkillLoader
    task_store: TaskStore
    background_runner: BackgroundRunner
    scheduler_runner: SchedulerRunner
    worktree_manager: WorktreeManager
    pursuit_store: PursuitStore
    mcp_manager: MCPClientManager | None
    context_info: dict[str, Any]
    budget_info: dict[str, Any]


class HarnessContextAssembler:
    """Build a compact, current-state snapshot for the model."""

    async def assemble(self, data: HarnessContextInput) -> str:
        sections = [
            "## Harness 状态快照",
            "这是每轮自动生成的运行环境快照，用来帮助你选择下一步工具和恢复长期任务。",
            self._tool_section(data.tool_registry),
            self._skill_section(data.skill_loader),
            await self._task_section(data.task_store),
            self._background_section(data.background_runner),
            self._scheduler_section(data.scheduler_runner),
            await self._worktree_section(data.worktree_manager),
            self._pursuit_section(data.pursuit_store),
            self._mcp_section(data.mcp_manager),
            self._budget_section(data.context_info, data.budget_info),
        ]
        body = "\n\n".join(section for section in sections if section.strip())
        return f"{HARNESS_CONTEXT_MARKER}\n{body}\n{HARNESS_CONTEXT_MARKER}"

    def _tool_section(self, registry: ToolRegistry) -> str:
        names = sorted(registry.names)
        families = Counter(_tool_family(name) for name in names)
        family_text = ", ".join(
            f"{family}:{count}" for family, count in sorted(families.items())
        )
        sample = ", ".join(names[:24])
        if len(names) > 24:
            sample += f", ... +{len(names) - 24}"
        return (
            "### 工具池\n"
            f"- 已注册工具：{len(names)} 个\n"
            f"- 领域分布：{family_text or '无'}\n"
            f"- 可见样例：{sample or '无'}"
        )

    def _skill_section(self, loader: SkillLoader) -> str:
        skills = sorted(loader.all(), key=lambda item: item.name)
        if not skills:
            return "### Skills\n- 当前未加载 skill。"
        lines = [
            f"- {skill.name}: {skill.description[:100] or '无描述'}"
            for skill in skills[:_MAX_LINES_PER_SECTION]
        ]
        if len(skills) > _MAX_LINES_PER_SECTION:
            lines.append(f"- ... 还有 {len(skills) - _MAX_LINES_PER_SECTION} 个")
        return "### Skills\n" + "\n".join(lines)

    async def _task_section(self, store: TaskStore) -> str:
        try:
            tasks = await store.list_tasks()
        except Exception as e:
            logger.debug("Task context assembly failed: %s", e)
            return "### 任务图\n- 任务状态读取失败。"
        if not tasks:
            return "### 任务图\n- 当前会话没有持久任务。"
        counts = Counter(task.status for task in tasks)
        lines = [
            "- 汇总："
            f"{counts[TaskStatus.COMPLETED]} 完成，"
            f"{counts[TaskStatus.IN_PROGRESS]} 进行中，"
            f"{counts[TaskStatus.PENDING]} 待处理"
        ]
        for task in tasks[:_MAX_LINES_PER_SECTION]:
            owner = f" owner={task.owner}" if task.owner else ""
            blocked = f" blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(f"- #{task.id} [{task.status.value}] {task.subject}{owner}{blocked}")
        if len(tasks) > _MAX_LINES_PER_SECTION:
            lines.append(f"- ... 还有 {len(tasks) - _MAX_LINES_PER_SECTION} 个")
        return "### 任务图\n" + "\n".join(lines)

    def _background_section(self, runner: BackgroundRunner) -> str:
        tasks = runner.list_tasks()
        if not tasks:
            return "### 后台任务\n- 当前没有后台任务。"
        counts = Counter(task.status for task in tasks)
        interesting = [
            task for task in tasks
            if task.status == BackgroundStatus.RUNNING or not task.notified
        ][: _MAX_LINES_PER_SECTION]
        lines = [
            "- 汇总："
            f"{counts[BackgroundStatus.RUNNING]} 运行中，"
            f"{counts[BackgroundStatus.COMPLETED]} 已完成，"
            f"{counts[BackgroundStatus.FAILED]} 失败，"
            f"{counts[BackgroundStatus.TIMED_OUT]} 超时"
        ]
        for task in interesting:
            lines.append(f"- {task.id} [{task.status.value}] {task.command[:120]}")
        return "### 后台任务\n" + "\n".join(lines)

    def _scheduler_section(self, runner: SchedulerRunner) -> str:
        jobs = runner.list_jobs(include_inactive=False)
        if not jobs:
            return "### 调度任务\n- 当前没有启用中的调度任务。"
        active = [job for job in jobs if job.status == ScheduleStatus.ACTIVE]
        lines = [f"- 启用中：{len(active)} 个"]
        for job in active[:_MAX_LINES_PER_SECTION]:
            lines.append(
                f"- {job.id} [{job.kind.value}] "
                f"下次 {job.next_fire_at}: {job.prompt[:100]}"
            )
        if len(active) > _MAX_LINES_PER_SECTION:
            lines.append(f"- ... 还有 {len(active) - _MAX_LINES_PER_SECTION} 个")
        return "### 调度任务\n" + "\n".join(lines)

    async def _worktree_section(self, manager: WorktreeManager) -> str:
        try:
            status = await manager.status()
        except Exception as e:
            logger.debug("Worktree context assembly failed: %s", e)
            return "### Worktree\n- worktree 状态读取失败。"
        records = status if isinstance(status, list) else [status]
        if not records:
            return "### Worktree\n- 当前没有 Naumi 管理的隔离 worktree。"
        lines = []
        for record in records[:_MAX_LINES_PER_SECTION]:
            lines.append(_format_worktree_record(record))
        if len(records) > _MAX_LINES_PER_SECTION:
            lines.append(f"- ... 还有 {len(records) - _MAX_LINES_PER_SECTION} 个")
        return "### Worktree\n" + "\n".join(lines)

    def _pursuit_section(self, store: PursuitStore) -> str:
        try:
            runs = store.list_runs(include_finished=False)
        except Exception as e:
            logger.debug("Pursuit context assembly failed: %s", e)
            return "### Pursuit\n- 目标追踪状态读取失败。"
        if not runs:
            return "### Pursuit\n- 当前没有运行中或等待中的目标追踪。"
        lines = []
        for run in runs[:_MAX_LINES_PER_SECTION]:
            wait_count = len(run.waiting_on or [])
            lines.append(
                f"- {run.id} [{run.status.value}] {run.phase} "
                f"criteria={run.criteria_verified}/{run.criteria_total} "
                f"waiting={wait_count}: {run.goal[:100]}"
            )
        if len(runs) > _MAX_LINES_PER_SECTION:
            lines.append(f"- ... 还有 {len(runs) - _MAX_LINES_PER_SECTION} 个")
        return "### Pursuit\n" + "\n".join(lines)

    def _mcp_section(self, manager: MCPClientManager | None) -> str:
        if manager is None:
            return "### MCP\n- 当前没有已连接 MCP server。"
        sessions = getattr(manager, "_sessions", {})
        tool_to_server = getattr(manager, "_tool_to_server", {})
        if not sessions:
            return "### MCP\n- 当前没有已连接 MCP server。"
        names = sorted(str(name) for name in sessions)
        counts = Counter(str(server) for server in tool_to_server.values())
        details = ", ".join(f"{name}:{counts[name]} tools" for name in names)
        return "### MCP\n" + f"- 已连接：{details}"

    def _budget_section(
        self,
        context_info: dict[str, Any],
        budget_info: dict[str, Any],
    ) -> str:
        return (
            "### 资源\n"
            f"- 上下文：{context_info.get('used', 0)}/"
            f"{context_info.get('window', 0)} tokens "
            f"({context_info.get('percentage', 0)}%)\n"
            f"- 预算：${budget_info.get('used_usd', 0):.4f}/"
            f"${budget_info.get('max_usd', 0):.2f} "
            f"({budget_info.get('percentage', 0)}%)"
        )


def is_harness_context_message(message: dict[str, Any]) -> bool:
    return (
        message.get("role") == "system"
        and HARNESS_CONTEXT_MARKER in str(message.get("content", ""))
    )


def _tool_family(name: str) -> str:
    for prefix in (
        "browser_daemon",
        "browser",
        "background",
        "schedule",
        "worktree",
        "pursuit",
        "task",
        "memory",
        "mcp",
        "file",
    ):
        if name.startswith(prefix):
            return prefix
    return name.split("_", 1)[0] if "_" in name else "other"


def _format_worktree_record(record: WorktreeRecord) -> str:
    task = f" task=#{record.task_id}" if record.task_id else ""
    return (
        f"- {record.name} [{record.status.value}] "
        f"dirty={record.dirty_files} commits={record.commits_ahead}{task} "
        f"path={record.path}"
    )
