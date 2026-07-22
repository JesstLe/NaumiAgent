"""Structured, non-injectable reflection memories for Evolution decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.decision_resolutions import (
    EvolutionDecisionResolution,
    EvolutionDecisionResolutionError,
    EvolutionDecisionResolutionOutcome,
    EvolutionDecisionResolutionStore,
)
from naumi_agent.evolution.decision_states import (
    EvolutionDecisionState,
    EvolutionDecisionStateError,
    EvolutionDecisionStateStore,
    EvolutionDecisionStateValue,
)

EVOLUTION_REFLECTION_MEMORY_POLICY = "evolution-reflection-memory-v1"
EVOLUTION_REFLECTION_REVOCATION_POLICY = "evolution-reflection-revocation-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_MEMORY_BYTES = 256 * 1_024


class EvolutionReflectionEvidenceKind(StrEnum):
    DECISION_INPUT = "decision_input"
    MECHANICAL_GATE = "mechanical_gate"
    INDEPENDENT_REVIEW = "independent_review"
    COUNTERFACTUAL = "counterfactual"
    REWARD_HACKING = "reward_hacking"
    DECISION_STATE = "decision_state"
    DECISION_RESOLUTION = "decision_resolution"


class EvolutionReflectionLessonKind(StrEnum):
    VALIDATED_EXPERIMENT = "validated_experiment"
    MECHANICAL_REJECTION = "mechanical_rejection"
    STRUCTURED_REVISION = "structured_revision"
    EVIDENCE_GAP = "evidence_gap"
    RISK_ESCALATION = "risk_escalation"
    USER_DIRECTED_REVISION = "user_directed_revision"
    USER_DIRECTED_REJECTION = "user_directed_rejection"
    CUSTOM_FOLLOW_UP = "custom_follow_up"


class EvolutionReflectionAction(StrEnum):
    REVIEW_FOR_PROMOTION = "review_for_promotion"
    REVISE_CANDIDATE = "revise_candidate"
    TERMINATE_CANDIDATE = "terminate_candidate"
    COLLECT_EVIDENCE = "collect_evidence"
    HUMAN_REVIEW = "human_review"
    CONSTRAINED_CUSTOM_FOLLOW_UP = "constrained_custom_follow_up"


class EvolutionReflectionSignal(StrEnum):
    ALL_STRUCTURED_EVIDENCE_CLEAR = "all_structured_evidence_clear"
    MECHANICAL_VETO = "mechanical_veto"
    COUNTERFACTUAL_CONCERN = "counterfactual_concern"
    REWARD_HACKING_CONCERN = "reward_hacking_concern"
    EVIDENCE_INCONCLUSIVE = "evidence_inconclusive"
    HIGH_RISK_REQUIRES_HUMAN = "high_risk_requires_human"
    USER_REQUESTED_EVIDENCE = "user_requested_evidence"
    USER_REQUESTED_HUMAN_REVIEW = "user_requested_human_review"
    USER_REQUESTED_REVISION = "user_requested_revision"
    USER_REJECTED_CANDIDATE = "user_rejected_candidate"
    USER_CUSTOM_FOLLOW_UP = "user_custom_follow_up"


class EvolutionReflectionRevocationReason(StrEnum):
    INCORRECT_EVIDENCE = "incorrect_evidence"
    SUPERSEDED = "superseded"
    PRIVACY = "privacy"
    USER_REQUEST = "user_request"
    POLICY_CHANGE = "policy_change"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionReflectionEvidenceRef(_StrictModel):
    kind: EvolutionReflectionEvidenceKind
    authority_id: str = Field(min_length=1, max_length=80)
    authority_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _authority_identity_matches_kind(self) -> Self:
        patterns = {
            EvolutionReflectionEvidenceKind.DECISION_INPUT: r"^evdin_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.MECHANICAL_GATE: r"^evgate_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.INDEPENDENT_REVIEW: r"^evreview_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.COUNTERFACTUAL: r"^evcounter_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.REWARD_HACKING: r"^evreward_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.DECISION_STATE: r"^evdecision_[0-9a-f]{24}$",
            EvolutionReflectionEvidenceKind.DECISION_RESOLUTION: r"^evresolution_[0-9a-f]{24}$",
        }
        if re.fullmatch(patterns[self.kind], self.authority_id) is None:
            raise ValueError("Reflection evidence authority ID 与 kind 不一致。")
        return self


class EvolutionReflectionMemory(_StrictModel):
    """Minimal structured lesson; never enters automatic model context."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-reflection-memory-v1"] = EVOLUTION_REFLECTION_MEMORY_POLICY
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    reflection_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    decision_state_id: str = Field(pattern=r"^evdecision_[0-9a-f]{24}$")
    decision_state_sha256: str = Field(pattern=_SHA256_RE)
    resolution_id: str | None = Field(
        default=None,
        pattern=r"^evresolution_[0-9a-f]{24}$",
    )
    resolution_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    decision_state: EvolutionDecisionStateValue
    resolution_outcome: EvolutionDecisionResolutionOutcome | None = None
    lesson_kind: EvolutionReflectionLessonKind
    required_action: EvolutionReflectionAction
    signals: tuple[EvolutionReflectionSignal, ...] = Field(min_length=1, max_length=8)
    evidence_refs: tuple[EvolutionReflectionEvidenceRef, ...] = Field(
        min_length=4,
        max_length=7,
    )
    changed_files: int = Field(ge=1, le=16)
    changed_lines: int = Field(ge=0, le=134_217_728)
    candidate_acceptance_decided: bool
    candidate_accepted: bool
    promotion_review_ready: bool
    promotion_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    eligible_for_policy_learning: bool
    vector_indexed: Literal[False] = False
    automatic_recall_allowed: Literal[False] = False
    system_prompt_injection_allowed: Literal[False] = False
    contains_freeform_narrative: Literal[False] = False
    contains_user_custom_text: Literal[False] = False
    llm_generated: Literal[False] = False
    revocable: Literal[True] = True
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _memory_is_exact_and_tamper_evident(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Reflection Memory workspace 必须是 canonical 绝对路径。")
        if any(char in self.workspace_root for char in ("\x00", "\r", "\n")):
            raise ValueError("Reflection Memory workspace 包含不安全字符。")
        _aware(self.created_at)
        if len({item.kind for item in self.evidence_refs}) != len(self.evidence_refs):
            raise ValueError("Reflection Memory evidence kind 不得重复。")
        if len(set(self.signals)) != len(self.signals):
            raise ValueError("Reflection Memory signal 不得重复。")
        if (self.resolution_id is None) != (self.resolution_sha256 is None):
            raise ValueError("Reflection Memory resolution ID/digest 必须同时存在。")
        if (self.resolution_id is None) != (self.resolution_outcome is None):
            raise ValueError("Reflection Memory resolution outcome 投影不一致。")
        lesson, action, decided, learning = _projection_for_values(
            self.decision_state,
            self.resolution_outcome,
        )
        accepted = self.decision_state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
        if not (
            self.lesson_kind is lesson
            and self.required_action is action
            and self.candidate_acceptance_decided is decided
            and self.candidate_accepted is accepted
            and self.promotion_review_ready is accepted
            and self.eligible_for_policy_learning is learning
        ):
            raise ValueError("Reflection Memory lesson、action 或 readiness 投影不一致。")
        _validate_signal_projection(
            state=self.decision_state,
            outcome=self.resolution_outcome,
            signals=self.signals,
        )
        _validate_evidence_projection(
            state=self.decision_state,
            evidence_refs=self.evidence_refs,
            decision_state_id=self.decision_state_id,
            decision_state_sha256=self.decision_state_sha256,
            resolution_id=self.resolution_id,
            resolution_sha256=self.resolution_sha256,
        )
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"reflection_id", "reflection_sha256"})
        )
        if not hmac.compare_digest(self.reflection_sha256, digest):
            raise ValueError("Reflection Memory 摘要不一致。")
        if self.reflection_id != f"evreflection_{digest[:24]}":
            raise ValueError("Reflection Memory identity 不一致。")
        return self


class EvolutionReflectionMemoryRevocation(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-reflection-revocation-v1"] = (
        EVOLUTION_REFLECTION_REVOCATION_POLICY
    )
    revocation_id: str = Field(pattern=r"^evreflectrevoke_[0-9a-f]{24}$")
    revocation_sha256: str = Field(pattern=_SHA256_RE)
    reflection_id: str = Field(pattern=r"^evreflection_[0-9a-f]{24}$")
    reflection_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    reason: EvolutionReflectionRevocationReason
    revoked_at: str = Field(min_length=1, max_length=100)
    append_only: Literal[True] = True

    @model_validator(mode="after")
    def _revocation_is_tamper_evident(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Reflection revocation workspace 无效。")
        _aware(self.revoked_at)
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"revocation_id", "revocation_sha256"})
        )
        if not hmac.compare_digest(self.revocation_sha256, digest):
            raise ValueError("Reflection revocation 摘要不一致。")
        if self.revocation_id != f"evreflectrevoke_{digest[:24]}":
            raise ValueError("Reflection revocation identity 不一致。")
        return self


class EvolutionReflectionMemoryView(_StrictModel):
    memory: EvolutionReflectionMemory
    revocation: EvolutionReflectionMemoryRevocation | None = None
    active: bool

    @model_validator(mode="after")
    def _status_is_exact(self) -> Self:
        if self.active is (self.revocation is not None):
            raise ValueError("Reflection Memory active/revocation 状态不一致。")
        if self.revocation is not None and not (
            self.revocation.reflection_id == self.memory.reflection_id
            and self.revocation.reflection_sha256 == self.memory.reflection_sha256
            and self.revocation.workspace_root == self.memory.workspace_root
        ):
            raise ValueError("Reflection Memory revocation authority 不一致。")
        return self


class EvolutionReflectionMemoryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionReflectionMemoryBuilder:
    def build(
        self,
        *,
        decision: EvolutionDecisionState,
        resolution: EvolutionDecisionResolution | None,
    ) -> EvolutionReflectionMemory:
        try:
            decision = EvolutionDecisionState.model_validate(decision.model_dump(mode="json"))
            resolution = (
                EvolutionDecisionResolution.model_validate_json(resolution.model_dump_json())
                if resolution is not None
                else None
            )
            _validate_resolution(decision, resolution)
            lesson, action, decided, learning = _lesson_projection(decision, resolution)
            signals = _signals(decision, resolution)
            refs = _evidence_refs(decision, resolution)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_authority_invalid",
                "Decision/Resolution authority 无效、不完整或已被篡改。",
            ) from exc
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REFLECTION_MEMORY_POLICY,
            "workspace_root": decision.workspace_root,
            "decision_input_id": decision.decision_input_id,
            "decision_state_id": decision.decision_id,
            "decision_state_sha256": decision.decision_sha256,
            "resolution_id": resolution.resolution_id if resolution else None,
            "resolution_sha256": resolution.resolution_sha256 if resolution else None,
            "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision,
            "risk_level": decision.risk_level,
            "decision_state": decision.state.value,
            "resolution_outcome": resolution.outcome.value if resolution else None,
            "lesson_kind": lesson.value,
            "required_action": action.value,
            "signals": [item.value for item in signals],
            "evidence_refs": [item.model_dump(mode="json") for item in refs],
            "changed_files": decision.gate.changed_files,
            "changed_lines": decision.gate.changed_lines,
            "candidate_acceptance_decided": decided,
            "candidate_accepted": (
                decision.state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
            ),
            "promotion_review_ready": (
                decision.state is EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT
            ),
            "promotion_executed": False,
            "promotion_authority": False,
            "eligible_for_policy_learning": learning,
            "vector_indexed": False,
            "automatic_recall_allowed": False,
            "system_prompt_injection_allowed": False,
            "contains_freeform_narrative": False,
            "contains_user_custom_text": False,
            "llm_generated": False,
            "revocable": True,
            "created_at": resolution.created_at if resolution else decision.created_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionReflectionMemory.model_validate(
                {
                    **payload,
                    "reflection_id": f"evreflection_{digest[:24]}",
                    "reflection_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_artifact_invalid",
                "Reflection Memory artifact 无法验证。",
            ) from exc


class EvolutionReflectionMemoryStore:
    """Append-only memory plus optional append-only revocation authority."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, memory: EvolutionReflectionMemory) -> EvolutionReflectionMemoryView:
        try:
            item = EvolutionReflectionMemory.model_validate_json(memory.model_dump_json())
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_artifact_invalid",
                "Reflection Memory 无效或已被篡改。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_MEMORY_BYTES:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_oversized",
                "Reflection Memory 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_reflection_memories WHERE decision_state_id = ?",
                        (item.decision_state_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _memory_from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionReflectionMemoryError(
                            "reflection_memory_conflict",
                            "同一 Decision State 不可覆盖为不同 Reflection Memory。",
                        )
                    await db.rollback()
                    view = await self.get(item.reflection_id)
                    assert view is not None
                    return view
                await db.execute(
                    "INSERT INTO evolution_reflection_memories "
                    "(reflection_id, reflection_sha256, decision_state_id, "
                    "decision_input_id, workspace_root, lesson_kind, memory_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.reflection_id,
                        item.reflection_sha256,
                        item.decision_state_id,
                        item.decision_input_id,
                        item.workspace_root,
                        item.lesson_kind.value,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionReflectionMemoryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_store_error",
                "Reflection Memory 无法持久化。",
            ) from exc
        view = await self.get(item.reflection_id)
        assert view is not None
        return view

    async def get(self, reflection_id: str) -> EvolutionReflectionMemoryView | None:
        if (
            not isinstance(reflection_id, str)
            or re.fullmatch(r"evreflection_[0-9a-f]{24}", reflection_id) is None
        ):
            raise ValueError("reflection_id 格式无效。")
        return await self._read("reflection_id", reflection_id)

    async def get_by_decision_state(
        self,
        decision_state_id: str,
    ) -> EvolutionReflectionMemoryView | None:
        if (
            not isinstance(decision_state_id, str)
            or re.fullmatch(r"evdecision_[0-9a-f]{24}", decision_state_id) is None
        ):
            raise ValueError("decision_state_id 格式无效。")
        return await self._read("decision_state_id", decision_state_id)

    async def revoke(
        self,
        *,
        memory: EvolutionReflectionMemory,
        reason: EvolutionReflectionRevocationReason,
        revoked_at: str,
    ) -> EvolutionReflectionMemoryView:
        current = await self.get(memory.reflection_id)
        if current is None:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_missing",
                "Reflection Memory 不存在。",
            )
        if current.memory != memory:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_conflict",
                "Reflection Memory authority 已变化。",
            )
        if current.revocation is not None:
            return current
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REFLECTION_REVOCATION_POLICY,
            "reflection_id": memory.reflection_id,
            "reflection_sha256": memory.reflection_sha256,
            "workspace_root": memory.workspace_root,
            "reason": reason.value,
            "revoked_at": _aware(revoked_at).isoformat(),
            "append_only": True,
        }
        digest = _sha256_payload(payload)
        revocation = EvolutionReflectionMemoryRevocation.model_validate(
            {
                **payload,
                "revocation_id": f"evreflectrevoke_{digest[:24]}",
                "revocation_sha256": digest,
            }
        )
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_reflection_revocations "
                        "WHERE reflection_id = ?",
                        (memory.reflection_id,),
                    )
                ).fetchone()
                if row is not None:
                    await db.rollback()
                    existing = _revocation_from_row(row)
                    return EvolutionReflectionMemoryView(
                        memory=memory,
                        revocation=existing,
                        active=False,
                    )
                await db.execute(
                    "INSERT INTO evolution_reflection_revocations "
                    "(revocation_id, revocation_sha256, reflection_id, "
                    "reflection_sha256, workspace_root, reason, revocation_json, "
                    "revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revocation.revocation_id,
                        revocation.revocation_sha256,
                        revocation.reflection_id,
                        revocation.reflection_sha256,
                        revocation.workspace_root,
                        revocation.reason.value,
                        revocation.model_dump_json(),
                        revocation.revoked_at,
                    ),
                )
                await db.commit()
        except EvolutionReflectionMemoryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_revocation_store_error",
                "Reflection Memory 撤销无法持久化。",
            ) from exc
        view = await self.get(memory.reflection_id)
        assert view is not None and not view.active
        return view

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionReflectionMemoryView | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_reflection_memories WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                if row is None:
                    return None
                memory = _memory_from_row(row)
                revoked = await (
                    await db.execute(
                        "SELECT * FROM evolution_reflection_revocations WHERE reflection_id = ?",
                        (memory.reflection_id,),
                    )
                ).fetchone()
                revocation = _revocation_from_row(revoked) if revoked is not None else None
                return EvolutionReflectionMemoryView(
                    memory=memory,
                    revocation=revocation,
                    active=revocation is None,
                )
        except EvolutionReflectionMemoryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_store_corrupt",
                "Reflection Memory 损坏或无法读取。",
            ) from exc


class EvolutionReflectionMemoryExecutor:
    def __init__(
        self,
        *,
        decision_store: EvolutionDecisionStateStore,
        resolution_store: EvolutionDecisionResolutionStore,
        memory_store: EvolutionReflectionMemoryStore,
        builder: EvolutionReflectionMemoryBuilder | None = None,
    ) -> None:
        self._decision_store = decision_store
        self._resolution_store = resolution_store
        self._memory_store = memory_store
        self._builder = builder or EvolutionReflectionMemoryBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        decision_input_id: str,
    ) -> EvolutionReflectionMemoryView:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(decision_input_id, str)
            or re.fullmatch(r"evdin_[0-9a-f]{24}", decision_input_id) is None
        ):
            raise EvolutionReflectionMemoryError(
                "reflection_memory_input_id_invalid",
                "Decision Input ID 格式无效。",
            )
        try:
            decision = await self._decision_store.get_by_decision_input(decision_input_id)
        except (EvolutionDecisionStateError, OSError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_decision_read_failed",
                "无法读取 Reflection Memory Decision authority。",
            ) from exc
        if decision is None:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_decision_missing",
                "Decision Input 尚无 Decision State。",
            )
        if decision.workspace_root != str(workspace):
            raise EvolutionReflectionMemoryError(
                "reflection_memory_workspace_mismatch",
                "Decision State 不属于当前工作区。",
            )
        try:
            resolution = await self._resolution_store.get_by_decision_state(decision.decision_id)
        except (EvolutionDecisionResolutionError, OSError, TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_resolution_read_failed",
                "无法读取 Reflection Memory Resolution authority。",
            ) from exc
        if decision.state is EvolutionDecisionStateValue.ESCALATED and resolution is None:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_resolution_missing",
                "escalated Decision State 尚无用户 Resolution。",
            )
        if decision.state is not EvolutionDecisionStateValue.ESCALATED and resolution is not None:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_resolution_unexpected",
                "非 escalated Decision State 不得绑定 Resolution。",
            )
        memory = self._builder.build(decision=decision, resolution=resolution)
        return await self._memory_store.record(memory)


class EvolutionReflectionMemoryRevoker:
    def __init__(
        self,
        *,
        store: EvolutionReflectionMemoryStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        reflection_id: str,
        reason: EvolutionReflectionRevocationReason | str,
    ) -> EvolutionReflectionMemoryView:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(reflection_id, str)
            or re.fullmatch(r"evreflection_[0-9a-f]{24}", reflection_id) is None
        ):
            raise EvolutionReflectionMemoryError(
                "reflection_revocation_id_invalid",
                "Reflection Memory ID 格式无效。",
            )
        try:
            reason_value = EvolutionReflectionRevocationReason(reason)
        except (TypeError, ValueError) as exc:
            raise EvolutionReflectionMemoryError(
                "reflection_revocation_reason_invalid",
                "撤销原因必须是预定义结构化 reason。",
            ) from exc
        view = await self._store.get(reflection_id)
        if view is None:
            raise EvolutionReflectionMemoryError(
                "reflection_memory_missing",
                "Reflection Memory 不存在。",
            )
        if view.memory.workspace_root != str(workspace):
            raise EvolutionReflectionMemoryError(
                "reflection_revocation_workspace_mismatch",
                "Reflection Memory 不属于当前工作区。",
            )
        return await self._store.revoke(
            memory=view.memory,
            reason=reason_value,
            revoked_at=_aware(self._clock()).isoformat(),
        )


def render_evolution_reflection_memory(view: EvolutionReflectionMemoryView) -> str:
    item = EvolutionReflectionMemoryView.model_validate(view.model_dump(mode="python"))
    memory = item.memory
    lines = [
        f"# Evolution Reflection Memory `{memory.reflection_id}`",
        "",
        "**仅保存结构化经验与证据引用；不会进入向量记忆或系统 Prompt。**",
        "",
        f"- 状态：`{'active' if item.active else 'revoked'}`",
        f"- Lesson：`{memory.lesson_kind.value}`",
        f"- Required action：`{memory.required_action.value}`",
        f"- Decision：`{memory.decision_state.value}`",
        "- Signals：" + ", ".join(f"`{signal.value}`" for signal in memory.signals),
        f"- Evidence refs：{len(memory.evidence_refs)}",
        f"- Candidate accepted：{'是' if memory.candidate_accepted else '否'}",
        f"- Promotion review ready：{'是' if memory.promotion_review_ready else '否'}",
        "- Promotion authority：`false`",
        "- Automatic recall：`false`",
        f"- Reflection SHA-256：`{memory.reflection_sha256}`",
    ]
    if item.revocation is not None:
        lines.extend(
            (
                "",
                f"- Revocation：`{item.revocation.revocation_id}`",
                f"- Reason：`{item.revocation.reason.value}`",
            )
        )
    lines.extend(
        (
            "",
            "下一步：active 记录可供后续显式 policy review；"
            "revoked 记录只保留审计，不参与学习或 promotion。",
        )
    )
    return "\n".join(lines)


def _validate_resolution(
    decision: EvolutionDecisionState,
    resolution: EvolutionDecisionResolution | None,
) -> None:
    escalated = decision.state is EvolutionDecisionStateValue.ESCALATED
    if escalated != (resolution is not None):
        raise ValueError("Reflection Memory Decision/Resolution 分支不一致。")
    if resolution is not None and not (
        resolution.decision_state_id == decision.decision_id
        and resolution.decision_state_sha256 == decision.decision_sha256
        and resolution.workspace_root == decision.workspace_root
        and resolution.decision == decision
    ):
        raise ValueError("Reflection Memory Resolution 未绑定 exact Decision。")


def _lesson_projection(
    decision: EvolutionDecisionState,
    resolution: EvolutionDecisionResolution | None,
) -> tuple[EvolutionReflectionLessonKind, EvolutionReflectionAction, bool, bool]:
    return _projection_for_values(
        decision.state,
        resolution.outcome if resolution is not None else None,
    )


def _projection_for_values(
    state: EvolutionDecisionStateValue,
    outcome: EvolutionDecisionResolutionOutcome | None,
) -> tuple[EvolutionReflectionLessonKind, EvolutionReflectionAction, bool, bool]:
    direct = {
        EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT: (
            EvolutionReflectionLessonKind.VALIDATED_EXPERIMENT,
            EvolutionReflectionAction.REVIEW_FOR_PROMOTION,
            True,
            True,
        ),
        EvolutionDecisionStateValue.REVISE: (
            EvolutionReflectionLessonKind.STRUCTURED_REVISION,
            EvolutionReflectionAction.REVISE_CANDIDATE,
            True,
            True,
        ),
        EvolutionDecisionStateValue.REJECTED: (
            EvolutionReflectionLessonKind.MECHANICAL_REJECTION,
            EvolutionReflectionAction.TERMINATE_CANDIDATE,
            True,
            True,
        ),
    }
    if state in direct:
        if outcome is not None:
            raise ValueError("非 escalated Reflection 不得携带 Resolution outcome。")
        return direct[state]
    if state is not EvolutionDecisionStateValue.ESCALATED or outcome is None:
        raise ValueError("escalated Reflection 缺少 Resolution outcome。")
    mapped = {
        EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED: (
            EvolutionReflectionLessonKind.EVIDENCE_GAP,
            EvolutionReflectionAction.COLLECT_EVIDENCE,
            False,
            True,
        ),
        EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED: (
            EvolutionReflectionLessonKind.RISK_ESCALATION,
            EvolutionReflectionAction.HUMAN_REVIEW,
            False,
            True,
        ),
        EvolutionDecisionResolutionOutcome.REVISE: (
            EvolutionReflectionLessonKind.USER_DIRECTED_REVISION,
            EvolutionReflectionAction.REVISE_CANDIDATE,
            True,
            True,
        ),
        EvolutionDecisionResolutionOutcome.REJECTED: (
            EvolutionReflectionLessonKind.USER_DIRECTED_REJECTION,
            EvolutionReflectionAction.TERMINATE_CANDIDATE,
            True,
            True,
        ),
        EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP: (
            EvolutionReflectionLessonKind.CUSTOM_FOLLOW_UP,
            EvolutionReflectionAction.CONSTRAINED_CUSTOM_FOLLOW_UP,
            False,
            False,
        ),
    }
    return mapped[outcome]


def _validate_signal_projection(
    *,
    state: EvolutionDecisionStateValue,
    outcome: EvolutionDecisionResolutionOutcome | None,
    signals: tuple[EvolutionReflectionSignal, ...],
) -> None:
    direct = {
        EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT: (
            EvolutionReflectionSignal.ALL_STRUCTURED_EVIDENCE_CLEAR,
        ),
        EvolutionDecisionStateValue.REJECTED: (
            EvolutionReflectionSignal.MECHANICAL_VETO,
        ),
    }
    if state in direct:
        if signals != direct[state]:
            raise ValueError("Reflection Memory direct-state signal 投影不一致。")
        return
    if state is EvolutionDecisionStateValue.REVISE:
        allowed = {
            (EvolutionReflectionSignal.COUNTERFACTUAL_CONCERN,),
            (EvolutionReflectionSignal.REWARD_HACKING_CONCERN,),
            (
                EvolutionReflectionSignal.COUNTERFACTUAL_CONCERN,
                EvolutionReflectionSignal.REWARD_HACKING_CONCERN,
            ),
        }
        if signals not in allowed:
            raise ValueError("Reflection Memory revise signal 投影不一致。")
        return
    if state is not EvolutionDecisionStateValue.ESCALATED or outcome is None:
        raise ValueError("Reflection Memory escalation signal 缺少 outcome。")
    user_signal = {
        EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED: (
            EvolutionReflectionSignal.USER_REQUESTED_EVIDENCE
        ),
        EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED: (
            EvolutionReflectionSignal.USER_REQUESTED_HUMAN_REVIEW
        ),
        EvolutionDecisionResolutionOutcome.REVISE: (
            EvolutionReflectionSignal.USER_REQUESTED_REVISION
        ),
        EvolutionDecisionResolutionOutcome.REJECTED: (
            EvolutionReflectionSignal.USER_REJECTED_CANDIDATE
        ),
        EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP: (
            EvolutionReflectionSignal.USER_CUSTOM_FOLLOW_UP
        ),
    }[outcome]
    base = signals[:-1]
    allowed_base = {
        (EvolutionReflectionSignal.EVIDENCE_INCONCLUSIVE,),
        (EvolutionReflectionSignal.HIGH_RISK_REQUIRES_HUMAN,),
        (
            EvolutionReflectionSignal.EVIDENCE_INCONCLUSIVE,
            EvolutionReflectionSignal.HIGH_RISK_REQUIRES_HUMAN,
        ),
    }
    if base not in allowed_base or signals[-1] is not user_signal:
        raise ValueError("Reflection Memory escalation signal 投影不一致。")


def _validate_evidence_projection(
    *,
    state: EvolutionDecisionStateValue,
    evidence_refs: tuple[EvolutionReflectionEvidenceRef, ...],
    decision_state_id: str,
    decision_state_sha256: str,
    resolution_id: str | None,
    resolution_sha256: str | None,
) -> None:
    required = [
        EvolutionReflectionEvidenceKind.DECISION_INPUT,
        EvolutionReflectionEvidenceKind.MECHANICAL_GATE,
        EvolutionReflectionEvidenceKind.INDEPENDENT_REVIEW,
        EvolutionReflectionEvidenceKind.DECISION_STATE,
    ]
    if state is not EvolutionDecisionStateValue.REJECTED:
        required.extend(
            [
                EvolutionReflectionEvidenceKind.COUNTERFACTUAL,
                EvolutionReflectionEvidenceKind.REWARD_HACKING,
            ]
        )
    if state is EvolutionDecisionStateValue.ESCALATED:
        required.append(EvolutionReflectionEvidenceKind.DECISION_RESOLUTION)
    by_kind = {item.kind: item for item in evidence_refs}
    if tuple(by_kind) != tuple(required):
        raise ValueError("Reflection Memory evidence kind 投影不一致。")
    decision_ref = by_kind[EvolutionReflectionEvidenceKind.DECISION_STATE]
    if not (
        decision_ref.authority_id == decision_state_id
        and decision_ref.authority_sha256 == decision_state_sha256
    ):
        raise ValueError("Reflection Memory Decision State evidence ref 不一致。")
    resolution_ref = by_kind.get(EvolutionReflectionEvidenceKind.DECISION_RESOLUTION)
    if resolution_ref is not None and not (
        resolution_ref.authority_id == resolution_id
        and resolution_ref.authority_sha256 == resolution_sha256
    ):
        raise ValueError("Reflection Memory Resolution evidence ref 不一致。")


def _signals(
    decision: EvolutionDecisionState,
    resolution: EvolutionDecisionResolution | None,
) -> tuple[EvolutionReflectionSignal, ...]:
    values = [EvolutionReflectionSignal(reason.value) for reason in decision.reasons]
    if resolution is not None:
        values.append(
            {
                EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED: (
                    EvolutionReflectionSignal.USER_REQUESTED_EVIDENCE
                ),
                EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED: (
                    EvolutionReflectionSignal.USER_REQUESTED_HUMAN_REVIEW
                ),
                EvolutionDecisionResolutionOutcome.REVISE: (
                    EvolutionReflectionSignal.USER_REQUESTED_REVISION
                ),
                EvolutionDecisionResolutionOutcome.REJECTED: (
                    EvolutionReflectionSignal.USER_REJECTED_CANDIDATE
                ),
                EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP: (
                    EvolutionReflectionSignal.USER_CUSTOM_FOLLOW_UP
                ),
            }[resolution.outcome]
        )
    return tuple(values)


def _evidence_refs(
    decision: EvolutionDecisionState,
    resolution: EvolutionDecisionResolution | None,
) -> tuple[EvolutionReflectionEvidenceRef, ...]:
    refs = [
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.DECISION_INPUT,
            authority_id=decision.decision_input_id,
            authority_sha256=decision.decision_input_sha256,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.MECHANICAL_GATE,
            authority_id=decision.mechanical_gate_id,
            authority_sha256=decision.mechanical_gate_sha256,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.INDEPENDENT_REVIEW,
            authority_id=decision.independent_review_id,
            authority_sha256=decision.independent_review_sha256,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.DECISION_STATE,
            authority_id=decision.decision_id,
            authority_sha256=decision.decision_sha256,
        ),
    ]
    if decision.counterfactual is not None:
        refs.append(
            EvolutionReflectionEvidenceRef(
                kind=EvolutionReflectionEvidenceKind.COUNTERFACTUAL,
                authority_id=decision.counterfactual.evidence_id,
                authority_sha256=decision.counterfactual.evidence_sha256,
            )
        )
    if decision.reward_hacking is not None:
        refs.append(
            EvolutionReflectionEvidenceRef(
                kind=EvolutionReflectionEvidenceKind.REWARD_HACKING,
                authority_id=decision.reward_hacking.evidence_id,
                authority_sha256=decision.reward_hacking.evidence_sha256,
            )
        )
    if resolution is not None:
        refs.append(
            EvolutionReflectionEvidenceRef(
                kind=EvolutionReflectionEvidenceKind.DECISION_RESOLUTION,
                authority_id=resolution.resolution_id,
                authority_sha256=resolution.resolution_sha256,
            )
        )
    return tuple(refs)


def _workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionReflectionMemoryError(
            "reflection_memory_workspace_missing",
            "Reflection Memory 工作区不存在。",
        ) from exc
    if not path.is_dir():
        raise EvolutionReflectionMemoryError(
            "reflection_memory_workspace_missing",
            "Reflection Memory 工作区不存在。",
        )
    return path


def _aware(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Reflection timestamp 必须包含时区。")
    return parsed


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_reflection_memories (
            reflection_id TEXT PRIMARY KEY,
            reflection_sha256 TEXT NOT NULL,
            decision_state_id TEXT NOT NULL UNIQUE,
            decision_input_id TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            lesson_kind TEXT NOT NULL,
            memory_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_reflection_revocations (
            revocation_id TEXT PRIMARY KEY,
            revocation_sha256 TEXT NOT NULL,
            reflection_id TEXT NOT NULL UNIQUE,
            reflection_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            reason TEXT NOT NULL,
            revocation_json TEXT NOT NULL,
            revoked_at TEXT NOT NULL
        )"""
    )


def _memory_from_row(row: aiosqlite.Row) -> EvolutionReflectionMemory:
    encoded = str(row["memory_json"])
    if len(encoded.encode("utf-8")) > _MAX_MEMORY_BYTES:
        raise ValueError("Reflection Memory Store artifact 过大。")
    item = EvolutionReflectionMemory.model_validate_json(encoded)
    if not (
        row["reflection_id"] == item.reflection_id
        and row["reflection_sha256"] == item.reflection_sha256
        and row["decision_state_id"] == item.decision_state_id
        and row["decision_input_id"] == item.decision_input_id
        and row["workspace_root"] == item.workspace_root
        and row["lesson_kind"] == item.lesson_kind.value
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Reflection Memory Store index 不一致。")
    return item


def _revocation_from_row(row: aiosqlite.Row) -> EvolutionReflectionMemoryRevocation:
    item = EvolutionReflectionMemoryRevocation.model_validate_json(str(row["revocation_json"]))
    if not (
        row["revocation_id"] == item.revocation_id
        and row["revocation_sha256"] == item.revocation_sha256
        and row["reflection_id"] == item.reflection_id
        and row["reflection_sha256"] == item.reflection_sha256
        and row["workspace_root"] == item.workspace_root
        and row["reason"] == item.reason.value
        and row["revoked_at"] == item.revoked_at
    ):
        raise ValueError("Reflection revocation Store index 不一致。")
    return item


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EVOLUTION_REFLECTION_MEMORY_POLICY",
    "EVOLUTION_REFLECTION_REVOCATION_POLICY",
    "EvolutionReflectionAction",
    "EvolutionReflectionEvidenceKind",
    "EvolutionReflectionEvidenceRef",
    "EvolutionReflectionLessonKind",
    "EvolutionReflectionMemory",
    "EvolutionReflectionMemoryBuilder",
    "EvolutionReflectionMemoryError",
    "EvolutionReflectionMemoryExecutor",
    "EvolutionReflectionMemoryRevocation",
    "EvolutionReflectionMemoryRevoker",
    "EvolutionReflectionMemoryStore",
    "EvolutionReflectionMemoryView",
    "EvolutionReflectionRevocationReason",
    "EvolutionReflectionSignal",
    "render_evolution_reflection_memory",
]
