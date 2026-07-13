"""Shared Textual formatting for authoritative completion receipts."""

from __future__ import annotations

from typing import Any

from naumi_agent.runs.models import CompletionReceipt


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


def _change_status(status: str) -> str:
    return {
        "modified": "修改",
        "added": "新增",
        "deleted": "删除",
        "renamed": "重命名",
        "untracked": "新增",
        "copied": "复制",
        "conflicted": "冲突",
        "restored": "还原",
        "removed_untracked": "删除",
    }.get(status, _plain(status or "变化"))


def _change_summary(changes: tuple[Any, ...]) -> str:
    order = ("删除", "新增", "修改", "重命名", "还原", "冲突")
    counts: dict[str, int] = {}
    for item in changes:
        label = _change_status(item.status)
        counts[label] = counts.get(label, 0) + 1
    labels = sorted(
        counts,
        key=lambda label: order.index(label) if label in order else len(order),
    )
    return " · ".join(f"{label} {counts[label]} 个文件" for label in labels)


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


__all__ = ["completion_outcome_label", "format_completion_receipt_markdown"]
