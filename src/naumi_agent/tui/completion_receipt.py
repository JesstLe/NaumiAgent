"""Shared Textual formatting for authoritative completion receipts."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from naumi_agent.runs.models import CompletionReceipt


def format_completion_receipt_text(
    value: CompletionReceipt | dict[str, Any],
) -> Text:
    """Render a semantic Rich completion receipt for Textual."""
    receipt = (
        value
        if isinstance(value, CompletionReceipt)
        else CompletionReceipt.from_dict(value)
    )
    outcome = {
        "completed": ("已完成", "green"),
        "partial": ("部分完成", "yellow"),
        "failed": ("失败", "red"),
        "cancelled": ("已取消", "yellow"),
    }[receipt.outcome]
    task_changes = tuple(item for item in receipt.changes if item.scope != "background")
    background_changes = tuple(item for item in receipt.changes if item.scope == "background")
    rows = [_rich_row(("完成回执 · ", "bold cyan"), outcome)]
    if receipt.summary:
        rows.append(Text(_plain(receipt.summary)))

    if receipt.validations:
        for item in receipt.validations[:2]:
            passed = item.status == "passed"
            counts = _validation_counts(item)
            row = _rich_row(
                (f"验证{'通过' if passed else '失败'}", "green" if passed else "red"),
                (" · ", None),
                (_plain(item.command or "未知命令"), "cyan"),
            )
            if counts:
                row.append(f" · {counts}")
            rows.append(row)
        if len(receipt.validations) > 2:
            rows.append(_rich_row((f"另有 {len(receipt.validations) - 2} 项验证", "dim")))
    elif task_changes:
        rows.append(_rich_row(("未验证 · 本轮任务改动尚无验证证据", "yellow")))

    if task_changes:
        row = Text("影响 · ")
        for index, (label, count, style) in enumerate(_change_summary_items(task_changes)):
            if index:
                row.append(" · ")
            row.append(f"{label} {count} 个文件", style=style)
        rows.append(row)
    if background_changes:
        rows.append(_rich_row((f"工作区另有 {len(background_changes)} 项运行时变化", "dim")))

    reviewable_changes = tuple(
        item
        for item in task_changes
        if item.status not in {"removed_untracked", "restored"}
    )
    if not receipt.git_state.available and receipt.outcome != "completed":
        rows.append(_rich_git_summary(receipt))
    elif receipt.git_state.available and (
        reviewable_changes or receipt.git_state.behind
    ):
        rows.append(_rich_git_summary(receipt))

    for item in receipt.approvals[:3]:
        if item.decision in {"denied", "error"}:
            rows.append(
                _rich_row(
                    (
                        f"审批 · {_plain(item.tool_name)} · "
                        f"{_approval_label(item.decision)}",
                        "red",
                    )
                )
            )
    for item in receipt.unverified[:3]:
        rows.append(_rich_row((f"未验证 · {_plain(item)}", "yellow")))
    for item in receipt.risks[:3]:
        style = "red" if item.level in {"high", "critical"} else "yellow"
        rows.append(_rich_row((f"风险 · {_plain(item.message)}", style)))
    for item in receipt.next_actions[:3]:
        rows.append(_rich_row((f"下一步 · {_plain(item.label)}", "cyan")))
    return Text("\n").join(rows)


def format_completion_receipt_markdown(
    value: CompletionReceipt | dict[str, Any],
) -> str:
    """Render a bounded Chinese completion card for the legacy Textual TUI."""
    receipt = (
        value
        if isinstance(value, CompletionReceipt)
        else CompletionReceipt.from_dict(value)
    )
    outcome = {
        "completed": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
    }[receipt.outcome]
    task_changes = tuple(item for item in receipt.changes if item.scope != "background")
    background_changes = tuple(item for item in receipt.changes if item.scope == "background")
    lines = [
        f"## 完成回执 · {outcome}",
        "",
        _plain(receipt.summary),
    ]
    if receipt.validations:
        lines.append("")
        for item in receipt.validations[:2]:
            counts = _validation_counts(item)
            suffix = f" · {counts}" if counts else ""
            lines.append(
                f"- 验证{'通过' if item.status == 'passed' else '失败'} · "
                f"{_code(item.command)}{suffix}"
            )
        if len(receipt.validations) > 2:
            lines.append(f"- 另有 {len(receipt.validations) - 2} 项验证")
    elif task_changes:
        lines.extend(["", "- 未验证：本轮任务改动尚无验证证据"])

    if task_changes:
        lines.append(f"- 影响：{_change_summary(task_changes)}")
    if background_changes:
        lines.append(f"- 工作区另有 {len(background_changes)} 项运行时变化")

    reviewable_changes = tuple(
        item
        for item in task_changes
        if item.status not in {"removed_untracked", "restored"}
    )
    if not receipt.git_state.available and receipt.outcome != "completed":
        lines.append(f"- {_git_summary(receipt)}")
    elif receipt.git_state.available and (
        reviewable_changes or receipt.git_state.behind
    ):
        lines.append(f"- {_git_summary(receipt)}")

    actionable_approvals = tuple(
        item for item in receipt.approvals if item.decision in {"denied", "error"}
    )
    if actionable_approvals:
        for item in actionable_approvals[:3]:
            lines.append(
                f"- 审批：{_plain(item.tool_name)} · {_approval_label(item.decision)}"
            )
    for item in receipt.unverified[:3]:
        lines.append(f"- 未验证：{_plain(item)}")
    for item in receipt.risks[:3]:
        lines.append(f"- 风险：{_plain(item.message)}")
    for item in receipt.next_actions[:3]:
        lines.append(f"- 下一步：{_plain(item.label)}")
    return "\n".join(line for line in lines if line is not None).strip()


def completion_outcome_label(receipt: CompletionReceipt) -> str:
    return {
        "completed": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
    }[receipt.outcome]


def _git_summary(receipt: CompletionReceipt) -> str:
    git = receipt.git_state
    if not git.available:
        return "Git 未核查"
    parts = [f"Git {_plain(git.branch or 'detached')}"]
    parts.append("工作区有改动" if git.dirty else "工作区干净")
    if git.ahead:
        parts.append(f"领先 {git.ahead}")
    if git.behind:
        parts.append(f"落后 {git.behind}")
    return " · ".join(parts)


def _validation_counts(item: Any) -> str:
    parts: list[str] = []
    if item.passed:
        parts.append(f"通过 {item.passed}")
    if item.failed:
        parts.append(f"失败 {item.failed}")
    if item.skipped:
        parts.append(f"跳过 {item.skipped}")
    if not parts and item.exit_code is not None and item.scope != "文件系统":
        parts.append(f"退出码 {item.exit_code}")
    return " · ".join(parts)


_CHANGE_VIEW = {
    "modified": ("修改", "yellow"),
    "added": ("新增", "green"),
    "deleted": ("删除", "red"),
    "renamed": ("重命名", "cyan"),
    "untracked": ("新增", "green"),
    "copied": ("复制", "cyan"),
    "conflicted": ("冲突", "bold red"),
    "restored": ("还原", "blue"),
    "removed_untracked": ("删除", "red"),
}


def _change_status(status: str) -> str:
    return _CHANGE_VIEW.get(status, (_plain(status or "变化"), "dim"))[0]


def _change_summary(changes: tuple[Any, ...]) -> str:
    return " · ".join(
        f"{label} {count} 个文件"
        for label, count, _style in _change_summary_items(changes)
    )


def _change_summary_items(changes: tuple[Any, ...]) -> list[tuple[str, int, str]]:
    order = ("删除", "新增", "修改", "重命名", "复制", "还原", "冲突")
    counts: dict[str, tuple[int, str]] = {}
    for item in changes:
        label, style = _CHANGE_VIEW.get(
            item.status,
            (_plain(item.status or "变化"), "dim"),
        )
        count, current_style = counts.get(label, (0, style))
        counts[label] = (count + 1, current_style)
    labels = sorted(
        counts,
        key=lambda label: order.index(label) if label in order else len(order),
    )
    return [(label, counts[label][0], counts[label][1]) for label in labels]


def _approval_label(decision: str) -> str:
    return {
        "allowed_once": "仅本次允许",
        "allowed_session": "本会话允许",
        "bypass": "已绕过确认",
        "denied": "已拒绝",
        "error": "确认失败",
    }.get(decision, _plain(decision or "已记录"))


def _plain(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def _code(value: Any) -> str:
    return f"`{_plain(value).replace('`', 'ˋ')}`"


def _rich_row(*segments: tuple[str, str | None]) -> Text:
    row = Text()
    for value, style in segments:
        row.append(value, style=style)
    return row


def _rich_git_summary(receipt: CompletionReceipt) -> Text:
    git = receipt.git_state
    if not git.available:
        return _rich_row(("Git 未核查", "yellow"))
    row = _rich_row(
        (f"Git {_plain(git.branch or 'detached')}", "cyan"),
        (" · ", None),
        ("工作区有改动" if git.dirty else "工作区干净", "yellow" if git.dirty else "green"),
    )
    if git.ahead:
        row.append(" · ")
        row.append(f"领先 {git.ahead}", style="green")
    if git.behind:
        row.append(" · ")
        row.append(f"落后 {git.behind}", style="red")
    return row


__all__ = [
    "completion_outcome_label",
    "format_completion_receipt_markdown",
    "format_completion_receipt_text",
]
