"""Actor-bound item operations over the shared durable personal-item service."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

_DEFINITION = {
    "request_id",
    "title",
    "instructions",
    "tags",
    "assignee_id",
    "assignee_version",
    "schedule",
    "input_refs",
    "directories",
    "policy",
}
_CREATE_REQUIRED = {
    "request_id",
    "title",
    "instructions",
    "assignee_id",
    "assignee_version",
    "schedule",
}
FIELDS = {
    "list": {"state", "query", "limit", "offset"},
    "get": {"item_id"},
    "create": _DEFINITION,
    "update": _DEFINITION | {"item_id", "expected_revision"},
    **{
        name: {"item_id", "expected_revision", "request_id"}
        for name in ("pause", "resume", "cancel", "run_now", "close")
    },
    "restart": {"item_id", "expected_revision", "request_id", "schedule"},
    "history": {"item_id"},
    "occurrences": {"item_id"},
    "results": {"unread", "limit", "offset"},
    "result": {"occurrence_id"},
}
REQUIRED = {
    "list": set(),
    "get": {"item_id"},
    "create": _CREATE_REQUIRED,
    "update": _CREATE_REQUIRED | {"item_id", "expected_revision"},
    **{
        name: {"item_id", "expected_revision", "request_id"}
        for name in ("pause", "resume", "cancel", "run_now", "restart", "close")
    },
    "history": {"item_id"},
    "occurrences": {"item_id"},
    "results": set(),
    "result": {"occurrence_id"},
}


def request_schema() -> dict:
    """Publish strict model inputs without human acknowledgement or trusted identity fields."""

    def string(maximum):
        return {"type": "string", "minLength": 1, "maxLength": maximum}

    def number(minimum, maximum):
        return {"type": "integer", "minimum": minimum, "maximum": maximum}

    ref = {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_id", "version", "sha256", "grant_id", "grant_revision"],
        "properties": {
            "artifact_id": {"type": "string", "pattern": "^art_[a-f0-9]{32}$"},
            "version": number(1, 10000),
            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "grant_id": {"type": "string", "pattern": "^(|asg_[a-f0-9]{32})$"},
            "grant_revision": number(0, 2**31 - 1),
        },
    }
    schedule = {
        "type": "object",
        "additionalProperties": False,
        "required": ["mode", "timezone"],
        "properties": {
            "mode": {"type": "string", "enum": ["now", "once"]},
            "local_time": string(64),
            "timezone": string(128),
            "fold": {"type": ["integer", "null"], "enum": [0, 1, None]},
            "misfire_policy": {"type": "string", "enum": ["run_once", "skip"]},
            "grace_seconds": number(0, 604800),
        },
    }
    policy = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool_names": {
                "type": "array",
                "uniqueItems": True,
                "maxItems": 5,
                "items": {
                    "type": "string",
                    "enum": ["personal_artifacts", "code_execute", "bash_run", "file_read", "read"]
                },
            },
            "artifact_operations": {
                "type": "array",
                "uniqueItems": True,
                "maxItems": 3,
                "items": {"type": "string", "enum": ["get", "read", "publish"]},
            },
            "model_tier": {"type": "string", "enum": ["fast", "capable", "reasoning"]},
            "max_turns": number(1, 100),
            "max_cost_microusd": number(0, 100000000),
            "timeout_seconds": number(1, 3600),
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": list(FIELDS)},
            "item_id": {"type": "string", "pattern": "^pi_[a-f0-9]{32}$"},
            "occurrence_id": {"type": "string", "pattern": "^pio_[a-f0-9]{32}$"},
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"},
            "expected_revision": number(1, 2**31 - 1),
            "title": string(120),
            "instructions": string(12000),
            "tags": {"type": "array", "uniqueItems": True, "maxItems": 16, "items": string(80)},
            "assignee_id": {"type": "string", "pattern": "^(main|pa_[a-f0-9]{32})$"},
            "assignee_version": number(0, 2**31 - 1),
            "schedule": schedule,
            "input_refs": {"type": "array", "maxItems": 64, "items": ref},
            "directories": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "pattern", "max_files", "max_bytes"],
                    "properties": {
                        "path": string(4096),
                        "pattern": string(256),
                        "max_files": number(1, 100),
                        "max_bytes": number(1, 50 * 1024**2),
                    },
                },
            },
            "policy": policy,
            "state": {"type": "string", "enum": ["active", "paused", "cancelled", "closed"]},
            "query": {"type": "string", "maxLength": 200},
            "unread": {"type": "boolean"},
            "limit": number(1, 100),
            "offset": number(0, 2**31 - 1),
        },
    }


def validate_item_arguments(value: dict) -> dict:
    operation = value.get("operation")
    if (
        list(Draft202012Validator(request_schema()).iter_errors(value))
        or operation not in FIELDS
        or set(value) - {"operation"} - FIELDS[operation]
        or not REQUIRED[operation] <= set(value)
    ):
        raise PersonalAgentError("invalid_input", "事项操作包含无效字段或缺少必要参数")
    return value


class PersonalItemsTool(Tool):
    name = "personal_items"
    description = (
        "管理当前 Agent 的持久事项与结果。create/update 提交真实要求、确切输入版本和单次时间；"
        "list/get/history/occurrences/results/result 读取真实事项、Job、Run 与报告。"
        "pause/resume/cancel/run_now 控制原事项；restart 只对已知终态明确再次执行。"
        "写操作必须保留稳定 request_id 和原完整参数重试，UNKNOWN 不代表未受理。"
        "不接受调用者身份、执行路由或人类已读标记；读取结果不会确认已读。"
    )

    def __init__(self, service):
        self.service = service
        self.engine = service.engine

    @property
    def parameters_schema(self):
        return request_schema()

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=True,
            user_facing_name="事项",
            search_hint="personal items one-shot schedule inbox report run history pause resume",
        )

    def parse_arguments(self, raw):
        if isinstance(raw, str):
            try:
                raw = strict_json_loads(raw)
            except PersonalAgentError as exc:
                raise ValueError(exc.message) from exc
        return super().parse_arguments(raw)

    async def execute(self, operation: str, **raw):
        from naumi_agent.personal.runtime import require_current_agent

        try:
            validate_item_arguments({"operation": operation, **raw})
            profile = require_current_agent(self.engine)
            actor = profile.id if profile is not None else "main"
            fields = dict(raw)
            if operation in {"list", "results"}:
                result = await getattr(self.service, operation)(**fields, caller_actor=actor)
            elif operation == "create":
                session = getattr(self.engine, "_session", None)
                if session is not None:
                    fields["origin_session_id"] = session.id
                result = await self.service.create(fields, caller_actor=actor)
            elif operation == "update":
                item_id = fields.pop("item_id")
                result = await self.service.update(item_id, fields, caller_actor=actor)
            elif operation in {"pause", "resume", "cancel", "run_now", "restart", "close"}:
                item_id = fields.pop("item_id")
                action = "run-now" if operation == "run_now" else operation
                result = await self.service.control(item_id, action, fields, caller_actor=actor)
            elif operation == "result":
                result = await self.service.result(fields["occurrence_id"], caller_actor=actor)
            else:
                result = await getattr(self.service, operation)(
                    fields["item_id"], caller_actor=actor
                )
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except PersonalAgentError as exc:
            message = " ".join(exc.message.split())[:300] or "事项操作未完成"
            raise ToolExecutionError(exc.code, message) from exc
