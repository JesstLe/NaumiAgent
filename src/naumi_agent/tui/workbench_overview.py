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


def format_workbench_worktrees_markdown(
    value: Mapping[str, Any],
    *,
    selected_index: int = 0,
) -> str:
    """Render the authoritative worktree inventory and one bounded detail card."""
    snapshot = _validate_snapshot(value)
    status = _normalized(snapshot.get("worktrees_status"))
    if status != "ready":
        return (
            "## Worktrees\n\n"
            "权威 worktree 状态暂时不可用。\n\n"
            f"诊断码：`{_code(snapshot.get('worktrees_code') or 'unknown')}`"
        )

    worktrees = _records(snapshot.get("worktrees"))
    if not worktrees:
        return "## Worktrees\n\n当前没有由 NaumiAgent 管理的 worktree。"

    index = min(len(worktrees) - 1, max(0, int(selected_index)))
    selected = worktrees[index]
    total = max(len(worktrees), _integer(snapshot.get("worktrees_total")))
    start = max(0, min(index - 5, len(worktrees) - 10))
    visible = worktrees[start : start + 10]
    lines = [
        "## Worktrees",
        "",
        f"共 {total} 个 · 当前 {index + 1}/{len(worktrees)} · ↑/↓ 选择",
        "",
        "### 列表",
    ]
    for offset, item in enumerate(visible, start=start):
        marker = "▶" if offset == index else "·"
        lines.append(
            f"- {marker} {_plain(item.get('name')) or '未命名'} · "
            f"{_worktree_status(item.get('status'))} · "
            f"{_integer(item.get('dirty_files'))} 个未提交文件"
        )
    if snapshot.get("worktrees_truncated") is True:
        lines.append("- 列表已按安全上限截断，请使用管理命令精确查询。")

    task = _mapping(selected.get("task"))
    lines.extend(
        [
            "",
            "### 当前 Worktree",
            f"- 名称：{_plain(selected.get('name')) or '-'}",
            f"- 状态：{_worktree_status(selected.get('status'))}",
            f"- 路径：`{_code(selected.get('path') or '-')}`",
            f"- 分支：`{_code(selected.get('branch') or '-')}`",
            f"- 任务：{_plain(task.get('subject') or task.get('id')) or '未绑定'}",
            f"- Agent：{_plain(selected.get('agent_id')) or '未占用'}",
            f"- 未提交文件：{_integer(selected.get('dirty_files'))}",
            f"- 新提交：{_integer(selected.get('commits_ahead'))}",
            f"- 可安全删除：{'是' if selected.get('removable') is True else '否'}",
        ]
    )
    kept_reason = _plain(selected.get("kept_reason"))
    if kept_reason:
        lines.append(f"- 保留原因：{kept_reason}")
    return "\n".join(lines)


def format_workbench_reviews_markdown(
    value: Mapping[str, Any],
    *,
    selected_index: int = 0,
    detail: Mapping[str, Any] | None = None,
    loading: bool = False,
    error: str = "",
) -> str:
    """Render waiting approvals and bounded evidence from the shared service."""
    snapshot = _validate_snapshot(value)
    reviews = _records(snapshot.get("approvals"))
    if not reviews:
        return (
            "## Reviews\n\n当前没有待审请求。\n\n"
            "Review 页面只读；审批动作将在 UI-10.6 接入。"
        )
    index = min(len(reviews) - 1, max(0, int(selected_index)))
    selected = reviews[index]
    start = max(0, min(index - 4, len(reviews) - 8))
    lines = [
        "## Reviews",
        "",
        f"共 {len(reviews)} 项 · 当前 {index + 1}/{len(reviews)} · ↑/↓ 选择",
        "",
        "### 列表",
    ]
    for offset, item in enumerate(reviews[start : start + 8], start=start):
        marker = "▶" if offset == index else "·"
        lines.append(
            f"- {marker} 待审 · {_plain(item.get('title') or item.get('id'))} · "
            f"{_plain(item.get('requester')) or '未知发起者'}"
        )
    lines.extend(["", "### 审查证据"])
    if error:
        lines.extend([f"- 证据不可用：{_plain(error)}", "- 下一步：按 `r` 重试。"])
        return "\n".join(lines)
    if loading or detail is None:
        lines.append("- 正在读取 diff、验证与阻塞证据…")
        return "\n".join(lines)
    evidence = _mapping(detail.get("evidence"))
    approval = _mapping(evidence.get("approval"))
    if _normalized(approval.get("id")) != _normalized(selected.get("id")):
        lines.append("- 当前证据与所选审查不匹配，请刷新。")
        return "\n".join(lines)
    worktree = _mapping(evidence.get("worktree"))
    runs = _records(evidence.get("validation_runs"))
    files = _records(evidence.get("changed_files"))
    hunks = _records(evidence.get("diff_hunks"))
    failed = [run for run in runs if _normalized(run.get("status")) in {"failed", "error"}]
    if _normalized(worktree.get("status")) != "present":
        gate = "阻塞：变更载体不可用"
    elif not runs:
        gate = "待补证据：尚未运行验证"
    elif failed:
        gate = f"阻塞：{len(failed)} 项验证失败"
    else:
        gate = "证据就绪：可进入人工判断"
    lines.extend(
        [
            f"- 标题：{_plain(approval.get('title') or selected.get('title'))}",
            f"- 发起者：{_plain(approval.get('requester')) or '未知'}",
            f"- 说明：{_plain(approval.get('detail')) or '未填写'}",
            f"- 状态：{gate}",
            f"- Worktree：{_plain(worktree.get('name')) or '未绑定'} "
            f"({_plain(worktree.get('status')) or 'unknown'})",
            f"- 验证：{len(runs)} 次，失败 {len(failed)} 次",
            f"- 变更：{len(files)} 个文件",
        ]
    )
    if files:
        lines.extend(["", "### 文件"])
        for item in files[:10]:
            lines.append(
                f"- {_plain(item.get('status')) or 'modified'} · "
                f"`{_code(item.get('path') or '-')}`"
            )
        if len(files) > 10:
            lines.append(f"- 另有 {len(files) - 10} 个文件")
    if hunks:
        first = hunks[0]
        patch_lines = [
            _code(line) for line in str(first.get("patch") or "").splitlines()[:20]
        ]
        lines.extend(
            [
                "",
                f"### Diff · `{_code(first.get('path') or '-')}`",
                "```diff",
                *patch_lines,
                "```",
            ]
        )
        if len(hunks) > 1:
            lines.append(f"另有 {len(hunks) - 1} 个 diff 文件。")
    else:
        lines.extend(["", "Diff：当前没有可展示的已跟踪文件差异。"])
    return "\n".join(lines)


class WorkbenchOverviewScreen(Screen[None]):
    """Full-page TUI view backed by ``WorkbenchService.dashboard_snapshot``."""

    BINDINGS = [
        Binding("escape", "close", "返回"),
        Binding("r", "refresh", "刷新"),
        Binding("tab", "next_tab", "切换页签", show=False),
        Binding("shift+tab", "previous_tab", "切换页签", show=False),
        Binding("1", "overview_tab", "概览", show=False),
        Binding("2", "worktrees_tab", "Worktrees", show=False),
        Binding("3", "reviews_tab", "Reviews", show=False),
        Binding("up", "select_previous", "上一项", show=False),
        Binding("down", "select_next", "下一项", show=False),
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
        self.selected_tab = "overview"
        self.selected_worktree_index = 0
        self.selected_review_index = 0
        self.review_detail: Mapping[str, Any] | None = None
        self.review_loading = False
        self.review_error = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="workbench-title")
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
        self.selected_worktree_index = min(
            self.selected_worktree_index,
            max(0, len(_records(snapshot.get("worktrees"))) - 1),
        )
        self.selected_review_index = min(
            self.selected_review_index,
            max(0, len(_records(snapshot.get("approvals"))) - 1),
        )
        self._render_snapshot()
        if self.selected_tab == "reviews":
            self.refresh_review_detail()

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_next_tab(self) -> None:
        tabs = ("overview", "worktrees", "reviews")
        self.selected_tab = tabs[(tabs.index(self.selected_tab) + 1) % len(tabs)]
        self._render_snapshot()
        if self.selected_tab == "reviews":
            self.refresh_review_detail()

    def action_previous_tab(self) -> None:
        tabs = ("overview", "worktrees", "reviews")
        self.selected_tab = tabs[(tabs.index(self.selected_tab) - 1) % len(tabs)]
        self._render_snapshot()
        if self.selected_tab == "reviews":
            self.refresh_review_detail()

    def action_overview_tab(self) -> None:
        self.selected_tab = "overview"
        self._render_snapshot()

    def action_worktrees_tab(self) -> None:
        self.selected_tab = "worktrees"
        self._render_snapshot()

    def action_reviews_tab(self) -> None:
        self.selected_tab = "reviews"
        self._render_snapshot()
        self.refresh_review_detail()

    def action_select_previous(self) -> None:
        if self.selected_tab == "worktrees":
            self.selected_worktree_index = max(0, self.selected_worktree_index - 1)
            self._render_snapshot()
        elif self.selected_tab == "reviews":
            self.selected_review_index = max(0, self.selected_review_index - 1)
            self.review_detail = None
            self._render_snapshot()
            self.refresh_review_detail()

    def action_select_next(self) -> None:
        if self.selected_tab == "worktrees" and self.snapshot is not None:
            last = max(0, len(_records(self.snapshot.get("worktrees"))) - 1)
            self.selected_worktree_index = min(last, self.selected_worktree_index + 1)
            self._render_snapshot()
        elif self.selected_tab == "reviews" and self.snapshot is not None:
            last = max(0, len(_records(self.snapshot.get("approvals"))) - 1)
            self.selected_review_index = min(last, self.selected_review_index + 1)
            self.review_detail = None
            self._render_snapshot()
            self.refresh_review_detail()

    @work(exclusive=True, group="workbench-review", exit_on_error=False)
    async def refresh_review_detail(self) -> None:
        if self.snapshot is None:
            return
        reviews = _records(self.snapshot.get("approvals"))
        if not reviews:
            self.review_detail = None
            self.review_loading = False
            self.review_error = ""
            self._render_snapshot()
            return
        index = min(len(reviews) - 1, max(0, self.selected_review_index))
        review_id = _normalized(reviews[index].get("id"))
        self.review_loading = True
        self.review_error = ""
        self._render_snapshot()
        try:
            session_id = _normalized(self.snapshot.get("session_id"))
            detail = await self.engine.workbench_service.get_review_evidence(
                session_id, review_id
            )
            if detail is None:
                raise WorkbenchSnapshotError("审查请求不存在")
            approval = _mapping(detail.get("approval"))
            if _normalized(approval.get("id")) != review_id:
                raise WorkbenchSnapshotError("审查证据不匹配")
        except Exception as exc:
            logger.warning("TUI Workbench review failed (%s)", type(exc).__name__)
            self.review_error = "审查证据加载失败；请稍后重试。"
            self.review_detail = None
        else:
            self.review_detail = {"evidence": detail}
        finally:
            self.review_loading = False
            self._render_snapshot()

    def _render_snapshot(self) -> None:
        title = self.query_one("#workbench-title", Static)
        tabs = (("overview", "1 概览"), ("worktrees", "2 Worktrees"), ("reviews", "3 Reviews"))
        title.update("Workbench · " + " · ".join(
            f"[{label}]" if self.selected_tab == name else label for name, label in tabs
        ))
        if self.snapshot is None:
            return
        content = self.query_one("#workbench-content", Markdown)
        if self.selected_tab == "worktrees":
            content.update(
                format_workbench_worktrees_markdown(
                    self.snapshot,
                    selected_index=self.selected_worktree_index,
                )
            )
        elif self.selected_tab == "reviews":
            content.update(
                format_workbench_reviews_markdown(
                    self.snapshot,
                    selected_index=self.selected_review_index,
                    detail=self.review_detail,
                    loading=self.review_loading,
                    error=self.review_error,
                )
            )
        else:
            content.update(format_workbench_overview_markdown(self.snapshot))


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


def _worktree_status(value: Any) -> str:
    return {
        "clean": "干净",
        "dirty": "有未提交改动",
        "missing": "目录缺失",
        "kept": "已保留",
    }.get(_normalized(value), _plain(value) or "未知")


__all__ = [
    "WorkbenchOverviewScreen",
    "WorkbenchSnapshotError",
    "format_workbench_overview_markdown",
    "format_workbench_reviews_markdown",
    "format_workbench_worktrees_markdown",
]
