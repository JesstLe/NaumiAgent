"""Bind a persistent specialist to the existing engine without a second executor."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.model.router import ModelTier
from naumi_agent.personal.models import PersonalAgent, PersonalAgentError


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
        )
    payload = {
        "agent_id": agent.id,
        "version": agent.version,
        "name": agent.name,
        "instructions": agent.instructions,
        "tool_names": list(agent.tool_names),
    }
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
        return schemas
    names = set(agent.tool_names)
    return [schema for schema in schemas or [] if schema["function"]["name"] in names] or None


def tool_denial(engine: Any, name: str) -> str | None:
    try:
        agent = require_current_agent(engine)
        if agent and name not in agent.tool_names:
            return "该工具未分配给当前助理，请先在助理设置中明确配置"
    except PersonalAgentError as exc:
        return exc.message
    return None
