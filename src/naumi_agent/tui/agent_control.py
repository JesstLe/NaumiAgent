"""Textual Agent Control Center backed by authoritative backend snapshots."""

from __future__ import annotations

from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)

from naumi_agent.agent_control import AgentControlSnapshot

AGENT_CONTROL_TABS = ("agents", "executions", "team")
_TAB_LABELS = {"agents": "Agent", "executions": "执行", "team": "协作"}
_TERMINAL_EXECUTION_STATUSES = {
    "completed", "error", "failed", "timeout", "max_turns", "cancelled",
}


def format_agent_control_markdown(
    snapshot: AgentControlSnapshot,
    tab: str,
    selected_id: str,
) -> str:
    """Render one Agent Control tab from a validated authoritative snapshot."""
    if not isinstance(snapshot, AgentControlSnapshot):
        raise TypeError("snapshot 必须是 AgentControlSnapshot。")
    if tab not in AGENT_CONTROL_TABS:
        raise ValueError(f"未知 Agent Control 标签: {tab}")
    summary = snapshot.summary
    lines = [
        f"## Agent Control Center · {_TAB_LABELS[tab]}",
        "",
        (
            f"revision {snapshot.revision} · Agent {summary.total_agents} · "
            f"运行 {summary.active_agents} · 需注意 {summary.attention_agents} · "
            f"可停止 {summary.stoppable_executions} · 消息 {summary.pending_messages}"
        ),
        f"最后更新：{_plain(snapshot.generated_at) or '-'}",
    ]
    if tab == "agents":
        lines.extend(_format_agent(snapshot, selected_id))
    elif tab == "executions":
        lines.extend(_format_execution(snapshot, selected_id))
    else:
        lines.extend(_format_team(snapshot, selected_id))
    for warning in snapshot.warnings[:5]:
        lines.append(f"- 警告：{_plain(warning)}")
    return "\n".join(lines).strip()


class AgentControlScreen(Screen[None]):
    """Full-page Textual view backed only by ``engine.agent_control``."""

    BINDINGS = [
        Binding("escape", "close", "返回"),
        Binding("tab", "next_tab", "下一标签", priority=True),
        Binding("shift+tab", "previous_tab", "上一标签", priority=True),
        Binding("[", "previous_tab", "上一标签"),
        Binding("]", "next_tab", "下一标签"),
        Binding("left", "previous_tab", "上一标签", show=False),
        Binding("right", "next_tab", "下一标签", show=False),
        Binding("r", "refresh", "刷新"),
        Binding("x", "request_stop", "停止"),
        Binding("y", "confirm_stop", "确认停止", show=False),
        Binding("n", "cancel_stop", "取消停止", show=False),
    ]

    DEFAULT_CSS = """
    AgentControlScreen {
        layout: vertical;
        background: $background;
    }
    #agent-title {
        height: 3;
        padding: 1 2;
        text-style: bold;
        color: $accent;
    }
    #agent-tabs {
        height: 1fr;
        margin: 0 1;
    }
    .agent-body {
        height: 1fr;
    }
    .agent-list {
        width: 42;
        min-width: 30;
        height: 1fr;
        border: round $primary;
        background: $surface;
    }
    .agent-content {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #agent-error {
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
        self.snapshot: AgentControlSnapshot | None = None
        self.selected_tab = "agents"
        self.selected_id = ""
        self.stop_confirmation_task_id = ""
        self.action_pending_task_id = ""
        self._entry_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Agent Control Center · 后端权威 Agent 视图", id="agent-title")
        with TabbedContent(initial="agents", id="agent-tabs"):
            for tab, label in _TAB_LABELS.items():
                with TabPane(label, id=tab):
                    with Horizontal(classes="agent-body"):
                        yield ListView(id=f"agent-list-{tab}", classes="agent-list")
                        yield Markdown(
                            "正在加载 Agent 权威快照…",
                            id=f"agent-content-{tab}",
                            classes="agent-content",
                        )
        yield Static("", id="agent-error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_snapshot()

    @on(TabbedContent.TabActivated)
    async def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = str(event.pane.id or "agents")
        if tab_id not in AGENT_CONTROL_TABS:
            return
        self.selected_tab = tab_id
        self.selected_id = ""
        self.stop_confirmation_task_id = ""
        await self._rebuild_list()

    @on(ListView.Highlighted)
    def on_list_highlighted(self, event: ListView.Highlighted) -> None:
        if event.control.id != f"agent-list-{self.selected_tab}":
            return
        index = event.control.index
        if index is None or index < 0 or index >= len(self._entry_ids):
            return
        self.selected_id = self._entry_ids[index]
        self._render_snapshot()

    @work(exclusive=True, group="agent-control-refresh", exit_on_error=False)
    async def refresh_snapshot(self) -> None:
        error = self.query_one("#agent-error", Static)
        if self.snapshot is None:
            self._content_widget().update(
                "正在加载 Agent 权威快照…"
            )
        error.update("")
        try:
            snapshot = await self.engine.agent_control.snapshot()
        except Exception as exc:
            if self.snapshot is None:
                self._content_widget().update(
                    "## Agent Control Center\n\nAgent 快照暂时不可用。"
                )
            error.update(f"刷新失败，已保留上一次快照：{type(exc).__name__} — {exc}")
            return
        self.snapshot = snapshot
        if self.action_pending_task_id:
            execution = next(
                (
                    item for item in snapshot.executions
                    if item.task_id == self.action_pending_task_id
                ),
                None,
            )
            if execution is None or execution.status in _TERMINAL_EXECUTION_STATUSES:
                self.action_pending_task_id = ""
        await self._rebuild_list()

    async def _rebuild_list(self) -> None:
        list_view = self.query_one(f"#agent-list-{self.selected_tab}", ListView)
        entries = self._entries()
        previous_id = self.selected_id
        await list_view.clear()
        self._entry_ids = [entry_id for entry_id, _ in entries]
        if entries:
            await list_view.extend(
                ListItem(Label(label)) for _, label in entries
            )
            index = self._entry_ids.index(previous_id) if previous_id in self._entry_ids else 0
            list_view.index = index
            self.selected_id = self._entry_ids[index]
        else:
            self.selected_id = ""
        self._render_snapshot()

    def _entries(self) -> list[tuple[str, str]]:
        if self.snapshot is None:
            return []
        if self.selected_tab == "agents":
            return [
                (item.name, f"{item.name} · {item.state} · 任务 {item.task_count}")
                for item in self.snapshot.agents
            ]
        if self.selected_tab == "executions":
            return [
                (
                    item.task_id,
                    f"{item.task_id} · {item.status} · {item.agent_name}"
                    + (" · 可停止" if item.stop_supported else ""),
                )
                for item in self.snapshot.executions
            ]
        return [
            *(
                (
                    f"message:{item.timestamp}:{item.sender}:{item.topic}",
                    f"消息 · {item.sender} → {item.recipient or 'all'} · {item.topic}",
                )
                for item in self.snapshot.team_messages
            ),
            *(
                (f"blackboard:{item.key}", f"黑板 · {item.key} · v{item.version}")
                for item in self.snapshot.blackboard
            ),
        ]

    def _render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        self._content_widget().update(
            format_agent_control_markdown(
                self.snapshot,
                self.selected_tab,
                self.selected_id,
            )
        )

    def action_previous_tab(self) -> None:
        self._select_tab(-1)

    def action_next_tab(self) -> None:
        self._select_tab(1)

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_request_stop(self) -> None:
        if self.selected_tab != "executions" or self.snapshot is None:
            return
        execution = next(
            (item for item in self.snapshot.executions if item.task_id == self.selected_id),
            None,
        )
        if execution is None or not execution.stop_supported or self.action_pending_task_id:
            return
        self.stop_confirmation_task_id = execution.task_id
        self.query_one("#agent-error", Static).update(
            f"确认停止 {execution.task_id}？按 y 确认，n/Esc 取消。"
        )

    def action_confirm_stop(self) -> None:
        if not self.stop_confirmation_task_id or self.action_pending_task_id:
            return
        task_id = self.stop_confirmation_task_id
        self.stop_confirmation_task_id = ""
        self.action_pending_task_id = task_id
        self.query_one("#agent-error", Static).update("正在请求停止…")
        self._stop_execution(task_id)

    def action_cancel_stop(self) -> None:
        if not self.stop_confirmation_task_id:
            return
        self.stop_confirmation_task_id = ""
        self.query_one("#agent-error", Static).update("")

    def action_close(self) -> None:
        if self.stop_confirmation_task_id:
            self.action_cancel_stop()
            return
        self.app.pop_screen()

    @work(exclusive=True, group="agent-control-stop", exit_on_error=False)
    async def _stop_execution(self, task_id: str) -> None:
        try:
            result = await self.engine.subagent_manager.stop_execution(
                task_id,
                "用户在 Textual Agent 控制中心确认停止。",
            )
        except Exception as exc:
            self.action_pending_task_id = ""
            self.query_one("#agent-error", Static).update(
                f"停止请求失败：{type(exc).__name__} — {exc}"
            )
            return
        self.query_one("#agent-error", Static).update(result.message)
        if not result.accepted:
            self.action_pending_task_id = ""
        self.refresh_snapshot()

    def _select_tab(self, delta: int) -> None:
        tabs = list(AGENT_CONTROL_TABS)
        index = tabs.index(self.selected_tab)
        self.selected_tab = tabs[(index + delta) % len(tabs)]
        self.query_one(TabbedContent).active = self.selected_tab

    def _content_widget(self) -> Markdown:
        return self.query_one(f"#agent-content-{self.selected_tab}", Markdown)


def _format_agent(snapshot: AgentControlSnapshot, selected_id: str) -> list[str]:
    if not snapshot.agents:
        return ["", "暂无 Agent"]
    item = next((value for value in snapshot.agents if value.name == selected_id), None)
    if item is None:
        item = snapshot.agents[0]
    return [
        "",
        f"### `{_code(item.name)}`",
        f"- 描述：{_plain(item.description) or '-'}",
        f"- 类型：{item.kind} · 状态：{item.state} · 任务：{item.task_count}",
        f"- 模型：`{_code(item.model_tier)}` · 权限：{_plain(item.permission_level)}",
        f"- 能力：{', '.join(_plain(value) for value in item.capabilities) or '-'}",
        f"- 工具：{', '.join(f'`{_code(value)}`' for value in item.tools) or '-'}",
        f"- age：{item.age_ms}ms · heartbeat：{item.heartbeat_age_ms}ms",
    ]


def _format_execution(snapshot: AgentControlSnapshot, selected_id: str) -> list[str]:
    if not snapshot.executions:
        return ["", "暂无执行记录"]
    item = next(
        (value for value in snapshot.executions if value.task_id == selected_id),
        None,
    )
    if item is None:
        item = snapshot.executions[0]
    return [
        "",
        f"### 执行 `{_code(item.task_id)}`",
        f"- Agent：`{_code(item.agent_name)}`",
        f"- 状态：{item.status} · 阶段：{item.phase}",
        f"- 当前工具：`{_code(item.current_tool or '-')}`",
        f"- 最近工具：{', '.join(f'`{_code(value)}`' for value in item.recent_tools) or '-'}",
        f"- 耗时：{item.elapsed_ms}ms · heartbeat：{item.heartbeat_age_ms}ms",
        f"- Token：{item.total_tokens} · ${item.total_cost_usd:.4f} · {item.turns} 轮",
        f"- 描述：{_plain(item.description) or '-'}",
        f"- 操作：{'可停止' if item.stop_supported else '不可停止'}",
        *( [f"- 错误：{_plain(item.error)}"] if item.error else [] ),
    ]


def _format_team(snapshot: AgentControlSnapshot, selected_id: str) -> list[str]:
    if not snapshot.team_messages and not snapshot.blackboard:
        return ["", "暂无团队消息或黑板记录"]
    if selected_id.startswith("blackboard:"):
        item = next(
            (
                value for value in snapshot.blackboard
                if f"blackboard:{value.key}" == selected_id
            ),
            None,
        )
        if item is not None:
            return [
                "",
                f"### 黑板 `{_code(item.key)}`",
                f"- 作者：{_plain(item.author)} · 版本：{item.version}",
                f"- 值摘要：{_plain(item.value_summary)}",
            ]
    message = next(
        (
            value for value in snapshot.team_messages
            if f"message:{value.timestamp}:{value.sender}:{value.topic}" == selected_id
        ),
        snapshot.team_messages[0] if snapshot.team_messages else None,
    )
    if message is not None:
        return [
            "",
            f"### 消息 · {_plain(message.topic)}",
            f"- {_plain(message.sender)} → {_plain(message.recipient or 'all')}",
            f"- 优先级：{message.priority}",
            f"- 内容：{_plain(message.content)}",
        ]
    item = snapshot.blackboard[0]
    return [
        "",
        f"### 黑板 `{_code(item.key)}`",
        f"- 作者：{_plain(item.author)} · 版本：{item.version}",
        f"- 值摘要：{_plain(item.value_summary)}",
    ]


def _plain(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()[:500]


def _code(value: Any) -> str:
    return _plain(value).replace("`", "ˋ")


__all__ = ["AgentControlScreen", "format_agent_control_markdown"]
