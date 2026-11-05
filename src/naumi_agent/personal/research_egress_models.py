"""Exact research egress inputs and immutable approval identity."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import digest
from naumi_agent.personal.research_models import (
    Digest,
    NotebookId,
    RequestId,
    Revision,
    SourceId,
    StrictPayload,
)

EgressId = Annotated[str, Field(pattern=r"^rx_[a-f0-9]{32}$")]

EGRESS_CORE = (
    "id",
    "proposal_revision",
    "workspace_key",
    "actor_id",
    "actor_name",
    "actor_version",
    "session_id",
    "notebook_id",
    "notebook_revision",
    "notebook_title",
    "notebook_digest",
    "action",
    "payload",
    "destination_plan",
    "reason",
    "research_basis",
    "artifact_refs",
    "memory_refs",
    "memory_item_id",
    "output_context",
    "created_at",
    "expires_at",
    "intent_request_id",
)


def egress_digest(value):
    try:
        return digest({key: value[key] for key in EGRESS_CORE})
    except (KeyError, TypeError):
        raise PersonalAgentError("storage_error", "外发提案的原始内容校验未通过") from None


class EgressPropose(StrictPayload):
    operation: Literal["egress_propose"]
    request_id: RequestId
    notebook_id: NotebookId
    expected_notebook_revision: Revision
    action: Literal["search", "capture", "recheck"]
    reason: str = Field(min_length=1, max_length=400)
    expires_in_minutes: Annotated[int, Field(strict=True, ge=10, le=60)] = 30
    query: str | None = Field(default=None, min_length=1, max_length=400)
    provider: Literal["duckduckgo", "brave"] | None = None
    max_results: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    max_age_days: Annotated[int, Field(strict=True, ge=1, le=90)] | None = None
    source_id: SourceId | None = None
    expected_source_revision: Revision | None = None

    @field_validator("query", "reason")
    @classmethod
    def meaningful_text(cls, value):
        if value is not None and (
            not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("外发内容和用途需要明确文本")
        return value

    @model_validator(mode="after")
    def action_fields(self):
        action_fields = {
            "search": {"query", "provider", "max_results"},
            "capture": {"url", "max_age_days"},
            "recheck": {"source_id", "expected_source_revision"},
        }
        supplied = self.model_fields_set & {
            "query",
            "provider",
            "max_results",
            "url",
            "max_age_days",
            "source_id",
            "expected_source_revision",
        }
        if supplied - action_fields[self.action]:
            raise ValueError("外发操作包含不属于本次动作的字段")
        if self.action == "search" and self.query is None:
            raise ValueError("检索提案需要确切检索词")
        if self.action == "capture" and self.url is None:
            raise ValueError("抓取提案需要确切网址")
        if self.action == "recheck" and (
            self.source_id is None or self.expected_source_revision is None
        ):
            raise ValueError("复查提案需要原来源和版本")
        if self.query is not None and len(self.query.split()) > 50:
            raise ValueError("检索词超过单次长度")
        return self


class EgressList(StrictPayload):
    operation: Literal["egress_list"]
    notebook_id: NotebookId | None = None
    after: EgressId | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20


class EgressGet(StrictPayload):
    operation: Literal["egress_get"]
    id: EgressId


class EgressRequest(StrictPayload):
    operation: Literal["egress_request"]
    request_id: RequestId


class EgressExecute(EgressGet):
    operation: Literal["egress_execute"]
    request_id: RequestId
    expected_revision: Revision
    expected_digest: Digest


class EgressDecide(EgressExecute):
    operation: Literal["egress_decide"]
    decision: Literal["approve", "reject", "revoke"]


EGRESS_OPERATIONS = {
    model.model_fields["operation"].annotation.__args__[0]: model
    for model in (EgressPropose, EgressList, EgressGet, EgressRequest, EgressExecute, EgressDecide)
}


def validate_egress_arguments(value, *, human=False):
    from naumi_agent.personal.research_models import validate_payload

    if (
        type(value) is not dict
        or type(value.get("operation")) is not str
        or value["operation"] not in EGRESS_OPERATIONS
    ):
        raise PersonalAgentError("invalid_input", "外发提案参数不完整或格式不正确")
    if type(human) is not bool:
        raise PersonalAgentError("invalid_input", "外发调用入口格式不正确")
    if value["operation"] == "egress_decide" and not human:
        raise PersonalAgentError("scope_denied", "外发批准、拒绝和撤销需要用户确认")
    result = validate_payload(EGRESS_OPERATIONS[value["operation"]], value)
    return {key: item for key, item in result.items() if item is not None}
