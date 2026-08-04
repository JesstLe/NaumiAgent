"""Textual fallback for the authoritative Workbench overview and governance."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractStoreError,
    default_experiment_seed,
)
from naumi_agent.workbench.proposal_governance import (
    DEFER_PRESET_DAYS,
    ProposalAction,
    ProposalGovernanceConflictError,
    proposal_defer_until_for_preset,
)

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
            f"审阅项 {_integer(counts.get('reviews'))} · "
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
    notice: str = "",
) -> str:
    """Render waiting approvals plus actionable open/approved Proposals."""
    snapshot = _validate_snapshot(value)
    reviews = _review_records(snapshot)
    if not reviews:
        lines = [
            "## Reviews",
            "",
            "当前没有待审 Approval、开放 Proposal 或待转换的 approved Proposal。",
        ]
        if notice:
            lines.extend(["", f"**{_plain(notice)}**"])
        if error:
            lines.extend(["", f"- 决策失败：{_plain(error)}"])
        return "\n".join(lines)
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
        if item.get("review_kind") == "proposal":
            lines.append(
                f"- {marker} Proposal · "
                f"{_plain(item.get('title') or item.get('id'))} · "
                f"{_risk_label(item.get('risk_level'))}"
            )
        else:
            lines.append(
                f"- {marker} Approval · "
                f"{_plain(item.get('title') or item.get('id'))} · "
                f"{_plain(item.get('requester')) or '未知发起者'}"
            )
    if notice:
        lines.extend(["", f"**{_plain(notice)}**"])
    if item_kind := _normalized(selected.get("review_kind")):
        if item_kind == "proposal":
            if error:
                lines.extend(["", f"- 决策失败：{_plain(error)}"])
            return _append_proposal_review(lines, selected)
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


def _append_proposal_review(
    lines: list[str],
    proposal: Mapping[str, Any],
) -> str:
    lines.extend(
        [
            "",
            "### Proposal 决策",
            f"- 标题：{_plain(proposal.get('title') or proposal.get('id'))}",
            f"- 状态：{_plain(proposal.get('state')) or 'open'}",
            f"- 风险：{_risk_label(proposal.get('risk_level'))}",
            f"- 类型：{_plain(proposal.get('proposal_kind')) or 'manual'}",
            f"- 来源：{_plain(proposal.get('source_kind')) or 'manual'} · "
            f"{_plain(proposal.get('source_id')) or '-'} · "
            f"r{_integer(proposal.get('source_revision'))}",
            f"- 影响：{_plain(proposal.get('impact_scope')) or '未填写'}",
            f"- Agent：{_plain(proposal.get('agent_id')) or '-'}",
            f"- Task：{_plain(proposal.get('task_id')) or '-'}",
        ]
    )
    files = _strings(proposal.get("intended_files"))
    validation = _strings(proposal.get("validation_plan"))
    lines.extend(["", f"### 目标文件 · {len(files)}"])
    lines.extend(f"- `{_code(path)}`" for path in files[:8])
    if not files:
        lines.append("- 未声明")
    lines.extend(["", f"### 验证计划 · {len(validation)}"])
    lines.extend(f"- {_plain(step)}" for step in validation[:8])
    if not validation:
        lines.append("- 未声明")
    if _normalized(proposal.get("state")) == "approved":
        lines.extend(
            [
                "",
                (
                    "> Proposal 已批准，但尚未执行代码；Experiment Contract 只冻结 "
                    "baseline、scope、预算和验证约束，不会修改代码、运行实验或授予发布权限。"
                ),
                "",
                "`c` 签发或重开 Experiment Contract · `r` 刷新 · `Esc` 返回",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "> 批准只进入下一 policy gate，不执行代码，也不授予实验资格。",
                "",
                "`a` 批准 · `x` 拒绝 · `d` 延后 · `m` 合并 · `r` 刷新 · `Esc` 返回",
            ]
        )
    return "\n".join(lines)


class ProposalDecisionScreen(ModalScreen[dict[str, Any] | None]):
    """Collect one explicit Proposal decision without persisting draft input."""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    DEFAULT_CSS = """
    ProposalDecisionScreen {
        align: center middle;
    }
    ProposalDecisionScreen > Container {
        width: 76;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: thick $warning 80%;
        background: $surface;
    }
    ProposalDecisionScreen Label,
    ProposalDecisionScreen Input {
        width: 1fr;
        margin: 0 0 1 0;
    }
    ProposalDecisionScreen .proposal-decision-error {
        color: $error;
        margin: 0 0 1 0;
    }
    ProposalDecisionScreen Horizontal {
        width: auto;
        height: auto;
    }
    ProposalDecisionScreen Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(self, *, action: ProposalAction, title: str) -> None:
        super().__init__()
        self.proposal_action = action
        self.proposal_title = title

    def compose(self) -> ComposeResult:
        label = {
            ProposalAction.APPROVE: "批准",
            ProposalAction.REJECT: "拒绝",
            ProposalAction.DEFER: "延后",
        }[self.proposal_action]
        with Container():
            heading = (
                "延后 Proposal"
                if self.proposal_action is ProposalAction.DEFER
                else f"确认{label} Proposal？"
            )
            yield Label(f"[bold]{heading}[/bold]")
            yield Label(_plain(self.proposal_title) or "未命名 Proposal")
            if self.proposal_action in {ProposalAction.REJECT, ProposalAction.DEFER}:
                yield Input(
                    placeholder=f"填写{label}原因（必填，最多 2000 字符）",
                    max_length=2_000,
                    id="proposal-decision-note",
                )
            if self.proposal_action is ProposalAction.DEFER:
                yield Input(
                    value="7",
                    placeholder="延后天数：1、7 或 30",
                    max_length=2,
                    id="proposal-defer-days",
                )
            error = Static("", classes="proposal-decision-error", id="proposal-decision-error")
            error.display = False
            yield error
            with Horizontal():
                button_label = (
                    "提交延后"
                    if self.proposal_action is ProposalAction.DEFER
                    else f"确认{label}"
                )
                yield Button(button_label, variant="warning", id="proposal-confirm")
                yield Button("取消", variant="primary", id="proposal-cancel")

    def on_mount(self) -> None:
        inputs = list(self.query(Input))
        (inputs[0] if inputs else self.query_one("#proposal-confirm", Button)).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#proposal-decision-note")
    def on_note_submitted(self) -> None:
        if self.proposal_action is ProposalAction.DEFER:
            if not self.query_one("#proposal-decision-note", Input).value.strip():
                self._submit()
                return
            self.query_one("#proposal-defer-days", Input).focus()
            return
        self._submit()

    @on(Input.Submitted, "#proposal-defer-days")
    def on_defer_days_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proposal-cancel":
            self.dismiss(None)
            return
        if event.button.id == "proposal-confirm":
            self._submit()

    def _submit(self) -> None:
        note_input = self.query("#proposal-decision-note").first(Input)
        note = note_input.value.strip() if note_input is not None else ""
        if self.proposal_action in {ProposalAction.REJECT, ProposalAction.DEFER} and not note:
            error = self.query_one("#proposal-decision-error", Static)
            error.update(
                "延后原因不能为空。"
                if self.proposal_action is ProposalAction.DEFER
                else "拒绝原因不能为空。"
            )
            error.display = True
            if note_input is not None:
                note_input.focus()
            return
        defer_days = 0
        if self.proposal_action is ProposalAction.DEFER:
            value = self.query_one("#proposal-defer-days", Input).value.strip()
            try:
                defer_days = int(value)
            except ValueError:
                defer_days = 0
            if defer_days not in DEFER_PRESET_DAYS:
                error = self.query_one("#proposal-decision-error", Static)
                error.update("延后天数只支持 1、7 或 30。")
                error.display = True
                self.query_one("#proposal-defer-days", Input).focus()
                return
        self.dismiss(
            {
                "action": self.proposal_action.value,
                "decision_note": note,
                "defer_days": defer_days,
            }
        )


class ProposalMergeScreen(ModalScreen[str | None]):
    """Select one backend-projected merge target without persisting UI state."""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]
    DEFAULT_CSS = """
    ProposalMergeScreen {
        align: center middle;
    }
    ProposalMergeScreen > Container {
        width: 84;
        max-width: 94%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $warning 80%;
        background: $surface;
    }
    ProposalMergeScreen OptionList {
        width: 1fr;
        height: auto;
        max-height: 16;
        margin: 1 0;
    }
    """

    def __init__(self, *, title: str, targets: list[Mapping[str, Any]]) -> None:
        super().__init__()
        self.proposal_title = title
        self.targets = targets[:20]

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("[bold]合并 Proposal[/bold]")
            yield Label(_plain(self.proposal_title) or "未命名 Proposal")
            yield Label("仅显示同一 Candidate 的较新 open revision。")
            yield OptionList(
                *[
                    Option(
                        (
                            f"r{_integer(target.get('source_revision'))} · "
                            f"{_plain(target.get('title') or target.get('id'))} · "
                            f"{_plain(target.get('id'))}"
                        ),
                        id=_normalized(target.get("id")),
                    )
                    for target in self.targets
                ],
                id="proposal-merge-targets",
            )
            yield Label("↑/↓ 选择 · Enter 合并 · Esc 取消")

    def on_mount(self) -> None:
        self.query_one("#proposal-merge-targets", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#proposal-merge-targets")
    def on_target_selected(self, event: OptionList.OptionSelected) -> None:
        target_id = _normalized(event.option.id)
        self.dismiss(target_id or None)


class ExperimentContractIssueScreen(ModalScreen[bool]):
    """Confirm the separate approved Proposal to Contract transition."""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]
    DEFAULT_CSS = ProposalDecisionScreen.DEFAULT_CSS

    def __init__(self, *, title: str) -> None:
        super().__init__()
        self.proposal_title = title

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("[bold]签发不可执行 Experiment Contract？[/bold]")
            yield Label(_plain(self.proposal_title) or "未命名 Proposal")
            yield Label(
                "该操作只冻结 baseline、scope、预算和验证约束；"
                "不会修改代码、运行实验或批准发布。"
            )
            with Horizontal():
                yield Button("确认签发", variant="warning", id="contract-confirm")
                yield Button("取消", variant="primary", id="contract-cancel")

    def on_mount(self) -> None:
        self.query_one("#contract-confirm", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "contract-confirm")


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
        Binding("a", "approve_proposal", "批准 Proposal", show=False),
        Binding("x", "reject_proposal", "拒绝 Proposal", show=False),
        Binding("d", "defer_proposal", "延后 Proposal", show=False),
        Binding("m", "merge_proposal", "合并 Proposal", show=False),
        Binding("c", "issue_experiment_contract", "签发实验契约", show=False),
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
        self.review_notice = ""
        self.proposal_action_pending = False

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
            max(0, len(_review_records(snapshot)) - 1),
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
        self.review_error = ""
        self._render_snapshot()
        self.refresh_review_detail()

    def action_select_previous(self) -> None:
        if self.selected_tab == "worktrees":
            self.selected_worktree_index = max(0, self.selected_worktree_index - 1)
            self._render_snapshot()
        elif self.selected_tab == "reviews":
            self.selected_review_index = max(0, self.selected_review_index - 1)
            self.review_detail = None
            self.review_error = ""
            self._render_snapshot()
            self.refresh_review_detail()

    def action_select_next(self) -> None:
        if self.selected_tab == "worktrees" and self.snapshot is not None:
            last = max(0, len(_records(self.snapshot.get("worktrees"))) - 1)
            self.selected_worktree_index = min(last, self.selected_worktree_index + 1)
            self._render_snapshot()
        elif self.selected_tab == "reviews" and self.snapshot is not None:
            last = max(0, len(_review_records(self.snapshot)) - 1)
            self.selected_review_index = min(last, self.selected_review_index + 1)
            self.review_detail = None
            self.review_error = ""
            self._render_snapshot()
            self.refresh_review_detail()

    @work(exclusive=True, group="workbench-review", exit_on_error=False)
    async def refresh_review_detail(self) -> None:
        if self.snapshot is None:
            return
        reviews = _review_records(self.snapshot)
        if not reviews:
            self.review_detail = None
            self.review_loading = False
            self.review_error = ""
            self._render_snapshot()
            return
        index = min(len(reviews) - 1, max(0, self.selected_review_index))
        selected = reviews[index]
        if selected.get("review_kind") == "proposal":
            self.review_detail = None
            self.review_loading = False
            self._render_snapshot()
            return
        review_id = _normalized(selected.get("id"))
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

    def action_approve_proposal(self) -> None:
        self._begin_proposal_action(ProposalAction.APPROVE)

    def action_reject_proposal(self) -> None:
        self._begin_proposal_action(ProposalAction.REJECT)

    def action_defer_proposal(self) -> None:
        self._begin_proposal_action(ProposalAction.DEFER)

    def action_merge_proposal(self) -> None:
        if self.proposal_action_pending:
            return
        selected = self._selected_review()
        if selected is None or selected.get("review_kind") != "proposal":
            return
        try:
            target_ids = _proposal_merge_target_ids(selected.get("merge_target_ids"))
        except WorkbenchSnapshotError:
            self.review_error = "Merge 目标快照格式无效，请刷新 Workbench。"
            self._render_snapshot()
            return
        if not target_ids:
            self.review_error = "当前没有同 Candidate 的较新 open Proposal 可合并。"
            self._render_snapshot()
            return
        decision = self.engine._permission_checker.check(
            "workbench_govern_proposal",
            {"proposal_id": selected.get("id"), "action": "merge"},
        )
        if not decision.allowed:
            self.review_error = "当前权限模式不允许治理 Proposal。"
            self._render_snapshot()
            return
        raw_proposals = self.snapshot.get("proposals", []) if self.snapshot else []
        if not isinstance(raw_proposals, (list, tuple)):
            raw_proposals = []
        proposal_by_id = {
            item["id"]: dict(item)
            for item in raw_proposals
            if isinstance(item, Mapping)
            and isinstance(item.get("id"), str)
            and item["id"] in target_ids
        }
        targets = [
            proposal_by_id[target_id]
            for target_id in target_ids
            if target_id in proposal_by_id
        ]
        if len(targets) != len(target_ids):
            self.review_error = "Merge 目标快照不完整，请刷新 Workbench。"
            self._render_snapshot()
            return

        def on_target(target_id: str | None) -> None:
            if not target_id:
                return
            self._start_proposal_action(
                _normalized(selected.get("id")),
                ProposalAction.MERGE,
                decision_note="",
                merge_into_id=target_id,
                confirmed=decision.requires_confirmation,
            )

        self.app.push_screen(
            ProposalMergeScreen(
                title=_plain(selected.get("title") or selected.get("id")),
                targets=targets,
            ),
            on_target,
        )

    def action_issue_experiment_contract(self) -> None:
        if self.proposal_action_pending:
            return
        selected = self._selected_review()
        if not (
            selected is not None
            and selected.get("review_kind") == "proposal"
            and _normalized(selected.get("state")) == "approved"
            and _normalized(selected.get("source_kind")) == "evolution_candidate"
        ):
            return
        proposal_id = _normalized(selected.get("id"))
        decision = self.engine._permission_checker.check(
            "evolution_issue_experiment_contract",
            {"proposal_id": proposal_id},
        )
        if not decision.allowed:
            self.review_error = "当前权限模式不允许签发 Experiment Contract。"
            self._render_snapshot()
            return
        if not decision.requires_confirmation:
            self.submit_experiment_contract(proposal_id, confirmed=False)
            return

        def on_confirmed(confirmed: bool) -> None:
            if confirmed:
                self.submit_experiment_contract(proposal_id, confirmed=True)

        self.app.push_screen(
            ExperimentContractIssueScreen(
                title=_plain(selected.get("title") or proposal_id),
            ),
            on_confirmed,
        )

    @work(exclusive=True, group="workbench-contract-issue", exit_on_error=False)
    async def submit_experiment_contract(
        self,
        proposal_id: str,
        *,
        confirmed: bool,
    ) -> None:
        if self.proposal_action_pending:
            return
        self.proposal_action_pending = True
        try:
            decision = self.engine._permission_checker.check(
                "evolution_issue_experiment_contract",
                {"proposal_id": proposal_id},
            )
            if not decision.allowed:
                raise WorkbenchSnapshotError(
                    "当前权限模式不允许签发 Experiment Contract。"
                )
            if decision.requires_confirmation and not confirmed:
                raise WorkbenchSnapshotError("签发 Experiment Contract 需要明确确认。")
            if self.snapshot is None:
                raise WorkbenchSnapshotError("Workbench 权威快照不可用。")
            session_id = _normalized(self.snapshot.get("session_id"))
            self.review_error = ""
            self.review_notice = "正在签发或重开 durable Experiment Contract…"
            self._render_snapshot()
            contract = await self.engine.evolution_experiment_contract_issuer.issue(
                self.engine.workspace_root,
                session_id=session_id,
                proposal_id=proposal_id,
                seed=default_experiment_seed(proposal_id),
            )
            authority = await self.engine.evolution_experiment_contract_store.get(
                self.engine.workspace_root,
                contract.contract_id,
            )
            if authority is None:
                raise RuntimeError("Experiment Contract authority 未持久化。")
            self.snapshot = _validate_snapshot(
                await self.engine.workbench_service.dashboard_snapshot(session_id),
                session_id=session_id,
            )
        except EvolutionExperimentContractStoreError as exc:
            logger.warning("TUI Experiment Contract store failed (%s)", exc.code)
            self.review_error = "Experiment Contract 状态库损坏或暂不可用。"
            self.review_notice = ""
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "TUI Experiment Contract issue failed (%s)",
                type(exc).__name__,
            )
            self.review_error = (
                _plain(exc)
                if isinstance(exc, ValueError)
                else "Experiment Contract 签发暂时失败。"
            )
            self.review_notice = ""
        else:
            self.review_error = ""
            self.review_notice = (
                f"Experiment Contract {authority.contract_id} 已持久化；"
                "execution_ready=false，尚未修改代码或批准发布。"
            )
        finally:
            self.proposal_action_pending = False
            self._render_snapshot()

    def _begin_proposal_action(self, action: ProposalAction) -> None:
        if self.proposal_action_pending:
            return
        selected = self._selected_review()
        if selected is None or selected.get("review_kind") != "proposal":
            return
        decision = self.engine._permission_checker.check(
            "workbench_govern_proposal",
            {"proposal_id": selected.get("id"), "action": action.value},
        )
        if not decision.allowed:
            self.review_error = "当前权限模式不允许治理 Proposal。"
            self._render_snapshot()
            return
        if action is ProposalAction.APPROVE and not decision.requires_confirmation:
            self._start_proposal_action(
                _normalized(selected.get("id")),
                action,
                decision_note="",
                confirmed=False,
            )
            return

        def on_decision(result: dict[str, Any] | None) -> None:
            if result is None:
                return
            self._start_proposal_action(
                _normalized(selected.get("id")),
                ProposalAction(result["action"]),
                decision_note=result.get("decision_note", ""),
                defer_days=int(result.get("defer_days", 0)),
                confirmed=decision.requires_confirmation,
            )

        self.app.push_screen(
            ProposalDecisionScreen(
                action=action,
                title=_plain(selected.get("title") or selected.get("id")),
            ),
            on_decision,
        )

    def _start_proposal_action(
        self,
        proposal_id: str,
        action: ProposalAction,
        *,
        decision_note: str,
        defer_days: int = 0,
        merge_into_id: str = "",
        confirmed: bool,
    ) -> None:
        if self.proposal_action_pending:
            return
        self.proposal_action_pending = True
        self.submit_proposal_action(
            proposal_id,
            action,
            decision_note=decision_note,
            defer_days=defer_days,
            merge_into_id=merge_into_id,
            confirmed=confirmed,
        )

    @work(exclusive=True, group="workbench-proposal-action", exit_on_error=False)
    async def submit_proposal_action(
        self,
        proposal_id: str,
        action: ProposalAction,
        *,
        decision_note: str,
        defer_days: int = 0,
        merge_into_id: str = "",
        confirmed: bool,
    ) -> None:
        try:
            decision = self.engine._permission_checker.check(
                "workbench_govern_proposal",
                {
                    "proposal_id": proposal_id,
                    "action": action.value,
                    "defer_days": defer_days,
                    "merge_into_id": merge_into_id,
                },
            )
            if not decision.allowed:
                self.review_error = "当前权限模式不允许治理 Proposal。"
                self._render_snapshot()
                return
            if decision.requires_confirmation and not confirmed:
                self.review_error = "该 Proposal 决策需要明确确认。"
                self._render_snapshot()
                return
            self.review_error = ""
            self.review_notice = "正在提交 Proposal 决策…"
            self._render_snapshot()
            session_id = _normalized(self.snapshot.get("session_id"))  # type: ignore[union-attr]
            defer_until = (
                proposal_defer_until_for_preset(defer_days)
                if action is ProposalAction.DEFER
                else ""
            )
            governance_kwargs: dict[str, Any] = {
                "action": action,
                "reviewer": "Human",
                "decision_note": decision_note,
            }
            if defer_until:
                governance_kwargs["defer_until"] = defer_until
            if merge_into_id:
                governance_kwargs["merge_into_id"] = merge_into_id
            proposal = await self.engine.workbench_service.govern_proposal(
                session_id,
                proposal_id,
                **governance_kwargs,
            )
            if proposal is None:
                raise WorkbenchSnapshotError("Proposal 不存在或不属于当前会话。")
            snapshot = _validate_snapshot(
                await self.engine.workbench_service.dashboard_snapshot(session_id),
                session_id=session_id,
            )
        except ProposalGovernanceConflictError as exc:
            self.review_error = _plain(exc)
            self.review_notice = ""
            await self._refresh_after_proposal_conflict()
            return
        except (RuntimeError, ValueError) as exc:
            logger.warning("TUI Workbench Proposal action failed (%s)", type(exc).__name__)
            self.review_error = (
                _plain(exc) if isinstance(exc, ValueError) else "Proposal 决策暂时失败。"
            )
            self.review_notice = ""
            self._render_snapshot()
            return
        finally:
            self.proposal_action_pending = False
        self.snapshot = snapshot
        if action is ProposalAction.APPROVE:
            self.review_notice = "Proposal 已批准。"
        elif action is ProposalAction.REJECT:
            self.review_notice = "Proposal 已拒绝。"
        elif action is ProposalAction.DEFER:
            self.review_notice = (
                f"Proposal 已延后至 {proposal.get('cooldown_until', '-')}。"
            )
        else:
            self.review_notice = (
                f"Proposal 已合并到 {proposal.get('merged_into_id', '-')}。"
            )
        self.selected_review_index = min(
            self.selected_review_index,
            max(0, len(_review_records(snapshot)) - 1),
        )
        self.review_detail = None
        self._render_snapshot()
        self.refresh_review_detail()

    async def _refresh_after_proposal_conflict(self) -> None:
        if self.snapshot is None:
            return
        session_id = _normalized(self.snapshot.get("session_id"))
        try:
            self.snapshot = _validate_snapshot(
                await self.engine.workbench_service.dashboard_snapshot(session_id),
                session_id=session_id,
            )
        except (RuntimeError, ValueError):
            pass
        self._render_snapshot()

    def _selected_review(self) -> dict[str, Any] | None:
        if self.snapshot is None or self.selected_tab != "reviews":
            return None
        reviews = _review_records(self.snapshot)
        if not reviews:
            return None
        index = min(len(reviews) - 1, max(0, self.selected_review_index))
        return reviews[index]

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
                    notice=self.review_notice,
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


def _review_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    approvals = [
        {**item, "review_kind": "approval"}
        for item in _records(snapshot.get("approvals"))
    ]
    proposals = [
        {**item, "review_kind": "proposal"}
        for item in _records(snapshot.get("proposals"))
        if (
            _normalized(item.get("state")) == "open"
            or (
                _normalized(item.get("state")) == "approved"
                and _normalized(item.get("source_kind")) == "evolution_candidate"
            )
        )
    ]
    return [*approvals, *proposals]


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


def _proposal_merge_target_ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        if value in (None, []):
            return []
        raise WorkbenchSnapshotError("merge_target_ids 必须是不超过 20 项的数组")
    target_ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 128
            or re.search(r"[\x00-\x1f\x7f]", item)
            or item in seen
        ):
            raise WorkbenchSnapshotError("merge_target_ids 包含无效或重复 ID")
        seen.add(item)
        target_ids.append(item)
    return target_ids


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
    "ProposalDecisionScreen",
    "ProposalMergeScreen",
    "WorkbenchOverviewScreen",
    "WorkbenchSnapshotError",
    "format_workbench_overview_markdown",
    "format_workbench_reviews_markdown",
    "format_workbench_worktrees_markdown",
]
