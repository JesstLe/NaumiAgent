"""Textual QuickOpen backed by authoritative command, task, and session projections."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from naumi_agent.agent_control import AgentDescriptor
from naumi_agent.ui.agent_quick_open import (
    search_terminal_agents,
    terminal_agent_template,
)
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
from naumi_agent.ui.workspace_file_index import (
    WorkspaceFileItem,
    WorkspaceFileSearchResult,
    workspace_file_template,
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
_AGENT_STATE_LABELS = {
    "uninitialized": "未初始化",
    "spawned": "已启动",
    "ready": "就绪",
    "running": "运行中",
    "idle": "空闲",
    "destroyed": "已销毁",
}
_AGENT_STATE_STYLES = {
    "uninitialized": "dim",
    "spawned": "cyan",
    "ready": "cyan",
    "running": "green",
    "idle": "dim",
    "destroyed": "red",
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
        self._file_items: tuple[WorkspaceFileItem, ...] = ()
        self._file_result: WorkspaceFileSearchResult | None = None
        self._file_loading = False
        self._file_error = ""
        self._file_query_task: asyncio.Task[None] | None = None
        self._agent_items: tuple[AgentDescriptor, ...] = ()
        self._agent_loaded = False
        self._agent_loading = False
        self._agent_error = ""
        self._agent_warnings: tuple[str, ...] = ()
        self._agent_revision = 0
        self._results: tuple[
            TerminalCommandIndexEntry
            | TaskViewItem
            | SessionListItem
            | WorkspaceFileItem
            | AgentDescriptor,
            ...,
        ] = ()

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(
                "[bold]命令 QuickOpen[/bold] · "
                "Tab 切换任务/会话/文件/Agent · 选择后仅填入输入框",
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
                "Tab 切换命令/任务/会话/文件/Agent · "
                "↑/↓ 选择 · Enter 填入 · Esc 取消 · 不会自动执行",
                id="command-quick-open-help",
            )

    async def on_mount(self) -> None:
        await self._refresh_results("")
        self.query_one("#command-quick-open-query", Input).focus()

    @on(Input.Changed, "#command-quick-open-query")
    async def on_query_changed(self, event: Input.Changed) -> None:
        if self._provider == "files":
            self._schedule_file_search(event.value)
            return
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
            await self._cancel_file_search(cancel_index=True)
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
        elif self._provider == "files":
            self._results = self._file_items
        elif self._provider == "agents":
            self._results = search_terminal_agents(self._agent_items, query, limit=50)
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
        self,
        entry: (
            TerminalCommandIndexEntry
            | TaskViewItem
            | SessionListItem
            | WorkspaceFileItem
            | AgentDescriptor
        ),
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
        if isinstance(entry, WorkspaceFileItem):
            directory = f" · {escape(entry.directory)}" if entry.directory else ""
            extension = f" · {escape(entry.extension)}" if entry.extension else ""
            return f"[bold]{escape(entry.name)}[/bold]{directory}{extension}"
        if isinstance(entry, AgentDescriptor):
            state = _AGENT_STATE_LABELS.get(entry.state, entry.state)
            style = _AGENT_STATE_STYLES.get(entry.state, "dim")
            kind = "动态" if entry.kind == "dynamic" else "预置"
            tier = f" · {escape(entry.model_tier)}" if entry.model_tier else ""
            return (
                f"[bold]{escape(entry.name)}[/bold] "
                f"[{style}]{escape(state)}[/] · {kind} · "
                f"任务 {entry.task_count}{tier}"
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
            elif self._provider == "files" and self._file_loading:
                detail.update("[cyan]正在后台建立 Workspace 文件索引，可按 Esc 取消…[/cyan]")
            elif self._provider == "files" and self._file_error:
                detail.update(f"[yellow]{escape(self._file_error)}[/yellow]")
            elif self._provider == "agents" and self._agent_loading:
                detail.update("[cyan]正在读取当前会话 Agent 权威快照…[/cyan]")
            elif self._provider == "agents" and self._agent_error:
                detail.update(f"[yellow]{escape(self._agent_error)}[/yellow]")
            elif self._provider == "agents" and self._agent_warnings:
                detail.update("[yellow]没有匹配 Agent。部分 Agent 数据源暂不可用。[/yellow]")
            else:
                label = {
                    "tasks": "任务",
                    "sessions": "会话",
                    "files": "文件",
                    "agents": "Agent",
                }.get(self._provider, "命令")
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
        if isinstance(selected, WorkspaceFileItem):
            result = self._file_result
            source = (
                "Git ignore-aware"
                if result is not None and result.source == "git"
                else "文件系统"
            )
            detail.update(
                f"相对路径：{escape(selected.path)} · 索引来源：{source}\n"
                f"将填入：[bold]{escape(workspace_file_template(selected))}[/bold]"
            )
            return
        if isinstance(selected, AgentDescriptor):
            capabilities = "、".join(selected.capabilities[:5]) or "无"
            detail.update(
                f"状态：{escape(_AGENT_STATE_LABELS.get(selected.state, selected.state))} · "
                f"能力：{escape(capabilities)} · revision {self._agent_revision}\n"
                f"将填入：[bold]{escape(terminal_agent_template(selected))}[/bold]"
            )
            return
        aliases = "、".join(selected.aliases) if selected.aliases else "无"
        detail.update(
            f"类别：{escape(selected.category)} · 来源：{escape(selected.source)} · "
            f"别名：{escape(aliases)}\n"
            f"将填入：[bold]{escape(terminal_command_template(selected))}[/bold]"
        )

    def _selected_entry(
        self,
    ) -> (
        TerminalCommandIndexEntry
        | TaskViewItem
        | SessionListItem
        | WorkspaceFileItem
        | AgentDescriptor
        | None
    ):
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
                else workspace_file_template(selected)
                if isinstance(selected, WorkspaceFileItem)
                else terminal_agent_template(selected)
                if isinstance(selected, AgentDescriptor)
                else terminal_command_template(selected)
            )
            self.dismiss(template)

    async def _switch_provider(self) -> None:
        if self._provider == "files":
            await self._cancel_file_search(cancel_index=False)
        self._provider = {
            "commands": "tasks",
            "tasks": "sessions",
            "sessions": "files",
            "files": "agents",
            "agents": "commands",
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
        elif self._provider == "files":
            title.update("[bold]文件 QuickOpen[/bold] · Tab 切换命令 · 选择后仅填入输入框")
            query.placeholder = "搜索相对路径、文件名或扩展名…"
            self._schedule_file_search("", refresh=self._file_result is None)
        elif self._provider == "agents":
            title.update(
                "[bold]Agent QuickOpen[/bold] · "
                "Tab 切换命令 · 选择后仅填入输入框"
            )
            query.placeholder = "搜索 Agent 名称、说明、状态、能力或工具…"
            await self._load_agents()
        else:
            title.update(
                "[bold]命令 QuickOpen[/bold] · "
                "Tab 切换任务/会话/文件/Agent · 选择后仅填入输入框"
            )
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

    async def _load_agents(self) -> None:
        if self._agent_loaded or self._agent_loading or self._engine is None:
            return
        service = getattr(self._engine, "agent_control", None)
        if service is None:
            self._agent_error = "Agent 权威快照尚未初始化。"
            return
        self._agent_loading = True
        self._agent_error = ""
        try:
            snapshot = await service.snapshot()
            self._agent_items = snapshot.agents
            self._agent_warnings = snapshot.warnings
            self._agent_revision = snapshot.revision
            self._agent_loaded = True
        except Exception:
            self._agent_error = "Agent 权威快照读取失败，请稍后重试。"
        finally:
            self._agent_loading = False

    def _schedule_file_search(self, query: str, *, refresh: bool = False) -> None:
        task = self._file_query_task
        if task is not None and not task.done():
            task.cancel()
        self._file_query_task = asyncio.create_task(
            self._load_files(query, refresh=refresh),
            name="tui-workspace-file-search",
        )

    async def _load_files(self, query: str, *, refresh: bool) -> None:
        index = getattr(self._engine, "workspace_file_index", None)
        if index is None:
            self._file_error = "Workspace 文件索引尚未初始化。"
            self._file_loading = False
            await self._refresh_results(query)
            return
        self._file_loading = True
        self._file_error = ""
        self._file_items = ()
        await self._refresh_results(query)
        try:
            result = await index.search(query, limit=200, refresh=refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._file_error = "Workspace 文件索引读取失败，请检查目录权限后重试。"
        else:
            current_query = self.query_one("#command-quick-open-query", Input).value
            if self._provider == "files" and current_query == query:
                self._file_result = result
                self._file_items = result.items
        finally:
            if self._file_query_task is asyncio.current_task():
                self._file_query_task = None
                self._file_loading = False
                current_query = self.query_one("#command-quick-open-query", Input).value
                await self._refresh_results(current_query)

    async def _cancel_file_search(self, *, cancel_index: bool) -> None:
        task = self._file_query_task
        self._file_query_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        index = getattr(self._engine, "workspace_file_index", None)
        if cancel_index and index is not None and index.building:
            await index.cancel()

    async def on_unmount(self) -> None:
        await self._cancel_file_search(cancel_index=True)


__all__ = ["CommandQuickOpenScreen"]
