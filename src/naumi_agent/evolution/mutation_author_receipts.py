"""Durable author identity and prompt authority for model-generated mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.mutation_generation import EvolutionMutationGenerationTrace
from naumi_agent.model.router import ModelResponse, ModelRuntimeIdentity

MUTATION_AUTHOR_POLICY = "evolution-mutation-author-receipt-v1"
MUTATION_TURN_PROMPT_POLICY = "evolution-mutation-turn-prompt-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 128 * 1024
_SAFE_TEXT = re.compile(r"^[^\x00\r\n]{1,512}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class MutationAuthorModelCallFact(_StrictModel):
    """Tamper-evident facts for one accepted mutation model response."""

    order: int = Field(ge=1, le=50)
    input_context_sha256: str = Field(pattern=_SHA256_RE)
    response_model: str = Field(min_length=1, max_length=512)
    finish_reason: str = Field(default="", max_length=128)
    tool_call_count: int = Field(ge=0, le=200)
    input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=2_000_000)
    total_tokens: int = Field(ge=0, le=2_000_000)
    cache_tokens: int = Field(ge=0, le=2_000_000)
    cost_usd: float = Field(ge=0, le=1_000_000)
    fact_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("response_model")
    @classmethod
    def _safe_response_model(cls, value: str) -> str:
        return _safe_text(value, field="response model")

    @field_validator("finish_reason")
    @classmethod
    def _safe_finish_reason(cls, value: str) -> str:
        if not value:
            return ""
        return _safe_text(value, field="finish reason", max_length=128)

    @model_validator(mode="after")
    def _fact_is_consistent(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Mutation Author model call Token 计数不一致。")
        if not math.isfinite(self.cost_usd):
            raise ValueError("Mutation Author model call cost 无效。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"fact_sha256"})
        )
        if not hmac.compare_digest(self.fact_sha256, expected):
            raise ValueError("Mutation Author model call 摘要不一致。")
        return self


class EvolutionMutationAuthorReceipt(_StrictModel):
    """Immutable proof of the model identity and prompts that authored a trace."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-mutation-author-receipt-v1"] = (
        MUTATION_AUTHOR_POLICY
    )
    receipt_id: str = Field(pattern=r"^evmar_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    mutation_trace_id: str = Field(pattern=r"^evmgt_[0-9a-f]{24}$")
    mutation_trace_sha256: str = Field(pattern=_SHA256_RE)
    run_id: str = Field(min_length=1, max_length=128)
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    mutation_plan_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=3)
    completed_at: str = Field(min_length=1, max_length=100)

    author_kind: Literal["model"] = "model"
    requested_model: str = Field(min_length=1, max_length=512)
    canonical_model: str = Field(min_length=1, max_length=512)
    upstream_model: str = Field(min_length=1, max_length=512)
    provider: str = Field(min_length=1, max_length=128)
    api_format: str = Field(min_length=1, max_length=128)
    identity_source: str = Field(min_length=1, max_length=128)

    prompt_policy_version: Literal["evolution-mutation-turn-prompt-v1"] = (
        MUTATION_TURN_PROMPT_POLICY
    )
    system_prompt_sha256: str = Field(pattern=_SHA256_RE)
    initial_user_prompt_sha256: str = Field(pattern=_SHA256_RE)
    tool_schema_sha256: str = Field(pattern=_SHA256_RE)
    model_calls: tuple[MutationAuthorModelCallFact, ...] = Field(
        min_length=1,
        max_length=50,
    )
    model_calls_sha256: str = Field(pattern=_SHA256_RE)
    response_models: tuple[str, ...] = Field(min_length=1, max_length=50)
    total_model_calls: int = Field(ge=1, le=50)
    total_tool_calls: int = Field(ge=1, le=200)
    total_tokens: int = Field(ge=0, le=2_000_000)
    total_cost_usd: float = Field(ge=0, le=1_000_000)

    author_identity_ready: Literal[True] = True
    reviewer_identity_bound: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False

    @field_validator(
        "run_id",
        "requested_model",
        "canonical_model",
        "upstream_model",
        "provider",
        "api_format",
        "identity_source",
    )
    @classmethod
    def _safe_identity_text(cls, value: str) -> str:
        return _safe_text(value, field="author identity")

    @field_validator("response_models")
    @classmethod
    def _safe_response_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_text(item, field="response model") for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("Mutation Author response models 不得重复。")
        return normalized

    @model_validator(mode="after")
    def _receipt_is_bound_and_tamper_evident(self) -> Self:
        if self.mutation_trace_id != f"evmgt_{self.mutation_trace_sha256[:24]}":
            raise ValueError("Mutation Author Trace identity 不一致。")
        if self.mutation_plan_id != f"evpplan_{self.mutation_plan_sha256[:24]}":
            raise ValueError("Mutation Author Plan identity 不一致。")
        if self.total_model_calls != len(self.model_calls):
            raise ValueError("Mutation Author model call 总数不一致。")
        if tuple(item.order for item in self.model_calls) != tuple(
            range(1, len(self.model_calls) + 1)
        ):
            raise ValueError("Mutation Author model call 顺序必须连续。")
        expected_models = tuple(dict.fromkeys(
            item.response_model for item in self.model_calls
        ))
        if self.response_models != expected_models:
            raise ValueError("Mutation Author response model 集合不一致。")
        if self.total_tool_calls != sum(
            item.tool_call_count for item in self.model_calls
        ):
            raise ValueError("Mutation Author tool call 总数不一致。")
        if self.total_tokens != sum(item.total_tokens for item in self.model_calls):
            raise ValueError("Mutation Author Token 总数不一致。")
        expected_cost = round(sum(item.cost_usd for item in self.model_calls), 6)
        if not math.isclose(self.total_cost_usd, expected_cost, abs_tol=1e-9):
            raise ValueError("Mutation Author cost 总数不一致。")
        expected_calls = _sha256_payload([
            item.model_dump(mode="json") for item in self.model_calls
        ])
        if not hmac.compare_digest(self.model_calls_sha256, expected_calls):
            raise ValueError("Mutation Author model calls 摘要不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Mutation Author Receipt 摘要不一致。")
        if self.receipt_id != f"evmar_{expected[:24]}":
            raise ValueError("Mutation Author Receipt identity 不一致。")
        return self


class EvolutionMutationAuthorReceiptError(RuntimeError):
    """Typed failure without prompts, model output, or source contents."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMutationAuthorReceiptBuilder:
    """Build model-call facts and a receipt from already validated runtime data."""

    @staticmethod
    def build_call_fact(
        *,
        order: int,
        input_messages: Sequence[Mapping[str, Any]],
        response: ModelResponse,
        response_model: str,
        tool_call_count: int,
    ) -> MutationAuthorModelCallFact:
        payload = {
            "order": order,
            "input_context_sha256": _sha256_payload(list(input_messages)),
            "response_model": response_model,
            "finish_reason": response.finish_reason,
            "tool_call_count": tool_call_count,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
            "cache_tokens": response.usage.cache_tokens,
            "cost_usd": round(float(response.usage.cost_usd), 6),
        }
        payload["fact_sha256"] = _sha256_payload(payload)
        try:
            return MutationAuthorModelCallFact.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_call_invalid",
                "Mutation Author model call 事实不可验证。",
            ) from exc

    @staticmethod
    def build(
        *,
        trace: EvolutionMutationGenerationTrace,
        identity: ModelRuntimeIdentity,
        initial_messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
        model_calls: Sequence[MutationAuthorModelCallFact],
    ) -> EvolutionMutationAuthorReceipt:
        if len(initial_messages) != 2:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_prompt_invalid",
                "Mutation Author 初始 Prompt authority 不完整。",
            )
        system, user = initial_messages
        if system.get("role") != "system" or user.get("role") != "user":
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_prompt_invalid",
                "Mutation Author 初始 Prompt role 不一致。",
            )
        calls = tuple(model_calls)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "policy_version": MUTATION_AUTHOR_POLICY,
            "mutation_trace_id": trace.trace_id,
            "mutation_trace_sha256": trace.trace_sha256,
            "run_id": trace.run_id,
            "mutation_plan_id": trace.mutation_plan_id,
            "mutation_plan_sha256": trace.mutation_plan_sha256,
            "attempt": trace.attempt,
            "completed_at": trace.completed_at,
            "author_kind": "model",
            "requested_model": identity.requested_model,
            "canonical_model": identity.canonical_model,
            "upstream_model": identity.upstream_model,
            "provider": identity.provider,
            "api_format": identity.api_format,
            "identity_source": identity.source,
            "prompt_policy_version": MUTATION_TURN_PROMPT_POLICY,
            "system_prompt_sha256": _sha256_payload(system.get("content")),
            "initial_user_prompt_sha256": _sha256_payload(user.get("content")),
            "tool_schema_sha256": _sha256_payload(list(tool_schemas)),
            "model_calls": [item.model_dump(mode="json") for item in calls],
            "model_calls_sha256": _sha256_payload([
                item.model_dump(mode="json") for item in calls
            ]),
            "response_models": list(dict.fromkeys(
                item.response_model for item in calls
            )),
            "total_model_calls": len(calls),
            "total_tool_calls": sum(item.tool_call_count for item in calls),
            "total_tokens": sum(item.total_tokens for item in calls),
            "total_cost_usd": round(sum(item.cost_usd for item in calls), 6),
            "author_identity_ready": True,
            "reviewer_identity_bound": False,
            "candidate_acceptance_decided": False,
        }
        digest = _sha256_payload(payload)
        payload["receipt_id"] = f"evmar_{digest[:24]}"
        payload["receipt_sha256"] = digest
        try:
            receipt = EvolutionMutationAuthorReceipt.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_invalid",
                "Mutation Author Receipt 不可验证。",
            ) from exc
        if receipt.total_tool_calls != trace.total_tool_calls:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_trace_call_mismatch",
                "Mutation Author model calls 与 Generation Trace 不一致。",
            )
        return receipt


class EvolutionMutationAuthorReceiptStore:
    """Immutable SQLite store with one author receipt per generation trace."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)
        self._initialize_database()

    def put(
        self,
        receipt: EvolutionMutationAuthorReceipt,
    ) -> EvolutionMutationAuthorReceipt:
        try:
            receipt = EvolutionMutationAuthorReceipt.model_validate(
                receipt.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_invalid",
                "Mutation Author Receipt 输入不可验证。",
            ) from exc
        serialized = receipt.model_dump_json()
        if len(serialized.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_oversized",
                "Mutation Author Receipt 超过 128 KiB。",
            )
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT * FROM evolution_mutation_author_receipts
                       WHERE mutation_trace_id = ?""",
                    (receipt.mutation_trace_id,),
                ).fetchone()
                if row is not None:
                    current = self._from_row(row)
                    if current != receipt:
                        raise EvolutionMutationAuthorReceiptError(
                            "mutation_author_receipt_conflict",
                            "同一 Generation Trace 已存在不同 Author Receipt。",
                        )
                    db.commit()
                    return current
                db.execute(
                    """INSERT INTO evolution_mutation_author_receipts
                       (receipt_id, mutation_trace_id, receipt_sha256,
                        canonical_model, provider, receipt_json, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        receipt.receipt_id,
                        receipt.mutation_trace_id,
                        receipt.receipt_sha256,
                        receipt.canonical_model,
                        receipt.provider,
                        serialized,
                        receipt.completed_at,
                    ),
                )
                db.commit()
        except EvolutionMutationAuthorReceiptError:
            raise
        except sqlite3.Error as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_store_failed",
                "Mutation Author Receipt 持久化失败。",
            ) from exc
        return receipt

    def get(self, receipt_id: str) -> EvolutionMutationAuthorReceipt | None:
        return self._get("receipt_id", receipt_id)

    def get_for_trace(
        self,
        trace_id: str,
    ) -> EvolutionMutationAuthorReceipt | None:
        return self._get("mutation_trace_id", trace_id)

    def _get(self, column: str, value: str) -> EvolutionMutationAuthorReceipt | None:
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    f"SELECT * FROM evolution_mutation_author_receipts WHERE {column} = ?",
                    (value,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_store_failed",
                "Mutation Author Receipt 读取失败。",
            ) from exc
        return self._from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    def _initialize_database(self) -> None:
        try:
            with closing(sqlite3.connect(self._database_path, timeout=10)) as db:
                db.execute("PRAGMA busy_timeout = 10000")
                db.execute("PRAGMA journal_mode = WAL")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS evolution_mutation_author_receipts (
                           receipt_id TEXT PRIMARY KEY,
                           mutation_trace_id TEXT NOT NULL UNIQUE,
                           receipt_sha256 TEXT NOT NULL,
                           canonical_model TEXT NOT NULL,
                           provider TEXT NOT NULL,
                           receipt_json TEXT NOT NULL,
                           completed_at TEXT NOT NULL
                       )"""
                )
                db.commit()
        except sqlite3.Error as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_store_failed",
                "Mutation Author Receipt Store 初始化失败。",
            ) from exc

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EvolutionMutationAuthorReceipt:
        serialized = row["receipt_json"]
        if (
            not isinstance(serialized, str)
            or len(serialized.encode("utf-8")) > _MAX_RECEIPT_BYTES
        ):
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_corrupt",
                "Mutation Author Receipt 持久化内容损坏。",
            )
        try:
            receipt = EvolutionMutationAuthorReceipt.model_validate_json(serialized)
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_corrupt",
                "Mutation Author Receipt 持久化内容损坏。",
            ) from exc
        if (
            row["receipt_id"] != receipt.receipt_id
            or row["mutation_trace_id"] != receipt.mutation_trace_id
            or row["receipt_sha256"] != receipt.receipt_sha256
            or row["canonical_model"] != receipt.canonical_model
            or row["provider"] != receipt.provider
            or row["completed_at"] != receipt.completed_at
        ):
            raise EvolutionMutationAuthorReceiptError(
                "mutation_author_receipt_corrupt",
                "Mutation Author Receipt 索引与内容不一致。",
            )
        return receipt


def _safe_text(value: str, *, field: str, max_length: int = 512) -> str:
    normalized = value.strip()
    if len(normalized) > max_length or not _SAFE_TEXT.fullmatch(normalized):
        raise ValueError(f"Mutation Author {field} 格式无效。")
    return normalized


def _sha256_payload(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvolutionMutationAuthorReceiptError(
            "mutation_author_payload_invalid",
            "Mutation Author authority 无法规范化。",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionMutationAuthorReceipt",
    "EvolutionMutationAuthorReceiptBuilder",
    "EvolutionMutationAuthorReceiptError",
    "EvolutionMutationAuthorReceiptStore",
    "MUTATION_AUTHOR_POLICY",
    "MUTATION_TURN_PROMPT_POLICY",
    "MutationAuthorModelCallFact",
]
