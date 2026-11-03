"""The same personal-agent service exposed as one discoverable tool."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

_CREATE = {"name", "description", "instructions", "model_tier", "tool_names", "kind", "member_ids"}
_FIELDS = {
    "tools": set(),
    "list": {"include_archived"},
    "get": {"agent_id"},
    "history": {"agent_id"},
    "open": {"agent_id"},
    "create": _CREATE | {"request_id"},
    "update": (_CREATE - {"kind"}) | {"agent_id", "expected_version", "status"},
}
_REQUIRED = {
    "tools": set(),
    "list": set(),
    "get": {"agent_id"},
    "history": {"agent_id"},
    "open": {"agent_id"},
    "create": {"name", "instructions", "request_id"},
    "update": {"agent_id", "expected_version"},
}


class PersonalAgentsTool(Tool):
    def __init__(self, service: PersonalAgentService) -> None:
        self.service = service

    @property
    def name(self) -> str:
        return "personal_agents"

    @property
    def description(self) -> str:
        return (
            "按用户明确要求管理长期存在的个人专员或团队。初始没有预置专员。"
            "可查看、创建、修改、查看历史和打开独立会话。创建须提供稳定 request_id；"
            "修改须提供当前 expected_version。工具列表为空代表无工具权限。"
            "团队目前仅保存档案，不声明已具备协作执行能力。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=True,
            user_facing_name="管理个人助理",
            search_hint="personal agents persistent specialist team create update session",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": list(_FIELDS)},
                "agent_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "name": {"type": "string", "minLength": 1, "maxLength": 80},
                "description": {"type": "string", "maxLength": 500},
                "instructions": {"type": "string", "minLength": 1, "maxLength": 12000},
                "model_tier": {"type": "string", "enum": ["fast", "capable", "reasoning"]},
                "tool_names": {
                    "type": "array",
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "member_ids": {
                    "type": "array",
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                },
                "kind": {"type": "string", "enum": ["specialist", "team"]},
                "status": {"type": "string", "enum": ["active", "paused", "archived"]},
                "expected_version": {"type": "integer", "minimum": 1},
                "include_archived": {"type": "boolean"},
            },
            "required": ["operation"],
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            if not Draft202012Validator(self.parameters_schema).is_valid(kwargs):
                raise PersonalAgentError("invalid_input", "助理操作参数类型、长度或字段不正确")
            fields = dict(kwargs)
            operation = fields.pop("operation")
            if set(fields) - _FIELDS[operation] or not _REQUIRED[operation] <= set(fields):
                raise PersonalAgentError("invalid_input", "当前操作缺少必填字段或包含无关字段")
            if operation == "tools":
                result = {"tools": self.service.assignable_tools()}
            elif operation == "list":
                result = {"agents": [agent.to_dict() for agent in self.service.list(**fields)]}
            elif operation == "get":
                result = self.service.get(fields["agent_id"]).to_dict()
            elif operation == "history":
                result = {
                    "revisions": [a.to_dict() for a in self.service.history(fields["agent_id"])]
                }
            elif operation == "create":
                result = self.service.create(**fields).to_dict()
            elif operation == "update":
                agent_id = fields.pop("agent_id")
                expected = fields.pop("expected_version")
                result = self.service.update(agent_id, expected, **fields).to_dict()
            else:
                result = await self.service.open_session(fields["agent_id"])
        except PersonalAgentError as exc:
            raise ToolExecutionError(exc.code, exc.message) from exc
        return json.dumps(result, ensure_ascii=False)
