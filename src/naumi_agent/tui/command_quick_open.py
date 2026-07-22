"""Textual command QuickOpen backed by the authoritative terminal index."""

from __future__ import annotations

from collections.abc import Sequence

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
    ) -> None:
        super().__init__()
        self._entries = tuple(entries)
        self._recent_commands = tuple(recent_commands)[:20]
        self._results: tuple[TerminalCommandIndexEntry, ...] = ()

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("[bold]命令 QuickOpen[/bold] · 选择后仅填入输入框")
            yield Input(
                placeholder="搜索命令、别名、说明、类别或风险…",
                id="command-quick-open-query",
                max_length=200,
            )
            yield ListView(id="command-quick-open-results")
            yield Static("", id="command-quick-open-detail")
            yield Static(
                "↑/↓ 选择 · Enter 填入 · Esc 取消 · 不会自动执行",
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

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
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

    def _render_entry(self, entry: TerminalCommandIndexEntry) -> str:
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
            detail.update("[yellow]没有匹配命令。[/yellow]")
            return
        aliases = "、".join(selected.aliases) if selected.aliases else "无"
        detail.update(
            f"类别：{escape(selected.category)} · 来源：{escape(selected.source)} · "
            f"别名：{escape(aliases)}\n"
            f"将填入：[bold]{escape(terminal_command_template(selected))}[/bold]"
        )

    def _selected_entry(self) -> TerminalCommandIndexEntry | None:
        if not self._results:
            return None
        index = self.query_one("#command-quick-open-results", ListView).index
        return self._results[max(0, min(len(self._results) - 1, index or 0))]

    def _accept_selected(self) -> None:
        selected = self._selected_entry()
        if selected is not None:
            self.dismiss(terminal_command_template(selected))


__all__ = ["CommandQuickOpenScreen"]
