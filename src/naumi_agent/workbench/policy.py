"""Human intent lock and risk policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from naumi_agent.workbench.models import IntentLock, RiskLevel

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_proposal: bool
    reason: str
    matched_lock_id: str = ""


def evaluate_intent_locks(
    *,
    mission_id: str,
    changed_paths: list[str],
    risk_level: RiskLevel,
    intent_locks: list[IntentLock],
) -> PolicyDecision:
    """Return whether an action may execute directly under active human intent locks."""
    normalized_paths = [path.strip() for path in changed_paths if path.strip()]
    for lock in intent_locks:
        if not lock.active or lock.mission_id != mission_id:
            continue
        for prefix in lock.blocked_paths:
            if any(path.startswith(prefix) for path in normalized_paths):
                return PolicyDecision(
                    allowed=False,
                    requires_proposal=True,
                    reason=f"命中意图锁：{lock.rule}",
                    matched_lock_id=lock.id,
                )
        if _RISK_ORDER[risk_level] >= _RISK_ORDER[lock.require_proposal_for_risk]:
            return PolicyDecision(
                allowed=False,
                requires_proposal=True,
                reason=f"风险等级需要先提交 proposal：{lock.rule}",
                matched_lock_id=lock.id,
            )
    return PolicyDecision(allowed=True, requires_proposal=False, reason="允许执行")
