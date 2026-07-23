"""Shared terminal formatting for authoritative completion receipts."""

from __future__ import annotations

import re
import shlex
from typing import Any

from rich.text import Text

from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.safety.guardrails import OutputGuardrail

_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def format_completion_receipt_text(
    value: CompletionReceipt | dict[str, Any],
    harness_receipt: dict[str, Any] | None = None,
    *,
    detail_shortcut: str = "Ctrl+O",
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
    rows.extend(_harness_receipt_rows(harness_receipt))

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
    if has_correlated_harness_receipt(receipt, harness_receipt):
        rows.append(
            _rich_row(
                ("操作 · ", "dim"),
                (f"{_plain(detail_shortcut)} 查看详情", "cyan"),
                (" · ", "dim"),
                (_harness_detail_command(receipt.run_id), "cyan"),
            )
        )
    rows.append(
        _rich_row(
            ("操作 · ", "dim"),
            (_copy_receipt_command(receipt.receipt_id), "cyan"),
        )
    )
    return Text("\n").join(rows)


def _harness_receipt_rows(value: dict[str, Any] | None) -> list[Text]:
    if not isinstance(value, dict):
        return []
    raw_checks = value.get("checks")
    checks = tuple(
        item
        for item in (raw_checks[:50] if isinstance(raw_checks, (list, tuple)) else ())
        if isinstance(item, dict)
    )
    raw_criteria = value.get("criteria")
    criteria = tuple(
        item
        for item in (
            raw_criteria[:100] if isinstance(raw_criteria, (list, tuple)) else ()
        )
        if isinstance(item, dict)
    )
    raw_warnings = value.get("warnings")
    warnings = tuple(
        str(item)
        for item in (
            raw_warnings[:20] if isinstance(raw_warnings, (list, tuple)) else ()
        )
        if str(item)
    )
    status = str(value.get("status") or "")
    status_label, status_style = {
        "completed_verified": ("Harness 已验证", "green"),
        "completed_unverified": ("Harness 未验证", "yellow"),
        "blocked": ("Harness 阻塞", "red"),
    }.get(status, ("Harness 状态未知", "dim"))
    passed = sum(item.get("status") == "passed" for item in checks)
    satisfied = sum(item.get("status") == "satisfied" for item in criteria)
    evidence_ids = {
        str(evidence_id)
        for item in criteria
        for evidence_id in _harness_evidence_ids(item)
        if str(evidence_id)
    }
    rows = [
        _rich_row(
            (status_label, status_style),
            (
                f" · 检查 {passed}/{len(checks)} · 准则 "
                f"{satisfied}/{len(criteria)} · 证据 {len(evidence_ids)}",
                None,
            ),
        )
    ]
    failed = tuple(item for item in checks if item.get("status") != "passed")
    for item in failed[:2]:
        rows.append(
            _rich_row(
                (
                    f"{_harness_check_label(str(item.get('status') or ''))} · "
                    f"{_plain(item.get('id') or '未知检查')}",
                    "red" if item.get("status") == "failed" else "yellow",
                )
            )
        )
    if len(failed) > 2:
        rows.append(_rich_row((f"另有 {len(failed) - 2} 项未通过检查", "dim")))
    if satisfied < len(criteria):
        rows.append(
            _rich_row(
                (
                    f"准则未满足 · {satisfied}/{len(criteria)}",
                    "red" if status == "blocked" else "yellow",
                )
            )
        )
    rows.extend(_rich_row((f"Harness 警告 · {_plain(item)}", "yellow")) for item in warnings[:2])
    if len(warnings) > 2:
        rows.append(_rich_row((f"另有 {len(warnings) - 2} 条 Harness 警告", "dim")))
    return rows


def _harness_evidence_ids(item: dict[str, Any]) -> tuple[Any, ...]:
    value = item.get("evidence_ids")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(value[:100])


def _harness_check_label(status: str) -> str:
    return {
        "failed": "检查失败",
        "timed_out": "检查超时",
        "cancelled": "检查取消",
        "blocked_by_policy": "策略阻止",
        "infrastructure_error": "基础设施异常",
        "stale": "结果失效",
        "missing": "缺少检查",
    }.get(status, f"检查状态 {status or '未知'}")


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
    lines.append(
        f"- 操作：{_code(_copy_receipt_command(receipt.receipt_id))}"
    )
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
    return OutputGuardrail.redact(sanitize_completion_receipt_inline(value))


def sanitize_completion_receipt_inline(value: Any) -> str:
    """Return one bounded receipt field safe for terminal and plain-text output."""
    text = _OSC_RE.sub("", str(value or ""))
    text = _CSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return " ".join(text.replace("\r", "\n").split()).strip()


def _code(value: Any) -> str:
    return f"`{_plain(value).replace('`', 'ˋ')}`"


def _copy_receipt_command(receipt_id: Any) -> str:
    safe_receipt_id = sanitize_completion_receipt_inline(receipt_id)
    return f"/copy receipt {shlex.quote(safe_receipt_id)}"


def _harness_detail_command(run_id: Any) -> str:
    safe_run_id = sanitize_completion_receipt_inline(run_id)
    return f"/harness detail {shlex.quote(safe_run_id)}"


def has_correlated_harness_receipt(
    receipt: CompletionReceipt,
    harness_receipt: dict[str, Any] | None,
) -> bool:
    if not isinstance(harness_receipt, dict):
        return False
    return (
        str(harness_receipt.get("run_id") or "").strip() == receipt.run_id
        and harness_receipt.get("status")
        in {"completed_verified", "completed_unverified", "blocked"}
    )


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
    "has_correlated_harness_receipt",
    "sanitize_completion_receipt_inline",
]
