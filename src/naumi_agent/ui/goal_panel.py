"""Typed, read-only Goal/Pursuit snapshot shared by terminal frontends."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.orchestrator.goal_store import Goal, GoalStatus, GoalStore
from naumi_agent.orchestrator.pursuit import PursuitRun
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.orchestrator.pursuit_terminal_outbox_worker import (
    PursuitTerminalOutboxWorkerSnapshot,
)
from naumi_agent.ui.pursuit_recovery import (
    PursuitRecoveryAuthority,
    build_pursuit_recovery_snapshot,
)

GOAL_PANEL_SCHEMA_VERSION = 2
MAX_GOAL_PANEL_ITEMS = 50
MAX_GOAL_PANEL_EVIDENCE = 20
MAX_GOAL_PANEL_WAITS = 20
MAX_GOAL_PANEL_INTERACTIONS = 50
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TerminalOutboxCounts(BaseModel):
    """Bounded public counts; dispatch identities never cross the UI boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_pending: int = Field(ge=0, le=10_000)
    due: int = Field(ge=0, le=10_000)
    backoff: int = Field(ge=0, le=10_000)
    live_claimed: int = Field(ge=0, le=10_000)
    expired_claimed: int = Field(ge=0, le=10_000)
    dead_letter: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _sum_matches_total(self) -> TerminalOutboxCounts:
        classified = (
            self.due
            + self.backoff
            + self.live_claimed
            + self.expired_claimed
            + self.dead_letter
        )
        if classified != self.total_pending:
            raise ValueError("terminal outbox 分类计数与总数不一致。")
        return self


class TerminalOutboxProjection(BaseModel):
    """Typed Goal-page projection of the automatic terminal recovery service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    enabled: bool
    status: Literal[
        "idle", "recovering", "backoff", "degraded", "disabled", "unavailable"
    ]
    worker_state: Literal[
        "running", "waiting", "stopping", "stopped", "disabled", "unavailable"
    ]
    assessed_at: str = Field(min_length=1, max_length=64)
    counts: TerminalOutboxCounts
    pass_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    retry_scheduled_count: int = Field(ge=0)
    dead_lettered_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    next_delay_seconds: float = Field(ge=0, le=604_800)
    failure_codes: tuple[str, ...] = Field(max_length=8)
    warning: str = Field(max_length=500)

    @model_validator(mode="after")
    def _state_is_coherent(self) -> TerminalOutboxProjection:
        _parse_aware(self.assessed_at)
        if any(not _FAILURE_CODE_RE.fullmatch(code) for code in self.failure_codes):
            raise ValueError("terminal outbox failure code 无效。")
        if not self.enabled and (
            self.status != "disabled" or self.worker_state != "disabled"
        ):
            raise ValueError("terminal outbox 关闭状态不一致。")
        if self.enabled and self.worker_state == "disabled":
            raise ValueError("terminal outbox 启用状态不得标记为 disabled。")
        if self.status == "unavailable" and not self.warning:
            raise ValueError("terminal outbox unavailable 必须说明原因。")
        if self.status == "degraded" and not self.failure_codes:
            raise ValueError("terminal outbox degraded 必须包含 failure code。")
        return self


@dataclass(frozen=True, slots=True)
class GoalPursuitSnapshot:
    """Bounded public projection of durable Goal and Pursuit facts."""

    current_goal_id: str = ""
    goals: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    include_finished: bool = True
    interactions: tuple[dict[str, Any], ...] = ()
    interaction_filter: str = "all"
    interaction_cursor: str = ""
    interaction_next_cursor: str = ""
    selected_interaction: dict[str, Any] | None = None
    terminal_outbox: TerminalOutboxProjection | None = None

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
            "interactions": [
                dict(item) for item in self.interactions[:MAX_GOAL_PANEL_INTERACTIONS]
            ],
            "interaction_filter": self.interaction_filter,
            "interaction_cursor": self.interaction_cursor,
            "interaction_next_cursor": self.interaction_next_cursor,
            "interaction_has_more": bool(self.interaction_next_cursor),
            "selected_interaction": (
                dict(self.selected_interaction)
                if self.selected_interaction is not None
                else None
            ),
            "terminal_outbox": (
                self.terminal_outbox.model_dump(mode="json")
                if self.terminal_outbox is not None
                else None
            ),
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
    interaction_limit: int = 10,
    interaction_filter: str = "all",
    interaction_cursor: str = "",
    selected_interaction_id: str = "",
    terminal_outbox_enabled: bool | None = None,
    terminal_outbox_worker_snapshot: (
        Callable[[], PursuitTerminalOutboxWorkerSnapshot] | None
    ) = None,
    assessed_at: str | None = None,
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
    interactions: tuple[dict[str, Any], ...] = ()
    next_cursor = ""
    selected_interaction: dict[str, Any] | None = None
    normalized_filter = str(interaction_filter or "all").strip().lower()
    if normalized_filter not in {
        "all", "pending", "answered", "expired", "cancelled",
    }:
        normalized_filter = "all"
        warnings.append("交互状态筛选无效，已回退为全部。")
    visible_run_ids = {
        str(item.get("pursuit_run_id") or "")
        for item in items
        if item.get("pursuit_run_id")
    }
    list_page = getattr(authority, "list_interactions_page", None)
    list_interactions = getattr(authority, "list_interactions", None)
    if callable(list_page) and visible_run_ids:
        try:
            page = await list_page(
                workspace_root=workspace_root,
                subject_kind="pursuit",
                subject_ids=tuple(sorted(visible_run_ids)),
                state_filter=normalized_filter,
                limit=max(1, min(int(interaction_limit), 50)),
                cursor=interaction_cursor,
            )
            interactions = tuple(
                _interaction_projection(record)
                for record in page.items
                if record.subject_kind == "pursuit"
                and record.subject_id in visible_run_ids
            )
            next_cursor = page.next_cursor
        except Exception:
            warnings.append("Goal 用户交互分页读取失败，请刷新页面。")
    elif callable(list_interactions) and visible_run_ids:
        visible_run_ids = {
            str(item.get("pursuit_run_id") or "")
            for item in items
            if item.get("pursuit_run_id")
        }
        try:
            records = await list_interactions(
                workspace_root=workspace_root,
                subject_kind="pursuit",
                subject_ids=tuple(sorted(visible_run_ids)),
                limit=MAX_GOAL_PANEL_INTERACTIONS,
            )
            interactions = tuple(
                _interaction_projection(record)
                for record in records
                if record.subject_kind == "pursuit"
                and record.subject_id in visible_run_ids
            )
        except Exception:
            warnings.append("Goal 用户交互历史读取失败，请运行 `/doctor`。")
    if selected_interaction_id:
        get_interaction = getattr(authority, "get_interaction", None)
        if callable(get_interaction):
            try:
                record = await get_interaction(
                    workspace_root=workspace_root,
                    interaction_id=selected_interaction_id,
                )
                if (
                    record is None
                    or record.subject_kind != "pursuit"
                    or record.subject_id not in visible_run_ids
                ):
                    warnings.append("所选用户交互不属于当前 Goal 页面。")
                else:
                    selected_interaction = _interaction_detail_projection(record)
            except Exception:
                warnings.append("Goal 用户交互详情读取失败，请刷新页面。")
    return GoalPursuitSnapshot(
        current_goal_id=base.current_goal_id,
        goals=tuple(items),
        warnings=tuple(dict.fromkeys(warnings))[:20],
        truncated=base.truncated,
        include_finished=base.include_finished,
        interactions=interactions,
        interaction_filter=normalized_filter,
        interaction_cursor=_bounded_text(interaction_cursor, 1_024),
        interaction_next_cursor=_bounded_text(next_cursor, 1_024),
        selected_interaction=selected_interaction,
        terminal_outbox=_build_terminal_outbox_projection(
            pursuit_store,
            enabled=terminal_outbox_enabled,
            worker_snapshot=terminal_outbox_worker_snapshot,
            assessed_at=assessed_at,
        ),
    )


def _build_terminal_outbox_projection(
    pursuit_store: PursuitStore,
    *,
    enabled: bool | None,
    worker_snapshot: Callable[[], PursuitTerminalOutboxWorkerSnapshot] | None,
    assessed_at: str | None,
) -> TerminalOutboxProjection | None:
    if enabled is None:
        return None
    now = _parse_aware(assessed_at or datetime.now(UTC).isoformat())
    counts = TerminalOutboxCounts(
        total_pending=0,
        due=0,
        backoff=0,
        live_claimed=0,
        expired_claimed=0,
        dead_letter=0,
    )
    warnings: list[str] = []
    if pursuit_store.db_path.is_file():
        try:
            backlog = pursuit_store.terminal_outbox_backlog(
                now=now.timestamp(),
                scan_limit=10_000,
            )
            counts = TerminalOutboxCounts(
                total_pending=backlog.total_pending,
                due=backlog.due,
                backoff=backlog.backoff,
                live_claimed=backlog.live_claimed,
                expired_claimed=backlog.expired_claimed,
                dead_letter=backlog.dead_letter,
            )
        except Exception:
            warnings.append(
                "终态恢复队列读取失败，请运行 `/doctor` 检查 Pursuit Store。"
            )

    snapshot: PursuitTerminalOutboxWorkerSnapshot | None = None
    if enabled and worker_snapshot is not None:
        try:
            candidate = worker_snapshot()
            if not isinstance(candidate, PursuitTerminalOutboxWorkerSnapshot):
                raise TypeError("terminal outbox worker snapshot 类型无效")
            snapshot = candidate
        except Exception:
            warnings.append("终态恢复 Worker 状态读取失败，请运行 `/doctor`。")
    elif enabled:
        warnings.append("终态恢复 Worker 状态 authority 未接入。")

    authority_unavailable = bool(warnings)

    raw_failure_codes = list(
        snapshot.last_failure_codes if snapshot is not None else ()
    )
    if (
        enabled
        and snapshot is not None
        and snapshot.state.value == "stopped"
        and "worker_stopped" not in raw_failure_codes
    ):
        raw_failure_codes.append("worker_stopped")
    if counts.dead_letter:
        raw_failure_codes.insert(0, "dead_letter_present")
        warnings.append(
            f"存在 {counts.dead_letter} 条终态恢复死信，自动重试已停止，请人工审查。"
        )
    warning = _bounded_text("；".join(warnings), 500)
    failure_codes = tuple(dict.fromkeys(
        _bounded_text(item, 64) for item in raw_failure_codes
    ))[:8]
    if not enabled:
        status = "disabled"
    elif authority_unavailable:
        status = "unavailable"
    elif failure_codes:
        status = "degraded"
    elif counts.due or counts.live_claimed or counts.expired_claimed:
        status = "recovering"
    elif counts.backoff:
        status = "backoff"
    else:
        status = "idle"
    return TerminalOutboxProjection(
        enabled=enabled,
        status=status,
        worker_state=(
            snapshot.state.value
            if snapshot is not None
            else "disabled" if not enabled else "unavailable"
        ),
        assessed_at=now.isoformat(),
        counts=counts,
        pass_count=snapshot.pass_count if snapshot is not None else 0,
        delivered_count=snapshot.delivered_count if snapshot is not None else 0,
        retry_scheduled_count=(
            snapshot.retry_scheduled_count if snapshot is not None else 0
        ),
        dead_lettered_count=(
            snapshot.dead_lettered_count if snapshot is not None else 0
        ),
        failure_count=snapshot.failure_count if snapshot is not None else 0,
        next_delay_seconds=(
            snapshot.next_delay_seconds if snapshot is not None else 0
        ),
        failure_codes=failure_codes,
        warning=warning,
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
                boundary = pursuit.get("boundary_decision")
                if isinstance(boundary, dict):
                    lines.extend([
                        f"- 最近裁判：`{boundary['code']}` · "
                        f"{boundary['status']} · `{boundary['decision_id'][:12]}`",
                        f"- 裁判原因：{boundary['reason']}",
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
    if snapshot.terminal_outbox is not None:
        lines.extend(["", *_render_terminal_outbox(snapshot.terminal_outbox)])
    if (
        snapshot.interactions
        or snapshot.interaction_filter != "all"
        or bool(snapshot.interaction_cursor)
    ):
        lines.extend([
            "",
            f"#### 用户交互 · {_interaction_filter_label(snapshot.interaction_filter)}",
        ])
        if not snapshot.interactions:
            lines.append("- 当前筛选没有交互记录。")
        for item in snapshot.interactions:
            lines.append(
                f"- `{item['interaction_id']}` · {_interaction_status_label(item['state'])} · "
                f"{item['header']}：{item['question']}"
            )
            lines.append(
                f"  - 详情：`/goal interaction detail {item['interaction_id']}`"
            )
            if item["can_cancel"]:
                lines.append(
                    f"  - 取消：`/goal interaction cancel {item['interaction_id']}`"
                )
            if item["can_takeover"]:
                lines.append(
                    f"  - 接管：`/goal interaction takeover {item['interaction_id']}`"
                )
        if snapshot.interaction_next_cursor:
            lines.append(
                "- 下一页：`/goal interaction list "
                f"{snapshot.interaction_filter} {snapshot.interaction_next_cursor}`"
            )
    if snapshot.selected_interaction:
        lines.extend([
            "",
            _render_interaction_detail_projection(snapshot.selected_interaction),
        ])
    return "\n".join(lines).rstrip()


def _render_terminal_outbox(value: TerminalOutboxProjection) -> list[str]:
    counts = value.counts
    status = {
        "idle": "空闲",
        "recovering": "正在恢复",
        "backoff": "等待重试",
        "degraded": "部分失败",
        "disabled": "已关闭",
        "unavailable": "状态不可用",
    }[value.status]
    worker = {
        "running": "运行中",
        "waiting": "等待中",
        "stopping": "正在停止",
        "stopped": "已停止",
        "disabled": "已关闭",
        "unavailable": "不可用",
    }[value.worker_state]
    lines = [
        "#### 终态自动恢复",
        f"- 状态：{status} · Worker {worker}",
        f"- 队列：{counts.total_pending} · 到期 {counts.due} · "
        f"退避 {counts.backoff} · 认领 {counts.live_claimed} · "
        f"过期认领 {counts.expired_claimed} · 死信 {counts.dead_letter}",
        f"- 累计：轮次 {value.pass_count} · 已收口 {value.delivered_count} · "
        f"已退避 {value.retry_scheduled_count} · 死信 {value.dead_lettered_count} · "
        f"失败 {value.failure_count}",
    ]
    if value.enabled and value.worker_state in {"running", "waiting"}:
        lines.append(f"- 下次检查：约 {value.next_delay_seconds:.1f}s")
    if value.failure_codes:
        lines.append(f"- 最近失败：{', '.join(value.failure_codes)}")
    if value.warning:
        lines.append(f"- ⚠️ {value.warning}")
    return lines


def render_goal_interaction_detail(
    record: HarnessInteractionRecord,
    *,
    assessed_at: str | None = None,
) -> str:
    """Render one durable interaction without exposing its private owner identity."""
    now = _parse_aware(assessed_at or datetime.now(UTC).isoformat())
    deadline = _parse_aware(record.expires_at) if record.expires_at else None
    owner_lease = _parse_aware(record.owner_lease_expires_at)
    question_expired = deadline is not None and now >= deadline
    lease_expired = now >= owner_lease

    if record.state != "pending":
        authority = "终态记录 · 不适用接管"
    elif question_expired:
        authority = "问题期限已到 · 等待 authority 收口为超时"
    elif lease_expired:
        authority = "Owner 租约已过期 · 可由活动界面接管"
    else:
        authority = "Owner 租约生效 · 暂不可接管"

    lines = [
        "### Goal 用户交互详情",
        "",
        f"- 交互 ID：`{_bounded_text(record.interaction_id, 132)}`",
        f"- Pursuit：`{_bounded_text(record.subject_id, 128)}`",
        f"- 状态：{_interaction_status_label(record.state)}",
        f"- 问题：{_bounded_text(record.header, 40)} · "
        f"{_bounded_text(record.question, 2_000)}",
        "- 选项：",
    ]
    for index, option in enumerate(record.options, start=1):
        description = _bounded_text(option.description, 300)
        suffix = f" — {description}" if description else ""
        lines.append(
            f"  {index}. {_bounded_text(option.label, 80)} "
            f"(`{_bounded_text(option.value, 80)}`){suffix}"
        )
    if record.allow_custom:
        lines.append(f"  - 自定义：{_bounded_text(record.custom_label, 80)}")

    if record.state == "answered":
        if record.answer_kind == "custom":
            answer = _bounded_text(record.custom_text, 4_000)
            lines.append(f"- 回答：自定义 · {answer}")
        else:
            lines.append(
                f"- 回答：{_bounded_text(record.answer_label, 80)} "
                f"(`{_bounded_text(record.answer_value, 80)}`)"
            )
    elif record.state == "pending":
        lines.append("- 回答：尚未提交")
    elif record.state == "expired":
        lines.append("- 回答：未回答，已超时")
    else:
        lines.append("- 回答：未回答，已取消")

    lines.extend([
        f"- Authority：{authority}",
        f"- Fencing：sequence {record.sequence} · owner epoch {record.owner_epoch}",
        f"- 时间：创建 {record.created_at} · 更新 {record.updated_at}",
        f"- 问题截止：{record.expires_at or '无'}",
        f"- Owner 租约截止：{record.owner_lease_expires_at}",
    ])
    if record.answered_at:
        lines.append(f"- 回答时间：{record.answered_at}")
    if record.state == "pending":
        lines.append(
            f"- 取消：`/goal interaction cancel {record.interaction_id}`"
        )
    if record.state == "pending" and lease_expired and not question_expired:
        lines.append(
            f"- 接管：`/goal interaction takeover {record.interaction_id}`"
        )
    return "\n".join(lines)


def _parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("交互时间必须包含时区。")
    return parsed.astimezone(UTC)


def _interaction_projection(record: HarnessInteractionRecord) -> dict[str, Any]:
    now = datetime.now(UTC)
    question_live = (
        not record.expires_at or _parse_aware(record.expires_at) > now
    )
    can_takeover = (
        record.state == "pending"
        and question_live
        and _parse_aware(record.owner_lease_expires_at) <= now
    )
    return {
        "interaction_id": _bounded_text(record.interaction_id, 132),
        "pursuit_run_id": _bounded_text(record.subject_id, 128),
        "state": record.state,
        "sequence": record.sequence,
        "priority": record.priority,
        "header": _bounded_text(record.header, 40),
        "question": _bounded_text(record.question, 2_000),
        "created_at": _bounded_text(record.created_at, 64),
        "expires_at": _bounded_text(record.expires_at, 64),
        "updated_at": _bounded_text(record.updated_at, 64),
        "can_cancel": record.state == "pending",
        "can_takeover": can_takeover,
    }


def _interaction_detail_projection(
    record: HarnessInteractionRecord,
    *,
    assessed_at: str | None = None,
) -> dict[str, Any]:
    now = _parse_aware(assessed_at or datetime.now(UTC).isoformat())
    deadline = _parse_aware(record.expires_at) if record.expires_at else None
    owner_lease = _parse_aware(record.owner_lease_expires_at)
    question_expired = deadline is not None and now >= deadline
    lease_expired = now >= owner_lease
    return {
        **_interaction_projection(record),
        "options": [
            {
                "value": _bounded_text(option.value, 80),
                "label": _bounded_text(option.label, 80),
                "description": _bounded_text(option.description, 300),
            }
            for option in record.options
        ],
        "allow_custom": bool(record.allow_custom),
        "custom_label": _bounded_text(record.custom_label, 80),
        "answer_kind": _bounded_text(record.answer_kind, 16),
        "answer_value": _bounded_text(record.answer_value, 80),
        "answer_label": _bounded_text(record.answer_label, 80),
        "custom_text": _bounded_text(record.custom_text, 4_000),
        "answered_at": _bounded_text(record.answered_at, 64),
        "owner_epoch": max(1, int(record.owner_epoch)),
        "question_expired": question_expired,
        "lease_expired": lease_expired,
    }


def _render_interaction_detail_projection(item: dict[str, Any]) -> str:
    lines = [
        "#### 所选用户交互详情",
        f"- 交互 ID：`{item['interaction_id']}`",
        f"- 状态：{_interaction_status_label(str(item['state']))}",
        f"- 优先级：{item['priority']}",
        f"- 问题：{item['header']} · {item['question']}",
        "- 选项：",
    ]
    for index, option in enumerate(item.get("options") or (), start=1):
        suffix = f" — {option['description']}" if option.get("description") else ""
        lines.append(
            f"  {index}. {option['label']} (`{option['value']}`){suffix}"
        )
    if item.get("allow_custom"):
        lines.append(f"  - 自定义：{item.get('custom_label') or '自定义回答'}")
    if item["state"] == "answered":
        if item.get("answer_kind") == "custom":
            lines.append(f"- 回答：自定义 · {item.get('custom_text') or '-'}")
        else:
            lines.append(
                f"- 回答：{item.get('answer_label') or '-'} "
                f"(`{item.get('answer_value') or '-'}`)"
            )
    else:
        lines.append("- 回答：尚未提交" if item["state"] == "pending" else "- 回答：无")
    lines.append(
        f"- Fencing：sequence {item['sequence']} · owner epoch {item['owner_epoch']}"
    )
    return "\n".join(lines)


def _interaction_filter_label(value: str) -> str:
    return {
        "all": "全部",
        "pending": "等待回答",
        "answered": "已回答",
        "expired": "已超时",
        "cancelled": "已取消",
    }.get(value, value)


def _interaction_status_label(state: str) -> str:
    return {
        "pending": "等待回答",
        "answered": "已回答",
        "expired": "已超时",
        "cancelled": "已取消",
    }.get(state, state)


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
    boundary = run.boundary_decision
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
        "boundary_decision": (
            {
                "schema_version": boundary.schema_version,
                "decision_id": boundary.decision_id,
                "facts_sha256": boundary.facts_sha256,
                "status": boundary.status,
                "code": boundary.code,
                "reason": _bounded_text(boundary.reason, 300),
                "next_action": _bounded_text(boundary.next_action, 300),
                "terminal": boundary.terminal,
                "resumable": boundary.resumable,
            }
            if boundary is not None
            else None
        ),
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
    action = recovery.get("resume_action")
    if isinstance(action, dict):
        action_state = str(action.get("state") or "unavailable")
        lines.append(
            f"- 恢复动作：{_recovery_action_label(action_state)}"
            f" · `{_bounded_text(action.get('code'), 64)}`"
            f" · {_bounded_text(action.get('reason'), 300)}"
        )
        lines.append(
            f"- 恢复命令：`{_bounded_text(action.get('command'), 300)}`"
        )
    attempts = recovery.get("attempts")
    if isinstance(attempts, list | tuple) and attempts:
        lines.append(f"- 恢复请求：最近 {min(len(attempts), 5)} 项")
        for attempt in attempts[:5]:
            if not isinstance(attempt, dict):
                continue
            result = (
                f" · `{_bounded_text(attempt.get('result_code'), 64)}`"
                if attempt.get("result_code")
                else ""
            )
            lines.append(
                "  - "
                f"`{_bounded_text(attempt.get('attempt_id'), 73)}`"
                f" · {_recovery_attempt_label(str(attempt.get('state') or ''))}"
                f"{result}"
                f" · {_bounded_text(attempt.get('updated_at'), 64)}"
            )
            if attempt.get("state") == "admitted":
                lines.append(
                    "    - 对账命令："
                    f"`/pursue reconcile "
                    f"{_bounded_text(attempt.get('attempt_id'), 73)}`"
                )
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


def _recovery_action_label(value: str) -> str:
    return {
        "available": "可恢复",
        "busy": "处理中",
        "blocked": "已阻止",
        "unavailable": "不适用",
    }.get(value, value)


def _recovery_attempt_label(value: str) -> str:
    return {
        "requested": "已记录",
        "admitted": "已准入",
        "resolved": "已完成",
        "failed": "失败关闭",
    }.get(value, value)


__all__ = [
    "GOAL_PANEL_SCHEMA_VERSION",
    "GoalPursuitSnapshot",
    "TerminalOutboxCounts",
    "TerminalOutboxProjection",
    "build_goal_pursuit_snapshot",
    "build_goal_pursuit_snapshot_with_recovery",
    "render_goal_interaction_detail",
    "render_goal_pursuit_snapshot",
]
