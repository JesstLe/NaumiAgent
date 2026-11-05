"""Strict human followup envelopes and read-only source-pinned model adapters."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.connection_tools import ActionBody
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_schedule import _zone
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MANUAL_CHANNELS = frozenset({"web", "cli", "tui"})
Actor = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
RuleId = Annotated[str, Field(pattern=r"^fr_[a-f0-9]{32}$")]
ProposalId = Annotated[str, Field(pattern=r"^fp_[a-f0-9]{32}$")]
EffectId = Annotated[str, Field(pattern=r"^fe_[a-f0-9]{32}$")]
CursorId = Annotated[str, Field(pattern=r"^fc_[a-f0-9]{32}$")]
ItemId = Annotated[str, Field(pattern=r"^pi_[a-f0-9]{32}$")]
ConnectionId = Annotated[str, Field(pattern=r"^pc_[a-f0-9]{32}$")]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
PositiveRevision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _unique(values):
    if len(values) != len(set(values)):
        raise ValueError("选项重复")


def _utc(value):
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("需要明确 UTC 时刻")
    return value


class DeadlineTrigger(StrictPayload):
    kind: Literal["deadline"]
    item_ids: list[ItemId] = Field(max_length=100)
    offsets_minutes: list[Annotated[int, Field(strict=True, ge=-10080, le=10080)]] = Field(
        min_length=1, max_length=8
    )

    @model_validator(mode="after")
    def exact_values(self):
        _unique(self.item_ids)
        _unique(self.offsets_minutes)
        return self


class CalendarTrigger(StrictPayload):
    kind: Literal["calendar_change"]
    connection_id: ConnectionId
    event_id: str | None = Field(min_length=1, max_length=1024)
    change_kinds: list[Literal["created", "updated", "cancelled", "unavailable", "restored"]] = (
        Field(min_length=1, max_length=5)
    )
    fields: list[Literal["summary", "description", "start", "end", "status", "available"]] = Field(
        min_length=1, max_length=6
    )

    @model_validator(mode="after")
    def exact_values(self):
        _unique(self.change_kinds)
        _unique(self.fields)
        return self


class RuleBody(StrictPayload):
    name: str = Field(min_length=1, max_length=80)
    actor_id: Actor
    actor_version: Revision
    status: Literal["active", "paused"]
    timezone: str = Field(min_length=1, max_length=128)
    include_existing: bool
    max_lateness_minutes: Annotated[int, Field(strict=True, ge=1, le=10080)]
    freshness_seconds: Annotated[int, Field(strict=True, ge=60, le=604800)]
    misfire_policy: Literal["latest", "skip"]
    trigger: Annotated[DeadlineTrigger | CalendarTrigger, Field(discriminator="kind")]

    @model_validator(mode="after")
    def exact_rule(self):
        if (self.actor_id == "main") != (self.actor_version == 0):
            raise ValueError("Agent 编号与版本不匹配")
        if self.name != self.name.strip() or any(ord(char) < 32 for char in self.name):
            raise ValueError("规则名称格式不正确")
        _zone(self.timezone)
        return self


class ProposalRef(StrictPayload):
    proposal_id: ProposalId
    revision: Literal[1]
    digest: Digest
    workspace_key: Digest


class FollowupNotificationSource(StrictPayload):
    proposal_ref: ProposalRef
    decision_revision: Revision
    decision_digest: Digest
    presentation_epoch: Revision
    actor_id: Actor
    actor_version: Revision
    occurred_at: str = Field(min_length=1, max_length=80)
    store_instance: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]

    @model_validator(mode="after")
    def exact_identity(self):
        if (self.actor_id == "main") != (self.actor_version == 0):
            raise ValueError("原跟进 Agent 与版本不一致")
        if type(self.proposal_ref.revision) is not int:
            raise ValueError("原提案版本需要确切整数")
        _utc(self.occurred_at)
        return self


class TodayTarget(StrictPayload):
    kind: Literal["today_place"]
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    timezone: str = Field(min_length=1, max_length=128)
    expected_day_revision: Revision
    expected_metadata_revision: Revision

    @model_validator(mode="after")
    def exact_civil_date(self):
        date.fromisoformat(self.date)
        _zone(self.timezone)
        return self


class CalendarTarget(StrictPayload):
    kind: Literal["calendar_draft"]
    body: ActionBody
    expires_at: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def exact_expiry(self):
        _utc(self.expires_at)
        return self


class CreateRulePayload(StrictPayload):
    request_id: RequestId
    rule: RuleBody


class UpdateRulePayload(CreateRulePayload):
    rule_id: RuleId
    expected_revision: PositiveRevision


class SetStatusPayload(StrictPayload):
    request_id: RequestId
    rule_id: RuleId
    expected_revision: PositiveRevision
    status: Literal["active", "paused", "archived"]


class DecisionPayload(StrictPayload):
    request_id: RequestId
    proposal_ref: ProposalRef
    expected_decision_revision: Revision


class SnoozePayload(DecisionPayload):
    until: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def exact_until(self):
        _utc(self.until)
        return self


class AcceptPayload(DecisionPayload):
    target: Annotated[TodayTarget | CalendarTarget, Field(discriminator="kind")]


class ReconcilePayload(StrictPayload):
    request_id: RequestId
    effect_id: EffectId


class ListArguments(StrictPayload):
    status: Literal["pending", "all", "accepted", "dismissed", "snoozed"] = "pending"
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    after: CursorId | None = None


class HumanListArguments(ListArguments):
    actor_id: Actor | None = None


class GetArguments(StrictPayload):
    proposal_id: ProposalId


class RequestArguments(StrictPayload):
    request_id: RequestId


_READS = {
    "context": StrictPayload,
    "rules": StrictPayload,
    "list": ListArguments,
    "get": GetArguments,
}
_WRITES = {
    "create_rule": CreateRulePayload,
    "update_rule": UpdateRulePayload,
    "set_status": SetStatusPayload,
    "dismiss": DecisionPayload,
    "snooze": SnoozePayload,
    "accept": AcceptPayload,
    "reconcile": ReconcilePayload,
}


def _operations(human):
    arguments = dict(_READS)
    if human:
        arguments.update(list=HumanListArguments, requests=RequestArguments)
        for operation, payload in _WRITES.items():
            arguments[operation] = create_model(
                operation.title() + "FollowupArguments",
                __base__=StrictPayload,
                payload=(payload, ...),
            )
    return {
        operation: create_model(
            ("Human" if human else "Model") + operation.title() + "FollowupOperation",
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


def validate_followup_arguments(raw, *, human=False):
    try:
        models = _HUMAN_MODELS if human else _MODELS
        if type(raw) is not dict or type(raw.get("operation")) is not str:
            raise ValueError()
        model = models.get(raw["operation"])
        if model is None:
            raise ValueError()
        if (
            len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > MAX_REQUEST_BYTES
        ):
            raise ValueError()
        result = model.model_validate(raw).model_dump(exclude_unset=True)
        proposal = raw.get("payload", {}).get("proposal_ref")
        if proposal is not None and type(proposal.get("revision")) is not int:
            raise ValueError()
        return result
    except (
        PersonalAgentError,
        ValidationError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ):
        raise PersonalAgentError("invalid_input", public_error("invalid_input")) from None


_ERRORS = {
    "invalid_input": "跟进字段不完整、格式不正确或超过容量上限",
    "scope_denied": "请由用户确认这项跟进操作",
    "not_found": "这条规则、建议或原请求不在当前范围",
    "source_unavailable": "原跟进来源暂不可读，请核对原资料与授权",
    "source_stale": "原跟进来源已变化，请刷新后核对确切建议",
    "version_conflict": "规则、建议或目标版本已变化，请刷新后核对",
    "idempotency_conflict": "原请求编号已绑定其他内容，请核对原完整请求",
    "storage_error": "跟进资料与原目录不一致，请恢复原资料库后继续",
    "configuration_error": "跟进服务尚未就绪，请核对运行时配置",
    "followup_busy": "跟进状态正在更新，请稍后核对原请求",
    "followup_unknown": "操作结果待核对，请查询原请求编号",
    "input_limit": "跟进内容超过本次容量，请缩小范围后查看",
    "clock_rollback": "系统时钟发生回退，跟进扫描已暂停",
    "timezone_data_changed": "时区资料已变化，请重新核对原规则",
    "calendar_stale": "日历镜像需要重新同步，请核对当前原事件",
    "agent_inactive": "该 Agent 已停用，请恢复后再处理建议",
    "rule_inactive": "这条规则已暂停或归档，请刷新后核对",
    "proposal_resolved": "这条建议已处理，请查看原决策",
    "proposal_expired": "这条建议已到期，请查看当前原来源",
    "decision_conflict": "这条建议已有新的决策，请刷新后核对",
    "effect_blocked": "原目标条件已变化，请核对原接受记录",
}


def public_code(code):
    return code if type(code) is str and code in _ERRORS else "followup_unknown"


def public_error(code):
    return _ERRORS[public_code(code)]


def accepted_projection_receipt(result, arguments):
    if type(result) is not dict or result.get("accepted") is not True:
        return None
    if arguments.get("operation") not in _WRITES:
        return None
    request = arguments.get("payload", {}).get("request_id")
    if type(request) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", request):
        return None
    identity, raw = {}, result.get("identity", {})
    if type(raw) is dict:
        for key, prefix in (("rule_id", "fr"), ("proposal_id", "fp"), ("effect_id", "fe")):
            if type(raw.get(key)) is str and re.fullmatch(prefix + r"_[a-f0-9]{32}", raw[key]):
                identity[key] = raw[key]
        for key, minimum in (("rule_revision", 1), ("decision_revision", 0)):
            if type(raw.get(key)) is int and minimum <= raw[key] <= 2**53 - 1:
                identity[key] = raw[key]
    return {
        "accepted": True,
        "request_id": request,
        "operation": arguments["operation"],
        "identity": identity,
        "projection_available": False,
    }


def serializable_result(result, arguments):
    try:
        content = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError()
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        receipt = accepted_projection_receipt(result, arguments)
        if receipt is not None:
            return receipt
        raise PersonalAgentError("followup_unknown", public_error("followup_unknown")) from None


async def execute_followup_operation(
    service, arguments, *, caller_actor=None, trusted_channel=None
):
    human = caller_actor is None
    normalized = validate_followup_arguments(arguments, human=human)
    if not human and (
        type(caller_actor) is not str or not re.fullmatch(r"main|pa_[a-f0-9]{32}", caller_actor)
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if trusted_channel is not None and (
        not human or type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS
    ):
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    if normalized["operation"] in _WRITES and trusted_channel not in MANUAL_CHANNELS:
        raise PersonalAgentError("scope_denied", public_error("scope_denied"))
    return await service.execute_operation(
        normalized, caller_actor=caller_actor, trusted_channel=trusted_channel
    )


class PersonalFollowupsTool(Tool):
    name = "personal_followups"
    description = (
        "只读查看当前 Agent 的截止跟进规则与原建议。context 查看状态，rules 查看自身规则，"
        "list 查询建议，get 读取确切建议与原目标读回。"
        "只返回当前仍有授权的原来源；不创建规则，不采纳、忽略、稍后提醒，"
        "不安排执行，不批准日历写入。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="跟进",
            search_hint="followups deadline calendar proposal rules next steps",
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
            return validate_followup_arguments(raw)
        except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("跟进工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            arguments = validate_followup_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_followup_operation(self.service, arguments, caller_actor=actor)
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
            code = "followup_unknown"
        raise ToolExecutionError(code, public_error(code))
