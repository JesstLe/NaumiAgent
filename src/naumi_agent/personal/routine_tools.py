"""Strict shared routine payloads and provenance-pinned model operations."""

from __future__ import annotations

import copy
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_models import raw_definition
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MAX_REQUEST_BYTES = 256 * 1024
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
RoutineId = Annotated[str, Field(pattern=r"^pr_[a-f0-9]{32}$")]
OccurrenceId = Annotated[str, Field(pattern=r"^pro_[a-f0-9]{32}$")]
PreviewId = Annotated[str, Field(pattern=r"^prp_[a-f0-9]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ScheduleBase(StrictPayload):
    timezone: str = Field(min_length=1, max_length=128)
    starts_at: str | None = Field(default=None, min_length=1, max_length=80)
    ends_at: str | None = Field(default=None, min_length=1, max_length=80)
    dst_gap: Literal["skip"] = "skip"
    dst_fold: Literal["first", "second", "both"] = "first"


class OnceSchedule(ScheduleBase):
    kind: Literal["once"]
    local_time: str = Field(min_length=1, max_length=64)
    fold: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None


class CronSchedule(ScheduleBase):
    kind: Literal["cron"]
    expression: str = Field(min_length=1, max_length=200)


class TradingSchedule(ScheduleBase):
    kind: Literal["trading_day"]
    calendar_id: str = Field(min_length=1, max_length=64)
    coverage_start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    coverage_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    at: Literal["open", "close", "time"]
    local_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    offset_minutes: int = Field(default=0, strict=True, ge=-10080, le=10080)

    @model_validator(mode="after")
    def local_clock(self):
        if (self.at == "time") != (self.local_time is not None):
            raise ValueError("指定交易日时刻需要明确本地时间，开闭市触发不填写本地时间")
        return self


class RoutineArtifactRef(StrictPayload):
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{32}$")
    version: int = Field(strict=True, ge=1, le=10000)
    sha256: Digest
    grant_id: str = Field(pattern=r"^(|asg_[a-f0-9]{32})$")
    grant_revision: int = Field(strict=True, ge=0, le=2**31 - 1)


class RoutineDirectory(StrictPayload):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(min_length=1, max_length=256)
    max_files: int = Field(strict=True, ge=1, le=100)
    max_bytes: int = Field(strict=True, ge=1, le=50 * 1024**2)


class RoutinePolicy(StrictPayload):
    tool_names: list[
        Literal["personal_artifacts", "code_execute", "bash_run", "file_read", "read"]
    ] = Field(default_factory=list, max_length=5)
    artifact_operations: list[Literal["get", "read", "publish"]] = Field(
        default_factory=list, max_length=3
    )
    model_tier: Literal["fast", "capable", "reasoning"] = "capable"
    max_turns: int = Field(default=12, strict=True, ge=1, le=100)
    max_cost_microusd: int = Field(default=2000000, strict=True, ge=0, le=100000000)
    timeout_seconds: int = Field(default=180, strict=True, ge=1, le=3600)


class RoutineLimits(StrictPayload):
    max_executions: int = Field(strict=True, ge=0, le=10**6)
    total_cost_microusd: int = Field(strict=True, ge=0, le=10**15)
    max_concurrent: int = Field(strict=True, ge=1, le=64)
    max_pending: int = Field(strict=True, ge=1, le=10000)


class RoutineRecovery(StrictPayload):
    misfire_policy: Literal["skip", "latest", "catch_up"]
    grace_seconds: int = Field(strict=True, ge=0, le=604800)
    max_catch_up: int = Field(strict=True, ge=1, le=1000)
    overlap_policy: Literal["queue", "skip", "allow"]


class RoutineDefinitionPayload(StrictPayload):
    title: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=12000)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=16
    )
    assignee_id: str = Field(pattern=r"^(main|pa_[a-f0-9]{32})$")
    assignee_version: int = Field(strict=True, ge=0, le=2**31 - 1)
    schedule: Annotated[OnceSchedule | CronSchedule | TradingSchedule, Field(discriminator="kind")]
    policy: RoutinePolicy | None = None
    input_refs: list[RoutineArtifactRef] = Field(default_factory=list, max_length=64)
    directories: list[RoutineDirectory] = Field(default_factory=list, max_length=8)
    limits: RoutineLimits
    recovery: RoutineRecovery

    @model_validator(mode="after")
    def shared_definition_contract(self):
        try:
            raw_definition(self.model_dump(exclude_unset=True))
        except PersonalAgentError:
            raise ValueError("周期定义与执行合同不一致") from None
        return self


class RoutinePreviewPayload(StrictPayload):
    request_id: RequestId
    definition: RoutineDefinitionPayload
    routine_id: RoutineId | None = None
    expected_revision: Revision | None = None

    @model_validator(mode="after")
    def exact_base(self):
        if (self.routine_id is None) != (self.expected_revision is None):
            raise ValueError("修改预览需要原计划与确切版本")
        return self


class RoutineCreatePayload(StrictPayload):
    request_id: RequestId
    preview_id: PreviewId
    expected_digest: Digest
    enabled: bool


class RoutineControlPayload(StrictPayload):
    request_id: RequestId
    expected_revision: Revision


class RoutineRevisePayload(RoutineControlPayload):
    preview_id: PreviewId
    expected_digest: Digest


class ListArguments(StrictPayload):
    status: Literal["active", "paused", "cancelled", "closed"] | None = None
    query: str = Field(default="", max_length=200)
    limit: PageSize = 50
    after: RoutineId | None = None


class RoutineArguments(StrictPayload):
    routine_id: RoutineId


class HistoryArguments(RoutineArguments):
    limit: PageSize = 50
    after: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None


class OccurrenceArguments(RoutineArguments):
    limit: PageSize = 50
    after: OccurrenceId | None = None


class RequestArguments(StrictPayload):
    request_id: RequestId


class PreviewArguments(StrictPayload):
    payload: RoutinePreviewPayload


class CreateArguments(StrictPayload):
    payload: RoutineCreatePayload


class ReviseArguments(RoutineArguments):
    payload: RoutineRevisePayload


class ControlArguments(RoutineArguments):
    payload: RoutineControlPayload


_ARGUMENTS = {
    "context": StrictPayload,
    "list": ListArguments,
    "get": RoutineArguments,
    "history": HistoryArguments,
    "occurrences": OccurrenceArguments,
    "requests": RequestArguments,
    "preview": PreviewArguments,
    "create": CreateArguments,
    "revise": ReviseArguments,
    **{name: ControlArguments for name in ("pause", "resume", "cancel", "extra-run")},
}
_MODELS = {
    operation: create_model(
        operation.replace("-", "_").title() + "RoutineOperation",
        __base__=argument,
        operation=(Literal[operation], ...),
    )
    for operation, argument in _ARGUMENTS.items()
}


def request_schema():
    definitions, branches = {}, []
    for model in _MODELS.values():
        branch = copy.deepcopy(model.model_json_schema())
        definitions.update(branch.pop("$defs", {}))
        branches.append(branch)
    return {"type": "object", "oneOf": branches, "$defs": definitions}


def validate_payload(model, value):
    try:
        return model.model_validate(value).model_dump(exclude_unset=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise PersonalAgentError("invalid_input", "周期请求字段不完整或格式不正确") from None


def validate_routine_arguments(value):
    try:
        if not isinstance(value, dict) or value.get("operation") not in _MODELS:
            raise ValueError()
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
            raise ValueError()
        return validate_payload(_MODELS[value["operation"]], value)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PersonalAgentError("invalid_input", "周期工具参数格式不正确或超过容量上限") from None


def public_error(code):
    return {
        "invalid_input": "周期请求字段不完整或格式不正确",
        "not_found": "原周期记录不在当前身份范围，请核对原编号",
        "scope_denied": "当前 Agent 未取得该计划或来源的读取范围",
        "binding_conflict": "原周期身份或来源已变化，请核对原记录",
        "version_conflict": "计划或 Agent 版本已变化，请刷新后重新预览",
        "idempotency_conflict": "原请求编号已绑定其他内容，请核对原完整请求",
        "storage_error": "原周期资料暂未就绪，请恢复原资料库后核对请求",
        "configuration_error": "周期服务尚未就绪，请检查运行时配置",
        "preview_expired": "原预览已过期，请保留输入并重新预览",
        "budget_exhausted": "次数或估算额度不足，请先核对原预留与执行",
        "routine_closed": "原周期已经结束，请核对原记录",
        "schedule_exhausted": "规则在所选范围内没有下一次触发，请修改后预览",
        "calendar_coverage_unknown": "所选日期缺少交易日历凭据，请更新覆盖后预览",
        "agent_inactive": "所选 Agent 已暂停或归档，请重新选择",
    }.get(code, "周期操作结果待核对，请查询原请求")


_SUMMARY = (
    "id",
    "revision",
    "status",
    "epoch",
    "definition_sha256",
    "next_due_at",
    "created_at",
    "updated_at",
)
_OCCURRENCE = (
    "id",
    "routine_id",
    "epoch",
    "definition_revision",
    "nominal_due_at",
    "local_time",
    "fold",
    "status",
    "item_id",
    "reserved_cost_microusd",
    "settled_cost_microusd",
    "created_at",
)
_PREVIEW = (
    "preview_id",
    "definition_digest",
    "expires_at",
    "schedule_preview",
    "base_routine_id",
    "base_revision",
)


def _select(value, keys):
    if not isinstance(value, dict):
        raise PersonalAgentError("routine_unknown", "周期回执缺少原身份")
    return {key: copy.deepcopy(value[key]) for key in keys if key in value}


async def _pin_plan(service, value, actor):
    pin = getattr(service, "pin_plan_read", None)
    if not callable(pin):
        raise PersonalAgentError("configuration_error", "周期正文来源校验尚未就绪")
    return await pin(value, actor)


async def _model_result(service, operation, result, actor):
    if operation in {"list", "history"}:
        key = "routines" if operation == "list" else "history"
        return {
            key: [_select(row, _SUMMARY) for row in result[key]],
            "next_after": result.get("next_after"),
            "content_transport": "get",
        }
    if operation == "preview":
        public = _select(result, _PREVIEW)
        # Calendar evidence is structured by the scheduler; do not transport arbitrary body keys.
        public["schedule_preview"] = [
            _select(row, ("due_at", "local_time", "fold", "calendar_evidence"))
            for row in result["schedule_preview"]
        ]
        return public
    if operation == "occurrences":
        return {
            "occurrences": [_select(row, _OCCURRENCE) for row in result["occurrences"]],
            "next_after": result.get("next_after"),
            "content_transport": "personal_items",
        }
    if operation == "extra-run":
        return _select(result, _OCCURRENCE)
    if operation == "requests":
        public = _select(result, ("request_id", "operation", "status", "result_kind", "result_id"))
        kind, value = result.get("result_kind"), result.get("result")
        if value is None:
            public["result"] = None
        elif kind == "routine":
            public["result"] = await _pin_plan(service, value, actor)
        elif kind == "preview":
            public["result"] = await _model_result(service, "preview", value, actor)
        elif kind == "occurrence":
            public["result"] = _select(value, _OCCURRENCE)
        else:
            raise PersonalAgentError("routine_unknown", "原周期回执类型待核对")
        return public
    if operation == "context":
        return _select(result, ("calendars", "default_policy"))
    return await _pin_plan(service, result, actor)


async def execute_routine_operation(service, arguments, *, caller_actor=None, model_output=False):
    """One dispatcher for real service operations; model content has a mandatory pin barrier."""
    fields = dict(validate_routine_arguments(arguments))
    operation = fields.pop("operation")
    if model_output and caller_actor is None:
        raise PersonalAgentError("scope_denied", "模型周期操作缺少当前 Agent 身份")
    if operation == "context":
        result = await service.context()
    elif operation == "list":
        result = await service.list(**fields, caller_actor=caller_actor)
    elif operation in {"preview", "create"}:
        result = await getattr(service, operation)(fields["payload"], caller_actor=caller_actor)
    elif operation == "revise":
        result = await service.revise(
            fields["routine_id"], fields["payload"], caller_actor=caller_actor
        )
    elif operation in {"pause", "resume", "cancel", "extra-run"}:
        result = await service.control(
            fields["routine_id"], operation, fields["payload"], caller_actor=caller_actor
        )
    elif operation == "requests":
        result = await service.request_status(fields["request_id"], caller_actor=caller_actor)
    else:
        identity = fields.pop("routine_id")
        result = await getattr(service, operation)(identity, **fields, caller_actor=caller_actor)
    return await _model_result(service, operation, result, caller_actor) if model_output else result


class PersonalRoutinesTool(Tool):
    name = "personal_routines"
    description = (
        "预览、创建和控制当前 Agent 范围的持续计划。自然语言要求需转为明确时区、"
        "once/cron/交易日日历规则，先 preview，再持原 preview_id/digest 确认 create/revise。"
        "list/history/occurrences 仅返回原身份摘要；get 读取确切计划正文并绑定来源。"
        "pause/cancel 仅停止后续触发；extra-run 显式增加独立执行与费用。"
        "请求未知时用 requests 查询原 request_id，不自动重放。"
        "不接受调用者、owner、授权或人工已读参数。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=True,
            user_facing_name="周期计划",
            search_hint="routines recurring cron trading calendar preview budget history",
        )

    @property
    def parameters_schema(self):
        return request_schema()

    def parse_arguments(self, raw):
        try:
            if isinstance(raw, str):
                if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
                    raise ValueError()
                raw = strict_json_loads(raw)
            return validate_routine_arguments(raw)
        except (PersonalAgentError, ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("周期工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            arguments = validate_routine_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_routine_operation(
                self.service, arguments, caller_actor=actor, model_output=True
            )
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            code = exc.code
        except (ValueError, TypeError, UnicodeError, RecursionError):
            code = "invalid_input"
        except Exception:
            code = "routine_unknown"
        raise ToolExecutionError(code, public_error(code))
