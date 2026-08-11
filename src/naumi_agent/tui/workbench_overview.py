"""Textual fallback for the authoritative Workbench overview and governance."""

from __future__ import annotations

import logging
import math
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
from naumi_agent.workbench.models import ApprovalState
from naumi_agent.workbench.proposal_governance import (
    DEFER_PRESET_DAYS,
    ProposalAction,
    ProposalGovernanceConflictError,
    proposal_defer_until_for_preset,
)
from naumi_agent.workbench.stable_population_finalization import (
    WorkbenchStablePopulationFinalizationProjection,
)
from naumi_agent.workbench.store import ApprovalResolutionConflictError

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
    lines.extend(
        [
            "",
            "### Stable Population Finalization",
            *_format_stable_population_finalization(snapshot),
        ]
    )
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


def _format_stable_population_finalization(
    snapshot: Mapping[str, Any],
) -> list[str]:
    if snapshot.get("stable_population_finalization_error"):
        return [
            "- 状态：暂不可用（内部错误已隐藏）",
            "- 下一步：按 `r` 重新读取 Control Plane 权威状态",
        ]
    raw = snapshot.get("stable_population_finalization")
    if raw is None:
        return ["- 状态：尚未接入 Population finalization authority"]
    projection = WorkbenchStablePopulationFinalizationProjection.model_validate(raw)
    if projection.status == "pending":
        return [
            "- 状态：等待全部远端 member 完成",
            "- 权限：尚未形成 Population Receipt；当前无发布权限",
        ]
    status = (
        "已完成（authority current）"
        if projection.status == "completed"
        else "历史完成（current authority 已撤销）"
    )
    lines = [
        f"- 状态：{status}",
        f"- Candidate：`{_code(projection.candidate_version)}`",
        f"- Members：{projection.completed_members}/{projection.population_denominator}",
        f"- Receipt：`{_code(projection.receipt_id)}`",
    ]
    if projection.invalidation_reasons:
        lines.append(
            "- 撤权原因："
            + "、".join(
                _population_invalidation_reason(reason)
                for reason in projection.invalidation_reasons
            )
        )
    lines.append("- 权限范围：配置/数据 finalization 否；Promotion 否")
    return lines


def _population_invalidation_reason(reason: str) -> str:
    return {
        "member_finalization_authority_changed": "成员签名/控制/凭据已变化",
        "member_receipt_set_changed": "成员回执集合已变化",
        "newer_population_finalization_exists": "存在更新的 Population Receipt",
        "population_finalization_receipt_changed": "Population Receipt 已变化",
        "population_snapshot_not_current": "Population Snapshot 已失效",
    }.get(reason, "权威来源已变化")


def format_workbench_release_markdown(value: Mapping[str, Any]) -> str:
    """Render the shared Stable Population finalization authority page."""
    snapshot = _validate_snapshot(value)
    return "\n".join(
        [
            "## Stable Population Finalization Authority",
            "",
            *_format_stable_population_finalization(snapshot),
        ]
    )


def format_workbench_timeline_markdown(
    value: Mapping[str, Any],
    *,
    selected_index: int = 0,
) -> str:
    """Render bounded persisted Workbench facts without inferring chat events."""
    snapshot = _validate_snapshot(value)
    events = _records(snapshot.get("events"))
    if not events:
        return (
            "## Timeline\n\n"
            "当前会话尚无 Workbench 审计事件。\n\n"
            "Timeline 只展示后端已持久化的事实，不从聊天文本推断事件。"
        )
    index = min(len(events) - 1, max(0, int(selected_index)))
    start = max(0, min(index - 4, len(events) - 9))
    selected = events[index]
    lines = [
        "## Timeline",
        "",
        f"共 {len(events)} 条 · 当前 {index + 1}/{len(events)} · ↑/↓ 选择",
        "",
        "### 事件",
    ]
    for offset, event in enumerate(events[start : start + 9], start=start):
        marker = "▶" if offset == index else "·"
        lines.append(
            f"- {marker} {_timeline_symbol(event)} "
            f"{_timeline_category(event.get('type'))} · "
            f"{_plain(str(event.get('type') or 'unknown').replace('.', ' › '), 160)} · "
            f"{_plain(event.get('subject_id'), 160) or '无对象'}"
        )
    lines.extend(
        [
            "",
            "### 当前事件",
            f"- 级别：{_timeline_severity(selected.get('severity'))}",
            f"- 时间：{_plain(selected.get('timestamp'), 100) or '-'}",
            f"- 执行者：{_plain(selected.get('actor'), 300) or '未知'}",
            f"- 对象：{_plain(selected.get('subject_id'), 300) or '-'}",
            f"- Event：`{_code(selected.get('id') or '-')}`",
        ]
    )
    correlation_id = _plain(selected.get("correlation_id"), 128)
    if correlation_id:
        lines.append(f"- 关联：`{_code(correlation_id)}`")
    payload = _mapping(selected.get("payload"))
    if payload:
        lines.extend(["", "### 证据字段"])
        for key, item in list(payload.items())[:12]:
            lines.append(f"- {_plain(key, 80)}：{_plain(item, 500)}")
        if len(payload) > 12:
            lines.append(f"- 另有 {len(payload) - 12} 个字段")
    else:
        lines.extend(["", "证据字段：无公开字段"])
    return "\n".join(lines)


def _timeline_category(value: Any) -> str:
    event_type = _normalized(value).lower()
    if re.search(r"permission|approval|proposal|review", event_type):
        return "权限"
    if re.search(r"git|worktree|branch|commit|merge", event_type):
        return "Git"
    if re.search(r"harness|pursuit|heartbeat|run\.|daemon|worker", event_type):
        return "Harness"
    if re.search(r"agent|lease|bid|dispatch", event_type):
        return "Agent"
    if re.search(r"tool|validation|check|eval", event_type):
        return "工具"
    return "工作台"


def _timeline_symbol(event: Mapping[str, Any]) -> str:
    severity = _normalized(event.get("severity"))
    if severity in {"error", "critical"}:
        return "🔴"
    if severity == "warning":
        return "🟡"
    return {
        "权限": "🟡",
        "Git": "🟢",
        "Harness": "🟣",
        "Agent": "🔵",
        "工具": "🔷",
        "工作台": "⚪",
    }[_timeline_category(event.get("type"))]


def _timeline_severity(value: Any) -> str:
    return {
        "info": "信息",
        "warning": "警告",
        "error": "错误",
        "critical": "严重",
    }.get(_normalized(value), "未知")


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
            outcome_status = _normalized(item.get("outcome_status"))
            outcome_label = f" · {outcome_status}" if outcome_status else ""
            lines.append(
                f"- {marker} Proposal · "
                f"{_plain(item.get('title') or item.get('id'))} · "
                f"{_risk_label(item.get('risk_level'))}{outcome_label}"
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
    lines.extend(
        [
            "",
            "`a` 批准 · `x` 拒绝 · `r` 刷新 · `Esc` 返回",
        ]
    )
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
    outcome = _mapping(proposal.get("outcome"))
    outcome_status = _normalized(proposal.get("outcome_status"))
    if outcome and _normalized(outcome.get("status")) == "promoted":
        authority = "可验证" if outcome.get("authority_valid") is True else "证据已失效"
        previous = _normalized(outcome.get("stable_previous_outcome_id"))
        lines.extend(
            [
                "",
                "### 稳定推广 Outcome",
                f"- 终态：promoted · {authority}",
                f"- Outcome：`{_code(outcome.get('outcome_id'))}` · "
                f"sequence {_integer(outcome.get('stable_promotion_sequence'))}",
                f"- Decision：`{_code(outcome.get('stable_decision_id'))}`",
                f"- Eligibility：`{_code(outcome.get('stable_eligibility_id'))}`",
                "- Observation Contract："
                f"`{_code(outcome.get('stable_observation_contract_id'))}`",
                "- Population Assessment："
                f"`{_code(outcome.get('stable_population_assessment_id'))}`",
                "- Prior Outcome："
                + (
                    f"`{_code(previous)}` · 已由当前 Outcome 替代"
                    if previous
                    else "无 · 首个 promoted Outcome"
                ),
                "- Supersede Event："
                f"`{_code(outcome.get('stable_supersede_event_id'))}`",
                "- Authority："
                f"head={str(outcome.get('projection_head_authority') is True).lower()} · "
                "outcome="
                f"{str(outcome.get('stable_promotion_outcome_authority') is True).lower()}",
                *(
                    [
                        "- 撤权原因："
                        + ", ".join(_strings(outcome.get("invalidation_reasons")))
                    ]
                    if _strings(outcome.get("invalidation_reasons"))
                    else []
                ),
                "- 长期观察已记录；Learning / Promotion / Execution authority：false",
            ]
        )
    elif outcome:
        authority = "可验证" if outcome.get("authority_valid") is True else "证据已失效"
        before_after = _mapping(outcome.get("before_after_evidence"))
        before_after_label = (
            f"已记录 · {len(before_after.get('lanes') or [])} lanes"
            if outcome.get("before_after_recorded") is True and before_after
            else "尚未记录"
        )
        post_rollback = _mapping(outcome.get("post_rollback_verification"))
        post_rollback_label = (
            "已验证 · fresh boot + launch identity"
            if outcome.get("post_rollback_verification_recorded") is True
            and post_rollback
            else "尚未记录"
        )
        behavioral_matrix = _mapping(
            outcome.get("post_rollback_behavioral_matrix")
        )
        behavioral_matrix_label = (
            f"已记录 · {len(behavioral_matrix.get('lanes') or [])} lanes · "
            f"{_normalized(behavioral_matrix.get('recovery_verdict'))}"
            if outcome.get("post_rollback_behavioral_evaluation_recorded") is True
            and behavioral_matrix
            else "尚未记录"
        )
        long_term = _mapping(outcome.get("long_term_outcome"))
        supersede_event = _mapping(outcome.get("long_term_supersede_event"))
        metrics_label = (
            "已记录" if outcome.get("long_term_metrics_recorded") is True else "尚未记录"
        )
        lines.extend(
            [
                "",
                "### 实施 Outcome",
                f"- 终态：{_plain(outcome_status or outcome.get('status'))} · {authority}",
                f"- Outcome：`{_code(outcome.get('outcome_id'))}`",
                *(
                    [
                        "- Rollback Root："
                        f"`{_code(outcome.get('root_rollback_outcome_id'))}`"
                    ]
                    if _normalized(outcome.get("root_rollback_outcome_id"))
                    != _normalized(outcome.get("outcome_id"))
                    else []
                ),
                f"- Rollback Receipt：`{_code(outcome.get('rollback_receipt_id'))}`",
                f"- Experiment Contract：`{_code(outcome.get('experiment_contract_id'))}`",
                f"- Breach：{', '.join(_strings(outcome.get('breach_reasons'))) or '-'}",
                f"- HAR-08 before/after：{before_after_label}；"
                f"长期指标：{metrics_label}",
            ]
        )
        if before_after:
            lines.extend(
                [
                    f"- Before/After Evidence：`{_code(before_after.get('evidence_id'))}`",
                    "- 口径：实施前 RED baseline → 实施后 GREEN candidate；不代表回滚后评测",
                ]
            )
        lines.append(f"- 回滚后 Runtime：{post_rollback_label}")
        if post_rollback:
            lines.extend(
                [
                    "- Post-Rollback Verification："
                    f"`{_code(post_rollback.get('verification_id'))}`",
                    "- Baseline："
                    f"`{_code(post_rollback.get('baseline_slot_id'))}` · "
                    f"{_normalized(post_rollback.get('baseline_version'))}",
                    "- 口径：installed-runtime mechanical verification；"
                    "行为矩阵由独立 Matrix authority 判定",
                ]
            )
        lines.append(f"- 回滚后行为矩阵：{behavioral_matrix_label}")
        if behavioral_matrix:
            lines.extend(
                [
                    "- Behavioral Matrix："
                    f"`{_code(behavioral_matrix.get('matrix_id'))}`",
                    "- 总体判定："
                    f"{_normalized(behavioral_matrix.get('recovery_verdict'))}；"
                    "长期指标 / Learning / Promotion：未授权",
                ]
            )
            for lane in list(behavioral_matrix.get("lanes") or [])[:4]:
                item = _mapping(lane)
                lines.append(
                    f"  - Lane #{_integer(item.get('order'))}："
                    f"{_normalized(item.get('lane_kind'))} · "
                    f"{_normalized(item.get('platform'))} · "
                    f"{_normalized(item.get('recovery_status'))} · "
                    f"{_normalized(item.get('evidence_kind'))}"
                )
        if long_term:
            lines.extend(
                [
                    "",
                    "### 长期恢复观察",
                    "- Long-Term Outcome："
                    f"`{_code(long_term.get('outcome_id'))}` · "
                    f"revision {_integer(long_term.get('revision_sequence'))}",
                    "- Assessment："
                    f"`{_code(long_term.get('assessment_id'))}` · "
                    f"head #{_integer(long_term.get('assessment_head_sequence'))}",
                    "- Runtime："
                    f"{_plain(long_term.get('runtime_subject_id'))} · "
                    f"`{_code(long_term.get('runtime_binding_id'))}`",
                    "- 持续健康："
                    f"{_integer(long_term.get('observation_seconds'))}s · "
                    f"{_integer(long_term.get('operational_sample_count'))} samples",
                    "- Supersede Event："
                    f"`{_code(supersede_event.get('event_id'))}` · "
                    "历史 rollback fact 保留",
                    "- Authority："
                    "health="
                    f"{str(outcome.get('current_long_term_health_authority') is True).lower()} · "
                    f"head={str(outcome.get('projection_head_authority') is True).lower()} · "
                    f"outcome={str(outcome.get('long_term_outcome_authority') is True).lower()}",
                ]
            )
    elif proposal.get("outcome_error"):
        lines.extend(
            [
                "",
                "### 实施 Outcome",
                "- Outcome source 暂不可用；未把 Proposal 冒充为已完成。",
            ]
        )
    lines.extend(["", f"### 目标文件 · {len(files)}"])
    lines.extend(f"- `{_code(path)}`" for path in files[:8])
    if not files:
        lines.append("- 未声明")
    lines.extend(["", f"### 验证计划 · {len(validation)}"])
    lines.extend(f"- {_plain(step)}" for step in validation[:8])
    if not validation:
        lines.append("- 未声明")
    if outcome and _normalized(outcome.get("status")) == "promoted":
        lines.extend(
            [
                "",
                (
                    "> 该 Proposal 已形成 post-observation promoted Outcome；"
                    "治理审计与 supersession 历史仍保留，但不得再次签发 Experiment "
                    "Contract，也不会自动进入 policy learning。"
                ),
                "",
                "`r` 刷新 · `Esc` 返回",
            ]
        )
    elif outcome:
        lines.extend(
            [
                "",
                (
                    "> 该 Proposal 已形成历史 rollback / recovery-observed Outcome；"
                    "治理状态仍保留 approved 审计，"
                    "但不能再次签发 Experiment Contract，也不能标记 promoted 或进入 "
                    "policy learning。"
                ),
                "",
                "`r` 刷新 · `Esc` 返回",
            ]
        )
    elif (
        _normalized(proposal.get("state")) == "approved"
        and proposal.get("contract_issue_allowed") is not True
    ):
        lines.extend(
            [
                "",
                "> Contract authority 暂不可用；已安全阻止签发，请刷新后重试。",
                "",
                "`r` 刷新 · `Esc` 返回",
            ]
        )
    elif _normalized(proposal.get("state")) == "approved":
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


class ApprovalDecisionScreen(ModalScreen[dict[str, str] | None]):
    """Collect one explicit Approval decision without retaining draft input."""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]
    DEFAULT_CSS = """
    ApprovalDecisionScreen {
        align: center middle;
    }
    ApprovalDecisionScreen > Container {
        width: 76;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: thick $warning 80%;
        background: $surface;
    }
    ApprovalDecisionScreen Label,
    ApprovalDecisionScreen Input {
        width: 1fr;
        margin: 0 0 1 0;
    }
    ApprovalDecisionScreen .approval-decision-error {
        color: $error;
        margin: 0 0 1 0;
    }
    ApprovalDecisionScreen Horizontal {
        width: auto;
        height: auto;
    }
    ApprovalDecisionScreen Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(
        self,
        *,
        action: str,
        title: str,
        confirmation_required: bool,
    ) -> None:
        super().__init__()
        if action not in {"approve", "reject"}:
            raise ValueError("Approval action 仅支持 approve 或 reject")
        self.approval_action = action
        self.approval_title = title
        self.confirmation_required = confirmation_required

    def compose(self) -> ComposeResult:
        label = "批准" if self.approval_action == "approve" else "拒绝"
        with Container():
            heading = (
                f"确认{label} Approval？"
                if self.confirmation_required
                else f"{label} Approval"
            )
            yield Label(f"[bold]{heading}[/bold]")
            yield Label(_plain(self.approval_title) or "未命名 Approval")
            if self.approval_action == "reject":
                yield Input(
                    placeholder="填写拒绝原因（必填，最多 2000 字符）",
                    max_length=2_000,
                    id="approval-decision-note",
                )
            error = Static("", classes="approval-decision-error", id="approval-decision-error")
            error.display = False
            yield error
            with Horizontal():
                button_label = (
                    f"确认{label}" if self.confirmation_required else f"提交{label}"
                )
                yield Button(button_label, variant="warning", id="approval-confirm")
                yield Button("取消", variant="primary", id="approval-cancel")

    def on_mount(self) -> None:
        note = self.query("#approval-decision-note").first(Input)
        (note or self.query_one("#approval-confirm", Button)).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#approval-decision-note")
    def on_note_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approval-cancel":
            self.dismiss(None)
        elif event.button.id == "approval-confirm":
            self._submit()

    def _submit(self) -> None:
        note_input = self.query("#approval-decision-note").first(Input)
        note = note_input.value.strip() if note_input is not None else ""
        if self.approval_action == "reject" and not note:
            error = self.query_one("#approval-decision-error", Static)
            error.update("拒绝原因不能为空。")
            error.display = True
            if note_input is not None:
                note_input.focus()
            return
        self.dismiss({"action": self.approval_action, "decision_note": note})


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
        Binding("4", "release_tab", "Release", show=False),
        Binding("5", "timeline_tab", "Timeline", show=False),
        Binding("up", "select_previous", "上一项", show=False),
        Binding("down", "select_next", "下一项", show=False),
        Binding("a", "approve_proposal", "批准审查项", show=False),
        Binding("x", "reject_proposal", "拒绝审查项", show=False),
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
        self.selected_event_index = 0
        self.selected_event_id = ""
        self.review_detail: Mapping[str, Any] | None = None
        self.review_loading = False
        self.review_error = ""
        self.review_notice = ""
        self.proposal_action_pending = False
        self._timeline_timer: Any | None = None
        self._timeline_refresh_error = False

    def compose(self) -> ComposeResult:
        yield Static("", id="workbench-title")
        yield Markdown("正在加载 Workbench 权威快照…", id="workbench-content")
        yield Static("", id="workbench-error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_snapshot()
        self._timeline_timer = self.set_interval(0.5, self.refresh_timeline)

    def on_unmount(self) -> None:
        if self._timeline_timer is not None:
            self._timeline_timer.stop()
            self._timeline_timer = None

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
        previous_event_id = self.selected_event_id
        self.snapshot = snapshot
        self._timeline_refresh_error = False
        self.selected_worktree_index = min(
            self.selected_worktree_index,
            max(0, len(_records(snapshot.get("worktrees"))) - 1),
        )
        self.selected_review_index = min(
            self.selected_review_index,
            max(0, len(_review_records(snapshot)) - 1),
        )
        events = _records(snapshot.get("events"))
        preserved_index = next(
            (
                index
                for index, event in enumerate(events)
                if _normalized(event.get("id")) == previous_event_id
            ),
            min(self.selected_event_index, max(0, len(events) - 1)),
        )
        self._set_selected_event_index(preserved_index)
        self._render_snapshot()
        if self.selected_tab == "reviews":
            self.refresh_review_detail()

    @work(exclusive=True, group="workbench-timeline", exit_on_error=False)
    async def refresh_timeline(self) -> None:
        """Apply contiguous persisted events; fall back to a full snapshot on gaps."""
        if self.snapshot is None:
            return
        service = getattr(self.engine, "workbench_service", None)
        replay_builder = getattr(service, "timeline_replay_window", None)
        if not callable(replay_builder):
            return
        session_id = _normalized(self.snapshot.get("session_id"))
        stream_id = _normalized(self.snapshot.get("timeline_stream_id"))
        cursor = _timeline_cursor(self.snapshot.get("timeline_cursor", 0), "cursor")
        try:
            recovery = await replay_builder(
                session_id,
                after_cursor=cursor,
                expected_stream_id=stream_id,
                limit=100,
            )
            if not isinstance(recovery, Mapping):
                raise WorkbenchSnapshotError("Timeline replay 必须是对象")
            if _integer(recovery.get("schema_version")) != 1:
                raise WorkbenchSnapshotError("Timeline replay schema 无效")
            if _normalized(recovery.get("session_id")) != session_id:
                raise WorkbenchSnapshotError("Timeline replay 会话不匹配")
            authority_stream = _strict_timeline_text(
                recovery.get("stream_id") or "",
                128,
                allow_empty=True,
            )
            if recovery.get("gap") is True:
                self.query_one("#workbench-error", Static).update(
                    "Timeline 游标出现缺口，正在恢复权威完整快照。"
                )
                self.refresh_snapshot()
                return
            raw_events = recovery.get("events")
            events = _validate_timeline_events(raw_events, session_id=session_id)
            current_cursor = cursor
            current_stream = stream_id
            current = _records(self.snapshot.get("events"))
            changed = False
            for event in events:
                event_cursor = _timeline_cursor(event.get("cursor"), "event cursor")
                if authority_stream == current_stream and event_cursor <= current_cursor:
                    continue
                first = not current_stream and current_cursor == 0 and event_cursor == 1
                continuous = (
                    authority_stream == current_stream
                    and event_cursor == current_cursor + 1
                )
                if not first and not continuous:
                    self.query_one("#workbench-error", Static).update(
                        "Timeline 增量不连续，正在恢复权威完整快照。"
                    )
                    self.refresh_snapshot()
                    return
                event_id = _normalized(event.get("id"))
                current = [
                    event,
                    *(item for item in current if _normalized(item.get("id")) != event_id),
                ][:100]
                current_stream = authority_stream
                current_cursor = event_cursor
                changed = True
            if changed:
                snapshot = dict(self.snapshot)
                snapshot["events"] = current
                snapshot["timeline_stream_id"] = current_stream
                snapshot["timeline_cursor"] = current_cursor
                self.snapshot = snapshot
                previous_id = self.selected_event_id
                self.selected_event_index = next(
                    (
                        index
                        for index, event in enumerate(current)
                        if _normalized(event.get("id")) == previous_id
                    ),
                    min(self.selected_event_index, max(0, len(current) - 1)),
                )
                self._set_selected_event_index(self.selected_event_index)
                self._render_snapshot()
            self._timeline_refresh_error = False
        except Exception as exc:
            logger.warning("TUI Workbench Timeline refresh failed (%s)", type(exc).__name__)
            if not self._timeline_refresh_error:
                self._timeline_refresh_error = True
                self.query_one("#workbench-error", Static).update(
                    "Timeline 增量刷新失败，已保留上一次权威快照。"
                )

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_next_tab(self) -> None:
        tabs = ("overview", "worktrees", "reviews", "timeline", "release")
        self.selected_tab = tabs[(tabs.index(self.selected_tab) + 1) % len(tabs)]
        self._render_snapshot()
        if self.selected_tab == "reviews":
            self.refresh_review_detail()

    def action_previous_tab(self) -> None:
        tabs = ("overview", "worktrees", "reviews", "timeline", "release")
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

    def action_release_tab(self) -> None:
        self.selected_tab = "release"
        self._render_snapshot()

    def action_timeline_tab(self) -> None:
        self.selected_tab = "timeline"
        self._set_selected_event_index(self.selected_event_index)
        self._render_snapshot()

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
        elif self.selected_tab == "timeline":
            self._set_selected_event_index(self.selected_event_index - 1)
            self._render_snapshot()

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
        elif self.selected_tab == "timeline" and self.snapshot is not None:
            self._set_selected_event_index(self.selected_event_index + 1)
            self._render_snapshot()

    def _set_selected_event_index(self, index: int) -> None:
        events = _records((self.snapshot or {}).get("events"))
        if not events:
            self.selected_event_index = 0
            self.selected_event_id = ""
            return
        self.selected_event_index = min(len(events) - 1, max(0, int(index)))
        self.selected_event_id = _normalized(
            events[self.selected_event_index].get("id")
        )

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
        selected = self._selected_review()
        if selected is not None and selected.get("review_kind") == "approval":
            self._begin_approval_action("approve")
            return
        self._begin_proposal_action(ProposalAction.APPROVE)

    def action_reject_proposal(self) -> None:
        selected = self._selected_review()
        if selected is not None and selected.get("review_kind") == "approval":
            self._begin_approval_action("reject")
            return
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
        if selected.get("contract_issue_allowed") is not True:
            self.review_error = (
                "该 Proposal 已有实施 Outcome 或 Outcome source 不可用，不能再次签发 Contract。"
            )
            self._render_snapshot()
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

    def _begin_approval_action(self, action: str) -> None:
        if self.proposal_action_pending:
            return
        selected = self._selected_review()
        if not (
            selected is not None
            and selected.get("review_kind") == "approval"
            and _normalized(selected.get("state")) == ApprovalState.WAITING.value
        ):
            return
        approval_id = _normalized(selected.get("id"))
        decision = self.engine._permission_checker.check(
            "workbench_resolve_approval",
            {"approval_id": approval_id, "action": action},
        )
        if not decision.allowed:
            self.review_error = "当前权限模式不允许决策 Approval。"
            self._render_snapshot()
            return
        if action == "approve" and not decision.requires_confirmation:
            self._start_approval_action(
                approval_id,
                action,
                decision_note="",
                confirmed=False,
            )
            return

        def on_decision(result: dict[str, str] | None) -> None:
            if result is None:
                return
            self._start_approval_action(
                approval_id,
                result["action"],
                decision_note=result.get("decision_note", ""),
                confirmed=decision.requires_confirmation,
            )

        self.app.push_screen(
            ApprovalDecisionScreen(
                action=action,
                title=_plain(selected.get("title") or selected.get("id")),
                confirmation_required=decision.requires_confirmation,
            ),
            on_decision,
        )

    def _start_approval_action(
        self,
        approval_id: str,
        action: str,
        *,
        decision_note: str,
        confirmed: bool,
    ) -> None:
        if self.proposal_action_pending:
            return
        self.proposal_action_pending = True
        self.submit_approval_action(
            approval_id,
            action,
            decision_note=decision_note,
            confirmed=confirmed,
        )

    @work(exclusive=True, group="workbench-approval-action", exit_on_error=False)
    async def submit_approval_action(
        self,
        approval_id: str,
        action: str,
        *,
        decision_note: str,
        confirmed: bool,
    ) -> None:
        try:
            decision = self.engine._permission_checker.check(
                "workbench_resolve_approval",
                {"approval_id": approval_id, "action": action},
            )
            if not decision.allowed:
                raise WorkbenchSnapshotError(
                    "当前权限模式不允许决策 Approval。"
                )
            if decision.requires_confirmation and not confirmed:
                raise WorkbenchSnapshotError("该 Approval 决策需要明确确认。")
            if self.snapshot is None:
                raise WorkbenchSnapshotError("Workbench 权威快照不可用。")
            session_id = _normalized(self.snapshot.get("session_id"))
            if action == "approve":
                state = ApprovalState.APPROVED
            elif action == "reject":
                state = ApprovalState.REJECTED
            else:
                raise WorkbenchSnapshotError("Approval action 格式无效。")
            self.review_error = ""
            self.review_notice = "正在提交 Approval 决策…"
            self._render_snapshot()
            approval = await self.engine.workbench_service.resolve_approval(
                session_id=session_id,
                approval_id=approval_id,
                actor="Human",
                state=state,
                decision_note=decision_note,
            )
            if approval is None:
                raise WorkbenchSnapshotError(
                    "Approval 不存在或不属于当前会话。"
                )
            snapshot = _validate_snapshot(
                await self.engine.workbench_service.dashboard_snapshot(session_id),
                session_id=session_id,
            )
        except ApprovalResolutionConflictError as exc:
            self.review_error = _plain(exc)
            self.review_notice = ""
            await self._refresh_after_approval_conflict()
            return
        except Exception as exc:
            logger.warning(
                "TUI Workbench Approval action failed (%s)",
                type(exc).__name__,
            )
            self.review_error = (
                _plain(exc)
                if isinstance(exc, ValueError)
                else "Approval 决策暂时失败。"
            )
            self.review_notice = ""
            self._render_snapshot()
            return
        finally:
            self.proposal_action_pending = False
        self.snapshot = snapshot
        self.review_notice = (
            "Approval 已批准。" if action == "approve" else "Approval 已拒绝。"
        )
        self.selected_review_index = min(
            self.selected_review_index,
            max(0, len(_review_records(snapshot)) - 1),
        )
        self.review_detail = None
        self._render_snapshot()
        self.refresh_review_detail()

    async def _refresh_after_approval_conflict(self) -> None:
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
        self.review_detail = None
        self._render_snapshot()

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
        tabs = (
            ("overview", "1 概览"),
            ("worktrees", "2 Worktrees"),
            ("reviews", "3 Reviews"),
            ("release", "4 Release"),
            ("timeline", "5 Timeline"),
        )
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
        elif self.selected_tab == "release":
            content.update(format_workbench_release_markdown(self.snapshot))
        elif self.selected_tab == "timeline":
            content.update(
                format_workbench_timeline_markdown(
                    self.snapshot,
                    selected_index=self.selected_event_index,
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
    normalized = dict(value)
    timeline_stream_id = _strict_timeline_text(
        value.get("timeline_stream_id") or "",
        128,
        allow_empty=True,
    )
    timeline_cursor = _timeline_cursor(value.get("timeline_cursor", 0), "cursor")
    timeline_earliest_cursor = _timeline_cursor(
        value.get("timeline_earliest_cursor", 0),
        "earliest cursor",
    )
    if timeline_cursor > 0 and not timeline_stream_id:
        raise WorkbenchSnapshotError("Workbench Timeline cursor 缺少 stream identity")
    normalized["timeline_stream_id"] = timeline_stream_id
    normalized["timeline_cursor"] = timeline_cursor
    normalized["timeline_earliest_cursor"] = timeline_earliest_cursor
    error = _normalized(value.get("stable_population_finalization_error"))
    if error not in {"", "stable_population_finalization_unavailable"}:
        raise WorkbenchSnapshotError("Stable Population finalization error contract 无效")
    raw_projection = value.get("stable_population_finalization")
    if raw_projection is not None:
        if error:
            raise WorkbenchSnapshotError("Stable Population finalization availability 投影冲突")
        try:
            projection = WorkbenchStablePopulationFinalizationProjection.model_validate(
                raw_projection
            )
        except (TypeError, ValueError) as exc:
            raise WorkbenchSnapshotError("Stable Population finalization projection 无效") from exc
        normalized["stable_population_finalization"] = projection.model_dump(mode="json")
    normalized["stable_population_finalization_error"] = error
    normalized["events"] = _validate_timeline_events(
        value.get("events"),
        session_id=_normalized(value.get("session_id")),
    )
    return normalized


def _validate_timeline_events(
    value: Any,
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise WorkbenchSnapshotError("Workbench Timeline 必须是不超过 100 项的数组")
    events: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise WorkbenchSnapshotError("Workbench Timeline event 必须是对象")
        severity = _strict_timeline_text(raw.get("severity", "info"), 16)
        if severity not in {"info", "warning", "error", "critical"}:
            raise WorkbenchSnapshotError("Workbench Timeline severity 无效")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping) or len(payload) > 20:
            raise WorkbenchSnapshotError("Workbench Timeline payload 无效")
        safe_payload: dict[str, str] = {}
        for key, item in payload.items():
            safe_key = _strict_timeline_text(key, 80)
            if item is None:
                rendered = "null"
            elif isinstance(item, float) and not math.isfinite(item):
                raise WorkbenchSnapshotError("Workbench Timeline payload 数值无效")
            elif isinstance(item, (str, int, float, bool)):
                rendered = _normalized(item, limit=500)
            elif isinstance(item, (list, tuple)):
                rendered = f"[{min(len(item), 10_000)} 项]"
            else:
                rendered = "[结构化对象]"
            safe_payload[safe_key] = rendered
        event = {
                "id": _strict_timeline_text(raw.get("id"), 128),
                "session_id": _strict_timeline_text(raw.get("session_id"), 500),
                "type": _strict_timeline_text(raw.get("type"), 160),
                "actor": _strict_timeline_text(raw.get("actor"), 500),
                "subject_id": _strict_timeline_text(raw.get("subject_id"), 500),
                "timestamp": _strict_timeline_text(raw.get("timestamp"), 100),
                "correlation_id": _strict_timeline_text(
                    raw.get("correlation_id") or "", 128, allow_empty=True
                ),
                "parent_event_id": _strict_timeline_text(
                    raw.get("parent_event_id") or "", 128, allow_empty=True
                ),
                "severity": severity,
                "cursor": _timeline_cursor(raw.get("cursor", 0), "event cursor"),
                "payload": safe_payload,
            }
        if event["session_id"] != session_id:
            raise WorkbenchSnapshotError("Workbench Timeline event 会话不匹配")
        events.append(event)
    return events


def _timeline_cursor(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkbenchSnapshotError(f"Workbench Timeline {name} 必须是非负整数")
    if value > 9_007_199_254_740_991:
        raise WorkbenchSnapshotError(f"Workbench Timeline {name} 超出安全范围")
    return value


def _strict_timeline_text(
    value: Any,
    limit: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise WorkbenchSnapshotError("Workbench Timeline 文本字段类型无效")
    if (
        len(value) > limit
        or value != value.strip()
        or (not allow_empty and not value)
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise WorkbenchSnapshotError("Workbench Timeline 文本字段无效")
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
    "format_workbench_release_markdown",
    "format_workbench_timeline_markdown",
    "format_workbench_reviews_markdown",
    "format_workbench_worktrees_markdown",
]
