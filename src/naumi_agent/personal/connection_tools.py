"""Strict calendar payloads and identity-bound, non-administrative model tools."""

from __future__ import annotations

import copy
import json
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MAX_REQUEST_BYTES = 128 * 1024
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
ConnectionId = Annotated[str, Field(pattern=r"^pc_[a-f0-9]{32}$")]
ActorId = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class RequestPayload(StrictPayload):
    request_id: RequestId


class MutationPayload(RequestPayload):
    expected_revision: Revision


class ClientPayload(RequestPayload):
    client_json: str = Field(min_length=1, max_length=16384)


class OAuthPayload(RequestPayload):
    connection_id: ConnectionId | None = None
    expected_revision: Revision | None = None
    write: bool

    @model_validator(mode="after")
    def exact_reconnect(self):
        if (self.connection_id is None) != (self.expected_revision is None):
            raise ValueError("重新连接需要原连接及其版本")
        return self


class GrantPayload(MutationPayload):
    actor_id: ActorId
    actor_version: int = Field(strict=True, ge=1, le=2**31 - 1)


class RevokePayload(MutationPayload):
    actor_id: ActorId
    actor_version: int | None = Field(default=None, strict=True, ge=1, le=2**31 - 1)


class DisconnectPayload(MutationPayload):
    remote_revoke: bool


class SchedulePayload(StrictPayload):
    mode: Literal["now", "once"]
    timezone: str = Field(min_length=1, max_length=128)
    local_time: str | None = Field(default=None, min_length=1, max_length=64)
    fold: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None
    misfire_policy: Literal["run_once"]
    grace_seconds: int = Field(strict=True, ge=3600, le=3600)


class ToolEventItemPayload(RequestPayload):
    event_revision: Revision
    title: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=12000)
    schedule: SchedulePayload


class EventItemPayload(ToolEventItemPayload):
    actor_id: ActorId


class AllDayPoint(StrictPayload):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class TimedPoint(StrictPayload):
    dateTime: str = Field(min_length=1, max_length=80)  # noqa: N815
    timeZone: str = Field(min_length=1, max_length=100)  # noqa: N815


class ActionBody(StrictPayload):
    summary: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20000)
    start: AllDayPoint | TimedPoint
    end: AllDayPoint | TimedPoint


class SourceRef(StrictPayload):
    connection_id: ConnectionId
    auth_epoch: Revision
    event_id: str = Field(min_length=1, max_length=1024)
    revision: Revision
    payload_sha256: Digest
    occurrence_key: str = Field(min_length=1, max_length=2048)
    grant_id: str = Field(default="", max_length=64)
    grant_revision: int = Field(default=0, strict=True, ge=0, le=2**53 - 1)


class ToolActionProposal(RequestPayload):
    connection_id: ConnectionId
    source_refs: list[SourceRef] = Field(max_length=64)
    operation: Literal["insert", "update"]
    target_event_id: str | None = Field(default=None, min_length=1, max_length=1024)
    base_etag: str | None = Field(default=None, min_length=1, max_length=512)
    body: ActionBody
    expires_at: str = Field(min_length=1, max_length=80)


class ActionProposal(ToolActionProposal):
    actor_id: ActorId | None = None


class ActionRevision(MutationPayload):
    body: ActionBody
    expires_at: str = Field(min_length=1, max_length=80)


class ActionApproval(MutationPayload):
    expected_digest: Digest


def payload(model, value):
    try:
        return model.model_validate(value).model_dump(exclude_unset=True, exclude_none=True)
    except ValidationError:
        pass
    raise PersonalAgentError("invalid_input", "连接请求字段不完整或格式不正确")


def _schema(operations):
    definitions, branches = {}, []
    for operation, (fields, required, body_type) in operations.items():
        properties = {"operation": {"const": operation}, **copy.deepcopy(fields)}
        if body_type is not None:
            body = body_type.model_json_schema()
            definitions.update(body.pop("$defs", {}))
            properties["payload"] = body
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": ["operation", *required],
            }
        )
    return {"type": "object", "oneOf": branches, "$defs": definitions}


_CONNECTION = {"type": "string", "pattern": r"^pc_[a-f0-9]{32}$"}
_ACTION = {"type": "string", "pattern": r"^ca_[a-f0-9]{32}$"}
_CONNECTION_OPERATIONS = {
    "context": ({}, [], None),
    "list": ({}, [], None),
    "events": (
        {"connection_id": _CONNECTION, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        ["connection_id"],
        None,
    ),
    "sync": ({"connection_id": _CONNECTION}, ["connection_id", "payload"], MutationPayload),
    "event_to_item": (
        {
            "connection_id": _CONNECTION,
            "event_id": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
        ["connection_id", "event_id", "payload"],
        ToolEventItemPayload,
    ),
}
_ACTION_OPERATIONS = {
    "list_actions": ({}, [], None),
    "get_action": ({"action_id": _ACTION}, ["action_id"], None),
    "prepare_action": ({}, ["payload"], ToolActionProposal),
    "reconcile": ({"action_id": _ACTION}, ["action_id", "payload"], MutationPayload),
}


class _ConnectionTool(Tool):
    def __init__(self, service):
        self.service = service
        self.engine = service.engine

    @property
    def metadata(self):
        return ToolMetadata(requires_confirmation=False, user_facing_name="日程连接")

    def parse_arguments(self, raw):
        failed = False
        try:
            if isinstance(raw, str):
                if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
                    raise ValueError()
                raw = strict_json_loads(raw)
            if not isinstance(raw, dict):
                raise ValueError()
            return dict(raw)
        except (PersonalAgentError, ValueError, TypeError, UnicodeError):
            failed = True
        if failed:
            raise ValueError("日程工具参数格式不正确")

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        error = None
        try:
            if list(Draft202012Validator(self.parameters_schema).iter_errors(raw)):
                raise PersonalAgentError("invalid_input", "日程工具参数不完整或包含未允许的字段")
            if (
                len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode())
                > MAX_REQUEST_BYTES
            ):
                raise PersonalAgentError("invalid_input", "日程工具参数超过容量限制")
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await self._dispatch(raw, actor)
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            error = (exc.code, " ".join(exc.message.split())[:300])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            error = ("invalid_input", "日程工具参数格式不正确")
        except Exception:
            error = ("connection_unknown", "日程操作结果待核对，请查询原请求")
        if error:
            raise ToolExecutionError(error[0], error[1] or "日程操作未完成")


class PersonalConnectionsTool(_ConnectionTool):
    name = "personal_connections"
    description = "查看已授权日程来源、同步只读镜像或将确切日程转为事项。账户连接与授权由用户管理。"

    @property
    def parameters_schema(self):
        return _schema(_CONNECTION_OPERATIONS)

    async def _dispatch(self, raw, actor):
        operation = raw["operation"]
        if operation == "context":
            return await self.service.context()
        if operation == "list":
            return await self.service.list_connections(actor_id=actor)
        identity = raw["connection_id"]
        if operation == "events":
            if type(raw.get("limit", 20)) is not int:
                raise PersonalAgentError("invalid_input", "日程条数需要有效整数")
            return await self.service.tool_events(identity, actor, limit=raw.get("limit", 20))
        if operation == "sync":
            return await self.service.sync(
                identity, payload(MutationPayload, raw["payload"]), caller_actor=actor
            )
        return await self.service.event_to_item(
            identity,
            raw["event_id"],
            payload(ToolEventItemPayload, raw["payload"]),
            caller_actor=actor,
        )


class PersonalActionsTool(_ConnectionTool):
    name = "personal_actions"
    description = "提出日程写入草稿、查看自己的提案或核对原请求；执行需用户批准精确版本与摘要。"

    @property
    def parameters_schema(self):
        return _schema(_ACTION_OPERATIONS)

    async def _dispatch(self, raw, actor):
        operation = raw["operation"]
        if operation == "list_actions":
            result = await self.service.list_actions(caller_actor=actor)
            return {
                "actions": [
                    {
                        key: value.get(key)
                        for key in (
                            "id",
                            "revision",
                            "status",
                            "digest",
                            "operation",
                            "created_at",
                            "updated_at",
                        )
                    }
                    for value in result["actions"]
                ],
                "content_transport": "get_action",
            }
        if operation == "get_action":
            result = await self.service.get_action(raw["action_id"], caller_actor=actor)
        elif operation == "prepare_action":
            result = await self.service.prepare_action(
                payload(ToolActionProposal, raw["payload"]), caller_actor=actor
            )
        else:
            result = await self.service.reconcile_action(
                raw["action_id"], payload(MutationPayload, raw["payload"]), caller_actor=actor
            )
        return await self.service.pin_action_read(result, actor)
