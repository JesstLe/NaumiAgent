"""Unified task/subagent/background panel rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from naumi_agent.background.models import BackgroundStatus
from naumi_agent.ui.task_status_renderer import (
    AgentStatus,
    BackgroundTaskStatus,
    TaskPhase,
    TodoItem,
    render_agent_status,
    render_background_status,
    render_task_summary_bar,
    render_todo_bar,
    render_todo_detail_panel,
)


@dataclass(frozen=True)
class BrowserTaskStatus:
    """One browser task-run entry for the unified task panel."""

    run_id: str = ""
    instruction: str = ""
    status: str = ""
    step_count: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class TaskPanelSnapshot:
    """Normalized task UI state collected from the engine."""

    todo_items: tuple[TodoItem, ...] = ()
    agents: tuple[AgentStatus, ...] = ()
    subagent_events: tuple[dict[str, Any], ...] = ()
    permission_bubbles: tuple[dict[str, Any], ...] = ()
    background_tasks: tuple[BackgroundTaskStatus, ...] = ()
    browser_tasks: tuple[BrowserTaskStatus, ...] = ()
    warnings: tuple[str, ...] = ()


async def build_task_panel_snapshot(engine: Any, *, limit: int = 12) -> TaskPanelSnapshot:
    """Collect a read-only snapshot for the task panel."""
    safe_limit = max(1, min(limit, 50))
    warnings: list[str] = []

    todo_items: tuple[TodoItem, ...] = ()
    try:
        task_store = getattr(engine, "task_store", None)
        if task_store is not None:
            tasks = await task_store.list_tasks()
            todo_items = tuple(
                TodoItem(
                    id=str(task.id),
                    text=task.active_form or task.subject,
                    status=task.status.value,
                )
                for task in tasks[:safe_limit]
            )
    except Exception as exc:
        warnings.append(f"todo 读取失败：{type(exc).__name__}: {exc}")

    agents: tuple[AgentStatus, ...] = ()
    subagent_events: tuple[dict[str, Any], ...] = ()
    permission_bubbles: tuple[dict[str, Any], ...] = ()
    try:
        manager = getattr(engine, "subagent_manager", None)
        if manager is not None:
            agents = tuple(
                _agent_status_from_dict(item)
                for item in manager.list_agents()[:safe_limit]
            )
            subagent_events = tuple(manager.get_recent_events(limit=safe_limit))
        bubbles_getter = getattr(engine, "get_recent_permission_bubbles", None)
        if callable(bubbles_getter):
            permission_bubbles = tuple(bubbles_getter(limit=safe_limit))
    except Exception as exc:
        warnings.append(f"subagent 读取失败：{type(exc).__name__}: {exc}")

    background_tasks: tuple[BackgroundTaskStatus, ...] = ()
    try:
        runner = getattr(engine, "background_runner", None)
        if runner is not None:
            background_tasks = tuple(
                _background_status_from_task(task)
                for task in runner.list_tasks()[:safe_limit]
            )
    except Exception as exc:
        warnings.append(f"后台任务读取失败：{type(exc).__name__}: {exc}")

    browser_tasks: tuple[BrowserTaskStatus, ...] = ()
    try:
        task_runner = getattr(engine, "task_runner", None)
        if task_runner is not None:
            browser_tasks = tuple(
                _browser_status_from_run(run)
                for run in task_runner.list_runs(limit=safe_limit)
            )
    except Exception as exc:
        warnings.append(f"浏览器任务读取失败：{type(exc).__name__}: {exc}")

    return TaskPanelSnapshot(
        todo_items=todo_items,
        agents=agents,
        subagent_events=subagent_events,
        permission_bubbles=permission_bubbles,
        background_tasks=background_tasks,
        browser_tasks=browser_tasks,
        warnings=tuple(warnings),
    )


async def render_task_panel(engine: Any, *, limit: int = 12) -> str:
    """Build and render the unified task panel for CLI/TUI commands."""
    snapshot = await build_task_panel_snapshot(engine, limit=limit)
    return render_task_panel_snapshot(snapshot)


def render_task_panel_snapshot(snapshot: TaskPanelSnapshot) -> str:
    """Render a normalized task panel snapshot as ANSI-friendly text."""
    lines: list[str] = ["\033[1m任务面板\033[0m"]
    summary = render_task_summary_bar(
        todo_items=snapshot.todo_items,
        agents=snapshot.agents,
        background_tasks=snapshot.background_tasks,
    )
    if summary:
        lines.append(summary)

    lines.extend(["", "\033[1mTodo\033[0m"])
    todo_bar = render_todo_bar(snapshot.todo_items)
    if todo_bar:
        lines.append(todo_bar)
    lines.append(render_todo_detail_panel(snapshot.todo_items).rstrip())

    lines.extend(["", "\033[1mSubagent\033[0m"])
    active_agents = tuple(
        agent for agent in snapshot.agents
        if agent.phase not in {TaskPhase.IDLE, TaskPhase.COMPLETED}
    )
    agent_text = render_agent_status(active_agents or snapshot.agents).rstrip()
    lines.append(agent_text or "\033[2m  当前没有可见子 Agent 活动\033[0m")
    if snapshot.subagent_events:
        lines.append("\033[2m  最近事件:\033[0m")
        for event in snapshot.subagent_events[-8:]:
            status = str(event.get("status") or "?")
            agent = str(event.get("agent_name") or "未匹配")
            task_id = str(event.get("task_id") or "?")
            message = str(event.get("message") or event.get("description") or "")
            lines.append(f"  - {status}: {agent} / {task_id} {message[:120]}")
    if snapshot.permission_bubbles:
        lines.append("\033[2m  权限冒泡:\033[0m")
        for bubble in snapshot.permission_bubbles[-5:]:
            agent = str(bubble.get("agent_name") or "?")
            tool = str(bubble.get("tool_name") or "?")
            status = str(bubble.get("status") or "?")
            reason = str(bubble.get("reason") or "")
            lines.append(f"  - {agent} -> {tool} [{status}] {reason[:120]}")

    lines.extend(["", "\033[1mBackground\033[0m"])
    background_text = render_background_status(snapshot.background_tasks).rstrip()
    lines.append(background_text or "\033[2m  暂无后台任务\033[0m")

    lines.extend(["", "\033[1mBrowser Runs\033[0m"])
    if snapshot.browser_tasks:
        for run in snapshot.browser_tasks:
            instruction = run.instruction[:80] + ("..." if len(run.instruction) > 80 else "")
            lines.append(
                f"  - {run.run_id or '?'} [{run.status or '?'}] "
                f"steps={run.step_count} {instruction}"
            )
    else:
        lines.append("\033[2m  暂无浏览器任务运行\033[0m")

    if snapshot.warnings:
        lines.extend(["", "\033[33m面板警告\033[0m"])
        lines.extend(f"  - {warning}" for warning in snapshot.warnings)

    return "\n".join(lines).rstrip() + "\n"


def _agent_status_from_dict(item: dict[str, Any]) -> AgentStatus:
    state = str(item.get("state") or "idle")
    phase = {
        "spawned": TaskPhase.PENDING,
        "ready": TaskPhase.IDLE,
        "running": TaskPhase.RUNNING,
        "idle": TaskPhase.IDLE,
        "destroyed": TaskPhase.KILLED,
        "uninitialized": TaskPhase.IDLE,
    }.get(state, TaskPhase.IDLE)
    description = str(item.get("description") or state)
    return AgentStatus(
        name=str(item.get("name") or ""),
        phase=phase,
        description=description,
    )


def _background_status_from_task(task: Any) -> BackgroundTaskStatus:
    status = getattr(task, "status", BackgroundStatus.RUNNING)
    phase = {
        BackgroundStatus.RUNNING: TaskPhase.RUNNING,
        BackgroundStatus.COMPLETED: TaskPhase.COMPLETED,
        BackgroundStatus.FAILED: TaskPhase.FAILED,
        BackgroundStatus.CANCELLED: TaskPhase.KILLED,
        BackgroundStatus.TIMED_OUT: TaskPhase.FAILED,
    }.get(status, TaskPhase.IDLE)
    runtime_s = _elapsed_seconds(str(getattr(task, "started_at", "")))
    return BackgroundTaskStatus(
        task_id=str(getattr(task, "id", "")),
        command=str(getattr(task, "command", "")),
        phase=phase,
        exit_code=getattr(task, "exit_code", None),
        runtime_s=runtime_s,
        last_output_line=_last_non_empty_line(str(getattr(task, "output_preview", ""))),
    )


def _browser_status_from_run(run: dict[str, Any]) -> BrowserTaskStatus:
    step_count = run.get("stepCount", run.get("steps", 0))
    try:
        steps = int(step_count)
    except (TypeError, ValueError):
        steps = 0
    return BrowserTaskStatus(
        run_id=str(run.get("id") or ""),
        instruction=str(run.get("instruction") or ""),
        status=str(run.get("status") or ""),
        step_count=steps,
        created_at=str(run.get("createdAt") or ""),
    )


def _elapsed_seconds(started_at: str) -> float:
    if not started_at:
        return 0.0
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now() - started).total_seconds())


def _last_non_empty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
