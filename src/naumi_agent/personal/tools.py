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
    "collaborate": {"request_id", "source_session_id", "title", "context", "steps", "peer_agents"},
    "collaborations": {"source_session_id"},
    "collaboration": {"collaboration_id"},
    "cancel_collaboration": {"collaboration_id"},
    "collaboration_reply": {"collaboration_id", "question_id", "response"},
    "message_send": {
        "source_session_id",
        "request_id",
        "sender_agent_id",
        "sender_agent_version",
        "recipient_agent_id",
        "recipient_agent_version",
        "content",
        "context",
        "peer_agents",
    },
    "messages": {"collaboration_id"},
    "message": {"message_id"},
    "message_ack": {"message_id", "expected_response_sha256"},
    "team_send": {
        "source_session_id",
        "team_id",
        "team_version",
        "request_id",
        "content",
        "context",
        "source_ids",
        "member_grants",
    },
    "team_turns": {"team_session_id"},
    "team_turn": {"turn_id"},
    "cancel_team_turn": {"turn_id"},
}
_REQUIRED = {
    "tools": set(),
    "list": set(),
    "get": {"agent_id"},
    "history": {"agent_id"},
    "open": {"agent_id"},
    "create": {"name", "instructions", "request_id"},
    "update": {"agent_id", "expected_version"},
    "collaborate": {"request_id", "steps"},
    "collaborations": set(),
    "collaboration": {"collaboration_id"},
    "cancel_collaboration": {"collaboration_id"},
    "collaboration_reply": {"collaboration_id", "question_id", "response"},
    "message_send": {
        "request_id",
        "sender_agent_id",
        "sender_agent_version",
        "recipient_agent_id",
        "recipient_agent_version",
        "content",
    },
    "messages": {"collaboration_id"},
    "message": {"message_id"},
    "message_ack": {"message_id", "expected_response_sha256"},
    "team_send": {"team_id", "team_version", "request_id", "content"},
    "team_turns": set(),
    "team_turn": {"turn_id"},
    "cancel_team_turn": {"turn_id"},
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
            "按用户明确要求管理长期存在的个人Agent或团队。初始没有预置Agent。"
            "可查看、创建、修改、查看历史和打开独立会话。创建须提供稳定 request_id；"
            "修改须提供当前 expected_version。工具列表为空代表无工具权限。"
            "collaborate 将明确任务委派给已有Agent，steps 绑定AgentID与版本；"
            "depends_on 指定前置步骤，只共享显式 context 和前置结果。"
            "collaborations/collaboration 查看真实进度和交接；cancel_collaboration 请求停止。"
            "collaboration_reply 仅转交用户针对原 question_id 的真实回答，不替用户作决定。"
            "peer_agents 显式指定互询对象版本与工具范围，默认没有授权；省略工具范围只准只读与互询。"
            "message_send 以已有Agent发送真实消息；messages/message查询双方对话，"
            "message_ack确认回答。"
            "team_send向已有团队发送持久委托，team_turns/team_turn查询真实阶段与汇总；"
            "cancel_team_turn请求停止。团队规划、成员与汇总分别有真实Job/Run；"
            "member_grants省略只授予成员只读范围，写入必须明确授权。"
            "团队须显式配置personal_team才能分工；空工具团队只对话。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=True,
            user_facing_name="管理 Agent",
            search_hint="personal agents persistent specialist team create update session",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        schema = {
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
                "source_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "collaboration_id": {"type": "string", "pattern": "^pc_[0-9a-f]{32}$"},
                "question_id": {"type": "string", "pattern": "^ask-[0-9a-f]{32}$"},
                "response": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["option", "custom"]},
                        "value": {"type": "string", "maxLength": 80},
                        "custom_text": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["kind"],
                },
                "title": {"type": "string", "maxLength": 160},
                "context": {"type": "string", "maxLength": 32000},
                "content": {"type": "string", "minLength": 1, "maxLength": 16000},
                "sender_agent_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                "recipient_agent_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                "sender_agent_version": {"type": "integer", "minimum": 1},
                "recipient_agent_version": {"type": "integer", "minimum": 1},
                "message_id": {"type": "string", "pattern": "^pm_[0-9a-f]{32}$"},
                "turn_id": {"type": "string", "pattern": "^pt_[0-9a-f]{32}$"},
                "team_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                "team_version": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
                "team_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "source_ids": {
                    "type": "array",
                    "maxItems": 32,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "expected_response_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "peer_agents": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["agent_id", "agent_version"],
                        "properties": {
                            "agent_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                            "agent_version": {"type": "integer", "minimum": 1},
                            "tool_names": {
                                "type": "array",
                                "maxItems": 128,
                                "uniqueItems": True,
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                            "agent_id": {"type": "string", "pattern": "^pa_[0-9a-f]{32}$"},
                            "agent_version": {"type": "integer", "minimum": 1},
                            "task": {"type": "string", "minLength": 1, "maxLength": 16000},
                            "depends_on": {
                                "type": "array",
                                "maxItems": 11,
                                "uniqueItems": True,
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "agent_id", "agent_version", "task", "depends_on"],
                    },
                },
            },
            "required": ["operation"],
        }
        schema["properties"]["member_grants"] = schema["properties"]["peer_agents"].copy()
        return schema

    async def execute(self, **kwargs: Any) -> str:
        try:
            if not Draft202012Validator(self.parameters_schema).is_valid(kwargs):
                raise PersonalAgentError("invalid_input", "Agent 操作参数类型、长度或字段不正确")
            fields = dict(kwargs)
            operation = fields.pop("operation")
            if set(fields) - _FIELDS[operation] or not _REQUIRED[operation] <= set(fields):
                raise PersonalAgentError("invalid_input", "当前操作缺少必填字段或包含无关字段")
            if operation in {"team_send", "team_turns", "team_turn", "cancel_team_turn"}:
                service = self.service.engine.personal_teams
                if operation == "team_send":
                    session = await self.service.engine.get_or_create_session()
                    source_id = fields.pop("source_session_id", session.id)
                    if source_id != session.id:
                        raise PersonalAgentError("session_conflict", "请从原始对话发送团队委托")
                    result = await service.start_turn(source_session_id=source_id, **fields)
                elif operation == "team_turns":
                    result = {"turns": await service.list(**fields)}
                elif operation == "team_turn":
                    result = await service.get(fields["turn_id"])
                else:
                    result = await service.cancel(fields["turn_id"])
            elif operation in {"message_send", "messages", "message", "message_ack"}:
                service = self.service.engine.personal_collaborations.messages
                if operation == "message_send":
                    session = await self.service.engine.get_or_create_session()
                    source_id = fields.pop("source_session_id", session.id)
                    if source_id != session.id:
                        raise PersonalAgentError("session_conflict", "请从原始对话发送Agent消息")
                    result = await service.start_manual(source_session_id=source_id, **fields)
                elif operation == "messages":
                    result = {"messages": await service.list(fields["collaboration_id"])}
                elif operation == "message":
                    result = await service.get(fields["message_id"])
                else:
                    result = await service.acknowledge(
                        fields["message_id"], fields["expected_response_sha256"]
                    )
            elif operation in {
                "collaborate",
                "collaborations",
                "collaboration",
                "cancel_collaboration",
                "collaboration_reply",
            }:
                collaboration = getattr(self.service.engine, "personal_collaborations", None)
                if collaboration is None:
                    raise PersonalAgentError("configuration_error", "协作服务尚未启动")
                if operation == "collaborate":
                    session = await self.service.engine.get_or_create_session()
                    source_id = fields.pop("source_session_id", session.id)
                    if source_id != session.id:
                        raise PersonalAgentError("session_conflict", "请从原始对话发起协作")
                    result = await collaboration.create(source_session_id=source_id, **fields)
                elif operation == "collaborations":
                    result = {"collaborations": await collaboration.list(**fields)}
                elif operation == "collaboration":
                    result = await collaboration.get(fields["collaboration_id"])
                elif operation == "collaboration_reply":
                    result = await collaboration.reply(
                        fields["collaboration_id"], fields["question_id"], fields["response"]
                    )
                else:
                    result = await collaboration.cancel(fields["collaboration_id"])
            elif operation == "tools":
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
