"""Strict evidence-bounded independent Agent review operations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from naumi_agent.personal.research_models import (
    ClaimId,
    Digest,
    NotebookId,
    RequestId,
    Revision,
    SourceId,
    StrictPayload,
)

SemanticId = Annotated[str, Field(pattern=r"^rsv_[a-f0-9]{32}$")]
ReviewerId = Annotated[str, Field(pattern=r"^pa_[a-f0-9]{32}$")]
SessionId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]


class SemanticPreview(StrictPayload):
    operation: Literal["semantic_preview"]
    id: NotebookId
    source_session_id: SessionId
    reviewer_agent_id: ReviewerId


class SemanticStart(SemanticPreview):
    operation: Literal["semantic_start"]
    expected_reviewer_version: Revision
    expected_revision: Revision
    expected_input_digest: Digest
    request_id: RequestId


class SemanticGet(StrictPayload):
    operation: Literal["semantic_get"]
    id: SemanticId


class SemanticList(StrictPayload):
    operation: Literal["semantic_list"]
    id: NotebookId


class SemanticRequest(StrictPayload):
    operation: Literal["semantic_request"]
    request_id: RequestId


class SemanticReconcile(SemanticGet):
    operation: Literal["semantic_reconcile"]
    request_id: RequestId


class SemanticCancel(SemanticReconcile):
    operation: Literal["semantic_cancel"]


class SemanticSources(SemanticGet):
    operation: Literal["semantic_sources"]
    offset: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=10000)] = 10000


class SemanticSource(SemanticGet):
    operation: Literal["semantic_source"]
    source_id: SourceId
    source_revision: Revision
    request_id: RequestId
    offset: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=20000)] = 20000


class SemanticQuote(StrictPayload):
    source_id: SourceId
    source_revision: Revision
    body_sha256: Digest
    quote: str = Field(min_length=1, max_length=4000)
    start: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]

    @field_validator("quote")
    @classmethod
    def nonempty_quote(cls, value):
        if not value.strip():
            raise ValueError("复核引文不能为空白")
        return value


class SemanticVerdict(StrictPayload):
    claim_id: ClaimId
    verdict: Literal["supported", "contradicted", "insufficient"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[SemanticQuote] = Field(max_length=16)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        if not value.strip():
            raise ValueError("复核依据不能为空白")
        return value

    @model_validator(mode="after")
    def exact_evidence(self):
        keys = {
            (e.source_id, e.source_revision, e.body_sha256, e.quote, e.start) for e in self.evidence
        }
        if len(keys) != len(self.evidence):
            raise ValueError("复核引文不能重复")
        if self.verdict != "insufficient" and not self.evidence:
            raise ValueError("支持或反驳结论需要确切引文")
        return self


class SemanticSubmit(SemanticGet):
    operation: Literal["semantic_submit"]
    request_id: RequestId
    verdicts: list[SemanticVerdict] = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=3000)

    @field_validator("summary")
    @classmethod
    def nonempty_summary(cls, value):
        if not value.strip():
            raise ValueError("复核摘要不能为空白")
        return value

    @model_validator(mode="after")
    def unique_claims(self):
        if len({v.claim_id for v in self.verdicts}) != len(self.verdicts):
            raise ValueError("每条研究结论只提交一个复核结果")
        return self


SEMANTIC_OPERATIONS = {
    model.model_fields["operation"].annotation.__args__[0]: model
    for model in (
        SemanticPreview,
        SemanticStart,
        SemanticGet,
        SemanticList,
        SemanticRequest,
        SemanticReconcile,
        SemanticCancel,
        SemanticSources,
        SemanticSource,
        SemanticSubmit,
    )
}
