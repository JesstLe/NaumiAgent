"""Shared /items parser and manual output; only explicit human read bypasses Tool."""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any

from jsonschema import Draft202012Validator

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.item_tools import FIELDS, request_schema, validate_item_arguments
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolCall, ToolExecutionError

_HELP = """事项命令：
/items list [active|paused|cancelled|closed|all] [搜索词]
/items get <事项ID>
/items history <事项ID>
/items occurrences <事项ID>
/items results [unread|all] [每页数量] [偏移]
/items result <执行ID>
/items create-json '<JSON对象>'
/items update-json '<含 item_id、expected_revision 的 JSON对象>'
/items <pause|resume|cancel|run-now|restart|close>-json '<JSON对象>'
  JSON 需要 item_id、expected_revision、request_id
/items read-json '<含 occurrence_id、request_id、delivery_id、expected_delivery_sha256 的 JSON对象>'
写入需要稳定 request_id；核对或重试时保留原编号与原完整 JSON。
read-json 只确认指定投递已读；查看、刷新和 Agent 工具不确认已读。"""
_ACTIONS = {name.replace("_", "-"): name for name in FIELDS}
_READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["occurrence_id", "request_id", "delivery_id", "expected_delivery_sha256"],
    "properties": {
        "occurrence_id": {"type": "string", "pattern": "^pio_[a-f0-9]{32}$"},
        "request_id": request_schema()["properties"]["request_id"],
        "delivery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "expected_delivery_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
}


def _number(raw: str, label: str, *, minimum=0, maximum=2**31 - 1) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise ValueError(f"{label}需要有效整数")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}需要 {minimum} 至 {maximum} 的整数")
    return value


def parse_items_command(argument: str) -> dict[str, Any]:
    """Preserve JSON writes verbatim in value, without minting idempotency identities."""
    if not isinstance(argument, str) or len(argument) > 1024 * 1024:
        raise ValueError("事项命令超过 1 MB 上限")
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise ValueError("引号未闭合，请用单引号包裹完整 JSON 对象") from exc
    if not parts:
        return {"operation": "list"}
    action, *args = parts
    if action in {"help", "-h", "--help"} and not args:
        return {"operation": "help"}
    if action.endswith("-json") and len(args) == 1:
        operation = action[:-5]
        try:
            value = strict_json_loads(args[0])
            if not isinstance(value, dict):
                raise ValueError("事项操作需要完整 JSON 对象")
            if operation == "read":
                if list(Draft202012Validator(_READ_SCHEMA).iter_errors(value)):
                    raise ValueError(
                        "已读操作需要原投递、摘要与稳定 request_id；身份由当前命令确定"
                    )
                return {"operation": "read", **value}
            operation = _ACTIONS.get(operation, "")
            if operation not in FIELDS:
                raise ValueError("事项命令不正确，请查看 /items help")
            if "operation" in value:
                raise ValueError("JSON 不接受 operation 字段；操作由当前命令确定")
            result = {"operation": operation, **value}
            return validate_item_arguments(result)
        except PersonalAgentError as exc:
            raise ValueError(exc.message) from exc
    if action == "list":
        value = {"operation": "list"}
        if args and args[0] in {"active", "paused", "cancelled", "closed", "all"}:
            state = args.pop(0)
            if state != "all":
                value["state"] = state
        if args:
            value["query"] = " ".join(args)
    elif action in {"get", "history", "occurrences"} and len(args) == 1:
        value = {"operation": action, "item_id": args[0]}
    elif action == "result" and len(args) == 1:
        value = {"operation": "result", "occurrence_id": args[0]}
    elif action == "results" and len(args) <= 3:
        value = {"operation": "results"}
        if args:
            if args[0] not in {"unread", "all"}:
                raise ValueError("结果筛选需要 unread 或 all")
            value["unread"] = args[0] == "unread"
        if len(args) >= 2:
            value["limit"] = _number(args[1], "每页数量", minimum=1, maximum=100)
        if len(args) == 3:
            value["offset"] = _number(args[2], "结果偏移")
    else:
        raise ValueError("事项命令或参数不完整，请查看 /items help")
    try:
        return validate_item_arguments(value)
    except PersonalAgentError as exc:
        raise ValueError(exc.message) from exc


_LABELS = {
    "active": "待执行",
    "paused": "已暂停",
    "cancelled": "已取消",
    "closed": "已结束",
    "prepared": "准备中",
    "queued": "排队中",
    "running": "执行中",
    "completed": "已完成",
    "error": "执行出错",
    "timeout": "已超时",
    "max_turns": "已达轮数上限",
    "unknown": "结果未知",
    "blocked": "受阻",
    "skipped": "已跳过",
}


def _safe(value: Any, *, label=False) -> str:
    text = str(value)
    if label:
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return "".join(
        f"\\u{ord(char):04x}"
        if ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127
        else char
        for char in text
    )


def _execution(value: dict) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("occurrence_id"), str):
        raise ValueError("执行回执缺少原执行身份")
    status = value.get("status", "unknown")
    lines = [
        "执行：" + _safe(value["occurrence_id"], label=True),
        "状态：" + _LABELS.get(status, "状态待核对"),
    ]
    for key, title in (("job_id", "Job"), ("run_id", "Run"), ("delivery_id", "投递")):
        if value.get(key):
            lines.append(title + "：" + _safe(value[key], label=True))
    if value.get("error"):
        lines.append("错误：" + _safe(value["error"]))
    if value.get("answer"):
        lines.append("结果：\n" + _safe(value["answer"]))
    for ref in value.get("artifacts", []):
        lines.append(
            f"报告：{_safe(ref['artifact_id'], label=True)} · v{ref['version']}"
            f" · SHA-256 {_safe(ref['sha256'], label=True)}"
            f" · 授权 {_safe(ref['grant_id'] or '自有', label=True)} r{ref['grant_revision']}"
        )
    if "read_receipt" in value:
        lines.append("已读：" + ("已确认" if value["read_receipt"] else "未确认"))
    return "\n".join(lines)


def _item(value: dict) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("definition"), dict):
        raise ValueError("事项回执缺少真实定义")
    definition = value["definition"]
    lines = [
        _safe(definition["title"], label=True),
        f"事项：{_safe(value['item_id'], label=True)} · 修订 {value['revision']}",
        "控制状态：" + _LABELS.get(value.get("state"), "状态待核对"),
        "执行 Agent："
        + _safe(definition["assignee"]["name"], label=True)
        + f" · {_safe(definition['assignee']['id'], label=True)}"
        + f" v{definition['assignee']['version']}",
        "计划："
        + _safe(definition["schedule"].get("due_at", ""), label=True)
        + " · "
        + _safe(definition["schedule"]["timezone"], label=True),
        "执行要求：\n" + _safe(definition["instructions"]),
    ]
    if value.get("occurrence"):
        lines.append(_execution(value["occurrence"]))
    return "\n".join(lines)


def format_items_result(operation: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("事项回执需要 JSON 对象")
    if operation == "read":
        if not payload.get("read_receipt"):
            raise ValueError("已读操作尚无真实回执，请保留原请求核对")
        return "已确认指定结果已读。\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    if operation in {"list", "history"}:
        records = payload.get("items" if operation == "list" else "history")
        if not isinstance(records, list):
            raise ValueError("事项目录回执不完整")
        rendered = "\n\n".join(_item(value) for value in records) if records else "暂无事项。"
    elif operation in {"results", "occurrences"}:
        records = payload.get("results" if operation == "results" else "occurrences")
        if not isinstance(records, list):
            raise ValueError("执行结果目录回执不完整")
        rendered = "\n\n".join(_execution(value) for value in records) if records else "暂无结果。"
    elif operation == "result":
        return _execution(payload)
    else:
        return _item(payload)
    if payload.get("next_offset") is not None:
        rendered += "\n下一页偏移：" + str(payload["next_offset"])
    return rendered


def _mark_failed(engine):
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()


async def run_items_command(engine: Any, argument: str, *, trusted_channel="cli") -> str:
    """Execute ordinary operations through Engine authorization; only human read is separate."""
    try:
        fields = parse_items_command(argument)
    except ValueError as exc:
        _mark_failed(engine)
        return str(exc)
    if fields["operation"] == "help":
        return _HELP
    request = "\n请求编号：" + fields["request_id"] if "request_id" in fields else ""
    try:
        if fields["operation"] == "read":
            if trusted_channel not in {"cli", "tui", "web"}:
                raise PersonalAgentError("invalid_input", "人工已读通道不正确")
            from naumi_agent.personal.runtime import require_current_agent

            bound_engine = getattr(engine, "_engine", engine)
            profile = require_current_agent(bound_engine)
            payload = {
                key: value
                for key, value in fields.items()
                if key not in {"operation", "occurrence_id"}
            }
            value = await engine.personal_items.read_result(
                fields["occurrence_id"],
                payload,
                trusted_channel=trusted_channel,
                caller_actor=profile.id if profile is not None else "main",
            )
            try:
                occurrence = value.get("occurrence") if isinstance(value, dict) else None
                if (
                    not isinstance(occurrence, dict)
                    or occurrence.get("occurrence_id") != fields["occurrence_id"]
                    or occurrence.get("delivery_id") != fields["delivery_id"]
                    or occurrence.get("delivery_sha256") != fields["expected_delivery_sha256"]
                ):
                    raise ValueError("已读回执与原投递身份不一致")
                return format_items_result("read", value) + request
            except (KeyError, TypeError, ValueError) as exc:
                _mark_failed(engine)
                return "已读回执待核对，请保留原请求。" + request + "\n" + _safe(exc)
        result = await engine.execute_tool(
            ToolCall(
                id="items-command-" + uuid.uuid4().hex,
                name="personal_items",
                arguments=json.dumps(fields, ensure_ascii=False, allow_nan=False),
            ),
            agent_name="cli",
        )
        if result.status != "success":
            state = "结果待核对" if result.status == "unknown" else "操作未完成"
            return state + request + "\n" + _safe(result.content)
        try:
            return (
                format_items_result(fields["operation"], strict_json_loads(result.content))
                + request
            )
        except (KeyError, TypeError, ValueError) as exc:
            _mark_failed(engine)
            return (
                "事项回执待核对，请保留原请求。"
                + request
                + "\n"
                + _safe(exc)
                + "\n原回执："
                + _safe(result.content)
            )
    except (PersonalAgentError, ToolExecutionError) as exc:
        _mark_failed(engine)
        return "操作未完成" + request + "\n" + _safe(exc)
