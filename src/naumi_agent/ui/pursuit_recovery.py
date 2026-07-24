"""Read-only recovery health projection for one durable Pursuit run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatHealth,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLease
from naumi_agent.orchestrator.pursuit import PursuitRun
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    PursuitRecoveryAttempt,
)
from naumi_agent.orchestrator.pursuit_store import PursuitStore

RecoveryState = Literal[
    "active",
    "waiting",
    "blocked",
    "reconcile_required",
    "orphaned",
    "inconsistent",
    "terminal",
    "unknown",
]
HeartbeatHealth = Literal[
    "starting",
    "healthy",
    "draining",
    "stale",
    "offline",
    "stopped",
    "failed",
    "clock_regression",
    "missing",
    "error",
]
RecoveryActionState = Literal["available", "busy", "blocked", "unavailable"]
RecoveryAttemptState = Literal["requested", "admitted", "resolved", "failed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryHeartbeat(_StrictModel):
    health: HeartbeatHealth
    phase: str = Field(max_length=32)
    instance_id: str = Field(max_length=128)
    epoch: int = Field(ge=0)
    sequence: int = Field(ge=0)
    observed_at: str = Field(max_length=64)
    timeout_seconds: int = Field(ge=0, le=86_400)
    age_seconds: int = Field(ge=0)
    detail_code: str = Field(max_length=128)


class RecoveryLease(_StrictModel):
    status: Literal["active", "released", "missing", "error"]
    owner_id: str = Field(max_length=128)
    epoch: int = Field(ge=0)
    expires_at: str = Field(max_length=64)
    updated_at: str = Field(max_length=64)
    expired: bool


class RecoveryCheckpoint(_StrictModel):
    status: Literal["ready", "missing", "error"]
    checkpoint_id: str = Field(max_length=128)
    sequence: int = Field(ge=0)
    phase: str = Field(max_length=128)
    iteration: int = Field(ge=0)
    created_at: str = Field(max_length=64)


class RecoveryAttemptSummary(_StrictModel):
    """Bounded public attempt facts without the source request digest."""

    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^recovery-[0-9a-f]{64}$")
    state: RecoveryAttemptState
    requested_at: str = Field(max_length=64)
    updated_at: str = Field(max_length=64)
    admitted_at: str = Field(max_length=64)
    resolved_at: str = Field(max_length=64)
    lease_epoch: int = Field(ge=0)
    checkpoint_id: str = Field(max_length=128)
    result_code: str = Field(max_length=64)
    boundary_decision_id: str = Field(max_length=64)


class RecoveryResumeAction(_StrictModel):
    """Advisory UI action derived by the Python recovery authority."""

    schema_version: Literal[1] = 1
    action: Literal["resume"] = "resume"
    state: RecoveryActionState
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: str = Field(min_length=1, max_length=300)
    command: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _command_matches_state(self) -> RecoveryResumeAction:
        if not self.command.startswith("/pursue resume "):
            raise ValueError("Pursuit recovery command 必须使用共享 resume 命令。")
        return self


class PursuitRecoverySnapshot(_StrictModel):
    schema_version: Literal[2] = 2
    run_id: str = Field(min_length=1, max_length=128)
    generated_at: str = Field(max_length=64)
    recovery_state: RecoveryState
    heartbeat: RecoveryHeartbeat
    lease: RecoveryLease
    checkpoint: RecoveryCheckpoint
    reconcile_required: bool
    reconcile_reason: str = Field(max_length=128)
    alerts: tuple[str, ...] = Field(max_length=8)
    resume_action: RecoveryResumeAction
    attempts: tuple[RecoveryAttemptSummary, ...] = Field(max_length=5)


class PursuitRecoveryAuthority(Protocol):
    async def get_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
    ) -> HarnessHeartbeat | None: ...

    async def get_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
    ) -> HarnessRunLease | None: ...


async def build_pursuit_recovery_snapshot(
    run: PursuitRun,
    pursuit_store: PursuitStore,
    authority: PursuitRecoveryAuthority | None,
    *,
    workspace_root: str | Path,
    now: str | None = None,
) -> PursuitRecoverySnapshot:
    """Combine durable facts without changing run, lease, or checkpoint state."""
    generated_at = _normalized_now(now)
    alerts: list[str] = []
    heartbeat = await _heartbeat_projection(
        authority,
        workspace_root=workspace_root,
        run_id=run.id,
        now=generated_at,
        alerts=alerts,
    )
    lease = await _lease_projection(
        authority,
        workspace_root=workspace_root,
        run_id=run.id,
        now=generated_at,
        alerts=alerts,
    )
    checkpoint = _checkpoint_projection(pursuit_store, run.id, alerts=alerts)
    attempts, attempts_available = _attempt_projection(
        pursuit_store,
        run.id,
        alerts=alerts,
    )
    reconcile_reason = _latest_reconcile_reason(run)
    reconcile_required = (
        run.phase == "reconcile_required"
        or checkpoint.phase == "action_inflight"
    )
    state = _recovery_state(
        run,
        heartbeat=heartbeat,
        lease=lease,
        reconcile_required=reconcile_required,
        alerts=alerts,
    )
    return PursuitRecoverySnapshot(
        run_id=run.id,
        generated_at=generated_at,
        recovery_state=state,
        heartbeat=heartbeat,
        lease=lease,
        checkpoint=checkpoint,
        reconcile_required=reconcile_required,
        reconcile_reason=reconcile_reason,
        alerts=tuple(dict.fromkeys(alerts))[:8],
        resume_action=_resume_action(
            run,
            recovery_state=state,
            lease=lease,
            checkpoint=checkpoint,
            attempts_available=attempts_available,
            attempts=attempts,
        ),
        attempts=attempts,
    )


async def _heartbeat_projection(
    authority: PursuitRecoveryAuthority | None,
    *,
    workspace_root: str | Path,
    run_id: str,
    now: str,
    alerts: list[str],
) -> RecoveryHeartbeat:
    if authority is None:
        alerts.append("Heartbeat authority 未接入。")
        return _empty_heartbeat("missing")
    try:
        value = await authority.get_heartbeat(
            workspace_root=workspace_root,
            subject_kind=HarnessRunKind.PURSUIT,
            subject_id=run_id,
        )
        if value is None:
            alerts.append("没有找到该 Pursuit 的持久 heartbeat。")
            return _empty_heartbeat("missing")
        assessed = assess_heartbeat(value, now=now)
    except Exception:
        alerts.append("Heartbeat 状态读取失败，请运行 `/doctor` 检查 Harness Store。")
        return _empty_heartbeat("error")
    return RecoveryHeartbeat(
        health=assessed.health.value,
        phase=value.phase.value,
        instance_id=value.instance_id,
        epoch=value.epoch,
        sequence=value.sequence,
        observed_at=value.observed_at,
        timeout_seconds=value.timeout_seconds,
        age_seconds=max(0, round(assessed.age_seconds)),
        detail_code=value.detail_code,
    )


async def _lease_projection(
    authority: PursuitRecoveryAuthority | None,
    *,
    workspace_root: str | Path,
    run_id: str,
    now: str,
    alerts: list[str],
) -> RecoveryLease:
    if authority is None:
        alerts.append("Run lease authority 未接入。")
        return _empty_lease("missing")
    try:
        value = await authority.get_run_lease(
            workspace_root=workspace_root,
            run_kind=HarnessRunKind.PURSUIT,
            run_id=run_id,
        )
        if value is None:
            alerts.append("没有找到该 Pursuit 的持久 lease。")
            return _empty_lease("missing")
        expired = datetime.fromisoformat(value.expires_at) <= datetime.fromisoformat(now)
    except Exception:
        alerts.append("Run lease 状态读取失败，请运行 `/doctor` 检查 Harness Store。")
        return _empty_lease("error")
    return RecoveryLease(
        status=value.state.value,
        owner_id=value.owner_id,
        epoch=value.epoch,
        expires_at=value.expires_at,
        updated_at=value.updated_at,
        expired=expired,
    )


def _checkpoint_projection(
    pursuit_store: PursuitStore,
    run_id: str,
    *,
    alerts: list[str],
) -> RecoveryCheckpoint:
    try:
        value = pursuit_store.get_checkpoint(run_id)
        if value is None:
            alerts.append("该 Pursuit 没有持久 checkpoint。")
            return _empty_checkpoint("missing")
        return RecoveryCheckpoint(
            status="ready",
            checkpoint_id=value.checkpoint_id(),
            sequence=value.sequence,
            phase=value.phase,
            iteration=value.iteration,
            created_at=datetime.fromtimestamp(value.created_at, UTC).isoformat(),
        )
    except Exception:
        alerts.append("Checkpoint 校验失败，恢复操作已视为不安全。")
        return _empty_checkpoint("error")


def _attempt_projection(
    pursuit_store: PursuitStore,
    run_id: str,
    *,
    alerts: list[str],
) -> tuple[tuple[RecoveryAttemptSummary, ...], bool]:
    try:
        attempts = pursuit_store.list_recovery_attempts(run_id, limit=5)
    except Exception:
        alerts.append("恢复请求账本校验失败，恢复动作已阻止。")
        return (), False
    return tuple(_attempt_summary(item) for item in attempts), True


def _attempt_summary(attempt: PursuitRecoveryAttempt) -> RecoveryAttemptSummary:
    return RecoveryAttemptSummary(
        attempt_id=attempt.attempt_id,
        state=attempt.state.value,
        requested_at=_attempt_time(attempt.requested_at),
        updated_at=_attempt_time(attempt.updated_at),
        admitted_at=_attempt_time(attempt.admitted_at),
        resolved_at=_attempt_time(attempt.resolved_at),
        lease_epoch=attempt.lease_epoch,
        checkpoint_id=attempt.checkpoint_id,
        result_code=attempt.result_code,
        boundary_decision_id=attempt.boundary_decision_id,
    )


def _attempt_time(value: float) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, UTC).isoformat()


def _resume_action(
    run: PursuitRun,
    *,
    recovery_state: RecoveryState,
    lease: RecoveryLease,
    checkpoint: RecoveryCheckpoint,
    attempts_available: bool,
    attempts: tuple[RecoveryAttemptSummary, ...],
) -> RecoveryResumeAction:
    command = f"/pursue resume {run.id}"
    if not attempts_available:
        return RecoveryResumeAction(
            state="blocked",
            code="attempt_ledger_unavailable",
            reason="恢复请求账本不可验证，已阻止发起新恢复。",
            command=command,
        )
    if any(item.state in {"requested", "admitted"} for item in attempts):
        return RecoveryResumeAction(
            state="busy",
            code="recovery_attempt_active",
            reason="已有恢复请求正在准入或执行，不能重复发起。",
            command=command,
        )
    if recovery_state == "terminal":
        return RecoveryResumeAction(
            state="unavailable",
            code="already_terminal",
            reason="该 Pursuit 已处于终态，不需要恢复。",
            command=command,
        )
    if recovery_state == "active":
        return RecoveryResumeAction(
            state="busy",
            code="live_worker",
            reason="当前 worker 与 lease 均健康，不能并发恢复。",
            command=command,
        )
    if recovery_state in {"inconsistent", "reconcile_required"}:
        return RecoveryResumeAction(
            state="blocked",
            code=(
                "reconcile_required"
                if recovery_state == "reconcile_required"
                else "recovery_inconsistent"
            ),
            reason=(
                "存在未核对副作用，必须先完成人工核对。"
                if recovery_state == "reconcile_required"
                else "Heartbeat、lease 或运行状态不一致，不能安全恢复。"
            ),
            command=command,
        )
    if checkpoint.status != "ready":
        return RecoveryResumeAction(
            state="blocked",
            code=(
                "checkpoint_required"
                if checkpoint.status == "missing"
                else "checkpoint_invalid"
            ),
            reason=(
                "该 Pursuit 没有可恢复 checkpoint。"
                if checkpoint.status == "missing"
                else "Checkpoint 校验失败，不能安全恢复。"
            ),
            command=command,
        )
    if lease.status == "error":
        return RecoveryResumeAction(
            state="blocked",
            code="lease_unavailable",
            reason="Run lease 状态不可验证，不能安全恢复。",
            command=command,
        )
    if recovery_state not in {"waiting", "blocked", "orphaned"}:
        return RecoveryResumeAction(
            state="blocked",
            code="recovery_state_unknown",
            reason="当前恢复状态不受支持，请刷新或运行 `/doctor`。",
            command=command,
        )
    return RecoveryResumeAction(
        state="available",
        code="resume_ready",
        reason="Checkpoint 与恢复边界可验证，可通过 ToolExecution 发起恢复。",
        command=command,
    )


def _recovery_state(
    run: PursuitRun,
    *,
    heartbeat: RecoveryHeartbeat,
    lease: RecoveryLease,
    reconcile_required: bool,
    alerts: list[str],
) -> RecoveryState:
    if _identity_inconsistent(heartbeat, lease):
        alerts.append("Heartbeat 与 lease 的 owner/epoch 不一致。")
        return "inconsistent"
    if heartbeat.health == HarnessHeartbeatHealth.CLOCK_REGRESSION.value:
        alerts.append("Heartbeat 时钟倒退，健康状态不可采信。")
        return "inconsistent"
    if reconcile_required:
        return "reconcile_required"
    if run.status.value in {
        "completed", "failed", "cancelled", "budget_exceeded",
    }:
        if lease.status == "active" and not lease.expired:
            alerts.append("Pursuit 已是终态，但仍持有 live lease。")
            return "inconsistent"
        return "terminal"
    if run.status.value == "running":
        if lease.status != "active" or lease.expired:
            alerts.append("Pursuit 标记为运行中，但没有有效 live lease。")
            return "orphaned"
        if heartbeat.health in {"missing", "error", "stale", "offline", "stopped", "failed"}:
            alerts.append("Pursuit 持有 live lease，但 worker heartbeat 不健康。")
            return "inconsistent"
        return "active"
    if run.status.value == "waiting":
        return "waiting"
    if run.status.value == "blocked":
        return "blocked"
    return "unknown"


def _identity_inconsistent(
    heartbeat: RecoveryHeartbeat,
    lease: RecoveryLease,
) -> bool:
    return (
        lease.status == "active"
        and not lease.expired
        and heartbeat.health not in {"missing", "error"}
        and (
            heartbeat.instance_id != lease.owner_id
            or heartbeat.epoch != lease.epoch
        )
    )


def _latest_reconcile_reason(run: PursuitRun) -> str:
    for item in reversed(run.evidence or []):
        if item.kind == "reconcile":
            return str(item.source or "")[:128]
    return ""


def _normalized_now(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Pursuit Recovery now 必须包含时区偏移。")
    return parsed.astimezone(UTC).isoformat()


def _empty_heartbeat(health: Literal["missing", "error"]) -> RecoveryHeartbeat:
    return RecoveryHeartbeat(
        health=health,
        phase="",
        instance_id="",
        epoch=0,
        sequence=0,
        observed_at="",
        timeout_seconds=0,
        age_seconds=0,
        detail_code="",
    )


def _empty_lease(status: Literal["missing", "error"]) -> RecoveryLease:
    return RecoveryLease(
        status=status,
        owner_id="",
        epoch=0,
        expires_at="",
        updated_at="",
        expired=False,
    )


def _empty_checkpoint(
    status: Literal["missing", "error"],
) -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        status=status,
        checkpoint_id="",
        sequence=0,
        phase="",
        iteration=0,
        created_at="",
    )


__all__ = [
    "PursuitRecoveryAuthority",
    "PursuitRecoverySnapshot",
    "RecoveryAttemptSummary",
    "RecoveryResumeAction",
    "build_pursuit_recovery_snapshot",
]
