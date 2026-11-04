"""Strict, JSON-stable peer grants and explicit manual-dispatch inputs."""

from __future__ import annotations

import re
from typing import Any

from naumi_agent.personal.models import PersonalAgentError

_AGENT = re.compile(r"^pa_[0-9a-f]{32}$")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def text(value: Any, label: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or _CONTROL.search(value):
        raise PersonalAgentError("invalid_input", f"{label}必须是有效文字")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise PersonalAgentError("invalid_input", f"{label}包含无效编码") from exc
    if len(value) > maximum or (required and not value.strip()):
        raise PersonalAgentError("invalid_input", f"请填写有效的{label}，最多 {maximum} 个字符")
    return value


def agent_id(value: Any) -> str:
    if not isinstance(value, str) or not _AGENT.fullmatch(value):
        raise PersonalAgentError("invalid_input", "Agent 编号格式不正确")
    return value


def version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise PersonalAgentError("invalid_input", "Agent 版本必须是正整数")
    return value


def tool_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        raise PersonalAgentError("invalid_input", "互询工具必须是最多 128 项的列表")
    if any(not isinstance(name, str) or not _TOOL.fullmatch(name) for name in value):
        raise PersonalAgentError("invalid_input", "互询工具名称不正确")
    if len(set(value)) != len(value):
        raise PersonalAgentError("invalid_input", "互询工具包含重复项目")
    return sorted(value)


def grants(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise PersonalAgentError("invalid_input", "互询范围必须是最多 128 位 Agent 的列表")
    result, seen = [], set()
    for raw in value:
        if (
            not isinstance(raw, dict)
            or set(raw) - {"agent_id", "agent_version", "tool_names"}
            or not {"agent_id", "agent_version"} <= set(raw)
        ):
            raise PersonalAgentError("invalid_input", "互询范围缺少 Agent 编号或版本")
        identity = agent_id(raw["agent_id"])
        if identity in seen:
            raise PersonalAgentError("invalid_input", "互询范围包含重复 Agent")
        seen.add(identity)
        item = {"agent_id": identity, "agent_version": version(raw["agent_version"])}
        if "tool_names" in raw:
            item["tool_names"] = tool_names(raw["tool_names"])
        result.append(item)
    return sorted(result, key=lambda item: item["agent_id"])


def dispatch(value: Any) -> dict:
    fields = {"recipient_agent_id", "recipient_agent_version", "request_id", "content", "context"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PersonalAgentError("invalid_input", "消息派送参数缺少必填字段或包含无关字段")
    request = text(value["request_id"], "消息请求编号", 128)
    if not _REQUEST.fullmatch(request):
        raise PersonalAgentError("invalid_input", "消息请求编号格式不正确")
    return {
        "recipient_agent_id": agent_id(value["recipient_agent_id"]),
        "recipient_agent_version": version(value["recipient_agent_version"]),
        "request_id": request,
        "content": text(value["content"], "消息内容", 16000),
        "context": text(value["context"], "消息共享资料", 32000, required=False),
    }
