"""Textual QuickOpen backed by authoritative command, task, and session projections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from naumi_agent.ui.command_index import (
    TerminalCommandIndexEntry,
    search_terminal_commands,
    terminal_command_template,
)
from naumi_agent.ui.session_list import SessionListItem, build_session_list_snapshot
from naumi_agent.ui.session_quick_open import (
    search_terminal_sessions,
    terminal_session_template,
)
from naumi_agent.ui.task_panel import TaskViewItem, build_task_panel_snapshot
from naumi_agent.ui.task_quick_open import (
    search_terminal_tasks,
    terminal_task_template,
)

_RISK_LABELS = {
    "read_only": "只读",
    "session_state": "会话状态",
    "permission_change": "权限变更",
    "workspace_write": "工作区写入",
    "tool_execution": "工具执行",
    "destructive": "破坏性",
}
_RISK_STYLES = {
    "read_only": "green",
    "session_state": "cyan",
    "permission_change": "yellow",
    "workspace_write": "yellow",
    "tool_execution": "red",
    "destructive": "bold red",
}
_TASK_SOURCE_LABELS = {
    "todo": "待办",
    "subagent": "子智能体",
    "background": "后台任务",
    "browser": "浏览器",
}
_TASK_STATUS_LABELS = {
    "pending": "等待",
    "running": "运行中",
    "blocked": "阻塞",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
_TASK_STATUS_STYLES = {
    "pending": "dim",
    "running": "cyan",
    "blocked": "yellow",
    "completed": "green",
    "failed": "red",
    "cancelled": "red",
}


class CommandQuickOpenScreen(ModalScreen[str | None]):
    """Search commands and return an editable template without executing it."""

    DEFAULT_CSS = """
    CommandQuickOpenScreen {
        align: center middle;
    }
    CommandQuickOpenScreen > Container {
        width: 88;
        max-width: 94%;
        height: 28;
        max-height: 88%;
        padding: 1 2;
        border: thick $accent 80%;
        background: $surface;
    }
    CommandQuickOpenScreen Input {
        width: 1fr;
        margin: 0 0 1 0;
    }
    CommandQuickOpenScreen ListView {
        height: 1fr;
        border: round $primary 60%;
    }
    CommandQuickOpenScreen ListItem {
        height: auto;
        padding: 0 1;
    }
    CommandQuickOpenScreen #command-quick-open-detail {
        height: 3;
        margin: 1 0 0 0;
        color: $text-muted;
    }
    CommandQuickOpenScreen #command-quick-open-help {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        entries: Sequence[TerminalCommandIndexEntry],
        *,
        recent_commands: Sequence[str] = (),
        engine: Any | None = None,
    ) -> None:
        super().__init__()
        self._entries = tuple(entries)
        self._recent_commands = tuple(recent_commands)[:20]
        self._engine = engine
        self._provider = "commands"
        self._task_items: tuple[TaskViewItem, ...] = ()
        self._task_loaded = False
        self._task_loading = False
        self._task_error = ""
        self._task_warnings: tuple[str, ...] = ()
        self._session_items: tuple[SessionListItem, ...] = ()
        self._session_loaded = False
        self._session_loading = False
        self._session_error = ""
        self._results: tuple[TerminalCommandIndexEntry | TaskViewItem | SessionListItem, ...] = ()

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(
                "[bold]命令 QuickOpen[/bold] · Tab 切换任务/会话 · 选择后仅填入输入框",
                id="command-quick-open-title",
            )
            yield Input(
                placeholder="搜索命令、别名、说明、类别或风险…",
                id="command-quick-open-query",
                max_length=200,
            )
            yield ListView(id="command-quick-open-results")
            yield Static("", id="command-quick-open-detail")
            yield Static(
                "Tab 切换命令/任务/会话 · ↑/↓ 选择 · Enter 填入 · Esc 取消 · 不会自动执行",
                id="command-quick-open-help",
            )

    async def on_mount(self) -> None:
        await self._refresh_results("")
        self.query_one("#command-quick-open-query", Input).focus()

    @on(Input.Changed, "#command-quick-open-query")
    async def on_query_changed(self, event: Input.Changed) -> None:
        await self._refresh_results(event.value)

    @on(Input.Submitted, "#command-quick-open-query")
    def on_query_submitted(self, _event: Input.Submitted) -> None:
        self._accept_selected()

    @on(ListView.Selected, "#command-quick-open-results")
    def on_result_selected(self, _event: ListView.Selected) -> None:
        self._accept_selected()

    @on(ListView.Highlighted, "#command-quick-open-results")
    def on_result_highlighted(self, _event: ListView.Highlighted) -> None:
        self._render_detail()

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.prevent_default()
            event.stop()
            return
        if event.key == "tab":
            await self._switch_provider()
            event.prevent_default()
            event.stop()
            return
        if event.key not in {"up", "down"}:
            return
        results = self.query_one("#command-quick-open-results", ListView)
        if not self._results:
            event.prevent_default()
            event.stop()
            return
        current = max(0, results.index or 0)
        offset = -1 if event.key == "up" else 1
        results.index = (current + offset) % len(self._results)
        self._render_detail()
        event.prevent_default()
        event.stop()

    async def _refresh_results(self, query: str) -> None:
        if self._provider == "tasks":
            self._results = search_terminal_tasks(self._task_items, query, limit=50)
        elif self._provider == "sessions":
            self._results = search_terminal_sessions(self._session_items, query, limit=100)
        else:
            self._results = search_terminal_commands(
                self._entries,
                query,
                limit=50,
                recent_commands=self._recent_commands,
            )
        results = self.query_one("#command-quick-open-results", ListView)
        await results.clear()
        if self._results:
            await results.extend(
                ListItem(Label(self._render_entry(entry))) for entry in self._results
            )
            results.index = 0
        self._render_detail()

    def _render_entry(
        self, entry: TerminalCommandIndexEntry | TaskViewItem | SessionListItem
    ) -> str:
        if isinstance(entry, TaskViewItem):
            source = _TASK_SOURCE_LABELS.get(entry.source, entry.source)
            status = _TASK_STATUS_LABELS.get(entry.status, entry.status or "未知")
            style = _TASK_STATUS_STYLES.get(entry.status, "dim")
            owner = f" · {escape(entry.owner)}" if entry.owner else ""
            return (
                f"[bold]{escape(entry.title or entry.task_id)}[/bold] "
                f"[{style}]{escape(status)}[/] · {escape(source)} · "
                f"{escape(entry.task_id)}{owner}"
            )
        if isinstance(entry, SessionListItem):
            current = " [cyan]· 当前[/]" if entry.is_current else ""
            return (
                f"[bold]{escape(entry.title)}[/bold] · {escape(entry.session_id)} · "
                f"{escape(entry.model)}{current}"
            )
        syntax = f" {entry.arguments.syntax}" if entry.arguments.syntax else ""
        risk = _RISK_LABELS[entry.permission_risk]
        style = _RISK_STYLES[entry.permission_risk]
        recent = " [cyan]· 最近[/]" if entry.command in self._recent_commands else ""
        return (
            f"[bold]{escape(entry.command + syntax)}[/bold] "
            f"[{style}]{risk}[/]{recent} · {escape(entry.description)}"
        )

    def _render_detail(self) -> None:
        detail = self.query_one("#command-quick-open-detail", Static)
        selected = self._selected_entry()
        if selected is None:
            if self._provider == "tasks" and self._task_loading:
                detail.update("[cyan]正在读取权威任务快照…[/cyan]")
            elif self._provider == "tasks" and self._task_error:
                detail.update(f"[yellow]{escape(self._task_error)}[/yellow]")
            elif self._provider == "tasks" and self._task_warnings:
                detail.update(
                    "[yellow]没有匹配任务。部分来源不可用，可运行 /doctor 检查。[/yellow]"
                )
            elif self._provider == "sessions" and self._session_loading:
                detail.update("[cyan]正在读取当前工作区会话…[/cyan]")
            elif self._provider == "sessions" and self._session_error:
                detail.update(f"[yellow]{escape(self._session_error)}[/yellow]")
            else:
                label = {"tasks": "任务", "sessions": "会话"}.get(self._provider, "命令")
                detail.update(f"[yellow]没有匹配{label}。[/yellow]")
            return
        if isinstance(selected, TaskViewItem):
            detail.update(
                f"来源：{escape(_TASK_SOURCE_LABELS.get(selected.source, selected.source))} · "
                f"状态：{escape(_TASK_STATUS_LABELS.get(selected.status, selected.status))} · "
                f"Owner：{escape(selected.owner or '无')}\n"
                f"将填入：[bold]{escape(terminal_task_template(selected))}[/bold]"
            )
            return
        if isinstance(selected, SessionListItem):
            detail.update(
                f"模型：{escape(selected.model or '未知')} · "
                f"分支：{escape(selected.git_branch or '未知')}\n"
                f"将填入：[bold]{escape(terminal_session_template(selected))}[/bold]"
            )
            return
        aliases = "、".join(selected.aliases) if selected.aliases else "无"
        detail.update(
            f"类别：{escape(selected.category)} · 来源：{escape(selected.source)} · "
            f"别名：{escape(aliases)}\n"
            f"将填入：[bold]{escape(terminal_command_template(selected))}[/bold]"
        )

    def _selected_entry(self) -> TerminalCommandIndexEntry | TaskViewItem | SessionListItem | None:
        if not self._results:
            return None
        index = self.query_one("#command-quick-open-results", ListView).index
        return self._results[max(0, min(len(self._results) - 1, index or 0))]

    def _accept_selected(self) -> None:
        selected = self._selected_entry()
        if selected is not None:
            template = (
                terminal_task_template(selected)
                if isinstance(selected, TaskViewItem)
                else terminal_session_template(selected)
                if isinstance(selected, SessionListItem)
                else terminal_command_template(selected)
            )
            self.dismiss(template)

    async def _switch_provider(self) -> None:
        self._provider = {
            "commands": "tasks",
            "tasks": "sessions",
            "sessions": "commands",
        }[self._provider]
        query = self.query_one("#command-quick-open-query", Input)
        query.value = ""
        title = self.query_one("#command-quick-open-title", Label)
        if self._provider == "tasks":
            title.update("[bold]任务 QuickOpen[/bold] · Tab 切换命令 · 选择后仅填入输入框")
            query.placeholder = "搜索任务 ID、标题、Owner、来源或状态…"
            await self._load_tasks()
        elif self._provider == "sessions":
            title.update("[bold]会话 QuickOpen[/bold] · Tab 切换命令 · 选择后仅填入输入框")
            query.placeholder = "搜索会话标题、ID、模型或分支…"
            await self._load_sessions()
        else:
            title.update("[bold]命令 QuickOpen[/bold] · Tab 切换任务/会话 · 选择后仅填入输入框")
            query.placeholder = "搜索命令、别名、说明、类别或风险…"
        await self._refresh_results("")
        query.focus()

    async def _load_tasks(self) -> None:
        if self._task_loaded or self._task_loading or self._engine is None:
            return
        self._task_loading = True
        self._task_error = ""
        self._results = ()
        await self.query_one("#command-quick-open-results", ListView).clear()
        self._render_detail()
        try:
            snapshot = await build_task_panel_snapshot(self._engine, limit=50)
            self._task_items = snapshot.view_items
            self._task_warnings = snapshot.warnings
            self._task_loaded = True
        except Exception:
            self._task_error = "任务快照读取失败，请运行 /doctor 后重试。"
        finally:
            self._task_loading = False

    async def _load_sessions(self) -> None:
        if self._session_loaded or self._session_loading or self._engine is None:
            return
        self._session_loading = True
        self._session_error = ""
        try:
            snapshot = await build_session_list_snapshot(
                self._engine, page=1, page_size=100, query=""
            )
            self._session_items = snapshot.items
            self._session_loaded = True
        except Exception:
            self._session_error = "会话快照读取失败，请稍后重试。"
        finally:
            self._session_loading = False


__all__ = ["CommandQuickOpenScreen"]
