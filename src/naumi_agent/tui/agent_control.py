"""Textual Agent Control Center backed by authoritative backend snapshots."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

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
from naumi_agent.tools.base import ToolCall

AGENT_CONTROL_TABS = ("agents", "executions", "results", "recovery", "team")
_TAB_LABELS = {
    "agents": "Agent",
    "executions": "执行",
    "results": "结果",
    "recovery": "恢复",
    "team": "协作",
}
_TERMINAL_EXECUTION_STATUSES = {
    "completed", "error", "failed", "timeout", "max_turns", "cancelled",
}
_SAFE_AGENT_DEEP_LINK_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


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
            f"可停止 {summary.stoppable_executions} · 消息 {summary.pending_messages} · "
            f"结果 {summary.durable_results_visible} · "
            f"未读 {summary.durable_unread_results}"
        ),
        *(
            [_durable_publication_line(summary)]
            if (
                summary.durable_publications_pending
                or summary.durable_publications_claimed
                or summary.durable_publications_expired
                or summary.durable_publications_quarantined
            )
            else []
        ),
        *(
            [_durable_capacity_line(summary)]
            if summary.durable_capacity_configured
            else []
        ),
        *(
            [_recovery_catalog_line(snapshot)]
            if snapshot.recovery_catalog.items or snapshot.recovery_catalog.truncated
            else []
        ),
        f"最后更新：{_plain(snapshot.generated_at) or '-'}",
    ]
    if tab == "agents":
        lines.extend(_format_agent(snapshot, selected_id))
    elif tab == "executions":
        lines.extend(_format_execution(snapshot, selected_id))
    elif tab == "results":
        lines.extend(_format_result(snapshot, selected_id))
    elif tab == "recovery":
        lines.extend(_format_recovery(snapshot, selected_id))
    else:
        lines.extend(_format_team(snapshot, selected_id))
    for warning in snapshot.warnings[:5]:
        lines.append(f"- 警告：{_plain(warning)}")
    return "\n".join(lines).strip()


def _durable_capacity_line(summary: Any) -> str:
    active = (
        f"{summary.durable_active_jobs}/{summary.durable_max_active_jobs}"
    )
    waiting = (
        f"{summary.durable_waiting_jobs}/{summary.durable_max_waiters}"
    )
    if summary.durable_recovery_required_jobs:
        status = (
            f"🔴 待恢复 {summary.durable_recovery_required_jobs}"
        )
    elif summary.durable_waiting_jobs or summary.durable_reclaimable_jobs:
        status = (
            f"🟡 可接管 {summary.durable_reclaimable_jobs}"
        )
    else:
        status = "🟢 正常"
    return (
        f"**共享 Agent capacity**：`{active}` · 等待 `{waiting}` · {status}"
    )


def _durable_publication_line(summary: Any) -> str:
    status = (
        f"🔴 已隔离 {summary.durable_publications_quarantined}"
        if summary.durable_publications_quarantined
        else (
            f"🔴 过期 claim {summary.durable_publications_expired}"
            if summary.durable_publications_expired
            else "🟡 等待投递"
        )
    )
    return (
        "**Agent publication**："
        f"待处理 `{summary.durable_publications_pending}` · "
        f"已认领 `{summary.durable_publications_claimed}` · {status}"
    )


def _recovery_catalog_line(snapshot: AgentControlSnapshot) -> str:
    catalog = snapshot.recovery_catalog
    attention = sum(
        item.recovery_state in {
            "recovery_required",
            "outcome_unknown",
            "publication_claim_expired",
            "publication_quarantined",
        }
        for item in catalog.items
    )
    suffix = "+" if catalog.truncated else ""
    return (
        f"**恢复目录**：`{len(catalog.items)}{suffix}` 项 · "
        f"需裁决 `{attention}` · 人工动作不重放模型"
    )


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
        Binding("u", "resolve_recovery_unknown", "恢复裁决"),
        Binding("v", "acknowledge_result", "标记已读"),
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

    def __init__(
        self,
        engine: Any,
        *,
        initial_tab: str = "agents",
        initial_id: str = "",
    ) -> None:
        super().__init__()
        if initial_tab not in AGENT_CONTROL_TABS:
            raise ValueError(f"未知 Agent Control 标签: {initial_tab}")
        normalized_initial_id = str(initial_id or "")
        if normalized_initial_id and not _SAFE_AGENT_DEEP_LINK_ID.fullmatch(
            normalized_initial_id
        ):
            raise ValueError("Agent 初始定位名称必须为 1 到 200 个可显示字符。")
        self.engine = engine
        self.snapshot: AgentControlSnapshot | None = None
        self.selected_tab = initial_tab
        self.selected_id = normalized_initial_id
        self.stop_confirmation_task_id = ""
        self.action_pending_task_id = ""
        self.recovery_action_pending_id = ""
        self.result_ack_pending_id = ""
        self.action_notice = ""
        self._entry_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Agent Control Center · 后端权威 Agent 视图", id="agent-title")
        with TabbedContent(initial=self.selected_tab, id="agent-tabs"):
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
        changed_tab = tab_id != self.selected_tab
        self.selected_tab = tab_id
        if changed_tab:
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
        error.update(self.action_notice)
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
        if self.result_ack_pending_id:
            result = next(
                (
                    item for item in snapshot.results
                    if item.delivery_id == self.result_ack_pending_id
                ),
                None,
            )
            if result is not None and result.acknowledged:
                self.result_ack_pending_id = ""
        if self.recovery_action_pending_id:
            recovery = next(
                (
                    item for item in snapshot.recovery_catalog.items
                    if item.kind == "job"
                    and item.job_id == self.recovery_action_pending_id
                ),
                None,
            )
            if recovery is None or recovery.recovery_state == "outcome_unknown":
                self.recovery_action_pending_id = ""
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
        if self.selected_tab == "results":
            return [
                (
                    item.delivery_id,
                    f"{'已读' if item.acknowledged else '未读'} · "
                    f"{item.task_id} · {item.status} · {item.agent_name}"
                    + (" · 已脱敏/截断" if item.content_truncated else ""),
                )
                for item in self.snapshot.results
            ]
        if self.selected_tab == "recovery":
            return [
                (
                    f"recovery:{item.kind}:{item.item_id}",
                    f"{_recovery_state_label(item.recovery_state)} · "
                    f"{item.item_id} · {item.agent_name} · "
                    f"{_session_scope_label(item.session_scope)}",
                )
                for item in self.snapshot.recovery_catalog.items
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
        self.action_notice = ""
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
        self.action_notice = ""
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
        self.action_notice = ""
        self.query_one("#agent-error", Static).update("")

    def action_resolve_recovery_unknown(self) -> None:
        if (
            self.selected_tab != "recovery"
            or self.snapshot is None
            or self.recovery_action_pending_id
        ):
            return
        item = next(
            (
                value for value in self.snapshot.recovery_catalog.items
                if f"recovery:{value.kind}:{value.item_id}" == self.selected_id
            ),
            None,
        )
        if (
            item is None
            or item.kind != "job"
            or item.recovery_state != "recovery_required"
            or item.session_scope != "current"
        ):
            return
        self.action_notice = ""
        self.recovery_action_pending_id = item.job_id
        self.query_one("#agent-error", Static).update(
            "正在提交精确恢复裁决…"
        )
        self._resolve_recovery_unknown(
            item.job_id,
            item.request_sha256,
            item.claim_epoch,
            item.receipt_sha256,
        )

    def action_acknowledge_result(self) -> None:
        if (
            self.selected_tab != "results"
            or self.snapshot is None
            or self.result_ack_pending_id
        ):
            return
        item = next(
            (
                value for value in self.snapshot.results
                if value.delivery_id == self.selected_id
            ),
            None,
        )
        if item is None or item.acknowledged:
            return
        self.action_notice = ""
        self.result_ack_pending_id = item.delivery_id
        self.query_one("#agent-error", Static).update(
            "正在签发结果已读回执…"
        )
        self._acknowledge_result(item.delivery_id, item.delivery_sha256)

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
            self.action_notice = f"停止请求失败：{type(exc).__name__} — {exc}"
            self.query_one("#agent-error", Static).update(self.action_notice)
            return
        self.action_notice = result.message
        self.query_one("#agent-error", Static).update(self.action_notice)
        if not result.accepted:
            self.action_pending_task_id = ""
        self.refresh_snapshot()

    @work(exclusive=True, group="agent-control-recovery", exit_on_error=False)
    async def _resolve_recovery_unknown(
        self,
        job_id: str,
        request_sha256: str,
        claim_epoch: int,
        receipt_sha256: str,
    ) -> None:
        try:
            session = getattr(self.engine, "_session", None)
            if session is None:
                session = await self.engine.get_or_create_session()
            result = (
                await self.engine.subagent_manager.resolve_recovery_unknown(
                    session_id=str(getattr(session, "id", "") or ""),
                    job_id=job_id,
                    expected_request_sha256=request_sha256,
                    expected_claim_epoch=claim_epoch,
                    expected_latest_receipt_sha256=receipt_sha256,
                )
            )
        except Exception as exc:
            self.recovery_action_pending_id = ""
            self.action_notice = f"恢复裁决失败：{type(exc).__name__} — {exc}"
            self.query_one("#agent-error", Static).update(self.action_notice)
            return
        self.recovery_action_pending_id = ""
        self.action_notice = result.message
        self.query_one("#agent-error", Static).update(self.action_notice)
        self.refresh_snapshot()

    @work(exclusive=True, group="agent-control-result-ack", exit_on_error=False)
    async def _acknowledge_result(
        self,
        delivery_id: str,
        delivery_sha256: str,
    ) -> None:
        try:
            result = await self.engine.execute_tool(
                ToolCall(
                    id=f"tui-agent-result-ack-{uuid4()}",
                    name="agent_result_acknowledge",
                    arguments=json.dumps(
                        {
                            "delivery_id": delivery_id,
                            "delivery_sha256": delivery_sha256,
                        },
                        ensure_ascii=False,
                    ),
                ),
                agent_name="tui",
            )
        except Exception:
            self.result_ack_pending_id = ""
            self.action_notice = (
                "结果已读确认失败，请刷新后重试；持久结果未被改写。"
            )
            self.query_one("#agent-error", Static).update(self.action_notice)
            return
        self.result_ack_pending_id = ""
        self.action_notice = result.content
        self.query_one("#agent-error", Static).update(self.action_notice)
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
        (
            "- 执行后端：独立 Agent Worker"
            if item.worker_backend == "independent"
            else "- 执行后端：内嵌降级"
        ),
        f"- 当前工具：`{_code(item.current_tool or '-')}`",
        f"- 最近工具：{', '.join(f'`{_code(value)}`' for value in item.recent_tools) or '-'}",
        (
            "- Worker 工具范围："
            + _tool_scope_summary(item.worker_tool_scope)
        ),
        (
            f"- Worker 合同：请求 `{_code(_short_digest(item.worker_request_sha256))}`"
            f" · 结果 `{_code(_short_digest(item.worker_result_sha256))}`"
            + (
                f" · 降级 `{_code(item.worker_contract_failure_code)}`"
                if item.worker_contract_failure_code
                else ""
            )
        ),
        (
            f"- 持久任务：`{_code(_short_digest(item.worker_job_id))}`"
            f" · 状态 `{_code(item.worker_job_state or '未接入')}`"
            f" · epoch {item.worker_claim_epoch}"
            + (
                f" · 降级 `{_code(item.worker_job_failure_code)}`"
                if item.worker_job_failure_code
                else ""
            )
        ),
        f"- 耗时：{item.elapsed_ms}ms · heartbeat：{item.heartbeat_age_ms}ms",
        (
            f"- 持久心跳：{item.heartbeat_phase or '未启用'}"
            + (
                f" · 降级 `{_code(item.heartbeat_failure_code)}`"
                if item.heartbeat_failure_code
                else ""
            )
        ),
        f"- Token：{item.total_tokens} · ${item.total_cost_usd:.4f} · {item.turns} 轮",
        f"- 描述：{_plain(item.description) or '-'}",
        f"- 操作：{'可停止' if item.stop_supported else '不可停止'}",
        *( [f"- 错误：{_plain(item.error)}"] if item.error else [] ),
    ]


def _short_digest(value: str) -> str:
    return value[:12] if value else "待生成"


def _format_result(snapshot: AgentControlSnapshot, selected_id: str) -> list[str]:
    if not snapshot.results:
        return ["", "当前会话暂无持久结果"]
    item = next(
        (value for value in snapshot.results if value.delivery_id == selected_id),
        None,
    )
    if item is None:
        item = snapshot.results[0]
    return [
        "",
        f"### 持久结果 `{_code(item.task_id)}`",
        f"- Agent：`{_code(item.agent_name)}`",
        f"- 状态：{item.status} · 原因：`{_code(item.reason_code or '-')}`",
        f"- 投递时间：{_plain(item.delivered_at)}",
        (
            f"- Token：{item.total_tokens} · ${item.total_cost_usd:.4f} · "
            f"{item.turns} 轮 · {item.response_bytes} bytes"
        ),
        f"- 结果摘要：`{_code(_short_digest(item.result_sha256))}`",
        f"- 投递摘要：`{_code(_short_digest(item.delivery_sha256))}`",
        (
            f"- ✅ 已读：{_plain(item.acknowledged_at)} · 回执 "
            f"`{_code(_short_digest(item.acknowledgement_receipt_sha256))}`"
            if item.acknowledged
            else "- 🟡 未读：按 `v` 签发不可变已读回执。"
        ),
        *(
            ["- ⚠️ 展示内容已经脱敏或截断；原始结果仍保留在加密持久层。"]
            if item.content_truncated
            else []
        ),
        f"- 任务摘录：{_long_plain(item.task_excerpt) or '-'}",
        f"- 回复摘录：{_long_plain(item.response_excerpt) or '-'}",
        *(
            [f"- 错误摘录：{_long_plain(item.error_excerpt)}"]
            if item.error_excerpt
            else []
        ),
    ]


def _tool_scope_summary(values: tuple[str, ...]) -> str:
    if not values:
        return "-"
    visible = ", ".join(f"`{_code(value)}`" for value in values[:8])
    return (
        f"{visible}，另 {len(values) - 8} 项"
        if len(values) > 8
        else visible
    )


def _format_recovery(
    snapshot: AgentControlSnapshot,
    selected_id: str,
) -> list[str]:
    catalog = snapshot.recovery_catalog
    if not catalog.items:
        return ["", "✅ 暂无 Agent 恢复条目"]
    item = next(
        (
            value for value in catalog.items
            if f"recovery:{value.kind}:{value.item_id}" == selected_id
        ),
        catalog.items[0],
    )
    return [
        "",
        f"### Agent 恢复事实 · `{_code(item.item_id)}`",
        f"- 状态：{_recovery_state_label(item.recovery_state)}",
        f"- 类型：{item.kind} · Agent：`{_code(item.agent_name)}`",
        f"- Job：`{_code(item.job_id)}` · 状态：{item.job_state}",
        *(
            [f"- Publication：`{_code(item.publication_id)}`"]
            if item.publication_id
            else []
        ),
        f"- 会话范围：{_session_scope_label(item.session_scope)}",
        (
            f"- claim epoch：{item.claim_epoch}"
            + (
                f" · 到期：{_plain(item.claim_expires_at)}"
                if item.claim_expires_at
                else ""
            )
        ),
        *(
            [f"- 投递尝试：{item.attempt_count}"]
            if item.kind == "publication"
            else []
        ),
        f"- 发生时间：{_plain(item.occurred_at)}",
        f"- 请求摘要：`{_code(_short_digest(item.request_sha256))}`",
        f"- 回执摘要：`{_code(_short_digest(item.receipt_sha256))}`",
        f"- 原因码：`{_code(item.reason_code)}`",
        *(
            ["- ⚠️ 按 `u` 将该过期 running Job 精确收口为 `unknown`。"]
            if (
                item.kind == "job"
                and item.recovery_state == "recovery_required"
                and item.session_scope == "current"
            )
            else ["- ℹ️ 当前条目没有可用的人工恢复动作。"]
        ),
        "- ℹ️ 恢复裁决不会自动重放模型，也不会删除持久证据。",
        *(
            ["- ⚠️ 目录已达到 50 项展示上限，仅展示高优先级有界前缀。"]
            if catalog.truncated
            else []
        ),
    ]


def _recovery_state_label(state: str) -> str:
    return {
        "claim_active": "🔵 claim 仍有效",
        "worker_active": "🔵 worker 仍在运行",
        "reclaimable_prestart": "🟡 启动前 claim 可接管",
        "recovery_required": "🔴 running Job 需要恢复裁决",
        "outcome_unknown": "🔴 执行结果未知",
        "publication_pending": "🟡 终态结果等待发布",
        "publication_claim_expired": "🔴 发布 claim 已过期",
        "publication_quarantined": "🔴 发布失败已隔离",
    }.get(state, state)


def _session_scope_label(scope: str) -> str:
    return {
        "current": "当前会话",
        "other": "其他会话",
        "unknown": "会话未知",
    }.get(scope, "会话未知")


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


def _long_plain(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:2000]


def _code(value: Any) -> str:
    return _plain(value).replace("`", "ˋ")


__all__ = ["AgentControlScreen", "format_agent_control_markdown"]
