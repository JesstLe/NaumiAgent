"""Strict shared research adapters; caller and evidence authority stay service-owned."""

from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from naumi_agent.personal.artifact_models import normalize_refs, strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_egress_models import EGRESS_OPERATIONS, validate_egress_arguments
from naumi_agent.personal.research_models import (
    CreateNotebook,
    Digest,
    NotebookId,
    RequestId,
    ReviseNotebook,
    Revision,
    SourceId,
    StrictPayload,
)
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MAX_REQUEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MANUAL_CHANNELS = frozenset({"web", "cli", "tui"})
_MESSAGES = {
    "invalid_input": "研究参数不完整或格式不正确，请核对原请求",
    "scope_denied": "研究不属于当前 Agent，或缺少已验证的用户入口",
    "not_found": "原研究或来源不存在，请核对确切编号",
    "version_conflict": "研究或来源已更新，请重新打开原记录",
    "idempotency_conflict": "原请求编号已对应其他内容，请核对原请求",
    "request_pending": "原研究请求仍待核对，请先查询原请求",
    "source_unavailable": "原来源当前不可读取，请核对来源状态",
    "source_stale": "原来源或复核已变化，请重新检查当前版本",
    "source_fetch_failed": "来源读取未完成，请先核对原请求",
    "artifact_network_denied": "私有资料上下文未授权外发新的检索词或网址",
    "artifact_access_revoked": "原资料版本或授权已失效，请重新选择资料",
    "binding_conflict": "研究与当前对话的身份不一致，请重新打开原对话",
    "execution_session_readonly": "这是只读执行记录，请从 Agent 日常对话管理研究",
    "agent_inactive": "当前 Agent 已暂停或归档，请先启用",
    "research_busy": "研究正在处理其他操作，请稍后核对原请求",
    "storage_error": "研究资料校验未通过，请恢复原资料库后继续",
    "configuration_error": "研究组件尚未就绪，请检查当前运行配置",
    "input_limit": "研究请求或结果超过容量，请缩小范围后核对原请求",
    "research_unknown": "研究操作结果待核对，请先查询原请求",
    "egress_not_approved": "外发请求尚未获准，请先由用户核对确切内容",
    "egress_expired": "原外发批准已过期，请核对原记录",
    "egress_consumed": "原外发已进入发送阶段，请核对原请求",
    "egress_revoked": "原外发请求已撤销，请核对原记录",
    "approval_required": "请先由用户核对并批准这份确切外发提案",
    "approval_invalidated": "原外发批准或依据已变化，请重新核对原记录",
    "action_expired": "原外发提案已过期，请核对原记录",
    "action_locked": "原外发已经进入发送阶段，请核对原请求",
    "lease_lost": "原外发执行权已变化，请核对原请求",
}


def public_code(code):
    return code if type(code) is str and code in _MESSAGES else "research_unknown"


def public_error(code):
    return _MESSAGES[public_code(code)]


class Context(StrictPayload):
    operation: Literal["context"]


class ListNotebooks(StrictPayload):
    operation: Literal["list"]
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    after: NotebookId | None = None


class GetNotebook(StrictPayload):
    operation: Literal["get"]
    id: NotebookId
    revision: Revision | None = None


class GetSource(StrictPayload):
    operation: Literal["source"]
    id: SourceId
    revision: Revision | None = None


class History(StrictPayload):
    operation: Literal["history"]
    id: NotebookId
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    after_revision: Revision | None = None


class Request(StrictPayload):
    operation: Literal["request"]
    request_id: RequestId


class Create(CreateNotebook):
    operation: Literal["create"]


class Revise(ReviseNotebook):
    operation: Literal["revise"]
    id: NotebookId


class Search(StrictPayload):
    operation: Literal["search"]
    id: NotebookId
    request_id: RequestId
    query: str = Field(min_length=1, max_length=400)
    max_results: Annotated[int, Field(strict=True, ge=1, le=10)] = 5


class Capture(StrictPayload):
    operation: Literal["capture"]
    id: NotebookId
    request_id: RequestId
    url: str = Field(min_length=1, max_length=4096)
    max_age_days: Annotated[int, Field(strict=True, ge=1, le=90)] = 30


class ArtifactRef(StrictPayload):
    artifact_id: Annotated[str, Field(pattern=r"^art_[a-f0-9]{32}$")]
    version: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    sha256: Digest
    grant_id: Annotated[str, Field(pattern=r"^(|asg_[a-f0-9]{32})$")]
    grant_revision: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]

    @model_validator(mode="after")
    def exact_ref(self):
        normalize_refs([self.model_dump()])
        return self


class Import(StrictPayload):
    operation: Literal["import"]
    id: NotebookId
    request_id: RequestId
    artifact_ref: ArtifactRef
    max_age_days: Annotated[int, Field(strict=True, ge=1, le=90)] = 30


class Recheck(StrictPayload):
    operation: Literal["recheck"]
    source_id: SourceId
    request_id: RequestId
    expected_revision: Revision


class Review(StrictPayload):
    operation: Literal["review"]
    id: NotebookId
    request_id: RequestId
    expected_revision: Revision


class Publish(Review):
    operation: Literal["publish"]
    expected_review_digest: Digest


_OPERATIONS = {
    model.model_fields["operation"].annotation.__args__[0]: model
    for model in (
        Context,
        ListNotebooks,
        GetNotebook,
        GetSource,
        History,
        Request,
        Create,
        Revise,
        Search,
        Capture,
        Import,
        Recheck,
        Review,
        Publish,
    )
}


def request_schema():
    definitions, branches = {}, []
    for model in [
        *_OPERATIONS.values(),
        *(model for name, model in EGRESS_OPERATIONS.items() if name != "egress_decide"),
    ]:
        schema = model.model_json_schema()
        for key, value in schema.pop("$defs", {}).items():
            if key in definitions and definitions[key] != value:
                raise ValueError("Conflicting internal research schema definition")
            definitions[key] = value
        branches.append(schema)
    return {"type": "object", "oneOf": branches, "$defs": definitions}


def validate_research_arguments(value, *, human=False):
    try:
        if type(value) is not dict or type(value.get("operation")) is not str:
            raise ValueError()
        if type(human) is not bool:
            raise ValueError()
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError()
        if value["operation"].startswith("egress_"):
            return validate_egress_arguments(value, human=human)
        model = _OPERATIONS.get(value["operation"])
        if model is None:
            raise ValueError()
        return model.model_validate(value).model_dump(mode="json", exclude_none=True)
    except PersonalAgentError as exc:
        if exc.code == "scope_denied":
            raise
        raise PersonalAgentError("invalid_input", public_error("invalid_input")) from None
    except (
        ValidationError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ):
        raise PersonalAgentError("invalid_input", public_error("invalid_input")) from None


def serializable_result(value, arguments=None):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise PersonalAgentError("input_limit", public_error("input_limit"))
        return strict_json_loads(encoded)
    except PersonalAgentError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PersonalAgentError("research_unknown", public_error("research_unknown")) from None


async def execute_research_operation(
    service, arguments, *, caller_actor=None, trusted_channel=None
):
    normalized = validate_research_arguments(arguments, human=caller_actor is None)
    if caller_actor is None:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    elif (
        type(caller_actor) is not str
        or re.fullmatch(r"main|pa_[a-f0-9]{32}", caller_actor) is None
        or trusted_channel is not None
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    return await service.execute_operation(
        normalized,
        caller_actor=caller_actor,
        trusted_channel=trusted_channel,
    )


class PersonalResearchTool(Tool):
    name = "personal_research"
    description = (
        "管理有来源的研究：创建问题、检索、捕获或导入确切资料、修订事实/估算/未知主张，"
        "机械检查引用与数值，按确切复核摘要保存报告。来源时效与未核实标签会保留；"
        "私有上下文需要检索或读取网页时，先用egress_propose提出确切外发提案，"
        "用户批准后用egress_execute执行一次；egress_get/list/request可核对原状态。"
        "不提供外部发布、交易或人工批准。私有上下文中的网络请求需要明确外发授权。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="研究",
            search_hint="research sources citations claims ratio units review report evidence",
        )

    @property
    def parameters_schema(self):
        return copy.deepcopy(request_schema())

    def parse_arguments(self, raw):
        try:
            if type(raw) is str:
                if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
                    raise ValueError()
                raw = strict_json_loads(raw)
            return validate_research_arguments(raw)
        except (PersonalAgentError, ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("研究工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            arguments = validate_research_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_research_operation(self.service, arguments, caller_actor=actor)
            result = await self.service.model_result(arguments, result, actor)
            return json.dumps(serializable_result(result), ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            code = public_code(exc.code)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            code = "invalid_input"
        except Exception:
            code = "research_unknown"
        raise ToolExecutionError(code, public_error(code))
