"""Human /workspaces operations shared by the CLI, TUI and Web gateway."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_models import MAX_REQUEST_BYTES, error
from naumi_agent.personal.workspace_tools import (
    MANUAL_CHANNELS,
    execute_workspace_operation,
    serializable_result,
    validate_workspace_arguments,
)

_HELP = """工作区命令：
/workspaces context
/workspaces list
/workspaces recipient <AgentID>
/workspaces grants
/workspaces requests <原request_id>
/workspaces overview [--connection 原连接ID] [--after CURSOR] [--limit N]
/workspaces result <原连接ID> <原事项ID>
/workspaces <issue|revoke|connect|disconnect>-json '<完整JSON>'"""
_JSON_ACTIONS = {value + "-json": value for value in ("issue", "revoke", "connect", "disconnect")}


def _invalid():
    return ValueError("工作区命令参数不完整或格式不正确，请查看 /workspaces help")


def _options(args):
    if len(args) % 2:
        raise _invalid()
    result = {}
    mapping = {"--connection": "connection_id", "--after": "after", "--limit": "limit"}
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


def parse_workspace_command(argument):
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
            return validate_workspace_arguments(
                {"operation": _JSON_ACTIONS[action], "payload": strict_json_loads(raw)}, human=True
            )
        action, *args = shlex.split(argument)
        value = {"operation": action}
        if action in {"context", "list", "grants"} and not args:
            pass
        elif action == "overview":
            value.update(_options(args))
        elif action in {"recipient", "requests"} and len(args) == 1:
            value["actor_id" if action == "recipient" else "request_id"] = args[0]
        elif action == "result" and len(args) == 2:
            value.update(connection_id=args[0], item_id=args[1])
        else:
            raise _invalid()
        return validate_workspace_arguments(value, human=True)
    except (PersonalAgentError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


async def run_workspace_command(engine, argument, *, trusted_channel):
    try:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise error("scope_denied")
        arguments = parse_workspace_command(argument)
        if arguments["operation"] == "help":
            return _HELP
        service = getattr(engine, "personal_workspaces", None)
        if service is None:
            raise error("configuration_error")
        result = await execute_workspace_operation(
            service, arguments, trusted_channel=trusted_channel
        )
        rendered = json.dumps(
            serializable_result(result), ensure_ascii=False, allow_nan=False, indent=2
        )
        # Rich interprets ANSI independently of markup=False. Keep secrets plain text.
        return "".join(
            f"\\u{ord(char):04x}" if ord(char) == 27 or 127 <= ord(char) <= 159 else char
            for char in rendered
        )
    except PersonalAgentError as exc:
        message = error(exc.code).message
    except (TypeError, ValueError, UnicodeError, RecursionError):
        message = "工作区命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = error("workspace_unknown").message
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    return message


parse_workspaces_command = parse_workspace_command
run_workspaces_command = run_workspace_command
