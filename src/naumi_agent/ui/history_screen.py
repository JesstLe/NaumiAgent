"""Shared resume/history screen models and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from naumi_agent.harness.retention_executor import (
    RetentionPassStatus,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_periodic import (
    RetentionWorkerSnapshot,
    RetentionWorkerState,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPreview,
    SessionRetentionReason,
)
from naumi_agent.memory.lifecycle import SessionDeletePreview


@dataclass(frozen=True)
class HistoryItem:
    id: str
    title: str
    model: str
    updated_at: datetime
    message_count: int
    user_message_count: int
    total_tokens: int
    total_cost_usd: float
    workspace_root: str
    git_branch: str
    summary: str
    is_current: bool = False


@dataclass(frozen=True)
class HistorySnapshot:
    items: tuple[HistoryItem, ...]
    total: int
    query: str = ""


def build_history_item(
    session: Any,
    *,
    current_session_id: str | None = None,
    fallback_workspace: str = "",
    fallback_git_branch: str = "",
) -> HistoryItem:
    messages = list(getattr(session, "messages", []) or [])
    workspace = str(getattr(session, "workspace_root", "") or fallback_workspace)
    git_branch = str(getattr(session, "git_branch", "") or fallback_git_branch)
    return HistoryItem(
        id=str(getattr(session, "id", "")),
        title=str(getattr(session, "title", "") or "新会话"),
        model=str(getattr(session, "model", "") or "未知模型"),
        updated_at=getattr(session, "updated_at", datetime.now()),
        message_count=len(messages),
        user_message_count=sum(1 for m in messages if m.get("role") == "user"),
        total_tokens=int(getattr(session, "total_tokens", 0) or 0),
        total_cost_usd=float(getattr(session, "total_cost_usd", 0.0) or 0.0),
        workspace_root=workspace,
        git_branch=git_branch,
        summary=str(getattr(session, "summary", "") or summarize_session_messages(messages)),
        is_current=bool(current_session_id and getattr(session, "id", "") == current_session_id),
    )


def build_history_snapshot(
    sessions: list[Any],
    *,
    total: int,
    query: str = "",
    current_session_id: str | None = None,
    fallback_workspace: str = "",
    fallback_git_branch: str = "",
) -> HistorySnapshot:
    return HistorySnapshot(
        items=tuple(
            build_history_item(
                session,
                current_session_id=current_session_id,
                fallback_workspace=fallback_workspace,
                fallback_git_branch=fallback_git_branch,
            )
            for session in sessions
        ),
        total=total,
        query=query,
    )


def summarize_session_messages(messages: list[dict[str, Any]], max_chars: int = 160) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("role") not in {"user", "assistant"}:
            continue
        content = str(message.get("content", "") or "").strip().replace("\n", " ")
        if content:
            parts.append(content)
        if len(" / ".join(parts)) >= max_chars:
            break
    summary = " / ".join(parts) or "暂无摘要"
    if len(summary) > max_chars:
        return summary[: max_chars - 1].rstrip() + "…"
    return summary


def render_history_screen(snapshot: HistorySnapshot, *, max_summary: int = 96) -> str:
    if not snapshot.items:
        if snapshot.query:
            return f"没有匹配 `{snapshot.query}` 的历史会话。\n"
        return "暂无历史会话。\n"

    title = f"历史会话（共 {snapshot.total} 个）"
    if snapshot.query:
        title += f" · 搜索: {snapshot.query}"
    lines = [title, ""]
    for idx, item in enumerate(snapshot.items, 1):
        current = " *当前" if item.is_current else ""
        workspace = _display_workspace(item.workspace_root)
        git = item.git_branch or "未知分支"
        summary = item.summary
        if len(summary) > max_summary:
            summary = summary[: max_summary - 1].rstrip() + "…"
        lines.extend(
            [
                f"{idx}. {item.title}{current}",
                f"   id: {item.id} · {item.updated_at.strftime('%Y-%m-%d %H:%M')}",
                (
                    f"   model: {item.model} · messages: {item.user_message_count}/"
                    f"{item.message_count} · tokens: {item.total_tokens} · "
                    f"cost: ${item.total_cost_usd:.4f}"
                ),
                f"   workspace: {workspace} · git: {git}",
                f"   摘要: {summary}",
            ]
        )
    lines.append("")
    lines.append(
        "操作：/load <编号或ID> 恢复；/history <关键词> 搜索；"
        "/history preview <ID> 预览；/history delete-preview <ID> 删除影响；"
        "/history retention-preview 保留策略；/history retention-run 执行一轮；"
        "/history retention-worker status 周期状态；/history archive <ID> 归档。"
    )
    return "\n".join(lines) + "\n"


def render_history_preview(session: Any) -> str:
    item = build_history_item(session)
    lines = [
        f"## {item.title}",
        "",
        f"- ID：`{item.id}`",
        f"- 时间：{item.updated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型：{item.model}",
        f"- 消息：{item.user_message_count} 条用户消息 / {item.message_count} 条总消息",
        f"- Token：{item.total_tokens}",
        f"- 费用：${item.total_cost_usd:.4f}",
        f"- Workspace：`{item.workspace_root or '未知'}`",
        f"- Git：`{item.git_branch or '未知'}`",
        "",
        "### 摘要",
        item.summary or "暂无摘要",
        "",
        "### 最近消息",
    ]
    for message in list(getattr(session, "messages", []) or [])[-8:]:
        role = str(message.get("role", "unknown"))
        content = str(message.get("content", "") or "").strip().replace("\n", " ")
        if not content:
            content = "[无文本内容]"
        if len(content) > 180:
            content = content[:179].rstrip() + "…"
        lines.append(f"- **{role}**：{content}")
    return "\n".join(lines) + "\n"


def render_session_delete_preview(preview: SessionDeletePreview) -> str:
    """Render a read-only, workspace-scoped session deletion impact preview."""
    active = "（当前会话）" if preview.is_active else ""
    lines = [
        "## Session 删除影响预览",
        "",
        f"- 会话：{preview.title} {active}".rstrip(),
        f"- ID：`{preview.session_id}`",
        f"- Workspace：`{preview.workspace_root}`",
        f"- Session 消息：{preview.message_count}",
        f"- Harness Run：{preview.harness_run_count}",
        f"- Contract Criterion：{preview.criterion_count}",
        f"- Check：{preview.check_count}",
        f"- Evidence：{preview.evidence_count}",
        f"- Replay Baseline：{preview.replay_baseline_count}",
        f"- Artifact 引用：{preview.artifact_reference_count}",
        "",
        (
            "Artifact 统计是 Check 路径与 `artifact://` URI 的引用数，"
            "不是可安全删除文件数。"
        ),
        (
            "删除执行会重新校验共享引用与路径，只清理 `artifacts/` 或"
            "`.naumi/artifacts/` 中无共享的普通文件；其他文件会保留。"
        ),
        f"现有 Session 删除命令：`/delete {preview.session_id}`。",
    ]
    return "\n".join(lines) + "\n"


def render_session_retention_preview(preview: SessionRetentionPreview) -> str:
    """Render the bounded dry-run without implying automatic deletion is enabled."""
    policy = preview.policy
    limit_text = (
        _format_bytes(policy.max_archived_session_bytes)
        if policy.max_archived_session_bytes
        else "未启用"
    )
    lines = [
        "## Session 保留策略预览",
        "",
        "本结果仅为只读预览，不会删除任何内容，也不会启动后台清理。",
        "",
        f"- 已归档会话：{preview.total_archived_count}",
        f"- 本轮扫描：{preview.scanned_count} / 上限 {policy.scan_limit}",
        f"- 满足条件：{preview.eligible_count}",
        f"- 本轮拟处理：{len(preview.selected)}",
        f"- 因预算延后：{preview.deferred_eligible_count}",
        f"- 归档保留期：{policy.delete_archived_after_days} 天",
        f"- 会话持久化载荷：{_format_bytes(preview.total_archived_bytes)}",
        f"- 会话载荷空间上限：{limit_text}",
        f"- 本轮拟释放载荷：{_format_bytes(preview.selected_bytes)}",
        "",
    ]
    if preview.scan_truncated:
        lines.append("⚠ 候选超过扫描上限；本预览只覆盖最久未访问的一批。")
    if preview.budget_exhausted:
        lines.append("⚠ 单轮数量或字节预算已用尽，其余候选会延后。")
    if preview.selected:
        lines.extend(["### 本轮候选", ""])
        reason_labels = {
            SessionRetentionReason.AGE_EXPIRED: "超过保留期",
            SessionRetentionReason.STORAGE_PRESSURE: "空间压力",
            SessionRetentionReason.AGE_AND_STORAGE: "过期 + 空间压力",
        }
        for item in preview.selected:
            lines.append(
                f"- {item.title} (`{item.session_id}`) · "
                f"{reason_labels[item.reason]} · "
                f"最近访问 {item.effective_last_accessed_at:%Y-%m-%d %H:%M} · "
                f"{_format_bytes(item.payload_bytes)}"
            )
    else:
        lines.append("当前没有会在本轮进入清理的归档会话。")
    lines.extend(
        [
            "",
            (
                "这里的字节数仅指 Session 表中的会话持久化载荷，"
                "不包含 Harness 数据库和 Artifact 文件。"
            ),
            "恢复会话会更新最近访问时间，并使其退出归档清理候选。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_session_retention_result(result: SessionRetentionPassResult) -> str:
    """Render one explicit pass without conflating retries with completion."""
    lines = [
        "## Session 保留清理回执",
        "",
        f"- 状态：{_retention_status_label(result.status)}",
        f"- 计划 / 尝试：{result.planned_count} / {result.attempted_count}",
        f"- 完整删除：{result.completed_count}",
        f"- 安全重试：{result.retry_scheduled_count}",
        f"- 重试耗尽：{result.retry_exhausted_count}",
        f"- 策略阻止：{result.policy_blocked_count}",
        f"- 已不存在：{result.not_found_count}",
        f"- 未预期错误：{result.error_count}",
        f"- 剩余候选：{result.remaining_count}",
        f"- 计划会话载荷：{_format_bytes(result.planned_bytes)}",
        f"- 耗时：{result.duration_seconds:.2f}s",
        "",
        result.message,
    ]
    if result.retry_scheduled_count:
        lines.append("未完成协调已写入持久重试队列，不能视为完整删除。")
    if result.policy_blocked_count:
        lines.append("策略阻止通常表示会话已恢复或不再处于 archived 状态。")
    return "\n".join(lines) + "\n"


def render_session_retention_worker(
    snapshot: RetentionWorkerSnapshot,
    *,
    configured_enabled: bool,
) -> str:
    """Render periodic worker authority and counters without raw exceptions."""
    state_labels = {
        RetentionWorkerState.STOPPED: "已停止",
        RetentionWorkerState.STARTING: "启动中",
        RetentionWorkerState.STANDBY: "待命（其他实例持有租约）",
        RetentionWorkerState.RUNNING: "正在执行",
        RetentionWorkerState.WAITING: "等待下一轮",
        RetentionWorkerState.STOPPING: "停止中",
    }
    pass_status = "尚未执行"
    if snapshot.last_pass_status:
        try:
            pass_status = _retention_status_label(
                RetentionPassStatus(snapshot.last_pass_status)
            )
        except ValueError:
            pass_status = "未知状态（已失败关闭）"
    error_labels = {
        "": "无",
        "lease_acquire_failed": "租约获取失败",
        "lease_renew_failed": "租约续期失败",
        "lease_release_failed": "租约释放失败",
        "pass_failed": "单轮执行失败",
    }
    error_label = error_labels.get(snapshot.last_error_code, "未知错误（已脱敏）")
    lines = [
        "## Session Retention Worker",
        "",
        f"- 配置启用：{'是' if configured_enabled else '否'}",
        f"- 状态：{state_labels[snapshot.state]}",
        f"- 持有租约：{'是' if snapshot.lease_held else '否'}",
        f"- Owner：`{snapshot.owner_id}`",
        f"- 执行轮数：{snapshot.pass_count}",
        f"- 完整删除：{snapshot.completed_session_count}",
        f"- 安全重试：{snapshot.retry_scheduled_count}",
        f"- Worker 失败：{snapshot.failure_count}",
        f"- 连续空轮：{snapshot.consecutive_empty_passes}",
        f"- 下次等待：{snapshot.next_delay_seconds:.1f}s",
        f"- 最近轮状态：{pass_status}",
        f"- 最近错误：{error_label}",
        f"- 启动时间：{snapshot.started_at or '尚未启动'}",
        f"- 最近执行：{snapshot.last_pass_at or '尚未执行'}",
    ]
    if not configured_enabled:
        lines.extend(
            [
                "",
                "周期 worker 默认关闭；需在 memory.session_retention 中明确启用。",
            ]
        )
    return "\n".join(lines) + "\n"


def _retention_status_label(status: RetentionPassStatus) -> str:
    return {
        RetentionPassStatus.COMPLETED: "已完成",
        RetentionPassStatus.PARTIAL: "部分完成",
        RetentionPassStatus.DEADLINE_REACHED: "达到时间预算",
        RetentionPassStatus.CANCELLED: "已取消",
        RetentionPassStatus.FAILED: "失败关闭",
    }[status]


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _display_workspace(workspace: str) -> str:
    if not workspace:
        return "未知工作区"
    try:
        return Path(workspace).name or workspace
    except Exception:
        return workspace
