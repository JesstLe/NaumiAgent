"""Durable user resolutions for escalated Evolution decision states."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.decision_states import (
    EvolutionDecisionState,
    EvolutionDecisionStateError,
    EvolutionDecisionStateStore,
    EvolutionDecisionStateValue,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import UserInteractionUnavailableError

EVOLUTION_DECISION_RESOLUTION_POLICY = "evolution-decision-resolution-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 40 * 1_024 * 1_024
_INTERACTION_PREFIX_RE = re.compile(r"^ask-evolution-([0-9a-f]{24})-([1-9][0-9]{0,3})$")

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EvolutionDecisionResolutionAction(StrEnum):
    COLLECT_MISSING_EVIDENCE = "collect_missing_evidence"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    REVISE_CANDIDATE = "revise_candidate"
    REJECT_CANDIDATE = "reject_candidate"
    CUSTOM_INSTRUCTION = "custom_instruction"


class EvolutionDecisionResolutionOutcome(StrEnum):
    EVIDENCE_REQUIRED = "evidence_required"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    REVISE = "revise"
    REJECTED = "rejected"
    CUSTOM_FOLLOW_UP = "custom_follow_up"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionDecisionResolution(_StrictModel):
    """Immutable answer authority; it never accepts or promotes a candidate."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-decision-resolution-v1"] = (
        EVOLUTION_DECISION_RESOLUTION_POLICY
    )
    resolution_id: str = Field(pattern=r"^evresolution_[0-9a-f]{24}$")
    resolution_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    decision_state_id: str = Field(pattern=r"^evdecision_[0-9a-f]{24}$")
    decision_state_sha256: str = Field(pattern=_SHA256_RE)
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    interaction_id: str = Field(pattern=r"^ask-evolution-[0-9a-f]{24}-[1-9][0-9]{0,3}$")
    interaction_sha256: str = Field(pattern=_SHA256_RE)
    interaction_sequence: int = Field(ge=2)
    answer_kind: Literal["option", "custom"]
    answer_value: str = Field(max_length=80)
    answer_label: str = Field(min_length=1, max_length=80)
    custom_text: str = Field(max_length=4_000)
    action: EvolutionDecisionResolutionAction
    outcome: EvolutionDecisionResolutionOutcome
    requires_follow_up: bool
    candidate_acceptance_decided: bool
    candidate_accepted: Literal[False] = False
    experiment_accepted: Literal[False] = False
    promotion_review_ready: Literal[False] = False
    promotion_executed: Literal[False] = False
    user_authority_required: Literal[True] = True
    llm_decision_authority: Literal[False] = False
    decision: EvolutionDecisionState
    interaction: HarnessInteractionRecord
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _resolution_is_exact_and_tamper_evident(self) -> Self:
        action, outcome, follow_up, acceptance_decided = resolve_escalation_answer(
            self.decision,
            self.interaction,
        )
        if not (
            self.workspace_root == self.decision.workspace_root
            and self.decision_state_id == self.decision.decision_id
            and self.decision_state_sha256 == self.decision.decision_sha256
            and self.decision_input_id == self.decision.decision_input_id
            and self.candidate_id == self.decision.candidate_id
            and self.candidate_revision == self.decision.candidate_revision
            and self.interaction_id == self.interaction.interaction_id
            and self.interaction_sha256 == self.interaction.digest()
            and self.interaction_sequence == self.interaction.sequence
            and self.answer_kind == self.interaction.answer_kind
            and self.answer_value == self.interaction.answer_value
            and self.answer_label == self.interaction.answer_label
            and self.custom_text == self.interaction.custom_text
            and self.action is action
            and self.outcome is outcome
            and self.requires_follow_up is follow_up
            and self.candidate_acceptance_decided is acceptance_decided
            and self.created_at == self.interaction.answered_at
        ):
            raise ValueError("Escalation Resolution authority 或状态投影不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"resolution_id", "resolution_sha256"})
        )
        if not hmac.compare_digest(self.resolution_sha256, digest):
            raise ValueError("Escalation Resolution 摘要不一致。")
        if self.resolution_id != f"evresolution_{digest[:24]}":
            raise ValueError("Escalation Resolution identity 不一致。")
        return self


class EvolutionDecisionResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionDecisionResolutionBuilder:
    def build(
        self,
        *,
        decision: EvolutionDecisionState,
        interaction: HarnessInteractionRecord,
    ) -> EvolutionDecisionResolution:
        try:
            decision = EvolutionDecisionState.model_validate(decision.model_dump(mode="json"))
            interaction = HarnessInteractionRecord.model_validate_json(
                interaction.model_dump_json()
            )
            action, outcome, follow_up, acceptance_decided = resolve_escalation_answer(
                decision, interaction
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_authority_invalid",
                "Decision State 或用户交互 authority 无效、不完整或已被篡改。",
            ) from exc
        payload: dict[str, Any] = {
            "schema_version": 1,
            "policy_version": EVOLUTION_DECISION_RESOLUTION_POLICY,
            "workspace_root": decision.workspace_root,
            "decision_state_id": decision.decision_id,
            "decision_state_sha256": decision.decision_sha256,
            "decision_input_id": decision.decision_input_id,
            "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision,
            "interaction_id": interaction.interaction_id,
            "interaction_sha256": interaction.digest(),
            "interaction_sequence": interaction.sequence,
            "answer_kind": interaction.answer_kind,
            "answer_value": interaction.answer_value,
            "answer_label": interaction.answer_label,
            "custom_text": interaction.custom_text,
            "action": action.value,
            "outcome": outcome.value,
            "requires_follow_up": follow_up,
            "candidate_acceptance_decided": acceptance_decided,
            "candidate_accepted": False,
            "experiment_accepted": False,
            "promotion_review_ready": False,
            "promotion_executed": False,
            "user_authority_required": True,
            "llm_decision_authority": False,
            "decision": decision,
            "interaction": interaction,
            "created_at": interaction.answered_at,
        }
        digest_payload = {
            **payload,
            "decision": decision.model_dump(mode="json"),
            "interaction": interaction.model_dump(mode="json"),
        }
        digest = _sha256_payload(digest_payload)
        try:
            return EvolutionDecisionResolution.model_validate(
                {
                    **payload,
                    "resolution_id": f"evresolution_{digest[:24]}",
                    "resolution_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_artifact_invalid",
                "Escalation Resolution artifact 无法验证。",
            ) from exc


class EvolutionDecisionResolutionStore:
    """Immutable one-resolution-per-escalated-decision storage."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        artifact: EvolutionDecisionResolution,
    ) -> EvolutionDecisionResolution:
        try:
            item = EvolutionDecisionResolution.model_validate_json(artifact.model_dump_json())
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_artifact_invalid",
                "Escalation Resolution 无效或已被篡改。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_artifact_oversized",
                "Escalation Resolution 超过 40 MiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_decision_resolutions WHERE decision_state_id = ?",
                        (item.decision_state_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _from_row(row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionDecisionResolutionError(
                            "decision_resolution_conflict",
                            "同一 Decision State 不可绑定不同用户 Resolution。",
                        )
                    await db.rollback()
                    return existing
                await db.execute(
                    "INSERT INTO evolution_decision_resolutions "
                    "(resolution_id, resolution_sha256, decision_state_id, "
                    "decision_state_sha256, interaction_id, interaction_sha256, "
                    "workspace_root, outcome, resolution_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.resolution_id,
                        item.resolution_sha256,
                        item.decision_state_id,
                        item.decision_state_sha256,
                        item.interaction_id,
                        item.interaction_sha256,
                        item.workspace_root,
                        item.outcome.value,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionDecisionResolutionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_store_error",
                "Escalation Resolution 无法持久化。",
            ) from exc
        restored = await self.get(item.resolution_id)
        assert restored is not None
        return restored

    async def get(
        self,
        resolution_id: str,
    ) -> EvolutionDecisionResolution | None:
        if (
            not isinstance(resolution_id, str)
            or re.fullmatch(r"evresolution_[0-9a-f]{24}", resolution_id) is None
        ):
            raise ValueError("resolution_id 格式无效。")
        return await self._read("resolution_id", resolution_id)

    async def get_by_decision_state(
        self,
        decision_state_id: str,
    ) -> EvolutionDecisionResolution | None:
        if (
            not isinstance(decision_state_id, str)
            or re.fullmatch(r"evdecision_[0-9a-f]{24}", decision_state_id) is None
        ):
            raise ValueError("decision_state_id 格式无效。")
        return await self._read("decision_state_id", decision_state_id)

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionDecisionResolution | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_decision_resolutions WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_store_corrupt",
                "Escalation Resolution 损坏或无法读取。",
            ) from exc


class EvolutionDecisionResolutionService:
    """Create-before-display, answer-before-resolution orchestration."""

    def __init__(
        self,
        *,
        decision_store: EvolutionDecisionStateStore,
        interaction_store: HarnessStore,
        resolution_store: EvolutionDecisionResolutionStore,
        request_user_input: RequestUserInputCallback,
        builder: EvolutionDecisionResolutionBuilder | None = None,
    ) -> None:
        if not isinstance(decision_store, EvolutionDecisionStateStore):
            raise TypeError("Decision Resolution service 需要 Decision State Store。")
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Decision Resolution service 需要 Harness Store。")
        if not isinstance(resolution_store, EvolutionDecisionResolutionStore):
            raise TypeError("Decision Resolution service 需要 Resolution Store。")
        if not callable(request_user_input):
            raise TypeError("Decision Resolution service 需要用户交互 callback。")
        self._decision_store = decision_store
        self._interaction_store = interaction_store
        self._resolution_store = resolution_store
        self._request_user_input = request_user_input
        self._builder = builder or EvolutionDecisionResolutionBuilder()
        self._decision_locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        decision_input_id: str,
    ) -> EvolutionDecisionResolution:
        lock_key = f"{Path(workspace_root).expanduser()}::{decision_input_id}"
        lock = self._decision_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._execute_locked(
                workspace_root=workspace_root,
                decision_input_id=decision_input_id,
            )

    async def _execute_locked(
        self,
        *,
        workspace_root: str | Path,
        decision_input_id: str,
    ) -> EvolutionDecisionResolution:
        workspace = _workspace(workspace_root)
        if (
            not isinstance(decision_input_id, str)
            or re.fullmatch(r"evdin_[0-9a-f]{24}", decision_input_id) is None
        ):
            raise EvolutionDecisionResolutionError(
                "decision_resolution_input_id_invalid",
                "Decision Input ID 格式无效。",
            )
        try:
            decision = await self._decision_store.get_by_decision_input(decision_input_id)
        except (EvolutionDecisionStateError, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_state_read_failed",
                "无法读取 Escalation 的 Decision State authority。",
            ) from exc
        if decision is None:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_state_missing",
                "Decision Input 尚无 Decision State。",
            )
        if decision.workspace_root != str(workspace):
            raise EvolutionDecisionResolutionError(
                "decision_resolution_workspace_mismatch",
                "Decision State 不属于当前工作区。",
            )
        existing = await self._resolution_store.get_by_decision_state(decision.decision_id)
        if existing is not None:
            return existing
        if (
            decision.state is not EvolutionDecisionStateValue.ESCALATED
            or decision.escalation is None
        ):
            raise EvolutionDecisionResolutionError(
                "decision_resolution_not_escalated",
                "只有 escalated Decision State 需要用户 Resolution。",
            )
        records = await self._interaction_history(workspace, decision)
        answered = tuple(record for record in records if record.state == "answered")
        if len(answered) > 1 and len({_answer_key(item) for item in answered}) > 1:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_answer_ambiguous",
                "同一 Decision State 存在冲突的已回答交互，拒绝猜测用户意图。",
            )
        if answered:
            return await self._record(decision, answered[0])
        pending = next((record for record in records if record.state == "pending"), None)
        if pending is not None:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_interaction_pending",
                f"用户交互 {pending.interaction_id} 仍待回答；请先在界面完成选择。",
            )
        interaction_id = _next_interaction_id(decision, records)
        payload = {
            **decision.escalation.to_public_dict(),
            "priority": "high",
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": decision.decision_id,
        }
        try:
            await self._request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_interaction_unavailable",
                "当前界面无法创建持久 Evolution 用户交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_interaction_invalid",
                "Evolution 用户交互未通过运行时协议校验。",
            ) from exc
        try:
            interaction = await self._interaction_store.get_interaction(
                workspace_root=workspace,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_interaction_read_failed",
                "无法重读已回答的 Evolution 用户交互 authority。",
            ) from exc
        if interaction is None or interaction.state != "answered":
            raise EvolutionDecisionResolutionError(
                "decision_resolution_answer_not_committed",
                "用户答案尚未提交到 Harness authority，不能形成 Resolution。",
            )
        return await self._record(decision, interaction)

    async def _interaction_history(
        self,
        workspace: Path,
        decision: EvolutionDecisionState,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            records = await self._interaction_store.list_interactions(
                workspace_root=workspace,
                subject_kind="tool",
                subject_ids=(decision.decision_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionResolutionError(
                "decision_resolution_interaction_history_failed",
                "无法读取 Decision State 的持久交互历史。",
            ) from exc
        return tuple(
            record for record in records if _interaction_matches_decision(record, decision)
        )

    async def _record(
        self,
        decision: EvolutionDecisionState,
        interaction: HarnessInteractionRecord,
    ) -> EvolutionDecisionResolution:
        artifact = self._builder.build(decision=decision, interaction=interaction)
        return await self._resolution_store.record(artifact)


def resolve_escalation_answer(
    decision: EvolutionDecisionState,
    interaction: HarnessInteractionRecord,
) -> tuple[
    EvolutionDecisionResolutionAction,
    EvolutionDecisionResolutionOutcome,
    bool,
    bool,
]:
    """Map a fenced answer to a non-promoting deterministic resolution."""

    if decision.state is not EvolutionDecisionStateValue.ESCALATED or decision.escalation is None:
        raise ValueError("只有 escalated Decision State 可以形成 Resolution。")
    if not _interaction_matches_decision(interaction, decision):
        raise ValueError("用户交互与 Decision State escalation 不一致。")
    if interaction.state != "answered":
        raise ValueError("用户交互尚未 answered。")
    if interaction.answer_kind == "custom":
        return (
            EvolutionDecisionResolutionAction.CUSTOM_INSTRUCTION,
            EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP,
            True,
            False,
        )
    mapping = {
        "collect_missing_evidence": (
            EvolutionDecisionResolutionAction.COLLECT_MISSING_EVIDENCE,
            EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED,
            True,
            False,
        ),
        "request_human_review": (
            EvolutionDecisionResolutionAction.REQUEST_HUMAN_REVIEW,
            EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED,
            True,
            False,
        ),
        "revise_candidate": (
            EvolutionDecisionResolutionAction.REVISE_CANDIDATE,
            EvolutionDecisionResolutionOutcome.REVISE,
            False,
            True,
        ),
        "reject_candidate": (
            EvolutionDecisionResolutionAction.REJECT_CANDIDATE,
            EvolutionDecisionResolutionOutcome.REJECTED,
            False,
            True,
        ),
    }
    result = mapping.get(interaction.answer_value)
    if result is None:
        raise ValueError("用户 option 不属于 Decision State escalation。")
    return result


def render_evolution_decision_resolution(
    artifact: EvolutionDecisionResolution,
) -> str:
    item = EvolutionDecisionResolution.model_validate_json(artifact.model_dump_json())
    answer = item.custom_text if item.answer_kind == "custom" else item.answer_label
    lines = [
        f"# Evolution Escalation Resolution `{item.resolution_id}`",
        "",
        "**用户答案已通过 Harness fencing 持久化；本回执不会接受或发布 Candidate。**",
        "",
        f"- Decision State：`{item.decision_state_id}` · `escalated`",
        f"- Interaction：`{item.interaction_id}` · sequence {item.interaction_sequence}",
        f"- 用户答案：{answer}",
        f"- Action：`{item.action.value}`",
        f"- Outcome：`{item.outcome.value}`",
        f"- 仍需后续动作：{'是' if item.requires_follow_up else '否'}",
        f"- Candidate acceptance decided：{'是' if item.candidate_acceptance_decided else '否'}",
        "- Candidate accepted：`false`",
        "- Promotion review ready：`false`",
        "- Promotion executed：`false`",
        f"- Resolution SHA-256：`{item.resolution_sha256}`",
        "",
        _next_action_text(item),
    ]
    return "\n".join(lines)


def _interaction_matches_decision(
    record: HarnessInteractionRecord,
    decision: EvolutionDecisionState,
) -> bool:
    actual = record.request().to_public_dict()
    expected = decision.escalation.to_public_dict() if decision.escalation else {}
    if record.schema_version == 1:
        actual.pop("priority", None)
        expected.pop("priority", None)
    return bool(
        decision.escalation is not None
        and record.subject_kind == "tool"
        and record.subject_id == decision.decision_id
        and actual == expected
        and _INTERACTION_PREFIX_RE.fullmatch(record.interaction_id) is not None
        and _INTERACTION_PREFIX_RE.fullmatch(record.interaction_id).group(1)
        == decision.decision_id.removeprefix("evdecision_")
    )


def _next_interaction_id(
    decision: EvolutionDecisionState,
    records: tuple[HarnessInteractionRecord, ...],
) -> str:
    attempts = []
    for record in records:
        match = _INTERACTION_PREFIX_RE.fullmatch(record.interaction_id)
        if match is not None:
            attempts.append(int(match.group(2)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionDecisionResolutionError(
            "decision_resolution_attempts_exhausted",
            "Decision State 用户交互尝试次数已达上限。",
        )
    suffix = decision.decision_id.removeprefix("evdecision_")
    return f"ask-evolution-{suffix}-{attempt}"


def _answer_key(record: HarnessInteractionRecord) -> tuple[str, str, str]:
    return record.answer_kind, record.answer_value, record.custom_text


def _next_action_text(item: EvolutionDecisionResolution) -> str:
    messages = {
        EvolutionDecisionResolutionOutcome.EVIDENCE_REQUIRED: (
            "下一步：补齐缺失平台或资源证据，并形成新的不可变证据链。"
        ),
        EvolutionDecisionResolutionOutcome.HUMAN_REVIEW_REQUIRED: (
            "下一步：安排显式人工风险审查；当前 Resolution 不授予 promotion 权限。"
        ),
        EvolutionDecisionResolutionOutcome.REVISE: (
            "下一步：修订 Candidate，保留本轮 Decision 与用户 Resolution 作为审计证据。"
        ),
        EvolutionDecisionResolutionOutcome.REJECTED: (
            "下一步：终止本 Candidate；不得修改 baseline、main 或生产配置。"
        ),
        EvolutionDecisionResolutionOutcome.CUSTOM_FOLLOW_UP: (
            "下一步：把自定义要求作为受约束后续输入处理；LLM 无权将其解释为接受或 promotion。"
        ),
    }
    return messages[item.outcome]


def _workspace(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionDecisionResolutionError(
            "decision_resolution_workspace_missing",
            "Escalation Resolution 工作区不存在。",
        ) from exc
    if not path.is_dir():
        raise EvolutionDecisionResolutionError(
            "decision_resolution_workspace_missing",
            "Escalation Resolution 工作区不存在。",
        )
    return path


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_decision_resolutions (
            resolution_id TEXT PRIMARY KEY,
            resolution_sha256 TEXT NOT NULL,
            decision_state_id TEXT NOT NULL UNIQUE,
            decision_state_sha256 TEXT NOT NULL,
            interaction_id TEXT NOT NULL UNIQUE,
            interaction_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN (
                'evidence_required', 'human_review_required', 'revise',
                'rejected', 'custom_follow_up'
            )),
            resolution_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionDecisionResolution:
    encoded = str(row["resolution_json"])
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Escalation Resolution Store artifact 过大。")
    item = EvolutionDecisionResolution.model_validate_json(encoded)
    if not (
        row["resolution_id"] == item.resolution_id
        and row["resolution_sha256"] == item.resolution_sha256
        and row["decision_state_id"] == item.decision_state_id
        and row["decision_state_sha256"] == item.decision_state_sha256
        and row["interaction_id"] == item.interaction_id
        and row["interaction_sha256"] == item.interaction_sha256
        and row["workspace_root"] == item.workspace_root
        and row["outcome"] == item.outcome.value
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Escalation Resolution Store index 不一致。")
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
    "EVOLUTION_DECISION_RESOLUTION_POLICY",
    "EvolutionDecisionResolution",
    "EvolutionDecisionResolutionAction",
    "EvolutionDecisionResolutionBuilder",
    "EvolutionDecisionResolutionError",
    "EvolutionDecisionResolutionOutcome",
    "EvolutionDecisionResolutionService",
    "EvolutionDecisionResolutionStore",
    "render_evolution_decision_resolution",
    "resolve_escalation_answer",
]
