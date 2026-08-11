"""Project explicit durable Goals into revocable Evolution need Candidates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.evolution.candidate import EvolutionCandidateDraft, build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.store import EvolutionCandidateStore

if TYPE_CHECKING:
    from naumi_agent.orchestrator.goal_store import Goal, GoalStore

EVOLUTION_GOAL_NEED_OPPORTUNITY_POLICY = "evolution-goal-need-opportunity-v1"
_GOAL_ID_RE = re.compile(r"^goal_[0-9a-f]{12}$")
_GOAL_URI_RE = re.compile(r"^goal://goals/(goal_[0-9a-f]{12})$")
_OPEN_GOAL_STATUSES = frozenset({"active", "paused", "blocked"})


@runtime_checkable
class _GoalStoreReader(Protocol):
    def get(self, goal_id: str) -> Goal | None: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionGoalNeedOpportunityResult(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-goal-need-opportunity-v1"] = (
        EVOLUTION_GOAL_NEED_OPPORTUNITY_POLICY
    )
    status: Literal["recorded"] = "recorded"
    goal_id: str = Field(pattern=r"^goal_[0-9a-f]{12}$")
    goal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal_status: Literal["active", "paused", "blocked"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    occurrence_count: int = Field(ge=1, le=10_000)
    evidence_id: str = Field(pattern=r"^eve_[0-9a-f]{24}$")
    source_authority_valid: Literal[True] = True
    experiment_eligible: Literal[False] = False
    promotion_authority: Literal[False] = False


class EvolutionGoalNeedOpportunityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionGoalNeedOpportunityService:
    """Consume only current durable Goals; never persist the objective text."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        goal_store: GoalStore,
        candidate_store: EvolutionCandidateStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not isinstance(goal_store, _GoalStoreReader):
            raise TypeError("goal_store 必须实现 durable Goal 读取协议。")
        if not isinstance(candidate_store, EvolutionCandidateStore):
            raise TypeError("candidate_store 必须是 EvolutionCandidateStore 实例。")
        self.goal_store = goal_store
        self.candidate_store = candidate_store

    async def discover(self, *, goal_id: str) -> EvolutionGoalNeedOpportunityResult:
        goal = await self._load_open_goal(goal_id)
        evidence = adapt_goal_need_evidence(goal)
        stored = await self.candidate_store.upsert_candidate(
            self.workspace_root,
            build_candidate_draft((evidence,)),
        )
        return EvolutionGoalNeedOpportunityResult(
            goal_id=goal.id,
            goal_sha256=evidence.refs[0].sha256,
            goal_status=_goal_status_value(goal),
            candidate_id=stored.draft.candidate_id,
            candidate_revision=stored.revision,
            occurrence_count=stored.draft.occurrence_count,
            evidence_id=evidence.evidence_id,
        )

    async def validate_candidate_sources(
        self,
        candidate: EvolutionCandidateDraft,
    ) -> bool:
        """Re-read every Goal and require the exact current Evidence projection."""
        selected = tuple(
            item for item in candidate.evidence if item.source_kind == "goal_need"
        )
        if not selected:
            return True
        for expected in selected:
            match = _GOAL_URI_RE.fullmatch(expected.source_uri)
            if match is None:
                return False
            try:
                goal = await self._load_open_goal(match.group(1))
                actual = adapt_goal_need_evidence(goal)
            except (EvolutionGoalNeedOpportunityError, OSError, TypeError, ValueError):
                return False
            if actual != expected:
                return False
        return True

    async def _load_open_goal(self, goal_id: str) -> Goal:
        normalized = str(goal_id or "").strip()
        if _GOAL_ID_RE.fullmatch(normalized) is None:
            raise EvolutionGoalNeedOpportunityError(
                "goal_need_id_invalid",
                "Goal ID 格式无效；仅接受 goal_<12位十六进制>。",
            )
        try:
            goal = await asyncio.to_thread(self.goal_store.get, normalized)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionGoalNeedOpportunityError(
                "goal_need_source_unavailable",
                "Goal 状态库损坏或无法读取。",
            ) from exc
        if goal is None:
            raise EvolutionGoalNeedOpportunityError(
                "goal_need_source_unavailable",
                "当前工作区不存在该 Goal。",
            )
        status = _goal_status_value(goal)
        if status not in _OPEN_GOAL_STATUSES:
            code = (
                "goal_need_withdrawn"
                if status == "cancelled"
                else "goal_need_satisfied"
            )
            message = (
                "Goal 已取消，明确需求 authority 已撤回。"
                if status == "cancelled"
                else "Goal 已完成，不再作为未满足能力机会。"
            )
            raise EvolutionGoalNeedOpportunityError(code, message)
        return goal


def adapt_goal_need_evidence(goal: Goal) -> EvolutionEvidence:
    """Hash an immutable Goal identity without retaining objective or notes."""
    required_fields = ("id", "objective", "status", "session_id", "created_at")
    if any(not hasattr(goal, field) for field in required_fields):
        raise TypeError("Goal need adapter 只能消费 durable Goal。")
    if _goal_status_value(goal) not in _OPEN_GOAL_STATUSES:
        raise ValueError("只有未终结 Goal 才能形成明确需求 Evidence。")
    objective_sha256 = _digest({"objective": goal.objective})
    goal_sha256 = _digest({
        "created_at": goal.created_at,
        "goal_id": goal.id,
        "objective_sha256": objective_sha256,
        "session_id": goal.session_id,
    })
    finding_code = "user_explicit_need"
    scope = f"capability:need:{objective_sha256[:16]}"
    root_fingerprint = _digest({
        "finding_code": finding_code,
        "goal_id": goal.id,
        "objective_sha256": objective_sha256,
        "scope": scope,
    })
    evidence_sha256 = _digest({
        "goal_id": goal.id,
        "goal_sha256": goal_sha256,
        "root_fingerprint": root_fingerprint,
    })
    uri = f"goal://goals/{goal.id}"
    ref = EvolutionEvidenceRef(uri=uri, sha256=goal_sha256)
    return EvolutionEvidence(
        evidence_id=f"eve_{evidence_sha256[:24]}",
        source_kind="goal_need",
        source_uri=uri,
        observed_at=datetime.fromtimestamp(goal.created_at, tz=UTC).isoformat(),
        finding_code=finding_code,
        scope=scope,
        root_fingerprint=root_fingerprint,
        refs=(ref,),
    )


def render_goal_need_opportunity(result: EvolutionGoalNeedOpportunityResult) -> str:
    status_label = {
        "active": "进行中",
        "paused": "已暂停",
        "blocked": "已阻塞",
    }[result.goal_status]
    return "\n".join([
        "# Goal 明确需求已进入机会发现",
        "",
        f"- Goal：`{result.goal_id}` · {status_label}",
        f"- Candidate：`{result.candidate_id}` · revision {result.candidate_revision}",
        f"- 唯一证据：{result.occurrence_count}",
        f"- Evidence：`{result.evidence_id}`",
        "- 来源 authority：已实时重读 durable Goal",
        "- 隐私：未保存 Goal objective、note 或会话正文",
        "- 状态：只允许人工 Review；未授予实验或推广权限",
        "",
        f"下一步：`/evolution detail {result.candidate_id}`",
    ])


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _goal_status_value(goal: Goal) -> str:
    value = getattr(goal.status, "value", goal.status)
    return str(value).strip().lower()


__all__ = [
    "EVOLUTION_GOAL_NEED_OPPORTUNITY_POLICY",
    "EvolutionGoalNeedOpportunityError",
    "EvolutionGoalNeedOpportunityResult",
    "EvolutionGoalNeedOpportunityService",
    "adapt_goal_need_evidence",
    "render_goal_need_opportunity",
]
