"""Shared formatting and Textual screen for authoritative Runtime Inspector data."""

from __future__ import annotations

from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Markdown, Static, Tab, Tabs

from naumi_agent.inspector import INSPECTOR_TAB_NAMES, RuntimeInspectorSnapshot

_TAB_LABELS = {
    "plan": "Plan",
    "tools": "Tools",
    "context": "Context",
    "changes": "Changes",
    "tests": "Tests",
}


def format_runtime_inspector_markdown(
    value: RuntimeInspectorSnapshot | dict[str, Any],
    tab: str,
) -> str:
    """Render one bounded Inspector tab from a validated backend snapshot."""
    snapshot = (
        value
        if isinstance(value, RuntimeInspectorSnapshot)
        else RuntimeInspectorSnapshot.from_dict(value)
    )
    if tab not in INSPECTOR_TAB_NAMES:
        raise ValueError(f"未知 Runtime Inspector 标签: {tab}")
    section = getattr(snapshot, tab)
    lines = [
        f"## Runtime Inspector · {_TAB_LABELS[tab]}",
        "",
        (
            f"状态：{_state_label(section.state)} · revision {snapshot.revision}"
            + (f" · run `{_code_text(snapshot.active_run_id)}`" if snapshot.active_run_id else "")
        ),
    ]
    if tab == "plan":
        lines.extend(_format_plan(section))
    elif tab == "tools":
        lines.extend(_format_tools(section))
    elif tab == "context":
        lines.extend(_format_context(section))
    elif tab == "changes":
        lines.extend(_format_changes(section))
    else:
        lines.extend(_format_tests(section))
    for warning in section.warnings[:5]:
        lines.append(f"- 警告：{_plain(warning)}")
    return "\n".join(lines).strip()


class RuntimeInspectorScreen(Screen[None]):
    """Full-page Textual view backed by ``engine.runtime_inspector``."""

    BINDINGS = [
        Binding("escape", "close", "返回"),
        Binding("[", "previous_tab", "上一标签"),
        Binding("]", "next_tab", "下一标签"),
        Binding("left", "previous_tab", "上一标签", show=False),
        Binding("right", "next_tab", "下一标签", show=False),
        Binding("r", "refresh", "刷新"),
    ]

    DEFAULT_CSS = """
    RuntimeInspectorScreen {
        layout: vertical;
        background: $background;
    }
    #inspector-title {
        height: 3;
        padding: 1 2;
        text-style: bold;
        color: $accent;
    }
    #inspector-tabs {
        height: 3;
        margin: 0 1;
    }
    #inspector-content {
        height: 1fr;
        overflow-y: auto;
        margin: 0 1;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #inspector-error {
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
        self.snapshot: RuntimeInspectorSnapshot | None = None
        self.selected_tab = "plan"

    def compose(self) -> ComposeResult:
        yield Static("Runtime Inspector · 后端权威运行视图", id="inspector-title")
        yield Tabs(
            *(Tab(label, id=tab) for tab, label in _TAB_LABELS.items()),
            id="inspector-tabs",
        )
        yield Markdown("正在加载运行快照…", id="inspector-content")
        yield Static("", id="inspector-error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_snapshot()

    @on(Tabs.TabActivated)
    def on_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = str(event.tab.id or "plan")
        if tab_id not in INSPECTOR_TAB_NAMES:
            return
        self.selected_tab = tab_id
        self._render_snapshot()

    @work(exclusive=True, group="runtime-inspector", exit_on_error=False)
    async def refresh_snapshot(self) -> None:
        error = self.query_one("#inspector-error", Static)
        if self.snapshot is None:
            self.query_one("#inspector-content", Markdown).update(
                "正在加载运行快照…"
            )
        error.update("")
        try:
            snapshot = await self.engine.runtime_inspector.snapshot()
        except Exception as exc:
            if self.snapshot is None:
                self.query_one("#inspector-content", Markdown).update(
                    "## Runtime Inspector\n\n运行快照暂时不可用。"
                )
            error.update(f"刷新失败，已保留上一次快照：{type(exc).__name__} — {exc}")
            return
        self.snapshot = snapshot
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        self.query_one("#inspector-content", Markdown).update(
            format_runtime_inspector_markdown(self.snapshot, self.selected_tab)
        )

    def action_previous_tab(self) -> None:
        self._select_tab(-1)

    def action_next_tab(self) -> None:
        self._select_tab(1)

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_close(self) -> None:
        self.app.pop_screen()

    def _select_tab(self, delta: int) -> None:
        tabs = list(INSPECTOR_TAB_NAMES)
        index = tabs.index(self.selected_tab)
        self.selected_tab = tabs[(index + delta) % len(tabs)]
        self.query_one(Tabs).active = self.selected_tab
        self._render_snapshot()


def _format_plan(section: Any) -> list[str]:
    if not section.items and not section.next_actions:
        return ["", "尚未产生计划"]
    lines = ["", "### 当前计划"]
    for item in section.items[:30]:
        details = []
        if item.owner:
            details.append(f"负责人 {_plain(item.owner)}")
        if item.blocked_by:
            details.append(f"阻塞于 {', '.join(_plain(value) for value in item.blocked_by)}")
        suffix = f" · {' · '.join(details)}" if details else ""
        lines.append(
            f"- {_todo_status(item.status)} · {_plain(item.subject or item.id)}{suffix}"
        )
    for action in section.next_actions[:5]:
        lines.append(f"- 下一步：{_plain(action.label or action.kind)}")
    return lines


def _format_tools(section: Any) -> list[str]:
    if not section.items and not section.approvals:
        return ["", "尚未调用工具"]
    lines = ["", "### 工具调用"]
    for item in section.items[:30]:
        duration = f" · {item.duration_ms}ms" if item.duration_ms else ""
        summary = f" · {_plain(item.summary)}" if item.summary else ""
        lines.append(
            f"- {_tool_status(item.status)} · `{_code_text(item.name)}`{duration}{summary}"
        )
    for approval in section.approvals[:10]:
        lines.append(
            f"- 审批：`{_code_text(approval.tool_name)}` · {_approval_label(approval.decision)}"
        )
    return lines


def _format_context(section: Any) -> list[str]:
    if section.state == "empty":
        return ["", "尚未产生运行上下文"]
    git = "不可用"
    if section.git_available:
        dirty_label = "有改动" if section.git_dirty else "干净"
        git = f"{_plain(section.branch or 'detached')} · {dirty_label}"
        if section.commit:
            git += f" · {_plain(section.commit)}"
    return [
        "",
        f"- 工作区：`{_code_text(section.workspace_root)}`",
        f"- Git：{git}",
        f"- 模型：`{_code_text(section.model)}`",
        (
            f"- 模式：{_plain(section.runtime_mode or '-')} · "
            f"权限 {_plain(section.permission_mode or '-')}"
        ),
        (
            f"- 上下文：{section.context_used}/{section.context_window}"
            f"（{section.context_percentage:.1f}%）"
        ),
        (
            f"- 预算：${section.budget_used_usd:.4f}/${section.budget_max_usd:.4f}"
            f"（{section.budget_percentage:.1f}%）"
        ),
        (
            f"- Token：输入 {section.input_tokens} · "
            f"输出 {section.output_tokens} · 轮次 {section.turns}"
        ),
    ]


def _format_changes(section: Any) -> list[str]:
    if not section.items:
        suffix = ""
        if section.git_state.available:
            suffix = f" · Git {'有改动' if section.git_state.dirty else '干净'}"
        return ["", f"尚未记录文件改动{suffix}"]
    lines = ["", "### 文件改动"]
    if section.summary:
        lines.append(_plain(section.summary))
    for item in section.items[:50]:
        stats = []
        if item.additions:
            stats.append(f"+{item.additions}")
        if item.deletions:
            stats.append(f"-{item.deletions}")
        suffix = f" · {' '.join(stats)}" if stats else ""
        source = f" · 来源 {_plain(item.source_tool)}" if item.source_tool else ""
        lines.append(
            f"- {_change_status(item.status)} · `{_code_text(item.path)}`{suffix}{source}"
        )
    return lines


def _format_tests(section: Any) -> list[str]:
    if not section.validations and not section.unverified and not section.next_actions:
        return ["", "尚未记录验证"]
    lines = ["", "### 验证"]
    for item in section.validations[:30]:
        counts = _validation_counts(item)
        suffix = f" · {counts}" if counts else ""
        lines.append(
            f"- {'通过' if item.status == 'passed' else '失败'} · "
            f"`{_code_text(item.command)}`{suffix}"
        )
    for value in section.unverified[:10]:
        lines.append(f"- 未验证：{_plain(value)}")
    for action in section.next_actions[:5]:
        lines.append(f"- 下一步：{_plain(action.label or action.kind)}")
    return lines


def _state_label(state: str) -> str:
    return {
        "ready": "已就绪",
        "empty": "尚未产生",
        "loading": "加载中",
        "stale": "已过期",
        "error": "错误",
    }.get(state, "未知")


def _todo_status(status: str) -> str:
    return {
        "completed": "已完成",
        "in_progress": "进行中",
        "blocked": "已阻塞",
        "pending": "待处理",
    }.get(status, _plain(status or "未知"))


def _tool_status(status: str) -> str:
    return {
        "success": "成功",
        "running": "运行中",
        "prepared": "已准备",
        "error": "失败",
    }.get(status, _plain(status or "未知"))


def _approval_label(decision: str) -> str:
    return {
        "pending": "等待确认",
        "allowed_once": "仅本次允许",
        "allowed_session": "本会话允许",
        "bypass": "已绕过确认",
        "denied": "已拒绝",
        "error": "确认失败",
    }.get(decision, _plain(decision or "已记录"))


def _change_status(status: str) -> str:
    return {
        "modified": "修改",
        "added": "新增",
        "deleted": "删除",
        "renamed": "重命名",
        "untracked": "未跟踪",
        "conflicted": "冲突",
    }.get(status, _plain(status or "变化"))


def _validation_counts(item: Any) -> str:
    counts = []
    if item.passed:
        counts.append(f"通过 {item.passed}")
    if item.failed:
        counts.append(f"失败 {item.failed}")
    if item.skipped:
        counts.append(f"跳过 {item.skipped}")
    if not counts and item.exit_code is not None:
        counts.append(f"退出码 {item.exit_code}")
    return " · ".join(counts)


def _plain(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()[:500]


def _code_text(value: Any) -> str:
    return _plain(value).replace("`", "ˋ")


__all__ = ["RuntimeInspectorScreen", "format_runtime_inspector_markdown"]
