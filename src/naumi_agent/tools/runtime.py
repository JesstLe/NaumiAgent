"""Agent-facing runtime status tool."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from naumi_agent.background.models import BackgroundStatus
from naumi_agent.scheduler.models import ScheduleStatus
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tools.base import Tool

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

_ALL_SECTIONS = {
    "context",
    "todo",
    "team",
    "subagent",
    "hooks",
    "resources",
    "recommendations",
}


def create_runtime_tools(engine: AgentEngine) -> list[Tool]:
    return [RuntimeStatusTool(engine)]


async def build_runtime_status(
    engine: AgentEngine,
    *,
    sections: str = "all",
    limit: int = 8,
) -> str:
    """Build a deterministic runtime snapshot for the agent and manual users."""
    selected = _select_sections(sections)
    safe_limit = max(1, min(int(limit or 8), 50))
    collector = _RuntimeSnapshot(engine, safe_limit)
    blocks: list[str] = ["## Runtime 状态"]

    if "context" in selected:
        blocks.append(collector.context_section())
    if "todo" in selected:
        blocks.append(await collector.todo_section())
    if "team" in selected:
        blocks.append(await collector.team_section())
    if "subagent" in selected:
        blocks.append(collector.subagent_section())
    if "hooks" in selected:
        blocks.append(collector.hooks_section())
    if "resources" in selected:
        blocks.append(collector.resources_section())
    if "recommendations" in selected:
        blocks.append(await collector.recommendations_section())

    return "\n\n".join(block for block in blocks if block.strip())


async def run_runtime_command(engine: AgentEngine, arg: str) -> str:
    """Execute /runtime using the same implementation as runtime_status."""
    parts = arg.strip().split()
    if not parts:
        return await build_runtime_status(engine)
    if parts[0] in {"help", "-h", "--help"}:
        return (
            "用法：/runtime [all|context,todo,team,subagent,hooks,resources,recommendations] "
            "[limit]\n"
            "示例：/runtime team,todo 12"
        )
    sections = parts[0]
    limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8
    return await build_runtime_status(engine, sections=sections, limit=limit)


class RuntimeStatusTool(Tool):
    """读取当前运行时状态."""

    def __init__(self, engine: AgentEngine) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "runtime_status"

    @property
    def description(self) -> str:
        return (
            "读取当前 Agent 运行时状态快照，包含上下文压力、todo、team protocol、"
            "subagent、hook、后台/调度资源和下一步建议。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "string",
                    "description": (
                        "要读取的分区：all 或逗号分隔的 "
                        "context,todo,team,subagent,hooks,resources,recommendations"
                    ),
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "每个分区最多显示多少条明细。",
                    "default": 8,
                },
            },
        }

    async def execute(
        self,
        *,
        sections: str = "all",
        limit: int = 8,
        **kwargs: Any,
    ) -> str:
        return await build_runtime_status(self._engine, sections=sections, limit=limit)


class _RuntimeSnapshot:
    def __init__(self, engine: AgentEngine, limit: int) -> None:
        self.engine = engine
        self.limit = limit

    def context_section(self) -> str:
        context = self.engine.get_context_info()
        budget = self.engine.get_budget_info()
        config = self.engine._config
        return (
            "### 上下文与预算\n"
            f"- 上下文：{context.get('used', 0)}/{context.get('window', 0)} tokens "
            f"({context.get('percentage', 0)}%)\n"
            f"- 预算：${budget.get('used_usd', 0):.4f}/"
            f"${budget.get('max_usd', 0):.2f} "
            f"({budget.get('percentage', 0)}%)\n"
            f"- 权限模式：{config.safety.permission_mode}\n"
            f"- 工作区：{self.engine.workspace_root}"
        )

    async def todo_section(self) -> str:
        try:
            tasks = await self.engine.task_store.list_tasks()
        except Exception as e:
            return f"### Todo\n- 读取失败：{type(e).__name__}: {e}"
        if not tasks:
            return "### Todo\n- 当前会话没有持久 todo。"
        counts = Counter(task.status for task in tasks)
        lines = [
            "### Todo",
            "- 汇总："
            f"{counts[TaskStatus.COMPLETED]} 完成，"
            f"{counts[TaskStatus.IN_PROGRESS]} 进行中，"
            f"{counts[TaskStatus.BLOCKED]} 阻塞，"
            f"{counts[TaskStatus.PENDING]} 待处理",
        ]
        for task in tasks[: self.limit]:
            active = f" | 当前：{task.active_form}" if task.active_form else ""
            owner = f" | owner={task.owner}" if task.owner else ""
            blocked_by = f" | blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(
                f"- #{task.id} [{task.status.value}] {task.subject}"
                f"{active}{owner}{blocked_by}"
            )
        if len(tasks) > self.limit:
            lines.append(f"- ... 还有 {len(tasks) - self.limit} 个")
        return "\n".join(lines)

    async def team_section(self) -> str:
        bus = self.engine.subagent_manager.message_bus
        stats = bus.stats()
        history = [
            msg for msg in bus.get_history(limit=self.limit)
            if msg.topic.startswith("team.")
            or "team_event_type" in msg.metadata
        ]
        try:
            blackboard = await bus.blackboard_get_all()
        except Exception as e:
            blackboard = {}
            blackboard_error = f"{type(e).__name__}: {e}"
        else:
            blackboard_error = ""

        lines = [
            "### Team Protocol",
            f"- 消息总数：{stats['total_messages']}",
            f"- 待处理私信：{stats['pending_messages']}",
            f"- 黑板条目：{stats['blackboard_entries']}",
        ]
        if history:
            lines.append("- 最近团队消息：")
            for msg in history[-self.limit:]:
                event = msg.metadata.get("team_event_type", msg.topic)
                target = msg.recipient or "广播"
                lines.append(
                    f"  - {event}: {msg.sender} → {target} "
                    f"[{msg.priority.value}] {msg.content[:140]}"
                )

        team_entries = [
            (key, entry) for key, entry in sorted(blackboard.items())
            if key.startswith("team/")
        ]
        if team_entries:
            lines.append("- 团队黑板：")
            for key, entry in team_entries[-self.limit:]:
                value = entry.value
                content = value.get("content", value) if isinstance(value, dict) else value
                lines.append(
                    f"  - {key} (v{entry.version}, {entry.author}): {str(content)[:140]}"
                )
        elif blackboard_error:
            lines.append(f"- 团队黑板读取失败：{blackboard_error}")
        return "\n".join(lines)

    def subagent_section(self) -> str:
        lines = ["### Subagent"]
        agents = self.engine.subagent_manager.list_agents()
        if agents:
            lines.append("- 生命周期：")
            for agent in agents[: self.limit]:
                lines.append(
                    f"  - {agent['name']} [{agent.get('state', '?')}] "
                    f"tasks={agent.get('tasks', '0')} age={agent.get('age_s', '?')}s"
                )
        else:
            lines.append("- 当前没有可用子 Agent。")

        events = self.engine.subagent_manager.get_recent_events(limit=self.limit)
        if events:
            lines.append("- 最近事件：")
            for event in events:
                agent = str(event.get("agent_name") or "未匹配")
                status = str(event.get("status") or "?")
                task_id = str(event.get("task_id") or "?")
                message = str(event.get("message") or "")
                lines.append(f"  - {status}: {agent} / {task_id} {message[:140]}")
        bubbles = self.engine.get_recent_permission_bubbles(limit=self.limit)
        if bubbles:
            lines.append("- 权限冒泡：")
            for bubble in bubbles:
                agent = str(bubble.get("agent_name") or "?")
                tool = str(bubble.get("tool_name") or "?")
                status = str(bubble.get("status") or "?")
                reason = str(bubble.get("reason") or "")
                lines.append(f"  - {agent} → {tool} [{status}] {reason[:140]}")
        return "\n".join(lines)

    def hooks_section(self) -> str:
        trace = self.engine.hooks.get_trace()[-self.limit:]
        if not trace:
            return "### Hooks\n- 暂无 hook 触发记录。"
        lines = ["### Hooks"]
        for entry in trace:
            status = "aborted" if entry.aborted else "error" if entry.error else "ok"
            detail = f" | {entry.error}" if entry.error else ""
            lines.append(
                f"- {entry.point}:{entry.callback} [{status}] "
                f"{entry.duration_ms}ms{detail[:140]}"
            )
        return "\n".join(lines)

    def resources_section(self) -> str:
        background_tasks = self.engine.background_runner.list_tasks()
        schedules = self.engine.scheduler_runner.list_jobs(include_inactive=False)
        background_counts = Counter(task.status for task in background_tasks)
        schedule_counts = Counter(job.status for job in schedules)
        lines = [
            "### 资源与后台",
            "- 后台任务："
            f"{background_counts[BackgroundStatus.RUNNING]} 运行中，"
            f"{background_counts[BackgroundStatus.COMPLETED]} 已完成，"
            f"{background_counts[BackgroundStatus.FAILED]} 失败，"
            f"{background_counts[BackgroundStatus.TIMED_OUT]} 超时",
            "- 调度任务："
            f"{schedule_counts[ScheduleStatus.ACTIVE]} 启用中，"
            f"{schedule_counts[ScheduleStatus.PAUSED]} 暂停，"
            f"{schedule_counts[ScheduleStatus.COMPLETED]} 完成",
            f"- MCP：{_mcp_summary(self.engine)}",
        ]
        for task in background_tasks[: self.limit]:
            lines.append(f"- bg {task.id} [{task.status.value}] {task.command[:120]}")
        for job in schedules[: self.limit]:
            lines.append(f"- schedule {job.id} [{job.status.value}] {job.prompt[:120]}")
        return "\n".join(lines)

    async def recommendations_section(self) -> str:
        recommendations: list[str] = []
        context = self.engine.get_context_info()
        budget = self.engine.get_budget_info()
        if context.get("percentage", 0) >= 75:
            recommendations.append(
                "上下文压力较高：优先短输出，必要时触发压缩或读取 runtime_status。"
            )
        if budget.get("percentage", 0) >= 80:
            recommendations.append("预算接近上限：优先使用 fast/本地扫描，避免长推理。")

        try:
            tasks = await self.engine.task_store.list_tasks()
        except Exception:
            tasks = []
        blocked = [task for task in tasks if task.status == TaskStatus.BLOCKED]
        in_progress = [task for task in tasks if task.status == TaskStatus.IN_PROGRESS]
        if blocked:
            recommendations.append(f"有 {len(blocked)} 个 blocked todo：先解除阻塞或记录等待条件。")
        if in_progress:
            recommendations.append(
                f"有 {len(in_progress)} 个进行中 todo：下一步应回写进展或完成状态。"
            )

        bus = self.engine.subagent_manager.message_bus
        critical = [
            msg for msg in bus.get_history(limit=20)
            if msg.priority.value == "critical"
            or msg.metadata.get("team_event_type") == "blocker"
        ]
        if critical:
            recommendations.append("存在团队阻塞/高优先级消息：先读取 team 状态并处理 handoff。")
        if bus.stats()["pending_messages"]:
            recommendations.append("有子 Agent 待处理私信：委派前确认收件 Agent 的 inbox。")

        if not recommendations:
            recommendations.append("当前运行时没有明显阻塞；可以继续按计划执行下一步。")

        lines = ["### 操作建议"]
        lines.extend(f"- {item}" for item in recommendations[: self.limit])
        return "\n".join(lines)


def _select_sections(raw: str) -> set[str]:
    text = (raw or "all").strip().lower()
    if text in {"", "all", "*"}:
        return set(_ALL_SECTIONS)
    selected = {item.strip() for item in text.split(",") if item.strip()}
    return selected & _ALL_SECTIONS or set(_ALL_SECTIONS)


def _mcp_summary(engine: AgentEngine) -> str:
    manager = getattr(engine, "_mcp_manager", None)
    if manager is None:
        return "未连接"
    sessions = getattr(manager, "_sessions", {})
    tool_to_server = getattr(manager, "_tool_to_server", {})
    if not sessions:
        return "未连接"
    counts = Counter(str(server) for server in tool_to_server.values())
    return ", ".join(f"{name}:{counts[str(name)]} tools" for name in sorted(sessions))
