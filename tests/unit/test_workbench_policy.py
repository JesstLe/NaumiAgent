from __future__ import annotations

from naumi_agent.workbench.models import IntentLock, RiskLevel
from naumi_agent.workbench.policy import PolicyDecision, evaluate_intent_locks


def test_blocked_path_requires_proposal() -> None:
    decision = evaluate_intent_locks(
        mission_id="m1",
        changed_paths=["src/naumi_agent/model/router.py"],
        risk_level=RiskLevel.MEDIUM,
        intent_locks=[
            IntentLock(
                id="lock-1",
                session_id="s",
                mission_id="m1",
                rule="本轮不触碰模型路由",
                blocked_paths=["src/naumi_agent/model/"],
            )
        ],
    )

    assert decision == PolicyDecision(
        allowed=False,
        requires_proposal=True,
        reason="命中意图锁：本轮不触碰模型路由",
        matched_lock_id="lock-1",
    )


def test_high_risk_requires_proposal_even_without_path_match() -> None:
    decision = evaluate_intent_locks(
        mission_id="m1",
        changed_paths=["docs/README.md"],
        risk_level=RiskLevel.HIGH,
        intent_locks=[
            IntentLock(
                id="lock-2",
                session_id="s",
                mission_id="m1",
                rule="高风险任务先提交 proposal",
                require_proposal_for_risk=RiskLevel.HIGH,
            )
        ],
    )

    assert not decision.allowed
    assert decision.requires_proposal
    assert "高风险任务先提交 proposal" in decision.reason


def test_deactivated_intent_lock_no_longer_blocks_actions() -> None:
    """A deactivated lock must not block — the user can turn off governance."""
    decision = evaluate_intent_locks(
        mission_id="m1",
        changed_paths=["src/secret/api_keys.py"],
        risk_level=RiskLevel.CRITICAL,
        intent_locks=[
            IntentLock(
                id="lock-inactive",
                session_id="s",
                mission_id="m1",
                rule="禁止修改密钥目录",
                blocked_paths=["src/secret/"],
                require_proposal_for_risk=RiskLevel.HIGH,
                active=False,
            )
        ],
    )

    assert decision.allowed
    assert not decision.requires_proposal


def test_decision_strength_values_are_distinct() -> None:
    from naumi_agent.workbench.models import DecisionStrength

    assert DecisionStrength.ADVISORY.value == "advisory"
    assert DecisionStrength.REQUIRED.value == "required"
    assert DecisionStrength.BLOCKING.value == "blocking"
    strengths = {DecisionStrength.ADVISORY, DecisionStrength.REQUIRED, DecisionStrength.BLOCKING}
    assert len(strengths) == 3
