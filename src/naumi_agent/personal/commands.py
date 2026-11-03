"""Manual personal-agent commands using the same authorized tool as the model."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any

from naumi_agent.tools.base import ToolCall

_FIELDS = frozenset(
    {
        "name",
        "description",
        "instructions",
        "model_tier",
        "tool_names",
        "kind",
        "member_ids",
    }
)
_STATUS_LABELS = {"active": "启用", "paused": "暂停", "archived": "归档"}
_TIER_LABELS = {"fast": "快速", "capable": "标准", "reasoning": "推理"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HELP = """个人助理命令：
/assistants — 列出已创建的专员
/assistants list [all] — 列出专员，all 包含归档
/assistants get <ID> — 查看职责与配置
/assistants tools — 查看可配置工具
/assistants create "名称" "职责" — 创建专员，初始不授予工具
/assistants create-json '<JSON对象>' — 指定职责、工具与团队成员创建
/assistants update-json <ID> <版本> '<JSON对象>' — 修改指定版本的配置
/assistants open <ID> — 打开专员会话
/assistants pause|resume|archive <ID> <版本> — 暂停、恢复或归档
/assistants history <ID> — 查看配置修订记录"""


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("助理 ID 格式无效，请从 /assistants 列表复制。")
    return value


def _version(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError("版本必须是正整数，请先查看助理的当前版本。")
    version = int(value)
    if version < 1 or version > 2_147_483_647:
        raise ValueError("版本必须是正整数，请先查看助理的当前版本。")
    return version


def _object(value: str, *, create: bool) -> dict[str, Any]:
    try:
        fields = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise ValueError("JSON 格式有误，请用单引号包裹完整 JSON 对象。") from exc
    allowed = (_FIELDS | {"request_id"}) if create else ((_FIELDS - {"kind"}) | {"status"})
    if not isinstance(fields, dict) or not fields:
        raise ValueError("配置必须是非空 JSON 对象。")
    if set(fields) - allowed:
        raise ValueError("配置包含未知字段，请仅填写助理的名称、职责、模型、工具或成员。")
    if create and not {"name", "instructions"} <= fields.keys():
        raise ValueError("创建助理需要 name（名称）和 instructions（职责）。")
    return fields


def parse_assistants_command(argument: str) -> dict[str, Any]:
    """Parse one bounded command into tool arguments without executing anything."""
    if len(argument) > 16000:
        raise ValueError("命令过长，请缩短职责内容或通过助理配置界面编辑。")
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise ValueError("引号未闭合，请检查名称、职责或 JSON 的引号。") from exc
    if not parts:
        return {"operation": "list"}
    action, *args = parts
    if action == "list" and (not args or args == ["all"]):
        return {"operation": "list", "include_archived": bool(args)}
    if action == "tools" and not args:
        return {"operation": "tools"}
    if action in {"get", "history", "open"} and len(args) == 1:
        return {"operation": action, "agent_id": _identifier(args[0])}
    if action == "create" and len(args) == 2:
        name, instructions = (part.strip() for part in args)
        if not name or not instructions:
            raise ValueError("名称和职责都需要填写。")
        return {
            "operation": "create",
            "name": name,
            "instructions": instructions,
            "tool_names": [],
            "request_id": uuid.uuid4().hex,
        }
    if action == "create-json" and len(args) == 1:
        fields = _object(args[0], create=True)
        return {"operation": "create", "request_id": uuid.uuid4().hex, **fields}
    if action == "update-json" and len(args) == 3:
        return {
            "operation": "update",
            "agent_id": _identifier(args[0]),
            "expected_version": _version(args[1]),
            **_object(args[2], create=False),
        }
    if action in {"pause", "resume", "archive"} and len(args) == 2:
        return {
            "operation": "update",
            "agent_id": _identifier(args[0]),
            "expected_version": _version(args[1]),
            "status": {"pause": "paused", "resume": "active", "archive": "archived"}[action],
        }
    raise ValueError(_HELP)


def _display(value: Any) -> str:
    # Keep names and tool output from injecting terminal controls or extra table rows.
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or "")).split())[:500]


def _profile_summary(agent: dict[str, Any]) -> str:
    name = _display(agent.get("name"))
    identity = _display(agent.get("id"))
    version = _display(agent.get("version"))
    status = _STATUS_LABELS.get(str(agent.get("status")), "未知")
    return f"{name} · {identity} · v{version} · {status}"


def _format_result(operation: str, payload: Any) -> str:
    if operation == "tools" and isinstance(payload, dict):
        tools = payload.get("tools")
        if isinstance(tools, list):
            if not tools:
                return "当前没有可分配给专员的工具。"
            return "可配置工具\n" + "\n".join(
                f"{_display(tool.get('name'))}：{_display(tool.get('description'))}"
                for tool in tools
                if isinstance(tool, dict)
            )
    if operation == "list":
        agents = payload.get("agents") if isinstance(payload, dict) else payload
        if isinstance(agents, list):
            if not agents:
                return '尚未创建专员。输入 /assistants create "名称" "职责" 创建。'
            return "我的助理\n" + "\n".join(
                _profile_summary(agent) for agent in agents if isinstance(agent, dict)
            )
    if operation == "history":
        revisions = payload.get("revisions") if isinstance(payload, dict) else payload
        if isinstance(revisions, list):
            return "配置修订记录\n" + "\n".join(
                f"{_profile_summary(agent)} · {_display(agent.get('updated_at'))}"
                for agent in revisions
                if isinstance(agent, dict)
            )
    agent = payload.get("agent", payload) if isinstance(payload, dict) else None
    if isinstance(agent, dict) and "id" in agent:
        labels = {"create": "已创建", "update": "已更新", "get": "助理配置"}
        tools = agent.get("tool_names", [])
        tool_names = "、".join(_display(item) for item in tools) if isinstance(tools, list) else ""
        lines = [f"{labels.get(operation, '助理')}：{_profile_summary(agent)}"]
        lines.append(f"模型：{_TIER_LABELS.get(str(agent.get('model_tier')), '未知')}")
        lines.append(f"工具：{tool_names or '无'}")
        lines.append(f"职责：{_display(agent.get('instructions'))}")
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def run_assistants_command(engine: Any, argument: str) -> str:
    """Run through Engine authorization; only an explicit manual open changes sessions."""
    if argument.strip() in {"help", "--help", "-h"}:
        return _HELP
    try:
        arguments = parse_assistants_command(argument)
    except ValueError as exc:
        return str(exc)
    result = await engine.execute_tool(
        ToolCall(
            id=f"personal-command-{uuid.uuid4().hex}",
            name="personal_agents",
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
        agent_name="cli",
    )
    if result.status != "success":
        return str(result.content)
    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError, RecursionError):
        if arguments["operation"] == "open":
            return "助理会话回执格式有误，当前会话保持不变，请重试。"
        return str(result.content)
    if isinstance(payload, dict) and payload.get("error"):
        return f"操作未完成：{_display(payload['error'])}"
    if arguments["operation"] == "open":
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id):
            return "助理会话回执缺少有效会话 ID，当前会话保持不变，请重试。"
        if not await engine.load_session(session_id):
            return "助理会话暂未就绪，请重试打开；助理档案仍然保留。"
        return f"已打开助理 {_display(arguments['agent_id'])} 的会话 {session_id}。"
    return _format_result(arguments["operation"], payload)
