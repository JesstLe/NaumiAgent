"""One parser and executor for trusted human CLI/TUI calendar commands."""

from __future__ import annotations

import re
import shlex
import uuid
from pathlib import Path

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.connection_tools import (
    MAX_REQUEST_BYTES,
    ActionApproval,
    ActionProposal,
    ClientPayload,
    DisconnectPayload,
    GrantPayload,
    MutationPayload,
    OAuthPayload,
    RevokePayload,
    payload,
)
from naumi_agent.personal.models import PersonalAgentError


def _invalid():
    return PersonalAgentError("invalid_input", "日程命令参数不正确，请核对命令与原记录")


def _manual(channel):
    if channel not in {"manual_cli", "manual_tui", "manual_api"}:
        raise PersonalAgentError("scope_denied", "请从人工命令入口执行账户操作")


def _parts(arguments):
    if type(arguments) is not str:
        raise _invalid()
    try:
        if len(arguments.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        return shlex.split(arguments)
    except (ValueError, UnicodeError):
        pass
    raise _invalid()


def _check_text(arguments):
    failed = False
    try:
        if type(arguments) is not str or len(arguments.encode("utf-8")) > MAX_REQUEST_BYTES:
            failed = True
    except UnicodeError:
        failed = True
    if failed:
        raise _invalid()


def _revision(raw):
    if not raw.isascii() or not raw.isdecimal() or not 1 <= len(raw) <= 16:
        raise _invalid()
    result = int(raw)
    if not 1 <= result <= 2**53 - 1:
        raise _invalid()
    return result


def parse_connection_command(arguments: str):
    _check_text(arguments)
    if type(arguments) is str and arguments.strip().startswith("client "):
        path = arguments.strip()[7:].strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1]
        if not path or len(path) > 4096:
            raise _invalid()
        return {"operation": "client", "path": path}
    parts = _parts(arguments)
    if not parts:
        return {"operation": "list"}
    operation, *args = parts
    if operation in {"context", "list"} and not args:
        return {"operation": operation}
    if operation == "connect":
        if args.count("--write") > 1:
            raise _invalid()
        write = "--write" in args
        args = [arg for arg in args if arg != "--write"]
        if len(args) <= 1 and (not args or re.fullmatch(r"pc_[a-f0-9]{32}", args[0])):
            return {
                "operation": operation,
                "write": write,
                "connection_id": args[0] if args else None,
            }
    if (
        operation in {"events", "request"}
        and len(args) == 1
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}" if operation == "request" else r"pc_[a-f0-9]{32}",
            args[0],
        )
    ):
        return {"operation": operation, "id": args[0]}
    if (
        operation in {"sync", "disconnect"}
        and len(args) == 2
        and re.fullmatch(r"pc_[a-f0-9]{32}", args[0])
    ):
        return {"operation": operation, "id": args[0], "revision": _revision(args[1])}
    if operation == "grant" and len(args) == 4 and re.fullmatch(r"pc_[a-f0-9]{32}", args[0]):
        return {
            "operation": operation,
            "id": args[0],
            "revision": _revision(args[1]),
            "actor_id": args[2],
            "actor_version": _revision(args[3]),
        }
    if operation == "revoke" and len(args) == 3 and re.fullmatch(r"pc_[a-f0-9]{32}", args[0]):
        return {
            "operation": operation,
            "id": args[0],
            "revision": _revision(args[1]),
            "actor_id": args[2],
        }
    raise _invalid()


def parse_action_command(arguments: str):
    _check_text(arguments)
    if type(arguments) is str and arguments.strip().startswith("propose "):
        raw = arguments.strip()[8:].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _invalid()
        value = strict_json_loads(raw)
        if not isinstance(value, dict):
            raise _invalid()
        return {"operation": "propose", "payload": value}
    parts = _parts(arguments)
    if not parts or parts == ["list"]:
        return {"operation": "list"}
    operation, *args = parts
    if operation == "get" and len(args) == 1 and re.fullmatch(r"ca_[a-f0-9]{32}", args[0]):
        return {"operation": operation, "id": args[0]}
    if operation == "approve" and len(args) == 3 and re.fullmatch(r"ca_[a-f0-9]{32}", args[0]):
        return {
            "operation": operation,
            "id": args[0],
            "revision": _revision(args[1]),
            "digest": args[2],
        }
    if (
        operation in {"reject", "reconcile"}
        and len(args) == 2
        and re.fullmatch(r"ca_[a-f0-9]{32}", args[0])
    ):
        return {"operation": operation, "id": args[0], "revision": _revision(args[1])}
    raise _invalid()


async def _write(operation, request_id):
    error = None
    try:
        result = await operation()
        return {"request_id": request_id, "result": result}
    except PersonalAgentError as exc:
        error = (exc.code, exc.message)
    except (TimeoutError, ConnectionError):
        error = ("connection_unknown", "原日程操作结果待核对，请查询原请求")
    except Exception:
        error = ("connection_unknown", "原日程操作结果待核对，请查询原请求")
    if error:
        failure = PersonalAgentError(error[0], f"{error[1]}；请求编号：{request_id}")
        failure.request_id = request_id
        raise failure


async def _manual_events(service, identity):
    from naumi_agent.personal.runtime import require_current_agent

    engine = service.engine
    if getattr(engine, "_session", None) is None:
        await engine.get_or_create_session()
    session = getattr(engine, "_session", None)
    if session is None:
        raise PersonalAgentError("binding_conflict", "请先打开有效对话后读取日程")
    session_id = session.id
    profile = require_current_agent(engine)
    actor = profile.id if profile is not None else "main"
    pinned = await service.tool_events(identity, actor, limit=50)
    page = await service.events(identity, actor_id=actor, limit=50, offset=0)
    current = require_current_agent(engine)
    current_actor = current.id if current is not None else "main"
    if (
        getattr(getattr(engine, "_session", None), "id", None) != session_id
        or current_actor != actor
    ):
        raise PersonalAgentError("binding_conflict", "读取期间对话身份已变化，请在原对话重试")
    try:
        expected = [(item["event_id"], item["source_ref"]) for item in pinned["events"]]
        observed = [(item["event_id"], item["source_ref"]) for item in page["events"]]
    except (KeyError, TypeError):
        raise PersonalAgentError("storage_error", "日程来源绑定记录未通过校验") from None
    if expected != observed:
        raise PersonalAgentError("version_conflict", "日程在读取期间已更新，请重新读取原日程")
    return page


async def execute_connection_command(service, arguments: str, *, trusted_channel):
    _manual(trusted_channel)
    command = parse_connection_command(arguments)
    operation = command["operation"]
    if operation == "context":
        return await service.context()
    if operation == "list":
        return await service.list_connections()
    if operation == "events":
        return await _manual_events(service, command["id"])
    if operation == "request":
        return await service.request_status(command["id"])
    request_id = uuid.uuid4().hex
    value = {"request_id": request_id}
    if operation == "client":
        failed = False
        try:
            with Path(command["path"]).expanduser().open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            value["client_json"] = raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            failed = True
        if failed:
            raise PersonalAgentError("invalid_input", "桌面 OAuth 配置读取失败或超过 16 KB")
        value = payload(ClientPayload, value)
        return await _write(
            lambda: service.configure_client(value, trusted_channel=trusted_channel), request_id
        )
    if operation == "connect":
        value["write"] = command["write"]
        if command["connection_id"]:
            records = await service.list_connections()
            matched = [
                record
                for record in records["connections"]
                if record["id"] == command["connection_id"]
            ]
            if len(matched) != 1:
                raise PersonalAgentError("not_found", "原账户连接不存在")
            value.update(connection_id=matched[0]["id"], expected_revision=matched[0]["revision"])
        value = payload(OAuthPayload, value)
        return await _write(
            lambda: service.start_oauth(value, trusted_channel=trusted_channel), request_id
        )
    value["expected_revision"] = command["revision"]
    identity = command["id"]
    if operation == "sync":
        value = payload(MutationPayload, value)
        return await _write(
            lambda: service.sync(identity, value, trusted_channel=trusted_channel), request_id
        )
    if operation == "disconnect":
        value = payload(DisconnectPayload, {**value, "remote_revoke": False})
        return await _write(
            lambda: service.disconnect(identity, value, trusted_channel=trusted_channel), request_id
        )
    value["actor_id"] = command["actor_id"]
    revoke = operation == "revoke"
    if not revoke:
        value["actor_version"] = command["actor_version"]
    value = payload(RevokePayload if revoke else GrantPayload, value)
    return await _write(
        lambda: service.grant(identity, value, trusted_channel=trusted_channel, revoke=revoke),
        request_id,
    )


async def execute_action_command(service, arguments: str, *, trusted_channel):
    _manual(trusted_channel)
    command = parse_action_command(arguments)
    operation = command["operation"]
    if operation == "list":
        return await service.list_actions()
    if operation == "get":
        return await service.get_action(command["id"])
    if operation == "propose":
        value = dict(command["payload"])
        if "request_id" not in value:
            value["request_id"] = uuid.uuid4().hex
        value = payload(ActionProposal, value)
        return await _write(lambda: service.prepare_action(value), value["request_id"])
    request_id = uuid.uuid4().hex
    value = {"request_id": request_id, "expected_revision": command["revision"]}
    identity = command["id"]
    if operation == "approve":
        value = payload(ActionApproval, {**value, "expected_digest": command["digest"]})
        return await _write(
            lambda: service.approve_action(identity, value, trusted_channel=trusted_channel),
            request_id,
        )
    value = payload(MutationPayload, value)
    if operation == "reject":
        return await _write(
            lambda: service.reject_action(identity, value, trusted_channel=trusted_channel),
            request_id,
        )
    return await _write(lambda: service.reconcile_action(identity, value), request_id)
