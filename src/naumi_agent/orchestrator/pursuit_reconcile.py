"""Typed, side-effect-free reconciliation decisions for in-flight Pursuit actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.orchestrator.pursuit_action_ledger import (
    PursuitActionRecord,
    PursuitActionState,
    action_safe_text,
)


class BackgroundTaskLookup(Protocol):
    def get(self, task_id: str) -> BackgroundTask | None: ...

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> BackgroundTask | None: ...

    def is_managed_active(self, task_id: str) -> bool: ...


class ReconcileDisposition(StrEnum):
    SAFE_CONTINUE = "safe_continue"
    WAITING = "waiting"
    BLOCKED = "blocked"


class ReconcileReason(StrEnum):
    ALL_ACCOUNTED = "all_accounted"
    BACKGROUND_ACTIVE = "background_active"
    LEGACY_UNKNOWN = "legacy_unknown"
    NON_BACKGROUND_AMBIGUOUS = "non_background_ambiguous"
    BACKGROUND_TASK_MISSING = "background_task_missing"
    BACKGROUND_STORE_ERROR = "background_store_error"
    BACKGROUND_IDENTITY_MISMATCH = "background_identity_mismatch"
    STALE_PREPARING = "stale_preparing"
    STALE_RUNNING = "stale_running"
    ORPHAN_RUNNING = "orphan_running"


@dataclass(frozen=True, slots=True)
class ReconcileTerminalUpdate:
    action_key: str
    succeeded: bool
    result_status: str
    result_summary: str


@dataclass(frozen=True, slots=True)
class ReconcileWait:
    action_key: str
    action_id: str
    task_id: str
    command: str
    created_at: float


@dataclass(frozen=True, slots=True)
class PursuitReconcileDecision:
    disposition: ReconcileDisposition
    reason: ReconcileReason
    summary: str
    abandon_action_keys: tuple[str, ...] = ()
    terminal_updates: tuple[ReconcileTerminalUpdate, ...] = ()
    waits: tuple[ReconcileWait, ...] = ()


def decide_background_reconcile(
    *,
    actions: list[PursuitActionRecord],
    iteration: int,
    background_tasks: BackgroundTaskLookup | None,
    now: float,
    pid_probe: Callable[[int], bool],
    preparing_stale_seconds: float = 30.0,
) -> PursuitReconcileDecision:
    """Classify one action-inflight checkpoint without mutating durable state."""
    current = sorted(
        (action for action in actions if action.iteration == iteration),
        key=lambda action: (action.prepared_at, action.action_id, action.action_key),
    )
    if not current:
        return _blocked(
            ReconcileReason.LEGACY_UNKNOWN,
            "当前轮次没有行动账本，无法证明计划行动是否已经派发。",
        )

    abandoned: list[str] = []
    terminal: list[ReconcileTerminalUpdate] = []
    waits: list[ReconcileWait] = []
    blocker: tuple[ReconcileReason, str] | None = None

    for action in current:
        if action.state is PursuitActionState.PREPARED:
            abandoned.append(action.action_key)
            continue
        if action.is_terminal:
            continue
        if action.tool_name != "background_run":
            blocker = blocker or (
                ReconcileReason.NON_BACKGROUND_AMBIGUOUS,
                f"行动 {action.action_key} 已派发到 {action.tool_name}，"
                "但该执行器没有可核对的幂等任务合同。",
            )
            continue
        if background_tasks is None:
            blocker = blocker or (
                ReconcileReason.BACKGROUND_TASK_MISSING,
                "后台任务存储未接入，无法核对已派发行动。",
            )
            continue

        try:
            task = _find_task(action, background_tasks)
        except Exception as exc:
            blocker = blocker or (
                ReconcileReason.BACKGROUND_STORE_ERROR,
                "后台任务存储读取失败，拒绝猜测恢复（"
                f"{type(exc).__name__}）。",
            )
            continue
        if task is None:
            blocker = blocker or (
                ReconcileReason.BACKGROUND_TASK_MISSING,
                f"行动 {action.action_key} 没有对应的后台任务回执。",
            )
            continue
        if task.idempotency_key != action.dispatch_token:
            blocker = blocker or (
                ReconcileReason.BACKGROUND_IDENTITY_MISMATCH,
                f"后台任务 {task.id} 的幂等身份与行动账本不一致。",
            )
            continue

        if task.status is BackgroundStatus.PREPARING:
            age = max(0.0, now - _timestamp(task.started_at))
            if age > preparing_stale_seconds:
                blocker = blocker or (
                    ReconcileReason.STALE_PREPARING,
                    f"后台任务 {task.id} 已准备 {age:.0f} 秒但没有 PID，"
                    "无法证明进程是否启动。",
                )
            else:
                waits.append(_wait(action, task))
            continue

        if task.status is BackgroundStatus.RUNNING:
            try:
                managed_active = bool(background_tasks.is_managed_active(task.id))
            except Exception as exc:
                blocker = blocker or (
                    ReconcileReason.BACKGROUND_STORE_ERROR,
                    "后台任务运行所有权读取失败，拒绝猜测恢复（"
                    f"{type(exc).__name__}）。",
                )
                continue
            if managed_active:
                waits.append(_wait(action, task))
            elif task.pid is not None and bool(pid_probe(task.pid)):
                blocker = blocker or (
                    ReconcileReason.ORPHAN_RUNNING,
                    f"后台任务 {task.id} 的 PID 仍存在，但当前 Runner 未持有进程和 watcher。",
                )
            else:
                blocker = blocker or (
                    ReconcileReason.STALE_RUNNING,
                    f"后台任务 {task.id} 记录为运行中，但 PID 不存在。",
                )
            continue

        succeeded = task.status is BackgroundStatus.COMPLETED
        terminal.append(ReconcileTerminalUpdate(
            action_key=action.action_key,
            succeeded=succeeded,
            result_status=task.status.value,
            result_summary=action_safe_text(
                f"task_id={task.id}; status={task.status.value}; "
                f"exit_code={task.exit_code}; error={task.error}",
                limit=2_000,
            ),
        ))

    if blocker is not None:
        return PursuitReconcileDecision(
            disposition=ReconcileDisposition.BLOCKED,
            reason=blocker[0],
            summary=blocker[1],
            abandon_action_keys=tuple(abandoned),
            terminal_updates=tuple(terminal),
            waits=tuple(waits),
        )
    if waits:
        return PursuitReconcileDecision(
            disposition=ReconcileDisposition.WAITING,
            reason=ReconcileReason.BACKGROUND_ACTIVE,
            summary=f"已核对 {len(waits)} 个仍在运行或准备中的后台任务。",
            abandon_action_keys=tuple(abandoned),
            terminal_updates=tuple(terminal),
            waits=tuple(waits),
        )
    return PursuitReconcileDecision(
        disposition=ReconcileDisposition.SAFE_CONTINUE,
        reason=ReconcileReason.ALL_ACCOUNTED,
        summary="当前轮次所有已记录行动均已确定终态或确认尚未派发。",
        abandon_action_keys=tuple(abandoned),
        terminal_updates=tuple(terminal),
    )


def _find_task(
    action: PursuitActionRecord,
    background_tasks: BackgroundTaskLookup,
) -> BackgroundTask | None:
    if action.background_task_id:
        task = background_tasks.get(action.background_task_id)
        if task is not None:
            return task
    return background_tasks.get_by_idempotency_key(action.dispatch_token)


def _wait(action: PursuitActionRecord, task: BackgroundTask) -> ReconcileWait:
    return ReconcileWait(
        action_key=action.action_key,
        action_id=action.action_id,
        task_id=task.id,
        command=task.command,
        created_at=_timestamp(task.started_at),
    )


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _blocked(reason: ReconcileReason, summary: str) -> PursuitReconcileDecision:
    return PursuitReconcileDecision(
        disposition=ReconcileDisposition.BLOCKED,
        reason=reason,
        summary=summary,
    )
