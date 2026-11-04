"""Manual personal-agent commands using the same authorized tool as the model."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any

from naumi_agent.personal import peer_models
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.team_models import normalize_client_payload
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
_HELP = """Agent 命令：
/assistants — 列出已创建的Agent
/assistants list [all] — 列出Agent，all 包含归档
/assistants get <ID> — 查看职责与配置
/assistants tools — 查看可配置工具
/assistants create "名称" "职责" — 创建Agent，初始不授予工具
/assistants create-json '<JSON对象>' — 指定职责、工具与团队成员创建
/assistants update-json <ID> <版本> '<JSON对象>' — 修改指定版本的配置
/assistants open <ID> — 打开Agent或团队会话
/assistants pause|resume|archive <ID> <版本> — 暂停、恢复或归档
/assistants history <ID> — 查看配置修订记录
/assistants collaborate-json '<JSON对象>' — 按步骤委派给已有Agent
/assistants collaborations — 查看协作记录
/assistants collaboration <ID> — 查看交接与结果
/assistants cancel-collaboration <ID> — 请求停止协作
/assistants reply-json <协作ID> <问题ID> '<JSON对象>' — 回答协作中的问题
/assistants message-json '<JSON对象>' — 指定已有Agent发送消息
/assistants messages <协作ID> — 查看双方消息记录
/assistants message <消息ID> — 查看完整问题、回答与回执
/assistants message-ack <消息ID> <回答SHA256> — 确认已收到原回答
/assistants team-json '<JSON对象>' — 向已有团队提交持久任务，team-send-json 同义
/assistants team-turns [团队会话ID] — 查看团队任务记录
/assistants team-turn <团队任务ID> — 查看阶段、成员执行与完整结果
/assistants cancel-team-turn <团队任务ID> — 请求停止团队任务"""


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("Agent ID 格式无效，请从 /assistants 列表复制。")
    return value


def _version(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError("版本必须是正整数，请先查看 Agent 的当前版本。")
    version = int(value)
    if version < 1 or version > 2_147_483_647:
        raise ValueError("版本必须是正整数，请先查看 Agent 的当前版本。")
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
        raise ValueError("配置包含未知字段，请仅填写 Agent 的名称、职责、模型、工具或成员。")
    if create and not {"name", "instructions"} <= fields.keys():
        raise ValueError("创建 Agent 需要 name（名称）和 instructions（职责）。")
    return fields


def _team_object(value: str) -> dict[str, Any]:
    def unique_fields(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("团队消息 JSON 包含重复字段")
            result[key] = item
        return result

    def invalid_constant(_value):
        raise ValueError("团队消息 JSON 包含非标准数字")

    try:
        raw = json.loads(value, object_pairs_hook=unique_fields, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("团队消息 JSON 格式有误，请检查引号、重复字段和数字。") from exc
    if not isinstance(raw, dict):
        raise ValueError("团队消息需要完整 JSON 对象。")
    fields = dict(raw)
    source = fields.pop("source_session_id", None)
    if "source_session_id" in raw and (
        not isinstance(source, str) or not _IDENTIFIER.fullmatch(source)
    ):
        raise ValueError("来源会话 ID 格式不正确，请从原始对话发送。")
    try:
        result = normalize_client_payload({"request_id": uuid.uuid4().hex, **fields})
    except PersonalAgentError as exc:
        raise ValueError(exc.message) from exc
    if "source_session_id" in raw:
        result["source_session_id"] = source
    return result


def parse_assistants_command(argument: str) -> dict[str, Any]:
    """Parse one bounded command into tool arguments without executing anything."""
    if len(argument) > 256000:
        raise ValueError("命令过长，请缩短职责内容或通过 Agent 配置界面编辑。")
    try:
        parts = shlex.split(argument)
    except ValueError as exc:
        raise ValueError("引号未闭合，请检查名称、职责或 JSON 的引号。") from exc
    if not parts:
        return {"operation": "list"}
    action, *args = parts
    if action in {"team-json", "team-send-json"} and len(args) == 1:
        return {"operation": "team_send", **_team_object(args[0])}
    if action == "team-turns" and len(args) <= 1:
        if args and not _IDENTIFIER.fullmatch(args[0]):
            raise ValueError("团队会话 ID 格式不正确，请从原始团队对话复制。")
        return {"operation": "team_turns", **({"team_session_id": args[0]} if args else {})}
    if action in {"team-turn", "cancel-team-turn"} and len(args) == 1:
        if not re.fullmatch(r"pt_[0-9a-f]{32}", args[0]):
            raise ValueError("团队任务 ID 格式无效，请从团队任务记录复制。")
        return {"operation": action.replace("-", "_"), "turn_id": args[0]}
    if action == "message-json" and len(args) == 1:
        try:
            fields = json.loads(args[0])
        except (ValueError, RecursionError) as exc:
            raise ValueError("消息 JSON 格式有误，请检查引号。") from exc
        required = {
            "sender_agent_id",
            "sender_agent_version",
            "recipient_agent_id",
            "recipient_agent_version",
            "content",
        }
        optional = {"source_session_id", "request_id", "context", "peer_agents"}
        if (
            not isinstance(fields, dict)
            or not required <= fields.keys()
            or fields.keys() - (required | optional)
        ):
            raise ValueError("消息需要发送与接收Agent的 ID、版本和 content（内容）。")
        fields = {"request_id": uuid.uuid4().hex, **fields}
        try:
            peer_models.agent_id(fields["sender_agent_id"])
            peer_models.version(fields["sender_agent_version"])
            peer_models.dispatch(
                {
                    key: fields.get(key, "")
                    for key in (
                        "recipient_agent_id",
                        "recipient_agent_version",
                        "request_id",
                        "content",
                        "context",
                    )
                }
            )
            if "peer_agents" in fields:
                peer_models.grants(fields["peer_agents"])
        except PersonalAgentError as exc:
            raise ValueError(exc.message) from exc
        if "source_session_id" in fields:
            source = fields["source_session_id"]
            if not isinstance(source, str) or not _IDENTIFIER.fullmatch(source):
                raise ValueError("来源会话 ID 格式不正确，请从原始对话发送。")
        return {"operation": "message_send", **fields}
    if action == "messages" and len(args) == 1:
        if not re.fullmatch(r"pc_[0-9a-f]{32}", args[0]):
            raise ValueError("协作 ID 格式无效，请从协作记录复制。")
        return {"operation": "messages", "collaboration_id": args[0]}
    if action in {"message", "message-ack"} and len(args) == (2 if action == "message-ack" else 1):
        if not re.fullmatch(r"pm_[0-9a-f]{32}", args[0]):
            raise ValueError("消息 ID 格式无效，请从双方消息记录复制。")
        result = {"operation": action.replace("-", "_"), "message_id": args[0]}
        if action == "message-ack":
            if not re.fullmatch(r"[0-9a-f]{64}", args[1]):
                raise ValueError("回答摘要格式不正确，请复制原回答的 SHA256。")
            result["expected_response_sha256"] = args[1]
        return result
    if action == "collaborations" and not args:
        return {"operation": "collaborations"}
    if action == "reply-json" and len(args) == 3:
        if not re.fullmatch(r"pc_[0-9a-f]{32}", args[0]) or not re.fullmatch(
            r"ask-[0-9a-f]{32}", args[1]
        ):
            raise ValueError("协作或问题 ID 格式不正确，请从原始记录复制。")
        try:
            response = json.loads(args[2])
        except (ValueError, RecursionError) as exc:
            raise ValueError("回答 JSON 格式有误，请检查引号。") from exc
        return {
            "operation": "collaboration_reply",
            "collaboration_id": args[0],
            "question_id": args[1],
            "response": response,
        }
    if action in {"collaboration", "cancel-collaboration"} and len(args) == 1:
        if not re.fullmatch(r"pc_[0-9a-f]{32}", args[0]):
            raise ValueError("协作 ID 格式无效，请从协作记录复制。")
        return {"operation": action.replace("-", "_"), "collaboration_id": args[0]}
    if action == "collaborate-json" and len(args) == 1:
        try:
            fields = json.loads(args[0])
        except (ValueError, RecursionError) as exc:
            raise ValueError("协作 JSON 格式有误，请检查引号。") from exc
        if (
            not isinstance(fields, dict)
            or set(fields)
            - {"source_session_id", "request_id", "title", "context", "steps", "peer_agents"}
            or "steps" not in fields
        ):
            raise ValueError(
                "协作需要 steps，可指定 title、context、request_id、来源对话和 peer_agents。"
            )
        return {"operation": "collaborate", "request_id": uuid.uuid4().hex, **fields}
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
    if operation == "team_turns" and isinstance(payload, dict):
        turns = payload.get("turns", [])
        if not turns:
            return "暂无团队任务。"
        return "团队任务记录\n" + "\n".join(
            _team_turn_summary(turn) for turn in turns if isinstance(turn, dict)
        )
    if operation in {"team_send", "team_turn", "cancel_team_turn"} and isinstance(payload, dict):
        return _format_team_turn(payload)
    if operation == "messages" and isinstance(payload, dict):
        messages = payload.get("messages", [])
        if not messages:
            return "暂无Agent消息。"
        return "双方消息\n" + "\n".join(_message_summary(message) for message in messages)
    if operation in {"message", "message_ack"} and isinstance(payload, dict):
        lines = [_message_summary(payload)]
        for key, label in (
            ("content", "问题"),
            ("context", "共享资料"),
            ("response", "回答"),
            ("error", "执行结果"),
        ):
            if payload.get(key):
                lines.append(label + "：\n" + _message_text(payload[key]))
        if payload.get("response_sha256"):
            lines.append("回答 SHA256：" + _display(payload["response_sha256"]))
        if payload.get("acknowledged_at"):
            actor = {"user": "用户", "agent": "发送Agent"}.get(
                payload.get("acknowledged_by"), "原确认方"
            )
            lines.append(f"{actor}已确认收到：{_display(payload['acknowledged_at'])}")
        return "\n".join(lines)
    if operation == "collaboration_reply":
        return "回答已记录：" + _display(payload.get("request_id"))
    if operation == "collaborations" and isinstance(payload, dict):
        values = payload.get("collaborations", [])
        if not values:
            return "暂无协作记录。"
        return "协作记录\n" + "\n".join(
            f"{_display(p.get('id'))} · {_display(p.get('title'))} · "
            f"{_collaboration_status(p.get('status'))}"
            for p in values
        )
    if operation in {"collaborate", "collaboration", "cancel_collaboration", "message_send"}:
        lines = [
            f"协作：{_display(payload.get('id'))} · {_collaboration_status(payload.get('status'))}"
        ]
        for step in payload.get("steps", []):
            lines.append(
                f"{_display(step.get('id'))} · {_display(step.get('agent_id'))} · "
                f"{_collaboration_status(step.get('status'))}"
            )
            if step.get("response") or step.get("error"):
                lines.append(_display(step.get("response") or step.get("error")))
        return "\n".join(lines)
    if operation == "tools" and isinstance(payload, dict):
        tools = payload.get("tools")
        if isinstance(tools, list):
            if not tools:
                return "当前没有可分配给Agent的工具。"
            return "可配置工具\n" + "\n".join(
                f"{_display(tool.get('name'))}：{_display(tool.get('description'))}"
                for tool in tools
                if isinstance(tool, dict)
            )
    if operation == "list":
        agents = payload.get("agents") if isinstance(payload, dict) else payload
        if isinstance(agents, list):
            if not agents:
                return '尚未创建Agent。输入 /assistants create "名称" "职责" 创建。'
            return "我的 Agent\n" + "\n".join(
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
        labels = {"create": "已创建", "update": "已更新", "get": "Agent 配置"}
        tools = agent.get("tool_names", [])
        tool_names = "、".join(_display(item) for item in tools) if isinstance(tools, list) else ""
        lines = [f"{labels.get(operation, 'Agent')}：{_profile_summary(agent)}"]
        lines.append(f"模型：{_TIER_LABELS.get(str(agent.get('model_tier')), '未知')}")
        lines.append(f"工具：{tool_names or '无'}")
        lines.append(f"职责：{_display(agent.get('instructions'))}")
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _message_text(value: Any) -> str:
    # Preserve complete multiline answers without permitting terminal escape sequences.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(value or "").replace("\r\n", "\n"))


def _team_turn_summary(turn: dict[str, Any]) -> str:
    labels = {
        "queued": "已受理，等待规划",
        "planning": "任务规划中",
        "working": "成员执行中",
        "summarizing": "汇总中",
        "delivery_pending": "结果待写回",
        "completed": "已完成",
        "cancelled": "已停止",
        "error": "执行未完成",
        "unknown": "结果待核实",
        "blocked": "执行受阻",
    }
    status = labels.get(str(turn.get("status")), "状态待核对")
    identity = _display(turn.get("id"))
    team = _display(turn.get("team_name") or turn.get("team_id"))
    version = _display(turn.get("team_version"))
    return f"{identity} · {team} · v{version} · {status}"


def _format_team_turn(turn: dict[str, Any]) -> str:
    lines = ["团队任务：" + _team_turn_summary(turn)]
    for key, label in (
        ("request_id", "原请求"),
        ("source_session_id", "来源会话"),
        ("team_session_id", "团队对话"),
        ("result_message_id", "结果消息"),
    ):
        if turn.get(key):
            lines.append(label + "：" + _display(turn[key]))
    if turn.get("cancel_requested") and turn.get("status") not in {
        "completed",
        "cancelled",
        "error",
        "unknown",
        "blocked",
    }:
        lines.append("停止请求已记录，等待执行者收口。")
    roster = turn.get("roster", {})
    for member in roster.get("agents", []) if isinstance(roster, dict) else []:
        if isinstance(member, dict):
            lines.append(
                f"授权成员：{_display(member.get('agent_id'))} · "
                f"v{_display(member.get('agent_version'))}"
            )
    phase_names = {"planning": "任务规划", "members": "成员分工", "summary": "结果汇总"}
    for phase in turn.get("phases", []):
        if not isinstance(phase, dict):
            continue
        collaboration = phase.get("collaboration", {})
        if not isinstance(collaboration, dict):
            continue
        name = phase_names.get(str(phase.get("phase")), "执行阶段")
        lines.append(
            f"{name}：{_display(phase.get('collaboration_id') or collaboration.get('id'))} · "
            f"{_collaboration_status(collaboration.get('status'))}"
        )
        for item in collaboration.get("steps", []):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"  {_display(item.get('id'))} · "
                f"{_display(item.get('agent_name') or item.get('agent_id'))} · "
                f"{_display(item.get('agent_id'))} · v{_display(item.get('agent_version'))} · "
                f"{_collaboration_status(item.get('status'))}"
            )
            for key, label in (("job_id", "Job"), ("run_id", "Run"), ("session_id", "执行会话")):
                if item.get(key):
                    lines.append(f"  {label}：{_display(item[key])}")
            for key, label in (("response", "阶段结果"), ("error", "阶段异常")):
                if item.get(key):
                    lines.append(label + "：\n" + _message_text(item[key]))
    for key, label in (("response", "完整结果"), ("error", "执行异常")):
        if turn.get(key):
            lines.append(label + "：\n" + _message_text(turn[key]))
    if turn.get("result_sha256"):
        lines.append("结果 SHA256：" + _display(turn["result_sha256"]))
    usage = turn.get("usage")
    if isinstance(usage, dict):
        lines.append(
            f"用量：{_display(usage.get('total_tokens', 0))} tokens · "
            f"{_display(usage.get('job_count', 0))} 个 Job · "
            f"{_display(usage.get('total_cost_usd', 0))} 美元"
        )
    return "\n".join(lines)


def _message_summary(message: dict[str, Any]) -> str:
    status = {
        "queued": "等待派送",
        "dispatching": "开始派送",
        "processing": "对方处理中",
        "responded": "已回答",
        "acknowledged": "已确认收到",
    }.get(str(message.get("state"))) or _collaboration_status(message.get("state"))
    return (
        f"{_display(message.get('id'))} · "
        f"{_display(message.get('sender_name') or message.get('sender_agent_id'))} → "
        f"{_display(message.get('recipient_name') or message.get('recipient_agent_id'))} · {status}"
    )


def _collaboration_status(value: Any) -> str:
    return {
        "waiting": "等待前置步骤",
        "admitted": "等待执行",
        "claimed": "准备执行",
        "running": "执行中",
        "completed": "已完成",
        "error": "未完成",
        "timeout": "已超时",
        "max_turns": "达到执行轮数",
        "cancelled": "已停止",
        "unknown": "结果不明",
        "blocked": "前置步骤未完成",
    }.get(str(value), "状态待核对")


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
            return "Agent 会话回执格式有误，当前会话保持不变，请重试。"
        return str(result.content)
    team_snapshot = (
        arguments["operation"] in {"team_send", "team_turn", "cancel_team_turn"}
        and isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
        and re.fullmatch(r"pt_[0-9a-f]{32}", payload["id"])
        and isinstance(payload.get("status"), str)
    )
    if isinstance(payload, dict) and payload.get("error") and not team_snapshot:
        return f"操作未完成：{_display(payload['error'])}"
    if arguments["operation"] == "open":
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id):
            return "Agent 会话回执缺少有效会话 ID，当前会话保持不变，请重试。"
        if not await engine.load_session(session_id):
            return "Agent 会话暂未就绪，请重试打开；Agent 档案仍然保留。"
        return f"已打开 Agent {_display(arguments['agent_id'])} 的会话 {session_id}。"
    return _format_result(arguments["operation"], payload)
