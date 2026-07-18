"""Typed, read-only Goal/Pursuit snapshot shared by terminal frontends."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.orchestrator.goal_store import Goal, GoalStatus, GoalStore
from naumi_agent.orchestrator.pursuit import PursuitRun
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.ui.pursuit_recovery import (
    PursuitRecoveryAuthority,
    build_pursuit_recovery_snapshot,
)

GOAL_PANEL_SCHEMA_VERSION = 1
MAX_GOAL_PANEL_ITEMS = 50
MAX_GOAL_PANEL_EVIDENCE = 20
MAX_GOAL_PANEL_WAITS = 20
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class GoalPursuitSnapshot:
    """Bounded public projection of durable Goal and Pursuit facts."""

    current_goal_id: str = ""
    goals: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    include_finished: bool = True

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GOAL_PANEL_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "full": True,
            "current_goal_id": _bounded_text(self.current_goal_id, 128),
            "goals": [dict(item) for item in self.goals[:MAX_GOAL_PANEL_ITEMS]],
            "warnings": [_bounded_text(item, 500) for item in self.warnings[:20]],
            "truncated": bool(self.truncated),
            "include_finished": bool(self.include_finished),
        }


def build_goal_pursuit_snapshot(
    goal_store: GoalStore,
    pursuit_store: PursuitStore,
    *,
    limit: int = 20,
    include_finished: bool = True,
) -> GoalPursuitSnapshot:
    """Read one bounded snapshot without creating a missing database."""
    safe_limit = max(1, min(int(limit), MAX_GOAL_PANEL_ITEMS))
    if not goal_store.db_path.is_file():
        return GoalPursuitSnapshot(include_finished=include_finished)

    warnings: list[str] = []
    try:
        goals = goal_store.list(
            include_finished=include_finished,
            limit=safe_limit + 1,
        )
        current = goal_store.current()
    except Exception:
        return GoalPursuitSnapshot(
            warnings=("Goal 状态读取失败，请运行 `/doctor` 检查本地状态库。",),
            include_finished=include_finished,
        )

    truncated = len(goals) > safe_limit
    goals = goals[:safe_limit]
    if current is not None and all(goal.id != current.id for goal in goals):
        goals = [current, *goals[: max(0, safe_limit - 1)]]
    pursuit_available = pursuit_store.db_path.is_file()
    items: list[dict[str, Any]] = []
    for goal in goals:
        run: PursuitRun | None = None
        link_status = "not_linked"
        if goal.pursuit_run_id:
            link_status = "missing"
            if pursuit_available:
                try:
                    run = pursuit_store.get_run(goal.pursuit_run_id)
                except Exception:
                    warnings.append(
                        f"目标 {goal.id} 的 Pursuit 记录读取失败，请稍后刷新。"
                    )
                if run is not None:
                    link_status = "ready"
            if run is None and len(warnings) < 20:
                warnings.append(
                    f"目标 {goal.id} 关联的追踪记录 {goal.pursuit_run_id} 不可用。"
                )
        items.append(_goal_projection(goal, run=run, link_status=link_status))

    return GoalPursuitSnapshot(
        current_goal_id=current.id if current is not None else "",
        goals=tuple(items),
        warnings=tuple(dict.fromkeys(warnings)),
        truncated=truncated,
        include_finished=include_finished,
    )


async def build_goal_pursuit_snapshot_with_recovery(
    goal_store: GoalStore,
    pursuit_store: PursuitStore,
    authority: PursuitRecoveryAuthority | None,
    *,
    workspace_root: str | Path,
    limit: int = 20,
    include_finished: bool = True,
) -> GoalPursuitSnapshot:
    """Add typed recovery facts while preserving the bounded base projection."""
    base = build_goal_pursuit_snapshot(
        goal_store,
        pursuit_store,
        limit=limit,
        include_finished=include_finished,
    )
    items: list[dict[str, Any]] = []
    warnings = list(base.warnings)
    for item in base.goals:
        projected = dict(item)
        pursuit = projected.get("pursuit")
        if isinstance(pursuit, dict):
            try:
                run = pursuit_store.get_run(str(pursuit.get("run_id") or ""))
                if run is None:
                    raise ValueError("Pursuit run disappeared during snapshot")
                recovery = await build_pursuit_recovery_snapshot(
                    run,
                    pursuit_store,
                    authority,
                    workspace_root=workspace_root,
                )
                pursuit = {**pursuit, "recovery": recovery.model_dump(mode="json")}
            except Exception:
                warnings.append(
                    f"目标 {projected['goal_id']} 的恢复健康读取失败，请运行 `/doctor`。"
                )
            projected["pursuit"] = pursuit
        items.append(projected)
    return GoalPursuitSnapshot(
        current_goal_id=base.current_goal_id,
        goals=tuple(items),
        warnings=tuple(dict.fromkeys(warnings))[:20],
        truncated=base.truncated,
        include_finished=base.include_finished,
    )


def render_goal_pursuit_snapshot(snapshot: GoalPursuitSnapshot) -> str:
    """Render the typed snapshot as safe Markdown for CLI and TUI fallback."""
    if not snapshot.goals:
        empty = (
            "当前没有持久目标记录。使用 `/goal <目标>` 创建。"
            if snapshot.include_finished
            else "当前没有未完成目标。使用 `/goal <目标>` 创建。"
        )
        lines = [
            "### 持久目标",
            "",
            empty,
        ]
    else:
        lines = ["### Goal / Pursuit", ""]
        for item in snapshot.goals:
            current = " · 当前" if item["goal_id"] == snapshot.current_goal_id else ""
            lines.extend([
                f"#### `{item['goal_id']}` · {_goal_status_label(item['status'])}{current}",
                f"- 目标：{item['objective']}",
                f"- 会话：`{item['session_id'] or '未绑定'}`",
            ])
            if item["note"]:
                lines.append(f"- 说明：{item['note']}")
            pursuit = item["pursuit"]
            if pursuit is not None:
                lines.extend([
                    f"- Pursuit：`{pursuit['run_id']}` · "
                    f"{_pursuit_status_label(pursuit['status'])} · {pursuit['phase']}",
                    f"- 成功标准：{pursuit['criteria_verified']}/"
                    f"{pursuit['criteria_total']} · 轮次 {pursuit['iteration']}",
                    f"- 下一步：{pursuit['next_action'] or '暂无'}",
                    f"- 等待任务：{len(pursuit['waits'])} · 证据：{len(pursuit['evidence'])}",
                ])
                recovery = pursuit.get("recovery")
                if isinstance(recovery, dict):
                    lines.extend(_render_recovery(recovery))
            elif item["pursuit_link_status"] == "missing":
                lines.append(
                    f"- Pursuit：`{item['pursuit_run_id']}` · ⚠️ 追踪记录不可用"
                )
            else:
                lines.append("- Pursuit：未启动")
            lines.append("")
    if snapshot.truncated:
        lines.append("> 目标记录较多，当前视图已按上限截断。")
    if snapshot.warnings:
        lines.extend(["", "#### 警告", *[f"- {item}" for item in snapshot.warnings]])
    return "\n".join(lines).rstrip()


def _goal_projection(
    goal: Goal,
    *,
    run: PursuitRun | None,
    link_status: str,
) -> dict[str, Any]:
    return {
        "goal_id": _bounded_text(goal.id, 128),
        "objective": _bounded_text(goal.objective, 4_000),
        "status": goal.status.value,
        "note": _bounded_text(goal.note, 2_000),
        "session_id": _bounded_text(goal.session_id, 128),
        "pursuit_run_id": _bounded_text(goal.pursuit_run_id, 128),
        "pursuit_link_status": link_status,
        "created_at": _timestamp(goal.created_at),
        "updated_at": _timestamp(goal.updated_at),
        "pursuit": _pursuit_projection(run) if run is not None else None,
    }


def _pursuit_projection(run: PursuitRun) -> dict[str, Any]:
    return {
        "run_id": _bounded_text(run.id, 128),
        "goal": _bounded_text(run.goal, 4_000),
        "status": run.status.value,
        "phase": _bounded_text(run.phase, 128),
        "started_at": _timestamp(run.started_at),
        "updated_at": _timestamp(run.updated_at),
        "iteration": max(0, int(run.iteration)),
        "criteria_total": max(0, int(run.criteria_total)),
        "criteria_verified": max(0, int(run.criteria_verified)),
        "failure_count": max(0, int(run.failure_count)),
        "blocked_reason": _bounded_text(run.blocked_reason, 2_000),
        "next_action": _bounded_text(run.next_action, 2_000),
        "worktree_name": _bounded_text(run.worktree_name, 256),
        "worktree_path": _bounded_text(run.worktree_path, 2_048),
        "waits": [
            {
                "task_id": _bounded_text(item.task_id, 128),
                "action_id": _bounded_text(item.action_id, 128),
                "command": _bounded_text(item.command, 2_000),
                "created_at": _timestamp(item.created_at),
            }
            for item in (run.waiting_on or [])[:MAX_GOAL_PANEL_WAITS]
        ],
        "evidence": [
            {
                "kind": _bounded_text(item.kind, 128),
                "source": _bounded_text(item.source, 512),
                "summary": _bounded_text(item.summary, 1_000),
                "is_hard": bool(item.is_hard),
                "timestamp": _timestamp(item.timestamp),
            }
            for item in (run.evidence or [])[-MAX_GOAL_PANEL_EVIDENCE:]
        ],
    }


def _timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def _bounded_text(value: object, limit: int) -> str:
    return _CONTROL_CHARS_RE.sub("", str(value or "")).strip()[:limit]


def _goal_status_label(status: str) -> str:
    return {
        GoalStatus.ACTIVE.value: "🟢 进行中",
        GoalStatus.PAUSED.value: "🟡 已暂停",
        GoalStatus.BLOCKED.value: "🔴 已阻塞",
        GoalStatus.COMPLETED.value: "✅ 已完成",
        GoalStatus.CANCELLED.value: "⚪ 已取消",
    }.get(status, status)


def _pursuit_status_label(status: str) -> str:
    return {
        "running": "🟢 运行中",
        "waiting": "🟡 等待中",
        "blocked": "🔴 已阻塞",
        "completed": "✅ 已完成",
        "failed": "❌ 失败",
        "cancelled": "⚪ 已取消",
        "budget_exceeded": "🟠 预算耗尽",
    }.get(status, status)


def _render_recovery(recovery: dict[str, Any]) -> list[str]:
    heartbeat = recovery.get("heartbeat") or {}
    lease = recovery.get("lease") or {}
    checkpoint = recovery.get("checkpoint") or {}
    state = _bounded_text(recovery.get("recovery_state"), 64) or "unknown"
    lines = [
        f"- 恢复健康：{_recovery_label(state)}"
        f" · 心跳 {_heartbeat_label(str(heartbeat.get('health', 'missing')))}"
        f" · 租约 {_lease_label(str(lease.get('status', 'missing')))}",
        f"- Worker：`{heartbeat.get('instance_id') or '未知'}`"
        f" · seq {heartbeat.get('sequence', 0)}"
        f" · age {heartbeat.get('age_seconds', 0)}s",
        f"- Lease：`{lease.get('owner_id') or '无 owner'}`"
        f" · epoch {lease.get('epoch', 0)}"
        f" · {'已过期' if lease.get('expired') else '未过期'}",
        f"- Checkpoint：{_checkpoint_label(str(checkpoint.get('status', 'missing')))}"
        f" · seq {checkpoint.get('sequence', 0)}"
        f" · {checkpoint.get('phase') or '-'}",
    ]
    reason = _bounded_text(recovery.get("reconcile_reason"), 128)
    if recovery.get("reconcile_required"):
        lines.append(f"- Reconcile：需要核对 · {reason or 'reason unavailable'}")
    alerts = recovery.get("alerts")
    if isinstance(alerts, list | tuple):
        lines.extend(f"- 恢复提醒：{_bounded_text(item, 500)}" for item in alerts[:3])
    return lines


def _recovery_label(value: str) -> str:
    return {
        "active": "运行健康",
        "waiting": "安全等待",
        "blocked": "已阻塞",
        "reconcile_required": "需要核对",
        "orphaned": "疑似孤立",
        "inconsistent": "状态不一致",
        "terminal": "已终止",
        "unknown": "未知",
    }.get(value, value)


def _heartbeat_label(value: str) -> str:
    return {
        "starting": "启动中", "healthy": "健康", "draining": "排空中",
        "stale": "陈旧", "offline": "离线", "stopped": "已停止",
        "failed": "失败", "clock_regression": "时钟倒退", "missing": "缺失",
        "error": "读取失败",
    }.get(value, value)


def _lease_label(value: str) -> str:
    return {
        "active": "生效", "released": "已释放", "missing": "缺失",
        "error": "读取失败",
    }.get(value, value)


def _checkpoint_label(value: str) -> str:
    return {"ready": "可用", "missing": "缺失", "error": "校验失败"}.get(
        value, value,
    )


__all__ = [
    "GOAL_PANEL_SCHEMA_VERSION",
    "GoalPursuitSnapshot",
    "build_goal_pursuit_snapshot",
    "build_goal_pursuit_snapshot_with_recovery",
    "render_goal_pursuit_snapshot",
]
