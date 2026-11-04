"""Shared /library commands through the Engine's authorized artifact tool."""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any

from naumi_agent.personal.artifact_models import (
    ARTIFACT_ID,
    GRANT_ID,
    PRINCIPAL,
    REQUEST_KEY,
    SHA256,
    normalize_refs,
    strict_json_loads,
    text,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolCall, ToolExecutionError

_HELP = """资料库命令：
/library list [material|output] [搜索词] — 查看当前 Agent 的资料与产物
/library get <产物ID> [版本] — 查看确切版本与来源
/library read <产物ID> <版本> [偏移] [长度] — 读取实际正文分片
/library versions <产物ID> — 查看可访问的版本
/library grants <产物ID> <版本> — 查看原版本分享记录
/library publish-json '<JSON对象>' — 发布正文或 Page
/library import-json '<JSON对象>' — 导入真实文件
/library revise-json '<JSON对象>' — 按 expected_version 修订
/library share-json '<JSON对象>' — 明确分享指定版本给已有 Agent
/library revoke-json '<JSON对象>' — 按 expected_revision 撤销原授权
写入操作需要稳定 request_id；重试保留原编号与原 JSON。"""
_JSON_FIELDS = {
    "publish": {"request_id", "title", "kind", "filename", "content", "source_refs"},
    "import": {"request_id", "title", "kind", "path", "source_refs"},
    "revise": {
        "request_id",
        "artifact_id",
        "expected_version",
        "title",
        "kind",
        "filename",
        "content",
        "path",
        "source_refs",
    },
    "share": {"artifact_id", "version", "recipient_id", "request_id"},
    "revoke": {"grant_id", "expected_revision", "request_id"},
}
_JSON_REQUIRED = {
    "publish": {"request_id", "title", "filename", "content"},
    "import": {"request_id", "title", "path"},
    "revise": {"request_id", "artifact_id", "expected_version", "title"},
    "share": {"artifact_id", "version", "recipient_id", "request_id"},
    "revoke": {"grant_id", "expected_revision", "request_id"},
}


def _number(value: str, label: str, *, minimum=1, maximum=10000) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise ValueError(f"{label}需要有效整数")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}需要 {minimum} 至 {maximum} 的整数")
    return result


def _json_arguments(operation: str, raw: str) -> dict:
    fields = strict_json_loads(raw)
    if not isinstance(fields, dict):
        raise ValueError("资料操作需要完整 JSON 对象")
    if set(fields) - _JSON_FIELDS[operation]:
        raise ValueError("JSON 包含未知字段；身份、会话与 operation 由当前命令确定")
    if "request_id" not in fields:
        raise ValueError("请填写稳定的 request_id；核对或重试时保留原编号与原 JSON")
    text(fields["request_id"], "请求编号", 128, REQUEST_KEY)
    if not _JSON_REQUIRED[operation] <= set(fields):
        raise ValueError("JSON 缺少该资料操作的必要字段，请查看 /library help")
    for key, maximum, pattern in (
        ("artifact_id", 36, ARTIFACT_ID),
        ("grant_id", 36, GRANT_ID),
        ("recipient_id", 35, PRINCIPAL),
        ("title", 120, None),
        ("filename", 240, None),
        ("path", 4096, None),
    ):
        if key in fields:
            text(fields[key], key, maximum, pattern)
    if "kind" in fields and fields["kind"] not in ("material", "output"):
        raise ValueError("kind 需要 material 或 output")
    for key, maximum in (("version", 10000), ("expected_version", 10000), ("expected_revision", 2)):
        if key in fields:
            value = fields[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{key} 需要 1 至 {maximum} 的整数")
    if "content" in fields and (not isinstance(fields["content"], str) or not fields["content"]):
        raise ValueError("content 需要非空正文")
    if "source_refs" in fields:
        fields["source_refs"] = normalize_refs(fields["source_refs"])
    if operation == "revise":
        if "path" in fields:
            if "content" in fields or "filename" in fields:
                raise ValueError("修订请选择实际文件路径，或正文与文件名，不要同时填写")
        elif not {"content", "filename"} <= set(fields):
            raise ValueError("修订需要 content 与 filename，或真实文件 path")
    return {"operation": operation, **fields}


def parse_library_command(argument: str) -> dict[str, Any]:
    """Parse one command without reading files, changing identity or minting requests."""
    if not isinstance(argument, str) or len(argument) > 42_000_000:
        raise ValueError("资料命令过长，请使用真实文件导入")
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise ValueError("引号未闭合，请用单引号包裹完整 JSON 对象") from exc
    if not parts:
        return {"operation": "list"}
    action, *arguments = parts
    if action in {"help", "-h", "--help"} and not arguments:
        return {"operation": "help"}
    if action.endswith("-json") and action[:-5] in _JSON_FIELDS and len(arguments) == 1:
        return _json_arguments(action[:-5], arguments[0])
    if action == "list":
        result = {"operation": "list"}
        if arguments and arguments[0] in {"material", "output"}:
            result["kind"] = arguments.pop(0)
        if arguments:
            result["query"] = text(" ".join(arguments), "搜索词", 200)
        return result
    if action == "get" and 1 <= len(arguments) <= 2:
        identity = text(arguments[0], "产物编号", 36, ARTIFACT_ID)
        return {
            "operation": "get",
            "artifact_id": identity,
            **({"version": _number(arguments[1], "产物版本")} if len(arguments) == 2 else {}),
        }
    if action == "versions" and len(arguments) == 1:
        return {"operation": action, "artifact_id": text(arguments[0], "产物编号", 36, ARTIFACT_ID)}
    if action == "grants" and len(arguments) == 2:
        return {
            "operation": action,
            "artifact_id": text(arguments[0], "产物编号", 36, ARTIFACT_ID),
            "version": _number(arguments[1], "产物版本"),
        }
    if action == "read" and 2 <= len(arguments) <= 4:
        result = {
            "operation": action,
            "artifact_id": text(arguments[0], "产物编号", 36, ARTIFACT_ID),
            "version": _number(arguments[1], "产物版本"),
        }
        if len(arguments) >= 3:
            result["offset"] = _number(arguments[2], "正文偏移", minimum=0, maximum=2**63 - 1)
        if len(arguments) == 4:
            result["limit"] = _number(arguments[3], "读取长度", maximum=20000)
        return result
    raise ValueError("资料命令或参数不完整，请查看 /library help")


def _safe(value: Any, *, label=False) -> str:
    result = str(value)
    if label:
        result = result.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return "".join(
        f"\\u{ord(character):04x}"
        if (ord(character) < 32 and character not in "\n\r\t") or ord(character) == 127
        else character
        for character in result
    )


def _artifact(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("资料回执缺少版本记录")
    identity = text(value.get("artifact_id"), "回执产物编号", 36, ARTIFACT_ID)
    number = value.get("version")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 10000:
        raise ValueError("资料回执缺少确切版本")
    content_sha = text(value.get("sha256"), "回执内容摘要", 64, SHA256)
    title = _safe(value.get("title", identity), label=True)
    lines = [title, f"产物：{identity} · v{number}", f"SHA-256：{content_sha}"]
    if "owner_id" in value:
        lines.append("所属 Agent：" + _safe(value["owner_id"], label=True))
    if "mime_type" in value:
        lines.append(
            "文件："
            + _safe(value["mime_type"], label=True)
            + (f" · {_safe(value['bytes'], label=True)} 字节" if "bytes" in value else "")
        )
    grant = value.get("grant_id")
    if grant:
        lines.append(
            f"授权：{_safe(grant, label=True)} · r{_safe(value.get('grant_revision'), label=True)}"
        )
    elif grant == "":
        lines.append("授权：自有资料")
    refs = value.get("source_refs", [])
    if not isinstance(refs, list):
        raise ValueError("资料来源回执格式不正确")
    if refs:
        lines.append("来源：")
        for reference in refs:
            if not isinstance(reference, dict):
                raise ValueError("资料来源缺少确切版本")
            lines.append(
                f"- {_safe(reference.get('artifact_id'), label=True)}"
                f" · v{_safe(reference.get('version'), label=True)}"
                f" · {_safe(reference.get('sha256'), label=True)}"
                f" · 授权 {_safe(reference.get('grant_id') or '自有', label=True)}"
                f" r{_safe(reference.get('grant_revision'), label=True)}"
            )
    if value.get("evidence"):
        lines.extend(
            [
                "执行依据：",
                json.dumps(value["evidence"], ensure_ascii=False, indent=2, allow_nan=False),
            ]
        )
    return "\n".join(lines)


def _grant(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("分享回执缺少原授权记录")
    identity = text(value.get("grant_id", value.get("id")), "回执授权编号", 36, GRANT_ID)
    artifact_id = text(value.get("artifact_id"), "回执产物编号", 36, ARTIFACT_ID)
    version, revision = value.get("version"), value.get("revision")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1
        for item in (version, revision)
    ):
        raise ValueError("分享回执缺少确切版本")
    status = value.get("status", "revoked" if value.get("revoked_at") else "active")
    state = {
        "active": "已授权",
        "revoked": "已撤销",
        "unknown": "结果待核对",
        "blocked": "授权待确认",
        "suspended": "授权待确认",
    }.get(status, "状态待核对")
    result = [
        f"授权：{identity} · r{revision} · {state}",
        f"产物：{artifact_id} · v{version}",
        "接收 Agent："
        + _safe(value.get("recipient_name") or value.get("recipient_id", ""), label=True),
    ]
    if value.get("revoked_at"):
        result.append("撤销时间：" + _safe(value["revoked_at"], label=True))
    scope = value.get("recipient_scope")
    if isinstance(scope, dict) and scope.get("kind") == "team":
        result.append(
            f"团队快照：{_safe(scope.get('team_id'), label=True)}"
            f" · v{_safe(scope.get('team_version'), label=True)}"
        )
        result.append(
            "当时成员："
            + "、".join(_safe(item, label=True) for item in scope.get("member_ids", []))
        )
    return "\n".join(result)


def _format(operation: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("资料回执需要 JSON 对象")
    if payload.get("error") or payload.get("status") in {"error", "failed", "unknown", "blocked"}:
        detail = payload.get("detail", payload.get("error", payload))
        rendered = (
            detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, indent=2)
        )
        state = "结果待核对" if payload.get("status") == "unknown" else "操作未完成"
        return state + "：\n" + _safe(rendered)
    if operation == "list":
        values = payload.get("artifacts")
        if not isinstance(values, list):
            raise ValueError("资料列表回执缺少 artifacts")
        return "\n\n".join(_artifact(value) for value in values) if values else "资料库为空。"
    if operation == "versions":
        values = payload.get("versions")
        if not isinstance(values, list):
            raise ValueError("版本回执缺少 versions")
        return "\n\n".join(_artifact(value) for value in values) if values else "没有可访问的版本。"
    if operation == "grants":
        values = payload.get("grants")
        if not isinstance(values, list):
            raise ValueError("分享回执缺少 grants")
        return "\n\n".join(_grant(value) for value in values) if values else "该版本没有分享记录。"
    if operation in {"share", "revoke"}:
        return _grant(payload.get(operation))
    if operation == "read":
        result = _artifact(payload.get("artifact"))
        if payload.get("text_available") is False:
            return (
                result
                + "\n"
                + _safe(payload.get("message", "该文件没有文本正文，请打开资料页预览。"))
            )
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("正文回执缺少实际内容")
        offset, total, next_offset = (
            payload.get("offset"),
            payload.get("total_chars"),
            payload.get("next_offset"),
        )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (offset, total)
        ):
            raise ValueError("正文回执缺少确切偏移与总长度")
        result += (
            f"\n正文：偏移 {offset} · 返回 {len(content)} 字符"
            f" · 总计 {total} 字符\n{_safe(content)}"
        )
        if next_offset is not None:
            if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset < 0:
                raise ValueError("正文回执的后续偏移不正确")
            result += f"\n下一段偏移：{next_offset}"
        return result
    result = _artifact(payload.get("get") if operation == "get" else payload.get("artifact"))
    if payload.get("url"):
        result += "\n打开：" + _safe(payload["url"], label=True)
    return result


async def run_library_command(engine: Any, argument: str) -> str:
    """Dispatch only through execute_tool, preserving the caller's stable request ID."""
    try:
        fields = parse_library_command(argument)
    except ValueError as exc:
        return str(exc)
    if fields["operation"] == "help":
        return _HELP
    try:
        result = await engine.execute_tool(
            ToolCall(
                id="library-command-" + uuid.uuid4().hex,
                name="personal_artifacts",
                arguments=json.dumps(fields, ensure_ascii=False, allow_nan=False),
            ),
            agent_name="cli",
        )
    except (PersonalAgentError, ToolExecutionError) as exc:
        request = "\n请求编号：" + fields["request_id"] if "request_id" in fields else ""
        return "操作未完成" + request + "\n" + _safe(exc)
    if result.status != "success":
        prefix = "结果待核对" if result.status == "unknown" else "操作未完成"
        request = "\n请求编号：" + fields["request_id"] if "request_id" in fields else ""
        return prefix + request + "\n" + _safe(result.content)
    try:
        payload = strict_json_loads(result.content)
        return _format(fields["operation"], payload)
    except (TypeError, ValueError, RecursionError) as exc:
        return (
            "资料工具回执格式有误，请保留原请求核对执行记录。\n"
            + _safe(exc)
            + "\n原回执："
            + _safe(result.content)
        )
