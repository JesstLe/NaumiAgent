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
    passed = sum(item.status == "passed" for item in receipt.validations)
    failed = sum(item.status == "failed" for item in receipt.validations)
    lines = [
        f"## 完成回执 · {outcome}",
        "",
        _plain(receipt.summary),
        "",
        (
            f"- 改动 **{len(receipt.changes)}** · "
            f"验证 **{passed}/{len(receipt.validations)}**（失败 {failed}） · "
            f"审批 **{len(receipt.approvals)}** · 风险 **{len(receipt.risks)}**"
        ),
        f"- {_git_summary(receipt)}",
    ]
    if receipt.validations:
        lines.extend(["", "### 验证"])
        for item in receipt.validations[:4]:
            counts = _validation_counts(item)
            suffix = f" · {counts}" if counts else ""
            lines.append(
                f"- {'通过' if item.status == 'passed' else '失败'} · "
                f"{_code(item.command)}{suffix}"
            )
    else:
        lines.extend(["", "- 验证：未记录验证命令"])

    if receipt.changes:
        lines.extend(["", "### 文件改动"])
        for item in receipt.changes[:5]:
            stats = _change_stats(item.additions, item.deletions)
            source = f" · 来源 {_plain(item.source_tool)}" if item.source_tool else ""
            lines.append(
                f"- {_change_status(item.status)} · {_code(item.path)}"
                f"{f' · {stats}' if stats else ''}{source}"
            )

    if receipt.approvals:
        lines.extend(["", "### 审批"])
        for item in receipt.approvals[:3]:
            lines.append(
                f"- {_plain(item.tool_name)} · {_approval_label(item.decision)}"
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
    if not parts and item.exit_code is not None:
        parts.append(f"退出码 {item.exit_code}")
    return " · ".join(parts)


def _change_stats(additions: int, deletions: int) -> str:
    parts = []
    if additions:
        parts.append(f"+{additions}")
    if deletions:
        parts.append(f"-{deletions}")
    return " ".join(parts)


def _change_status(status: str) -> str:
    return {
        "modified": "修改",
        "added": "新增",
        "deleted": "删除",
        "renamed": "重命名",
        "untracked": "未跟踪",
        "conflicted": "冲突",
        "restored": "还原",
    }.get(status, _plain(status or "变化"))


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
