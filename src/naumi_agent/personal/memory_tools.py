"""Scoped model-facing memory proposals and reference-only retrieval aliases."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata

CATEGORIES = ("preference", "fact", "decision", "experience", "plan_template")
SCOPES = ("all", "personal", "workspace", "item")
STATES = ("proposed", "active", "superseded", "rejected", "deleted")
MODEL_OPERATIONS = ("list", "get", "search", "propose", "history")
HUMAN_OPERATIONS = (
    "create",
    "propose",
    "confirm",
    "revise",
    "delete",
    "reject",
    "share",
    "revoke",
    "import_legacy",
    "index_sync",
)
MEMORY_ID = {"type": "string", "pattern": "^pm_[a-f0-9]{32}$"}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"}
SHA256 = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
ACTOR_ID = {"type": "string", "pattern": "^(main|pa_[a-f0-9]{32})$"}


def _string(maximum, *, empty=False):
    return {"type": "string", "minLength": 0 if empty else 1, "maxLength": maximum}


def _integer(minimum=0, maximum=2**31 - 1):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def memory_ref_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["memory_id", "revision", "content_sha256", "grant_id", "grant_revision"],
        "properties": {
            "memory_id": MEMORY_ID,
            "revision": _integer(1),
            "content_sha256": SHA256,
            "grant_id": {"type": "string", "pattern": "^(|pmg_[a-f0-9]{32})$"},
            "grant_revision": _integer(),
        },
    }


def _artifact_ref_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_id", "version", "sha256", "grant_id", "grant_revision"],
        "properties": {
            "artifact_id": {"type": "string", "pattern": "^art_[a-f0-9]{32}$"},
            "version": _integer(1, 10000),
            "sha256": SHA256,
            "grant_id": {"type": "string", "pattern": "^(|asg_[a-f0-9]{32})$"},
            "grant_revision": _integer(),
        },
    }


def operation_schema(operation: str) -> dict:
    """Validate the same untrusted payload for model, manual and HTTP adapters."""
    fields = {
        "request_id": REQUEST_ID,
        "topic": _string(120),
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "content": _string(8000),
        "scope": {"type": "string", "enum": list(SCOPES)},
        "item_id": {"type": "string", "pattern": "^pi_[a-f0-9]{32}$"},
        "memory_id": MEMORY_ID,
        "query": _string(2000, empty=True),
        "state": {"type": "string", "enum": list(STATES)},
        "limit": _integer(1, 100),
        "offset": _integer(),
        "expected_revision": _integer(1),
        "expected_content_sha256": SHA256,
        "expected_active_ref": {"anyOf": [{"type": "null"}, memory_ref_schema()]},
        "recipient_actor_id": ACTOR_ID,
        "recipient_version": _integer(0),
        "grant_id": {"type": "string", "pattern": "^pmg_[a-f0-9]{32}$"},
        "legacy_id": _string(128),
        "source_message_index": _integer(),
        "source_message_sha256": SHA256,
        "source_quote": _string(8000),
        "source_artifact_ref": _artifact_ref_schema(),
    }
    definitions = {
        "list": ({"scope", "item_id", "state", "query", "limit", "offset"}, set()),
        "get": ({"memory_id"}, {"memory_id"}),
        "history": ({"memory_id"}, {"memory_id"}),
        "grants": ({"memory_id"}, {"memory_id"}),
        "search": ({"query", "scope", "item_id", "limit", "category"}, {"query"}),
        "export": ({"scope", "item_id"}, set()),
        "legacy": ({"limit", "offset"}, set()),
        "create": (
            {"request_id", "topic", "category", "content", "scope", "item_id"},
            {"request_id", "topic", "category", "content", "scope"},
        ),
        "propose": (
            {
                "request_id",
                "topic",
                "category",
                "content",
                "scope",
                "item_id",
                "source_message_index",
                "source_message_sha256",
                "source_quote",
                "source_artifact_ref",
            },
            {"topic", "category", "content", "scope"},
        ),
        "confirm": (
            {
                "request_id",
                "memory_id",
                "expected_revision",
                "expected_content_sha256",
                "expected_active_ref",
            },
            {
                "request_id",
                "memory_id",
                "expected_revision",
                "expected_content_sha256",
                "expected_active_ref",
            },
        ),
        "revise": (
            {"request_id", "memory_id", "expected_revision", "expected_content_sha256", "content"},
            {"request_id", "memory_id", "expected_revision", "expected_content_sha256", "content"},
        ),
        "delete": (
            {"request_id", "memory_id", "expected_revision"},
            {"request_id", "memory_id", "expected_revision"},
        ),
        "reject": (
            {"request_id", "memory_id", "expected_revision"},
            {"request_id", "memory_id", "expected_revision"},
        ),
        "share": (
            {
                "request_id",
                "memory_id",
                "expected_revision",
                "expected_content_sha256",
                "recipient_actor_id",
                "recipient_version",
            },
            {
                "request_id",
                "memory_id",
                "expected_revision",
                "expected_content_sha256",
                "recipient_actor_id",
                "recipient_version",
            },
        ),
        "revoke": (
            {"request_id", "grant_id", "expected_revision"},
            {"request_id", "grant_id", "expected_revision"},
        ),
        "import_legacy": (
            {"request_id", "legacy_id", "expected_content_sha256", "topic", "scope", "item_id"},
            {"request_id", "legacy_id", "expected_content_sha256", "topic", "scope"},
        ),
        "index_sync": (set(), set()),
        "context": (set(), set()),
    }
    if operation not in definitions:
        raise PersonalAgentError("invalid_input", "记忆操作不正确")
    allowed, required = definitions[operation]
    properties = {k: fields[k] for k in allowed}
    if operation in {"create", "propose", "import_legacy"}:
        properties["scope"] = {"type": "string", "enum": ["personal", "workspace", "item"]}
    if operation == "list":
        properties["query"] = _string(200, empty=True)
        properties["state"] = {"type": "string", "enum": [*STATES, "conflict"]}
    if operation == "search":
        properties["limit"] = _integer(1, 20)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(required),
    }


def validate_operation_payload(operation: str, value: Any, *, manual=False) -> dict:
    if not isinstance(value, dict) or list(
        Draft202012Validator(operation_schema(operation)).iter_errors(value)
    ):
        raise PersonalAgentError("invalid_input", "记忆请求包含无效字段或缺少必要参数")
    if manual and operation == "propose" and not value.get("request_id"):
        raise PersonalAgentError("invalid_input", "请保留稳定 request_id 后提交记忆候选")
    if operation == "search" and not value["query"].strip():
        raise PersonalAgentError("invalid_input", "请填写记忆查询内容")
    return value


def _model_schema():
    properties = {"operation": {"type": "string", "enum": list(MODEL_OPERATIONS)}}
    for operation in MODEL_OPERATIONS:
        properties.update(operation_schema(operation)["properties"])
    properties["scope"] = {"type": "string", "enum": list(SCOPES)}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation"],
        "properties": properties,
    }


def _actor(engine):
    from naumi_agent.personal.runtime import require_current_agent

    profile = require_current_agent(engine)
    return profile.id if profile is not None else "main"


def _tool_error(exc: PersonalAgentError):
    message = " ".join(exc.message.split())[:300] or "记忆操作未完成"
    return ToolExecutionError(exc.code, message)


def _reference_result(operation, result):
    if (
        not isinstance(result, dict)
        or result.get("type") != "personal_memory_refs"
        or not isinstance(result.get("refs"), list)
        or len(result["refs"]) > 100
        or any(
            list(Draft202012Validator(memory_ref_schema()).iter_errors(ref))
            for ref in result["refs"]
        )
    ):
        raise PersonalAgentError("memory_result_unverified", "记忆结果缺少确切引用，请核对原操作")
    # Do not let an accidentally widened service DTO persist plaintext in Tool history.
    value = {"type": "personal_memory_refs", "operation": operation, "refs": result["refs"]}
    for key, schema in {
        "request_id": REQUEST_ID,
        "memory_id": MEMORY_ID,
        "candidate_id": MEMORY_ID,
        "revision": _integer(1),
        "state": {"type": "string", "enum": list(STATES)},
        "count": _integer(),
        "limit": _integer(1, 100),
        "offset": _integer(),
        "next_offset": _integer(),
        "omitted_count": _integer(),
    }.items():
        if key in result and not list(Draft202012Validator(schema).iter_errors(result[key])):
            value[key] = result[key]
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, dict):
        safe = {}
        for key in ("authorized_candidates", "lexical_hits", "omitted_count"):
            count = diagnostics.get(key)
            if type(count) is int and count >= 0:
                safe[key] = count
        if type(diagnostics.get("semantic_used")) is bool:
            safe["semantic_used"] = diagnostics["semantic_used"]
        if diagnostics.get("status") in {"ready", "unavailable", "not_loaded"}:
            safe["status"] = diagnostics["status"]
        if diagnostics.get("error") or diagnostics.get("semantic_error"):
            safe["notice"] = "语义通道暂未就绪，本次只使用已授权范围的文本检索"
        if safe:
            value["diagnostics"] = safe
    if operation == "propose":
        if not (value.get("candidate_id") or value.get("memory_id")) or "state" not in value:
            raise PersonalAgentError(
                "memory_result_unverified", "候选缺少原始身份或状态，请保留原请求"
            )
        value["notice"] = {
            "proposed": "候选已提交，等待用户确认；尚未生效",
            "active": "原候选现已确认；本次未重新创建，请查询当前版本",
            "deleted": "原候选已删除；本次未重新创建",
            "rejected": "原候选已拒绝；本次未重新创建",
            "superseded": "原候选已被替代；本次未重新创建",
        }[value["state"]]
    return value


class PersonalMemoryTool(Tool):
    name = "personal_memories"
    description = (
        "查询当前 Agent 范围内的记忆，或提出待用户确认的记忆候选。"
        "list/get/search/history返回确切引用，正文由系统在真正发送模型请求前重新授权解析。"
        "propose只创建候选，不表示已生效；不接受伪造actor/source角色/人工确认。"
        "个人范围可跨工作区但不自动跨Agent；项目与事项资料保持原范围。"
    )

    def __init__(self, service):
        self.service, self.engine = service, service.engine

    @property
    def parameters_schema(self):
        return _model_schema()

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="记忆",
            search_hint="personal memory recall preference proposal source scope",
        )

    def parse_arguments(self, raw):
        if isinstance(raw, str):
            try:
                raw = strict_json_loads(raw)
            except PersonalAgentError as exc:
                raise ValueError(exc.message) from exc
        return super().parse_arguments(raw)

    async def execute(self, operation: str, **payload):
        try:
            if operation not in MODEL_OPERATIONS:
                raise PersonalAgentError("invalid_input", "该记忆操作需要人工入口确认")
            validate_operation_payload(operation, payload)
            actor = _actor(self.engine)
            result = await self.service.dispatch(
                operation,
                payload,
                actor_id=actor,
                caller_actor=actor,
            )
            return json.dumps(
                _reference_result(operation, result), ensure_ascii=False, allow_nan=False
            )
        except PersonalAgentError as exc:
            raise _tool_error(exc) from exc


class MemoryStoreAlias(PersonalMemoryTool):
    name = "memory_store"
    description = "提出当前 Agent 的工作区记忆候选；等待用户明确确认，不直接生效。"

    @property
    def metadata(self):
        return ToolMetadata(
            requires_confirmation=False,
            user_facing_name="记忆候选",
            search_hint="propose memory preference fact user confirmation",
        )

    @property
    def parameters_schema(self):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["content"],
            "properties": {
                "content": _string(8000),
                "category": {"type": "string", "enum": list(CATEGORIES)},
            },
        }

    async def execute(self, *, content, category="fact", **payload):
        if payload or not isinstance(content, str):
            raise ToolExecutionError("invalid_input", "记忆候选不接受身份或人工确认参数")
        return await super().execute(
            "propose",
            content=content,
            category=category,
            topic=content.strip()[:120],
            scope="workspace",
        )


class MemoryRecallAlias(PersonalMemoryTool):
    name = "memory_recall"
    description = "在当前 Agent 的允许范围搜索记忆引用，发送模型前再次核验当前版本和来源。"

    @property
    def metadata(self):
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="检索记忆",
            search_hint="recall scoped memory references",
        )

    @property
    def parameters_schema(self):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": _string(1000),
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "top_k": _integer(1, 10),
            },
        }

    async def execute(self, *, query, category=None, top_k=3, **payload):
        if (
            payload
            or type(top_k) is not int
            or not 1 <= top_k <= 10
            or not isinstance(query, str)
            or not query.strip()
            or len(query) > 1000
        ):
            raise ToolExecutionError("invalid_input", "记忆检索参数不正确")
        fields = {"query": query, "limit": top_k}
        if category is not None:
            fields["category"] = category
        return await super().execute("search", **fields)
