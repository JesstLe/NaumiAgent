"""Strict federation adapters and immutable, source-bound model reads."""

from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Literal

from pydantic import Field, create_model

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_models import (
    MAX_REQUEST_BYTES,
    Actor,
    ConnectionId,
    ConnectPayload,
    DisconnectPayload,
    IssuePayload,
    ItemId,
    RequestId,
    RevokePayload,
    Strict,
    endpoint_url,
    error,
    normalize,
    timestamp,
)
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

MANUAL_CHANNELS = frozenset({"web", "cli", "tui"})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class OverviewArguments(Strict):
    connection_id: ConnectionId | None = None
    after: str | None = Field(default=None, min_length=1, max_length=8192)
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class ResultArguments(Strict):
    connection_id: ConnectionId
    item_id: ItemId


class RecipientArguments(Strict):
    actor_id: Actor


class RequestArguments(Strict):
    request_id: RequestId


_READS = {
    "context": Strict,
    "list": Strict,
    "overview": OverviewArguments,
    "result": ResultArguments,
}
_WRITES = {
    "issue": IssuePayload,
    "revoke": RevokePayload,
    "connect": ConnectPayload,
    "disconnect": DisconnectPayload,
}


def _operations(human):
    arguments = dict(_READS)
    if human:
        arguments.update(recipient=RecipientArguments, grants=Strict, requests=RequestArguments)
        for operation, payload in _WRITES.items():
            arguments[operation] = create_model(
                operation.title() + "WorkspaceArguments", __base__=Strict, payload=(payload, ...)
            )
    return {
        operation: create_model(
            ("Human" if human else "Model") + operation.title() + "WorkspaceOperation",
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


def validate_workspace_arguments(raw, *, human=False):
    models = _HUMAN_MODELS if human else _MODELS
    if type(raw) is not dict or type(raw.get("operation")) is not str:
        raise error("invalid_input")
    model = models.get(raw["operation"])
    if model is None:
        raise error("invalid_input")
    normalized = normalize(model, raw)
    payload = normalized.get("payload", {})
    if raw["operation"] == "connect":
        payload["endpoint"] = endpoint_url(payload["endpoint"])
        timestamp(payload["ticket"]["expires_at"])
    elif raw["operation"] == "issue":
        timestamp(payload["expires_at"])
    return normalized


def serializable_result(result):
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise error("workspace_unknown") from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise error("input_limit")
    return result


async def execute_workspace_operation(
    service, arguments, *, caller_actor=None, trusted_channel=None
):
    """Keep model identity and human authority out of the JSON envelope."""
    human = caller_actor is None
    normalized = validate_workspace_arguments(arguments, human=human)
    if not human and (
        type(caller_actor) is not str or not re.fullmatch(r"main|pa_[a-f0-9]{32}", caller_actor)
    ):
        raise error("scope_denied")
    if trusted_channel is not None and (
        not human or type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS
    ):
        raise error("scope_denied")
    if normalized["operation"] in _WRITES and trusted_channel not in MANUAL_CHANNELS:
        raise error("scope_denied")
    return await service.execute_operation(
        normalized, caller_actor=caller_actor, trusted_channel=trusted_channel
    )


class PersonalWorkspacesTool(Tool):
    name = "personal_workspaces"
    description = (
        "只读汇总用户确切授权给当前 Agent 的其他工作区。context 查看连接状态，"
        "list 查看当前 Agent 的连接，overview 分来源读取事项，result 读取原事项结果。"
        "内容始终绑定原授权、原 Agent 和原产物；来源离线或授权变化时停止读取。"
        "不创建执行器，不批准、不勾选、不修改原事项，不签发或修改工作区授权。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def metadata(self):
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="工作区",
            search_hint="workspaces federation overview result source agent",
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
            return validate_workspace_arguments(raw)
        except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("工作区工具参数格式不正确或超过容量上限") from None

    async def execute(self, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            arguments = validate_workspace_arguments(raw)
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            result = await execute_workspace_operation(self.service, arguments, caller_actor=actor)
            projected = await self.service.model_result(
                arguments["operation"], result, actor, arguments
            )
            return json.dumps(serializable_result(projected), ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            failure = error(exc.code)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            failure = error("invalid_input")
        except Exception:
            failure = error("workspace_unknown")
        raise ToolExecutionError(failure.code, failure.message)
