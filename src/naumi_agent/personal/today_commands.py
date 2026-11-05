"""One explicit human /today parser and dispatcher for CLI, TUI and Web."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_tools import (
    MANUAL_CHANNELS,
    MAX_REQUEST_BYTES,
    execute_today_operation,
    public_error,
    serializable_result,
    validate_today_arguments,
)

_HELP = """今日命令：
/today context
/today list <日期> <时区> [--planner AgentID] [--limit 数量] [--cursor 原游标]
/today get <日计划ID> [确切修订]
/today get <日期> <时区> [--planner AgentID] [--limit 数量] [--cursor 原游标]
/today sources [--limit 数量] [--after 原事项ID]
/today metadata <事项ID>
/today history <day|metadata> <原编号> [--limit 数量] [--after-revision 原修订]
/today requests <原request_id>
/today <set-metadata|place|reorder|remove|rebase|check|move>-json '<完整JSON>'
所有写入携带稳定 request_id 和确切版本；结果待核对时查询原请求。
check 只记录人工勾选，remove/move 不取消事项执行。"""
_WRITES = {"set-metadata", "set_metadata", "place", "reorder", "remove", "rebase", "check", "move"}


def _invalid():
    return ValueError("今日命令参数不完整或格式不正确，请查看 /today help")


def _number(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 16:
        raise _invalid()
    result = int(value)
    if result > 2**53 - 1:
        raise _invalid()
    return result


def _options(args, mapping):
    if len(args) % 2:
        raise _invalid()
    result = {}
    for index in range(0, len(args), 2):
        key = mapping.get(args[index])
        if key is None or key in result:
            raise _invalid()
        result[key] = (
            _number(args[index + 1]) if key in {"limit", "after_revision"} else args[index + 1]
        )
    return result


def parse_today_command(argument):
    try:
        if type(argument) is not str or len(argument.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        head = argument.strip().split(maxsplit=1)
        if not head:
            return {"operation": "context"}
        action, suffix = head[0], head[1] if len(head) == 2 else ""
        if action in {"help", "--help", "-h"} and not suffix:
            return {"operation": "help"}
        if action.endswith("-json"):
            action = action[:-5]
            if action not in _WRITES:
                raise _invalid()
            raw = suffix.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            payload = strict_json_loads(raw)
            return validate_today_arguments(
                {"operation": action.replace("-", "_"), "payload": payload}, human=True
            )
        action, *args = shlex.split(argument)
        value = {"operation": action}
        if action == "context" and not args:
            pass
        elif action in {"list", "get"}:
            if action == "get" and args and args[0].startswith("td_") and len(args) in {1, 2}:
                value["day_id"] = args[0]
                if len(args) == 2:
                    value["revision"] = _number(args[1])
            elif len(args) >= 2:
                value.update(date=args[0], timezone=args[1])
                value.update(
                    _options(
                        args[2:],
                        {"--planner": "planner_id", "--limit": "limit", "--cursor": "cursor"},
                    )
                )
            else:
                raise _invalid()
        elif action == "sources":
            value.update(_options(args, {"--limit": "limit", "--after": "after"}))
        elif action in {"metadata", "requests"} and len(args) == 1:
            value["item_id" if action == "metadata" else "request_id"] = args[0]
        elif action == "history" and len(args) >= 2:
            value.update(kind=args[0], identity=args[1])
            value.update(
                _options(args[2:], {"--limit": "limit", "--after-revision": "after_revision"})
            )
        else:
            raise _invalid()
        return validate_today_arguments(value, human=True)
    except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


async def run_today_command(engine, argument, *, trusted_channel):
    arguments = {}
    try:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        arguments = parse_today_command(argument)
        if arguments["operation"] == "help":
            return _HELP
        service = getattr(engine, "personal_today", None)
        if service is None:
            raise PersonalAgentError("configuration_error", public_error("configuration_error"))
        result = await execute_today_operation(service, arguments, trusted_channel=trusted_channel)
        result = serializable_result(result, arguments)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        return "".join(
            f"\\u{ord(char):04x}" if 127 <= ord(char) <= 159 else char for char in rendered
        )
    except PersonalAgentError as exc:
        message = public_error(exc.code)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        message = "今日命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = public_error("today_unknown")
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    request = arguments.get("payload", {}).get("request_id")
    return message + ("\n请求编号：" + request if request else "")
