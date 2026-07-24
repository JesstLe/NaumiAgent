"""Deterministic terminal-boundary decisions for long-running Pursuit runs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PursuitBoundaryStatus = Literal[
    "running",
    "waiting",
    "blocked",
    "completed",
    "cancelled",
    "budget_exceeded",
]
BudgetBreach = Literal["none", "time", "cost", "iterations"]
WaitingKind = Literal["none", "background", "interaction"]
BlockerKind = Literal[
    "none",
    "no_success_criteria",
    "planner_empty",
    "stagnation",
    "stagnation_no_recovery",
    "waiting_without_authority",
]
FinalVerification = Literal["not_run", "passed", "failed"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PursuitBoundaryFacts(_StrictModel):
    """Bounded mechanical facts allowed to influence one run boundary."""

    criterion_count: int = Field(ge=0, le=1_000)
    verified_count: int = Field(ge=0, le=1_000)
    hard_evidence_count: int = Field(ge=0, le=1_000)
    final_verification: FinalVerification = "not_run"
    cancel_requested: bool = False
    budget_breach: BudgetBreach = "none"
    waiting_kind: WaitingKind = "none"
    waiting_count: int = Field(default=0, ge=0, le=10_000)
    blocker: BlockerKind = "none"

    @model_validator(mode="after")
    def _integrity(self) -> PursuitBoundaryFacts:
        if self.verified_count > self.criterion_count:
            raise ValueError("verified_count 不得超过 criterion_count。")
        if self.hard_evidence_count > self.verified_count:
            raise ValueError("hard_evidence_count 不得超过 verified_count。")
        if (self.waiting_kind == "none") != (self.waiting_count == 0):
            raise ValueError("waiting_kind 与 waiting_count 必须一致。")
        if self.waiting_kind == "interaction" and self.waiting_count != 1:
            raise ValueError("一次 Pursuit 边界只能等待一个用户交互。")
        if (
            not self.cancel_requested
            and self.waiting_kind != "none"
            and self.blocker != "none"
        ):
            raise ValueError("同一边界不能同时声明 waiting 与 blocker。")
        if not self.cancel_requested and self.budget_breach != "none" and (
            self.waiting_kind != "none" or self.blocker != "none"
        ):
            raise ValueError("预算越界不能与 waiting/blocker 混合。")
        if (
            not self.cancel_requested
            and self.final_verification != "not_run"
            and (
                self.budget_breach != "none"
                or self.waiting_kind != "none"
                or self.blocker != "none"
            )
        ):
            raise ValueError("最终验证不能与预算、waiting 或 blocker 混合。")
        if self.final_verification == "passed" and (
            self.criterion_count == 0
            or self.verified_count != self.criterion_count
            or self.hard_evidence_count != self.criterion_count
        ):
            raise ValueError("最终验证通过必须绑定全部标准的强证据。")
        return self


class PursuitBoundaryDecision(_StrictModel):
    """Authenticated-by-content decision consumed by Pursuit runtime and UI."""

    schema_version: Literal[1] = 1
    decision_id: str
    facts: PursuitBoundaryFacts
    facts_sha256: str
    status: PursuitBoundaryStatus
    code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=300)
    next_action: str = Field(min_length=1, max_length=300)
    terminal: bool
    resumable: bool

    @model_validator(mode="after")
    def _integrity(self) -> PursuitBoundaryDecision:
        if not _SHA256_RE.fullmatch(self.decision_id):
            raise ValueError("decision_id 必须是完整小写 SHA-256。")
        if not _SHA256_RE.fullmatch(self.facts_sha256):
            raise ValueError("facts_sha256 必须是完整小写 SHA-256。")
        if self.code != self.code.strip() or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}",
            self.code,
        ):
            raise ValueError("decision code 格式无效。")
        if self.reason != self.reason.strip() or self.next_action != self.next_action.strip():
            raise ValueError("decision 文案不得包含首尾空白。")
        if self.facts_sha256 != _sha256_json(self.facts.model_dump(mode="json")):
            raise ValueError("facts_sha256 与机械事实不一致。")
        expected_terminal = self.status in {
            "blocked",
            "completed",
            "cancelled",
            "budget_exceeded",
        }
        expected_resumable = self.status in {"waiting", "blocked"}
        if self.terminal != expected_terminal or self.resumable != expected_resumable:
            raise ValueError("decision terminal/resumable 与 status 不一致。")
        if self.decision_id != pursuit_boundary_decision_sha256(self):
            raise ValueError("decision_id 与决策内容不一致。")
        return self


def decide_pursuit_boundary(facts: PursuitBoundaryFacts) -> PursuitBoundaryDecision:
    """Return one deterministic, fail-closed decision from mechanical facts."""
    if facts.cancel_requested:
        values = _decision_values(
            facts,
            status="cancelled",
            code="user_cancelled",
            reason="用户取消了目标追踪。",
            next_action="如需继续，请创建新运行或从人工确认的 checkpoint 恢复。",
        )
    elif facts.budget_breach != "none":
        label = {
            "time": ("time_budget_exceeded", "目标追踪超过最大运行时间。"),
            "cost": ("cost_budget_exceeded", "目标追踪超过预算上限。"),
            "iterations": ("iteration_budget_exceeded", "目标追踪超过最大迭代次数。"),
        }[facts.budget_breach]
        values = _decision_values(
            facts,
            status="budget_exceeded",
            code=label[0],
            reason=label[1],
            next_action="审查预算与当前证据后，显式创建后续运行。",
        )
    elif facts.waiting_kind != "none":
        interaction = facts.waiting_kind == "interaction"
        values = _decision_values(
            facts,
            status="waiting",
            code="waiting_for_interaction" if interaction else "waiting_for_background",
            reason=(
                "目标追踪正在等待用户回答。"
                if interaction
                else f"目标追踪正在等待 {facts.waiting_count} 个后台任务。"
            ),
            next_action=(
                "回答当前交互后，从持久 checkpoint 继续。"
                if interaction
                else "后台任务产生终态证据后，重新核对并继续。"
            ),
        )
    elif facts.blocker != "none":
        code, reason, next_action = {
            "no_success_criteria": (
                "no_success_criteria",
                "目标没有可机械验证的成功标准。",
                "补充至少一个可定向验证的成功标准后重新启动。",
            ),
            "planner_empty": (
                "planner_empty",
                "规划器没有给出下一步可执行行动。",
                "审查目标、约束与现有证据后重新规划。",
            ),
            "stagnation": (
                "stagnation",
                "连续多轮没有可观测进展。",
                "人工审查停滞证据，调整目标或恢复策略后继续。",
            ),
            "stagnation_no_recovery": (
                "stagnation_no_recovery",
                "检测到停滞，但没有生成可执行的恢复行动。",
                "人工提供恢复行动或缩小目标范围后继续。",
            ),
            "waiting_without_authority": (
                "waiting_without_authority",
                "行动报告等待，但没有可恢复的后台任务引用。",
                "核对行动账本和外部状态后，再决定重试或取消。",
            ),
        }[facts.blocker]
        values = _decision_values(
            facts,
            status="blocked",
            code=code,
            reason=reason,
            next_action=next_action,
        )
    elif facts.criterion_count == 0:
        return decide_pursuit_boundary(
            facts.model_copy(update={"blocker": "no_success_criteria"})
        )
    elif facts.final_verification == "failed":
        values = _decision_values(
            facts,
            status="running",
            code="final_verification_failed",
            reason="最终验证未通过，不能声明完成。",
            next_action="根据失败证据继续修复并重新运行定向验证。",
        )
    elif (
        facts.verified_count == facts.criterion_count
        and facts.hard_evidence_count == facts.criterion_count
    ):
        if facts.final_verification == "passed":
            values = _decision_values(
                facts,
                status="completed",
                code="completed_verified",
                reason="所有成功标准已通过强制验证。",
                next_action="保留完成证据并生成最终回执。",
            )
        else:
            values = _decision_values(
                facts,
                status="running",
                code="final_verification_required",
                reason="全部成功标准已有强证据，仍需执行最终定向验证。",
                next_action="运行成功标准声明的定向验证后重新裁决。",
            )
    else:
        values = _decision_values(
            facts,
            status="running",
            code="criteria_incomplete",
            reason="仍有成功标准未通过强证据验证。",
            next_action="继续处理未验证标准并收集机械证据。",
        )
    return PursuitBoundaryDecision(
        decision_id=_sha256_json(values),
        **values,
    )


def pursuit_boundary_decision_sha256(decision: PursuitBoundaryDecision) -> str:
    payload = decision.model_dump(mode="json")
    payload.pop("decision_id", None)
    return _sha256_json(payload)


def _decision_values(
    facts: PursuitBoundaryFacts,
    *,
    status: PursuitBoundaryStatus,
    code: str,
    reason: str,
    next_action: str,
) -> dict[str, object]:
    facts_payload = facts.model_dump(mode="json")
    return {
        "schema_version": 1,
        "facts": facts_payload,
        "facts_sha256": _sha256_json(facts_payload),
        "status": status,
        "code": code,
        "reason": reason,
        "next_action": next_action,
        "terminal": status in {
            "blocked",
            "completed",
            "cancelled",
            "budget_exceeded",
        },
        "resumable": status in {"waiting", "blocked"},
    }


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PursuitBoundaryDecision",
    "PursuitBoundaryFacts",
    "decide_pursuit_boundary",
    "pursuit_boundary_decision_sha256",
]
