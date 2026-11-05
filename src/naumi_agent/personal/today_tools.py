"""Strict Today adapters over the shared planning service and pinned model reads."""

from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MAX_REQUEST_BYTES = 256 * 1024
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
DayId = Annotated[str, Field(pattern=r"^td_[a-f0-9]{32}$")]
MetadataId = Annotated[str, Field(pattern=r"^tm_[a-f0-9]{32}$")]
PlacementId = Annotated[str, Field(pattern=r"^tp_[a-f0-9]{32}$")]
ItemId = Annotated[str, Field(pattern=r"^pi_[a-f0-9]{32}$")]
ActorId = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
PositiveRevision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
CivilDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Timezone = Annotated[str, Field(min_length=1, max_length=128)]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ItemSourceRef(StrictPayload):
    kind: Literal["item"]
    workspace_key: Digest
    item_id: ItemId
    item_revision: PositiveRevision
    once_key: str = Field(pattern=r"^[a-f0-9]{32}$")
    definition_sha256: Digest
    assignee_id: ActorId
    assignee_version: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]


class DateDeadline(StrictPayload):
    kind: Literal["date"]
    local_date: CivilDate
    timezone: Timezone


class InstantDeadline(StrictPayload):
    kind: Literal["instant"]
    local_time: str = Field(min_length=1, max_length=80)
    timezone: Timezone
    fold: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None


class SetMetadataPayload(StrictPayload):
    request_id: RequestId
    expected_revision: Revision
    source_ref: ItemSourceRef
    deadline: Annotated[DateDeadline | InstantDeadline, Field(discriminator="kind")] | None
    priority: Literal["high", "normal", "low"]
    status: Literal["active", "archived"]


class PlacePayload(StrictPayload):
    request_id: RequestId
    date: CivilDate
    timezone: Timezone
    expected_day_revision: Revision
    expected_metadata_revision: Revision
    source_ref: ItemSourceRef


class HumanPlacePayload(PlacePayload):
    planner_id: ActorId | None = None


class ReorderPayload(StrictPayload):
    request_id: RequestId
    day_id: DayId
    expected_revision: Revision
    placement_ids: list[PlacementId] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_placements(self):
        if len(self.placement_ids) != len(set(self.placement_ids)):
            raise ValueError("重复的今日条目")
        return self


class RemovePayload(StrictPayload):
    request_id: RequestId
    day_id: DayId
    expected_revision: Revision
    placement_id: PlacementId


class RebasePayload(StrictPayload):
    request_id: RequestId
    day_id: DayId
    expected_day_revision: Revision
    metadata_id: MetadataId
    expected_metadata_revision: Revision
    old_source_ref: ItemSourceRef
    new_source_ref: ItemSourceRef
    preserve_deadline: bool
    preserve_priority: bool


class CheckPayload(StrictPayload):
    request_id: RequestId
    metadata_id: MetadataId
    expected_revision: Revision
    source_ref: ItemSourceRef
    checked: bool


class MovePayload(StrictPayload):
    request_id: RequestId
    day_id: DayId
    expected_revision: Revision
    placement_id: PlacementId
    target_date: CivilDate
    target_timezone: Timezone
    target_planner_id: ActorId | None = None
    expected_target_revision: Revision


class DayArguments(StrictPayload):
    date: CivilDate
    timezone: Timezone
    limit: PageSize = 50
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class HumanDayArguments(DayArguments):
    planner_id: ActorId | None = None


class GetArguments(StrictPayload):
    day_id: DayId | None = None
    revision: Revision | None = None
    date: CivilDate | None = None
    timezone: Timezone | None = None
    limit: PageSize = 50
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def exact_query(self):
        fields = self.model_fields_set
        if "day_id" in fields:
            if self.day_id is None or fields & {
                "date",
                "timezone",
                "limit",
                "cursor",
                "planner_id",
            }:
                raise ValueError("历史今日读取不能混入日期查询")
        elif self.date is None or self.timezone is None or "revision" in fields:
            raise ValueError("今日读取需要日期和时区，或原日计划编号")
        return self


class HumanGetArguments(GetArguments):
    planner_id: ActorId | None = None


class SourcesArguments(StrictPayload):
    limit: PageSize = 50
    after: str = Field(default="", pattern=r"^(|pi_[a-f0-9]{32})$")


class MetadataArguments(StrictPayload):
    item_id: ItemId


class HistoryArguments(StrictPayload):
    kind: Literal["day", "metadata"]
    identity: str
    limit: PageSize = 20
    after_revision: Revision | None = None

    @model_validator(mode="after")
    def exact_identity(self):
        prefix = "td" if self.kind == "day" else "tm"
        if not re.fullmatch(prefix + r"_[a-f0-9]{32}", self.identity):
            raise ValueError("今日历史类型与编号不一致")
        return self


class RequestArguments(StrictPayload):
    request_id: RequestId


_PAYLOADS = {
    "set_metadata": SetMetadataPayload,
    "place": PlacePayload,
    "reorder": ReorderPayload,
    "remove": RemovePayload,
    "rebase": RebasePayload,
}
_HUMAN_PAYLOADS = {
    **_PAYLOADS,
    "place": HumanPlacePayload,
    "check": CheckPayload,
    "move": MovePayload,
}
_READS = {
    "context": StrictPayload,
    "list": DayArguments,
    "get": GetArguments,
    "sources": SourcesArguments,
    "metadata": MetadataArguments,
    "history": HistoryArguments,
    "requests": RequestArguments,
}


def _operations(human):
    arguments = dict(_READS)
    if human:
        arguments.update(list=HumanDayArguments, get=HumanGetArguments)
    for operation, payload in (_HUMAN_PAYLOADS if human else _PAYLOADS).items():
        arguments[operation] = create_model(
            operation.title() + "TodayArguments", __base__=StrictPayload, payload=(payload, ...)
        )
    return {
        operation: create_model(
            ("Human" if human else "Model") + operation.title() + "TodayOperation",
            __base__=argument,
            operation=(Literal[operation], ...),
        )
        for operation, argument in arguments.items()
    }


_MODELS, _HUMAN_MODELS = _operations(False), _operations(True)
MANUAL_CHANNELS = frozenset({"web", "cli", "tui"})


def request_schema():
    definitions, branches = {}, []
    for model in _MODELS.values():
        branch = copy.deepcopy(model.model_json_schema())
        definitions.update(branch.pop("$defs", {}))
        branches.append(branch)
    return {"type": "object", "oneOf": branches, "$defs": definitions}


def validate_today_arguments(value, *, human=False):
    try:
        models = _HUMAN_MODELS if human else _MODELS
        if type(value) is not dict or type(value.get("operation")) is not str:
            raise ValueError()
        if value["operation"] not in models:
            raise ValueError()
        if (
            len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > MAX_REQUEST_BYTES
        ):
            raise ValueError()
        return models[value["operation"]].model_validate(value).model_dump(exclude_unset=True)
    except (ValidationError, TypeError, ValueError, UnicodeError, RecursionError):
        raise PersonalAgentError(
            "invalid_input", "今日请求字段不完整、格式不正确或超过容量上限"
        ) from None


_ERRORS = {
    "invalid_input": "今日请求字段不完整或格式不正确",
    "invalid_date": "请选择真实存在的公历日期",
    "invalid_deadline": "截止时间需要完整本地日期、时刻与明确时区",
    "nonexistent_date": "该时区不存在这个本地日期，请重新选择",
    "nonexistent_time": "该本地时刻落在夏令时跳时区间，请重新选择",
    "ambiguous_time": "该本地时刻出现两次，请明确选择 fold 为 0 或 1",
    "calendar_timezone_changed": "时区规则已变化，请刷新后核对原计划",
    "immutable_change": "原变更记录保持不变，请提交新的确切修改",
    "not_found": "原今日记录不在当前范围，请核对原编号",
    "scope_denied": "当前身份没有这份日计划或来源的操作范围",
    "version_conflict": "日计划或事项版本已变化，请刷新后核对原修改",
    "binding_conflict": "原今日身份或来源已变化，请核对原记录",
    "idempotency_conflict": "原请求编号已绑定其他内容，请核对原完整请求",
    "storage_error": "原今日资料暂未就绪，请恢复原资料库后核对请求",
    "configuration_error": "今日服务尚未就绪，请检查运行时配置",
    "source_unavailable": "原事项来源暂不可读，请核对原记录与分享范围",
    "source_stale": "事项内容已变化，请核对新旧版本后重新绑定",
    "limit_exceeded": "今日安排已达到上限，请先调整现有条目",
    "today_unknown": "今日操作结果待核对，请查询原请求编号，不要重复新建请求",
}


def public_code(code):
    return code if type(code) is str and code in _ERRORS else "today_unknown"


def public_error(code):
    return _ERRORS[public_code(code)]


def accepted_projection_receipt(result, arguments):
    """Never turn a committed mutation into a fabricated unexecuted failure."""
    if not isinstance(result, dict) or result.get("accepted") is not True:
        return None
    payload = arguments.get("payload", {})
    request = payload.get("request_id", result.get("request_id"))
    if type(request) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", request):
        return None
    identity, raw = {}, result.get("identity", {})
    if type(raw) is dict:
        for key, pattern in (("day_id", r"td_[a-f0-9]{32}"), ("change_id", r"tc_[a-f0-9]{32}")):
            value = raw.get(key)
            if type(value) is str and re.fullmatch(pattern, value):
                identity[key] = value
        revision = raw.get("day_revision")
        if type(revision) is int and 0 <= revision <= 2**53 - 1:
            identity["day_revision"] = revision
        values = raw.get("metadata_ids")
        if (
            type(values) is list
            and len(values) <= 500
            and all(
                type(value) is str and re.fullmatch(r"tm_[a-f0-9]{32}", value) for value in values
            )
        ):
            identity["metadata_ids"] = list(values)
    return {
        "accepted": True,
        "request_id": request,
        "operation": arguments["operation"],
        "identity": identity,
        "projection_available": False,
        "content_transport": "requests",
    }


def serializable_result(result, arguments):
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        receipt = accepted_projection_receipt(result, arguments)
        if receipt is not None:
            return receipt
        raise PersonalAgentError("today_unknown", public_error("today_unknown")) from None


async def execute_today_operation(service, arguments, *, caller_actor=None, trusted_channel=None):
    """Dispatch all entry points to one service; identity never comes from payload."""
    human = caller_actor is None
    fields = validate_today_arguments(arguments, human=human)
    operation = fields.pop("operation")
    if not human and (
        type(caller_actor) is not str or not re.fullmatch(r"main|pa_[a-f0-9]{32}", caller_actor)
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if trusted_channel is not None and (not human or trusted_channel not in MANUAL_CHANNELS):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if operation in _HUMAN_PAYLOADS:
        if human and trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        kwargs = {"caller_actor": caller_actor}
        if human:
            kwargs["trusted_channel"] = trusted_channel
        return await getattr(service, operation)(fields["payload"], **kwargs)
    if operation == "list" or operation == "get" and "day_id" not in fields:
        return await service.day(**fields, caller_actor=caller_actor)
    if operation == "get":
        identity = fields.pop("day_id")
        return await service.get(identity, **fields, caller_actor=caller_actor)
    if operation == "metadata":
        return await service.metadata(fields["item_id"], caller_actor=caller_actor)
    if operation == "requests":
        return await service.request_status(fields["request_id"], caller_actor=caller_actor)
    return await getattr(service, operation)(**fields, caller_actor=caller_actor)


class PersonalTodayTool(Tool):
    name = "personal_today"
    description = (
        "查看和安排当前 Agent 的今日计划、事项截止与优先级。"
        "先 context 查询运行时，再按明确日期/时区 list。"
        "get/metadata 读取确切且绑定来源的内容；其他查询只返回原身份摘要。"
        "place/reorder/remove/set_metadata/rebase 只调整计划，不创建或取消原执行。"
        "人工勾选与跨日移动由用户入口操作；不接受planner、调用者、可信通道或管理员参数。"
        "写入保留稳定 request_id；受理后投影暂不可读时查询原 requests，不自动新建或重放。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="今日安排",
            search_hint="today day plan deadline priority ordered items metadata history",
        )

    @property
    def parameters_schema(self):
        return request_schema()

    def parse_arguments(self, raw):
        try:
            if type(raw) is str:
                if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
                    raise ValueError()
                raw = strict_json_loads(raw)
            return validate_today_arguments(raw)
        except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("今日工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        arguments, result = {}, None
        try:
            arguments = validate_today_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_today_operation(self.service, arguments, caller_actor=actor)
            try:
                projected = await self.service.model_result(
                    arguments["operation"], result, actor, arguments
                )
                projected = serializable_result(projected, arguments)
            except Exception:
                projected = accepted_projection_receipt(result, arguments)
                if projected is None:
                    raise
            return json.dumps(projected, ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            code = public_code(exc.code)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            code = "invalid_input"
        except Exception:
            code = "today_unknown"
        request = arguments.get("payload", {}).get("request_id")
        message = public_error(code) + ("；原请求：" + request if request else "")
        raise ToolExecutionError(code, message)
