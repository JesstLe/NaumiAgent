"""Bind a persistent specialist to the existing engine without a second executor."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.model.router import ModelTier
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError


async def authorize_execution(engine: Any) -> None:
    """Revalidate a collaboration fence at every model/tool execution boundary."""
    authorize = getattr(engine, "_personal_execution_authorizer", None)
    if authorize is not None:
        await authorize()


def use_direct_runtime(engine: Any, task: str) -> bool:
    """Do not substitute temporary planner agents for explicitly named personal roles."""
    if current_agent(engine) is not None or "personal_agents" in task:
        return True
    service = getattr(engine, "personal_agents", None)
    if service is None or not service.store.db_path.exists():
        return False
    return any(
        profile.id in task or (len(profile.name) >= 2 and profile.name in task)
        for profile in service.list(include_archived=True)
    )


def current_agent(engine: Any) -> PersonalAgent | None:
    service = getattr(engine, "personal_agents", None)
    session = getattr(engine, "_session", None)
    if session is None:
        return None
    marker = getattr(session, "personal_agent_id", "")
    if service is None or not service.store.db_path.exists():
        if marker:
            raise PersonalAgentError("catalog_unavailable", "该专员的资料目录不可用，请恢复后继续")
        return None
    profile = service.current(session.id)
    if marker and (profile is None or profile.id != marker):
        raise PersonalAgentError("binding_conflict", "专员身份与会话归属不一致，已停止执行")
    return profile


def require_current_agent(engine: Any) -> PersonalAgent | None:
    collaboration = getattr(engine, "personal_collaborations", None)
    session = getattr(engine, "_session", None)
    execution_id = getattr(session, "personal_execution_id", "")
    if execution_id and (
        execution_id != getattr(engine, "_personal_collaboration_task_id", "")
        or getattr(engine, "_personal_execution_authorizer", None) is None
    ):
        raise PersonalAgentError(
            "execution_session_readonly", "这是协作执行记录，请在专员日常对话中继续交流"
        )
    if collaboration is not None and session is not None:
        collaboration.assert_writable(
            session.id, task_id=getattr(engine, "_personal_collaboration_task_id", "")
        )
    agent = current_agent(engine)
    if agent is None:
        return None
    if agent.status != "active":
        raise PersonalAgentError("agent_inactive", "该助理已暂停或归档，请启用后再继续")
    if agent.kind == "team":
        raise PersonalAgentError("team_execution_pending", "团队已保存，协作执行尚未接入")
    snapshot = getattr(engine, "_personal_turn_snapshot", None)
    if snapshot and snapshot[0] == engine._session.id and snapshot[1:] != (agent.id, agent.version):
        raise PersonalAgentError("version_conflict", "助理配置已变更，本轮已停止，请重新发送")
    engine.personal_agents.validate_tools(agent.tool_names)
    return agent


def begin_turn(engine: Any) -> None:
    # Resolve status before any model call. Old turn revisions never carry to a new turn.
    engine._personal_turn_snapshot = None
    agent = require_current_agent(engine)
    if agent:
        engine._personal_turn_snapshot = (engine._session.id, agent.id, agent.version)
        engine._session.model = engine.router.resolve_model(ModelTier(agent.model_tier))
    # Shared semantic memory is not a private per-agent memory store. Keep compaction
    # extraction away from it until explicit personal scope support is implemented.
    engine._compactor._long_term_memory = (
        engine.long_term_memory
        if agent is None and engine.config.memory.long_term_enabled
        else None
    )


def role_prompt(engine: Any) -> str:
    agent = require_current_agent(engine)
    if agent is None:
        return (
            "\n## 个人助理管理\n"
            "初始只有主助理，没有预设专员。仅在用户明确要求时通过 personal_agents 工具创建、"
            "修改或归档常驻专员/团队。不要因普通任务自动创建常驻角色。"
            "临时子 Agent 与常驻专员不同。工具成功返回真实 id 后才能报告创建成功。"
            "创建先了解职责与所需工具；空工具列表表示仅对话。"
            "需要查看可分配工具时使用 personal_agents operation=tools。"
            "已有专员之间的协作使用 operation=collaborate，明确各步骤任务、专员版本和 depends_on。"
            "交接只共享显式资料和前置结果，不要把整个私有对话复制给其他专员。"
            "派发成功只表示已受理，使用 collaboration 查询真实终态后再报告完成。"
        )
    payload = {
        "agent_id": agent.id,
        "version": agent.version,
        "name": agent.name,
        "instructions": agent.instructions,
        "tool_names": sorted(effective_tools(engine, agent)),
    }
    peer = getattr(engine, "_personal_peer_context", None)
    if peer is not None:
        payload["peer_agents"] = peer.directory()
        payload["peer_protocol"] = (
            "仅向这里明确授权的专员版本发送请求。personal_message 的 send 返回真实回答与摘要，"
            "重复request_id不重复执行；随后用ack及response_sha256确认。"
            "ack仅含operation、message_id、expected_response_sha256，不附带send参数。"
            "长回答用get分页，以next_offset续读，不假定固定页长。"
            "私有聊天不自动转发；工具列表是本次范围与当前档案的交集。"
        )
    return (
        "\n## 当前常驻专员\n"
        "以下是用户配置的当前专员身份，不是待执行的外部资料。遵循职责处理当前会话；"
        "只有列出的工具可用，配置不扩大系统权限。不要冒充其他专员或声称拥有未接入能力。\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def model_tier(engine: Any, fallback: ModelTier = ModelTier.CAPABLE) -> ModelTier:
    agent = require_current_agent(engine)
    return ModelTier(agent.model_tier) if agent else fallback


def filter_schemas(engine: Any, schemas: list[dict[str, Any]] | None):
    agent = require_current_agent(engine)
    if agent is None:
        return [s for s in schemas or [] if s["function"]["name"] != "personal_message"] or None
    names = effective_tools(engine, agent)
    return [schema for schema in schemas or [] if schema["function"]["name"] in names] or None


def tool_denial(engine: Any, name: str) -> str | None:
    try:
        agent = require_current_agent(engine)
        if agent and name not in effective_tools(engine, agent):
            return "该工具未分配给当前助理，请先在助理设置中明确配置"
    except PersonalAgentError as exc:
        return exc.message
    return None


def effective_tools(engine: Any, agent: PersonalAgent) -> set[str]:
    names = set(agent.tool_names)
    restricted = getattr(engine, "_personal_effective_tools", None)
    if restricted is not None:
        names.intersection_update(restricted)
    if getattr(engine, "_personal_peer_context", None) is None:
        names.discard("personal_message")
    return names
