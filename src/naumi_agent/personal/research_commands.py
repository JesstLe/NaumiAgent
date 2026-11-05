"""Human research commands share strict operations with the model and HTTP adapters."""

from __future__ import annotations

import json
import shlex

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_tools import (
    MANUAL_CHANNELS,
    MAX_REQUEST_BYTES,
    execute_research_operation,
    public_error,
    serializable_result,
    validate_research_arguments,
)

_HELP = """研究命令：
/research context
/research list [--limit N] [--after 原研究ID]
/research get <研究ID> [--revision N]
/research source <来源ID> [--revision N]
/research history <研究ID> [--limit N] [--after-revision N]
/research request <原request_id>
/research egress-list [--limit N] [--after 原外发ID]
/research egress-get <原外发ID>
/research egress-request <原request_id>
/research <egress-propose|egress-decide|egress-execute>-json '<完整JSON>'
/research <create|revise|search|capture|import|recheck|review|publish>-json '<完整JSON>'"""
_JSON = frozenset(
    {
        "create",
        "revise",
        "search",
        "capture",
        "import",
        "recheck",
        "review",
        "publish",
        "egress-propose",
        "egress-decide",
        "egress-execute",
    }
)


def _invalid():
    return ValueError("研究命令参数不完整或格式不正确，请查看 /research help")


def _options(args, allowed):
    if len(args) % 2:
        raise _invalid()
    result = {}
    for index in range(0, len(args), 2):
        raw, value = args[index : index + 2]
        key = raw.removeprefix("--").replace("-", "_")
        if not raw.startswith("--") or key not in allowed or key in result:
            raise _invalid()
        if key in {"limit", "revision", "after_revision"}:
            if not value.isascii() or not value.isdecimal() or len(value) > 16:
                raise _invalid()
            value = int(value)
        result[key] = value
    return result


def parse_research_command(argument):
    try:
        if type(argument) is not str or len(argument.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        pieces = argument.strip().split(maxsplit=1)
        if not pieces:
            return {"operation": "context"}
        action, suffix = pieces[0], pieces[1] if len(pieces) == 2 else ""
        if action in {"help", "--help", "-h"} and not suffix:
            return {"operation": "help"}
        if action.endswith("-json") and action[:-5] in _JSON:
            raw = suffix.strip()
            if raw[:1] in {"'", '"'}:
                quoted = shlex.split(raw)
                if len(quoted) != 1:
                    raise _invalid()
                raw = quoted[0]
            body = strict_json_loads(raw)
            if type(body) is not dict or "operation" in body:
                raise _invalid()
            return validate_research_arguments(
                {"operation": action[:-5].replace("-", "_"), **body}, human=True
            )
        action, *args = shlex.split(argument)
        action = action.replace("-", "_")
        value = {"operation": action}
        if action == "context" and not args:
            pass
        elif action in {"list", "egress_list"}:
            value.update(_options(args, {"limit", "after"}))
        elif action == "egress_get" and len(args) == 1:
            value["id"] = args[0]
        elif action in {"get", "source", "history"} and args:
            value["id"] = args[0]
            options = {"limit", "after_revision"} if action == "history" else {"revision"}
            value.update(_options(args[1:], options))
        elif action in {"request", "egress_request"} and len(args) == 1:
            value["request_id"] = args[0]
        else:
            raise _invalid()
        return validate_research_arguments(value, human=True)
    except (PersonalAgentError, ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None


async def run_research_command(engine, argument, *, trusted_channel):
    try:
        if type(trusted_channel) is not str or trusted_channel not in MANUAL_CHANNELS:
            raise PersonalAgentError("scope_denied", public_error("scope_denied"))
        arguments = parse_research_command(argument)
        if arguments["operation"] == "help":
            return _HELP
        service = getattr(engine, "personal_research", None)
        if service is None:
            raise PersonalAgentError("configuration_error", public_error("configuration_error"))
        result = await execute_research_operation(
            service,
            arguments,
            trusted_channel=trusted_channel,
        )
        rendered = json.dumps(
            serializable_result(result), ensure_ascii=False, allow_nan=False, indent=2
        )
        return "".join(
            f"\\u{ord(char):04x}" if ord(char) == 27 or 127 <= ord(char) <= 159 else char
            for char in rendered
        )
    except PersonalAgentError as exc:
        message = public_error(exc.code)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        message = "研究命令参数或回执格式不正确，请核对原请求"
    except Exception:
        message = public_error("research_unknown")
    mark = getattr(engine, "mark_manual_command_failed", None)
    if callable(mark):
        mark()
    return message
