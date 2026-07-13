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
class TodoTaskDetail:
    """Detailed todo entry metadata for the task panel."""

    task_id: str = ""
    subject: str = ""
    status: str = ""
    owner: str = ""
    blocked_by: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    updated_at: str = ""


@dataclass(frozen=True)
class BackgroundTaskDetail:
    """Detailed background command metadata for the task panel."""

    task_id: str = ""
    command: str = ""
    cwd: str = ""
    status: str = ""
    pid: int | None = None
    port_hints: tuple[int, ...] = ()
    output_path: str = ""
    output_preview: str = ""
    exit_code: int | None = None
    error: str = ""
    started_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class BrowserTaskStatus:
    """One browser task-run entry for the unified task panel."""

    run_id: str = ""
    instruction: str = ""
    status: str = ""
    step_count: int = 0
    current_step: str = ""
    error: str = ""
    created_at: str = ""
    record_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskPanelFilter:
    """Read-only filters applied to a task panel snapshot."""

    source: str = "all"
    status: str = "all"
    detail_id: str = ""
    history: bool = False


@dataclass(frozen=True)
class TaskTimelineEvent:
    """Normalized cross-source event for the task panel timeline."""

    event_id: str = ""
    source: str = ""
    task_id: str = ""
    status: str = ""
    title: str = ""
    detail: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class TaskPanelSnapshot:
    """Normalized task UI state collected from the engine."""

    todo_items: tuple[TodoItem, ...] = ()
    todo_details: tuple[TodoTaskDetail, ...] = ()
    agents: tuple[AgentStatus, ...] = ()
    subagent_events: tuple[dict[str, Any], ...] = ()
    permission_bubbles: tuple[dict[str, Any], ...] = ()
    background_tasks: tuple[BackgroundTaskStatus, ...] = ()
    background_details: tuple[BackgroundTaskDetail, ...] = ()
    browser_tasks: tuple[BrowserTaskStatus, ...] = ()
    timeline_events: tuple[TaskTimelineEvent, ...] = ()
    filters: TaskPanelFilter = TaskPanelFilter()
    warnings: tuple[str, ...] = ()


async def build_task_panel_snapshot(
    engine: Any,
    *,
    limit: int = 12,
    source: str = "all",
    status: str = "all",
    detail_id: str = "",
    history: bool = False,
) -> TaskPanelSnapshot:
    """Collect a read-only snapshot for the task panel."""
    safe_limit = max(1, min(limit, 50))
    filters = TaskPanelFilter(
        source=_normalize_filter(source, _SOURCE_FILTERS),
        status=_normalize_filter(status, _STATUS_FILTERS),
        detail_id=str(detail_id or "").strip(),
        history=bool(history),
    )
    warnings: list[str] = []

    todo_items: tuple[TodoItem, ...] = ()
    todo_details: tuple[TodoTaskDetail, ...] = ()
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
            todo_details = tuple(
                _todo_detail_from_task(task)
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
    background_details: tuple[BackgroundTaskDetail, ...] = ()
    try:
        runner = getattr(engine, "background_runner", None)
        if runner is not None:
            list_method = getattr(
                runner,
                "list_history" if history else "list_active_tasks",
                None,
            )
            if not callable(list_method):
                list_method = runner.list_tasks
            tasks = list_method()[:safe_limit]
            background_tasks = tuple(
                _background_status_from_task(task)
                for task in tasks
            )
            background_details = tuple(
                _background_detail_from_task(task)
                for task in tasks
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

    snapshot = TaskPanelSnapshot(
        todo_items=todo_items,
        todo_details=todo_details,
        agents=agents,
        subagent_events=subagent_events,
        permission_bubbles=permission_bubbles,
        background_tasks=background_tasks,
        background_details=background_details,
        browser_tasks=browser_tasks,
        filters=filters,
        warnings=tuple(warnings),
    )
    timeline_events = _build_timeline_events(snapshot, limit=safe_limit)
    return _filter_snapshot(TaskPanelSnapshot(
        todo_items=snapshot.todo_items,
        todo_details=snapshot.todo_details,
        agents=snapshot.agents,
        subagent_events=snapshot.subagent_events,
        permission_bubbles=snapshot.permission_bubbles,
        background_tasks=snapshot.background_tasks,
        background_details=snapshot.background_details,
        browser_tasks=snapshot.browser_tasks,
        timeline_events=timeline_events,
        filters=snapshot.filters,
        warnings=snapshot.warnings,
    ))


async def render_task_panel(
    engine: Any,
    *,
    limit: int = 12,
    source: str = "all",
    status: str = "all",
    detail_id: str = "",
    history: bool = False,
) -> str:
    """Build and render the unified task panel for CLI/TUI commands."""
    snapshot = await build_task_panel_snapshot(
        engine,
        limit=limit,
        source=source,
        status=status,
        detail_id=detail_id,
        history=history,
    )
    return render_task_panel_snapshot(snapshot)


def render_task_panel_snapshot(snapshot: TaskPanelSnapshot) -> str:
    """Render a normalized task panel snapshot as ANSI-friendly text."""
    lines: list[str] = ["\033[1m任务面板\033[0m"]
    if (
        snapshot.filters.source != "all"
        or snapshot.filters.status != "all"
        or snapshot.filters.detail_id
        or snapshot.filters.history
    ):
        lines.append(
            f"\033[2mfilter: source={snapshot.filters.source} "
            f"status={snapshot.filters.status}"
            f"{f' detail={snapshot.filters.detail_id}' if snapshot.filters.detail_id else ''}"
            f"{' history=true' if snapshot.filters.history else ''}"
            "\033[0m"
        )
    summary = render_task_summary_bar(
        todo_items=snapshot.todo_items,
        agents=snapshot.agents,
        background_tasks=snapshot.background_tasks,
    )
    if summary:
        lines.append(summary)

    timeline_lines = _render_timeline_section(snapshot)
    if timeline_lines:
        lines.extend(["", "\033[1mTimeline\033[0m", *timeline_lines])

    detail_lines = _render_detail_section(snapshot)
    if detail_lines:
        lines.extend(["", "\033[1mDetail\033[0m", *detail_lines])

    lines.extend(["", "\033[1mTodo\033[0m"])
    todo_bar = render_todo_bar(snapshot.todo_items)
    if todo_bar:
        lines.append(todo_bar)
    lines.append(render_todo_detail_panel(snapshot.todo_items).rstrip())
    if snapshot.todo_details:
        lines.append("\033[2m  任务详情:\033[0m")
        for detail in snapshot.todo_details[-10:]:
            lines.append(_render_todo_detail(detail))

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
    if snapshot.background_details:
        lines.append("\033[2m  后台详情:\033[0m")
        for detail in snapshot.background_details[-10:]:
            lines.append(_render_background_detail(detail))

    lines.extend(["", "\033[1mBrowser Runs\033[0m"])
    if snapshot.browser_tasks:
        for run in snapshot.browser_tasks:
            instruction = run.instruction[:80] + ("..." if len(run.instruction) > 80 else "")
            details = [
                f"steps={run.step_count}",
                f"created={run.created_at or '-'}",
            ]
            if run.current_step:
                details.append(f"current={run.current_step[:80]}")
            if run.error:
                details.append(f"error={run.error[:80]}")
            if run.record_paths:
                details.append(f"records={', '.join(run.record_paths[:3])}")
            lines.append(
                f"  - {run.run_id or '?'} [{run.status or '?'}] {instruction}"
                f" | {'; '.join(details)}"
            )
    else:
        lines.append("\033[2m  暂无浏览器任务运行\033[0m")

    if snapshot.warnings:
        lines.extend(["", "\033[33m面板警告\033[0m"])
        lines.extend(f"  - {warning}" for warning in snapshot.warnings)

    return "\n".join(lines).rstrip() + "\n"


_SOURCE_FILTERS = {
    "all",
    "todo",
    "subagent",
    "background",
    "browser",
    "permissions",
}
_STATUS_FILTERS = {
    "all",
    "open",
    "running",
    "pending",
    "completed",
    "failed",
    "blocked",
    "attention",
    "needs_input",
    "needs_confirmation",
    "cancelled",
    "timed_out",
}
_OPEN_DONE = {"completed", "cancelled", "killed", "idle", "destroyed"}
_RUNNING_STATUSES = {"running", "in_progress", "spawned", "ready"}
_ATTENTION_STATUSES = {
    "blocked",
    "failed",
    "timed_out",
    "needs_input",
    "needs_confirmation",
}


def _normalize_filter(value: str, allowed: set[str]) -> str:
    normalized = str(value or "all").strip().lower().replace("-", "_")
    return normalized if normalized in allowed else "all"


def _filter_snapshot(snapshot: TaskPanelSnapshot) -> TaskPanelSnapshot:
    source = snapshot.filters.source
    status = snapshot.filters.status
    todo_ids = {
        detail.task_id
        for detail in snapshot.todo_details
        if _matches_filter("todo", detail.status, source, status)
    }
    background_ids = {
        detail.task_id
        for detail in snapshot.background_details
        if _matches_filter("background", detail.status, source, status)
    }
    background_status_by_id = {
        detail.task_id: detail.status
        for detail in snapshot.background_details
    }
    return TaskPanelSnapshot(
        todo_items=tuple(
            item for item in snapshot.todo_items
            if (item.id in todo_ids or not snapshot.todo_details)
            and _matches_filter("todo", item.status, source, status)
        ),
        todo_details=tuple(
            item for item in snapshot.todo_details
            if item.task_id in todo_ids
        ),
        agents=tuple(
            agent for agent in snapshot.agents
            if _matches_filter("subagent", agent.phase.value, source, status)
        ),
        subagent_events=tuple(
            event for event in snapshot.subagent_events
            if _matches_filter("subagent", str(event.get("status") or ""), source, status)
        ),
        permission_bubbles=tuple(
            bubble for bubble in snapshot.permission_bubbles
            if _matches_filter("permissions", str(bubble.get("status") or ""), source, status)
        ),
        background_tasks=tuple(
            item for item in snapshot.background_tasks
            if _matches_filter(
                "background",
                background_status_by_id.get(item.task_id, item.phase.value),
                source,
                status,
            )
            and (not snapshot.background_details or item.task_id in background_ids)
        ),
        background_details=tuple(
            item for item in snapshot.background_details
            if item.task_id in background_ids
        ),
        browser_tasks=tuple(
            item for item in snapshot.browser_tasks
            if _matches_filter("browser", item.status, source, status)
        ),
        timeline_events=tuple(
            event for event in snapshot.timeline_events
            if _matches_filter(event.source, event.status, source, status)
        ),
        filters=snapshot.filters,
        warnings=snapshot.warnings,
    )


def _build_timeline_events(
    snapshot: TaskPanelSnapshot,
    *,
    limit: int,
) -> tuple[TaskTimelineEvent, ...]:
    events: list[TaskTimelineEvent] = []

    for detail in snapshot.todo_details:
        events.append(TaskTimelineEvent(
            event_id=f"todo:{detail.task_id}",
            source="todo",
            task_id=detail.task_id,
            status=detail.status,
            title=detail.subject,
            detail=_join_attrs(
                owner=detail.owner or "-",
                blocked_by=",".join(detail.blocked_by) or "-",
                blocks=",".join(detail.blocks) or "-",
            ),
            timestamp=detail.updated_at,
        ))

    for event in snapshot.subagent_events:
        task_id = str(event.get("task_id") or event.get("agent_name") or "")
        status = str(event.get("status") or "")
        message = str(event.get("message") or event.get("description") or "")
        agent_name = str(event.get("agent_name") or "")
        timestamp = str(
            event.get("timestamp")
            or event.get("created_at")
            or event.get("createdAt")
            or ""
        )
        events.append(TaskTimelineEvent(
            event_id=f"subagent:{task_id}:{len(events)}",
            source="subagent",
            task_id=task_id,
            status=status,
            title=message or agent_name or "子 Agent 事件",
            detail=_join_attrs(agent=agent_name or "-", event=status or "-"),
            timestamp=timestamp,
        ))

    for bubble in snapshot.permission_bubbles:
        request_id = str(
            bubble.get("request_id")
            or bubble.get("call_id")
            or bubble.get("tool_call_id")
            or ""
        )
        tool = str(bubble.get("tool_name") or "")
        status = str(bubble.get("status") or "")
        timestamp = str(
            bubble.get("timestamp")
            or bubble.get("created_at")
            or bubble.get("createdAt")
            or ""
        )
        events.append(TaskTimelineEvent(
            event_id=f"permissions:{request_id or tool}:{len(events)}",
            source="permissions",
            task_id=request_id or tool,
            status=status,
            title=f"{bubble.get('agent_name') or '?'} -> {tool or '?'}",
            detail=_join_attrs(reason=str(bubble.get("reason") or "")[:160] or "-"),
            timestamp=timestamp,
        ))

    for detail in snapshot.background_details:
        events.append(TaskTimelineEvent(
            event_id=f"background:{detail.task_id}",
            source="background",
            task_id=detail.task_id,
            status=detail.status,
            title=detail.command,
            detail=_join_attrs(
                cwd=detail.cwd or "-",
                pid=str(detail.pid) if detail.pid is not None else "-",
                ports=",".join(str(port) for port in detail.port_hints) or "-",
                output=detail.output_path or "-",
            ),
            timestamp=detail.completed_at or detail.started_at,
        ))

    for run in snapshot.browser_tasks:
        events.append(TaskTimelineEvent(
            event_id=f"browser:{run.run_id}",
            source="browser",
            task_id=run.run_id,
            status=run.status,
            title=run.instruction,
            detail=_join_attrs(
                steps=str(run.step_count),
                current=run.current_step[:120] or "-",
                records=", ".join(run.record_paths[:3]) or "-",
            ),
            timestamp=run.created_at,
        ))

    def sort_key(event: TaskTimelineEvent) -> tuple[int, str]:
        return (1 if event.timestamp else 0, event.timestamp)

    return tuple(sorted(events, key=sort_key, reverse=True)[:limit])


def _render_timeline_section(snapshot: TaskPanelSnapshot) -> list[str]:
    if not snapshot.timeline_events:
        return []
    lines: list[str] = []
    for event in snapshot.timeline_events[:10]:
        time_label = event.timestamp or "-"
        title = event.title[:90] + ("..." if len(event.title) > 90 else "")
        details = [
            f"time={time_label}",
            f"source={event.source or '-'}",
            f"event={event.event_id or '-'}",
        ]
        if event.detail:
            details.append(event.detail)
        lines.append(
            f"  - {event.task_id or '?'} [{event.status or '?'}] {title}"
            f" | {'; '.join(details)}"
        )
    return lines


def _join_attrs(**attrs: str) -> str:
    return "; ".join(f"{key}={value}" for key, value in attrs.items() if value)


def _render_detail_section(snapshot: TaskPanelSnapshot) -> list[str]:
    detail_id = snapshot.filters.detail_id
    if not detail_id:
        return []

    for detail in snapshot.todo_details:
        if detail.task_id == detail_id:
            return [
                "  类型: Todo",
                f"  ID: {detail.task_id}",
                f"  状态: {detail.status or '-'}",
                f"  主题: {detail.subject or '-'}",
                f"  Owner: {detail.owner or '-'}",
                f"  Blocked by: {', '.join(detail.blocked_by) or '-'}",
                f"  Blocks: {', '.join(detail.blocks) or '-'}",
                f"  Updated: {detail.updated_at or '-'}",
            ]

    for agent in snapshot.agents:
        if agent.name == detail_id or agent.task_id == detail_id:
            return [
                "  类型: Subagent",
                f"  Name: {agent.name or '-'}",
                f"  Task ID: {agent.task_id or '-'}",
                f"  状态: {agent.phase.value}",
                f"  描述: {agent.description or '-'}",
            ]

    for event in snapshot.subagent_events:
        if (
            str(event.get("task_id") or "") == detail_id
            or str(event.get("agent_name") or "") == detail_id
        ):
            return [
                "  类型: Subagent Event",
                f"  Agent: {event.get('agent_name') or '-'}",
                f"  Task ID: {event.get('task_id') or '-'}",
                f"  状态: {event.get('status') or '-'}",
                f"  消息: {event.get('message') or event.get('description') or '-'}",
            ]

    for bubble in snapshot.permission_bubbles:
        if (
            str(bubble.get("request_id") or "") == detail_id
            or str(bubble.get("call_id") or "") == detail_id
        ):
            return [
                "  类型: Permission",
                f"  Request ID: {bubble.get('request_id') or '-'}",
                f"  Call ID: {bubble.get('call_id') or '-'}",
                f"  Agent: {bubble.get('agent_name') or '-'}",
                f"  Tool: {bubble.get('tool_name') or '-'}",
                f"  状态: {bubble.get('status') or '-'}",
                f"  原因: {bubble.get('reason') or '-'}",
            ]

    for detail in snapshot.background_details:
        if detail.task_id == detail_id:
            lines = [
                "  类型: Background",
                f"  ID: {detail.task_id}",
                f"  状态: {detail.status or '-'}",
                f"  命令: {detail.command or '-'}",
                f"  CWD: {detail.cwd or '-'}",
                f"  PID: {detail.pid if detail.pid is not None else '-'}",
                f"  Ports: {', '.join(str(port) for port in detail.port_hints) or '-'}",
                f"  Output: {detail.output_path or '-'}",
                f"  Started: {detail.started_at or '-'}",
                f"  Completed: {detail.completed_at or '-'}",
            ]
            if detail.output_preview:
                lines.append(f"  最近输出: {detail.output_preview[:240]}")
            if detail.exit_code is not None:
                lines.append(f"  Exit: {detail.exit_code}")
            if detail.error:
                lines.append(f"  Error: {detail.error[:240]}")
            return lines

    for run in snapshot.browser_tasks:
        if run.run_id == detail_id:
            return [
                "  类型: Browser Run",
                f"  ID: {run.run_id}",
                f"  状态: {run.status or '-'}",
                f"  指令: {run.instruction or '-'}",
                f"  Steps: {run.step_count}",
                f"  Current: {run.current_step or '-'}",
                f"  Error: {run.error or '-'}",
                f"  Created: {run.created_at or '-'}",
                f"  Records: {', '.join(run.record_paths) or '-'}",
            ]

    return [
        f"  未找到 ID: {detail_id}",
        "  可用来源: todo / subagent / background / browser / permissions",
    ]


def _matches_filter(
    source_name: str,
    raw_status: str,
    source_filter: str,
    status_filter: str,
) -> bool:
    if source_filter != "all" and source_name != source_filter:
        return False
    if status_filter == "all":
        return True
    status = str(raw_status or "").strip().lower().replace("-", "_")
    if status_filter == "open":
        return status not in _OPEN_DONE
    if status_filter == "running":
        return status in _RUNNING_STATUSES
    if status_filter == "attention":
        return status in _ATTENTION_STATUSES
    return status == status_filter


def _todo_detail_from_task(task: Any) -> TodoTaskDetail:
    return TodoTaskDetail(
        task_id=str(getattr(task, "id", "")),
        subject=str(getattr(task, "subject", "")),
        status=str(getattr(getattr(task, "status", ""), "value", getattr(task, "status", ""))),
        owner=str(getattr(task, "owner", "") or ""),
        blocked_by=tuple(str(item) for item in getattr(task, "blocked_by", []) or []),
        blocks=tuple(str(item) for item in getattr(task, "blocks", []) or []),
        updated_at=str(getattr(task, "updated_at", "") or ""),
    )


def _render_todo_detail(detail: TodoTaskDetail) -> str:
    attrs = [
        f"owner={detail.owner or '-'}",
        f"blocked_by={','.join(detail.blocked_by) or '-'}",
        f"blocks={','.join(detail.blocks) or '-'}",
    ]
    if detail.updated_at:
        attrs.append(f"updated={detail.updated_at}")
    return (
        f"  - #{detail.task_id or '?'} [{detail.status or '?'}] "
        f"{detail.subject[:80]} | {'; '.join(attrs)}"
    )


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
    task_count = item.get("tasks")
    if task_count not in (None, "", 0, "0"):
        description = f"{description} · tasks={task_count}"
    return AgentStatus(
        name=str(item.get("name") or ""),
        task_id=str(item.get("task_id") or item.get("current_task_id") or ""),
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


def _background_detail_from_task(task: Any) -> BackgroundTaskDetail:
    raw_status = getattr(task, "status", "")
    status = str(getattr(raw_status, "value", raw_status))
    return BackgroundTaskDetail(
        task_id=str(getattr(task, "id", "")),
        command=str(getattr(task, "command", "")),
        cwd=str(getattr(task, "cwd", "")),
        status=status,
        pid=getattr(task, "pid", None),
        port_hints=_safe_ports(getattr(task, "port_hints", []) or []),
        output_path=str(getattr(task, "output_path", "")),
        output_preview=str(getattr(task, "output_preview", "") or ""),
        exit_code=getattr(task, "exit_code", None),
        error=str(getattr(task, "error", "") or ""),
        started_at=str(getattr(task, "started_at", "") or ""),
        completed_at=str(getattr(task, "completed_at", "") or ""),
    )


def _safe_ports(raw_ports: Any) -> tuple[int, ...]:
    ports: list[int] = []
    for raw in raw_ports:
        try:
            ports.append(int(raw))
        except (TypeError, ValueError):
            continue
    return tuple(ports)


def _render_background_detail(detail: BackgroundTaskDetail) -> str:
    attrs = [
        f"cwd={detail.cwd or '-'}",
        f"pid={detail.pid if detail.pid is not None else '-'}",
        f"ports={','.join(str(port) for port in detail.port_hints) or '-'}",
        f"output={detail.output_path or '-'}",
    ]
    if detail.started_at:
        attrs.append(f"started={detail.started_at}")
    if detail.completed_at:
        attrs.append(f"completed={detail.completed_at}")
    if detail.exit_code is not None:
        attrs.append(f"exit={detail.exit_code}")
    if detail.error:
        attrs.append(f"error={detail.error[:80]}")
    return (
        f"  - {detail.task_id or '?'} [{detail.status or '?'}] "
        f"{detail.command[:80]} | {'; '.join(attrs)}"
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
        current_step=str(run.get("currentStep") or run.get("current_step") or ""),
        error=str(run.get("error") or ""),
        created_at=str(run.get("createdAt") or run.get("created_at") or ""),
        record_paths=_extract_browser_record_paths(run),
    )


def _extract_browser_record_paths(run: dict[str, Any]) -> tuple[str, ...]:
    """Extract concrete artifact/report paths from a browser run payload."""
    paths: list[str] = []
    for key in ("artifacts", "reports"):
        _collect_paths(run.get(key), paths)
    result = run.get("result")
    if isinstance(result, dict):
        for key in ("artifacts", "reports"):
            _collect_paths(result.get(key), paths)
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path and path not in seen:
            unique.append(path)
            seen.add(path)
    return tuple(unique)


def _collect_paths(value: Any, paths: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.startswith(("/", "~", ".")) or "://" in value:
            paths.append(value)
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in {"path", "file", "filepath", "file_path", "url"}:
                _collect_paths(nested, paths)
            elif isinstance(nested, (dict, list, tuple)):
                _collect_paths(nested, paths)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_paths(item, paths)


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
