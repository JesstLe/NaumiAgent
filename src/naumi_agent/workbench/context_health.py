"""Context health scoring for agent workbench cards."""

from __future__ import annotations

from dataclasses import dataclass, field

from naumi_agent.workbench.models import ContextHealth


@dataclass(frozen=True)
class ContextHealthInput:
    has_goal: bool
    has_acceptance_criteria: bool
    minutes_since_sync: int
    token_load_ratio: float
    policy_conflict: bool


@dataclass(frozen=True)
class ContextHealthResult:
    health: ContextHealth
    reasons: list[str] = field(default_factory=list)


def evaluate_context_health(value: ContextHealthInput) -> ContextHealthResult:
    reasons: list[str] = []
    if value.policy_conflict:
        return ContextHealthResult(ContextHealth.CONFLICTED, ["当前计划与意图锁或决策日志冲突"])
    if not value.has_goal:
        reasons.append("缺少 mission 目标")
    if not value.has_acceptance_criteria:
        reasons.append("缺少验收标准")
    if reasons:
        return ContextHealthResult(ContextHealth.MISSING, reasons)
    if value.minutes_since_sync >= 60:
        return ContextHealthResult(ContextHealth.STALE, ["超过 60 分钟未同步上下文"])
    if value.token_load_ratio >= 0.85:
        return ContextHealthResult(ContextHealth.OVERLOADED, ["上下文接近模型窗口上限"])
    return ContextHealthResult(ContextHealth.GOOD, ["上下文健康"])
