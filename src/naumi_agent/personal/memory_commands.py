"""Shared manual memory commands over the same scoped authority as model tools."""

from __future__ import annotations

import json
import shlex
from typing import Any

from jsonschema import Draft202012Validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.memory_tools import (
    HUMAN_OPERATIONS,
    _actor,
    memory_ref_schema,
    validate_operation_payload,
)
from naumi_agent.personal.models import PersonalAgentError

_HELP = """记忆命令：
/memory stats — 当前 Agent 与记忆索引状态
/memory list [personal|workspace|item] [搜索词]
/memory get|history|grants <记忆ID>
/memory search <查询>
/memory export [personal|workspace|item] — 导出当前授权范围 JSON
/memory legacy [每页数量] [偏移] — 审阅旧记忆，不自动生效
/memory clean|consolidate|index-sync — 同步派生索引，不合并或删除事实
/memory <create|propose|confirm|revise|delete|reject|share|revoke|import-legacy>-json '<JSON对象>'
写入保留原 request_id；confirm/revise/share 绑定原 revision 和 content SHA。
个人范围可跨工作区，不自动跨 Agent；模型候选经人工确认后才生效。"""
_READS = {"list", "get", "history", "grants", "search", "export", "legacy"}


def _number(raw):
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise ValueError("分页参数需要有效整数")
    return int(raw)


def parse_memory_command(argument: str) -> dict:
    if not isinstance(argument, str) or len(argument.encode("utf-8")) > 1024 * 1024:
        raise ValueError("记忆命令超过 1 MB 上限")
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise ValueError("引号未闭合，请用单引号包裹完整 JSON 对象") from exc
    if not parts:
        return {"operation": "context"}
    action, *args = parts
    if action in {"help", "-h", "--help"} and not args:
        return {"operation": "help"}
    if action in {"stats", "context"} and not args:
        return {"operation": "context"}
    if action in {"clean", "consolidate", "index-sync"} and not args:
        return {"operation": "index_sync"}
    try:
        if action.endswith("-json") and len(args) == 1:
            operation = action[:-5].replace("-", "_")
            if operation not in HUMAN_OPERATIONS:
                raise ValueError("记忆写入命令不正确，请查看 /memory help")
            payload = strict_json_loads(args[0])
        elif action in {"get", "history", "grants"} and len(args) == 1:
            operation, payload = action, {"memory_id": args[0]}
        elif action == "search" and args:
            operation, payload = "search", {"query": " ".join(args)}
        elif action == "export" and len(args) <= 1:
            operation, payload = "export", {"scope": args[0]} if args else {}
        elif action == "list":
            operation, payload = "list", {}
            if args and args[0] in {"all", "personal", "workspace", "item"}:
                payload["scope"] = args.pop(0)
            if args:
                payload["query"] = " ".join(args)
        elif action == "legacy" and len(args) <= 2:
            operation, payload = "legacy", {}
            if args:
                payload["limit"] = _number(args[0])
            if len(args) > 1:
                payload["offset"] = _number(args[1])
        else:
            raise ValueError("记忆命令或参数不完整，请查看 /memory help")
        return {
            "operation": operation,
            **validate_operation_payload(operation, payload, manual=True),
        }
    except PersonalAgentError as exc:
        raise ValueError(exc.message) from exc


def _refs(value):
    refs = []
    if isinstance(value, list):
        for record in value:
            refs.extend(_refs(record))
    elif isinstance(value, dict):
        if not list(Draft202012Validator(memory_ref_schema()).iter_errors(value)):
            refs.append(dict(value))
        else:
            for key in ("ref", "active_ref", "refs", "memories", "memory", "result"):
                if key in value:
                    refs.extend(_refs(value[key]))
    unique = {json.dumps(ref, sort_keys=True): ref for ref in refs}
    return list(unique.values())


def _safe(value):
    raw = str(value)
    return "".join(
        f"\\u{ord(char):04x}"
        if ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127
        else char
        for char in raw
    )


def _mark_failed(engine):
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()


async def run_memory_command(engine: Any, argument: str, *, trusted_channel="manual_cli") -> str:
    """Resolve human identity in the real runtime; never expose management to model tools."""
    engine._personal_memory_command_refs = []
    try:
        fields = parse_memory_command(argument)
        operation = fields.pop("operation")
        if operation == "help":
            return _HELP
        if trusted_channel not in {"manual_cli", "management_web"}:
            raise PersonalAgentError("invalid_input", "记忆人工操作通道不正确")
        bound = getattr(engine, "_engine", engine)
        actor = _actor(bound)
        if getattr(getattr(bound, "_session", None), "personal_execution_id", ""):
            raise PersonalAgentError("execution_session_readonly", "请在日常对话或记忆页面管理记忆")
        service = engine.personal_memory
        if operation == "context":
            result = await service.context(actor_id=actor)
        elif operation == "search":
            result = await service.search(fields.pop("query"), actor_id=actor, **fields)
        elif operation in {"get", "history", "grants"}:
            result = await getattr(service, operation)(fields["memory_id"], actor_id=actor)
        elif operation in _READS:
            result = await getattr(service, operation)(actor_id=actor, **fields)
        else:
            result = await service.dispatch(
                operation,
                fields,
                actor_id=actor,
                trusted_channel=trusted_channel,
            )
        if not isinstance(result, dict):
            raise PersonalAgentError("memory_result_unverified", "记忆操作回执待核对，请保留原请求")
        engine._personal_memory_command_refs = _refs(result)
        prefix = {
            "create": "记忆保存结果",
            "propose": "记忆候选结果",
            "confirm": "记忆确认结果",
            "revise": "记忆纠正结果",
            "delete": "记忆删除结果",
            "reject": "候选处理结果",
            "share": "记忆分享结果",
            "revoke": "记忆撤销结果",
            "import_legacy": "旧记忆候选结果",
            "index_sync": "派生索引同步结果",
        }.get(operation, "记忆")
        return prefix + "\n" + json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, PersonalAgentError) as exc:
        _mark_failed(engine)
        request = fields.get("request_id") if "fields" in locals() else None
        receipt = "\n请求编号：" + _safe(request) if request else ""
        return "记忆操作未完成" + receipt + "\n" + _safe(exc)
