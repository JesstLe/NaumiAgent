"""Fail-closed lifecycle policy shared by Harness retention workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LifecyclePolicy(StrEnum):
    """Persistence policy for a Session and its derived Harness records."""

    RETAIN = "retain"
    ARCHIVE = "archive"
    DELETE = "delete"
    LEGAL_HOLD = "legal_hold"


class LifecycleActor(StrEnum):
    """Closed set of authorities allowed to request policy transitions."""

    USER = "user"
    RETENTION_WORKER = "retention_worker"
    SYSTEM_RECOVERY = "system_recovery"


@dataclass(frozen=True, slots=True)
class LifecycleTransitionDecision:
    """Side-effect-free decision consumed before persistence reconciliation."""

    current_policy: LifecyclePolicy
    requested_policy: LifecyclePolicy
    effective_policy: LifecyclePolicy
    actor: LifecycleActor
    allowed: bool
    idempotent: bool
    requires_audit: bool
    automatic_cleanup_allowed: bool
    reason: str


def permits_automatic_cleanup(policy: LifecyclePolicy | str) -> bool:
    """Return whether derived records may enter automatic cleanup."""
    return _coerce_policy(policy, field="policy") is LifecyclePolicy.DELETE


def policy_from_session_status(status: str) -> LifecyclePolicy:
    """Map current Session Store status to the shared lifecycle policy."""
    normalized = status.strip() if isinstance(status, str) else ""
    mapping = {
        "active": LifecyclePolicy.RETAIN,
        "archived": LifecyclePolicy.ARCHIVE,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError("status 包含未知 Session 生命周期状态。") from exc


def decide_lifecycle_transition(
    current_policy: LifecyclePolicy | str,
    requested_policy: LifecyclePolicy | str,
    *,
    actor: LifecycleActor | str,
    audit_note: str = "",
) -> LifecycleTransitionDecision:
    """Evaluate one transition without mutating Session, Harness, or artifacts."""
    current = _coerce_policy(current_policy, field="current_policy")
    requested = _coerce_policy(requested_policy, field="requested_policy")
    normalized_actor = _coerce_actor(actor)
    note = audit_note.strip() if isinstance(audit_note, str) else ""

    if current is requested:
        return _decision(
            current=current,
            requested=requested,
            actor=normalized_actor,
            allowed=True,
            idempotent=True,
            requires_audit=False,
            reason="目标策略已经生效；本次请求可安全重放。",
        )

    touches_legal_hold = (
        current is LifecyclePolicy.LEGAL_HOLD
        or requested is LifecyclePolicy.LEGAL_HOLD
    )
    if touches_legal_hold:
        if normalized_actor is not LifecycleActor.USER:
            return _blocked(
                current,
                requested,
                normalized_actor,
                "Legal hold 只能由用户明确设置或解除。",
                requires_audit=True,
            )
        if not note:
            return _blocked(
                current,
                requested,
                normalized_actor,
                "设置或解除 legal hold 必须提供审计说明。",
                requires_audit=True,
            )
        return _decision(
            current=current,
            requested=requested,
            actor=normalized_actor,
            allowed=True,
            idempotent=False,
            requires_audit=True,
            reason="用户已提供 legal hold 变更的审计说明。",
        )

    if normalized_actor is LifecycleActor.USER:
        return _decision(
            current=current,
            requested=requested,
            actor=normalized_actor,
            allowed=True,
            idempotent=False,
            requires_audit=requested is LifecyclePolicy.DELETE,
            reason="用户明确请求了 Session 生命周期策略变更。",
        )

    if normalized_actor is LifecycleActor.RETENTION_WORKER:
        if current is LifecyclePolicy.ARCHIVE and requested is LifecyclePolicy.DELETE:
            return _decision(
                current=current,
                requested=requested,
                actor=normalized_actor,
                allowed=True,
                idempotent=False,
                requires_audit=True,
                reason="归档 Session 已通过保留资格检查，可进入协调删除。",
            )
        return _blocked(
            current,
            requested,
            normalized_actor,
            "Retention worker 只允许执行 archive 到 delete 的转换。",
        )

    return _blocked(
        current,
        requested,
        normalized_actor,
        "System recovery 只能幂等重放已经生效的策略。",
    )


def _decision(
    *,
    current: LifecyclePolicy,
    requested: LifecyclePolicy,
    actor: LifecycleActor,
    allowed: bool,
    idempotent: bool,
    requires_audit: bool,
    reason: str,
) -> LifecycleTransitionDecision:
    effective = requested if allowed else current
    return LifecycleTransitionDecision(
        current_policy=current,
        requested_policy=requested,
        effective_policy=effective,
        actor=actor,
        allowed=allowed,
        idempotent=idempotent,
        requires_audit=requires_audit,
        automatic_cleanup_allowed=(
            allowed and permits_automatic_cleanup(effective)
        ),
        reason=reason,
    )


def _blocked(
    current: LifecyclePolicy,
    requested: LifecyclePolicy,
    actor: LifecycleActor,
    reason: str,
    *,
    requires_audit: bool = False,
) -> LifecycleTransitionDecision:
    return _decision(
        current=current,
        requested=requested,
        actor=actor,
        allowed=False,
        idempotent=False,
        requires_audit=requires_audit,
        reason=reason,
    )


def _coerce_policy(value: LifecyclePolicy | str, *, field: str) -> LifecyclePolicy:
    if isinstance(value, LifecyclePolicy):
        return value
    try:
        return LifecyclePolicy(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 包含未知生命周期策略。") from exc


def _coerce_actor(value: LifecycleActor | str) -> LifecycleActor:
    if isinstance(value, LifecycleActor):
        return value
    try:
        return LifecycleActor(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("actor 包含未知生命周期操作者。") from exc
