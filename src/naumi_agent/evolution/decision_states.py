"""Deterministic final decision states for one Evolution experiment."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.counterfactual_evidence import (
    EvolutionCounterfactualEvidence,
    EvolutionCounterfactualEvidenceError,
    EvolutionCounterfactualEvidenceStore,
)
from naumi_agent.evolution.independent_reviews import (
    EvolutionIndependentReview,
    EvolutionIndependentReviewError,
    EvolutionIndependentReviewStore,
    IndependentReviewStatus,
)
from naumi_agent.evolution.mechanical_gates import (
    EvolutionMechanicalGate,
    EvolutionMechanicalGateError,
    EvolutionMechanicalGateStore,
)
from naumi_agent.evolution.reward_hacking_evidence import (
    EvolutionRewardHackingEvidence,
    EvolutionRewardHackingEvidenceError,
    EvolutionRewardHackingEvidenceStore,
)
from naumi_agent.user_interaction import normalize_interaction_request

EVOLUTION_DECISION_STATE_POLICY = "evolution-decision-state-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 32 * 1_024 * 1_024


class EvolutionDecisionStateValue(StrEnum):
    ACCEPTED_EXPERIMENT = "accepted_experiment"
    REVISE = "revise"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class EvolutionDecisionReason(StrEnum):
    MECHANICAL_VETO = "mechanical_veto"
    COUNTERFACTUAL_CONCERN = "counterfactual_concern"
    REWARD_HACKING_CONCERN = "reward_hacking_concern"
    EVIDENCE_INCONCLUSIVE = "evidence_inconclusive"
    HIGH_RISK_REQUIRES_HUMAN = "high_risk_requires_human"
    ALL_STRUCTURED_EVIDENCE_CLEAR = "all_structured_evidence_clear"


class EvolutionDecisionCheckStatus(StrEnum):
    PASS = "pass"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class EvolutionDecisionRule(StrEnum):
    MECHANICAL_GATE_PRESERVED = "mechanical_gate_preserved"
    REVIEW_AUTHORITY_BOUND = "review_authority_bound"
    COUNTERFACTUAL_AUTHORITY_BOUND = "counterfactual_authority_bound"
    REWARD_HACKING_AUTHORITY_BOUND = "reward_hacking_authority_bound"
    STRUCTURED_EVIDENCE_CLEAR = "structured_evidence_clear"
    RISK_POLICY_SATISFIED = "risk_policy_satisfied"
    REVIEWER_ADVISORY_NON_AUTHORITATIVE = "reviewer_advisory_non_authoritative"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionDecisionEscalationOption(_StrictModel):
    value: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)

    @field_validator("label", "description")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        return _clean_text(value)


class EvolutionDecisionEscalationRequest(_StrictModel):
    header: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=2_000)
    options: tuple[EvolutionDecisionEscalationOption, ...] = Field(
        min_length=3,
        max_length=3,
    )
    allow_custom: Literal[True] = True
    custom_label: str = Field(min_length=1, max_length=80)
    timeout_seconds: None = None

    @field_validator("header", "question", "custom_label")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _compatible_with_runtime_interaction(self) -> Self:
        if len({item.value for item in self.options}) != len(self.options):
            raise ValueError("Decision escalation option value 不得重复。")
        normalized = normalize_interaction_request(self.to_public_dict())
        if normalized.to_public_dict() != self.to_public_dict():
            raise ValueError("Decision escalation 与 runtime interaction 协议不一致。")
        return self

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "question": self.question,
            "options": [item.model_dump(mode="json") for item in self.options],
            "allow_custom": self.allow_custom,
            "custom_label": self.custom_label,
            "timeout_seconds": self.timeout_seconds,
        }


class EvolutionDecisionCheck(_StrictModel):
    order: int = Field(ge=1, le=16)
    rule: EvolutionDecisionRule
    status: EvolutionDecisionCheckStatus
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _refs_are_safe(self) -> Self:
        if self.evidence_refs != tuple(dict.fromkeys(self.evidence_refs)):
            raise ValueError("Decision check evidence refs 不得重复。")
        if any(
            not value or len(value) > 256 or any(char in value for char in ("\x00", "\r", "\n"))
            for value in self.evidence_refs
        ):
            raise ValueError("Decision check evidence ref 无效。")
        return self


class EvolutionDecisionState(_StrictModel):
    """Immutable structured decision; it never performs promotion."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-decision-state-v1"] = EVOLUTION_DECISION_STATE_POLICY
    decision_id: str = Field(pattern=r"^evdecision_[0-9a-f]{24}$")
    decision_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    decision_input_sha256: str = Field(pattern=_SHA256_RE)
    mechanical_gate_id: str = Field(pattern=r"^evgate_[0-9a-f]{24}$")
    mechanical_gate_sha256: str = Field(pattern=_SHA256_RE)
    independent_review_id: str = Field(pattern=r"^evreview_[0-9a-f]{24}$")
    independent_review_sha256: str = Field(pattern=_SHA256_RE)
    counterfactual_evidence_id: str | None = Field(
        default=None,
        pattern=r"^evcounter_[0-9a-f]{24}$",
    )
    counterfactual_evidence_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    reward_hacking_evidence_id: str | None = Field(
        default=None,
        pattern=r"^evreward_[0-9a-f]{24}$",
    )
    reward_hacking_evidence_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    state: EvolutionDecisionStateValue
    reasons: tuple[EvolutionDecisionReason, ...] = Field(min_length=1, max_length=4)
    checks: tuple[EvolutionDecisionCheck, ...] = Field(min_length=7, max_length=7)
    escalation: EvolutionDecisionEscalationRequest | None = None
    requires_user_input: bool
    candidate_acceptance_decided: bool
    candidate_accepted: bool
    experiment_accepted: bool
    promotion_review_ready: bool
    promotion_executed: Literal[False] = False
    reviewer_advisory_only: Literal[True] = True
    llm_decision_authority: Literal[False] = False
    terminal_state_immutable: Literal[True] = True
    gate: EvolutionMechanicalGate
    review: EvolutionIndependentReview
    counterfactual: EvolutionCounterfactualEvidence | None = None
    reward_hacking: EvolutionRewardHackingEvidence | None = None
    created_at: str = Field(min_length=1, max_length=100)

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Decision State workspace 必须是安全绝对路径。")
        return str(path.resolve())

    @model_validator(mode="after")
    def _decision_is_exact_and_tamper_evident(self) -> Self:
        _validate_authority_chain(
            self.gate,
            self.review,
            self.counterfactual,
            self.reward_hacking,
        )
        decision_input = self.gate.decision_input
        state, reasons = resolve_evolution_decision_state(
            mechanical_gate_outcome=self.gate.outcome,
            counterfactual_outcome=(
                self.counterfactual.outcome if self.counterfactual is not None else None
            ),
            reward_hacking_outcome=(
                self.reward_hacking.outcome if self.reward_hacking is not None else None
            ),
            risk_level=decision_input.risk_level,
        )
        checks = _build_checks(
            gate=self.gate,
            review=self.review,
            counterfactual=self.counterfactual,
            reward_hacking=self.reward_hacking,
            state=state,
        )
        escalation = (
            _build_escalation(reasons) if state is EvolutionDecisionStateValue.ESCALATED else None
        )
        requires_user_input, acceptance_decided, accepted = _state_flags(state)
        if not (
            self.workspace_root == self.gate.workspace_root == self.review.workspace_root
            and self.decision_input_id == decision_input.decision_input_id
            and self.decision_input_sha256 == decision_input.decision_input_sha256
            and self.mechanical_gate_id == self.gate.gate_id
            and self.mechanical_gate_sha256 == self.gate.gate_sha256
            and self.independent_review_id == self.review.review_id
            and self.independent_review_sha256 == self.review.review_sha256
            and self.counterfactual_evidence_id
            == (self.counterfactual.evidence_id if self.counterfactual else None)
            and self.counterfactual_evidence_sha256
            == (self.counterfactual.evidence_sha256 if self.counterfactual else None)
            and self.reward_hacking_evidence_id
            == (self.reward_hacking.evidence_id if self.reward_hacking else None)
            and self.reward_hacking_evidence_sha256
            == (self.reward_hacking.evidence_sha256 if self.reward_hacking else None)
            and self.candidate_id == decision_input.candidate_id
            and self.candidate_revision == decision_input.candidate_revision
            and self.risk_level == decision_input.risk_level
            and self.state is state
            and self.reasons == reasons
            and self.checks == checks
            and self.escalation == escalation
            and self.requires_user_input is requires_user_input
            and self.candidate_acceptance_decided is acceptance_decided
            and self.candidate_accepted is accepted
            and self.experiment_accepted is accepted
            and self.promotion_review_ready is accepted
            and self.created_at == self.review.reviewed_at
        ):
            raise ValueError("Decision State authority、状态或 readiness 投影不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"decision_id", "decision_sha256"})
        )
        if not hmac.compare_digest(self.decision_sha256, digest):
            raise ValueError("Decision State 摘要不一致。")
        if self.decision_id != f"evdecision_{digest[:24]}":
            raise ValueError("Decision State identity 不一致。")
        return self


class EvolutionDecisionStateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionDecisionStateBuilder:
    def build(
        self,
        *,
        gate: EvolutionMechanicalGate,
        review: EvolutionIndependentReview,
        counterfactual: EvolutionCounterfactualEvidence | None,
        reward_hacking: EvolutionRewardHackingEvidence | None,
    ) -> EvolutionDecisionState:
        try:
            gate = EvolutionMechanicalGate.model_validate(gate.model_dump(mode="json"))
            review = EvolutionIndependentReview.model_validate(review.model_dump(mode="json"))
            counterfactual = (
                EvolutionCounterfactualEvidence.model_validate(
                    counterfactual.model_dump(mode="json")
                )
                if counterfactual is not None
                else None
            )
            reward_hacking = (
                EvolutionRewardHackingEvidence.model_validate(
                    reward_hacking.model_dump(mode="json")
                )
                if reward_hacking is not None
                else None
            )
            _validate_authority_chain(gate, review, counterfactual, reward_hacking)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_authority_invalid",
                "Decision State authority 无效、不完整或已被篡改。",
            ) from exc
        decision_input = gate.decision_input
        state, reasons = resolve_evolution_decision_state(
            mechanical_gate_outcome=gate.outcome,
            counterfactual_outcome=(counterfactual.outcome if counterfactual is not None else None),
            reward_hacking_outcome=(reward_hacking.outcome if reward_hacking is not None else None),
            risk_level=decision_input.risk_level,
        )
        escalation = (
            _build_escalation(reasons) if state is EvolutionDecisionStateValue.ESCALATED else None
        )
        requires_user_input, acceptance_decided, accepted = _state_flags(state)
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_DECISION_STATE_POLICY,
            "workspace_root": gate.workspace_root,
            "decision_input_id": decision_input.decision_input_id,
            "decision_input_sha256": decision_input.decision_input_sha256,
            "mechanical_gate_id": gate.gate_id,
            "mechanical_gate_sha256": gate.gate_sha256,
            "independent_review_id": review.review_id,
            "independent_review_sha256": review.review_sha256,
            "counterfactual_evidence_id": (counterfactual.evidence_id if counterfactual else None),
            "counterfactual_evidence_sha256": (
                counterfactual.evidence_sha256 if counterfactual else None
            ),
            "reward_hacking_evidence_id": (reward_hacking.evidence_id if reward_hacking else None),
            "reward_hacking_evidence_sha256": (
                reward_hacking.evidence_sha256 if reward_hacking else None
            ),
            "candidate_id": decision_input.candidate_id,
            "candidate_revision": decision_input.candidate_revision,
            "risk_level": decision_input.risk_level,
            "state": state.value,
            "reasons": [item.value for item in reasons],
            "checks": [
                item.model_dump(mode="json")
                for item in _build_checks(
                    gate=gate,
                    review=review,
                    counterfactual=counterfactual,
                    reward_hacking=reward_hacking,
                    state=state,
                )
            ],
            "escalation": escalation.model_dump(mode="json") if escalation else None,
            "requires_user_input": requires_user_input,
            "candidate_acceptance_decided": acceptance_decided,
            "candidate_accepted": accepted,
            "experiment_accepted": accepted,
            "promotion_review_ready": accepted,
            "promotion_executed": False,
            "reviewer_advisory_only": True,
            "llm_decision_authority": False,
            "terminal_state_immutable": True,
            "gate": gate.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "counterfactual": (counterfactual.model_dump(mode="json") if counterfactual else None),
            "reward_hacking": (reward_hacking.model_dump(mode="json") if reward_hacking else None),
            "created_at": review.reviewed_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionDecisionState.model_validate(
                {
                    **payload,
                    "decision_id": f"evdecision_{digest[:24]}",
                    "decision_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionDecisionStateError(
                "decision_state_artifact_invalid",
                "Decision State artifact 无法验证。",
            ) from exc


class EvolutionDecisionStateStore:
    """Immutable one-decision-per-Decision-Input storage."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, artifact: EvolutionDecisionState) -> EvolutionDecisionState:
        try:
            item = EvolutionDecisionState.model_validate(artifact.model_dump(mode="json"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_artifact_invalid",
                "Decision State 无效或已被篡改。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionDecisionStateError(
                "decision_state_artifact_oversized",
                "Decision State 超过 32 MiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_decision_states WHERE decision_input_id = ?",
                        (item.decision_input_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionDecisionStateError(
                            "decision_state_conflict",
                            "同一 Decision Input 不可覆盖为不同 Decision State。",
                        )
                    await db.rollback()
                    return existing
                await db.execute(
                    "INSERT INTO evolution_decision_states "
                    "(decision_id, decision_sha256, decision_input_id, "
                    "decision_input_sha256, workspace_root, state, decision_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.decision_id,
                        item.decision_sha256,
                        item.decision_input_id,
                        item.decision_input_sha256,
                        item.workspace_root,
                        item.state.value,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionDecisionStateError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_store_error",
                "Decision State 无法持久化。",
            ) from exc
        restored = await self.get(item.decision_id)
        assert restored is not None
        return restored

    async def get(self, decision_id: str) -> EvolutionDecisionState | None:
        if (
            not isinstance(decision_id, str)
            or re.fullmatch(r"evdecision_[0-9a-f]{24}", decision_id) is None
        ):
            raise ValueError("decision_id 格式无效。")
        return await self._read("decision_id", decision_id)

    async def get_by_decision_input(
        self,
        decision_input_id: str,
    ) -> EvolutionDecisionState | None:
        if (
            not isinstance(decision_input_id, str)
            or re.fullmatch(r"evdin_[0-9a-f]{24}", decision_input_id) is None
        ):
            raise ValueError("decision_input_id 格式无效。")
        return await self._read("decision_input_id", decision_input_id)

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionDecisionState | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_decision_states WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_store_corrupt",
                "Decision State 损坏或无法读取。",
            ) from exc


class EvolutionDecisionStateExecutor:
    def __init__(
        self,
        *,
        gate_store: EvolutionMechanicalGateStore,
        review_store: EvolutionIndependentReviewStore,
        counterfactual_store: EvolutionCounterfactualEvidenceStore,
        reward_hacking_store: EvolutionRewardHackingEvidenceStore,
        decision_store: EvolutionDecisionStateStore,
        builder: EvolutionDecisionStateBuilder | None = None,
    ) -> None:
        if not isinstance(gate_store, EvolutionMechanicalGateStore):
            raise TypeError("Decision State executor 需要 Mechanical Gate Store。")
        if not isinstance(review_store, EvolutionIndependentReviewStore):
            raise TypeError("Decision State executor 需要 Independent Review Store。")
        if not isinstance(counterfactual_store, EvolutionCounterfactualEvidenceStore):
            raise TypeError("Decision State executor 需要 Counterfactual Store。")
        if not isinstance(reward_hacking_store, EvolutionRewardHackingEvidenceStore):
            raise TypeError("Decision State executor 需要 Reward-hacking Store。")
        if not isinstance(decision_store, EvolutionDecisionStateStore):
            raise TypeError("Decision State executor 需要 Decision Store。")
        self._gate_store = gate_store
        self._review_store = review_store
        self._counterfactual_store = counterfactual_store
        self._reward_hacking_store = reward_hacking_store
        self._decision_store = decision_store
        self._builder = builder or EvolutionDecisionStateBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        decision_input_id: str,
    ) -> EvolutionDecisionState:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(decision_input_id, str)
            or re.fullmatch(r"evdin_[0-9a-f]{24}", decision_input_id) is None
        ):
            raise EvolutionDecisionStateError(
                "decision_state_input_id_invalid",
                "Decision Input ID 格式无效。",
            )
        try:
            gate = await self._gate_store.get_by_decision_input(decision_input_id)
        except (EvolutionMechanicalGateError, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_gate_read_failed",
                "无法读取 Decision State Mechanical Gate authority。",
            ) from exc
        if gate is None:
            raise EvolutionDecisionStateError(
                "decision_state_gate_missing",
                "Decision Input 尚无 Mechanical Gate。",
            )
        if gate.workspace_root != str(workspace):
            raise EvolutionDecisionStateError(
                "decision_state_workspace_mismatch",
                "Mechanical Gate 不属于当前工作区。",
            )
        try:
            review = await self._review_store.get_by_gate(gate.gate_id)
        except (EvolutionIndependentReviewError, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionStateError(
                "decision_state_review_read_failed",
                "无法读取 Decision State Independent Review authority。",
            ) from exc
        if review is None:
            raise EvolutionDecisionStateError(
                "decision_state_review_missing",
                "Mechanical Gate 尚无 Independent Review。",
            )
        counterfactual = None
        reward_hacking = None
        if gate.outcome == "pass":
            try:
                counterfactual = await self._counterfactual_store.get_by_review(review.review_id)
            except (
                EvolutionCounterfactualEvidenceError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                raise EvolutionDecisionStateError(
                    "decision_state_counterfactual_read_failed",
                    "无法读取 Decision State Counterfactual authority。",
                ) from exc
            if counterfactual is None:
                raise EvolutionDecisionStateError(
                    "decision_state_counterfactual_missing",
                    "通过的 Gate 尚无 Counterfactual Evidence。",
                )
            try:
                reward_hacking = await self._reward_hacking_store.get_by_counterfactual(
                    counterfactual.evidence_id
                )
            except (
                EvolutionRewardHackingEvidenceError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                raise EvolutionDecisionStateError(
                    "decision_state_reward_hacking_read_failed",
                    "无法读取 Decision State Reward-hacking authority。",
                ) from exc
            if reward_hacking is None:
                raise EvolutionDecisionStateError(
                    "decision_state_reward_hacking_missing",
                    "通过的 Gate 尚无 Reward-hacking Evidence。",
                )
        artifact = self._builder.build(
            gate=gate,
            review=review,
            counterfactual=counterfactual,
            reward_hacking=reward_hacking,
        )
        return await self._decision_store.record(artifact)


def resolve_evolution_decision_state(
    *,
    mechanical_gate_outcome: Literal["pass", "veto"] | str,
    counterfactual_outcome: Literal["clear", "concern"] | str | None,
    reward_hacking_outcome: Literal["clear", "concern", "inconclusive"] | str | None,
    risk_level: Literal["low", "medium", "high", "critical"] | str,
) -> tuple[EvolutionDecisionStateValue, tuple[EvolutionDecisionReason, ...]]:
    """Resolve a terminal state without any LLM-authored opinion."""

    if mechanical_gate_outcome not in {"pass", "veto"}:
        raise ValueError("Mechanical Gate outcome 无效。")
    if risk_level not in {"low", "medium", "high", "critical"}:
        raise ValueError("Decision risk level 无效。")
    if mechanical_gate_outcome == "veto":
        if counterfactual_outcome is not None or reward_hacking_outcome is not None:
            raise ValueError("Mechanical veto 路径不得携带下游 evidence outcome。")
        return (
            EvolutionDecisionStateValue.REJECTED,
            (EvolutionDecisionReason.MECHANICAL_VETO,),
        )
    if counterfactual_outcome not in {"clear", "concern"} or (
        reward_hacking_outcome not in {"clear", "concern", "inconclusive"}
    ):
        raise ValueError("Mechanical pass 路径缺少完整下游 evidence outcome。")
    concerns = []
    if counterfactual_outcome == "concern":
        concerns.append(EvolutionDecisionReason.COUNTERFACTUAL_CONCERN)
    if reward_hacking_outcome == "concern":
        concerns.append(EvolutionDecisionReason.REWARD_HACKING_CONCERN)
    if concerns:
        return EvolutionDecisionStateValue.REVISE, tuple(concerns)
    escalations = []
    if reward_hacking_outcome == "inconclusive":
        escalations.append(EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE)
    if risk_level in {"high", "critical"}:
        escalations.append(EvolutionDecisionReason.HIGH_RISK_REQUIRES_HUMAN)
    if escalations:
        return EvolutionDecisionStateValue.ESCALATED, tuple(escalations)
    return (
        EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT,
        (EvolutionDecisionReason.ALL_STRUCTURED_EVIDENCE_CLEAR,),
    )


def _state_flags(
    state: EvolutionDecisionStateValue,
) -> tuple[bool, bool, bool]:
    requires_user_input = state is EvolutionDecisionStateValue.ESCALATED
    acceptance_decided = not requires_user_input
    accepted = state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
    return requires_user_input, acceptance_decided, accepted


def render_evolution_decision_state(artifact: EvolutionDecisionState) -> str:
    item = EvolutionDecisionState.model_validate(artifact.model_dump(mode="json"))
    titles = {
        EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT: "实验已通过结构化决策",
        EvolutionDecisionStateValue.REVISE: "Candidate 需要修订",
        EvolutionDecisionStateValue.REJECTED: "Candidate 已被机械拒绝",
        EvolutionDecisionStateValue.ESCALATED: "需要用户决策",
    }
    lines = [
        f"# Evolution Decision State `{item.decision_id}`",
        "",
        f"**{titles[item.state]}；本回执不会执行 promotion。**",
        "",
        f"- State：`{item.state.value}`",
        f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
        f"- Risk：`{item.risk_level}`",
        f"- Mechanical Gate：`{item.mechanical_gate_id}` · `{item.gate.outcome}`",
        f"- Independent Review：`{item.independent_review_id}`（advisory only）",
        "- Reasons：" + ", ".join(f"`{reason.value}`" for reason in item.reasons),
        f"- Candidate accepted：{'是' if item.candidate_accepted else '否'}",
        f"- Promotion review ready：{'是' if item.promotion_review_ready else '否'}",
        "- Promotion executed：`false`",
    ]
    if item.escalation is not None:
        lines.extend(
            (
                "",
                f"## {item.escalation.header}",
                "",
                item.escalation.question,
                "",
            )
        )
        lines.extend(
            f"{index}. **{option.label}** — {option.description} (`{option.value}`)"
            for index, option in enumerate(item.escalation.options, start=1)
        )
        lines.append(f"4. **{item.escalation.custom_label}** — 输入自定义处理要求。")
    lines.extend(
        (
            "",
            f"- Decision SHA-256：`{item.decision_sha256}`",
            "",
            "下一步：accepted_experiment 进入受控 promotion review；revise/rejected 不得发布；"
            "escalated 必须由用户选择或输入自定义要求。",
        )
    )
    return "\n".join(lines)


def _validate_authority_chain(
    gate: EvolutionMechanicalGate,
    review: EvolutionIndependentReview,
    counterfactual: EvolutionCounterfactualEvidence | None,
    reward_hacking: EvolutionRewardHackingEvidence | None,
) -> None:
    if review.gate != gate or review.gate_id != gate.gate_id:
        raise ValueError("Decision State Review/Gate authority 不一致。")
    if gate.outcome == "veto":
        if review.status is not IndependentReviewStatus.BLOCKED_BY_MECHANICAL_VETO:
            raise ValueError("Mechanical veto 缺少 blocked Review authority。")
        if counterfactual is not None or reward_hacking is not None:
            raise ValueError("Mechanical veto 路径不得携带下游 Evidence。")
        return
    if review.status is not IndependentReviewStatus.COMPLETED:
        raise ValueError("Mechanical pass 缺少 completed Review authority。")
    if counterfactual is None or counterfactual.review != review:
        raise ValueError("Decision State Counterfactual/Review authority 不一致。")
    if reward_hacking is None or reward_hacking.counterfactual != counterfactual:
        raise ValueError("Decision State Reward-hacking/Counterfactual authority 不一致。")


def _build_checks(
    *,
    gate: EvolutionMechanicalGate,
    review: EvolutionIndependentReview,
    counterfactual: EvolutionCounterfactualEvidence | None,
    reward_hacking: EvolutionRewardHackingEvidence | None,
    state: EvolutionDecisionStateValue,
) -> tuple[EvolutionDecisionCheck, ...]:
    veto = gate.outcome == "veto"
    structured_clear = (
        counterfactual is not None
        and reward_hacking is not None
        and counterfactual.outcome == "clear"
        and reward_hacking.outcome == "clear"
    )
    risk_clear = gate.decision_input.risk_level in {"low", "medium"}
    facts = (
        (
            EvolutionDecisionRule.MECHANICAL_GATE_PRESERVED,
            EvolutionDecisionCheckStatus.BLOCKED if veto else EvolutionDecisionCheckStatus.PASS,
            (gate.gate_id,),
        ),
        (
            EvolutionDecisionRule.REVIEW_AUTHORITY_BOUND,
            EvolutionDecisionCheckStatus.PASS,
            (review.review_id,),
        ),
        (
            EvolutionDecisionRule.COUNTERFACTUAL_AUTHORITY_BOUND,
            EvolutionDecisionCheckStatus.NOT_APPLICABLE
            if veto
            else EvolutionDecisionCheckStatus.PASS,
            (gate.gate_id,) if veto else (counterfactual.evidence_id,),  # type: ignore[union-attr]
        ),
        (
            EvolutionDecisionRule.REWARD_HACKING_AUTHORITY_BOUND,
            EvolutionDecisionCheckStatus.NOT_APPLICABLE
            if veto
            else EvolutionDecisionCheckStatus.PASS,
            (gate.gate_id,) if veto else (reward_hacking.evidence_id,),  # type: ignore[union-attr]
        ),
        (
            EvolutionDecisionRule.STRUCTURED_EVIDENCE_CLEAR,
            EvolutionDecisionCheckStatus.NOT_APPLICABLE
            if veto
            else EvolutionDecisionCheckStatus.PASS
            if structured_clear
            else EvolutionDecisionCheckStatus.BLOCKED,
            (gate.gate_id,) if veto else (counterfactual.evidence_id, reward_hacking.evidence_id),  # type: ignore[union-attr]
        ),
        (
            EvolutionDecisionRule.RISK_POLICY_SATISFIED,
            EvolutionDecisionCheckStatus.NOT_APPLICABLE
            if veto
            else EvolutionDecisionCheckStatus.PASS
            if risk_clear
            else EvolutionDecisionCheckStatus.BLOCKED,
            (gate.decision_input.candidate.authority_id,),
        ),
        (
            EvolutionDecisionRule.REVIEWER_ADVISORY_NON_AUTHORITATIVE,
            EvolutionDecisionCheckStatus.PASS,
            (review.review_id, state.value),
        ),
    )
    return tuple(
        EvolutionDecisionCheck(
            order=index,
            rule=rule,
            status=status,
            evidence_refs=refs,
        )
        for index, (rule, status, refs) in enumerate(facts, start=1)
    )


def _build_escalation(
    reasons: tuple[EvolutionDecisionReason, ...],
) -> EvolutionDecisionEscalationRequest:
    incomplete = EvolutionDecisionReason.EVIDENCE_INCONCLUSIVE in reasons
    if incomplete:
        first = EvolutionDecisionEscalationOption(
            value="collect_missing_evidence",
            label="补齐证据（推荐）",
            description="补齐缺失平台或资源观测后，使用同一 policy 重新形成新证据链。",
        )
        question = "当前结构化证据不足以自动接受实验。请选择下一步，或输入自定义要求。"
    else:
        first = EvolutionDecisionEscalationOption(
            value="request_human_review",
            label="安排人工审查（推荐）",
            description="由用户审查高风险 tradeoff；不会自动执行 promotion。",
        )
        question = "当前 Candidate 风险等级要求人工决定。请选择下一步，或输入自定义要求。"
    return EvolutionDecisionEscalationRequest(
        header="Evolution 决策升级",
        question=question,
        options=(
            first,
            EvolutionDecisionEscalationOption(
                value="revise_candidate",
                label="修订 Candidate",
                description="返回修改阶段，保留本轮证据和不可变决策记录。",
            ),
            EvolutionDecisionEscalationOption(
                value="reject_candidate",
                label="拒绝 Candidate",
                description="终止本 Candidate；不修改 baseline 或工作区主分支。",
            ),
        ),
        allow_custom=True,
        custom_label="其他处理要求",
        timeout_seconds=None,
    )


def _clean_text(value: str) -> str:
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError("Decision escalation 文本含非法控制字符。")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ValueError("Decision escalation 文本不能为空。")
    return normalized


def _workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionDecisionStateError(
            "decision_state_workspace_missing",
            "Decision State 工作区不存在。",
        ) from exc
    if not path.is_dir():
        raise EvolutionDecisionStateError(
            "decision_state_workspace_missing",
            "Decision State 工作区不存在。",
        )
    return path


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_decision_states (
            decision_id TEXT PRIMARY KEY,
            decision_sha256 TEXT NOT NULL,
            decision_input_id TEXT NOT NULL UNIQUE,
            decision_input_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'accepted_experiment', 'revise', 'rejected', 'escalated'
            )),
            decision_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionDecisionState:
    encoded = str(row["decision_json"])
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Decision State Store artifact 过大。")
    item = EvolutionDecisionState.model_validate_json(encoded)
    if not (
        row["decision_id"] == item.decision_id
        and row["decision_sha256"] == item.decision_sha256
        and row["decision_input_id"] == item.decision_input_id
        and row["decision_input_sha256"] == item.decision_input_sha256
        and row["workspace_root"] == item.workspace_root
        and row["state"] == item.state.value
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Decision State Store index 不一致。")
    return item


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _json_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "EVOLUTION_DECISION_STATE_POLICY",
    "EvolutionDecisionCheck",
    "EvolutionDecisionCheckStatus",
    "EvolutionDecisionEscalationOption",
    "EvolutionDecisionEscalationRequest",
    "EvolutionDecisionReason",
    "EvolutionDecisionRule",
    "EvolutionDecisionState",
    "EvolutionDecisionStateBuilder",
    "EvolutionDecisionStateError",
    "EvolutionDecisionStateExecutor",
    "EvolutionDecisionStateStore",
    "EvolutionDecisionStateValue",
    "render_evolution_decision_state",
    "resolve_evolution_decision_state",
]
