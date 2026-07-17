"""Shared resume/history screen models and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
        "/history archive <ID> 归档。"
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


def _display_workspace(workspace: str) -> str:
    if not workspace:
        return "未知工作区"
    try:
        return Path(workspace).name or workspace
    except Exception:
        return workspace
