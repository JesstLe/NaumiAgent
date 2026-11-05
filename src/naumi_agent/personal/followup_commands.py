"""Human followup commands shared by CLI, TUI and the authenticated Web gateway."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.followup_tools import (
    MANUAL_CHANNELS,
    MAX_REQUEST_BYTES,
    execute_followup_operation,
    public_error,
    serializable_result,
    validate_followup_arguments,
)
from naumi_agent.personal.models import PersonalAgentError

_HELP = """跟进命令：
/followups context
/followups rules
/followups list [--status 状态] [--actor AgentID] [--limit N] [--after 原游标ID]
/followups get <原建议ID>
/followups requests <原request_id>
/followups <create-rule|update-rule|set-status|accept|dismiss|snooze|reconcile>-json '<完整JSON>'"""
_JSON_ACTIONS = {
    name.replace("_", "-") + "-json": name
    for name in (
        "create_rule",
        "update_rule",
        "set_status",
        "accept",
        "dismiss",
        "snooze",
        "reconcile",
    )
}


def _invalid():
    return ValueError("跟进命令参数不完整或格式不正确，请查看 /followups help")


def _options(args):
    if len(args) % 2:
        raise _invalid()
    result = {}
    mapping = {"--status": "status", "--actor": "actor_id", "--limit": "limit", "--after": "after"}
    for index in range(0, len(args), 2):
        key, value = mapping.get(args[index]), args[index + 1]
        if key is None or key in result:
            raise _invalid()
        if key == "limit":
            if not value.isascii() or not value.isdecimal() or len(value) > 3:
                raise _invalid()
            value = int(value)
        result[key] = value
    return result


def parse_followup_command(argument):
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
            return validate_followup_arguments(
                {"operation": _JSON_ACTIONS[action], "payload": strict_json_loads(raw)}, human=True
            )
        action, *args = shlex.split(argument)
        value = {"operation": action}
        if action in {"context", "rules"} and not args:
            pass
        elif action == "list":
            value.update(_options(args))
        elif action in {"get", "requests"} and len(args) == 1:
            value["proposal_id" if action == "get" else "request_id"] = args[0]
        else:
            raise _invalid()
        return validate_followup_arguments(value, human=True)
    except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


async def run_followup_command(engine, argument, *, trusted_channel):
    try:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        arguments = parse_followup_command(argument)
        if arguments["operation"] == "help":
            return _HELP
        service = getattr(engine, "personal_followups", None)
        if service is None:
            raise PersonalAgentError("configuration_error", public_error("configuration_error"))
        result = await execute_followup_operation(
            service, arguments, trusted_channel=trusted_channel
        )
        rendered = json.dumps(
            serializable_result(result, arguments), ensure_ascii=False, allow_nan=False, indent=2
        )
        return "".join(
            f"\\u{ord(char):04x}" if ord(char) == 27 or 127 <= ord(char) <= 159 else char
            for char in rendered
        )
    except PersonalAgentError as exc:
        message = public_error(exc.code)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        message = "跟进命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = public_error("followup_unknown")
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    return message


parse_followups_command = parse_followup_command
run_followups_command = run_followup_command
