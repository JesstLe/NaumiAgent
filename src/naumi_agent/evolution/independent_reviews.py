"""Independent, advisory-only model review over mechanical gate authority."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.mechanical_gates import (
    EvolutionMechanicalGate,
    EvolutionMechanicalGateError,
    EvolutionMechanicalGateStore,
    MechanicalGateRequiredAction,
    MechanicalGateRule,
)
from naumi_agent.evolution.mutation_author_receipts import (
    EvolutionMutationAuthorReceipt,
    EvolutionMutationAuthorReceiptError,
    EvolutionMutationAuthorReceiptStore,
)
from naumi_agent.model.router import (
    ModelCapabilityContract,
    ModelContractStatus,
    ModelResponse,
    ModelRuntimeIdentity,
    ModelTier,
    TokenUsage,
)
from naumi_agent.runtime.ports.model import ModelPort

INDEPENDENT_REVIEW_POLICY = "evolution-independent-review-v1"
INDEPENDENT_REVIEW_PROMPT_POLICY = "evolution-independent-review-prompt-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 16 * 1_024 * 1_024
_MAX_PROMPT_BYTES = 2 * 1_024 * 1_024
_MAX_RESPONSE_BYTES = 64 * 1_024
_CLAIM_POLL_SECONDS = 0.05


class IndependentReviewStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED_BY_MECHANICAL_VETO = "blocked_by_mechanical_veto"


class IndependentReviewRecommendation(StrEnum):
    CONTINUE_TO_COUNTERFACTUAL = "continue_to_counterfactual"
    REVISE_BEFORE_COUNTERFACTUAL = "revise_before_counterfactual"
    ESCALATE_FOR_HUMAN_REVIEW = "escalate_for_human_review"


class IndependentReviewConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class IndependentReviewerIdentity(_StrictModel):
    requested_model: str = Field(min_length=1, max_length=512)
    canonical_model: str = Field(min_length=1, max_length=512)
    upstream_model: str = Field(min_length=1, max_length=512)
    provider: str = Field(min_length=1, max_length=128)
    api_format: str = Field(min_length=1, max_length=128)
    identity_source: str = Field(min_length=1, max_length=128)

    @field_validator("*")
    @classmethod
    def _safe_identity(cls, value: str) -> str:
        return _safe_text(value, field="reviewer identity", max_length=512)


class IndependentReviewOpinion(_StrictModel):
    schema_version: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=1_500)
    strengths: tuple[str, ...] = Field(max_length=8)
    concerns: tuple[str, ...] = Field(max_length=8)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=24)
    recommendation: IndependentReviewRecommendation
    confidence: IndependentReviewConfidence
    mechanical_gate_override_requested: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False

    @field_validator("summary")
    @classmethod
    def _safe_summary(cls, value: str) -> str:
        return _safe_text(value, field="review summary", max_length=1_500)

    @field_validator("strengths", "concerns")
    @classmethod
    def _safe_findings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _safe_text(item, field="review finding", max_length=500)
            for item in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("Independent Review finding 不得重复。")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def _safe_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _safe_text(item, field="review evidence ref", max_length=256)
            for item in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("Independent Review evidence refs 不得重复。")
        return normalized


class EvolutionIndependentReview(_StrictModel):
    """Advisory review or deterministic veto explanation; never a decision."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-independent-review-v1"] = (
        INDEPENDENT_REVIEW_POLICY
    )
    review_id: str = Field(pattern=r"^evreview_[0-9a-f]{24}$")
    review_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    gate_id: str = Field(pattern=r"^evgate_[0-9a-f]{24}$")
    gate_sha256: str = Field(pattern=_SHA256_RE)
    gate_outcome: Literal["pass", "veto"]
    author_receipt_id: str = Field(pattern=r"^evmar_[0-9a-f]{24}$")
    author_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    status: IndependentReviewStatus
    reviewer: IndependentReviewerIdentity | None = None
    author_canonical_model: str = Field(min_length=1, max_length=512)
    author_provider: str = Field(min_length=1, max_length=128)
    reviewer_author_isolated: bool | None = None
    prompt_policy_version: Literal[
        "evolution-independent-review-prompt-v1"
    ] | None = None
    system_prompt_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    review_request_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    model_response_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    response_model: str | None = Field(default=None, max_length=512)
    finish_reason: str | None = Field(default=None, max_length=128)
    usage: dict[str, int | float] | None = None
    opinion: IndependentReviewOpinion | None = None
    mechanical_veto_codes: tuple[MechanicalGateRule, ...] = Field(max_length=16)
    required_actions: tuple[MechanicalGateRequiredAction, ...] = Field(
        min_length=1,
        max_length=3,
    )
    model_called: bool
    gate_outcome_preserved: Literal[True] = True
    reviewer_advisory_only: Literal[True] = True
    llm_override_allowed: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False
    counterfactual_review_ready: bool
    promotion_ready: Literal[False] = False
    gate: EvolutionMechanicalGate
    author_receipt: EvolutionMutationAuthorReceipt
    reviewed_at: str = Field(min_length=1, max_length=100)

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in value for char in "\x00\r\n"):
            raise ValueError("Independent Review workspace 必须是安全绝对路径。")
        return str(path.resolve())

    @field_validator("author_canonical_model", "author_provider")
    @classmethod
    def _safe_author_identity(cls, value: str) -> str:
        return _safe_text(value, field="author identity", max_length=512)

    @field_validator("response_model", "finish_reason")
    @classmethod
    def _safe_optional_response_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value == "":
            return ""
        return _safe_text(value, field="review response", max_length=512)

    @field_validator("reviewed_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        _parse_time(value)
        return value

    @model_validator(mode="after")
    def _review_is_bound_and_tamper_evident(self) -> Self:
        gate = self.gate
        author = self.author_receipt
        _require_author_binding(gate, author)
        if not (
            self.workspace_root == gate.workspace_root
            and self.gate_id == gate.gate_id
            and self.gate_sha256 == gate.gate_sha256
            and self.gate_outcome == gate.outcome
            and self.author_receipt_id == author.receipt_id
            and self.author_receipt_sha256 == author.receipt_sha256
            and self.candidate_id == gate.candidate_id
            and self.candidate_revision == gate.candidate_revision
            and self.author_canonical_model == author.canonical_model
            and self.author_provider == author.provider
            and self.mechanical_veto_codes == gate.veto_codes
            and self.required_actions == gate.required_actions
        ):
            raise ValueError("Independent Review authority 投影不一致。")
        if gate.outcome == "veto":
            if not (
                self.status is IndependentReviewStatus.BLOCKED_BY_MECHANICAL_VETO
                and self.reviewer is None
                and self.reviewer_author_isolated is None
                and self.prompt_policy_version is None
                and self.system_prompt_sha256 is None
                and self.review_request_sha256 is None
                and self.model_response_sha256 is None
                and self.response_model is None
                and self.finish_reason is None
                and self.usage is None
                and self.opinion is None
                and not self.model_called
                and not self.counterfactual_review_ready
                and gate.mechanical_veto_applied
                and not gate.independent_review_ready
            ):
                raise ValueError("Mechanical veto 的 Reviewer 状态无效。")
        else:
            reviewer = self.reviewer
            opinion = self.opinion
            if reviewer is None or opinion is None:
                raise ValueError("Independent Review 缺少 reviewer 或结构化意见。")
            allowed_refs = _allowed_evidence_refs(gate, author)
            if not (
                self.status is IndependentReviewStatus.COMPLETED
                and self.reviewer_author_isolated is True
                and reviewer.canonical_model != author.canonical_model
                and self.prompt_policy_version == INDEPENDENT_REVIEW_PROMPT_POLICY
                and self.system_prompt_sha256 is not None
                and self.review_request_sha256 is not None
                and self.model_response_sha256 is not None
                and self.response_model is not None
                and self.response_model in {
                    reviewer.requested_model,
                    reviewer.canonical_model,
                    reviewer.upstream_model,
                }
                and self.usage is not None
                and self.model_called
                and self.counterfactual_review_ready
                and gate.mechanical_gate_passed
                and gate.independent_review_ready
                and not gate.veto_codes
                and set(opinion.evidence_refs).issubset(allowed_refs)
            ):
                raise ValueError("Independent Review 完成状态或 evidence refs 无效。")
            _validate_usage_dict(self.usage)
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"review_id", "review_sha256"})
        )
        if not hmac.compare_digest(self.review_sha256, digest):
            raise ValueError("Independent Review 摘要不一致。")
        if self.review_id != f"evreview_{digest[:24]}":
            raise ValueError("Independent Review identity 不一致。")
        return self


class EvolutionIndependentReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IndependentReviewerBudget:
    timeout_seconds: float = 120.0
    max_output_tokens: int = 4_096
    max_prompt_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 1 <= float(self.timeout_seconds) <= 600
        ):
            raise ValueError("Independent Reviewer timeout 必须在 1..600 秒。")
        if (
            isinstance(self.max_output_tokens, bool)
            or not 512 <= self.max_output_tokens <= 16_384
        ):
            raise ValueError("Independent Reviewer max output 必须在 512..16384。")
        if (
            isinstance(self.max_prompt_bytes, bool)
            or not 16_384 <= self.max_prompt_bytes <= _MAX_PROMPT_BYTES
        ):
            raise ValueError("Independent Reviewer prompt 必须在 16 KiB..2 MiB。")


class EvolutionIndependentReviewBuilder:
    def build_blocked(
        self,
        *,
        gate: EvolutionMechanicalGate,
        author_receipt: EvolutionMutationAuthorReceipt,
        reviewed_at: str,
    ) -> EvolutionIndependentReview:
        if gate.outcome != "veto":
            raise EvolutionIndependentReviewError(
                "independent_review_gate_state_invalid",
                "只有 Mechanical veto 才能生成无模型说明。",
            )
        return self._build(
            gate=gate,
            author_receipt=author_receipt,
            status=IndependentReviewStatus.BLOCKED_BY_MECHANICAL_VETO,
            reviewer=None,
            messages=None,
            response=None,
            opinion=None,
            reviewed_at=reviewed_at,
        )

    def build_completed(
        self,
        *,
        gate: EvolutionMechanicalGate,
        author_receipt: EvolutionMutationAuthorReceipt,
        reviewer: IndependentReviewerIdentity,
        messages: Sequence[Mapping[str, Any]],
        response: ModelResponse,
        opinion: IndependentReviewOpinion,
        reviewed_at: str,
    ) -> EvolutionIndependentReview:
        if gate.outcome != "pass":
            raise EvolutionIndependentReviewError(
                "independent_review_gate_state_invalid",
                "Mechanical veto 不得进入模型 Reviewer。",
            )
        parsed_opinion = _parse_opinion(response, gate, author_receipt)
        if parsed_opinion != opinion:
            raise EvolutionIndependentReviewError(
                "independent_review_response_mismatch",
                "Independent Review 意见与模型原始响应不一致。",
            )
        _require_review_messages(messages)
        return self._build(
            gate=gate,
            author_receipt=author_receipt,
            status=IndependentReviewStatus.COMPLETED,
            reviewer=reviewer,
            messages=messages,
            response=response,
            opinion=opinion,
            reviewed_at=reviewed_at,
        )

    @staticmethod
    def _build(
        *,
        gate: EvolutionMechanicalGate,
        author_receipt: EvolutionMutationAuthorReceipt,
        status: IndependentReviewStatus,
        reviewer: IndependentReviewerIdentity | None,
        messages: Sequence[Mapping[str, Any]] | None,
        response: ModelResponse | None,
        opinion: IndependentReviewOpinion | None,
        reviewed_at: str,
    ) -> EvolutionIndependentReview:
        try:
            gate = EvolutionMechanicalGate.model_validate(gate.model_dump(mode="json"))
            author_receipt = EvolutionMutationAuthorReceipt.model_validate(
                author_receipt.model_dump(mode="json")
            )
            _require_author_binding(gate, author_receipt)
            _parse_time(reviewed_at)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_authority_mismatch",
                "Independent Review Gate 或 Author authority 不一致。",
            ) from exc
        completed = status is IndependentReviewStatus.COMPLETED
        usage = _usage_payload(response.usage) if response is not None else None
        payload: dict[str, Any] = {
            "schema_version": 1,
            "policy_version": INDEPENDENT_REVIEW_POLICY,
            "workspace_root": gate.workspace_root,
            "gate_id": gate.gate_id,
            "gate_sha256": gate.gate_sha256,
            "gate_outcome": gate.outcome,
            "author_receipt_id": author_receipt.receipt_id,
            "author_receipt_sha256": author_receipt.receipt_sha256,
            "candidate_id": gate.candidate_id,
            "candidate_revision": gate.candidate_revision,
            "status": status.value,
            "reviewer": reviewer.model_dump(mode="json") if reviewer else None,
            "author_canonical_model": author_receipt.canonical_model,
            "author_provider": author_receipt.provider,
            "reviewer_author_isolated": True if completed else None,
            "prompt_policy_version": (
                INDEPENDENT_REVIEW_PROMPT_POLICY if completed else None
            ),
            "system_prompt_sha256": (
                _sha256_payload(messages[0].get("content"))
                if completed and messages is not None
                else None
            ),
            "review_request_sha256": (
                _sha256_payload(messages[1].get("content"))
                if completed and messages is not None
                else None
            ),
            "model_response_sha256": (
                hashlib.sha256(response.content.encode("utf-8")).hexdigest()
                if response is not None
                else None
            ),
            "response_model": (
                response.model or reviewer.requested_model
                if response is not None and reviewer is not None
                else None
            ),
            "finish_reason": response.finish_reason if response is not None else None,
            "usage": usage,
            "opinion": opinion.model_dump(mode="json") if opinion else None,
            "mechanical_veto_codes": [item.value for item in gate.veto_codes],
            "required_actions": [item.value for item in gate.required_actions],
            "model_called": completed,
            "gate_outcome_preserved": True,
            "reviewer_advisory_only": True,
            "llm_override_allowed": False,
            "candidate_acceptance_decided": False,
            "counterfactual_review_ready": completed,
            "promotion_ready": False,
            "gate": gate.model_dump(mode="json"),
            "author_receipt": author_receipt.model_dump(mode="json"),
            "reviewed_at": reviewed_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionIndependentReview.model_validate({
                **payload,
                "review_id": f"evreview_{digest[:24]}",
                "review_sha256": digest,
            })
        except (TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_invalid",
                "Independent Review artifact 无法验证。",
            ) from exc


@dataclass(frozen=True, slots=True)
class _ClaimResult:
    acquired: bool
    existing: EvolutionIndependentReview | None = None


class EvolutionIndependentReviewStore:
    """Immutable review storage plus a cross-process model-call lease."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    async def get(self, review_id: str) -> EvolutionIndependentReview | None:
        if not isinstance(review_id, str) or re.fullmatch(
            r"evreview_[0-9a-f]{24}", review_id
        ) is None:
            raise ValueError("review_id 格式无效。")
        return await self._read("review_id", review_id)

    async def get_by_gate(self, gate_id: str) -> EvolutionIndependentReview | None:
        _require_gate_id(gate_id)
        return await self._read("gate_id", gate_id)

    async def acquire(
        self,
        *,
        gate_id: str,
        owner_token: str,
        now: datetime,
        lease_seconds: float,
    ) -> _ClaimResult:
        _require_gate_id(gate_id)
        _require_owner_token(owner_token)
        _require_lease_seconds(lease_seconds)
        current_time = _aware_utc(now)
        lease_until = current_time + timedelta(seconds=lease_seconds)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._ensure_schema()
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row
                await _configure_connection(db)
                await db.execute("BEGIN IMMEDIATE")
                existing_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_independent_reviews WHERE gate_id = ?",
                        (gate_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _from_row(existing_row)
                    await db.rollback()
                    return _ClaimResult(acquired=False, existing=existing)
                claim = await (
                    await db.execute(
                        "SELECT * FROM evolution_independent_review_claims "
                        "WHERE gate_id = ?",
                        (gate_id,),
                    )
                ).fetchone()
                if claim is not None:
                    claim_until = _parse_time(claim["lease_until"])
                    if claim["owner_token"] != owner_token and claim_until > current_time:
                        await db.rollback()
                        return _ClaimResult(acquired=False)
                await db.execute(
                    "INSERT INTO evolution_independent_review_claims "
                    "(gate_id, owner_token, lease_until, claimed_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(gate_id) DO UPDATE SET owner_token=excluded.owner_token, "
                    "lease_until=excluded.lease_until, claimed_at=excluded.claimed_at",
                    (
                        gate_id,
                        owner_token,
                        _iso(lease_until),
                        _iso(current_time),
                    ),
                )
                await db.commit()
                return _ClaimResult(acquired=True)
        except EvolutionIndependentReviewError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_store_error",
                "Independent Review claim 无法持久化。",
            ) from exc

    async def record_owned(
        self,
        artifact: EvolutionIndependentReview,
        *,
        owner_token: str,
    ) -> EvolutionIndependentReview:
        _require_owner_token(owner_token)
        try:
            item = EvolutionIndependentReview.model_validate(
                artifact.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_invalid",
                "Independent Review artifact 无效或已被篡改。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionIndependentReviewError(
                "independent_review_oversized",
                "Independent Review artifact 超过 16 MiB。",
            )
        try:
            await self._ensure_schema()
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row
                await _configure_connection(db)
                await db.execute("BEGIN IMMEDIATE")
                existing_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_independent_reviews WHERE gate_id = ?",
                        (item.gate_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _from_row(existing_row)
                    if existing != item:
                        await db.rollback()
                        raise EvolutionIndependentReviewError(
                            "independent_review_conflict",
                            "同一 Mechanical Gate 不可覆盖为不同 Independent Review。",
                        )
                    await db.rollback()
                    return existing
                claim = await (
                    await db.execute(
                        "SELECT owner_token FROM evolution_independent_review_claims "
                        "WHERE gate_id = ?",
                        (item.gate_id,),
                    )
                ).fetchone()
                if claim is None or claim["owner_token"] != owner_token:
                    await db.rollback()
                    raise EvolutionIndependentReviewError(
                        "independent_review_claim_lost",
                        "Independent Review model-call claim 已失效或被接管。",
                    )
                await db.execute(
                    "INSERT INTO evolution_independent_reviews "
                    "(review_id, review_sha256, gate_id, gate_sha256, workspace_root, "
                    "review_status, review_json, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.review_id,
                        item.review_sha256,
                        item.gate_id,
                        item.gate_sha256,
                        item.workspace_root,
                        item.status.value,
                        encoded,
                        item.reviewed_at,
                    ),
                )
                await db.execute(
                    "DELETE FROM evolution_independent_review_claims WHERE gate_id = ?",
                    (item.gate_id,),
                )
                await db.commit()
        except EvolutionIndependentReviewError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_store_error",
                "Independent Review artifact 无法持久化。",
            ) from exc
        restored = await self.get(item.review_id)
        assert restored is not None
        return restored

    async def release(self, *, gate_id: str, owner_token: str) -> None:
        _require_gate_id(gate_id)
        _require_owner_token(owner_token)
        if not self._db_path.is_file():
            return
        try:
            await self._ensure_schema()
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                await _configure_connection(db)
                await db.execute(
                    "DELETE FROM evolution_independent_review_claims "
                    "WHERE gate_id = ? AND owner_token = ?",
                    (gate_id, owner_token),
                )
                await db.commit()
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_store_error",
                "Independent Review claim 无法释放。",
            ) from exc

    async def _read(
        self,
        column: str,
        value: str,
    ) -> EvolutionIndependentReview | None:
        if not self._db_path.is_file():
            return None
        try:
            await self._ensure_schema()
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row
                await _configure_connection(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_independent_reviews WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_store_corrupt",
                "Independent Review artifact 损坏或无法读取。",
            ) from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                await _ensure_schema(db)
                await db.commit()
            self._schema_ready = True


class EvolutionIndependentReviewExecutor:
    def __init__(
        self,
        *,
        model_port: ModelPort,
        gate_store: EvolutionMechanicalGateStore,
        author_store: EvolutionMutationAuthorReceiptStore,
        review_store: EvolutionIndependentReviewStore,
        builder: EvolutionIndependentReviewBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(gate_store, EvolutionMechanicalGateStore):
            raise TypeError("Independent Reviewer 需要 Mechanical Gate Store。")
        if not isinstance(author_store, EvolutionMutationAuthorReceiptStore):
            raise TypeError("Independent Reviewer 需要 Mutation Author Receipt Store。")
        if not isinstance(review_store, EvolutionIndependentReviewStore):
            raise TypeError("Independent Reviewer 需要 Independent Review Store。")
        if builder is not None and not isinstance(builder, EvolutionIndependentReviewBuilder):
            raise TypeError("Independent Reviewer 需要 Independent Review Builder。")
        self._model_port = model_port
        self._gate_store = gate_store
        self._author_store = author_store
        self._review_store = review_store
        self._builder = builder or EvolutionIndependentReviewBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        gate_id: str,
        reviewer_model: str | None = None,
        budget: IndependentReviewerBudget | None = None,
    ) -> EvolutionIndependentReview:
        workspace = _resolve_workspace(workspace_root)
        gate = await self._load_gate(gate_id)
        if gate.workspace_root != str(workspace):
            raise EvolutionIndependentReviewError(
                "independent_review_workspace_mismatch",
                "Mechanical Gate 不属于当前工作区。",
            )
        author = await self._load_author(gate)
        try:
            _require_author_binding(gate, author)
        except ValueError as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_authority_mismatch",
                "Mechanical Gate 与 Mutation Author Receipt 不一致。",
            ) from exc
        existing = await self._review_store.get_by_gate(gate.gate_id)
        if existing is not None:
            _require_existing_binding(existing, gate, author, workspace)
            return existing

        if budget is not None and not isinstance(budget, IndependentReviewerBudget):
            raise TypeError("Independent Reviewer budget 类型无效。")
        limits = budget or IndependentReviewerBudget()
        reviewer: IndependentReviewerIdentity | None = None
        messages: list[dict[str, Any]] | None = None
        resolved_model = ""
        max_tokens = 0
        if gate.outcome == "pass":
            resolved_model, reviewer, capability = self._resolve_reviewer(
                reviewer_model,
                author,
            )
            messages = _review_messages(gate, author)
            _require_prompt_budget(messages, limits, capability)
            max_tokens = min(
                limits.max_output_tokens,
                capability.request_max_tokens,
                capability.max_output,
            )

        owner_token = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(limits.timeout_seconds)
        acquired = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise EvolutionIndependentReviewError(
                        "independent_review_claim_timeout",
                        "等待 Independent Reviewer single-flight claim 超时。",
                    )
                claim = await self._review_store.acquire(
                    gate_id=gate.gate_id,
                    owner_token=owner_token,
                    now=self._clock(),
                    lease_seconds=float(limits.timeout_seconds) + 30,
                )
                if claim.existing is not None:
                    _require_existing_binding(claim.existing, gate, author, workspace)
                    return claim.existing
                if claim.acquired:
                    acquired = True
                    break
                await asyncio.sleep(min(_CLAIM_POLL_SECONDS, remaining))

            if gate.outcome == "veto":
                artifact = self._builder.build_blocked(
                    gate=gate,
                    author_receipt=author,
                    reviewed_at=_iso(self._clock()),
                )
            else:
                assert reviewer is not None and messages is not None
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise EvolutionIndependentReviewError(
                        "independent_review_timeout",
                        "Independent Reviewer 已超过时间预算。",
                    )
                response = await _call_reviewer(
                    self._model_port,
                    messages=messages,
                    model=resolved_model,
                    max_tokens=max_tokens,
                    timeout_seconds=remaining,
                )
                opinion = _parse_opinion(response, gate, author)
                artifact = self._builder.build_completed(
                    gate=gate,
                    author_receipt=author,
                    reviewer=reviewer,
                    messages=messages,
                    response=response,
                    opinion=opinion,
                    reviewed_at=_iso(self._clock()),
                )
            return await self._review_store.record_owned(
                artifact,
                owner_token=owner_token,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if acquired:
                try:
                    await self._review_store.release(
                        gate_id=gate.gate_id,
                        owner_token=owner_token,
                    )
                except EvolutionIndependentReviewError:
                    if not await self._review_store.get_by_gate(gate.gate_id):
                        raise

    async def _load_gate(self, gate_id: str) -> EvolutionMechanicalGate:
        try:
            gate = await self._gate_store.get(gate_id.strip())
        except (EvolutionMechanicalGateError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_gate_read_failed",
                "无法读取 Mechanical Gate。",
            ) from exc
        if gate is None:
            raise EvolutionIndependentReviewError(
                "independent_review_gate_missing",
                "Mechanical Gate 不存在。",
            )
        return gate

    async def _load_author(
        self,
        gate: EvolutionMechanicalGate,
    ) -> EvolutionMutationAuthorReceipt:
        try:
            author = await asyncio.to_thread(
                self._author_store.get_for_trace,
                gate.mutation_trace_id,
            )
        except (EvolutionMutationAuthorReceiptError, OSError, TypeError, ValueError) as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_author_read_failed",
                "无法读取 Mutation Author Receipt。",
            ) from exc
        if author is None:
            raise EvolutionIndependentReviewError(
                "independent_review_author_missing",
                "Mutation Author Receipt 不存在，无法证明 Reviewer 独立性。",
            )
        return author

    def _resolve_reviewer(
        self,
        requested: str | None,
        author: EvolutionMutationAuthorReceipt,
    ) -> tuple[str, IndependentReviewerIdentity, ModelCapabilityContract]:
        try:
            resolved = requested.strip() if requested else self._model_port.resolve_model(
                ModelTier.REASONING
            )
            if not resolved:
                raise ValueError("empty model")
            identity = self._model_port.get_runtime_identity(resolved)
            capability = self._model_port.get_model_capability_contract(resolved)
        except Exception as exc:
            raise EvolutionIndependentReviewError(
                "independent_review_model_metadata_failed",
                "无法读取 Independent Reviewer 模型 authority。",
            ) from exc
        reviewer = _reviewer_identity(identity, resolved)
        if reviewer.canonical_model == author.canonical_model:
            raise EvolutionIndependentReviewError(
                "independent_review_identity_conflict",
                "Reviewer canonical model 与 Mutation author 相同；请选择独立模型。",
            )
        if (
            not isinstance(capability, ModelCapabilityContract)
            or capability.canonical_model != reviewer.canonical_model
            or capability.provider != reviewer.provider
            or capability.status
            not in {ModelContractStatus.VERIFIED, ModelContractStatus.PARTIAL}
            or capability.supports_structured_output is not True
            or capability.max_context < 4_096
            or capability.max_output < 512
            or capability.request_max_tokens < 512
        ):
            raise EvolutionIndependentReviewError(
                "independent_review_model_capability_unverified",
                "Reviewer 模型的身份、上下文或 structured-output 能力未验证。",
            )
        return resolved, reviewer, capability


def render_independent_review(review: EvolutionIndependentReview) -> str:
    item = EvolutionIndependentReview.model_validate(review.model_dump(mode="json"))
    if item.status is IndependentReviewStatus.BLOCKED_BY_MECHANICAL_VETO:
        return "\n".join((
            f"# Evolution Independent Review `{item.review_id}`",
            "",
            "**机械门禁已否决；未调用任何 Reviewer 模型，LLM 无权改写该结果。**",
            "",
            f"- Gate：`{item.gate_id}` · `veto`",
            f"- Author：`{item.author_provider}/{item.author_canonical_model}`",
            "- Veto：" + ", ".join(
                f"`{code.value}`" for code in item.mechanical_veto_codes
            ),
            "- Required actions：" + ", ".join(
                f"`{action.value}`" for action in item.required_actions
            ),
            "- Model called：`false`",
            "- Candidate acceptance decided：`false`",
            f"- Review SHA-256：`{item.review_sha256}`",
        ))
    assert item.reviewer is not None and item.opinion is not None
    lines = [
        f"# Evolution Independent Review `{item.review_id}`",
        "",
        "**独立审查已完成；意见仅供后续反事实检查使用，不接受 Candidate，也不批准发布。**",
        "",
        f"- Gate：`{item.gate_id}` · `pass`（保持不变）",
        f"- Author：`{item.author_provider}/{item.author_canonical_model}`",
        f"- Reviewer：`{item.reviewer.provider}/{item.reviewer.canonical_model}`",
        "- 身份隔离：`verified`",
        f"- 建议：`{item.opinion.recommendation.value}`",
        f"- 置信度：`{item.opinion.confidence.value}`",
        f"- 摘要：{item.opinion.summary}",
    ]
    if item.opinion.strengths:
        lines.append("- 优点：" + "；".join(item.opinion.strengths))
    if item.opinion.concerns:
        lines.append("- 关注：" + "；".join(item.opinion.concerns))
    lines.extend((
        "- Evidence refs：" + ", ".join(
            f"`{ref}`" for ref in item.opinion.evidence_refs
        ),
        f"- System Prompt SHA-256：`{item.system_prompt_sha256}`",
        f"- Review SHA-256：`{item.review_sha256}`",
        "",
        "下一步：EVO-04.4a counterfactual；Reviewer 无权覆盖 Mechanical Gate。",
    ))
    return "\n".join(lines)


async def _call_reviewer(
    model_port: ModelPort,
    *,
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    timeout_seconds: float,
) -> ModelResponse:
    try:
        response = await asyncio.wait_for(
            model_port.call(
                messages,
                model=model,
                tier=ModelTier.REASONING,
                max_tokens=max_tokens,
                temperature=0.0,
                response_format="json",
                thinking={"type": "disabled"},
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise EvolutionIndependentReviewError(
            "independent_review_timeout",
            "Independent Reviewer 模型调用超时。",
        ) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise EvolutionIndependentReviewError(
            "independent_review_model_call_failed",
            "Independent Reviewer 模型调用失败。",
        ) from exc
    if not isinstance(response, ModelResponse):
        raise EvolutionIndependentReviewError(
            "independent_review_response_invalid",
            "Independent Reviewer 返回类型无效。",
        )
    return response


def _parse_opinion(
    response: ModelResponse,
    gate: EvolutionMechanicalGate,
    author: EvolutionMutationAuthorReceipt,
) -> IndependentReviewOpinion:
    if response.tool_calls:
        raise EvolutionIndependentReviewError(
            "independent_review_response_invalid",
            "Independent Reviewer 不得调用工具。",
        )
    encoded = response.content.encode("utf-8")
    if not encoded or len(encoded) > _MAX_RESPONSE_BYTES:
        raise EvolutionIndependentReviewError(
            "independent_review_response_invalid",
            "Independent Reviewer JSON 为空或超过 64 KiB。",
        )
    _usage_payload(response.usage)
    try:
        opinion = IndependentReviewOpinion.model_validate_json(response.content)
    except (TypeError, ValueError) as exc:
        raise EvolutionIndependentReviewError(
            "independent_review_response_invalid",
            "Independent Reviewer 未返回符合 schema 的纯 JSON。",
        ) from exc
    if not set(opinion.evidence_refs).issubset(_allowed_evidence_refs(gate, author)):
        raise EvolutionIndependentReviewError(
            "independent_review_evidence_ref_invalid",
            "Independent Reviewer 引用了未提供的 evidence authority。",
        )
    return opinion


def _review_messages(
    gate: EvolutionMechanicalGate,
    author: EvolutionMutationAuthorReceipt,
) -> list[dict[str, Any]]:
    decision = gate.decision_input
    final = decision.final_evaluation
    evidence = {
        "contract": {
            "gate_id": gate.gate_id,
            "gate_sha256": gate.gate_sha256,
            "gate_outcome": gate.outcome,
            "reviewer_advisory_only": True,
            "mechanical_gate_override_allowed": False,
            "candidate_acceptance_decided": False,
        },
        "candidate": {
            "candidate_id": gate.candidate_id,
            "revision": gate.candidate_revision,
            "risk_level": decision.risk_level,
            "finding_code": decision.candidate.draft.finding_code,
            "scope": decision.candidate.draft.scope,
        },
        "mutation": {
            "receipt_id": decision.mutation_receipt_id,
            "trace_id": gate.mutation_trace_id,
            "files": [
                {
                    "path": item.path,
                    "operation": item.operation,
                    "after_sha256": item.after_sha256,
                }
                for item in decision.mutation.files
            ],
            "changed_files": gate.changed_files,
            "changed_lines": gate.changed_lines,
            "tool_calls": gate.observed_tool_calls,
            "duration_ms": gate.observed_duration_ms,
        },
        "mechanical_checks": [
            {
                "order": item.order,
                "rule": item.rule.value,
                "passed": item.passed,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in gate.checks
        ],
        "constraints": decision.constraints.model_dump(mode="json"),
        "evaluation": {
            "receipt_id": decision.final_evaluation_receipt_id,
            "lane_count": final.lane_count,
            "comparison_ids": list(final.comparison_ids),
            "required_platforms": list(final.required_platforms),
            "failure_categories": [item.value for item in final.failure_categories],
            "failure_actions": [item.value for item in final.failure_actions],
            "baseline_resources": final.baseline_resources.model_dump(mode="json"),
            "candidate_resources": final.candidate_resources.model_dump(mode="json"),
        },
        "author": {
            "receipt_id": author.receipt_id,
            "canonical_model": author.canonical_model,
            "provider": author.provider,
            "api_format": author.api_format,
            "prompt_policy_version": author.prompt_policy_version,
            "system_prompt_sha256": author.system_prompt_sha256,
            "model_calls": author.total_model_calls,
        },
        "allowed_evidence_refs": sorted(_allowed_evidence_refs(gate, author)),
        "required_output_schema": IndependentReviewOpinion.model_json_schema(),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NaumiAgent 的独立变异审查员。只依据用户消息中的结构化 authority 审查；"
                "不得调用工具、请求源码、覆盖 mechanical gate、接受/拒绝 Candidate 或批准发布。"
                "只返回一个符合 required_output_schema 的 JSON object，"
                "不要 Markdown、代码围栏或额外字段。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _reviewer_identity(
    identity: object,
    resolved_model: str,
) -> IndependentReviewerIdentity:
    if not isinstance(identity, ModelRuntimeIdentity):
        raise EvolutionIndependentReviewError(
            "independent_review_model_identity_invalid",
            "Independent Reviewer runtime identity 无效。",
        )
    if identity.requested_model.strip() != resolved_model.strip():
        raise EvolutionIndependentReviewError(
            "independent_review_model_identity_invalid",
            "Independent Reviewer requested model 与 runtime identity 不一致。",
        )
    try:
        return IndependentReviewerIdentity(
            requested_model=identity.requested_model,
            canonical_model=identity.canonical_model,
            upstream_model=identity.upstream_model,
            provider=identity.provider,
            api_format=identity.api_format,
            identity_source=identity.source,
        )
    except ValueError as exc:
        raise EvolutionIndependentReviewError(
            "independent_review_model_identity_invalid",
            "Independent Reviewer runtime identity 无效。",
        ) from exc


def _require_prompt_budget(
    messages: list[dict[str, Any]],
    budget: IndependentReviewerBudget,
    capability: ModelCapabilityContract,
) -> None:
    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    token_bytes = max(0, capability.max_context - budget.max_output_tokens - 1_024) * 2
    limit = min(budget.max_prompt_bytes, token_bytes)
    if limit < 16_384 or len(encoded) > limit:
        raise EvolutionIndependentReviewError(
            "independent_review_prompt_oversized",
            "Independent Reviewer authority 超过模型或安全上下文预算。",
        )


def _require_author_binding(
    gate: EvolutionMechanicalGate,
    author: EvolutionMutationAuthorReceipt,
) -> None:
    trace = gate.mutation_trace
    if not (
        author.mutation_trace_id == gate.mutation_trace_id == trace.trace_id
        and author.mutation_trace_sha256 == gate.mutation_trace_sha256 == trace.trace_sha256
        and author.run_id == trace.run_id
        and author.mutation_plan_id == trace.mutation_plan_id
        and author.mutation_plan_sha256 == trace.mutation_plan_sha256
        and author.attempt == trace.attempt
        and author.total_tool_calls == trace.total_tool_calls
        and author.author_identity_ready
        and not author.reviewer_identity_bound
        and not author.candidate_acceptance_decided
    ):
        raise ValueError("Mutation Author Receipt 与 Mechanical Gate Trace 不一致。")


def _allowed_evidence_refs(
    gate: EvolutionMechanicalGate,
    author: EvolutionMutationAuthorReceipt,
) -> set[str]:
    return {
        gate.gate_id,
        gate.decision_input_id,
        gate.mutation_trace_id,
        gate.decision_input.mutation_receipt_id,
        gate.decision_input.experiment_contract_id,
        gate.decision_input.final_evaluation_receipt_id,
        author.receipt_id,
        *(ref for check in gate.checks for ref in check.evidence_refs),
    }


def _require_existing_binding(
    existing: EvolutionIndependentReview,
    gate: EvolutionMechanicalGate,
    author: EvolutionMutationAuthorReceipt,
    workspace: Path,
) -> None:
    if not (
        existing.workspace_root == str(workspace)
        and existing.gate_id == gate.gate_id
        and existing.gate_sha256 == gate.gate_sha256
        and existing.author_receipt_id == author.receipt_id
        and existing.author_receipt_sha256 == author.receipt_sha256
    ):
        raise EvolutionIndependentReviewError(
            "independent_review_existing_mismatch",
            "已存在的 Independent Review 与当前 authority 不一致。",
        )


def _usage_payload(usage: TokenUsage) -> dict[str, int | float]:
    payload: dict[str, int | float] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cache_tokens": usage.cache_tokens,
        "cost_usd": round(float(usage.cost_usd), 6),
    }
    _validate_usage_dict(payload)
    return payload


def _validate_usage_dict(usage: Mapping[str, int | float]) -> None:
    if set(usage) != {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_tokens",
        "cost_usd",
    }:
        raise ValueError("Independent Review usage 字段无效。")
    counters = tuple(usage[name] for name in (
        "input_tokens", "output_tokens", "total_tokens", "cache_tokens"
    ))
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counters
    ):
        raise ValueError("Independent Review Token usage 无效。")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise ValueError("Independent Review Token usage 不一致。")
    cost = usage["cost_usd"]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise ValueError("Independent Review cost usage 无效。")


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await _configure_connection(db)
    await _ensure_wal_mode(db)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_independent_reviews (
               review_id TEXT PRIMARY KEY,
               review_sha256 TEXT NOT NULL,
               gate_id TEXT NOT NULL UNIQUE,
               gate_sha256 TEXT NOT NULL,
               workspace_root TEXT NOT NULL,
               review_status TEXT NOT NULL CHECK(
                   review_status IN ('completed', 'blocked_by_mechanical_veto')
               ),
               review_json TEXT NOT NULL,
               reviewed_at TEXT NOT NULL
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_independent_review_claims (
               gate_id TEXT PRIMARY KEY,
               owner_token TEXT NOT NULL,
               lease_until TEXT NOT NULL,
               claimed_at TEXT NOT NULL
           )"""
    )


async def _configure_connection(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA busy_timeout = 10000")


async def _ensure_wal_mode(db: aiosqlite.Connection) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        current = await (await db.execute("PRAGMA journal_mode")).fetchone()
        if current is not None and str(current[0]).lower() == "wal":
            return
        try:
            updated = await (await db.execute("PRAGMA journal_mode = WAL")).fetchone()
        except aiosqlite.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.02)
            continue
        if updated is not None and str(updated[0]).lower() == "wal":
            return
        raise aiosqlite.OperationalError("Independent Review 数据库无法启用 WAL。")


def _from_row(row: aiosqlite.Row) -> EvolutionIndependentReview:
    encoded = row["review_json"]
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Independent Review 持久化内容无效。")
    item = EvolutionIndependentReview.model_validate_json(encoded)
    if not (
        row["review_id"] == item.review_id
        and row["review_sha256"] == item.review_sha256
        and row["gate_id"] == item.gate_id
        and row["gate_sha256"] == item.gate_sha256
        and row["workspace_root"] == item.workspace_root
        and row["review_status"] == item.status.value
        and row["reviewed_at"] == item.reviewed_at
    ):
        raise ValueError("Independent Review 索引与内容不一致。")
    return item


def _resolve_workspace(workspace_root: str | Path) -> Path:
    try:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvolutionIndependentReviewError(
            "independent_review_workspace_missing",
            "Independent Review 工作区不存在。",
        ) from exc
    if not workspace.is_dir():
        raise EvolutionIndependentReviewError(
            "independent_review_workspace_missing",
            "Independent Review 工作区不存在。",
        )
    return workspace


def _safe_text(value: str, *, field: str, max_length: int) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_length
        or "\x00" in normalized
        or "\r" in normalized
        or "\n" in normalized
    ):
        raise ValueError(f"Independent Review {field} 格式无效。")
    return normalized


def _require_gate_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"evgate_[0-9a-f]{24}", value) is None:
        raise ValueError("gate_id 格式无效。")


def _require_owner_token(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("Independent Review owner token 格式无效。")


def _require_lease_seconds(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 1 <= float(value) <= 630
    ):
        raise ValueError("Independent Review claim lease 必须在 1..630 秒。")


def _require_review_messages(messages: Sequence[Mapping[str, Any]]) -> None:
    if (
        len(messages) != 2
        or messages[0].get("role") != "system"
        or messages[1].get("role") != "user"
        or not isinstance(messages[0].get("content"), str)
        or not isinstance(messages[1].get("content"), str)
        or not messages[0]["content"]
        or not messages[1]["content"]
    ):
        raise EvolutionIndependentReviewError(
            "independent_review_prompt_invalid",
            "Independent Review Prompt 必须包含 system 与 user authority。",
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Independent Review clock 必须包含时区。")
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Independent Review timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "EvolutionIndependentReview",
    "EvolutionIndependentReviewBuilder",
    "EvolutionIndependentReviewError",
    "EvolutionIndependentReviewExecutor",
    "EvolutionIndependentReviewStore",
    "IndependentReviewConfidence",
    "IndependentReviewOpinion",
    "IndependentReviewRecommendation",
    "IndependentReviewStatus",
    "IndependentReviewerBudget",
    "IndependentReviewerIdentity",
    "render_independent_review",
]
