"""Explicit human /notifications parser shared by CLI and TUI."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_tools import (
    MANUAL_CHANNELS,
    MAX_REQUEST_BYTES,
    execute_notification_operation,
    public_error,
    serializable_result,
    validate_notification_arguments,
)

_HELP = """通知命令：
/notifications context
/notifications list [--filter unread|attention|all] [--actor AgentID] [--limit N] [--after CURSOR]
/notifications get-batch <原批次ID>
/notifications preferences
/notifications requests <原request_id>
/notifications <preferences|claim|present|read>-json '<完整JSON>'"""
_JSON_ACTIONS = {
    "preferences-json": "set_preferences",
    "claim-json": "claim",
    "present-json": "present",
    "read-json": "mark_read",
}


def _invalid():
    return ValueError("通知命令参数不完整或格式不正确，请查看 /notifications help")


def _number(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 3:
        raise _invalid()
    return int(value)


def _options(args):
    if len(args) % 2:
        raise _invalid()
    result = {}
    mapping = {"--filter": "filter", "--actor": "actor_id", "--limit": "limit", "--after": "after"}
    for index in range(0, len(args), 2):
        key = mapping.get(args[index])
        if key is None or key in result:
            raise _invalid()
        result[key] = _number(args[index + 1]) if key == "limit" else args[index + 1]
    return result


def parse_notification_command(argument):
    try:
        if type(argument) is not str or len(argument.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        head = argument.strip().split(maxsplit=1)
        if not head:
            return {"operation": "context"}
        action, suffix = head[0], head[1] if len(head) == 2 else ""
        if action in {"help", "--help", "-h"} and not suffix:
            return {"operation": "help"}
        if action in _JSON_ACTIONS:
            raw = suffix.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return validate_notification_arguments(
                {"operation": _JSON_ACTIONS[action], "payload": strict_json_loads(raw)}, human=True
            )
        action, *args = shlex.split(argument)
        value = {"operation": action}
        if action in {"context", "preferences"} and not args:
            pass
        elif action == "list":
            value.update(_options(args))
        elif action in {"get-batch", "requests"} and len(args) == 1:
            value["batch_id" if action == "get-batch" else "request_id"] = args[0]
        else:
            raise _invalid()
        return validate_notification_arguments(value, human=True)
    except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


async def run_notification_command(engine, argument, *, trusted_channel):
    arguments = {}
    try:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        arguments = parse_notification_command(argument)
        if arguments["operation"] == "help":
            return _HELP
        service = getattr(engine, "personal_notifications", None)
        if service is None:
            raise PersonalAgentError("configuration_error", public_error("configuration_error"))
        result = await execute_notification_operation(
            service, arguments, trusted_channel=trusted_channel
        )
        rendered = json.dumps(
            serializable_result(result, arguments), ensure_ascii=False, allow_nan=False, indent=2
        )
        return "".join(
            f"\\u{ord(char):04x}" if 127 <= ord(char) <= 159 else char for char in rendered
        )
    except PersonalAgentError as exc:
        message = public_error(exc.code)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        message = "通知命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = public_error("notification_unknown")
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    request = arguments.get("payload", {}).get("request_id")
    return message + ("\n请求编号：" + request if request else "")


parse_notifications_command = parse_notification_command
run_notifications_command = run_notification_command
