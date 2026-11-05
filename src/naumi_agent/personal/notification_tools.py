"""Strict notification adapters over one service and source-pinned model reads."""

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
MANUAL_CHANNELS = frozenset({"web", "cli", "tui"})
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
BatchId = Annotated[str, Field(pattern=r"^nb_[a-f0-9]{32}$")]
EventId = Annotated[str, Field(pattern=r"^ne_[a-f0-9]{32}$")]
ClientId = Annotated[str, Field(pattern=r"^nc_[a-f0-9]{32}$")]
ClaimId = Annotated[str, Field(pattern=r"^no_[a-f0-9]{32}$")]
ActorId = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class QuietWindow(StrictPayload):
    weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(
        min_length=1, max_length=7
    )
    start: ClockTime
    end: ClockTime
    full_day: bool

    @model_validator(mode="after")
    def exact_window(self):
        if len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("勿扰日期重复")
        if (self.start == self.end) is not self.full_day:
            raise ValueError("全天勿扰需要相同的起止时刻")
        return self


class SetPreferencesPayload(StrictPayload):
    request_id: RequestId
    expected_revision: Revision
    enabled: bool
    timezone: str = Field(min_length=1, max_length=128)
    quiet_hours: list[QuietWindow] = Field(max_length=14)
    muted_actor_ids: list[ActorId] = Field(max_length=1000)
    debounce_seconds: Annotated[int, Field(strict=True, ge=0, le=300)]
    max_delay_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)]
    bypass_kinds: list[Literal["item_failure", "calendar_approval"]] = Field(max_length=2)
    include_existing: bool

    @model_validator(mode="after")
    def exact_preferences(self):
        if self.max_delay_seconds < self.debounce_seconds:
            raise ValueError("最大等待时间小于合并时间")
        for values in (self.muted_actor_ids, self.bypass_kinds):
            if len(values) != len(set(values)):
                raise ValueError("通知设置包含重复值")
        windows = [
            (tuple(sorted(window.weekdays)), window.start, window.end, window.full_day)
            for window in self.quiet_hours
        ]
        if len(windows) != len(set(windows)):
            raise ValueError("勿扰窗口重复")
        return self


class EventRef(StrictPayload):
    event_id: EventId
    event_digest: Digest


class ClaimPayload(StrictPayload):
    request_id: RequestId
    batch_id: BatchId
    expected_digest: Digest
    client_id: ClientId


class _ExactMembers(StrictPayload):
    event_refs: list[EventRef] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_events(self):
        identities = [value.event_id for value in self.event_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("通知成员重复")
        return self


class PresentPayload(_ExactMembers):
    request_id: RequestId
    batch_id: BatchId
    batch_digest: Digest
    client_id: ClientId
    claim_id: ClaimId


class MarkReadPayload(_ExactMembers):
    request_id: RequestId


class ListArguments(StrictPayload):
    filter: Literal["unread", "attention", "all"] = "unread"
    limit: PageSize = 50
    after: str | None = Field(default=None, min_length=1, max_length=8192)


class HumanListArguments(ListArguments):
    actor_id: ActorId | None = None


class BatchArguments(StrictPayload):
    batch_id: BatchId


class RequestArguments(StrictPayload):
    request_id: RequestId


_READS = {
    "context": StrictPayload,
    "list": ListArguments,
    "preferences": StrictPayload,
    "get-batch": BatchArguments,
    "requests": RequestArguments,
}
_WRITES = {
    "set_preferences": SetPreferencesPayload,
    "claim": ClaimPayload,
    "present": PresentPayload,
    "mark_read": MarkReadPayload,
}


def _operations(human):
    arguments = dict(_READS)
    if human:
        arguments.update(list=HumanListArguments, prompt=StrictPayload)
        for operation, payload in _WRITES.items():
            arguments[operation] = create_model(
                operation.title() + "NotificationArguments",
                __base__=StrictPayload,
                payload=(payload, ...),
            )
    return {
        operation: create_model(
            ("Human" if human else "Model") + operation.title() + "NotificationOperation",
            __base__=argument,
            operation=(Literal[operation], ...),
        )
        for operation, argument in arguments.items()
    }


_MODELS, _HUMAN_MODELS = _operations(False), _operations(True)


def request_schema():
    definitions, branches = {}, []
    for model in _MODELS.values():
        branch = copy.deepcopy(model.model_json_schema())
        definitions.update(branch.pop("$defs", {}))
        branches.append(branch)
    return {"type": "object", "oneOf": branches, "$defs": definitions}


def validate_notification_arguments(value, *, human=False):
    try:
        models = _HUMAN_MODELS if human else _MODELS
        if type(value) is not dict or type(value.get("operation")) is not str:
            raise ValueError()
        if value["operation"] not in models:
            raise ValueError()
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError()
        return models[value["operation"]].model_validate(value).model_dump(exclude_unset=True)
    except (ValidationError, TypeError, ValueError, UnicodeError, RecursionError):
        raise PersonalAgentError(
            "invalid_input", "通知请求字段不完整、格式不正确或超过容量上限"
        ) from None


_ERRORS = {
    "invalid_input": "通知请求字段不完整或格式不正确",
    "not_found": "原通知不在当前范围，请核对原编号",
    "scope_denied": "当前身份没有这份通知或来源的操作范围",
    "version_conflict": "通知设置或状态已变化，请刷新后核对原修改",
    "binding_conflict": "原通知身份或来源已变化，请核对原记录",
    "idempotency_conflict": "原请求编号已绑定其他内容，请核对原完整请求",
    "immutable_change": "原通知记录保持不变，请提交新的确切修改",
    "storage_error": "原通知资料暂未就绪，请恢复原资料库后核对请求",
    "configuration_error": "通知服务尚未就绪，请检查运行时配置",
    "source_unavailable": "原通知来源暂不可读，请核对原记录与分享范围",
    "source_stale": "原通知来源已变化，请核对原确切版本",
    "limit_exceeded": "通知请求已达到容量上限，请减少本次条目",
    "notification_claimed": "该通知已由其他窗口领取，请从通知列表查看",
    "notification_unavailable": "该通知已处理、来源已变化或正在勿扰时段，请刷新后查看",
    "notification_busy": "通知状态正在更新，请刷新后核对原请求",
    "clock_rollback": "系统时钟发生回退，主动提示已暂停",
    "timezone_data_changed": "时区规则已变化，请重新核对并保存勿扰设置",
    "notification_unknown": "通知操作结果待核对，请查询原请求编号，不要重复新建请求",
}


def public_code(code):
    return code if type(code) is str and code in _ERRORS else "notification_unknown"


def public_error(code):
    return _ERRORS[public_code(code)]


def accepted_projection_receipt(result, arguments):
    """Preserve a committed mutation without leaking a failed body projection."""
    if type(result) is not dict or result.get("accepted") is not True:
        return None
    if arguments.get("operation") not in _WRITES:
        return None
    request = arguments.get("payload", {}).get("request_id")
    if type(request) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", request):
        return None
    identity, raw = {}, result.get("identity", {})
    if type(raw) is dict:
        patterns = {
            "preferences_id": r"np_[a-f0-9]{32}",
            "batch_id": r"nb_[a-f0-9]{32}",
            "batch_digest": r"[a-f0-9]{64}",
            "client_id": r"nc_[a-f0-9]{32}",
            "presentation_id": r"ns_[a-f0-9]{32}",
            "claim_id": r"no_[a-f0-9]{32}",
        }
        for key, pattern in patterns.items():
            value = raw.get(key)
            if type(value) is str and re.fullmatch(pattern, value):
                identity[key] = value
        revision = raw.get("preferences_revision")
        if type(revision) is int and 0 <= revision <= 2**53 - 1:
            identity["preferences_revision"] = revision
        refs = raw.get("event_refs")
        if type(refs) is list:
            try:
                exact = _ExactMembers.model_validate({"event_refs": refs})
                identity["event_refs"] = exact.model_dump()["event_refs"]
            except ValidationError:
                pass
        values = raw.get("notice_read_ids")
        if (
            type(values) is list
            and len(values) <= 100
            and all(
                type(value) is str and re.fullmatch(r"nr_[a-f0-9]{32}", value) for value in values
            )
            and len(values) == len(set(values))
        ):
            identity["notice_read_ids"] = list(values)
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
        raise PersonalAgentError(
            "notification_unknown", public_error("notification_unknown")
        ) from None


async def execute_notification_operation(
    service, arguments, *, caller_actor=None, trusted_channel=None
):
    """Keep caller and channel outside user/model-controlled envelopes."""
    human = caller_actor is None
    fields = validate_notification_arguments(arguments, human=human)
    operation = fields.pop("operation")
    if not human and (
        type(caller_actor) is not str or not re.fullmatch(r"main|pa_[a-f0-9]{32}", caller_actor)
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if trusted_channel is not None and (
        not human or type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if operation in _WRITES:
        if trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        return await getattr(service, operation)(
            fields["payload"], caller_actor=None, trusted_channel=trusted_channel
        )
    if operation == "get-batch":
        return await service.get_batch(fields["batch_id"], caller_actor=caller_actor)
    if operation == "requests":
        return await service.request_status(fields["request_id"], caller_actor=caller_actor)
    return await getattr(service, operation)(**fields, caller_actor=caller_actor)


class PersonalNotificationsTool(Tool):
    name = "personal_notifications"
    description = (
        "只读查看当前 Agent 的来源绑定通知。context 查询运行时，list 查询通知身份，"
        "get-batch 读取确切批次，preferences 查看设置，requests 核对原请求。"
        "读取不代表用户已读，不领取或呈现提示，不修改设置，不批准日历操作。"
        "不接受调用者、Agent筛选、可信通道或管理员覆盖参数。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="通知",
            search_hint="notifications batch unread attention quiet hours dnd requests",
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
            return validate_notification_arguments(raw)
        except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("通知工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            arguments = validate_notification_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_notification_operation(
                self.service, arguments, caller_actor=actor
            )
            projected = await self.service.model_result(
                arguments["operation"], result, actor, arguments
            )
            return json.dumps(
                serializable_result(projected, arguments), ensure_ascii=False, allow_nan=False
            )
        except PersonalAgentError as exc:
            code = public_code(exc.code)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            code = "invalid_input"
        except Exception:
            code = "notification_unknown"
        raise ToolExecutionError(code, public_error(code))


PersonalNotificationTool = PersonalNotificationsTool
