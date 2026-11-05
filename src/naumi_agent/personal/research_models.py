"""Strict research inputs; evidence authority is supplied only by the service."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from naumi_agent.personal.models import PersonalAgentError

RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
NotebookId = Annotated[str, Field(pattern=r"^rq_[a-f0-9]{32}$")]
SourceId = Annotated[str, Field(pattern=r"^rs_[a-f0-9]{32}$")]
ReviewId = Annotated[str, Field(pattern=r"^rr_[a-f0-9]{32}$")]
ClaimId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Domain = Literal["general", "competitor", "operations", "market"]
DecimalText = Annotated[str, Field(min_length=1, max_length=80)]
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?$")
MAX_DECIMAL = Decimal("1e30")


def decimal_value(value: str, *, nonnegative: bool = False) -> Decimal:
    """Parse bounded decimals without float conversion, implicit coercion or evaluation."""
    if not isinstance(value, str) or len(value) > 80 or _DECIMAL.fullmatch(value) is None:
        raise ValueError("数量需要明确的十进制字符串")
    try:
        result = Decimal(value)
        if (
            not result.is_finite()
            or result.copy_abs() > MAX_DECIMAL
            or max(-result.as_tuple().exponent, 0) > 18
            or nonnegative
            and result < 0
        ):
            raise ValueError("数量超出允许的精度或范围")
        return result
    except (InvalidOperation, OverflowError):
        raise ValueError("数量格式不正确") from None


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def clean_text(cls, value):
        strings = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        for item in strings:
            if isinstance(item, str):
                if any(
                    ord(char) < 32 and char not in "\t\n\r" or ord(char) == 127 for char in item
                ):
                    raise ValueError("研究字段包含无效控制字符")
                try:
                    item.encode("utf-8")
                except UnicodeError:
                    raise ValueError("研究字段包含无效字符") from None
        return value


class Citation(StrictPayload):
    source_id: SourceId
    source_revision: Revision
    quote: str = Field(min_length=1, max_length=4000)
    occurrence: int = Field(default=0, strict=True, ge=0, le=10000)

    @field_validator("quote")
    @classmethod
    def nonempty_quote(cls, value):
        if not value.strip():
            raise ValueError("引用正文不能为空")
        return value


class Quantity(StrictPayload):
    value: DecimalText
    unit: str = Field(min_length=1, max_length=32)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    period: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("value")
    @classmethod
    def valid_decimal(cls, value):
        decimal_value(value)
        return value

    @field_validator("unit", "period")
    @classmethod
    def nonempty_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("数量单位和时段需要明确内容")
        return value


class Calculation(StrictPayload):
    operation: Literal["ratio", "percent", "sum", "difference"]
    inputs: list[ClaimId] = Field(min_length=2, max_length=16)
    expected: DecimalText
    tolerance: DecimalText = "0.000001"

    @field_validator("expected")
    @classmethod
    def valid_expected(cls, value):
        decimal_value(value)
        return value

    @field_validator("tolerance")
    @classmethod
    def valid_tolerance(cls, value):
        decimal_value(value, nonnegative=True)
        return value

    @model_validator(mode="after")
    def exact_inputs(self):
        if len(self.inputs) != len(set(self.inputs)):
            raise ValueError("计算不能重复使用同一输入编号")
        if self.operation in {"ratio", "percent"} and len(self.inputs) != 2:
            raise ValueError("比例计算需要确切的两个输入")
        return self


class UpstreamClaim(StrictPayload):
    notebook_id: NotebookId
    revision: Revision
    claim_id: ClaimId


class ResearchClaim(StrictPayload):
    id: ClaimId
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["fact", "estimate", "unknown"]
    citations: list[Citation] = Field(default_factory=list, max_length=8)
    quantity: Quantity | None = None
    calculation: Calculation | None = None
    assumptions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=8
    )
    upstream_claims: list[UpstreamClaim] = Field(default_factory=list, max_length=8)
    subject: str = Field(default="", max_length=256)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("结论内容不能为空")
        return value

    @field_validator("assumptions")
    @classmethod
    def nonempty_assumptions(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("估算前提不能为空白")
        return values

    @model_validator(mode="after")
    def unique_references(self):
        citation_keys = {
            (c.source_id, c.source_revision, c.quote, c.occurrence) for c in self.citations
        }
        upstream_keys = {(u.notebook_id, u.revision, u.claim_id) for u in self.upstream_claims}
        if len(citation_keys) != len(self.citations) or len(upstream_keys) != len(
            self.upstream_claims
        ):
            raise ValueError("研究引用不能重复")
        return self


class ResearchConclusion(StrictPayload):
    stance: Literal["act", "watch", "avoid", "undecided"] = "undecided"
    premises: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=16
    )
    invalidators: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=16
    )

    @field_validator("premises", "invalidators")
    @classmethod
    def nonempty_conditions(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("判断条件不能为空白")
        return values


class CreateNotebook(StrictPayload):
    request_id: RequestId
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=4000)
    domain: Domain

    @field_validator("title", "question")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("研究标题和问题不能为空")
        return value


class ReviseNotebook(CreateNotebook):
    expected_revision: Revision
    claims: list[ResearchClaim] = Field(max_length=64)
    conclusion: ResearchConclusion

    @field_validator("claims")
    @classmethod
    def unique_claims(cls, values):
        if len(values) != len({value.id for value in values}):
            raise ValueError("结论编号不能重复")
        return values


def validate_payload(model: type[BaseModel], value: dict) -> dict:
    """Return normalized JSON inputs without echoing invalid or private request content."""
    try:
        return model.model_validate(value).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise PersonalAgentError("invalid_input", "研究字段不完整或格式不正确") from None


# Public aliases keep adapter names short without introducing another request contract.
Claim = ResearchClaim
Conclusion = ResearchConclusion
CreatePayload = CreateNotebook
RevisePayload = ReviseNotebook
payload = validate_payload
