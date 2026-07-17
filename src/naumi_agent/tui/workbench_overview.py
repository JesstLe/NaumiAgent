"""Read-only Textual fallback for the authoritative Workbench overview."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Markdown, Static

logger = logging.getLogger(__name__)


class WorkbenchSnapshotError(ValueError):
    """Raised when the backend violates the Workbench snapshot contract."""


def format_workbench_overview_markdown(value: Mapping[str, Any]) -> str:
    """Render a bounded overview without querying or deriving backend state."""
    snapshot = _validate_snapshot(value)
    counts = _mapping(snapshot.get("counts"))
    lines = [
        "## Workbench Overview",
        "",
        (
            f"revision {_integer(snapshot.get('revision'))} · "
            f"任务 {_integer(counts.get('tasks'))} · "
            f"worktree {_integer(counts.get('worktrees'))} · "
            f"待审 {_integer(counts.get('reviews'))} · "
            f"失败 {_integer(counts.get('failures'))}"
        ),
        f"最后更新：{_plain(snapshot.get('generated_at')) or '-'}",
    ]
    missions = _records(snapshot.get("missions"))
    tasks = _records(snapshot.get("tasks"))
    if not missions and not tasks:
        lines.extend(
            [
                "",
                "暂无 Workbench 任务。",
                "",
                "下一步：使用 `/task` 创建任务，或按 `r` 刷新当前会话。",
            ]
        )
        return "\n".join(lines)

    selection = _mapping(snapshot.get("active_selection"))
    mission = _select_record(missions, selection.get("mission_id"), active="active")
    task = _select_record(tasks, selection.get("task_id"), active="in_progress")
    task_id = _normalized(task.get("id")) if task else ""
    issue = _first_for_task(snapshot.get("issues"), task_id)
    lease = _first_for_task(snapshot.get("leases"), task_id)

    lines.extend(["", "### 当前目标"])
    if mission:
        lines.extend(
            [
                f"- 状态：{_mission_status(mission.get('status'))}",
                f"- 名称：{_plain(mission.get('title') or mission.get('id'))}",
                f"- 目标：{_plain(mission.get('goal')) or '未填写'}",
            ]
        )
    else:
        lines.append("- 尚未设置目标")

    lines.extend(["", "### 当前任务"])
    if task:
        owner = task.get("owner") or lease.get("agent_id") or "未分配"
        lines.extend(
            [
                f"- 状态：{_task_status(task.get('status'))}",
                f"- 名称：{_plain(task.get('subject') or task.get('id'))}",
                f"- 说明：{_plain(task.get('description') or task.get('active_form')) or '未填写'}",
                f"- Owner：{_plain(owner)}",
            ]
        )
        blocked_by = _strings(task.get("blocked_by"))
        if blocked_by:
            lines.append(f"- 阻塞于：{', '.join(blocked_by[:5])}")
    else:
        lines.append("- 当前目标下暂无任务")

    lines.extend(["", "### 变更载体"])
    if issue or lease:
        worktree = _plain(
            issue.get("related_worktree") or lease.get("worktree_name")
        )
        lines.extend(
            [
                f"- 分支：{_plain(issue.get('related_branch')) or '尚未绑定'}",
                f"- Worktree：{worktree or '尚未绑定'}",
                f"- PR：{_plain(issue.get('related_pr')) or '尚未绑定'}",
            ]
        )
    else:
        lines.append("- 尚未绑定分支或 worktree")

    lines.extend(["", "### 验证"])
    validation = _latest_for_task(snapshot.get("validation_runs"), task_id)
    if validation:
        command = validation.get("command")
        rendered_command = (
            " ".join(_strings(command))
            if isinstance(command, list)
            else _plain(command)
        )
        exit_code = _plain(validation.get("exit_code")) or "-"
        lines.extend(
            [
                f"- {_validation_status(validation.get('status'))} · 退出码 {exit_code}",
                f"- 命令：`{_code(rendered_command or '-')}`",
            ]
        )
    else:
        lines.append("- 尚未记录验证")

    lines.extend(["", "### 风险与待审"])
    lines.append(f"- 风险：{_risk_label(issue.get('risk_level'))}")
    failures = _for_task(snapshot.get("failures"), task_id)
    approvals = _for_task(snapshot.get("approvals"), task_id)
    lines.append(
        f"- 失败：{_plain(failures[0].get('title') or failures[0].get('kind'))}"
        if failures
        else "- 失败：无"
    )
    lines.append(
        f"- 待审：{_plain(approvals[0].get('title') or approvals[0].get('id'))}"
        if approvals
        else "- 待审：无"
    )
    return "\n".join(lines)


class WorkbenchOverviewScreen(Screen[None]):
    """Full-page TUI view backed by ``WorkbenchService.dashboard_snapshot``."""

    BINDINGS = [
        Binding("escape", "close", "返回"),
        Binding("r", "refresh", "刷新"),
    ]

    DEFAULT_CSS = """
    WorkbenchOverviewScreen {
        layout: vertical;
        background: $background;
    }
    #workbench-title {
        height: 3;
        padding: 1 2;
        text-style: bold;
        color: $accent;
    }
    #workbench-content {
        height: 1fr;
        overflow-y: auto;
        margin: 0 1;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #workbench-error {
        height: auto;
        max-height: 3;
        margin: 0 1;
        padding: 0 2;
        color: $warning;
    }
    """

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.snapshot: Mapping[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Static("Workbench Overview · 后端权威只读视图", id="workbench-title")
        yield Markdown("正在加载 Workbench 权威快照…", id="workbench-content")
        yield Static("", id="workbench-error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_snapshot()

    @work(exclusive=True, group="workbench-overview", exit_on_error=False)
    async def refresh_snapshot(self) -> None:
        error = self.query_one("#workbench-error", Static)
        if self.snapshot is None:
            self.query_one("#workbench-content", Markdown).update(
                "正在加载 Workbench 权威快照…"
            )
        error.update("")
        try:
            session = getattr(self.engine, "_session", None)
            if session is None:
                session = await self.engine.get_or_create_session()
            session_id = str(getattr(session, "id", "") or "")
            service = getattr(self.engine, "workbench_service", None)
            if service is None or not session_id:
                raise WorkbenchSnapshotError("Workbench 服务或当前会话不可用")
            snapshot = _validate_snapshot(
                await service.dashboard_snapshot(session_id),
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "TUI Workbench snapshot refresh failed (%s)",
                type(exc).__name__,
            )
            if self.snapshot is None:
                self.query_one("#workbench-content", Markdown).update(
                    "## Workbench Overview\n\n权威快照暂时不可用。"
                )
                error.update("加载失败；请稍后重试或运行 /doctor。")
            else:
                error.update("刷新失败，已保留上一次快照；请稍后重试。")
            return
        self.snapshot = snapshot
        self.query_one("#workbench-content", Markdown).update(
            format_workbench_overview_markdown(snapshot)
        )

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_close(self) -> None:
        self.app.pop_screen()


def _validate_snapshot(
    value: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchSnapshotError("Workbench snapshot 必须是对象")
    if (
        _integer(value.get("schema_version")) != 1
        or _integer(value.get("revision")) < 1
        or not _normalized(value.get("stream_id"))
        or value.get("full") is not True
    ):
        raise WorkbenchSnapshotError("Workbench snapshot contract 无效")
    if session_id is not None and _normalized(value.get("session_id")) != session_id:
        raise WorkbenchSnapshotError("Workbench snapshot 会话不匹配")
    return value


def _select_record(
    records: list[dict[str, Any]],
    selected_id: Any,
    *,
    active: str,
) -> dict[str, Any]:
    selected = _normalized(selected_id)
    return next(
        (item for item in records if _normalized(item.get("id")) == selected),
        next(
            (item for item in records if _normalized(item.get("status")) == active),
            records[0] if records else {},
        ),
    )


def _first_for_task(value: Any, task_id: str) -> dict[str, Any]:
    records = _for_task(value, task_id)
    return records[0] if records else {}


def _latest_for_task(value: Any, task_id: str) -> dict[str, Any]:
    records = _for_task(value, task_id)
    return records[-1] if records else {}


def _for_task(value: Any, task_id: str) -> list[dict[str, Any]]:
    records = _records(value)
    if not task_id:
        return []
    return [
        item for item in records if _normalized(item.get("task_id")) == task_id
    ]


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)][:100]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_plain(item) for item in value if _plain(item)][:100]


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _plain(value: Any, limit: int = 600) -> str:
    text = _normalized(value, limit=limit)
    text = text.replace("\\", "\\\\")
    return re.sub(r"([`*_\[\]<>#|])", r"\\\1", text)


def _normalized(value: Any, limit: int = 600) -> str:
    text = re.sub(
        r"[\x00-\x1f\x7f]",
        " ",
        str(value if value is not None else ""),
    )
    return " ".join(text.split())[:limit]


def _code(value: Any) -> str:
    return _normalized(value, 800).replace("`", "'")


def _mission_status(value: Any) -> str:
    return {
        "active": "进行中",
        "completed": "已完成",
        "blocked": "已阻塞",
        "cancelled": "已取消",
    }.get(_normalized(value), _plain(value) or "规划中")


def _task_status(value: Any) -> str:
    return {
        "in_progress": "进行中",
        "completed": "已完成",
        "blocked": "已阻塞",
        "pending": "待处理",
        "cancelled": "已取消",
    }.get(_normalized(value), _plain(value) or "未知")


def _validation_status(value: Any) -> str:
    status = _normalized(value)
    if status in {"passed", "success", "completed"}:
        return "验证通过"
    if status in {"failed", "error"}:
        return "验证失败"
    return f"验证{status or '未知'}"


def _risk_label(value: Any) -> str:
    return {
        "critical": "严重风险",
        "high": "高风险",
        "medium": "中风险",
        "low": "低风险",
    }.get(_normalized(value), "未标记")


__all__ = [
    "WorkbenchOverviewScreen",
    "WorkbenchSnapshotError",
    "format_workbench_overview_markdown",
]
