"""One strict parser and human dispatcher for /routines in CLI, TUI and Web."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_tools import (
    MAX_REQUEST_BYTES,
    execute_routine_operation,
    public_error,
    validate_routine_arguments,
)

_HELP = """周期计划命令：
/routines context
/routines list [--status 状态] [--query 搜索词] [--limit 数量] [--after 计划ID]
  状态：active / paused / cancelled / closed
/routines get <计划ID>
/routines history <计划ID> [每页数量] [原修订游标]
/routines occurrences <计划ID> [每页数量] [原执行游标]
/routines requests <原request_id>
/routines preview-json '<含 request_id、definition 的 JSON>'
/routines create-json '<含 request_id、preview_id、expected_digest、enabled 的 JSON>'
/routines revise-json '<完整修改 JSON>'
  需要 routine_id、request_id、preview_id、expected_digest、expected_revision
/routines <pause|resume|cancel|extra-run>-json '<完整控制 JSON>'
  需要 routine_id、request_id、expected_revision
/routines <pause|resume|cancel|extra-run> <计划ID> <原修订> <稳定request_id>
所有写入保留稳定 request_id；未知时只查询原 requests，不自动新增执行。
预览不会创建计划；创建和修改需要确认原预览摘要。"""
_WRITES = {"preview", "create", "revise", "pause", "resume", "cancel", "extra-run"}


def _invalid():
    return ValueError("周期命令参数不完整或格式不正确，请查看 /routines help")


def _number(raw, *, minimum=0):
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise _invalid()
    value = int(raw)
    if not minimum <= value <= 2**31 - 1:
        raise _invalid()
    return value


def parse_routines_command(argument):
    try:
        if type(argument) is not str or len(argument.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        head = argument.strip().split(maxsplit=1)
        if not head:
            return {"operation": "list"}
        action, suffix = head[0], head[1] if len(head) == 2 else ""
        if action in {"help", "--help", "-h"} and not suffix:
            return {"operation": "help"}
        if action.endswith("-json"):
            operation = action[:-5]
            if operation not in _WRITES:
                raise _invalid()
            raw = suffix.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            body = strict_json_loads(raw)
            if not isinstance(body, dict):
                raise _invalid()
            value = {"operation": operation}
            if operation in {"revise", "pause", "resume", "cancel", "extra-run"}:
                body = dict(body)
                value["routine_id"] = body.pop("routine_id", None)
            value["payload"] = body
            return validate_routine_arguments(value)
        parts = shlex.split(argument)
        action, *args = parts
        value = {"operation": action}
        if action in {"context"} and not args:
            pass
        elif action == "list":
            options = {
                "--status": "status",
                "--query": "query",
                "--limit": "limit",
                "--after": "after",
            }
            if len(args) % 2:
                raise _invalid()
            for index in range(0, len(args), 2):
                key = options.get(args[index])
                if key is None or key in value:
                    raise _invalid()
                value[key] = (
                    _number(args[index + 1], minimum=1) if key == "limit" else args[index + 1]
                )
        elif action in {"get", "requests"} and len(args) == 1:
            value["request_id" if action == "requests" else "routine_id"] = args[0]
        elif action in {"history", "occurrences"} and 1 <= len(args) <= 3:
            value["routine_id"] = args[0]
            if len(args) >= 2:
                value["limit"] = _number(args[1], minimum=1)
            if len(args) == 3:
                value["after"] = _number(args[2]) if action == "history" else args[2]
        elif action in {"pause", "resume", "cancel", "extra-run"} and len(args) == 3:
            value.update(
                routine_id=args[0],
                payload={
                    "expected_revision": _number(args[1], minimum=1),
                    "request_id": args[2],
                },
            )
        else:
            raise _invalid()
        return validate_routine_arguments(value)
    except (PersonalAgentError, UnicodeError, TypeError, ValueError, RecursionError):
        raise _invalid() from None


def format_routines_result(operation, result):
    """Preserve machine-readable original receipts without terminal control sequences."""
    if not isinstance(result, dict):
        raise ValueError("周期回执不完整，请核对原请求")
    rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    return "".join(f"\\u{ord(char):04x}" if 127 <= ord(char) <= 159 else char for char in rendered)


async def run_routines_command(engine, argument, *, trusted_channel="cli"):
    request = ""
    try:
        if trusted_channel not in {"cli", "tui", "web", "manual_cli", "manual_tui", "manual_api"}:
            raise PersonalAgentError("scope_denied", "请从人工命令入口操作周期计划")
        arguments = parse_routines_command(argument)
        operation = arguments["operation"]
        if operation == "help":
            return _HELP
        payload = arguments.get("payload", {})
        if "request_id" in payload:
            request = "\n请求编号：" + payload["request_id"]
        service = getattr(engine, "personal_routines", None)
        if service is None:
            raise PersonalAgentError("configuration_error", "周期服务尚未启动")
        result = await execute_routine_operation(service, arguments, caller_actor=None)
        return format_routines_result(operation, result)
    except PersonalAgentError as exc:
        message = public_error(exc.code)
    except ValueError:
        message = "周期命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = public_error("routine_unknown")
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    return message + request
