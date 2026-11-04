"""Strict request, roster, plan and remaining-budget contracts for personal teams."""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from naumi_agent.personal.models import PersonalAgent, PersonalAgentError
from naumi_agent.personal.peer_models import agent_id, dispatch, grants, text, tool_names, version

_MAX_TEAMS = 128
_MAX_AGENTS = 128
_MAX_DEPTH = 8
_STEP_FIELDS = frozenset({"id", "agent_id", "agent_version", "task", "depends_on"})
_POLICY_BUDGET_FIELDS = frozenset({"max_budget_usd", "max_input_tokens", "max_output_tokens"})
_SPENT_FIELDS = frozenset({"total_cost_usd", "input_tokens", "output_tokens"})


def _invalid(message: str) -> PersonalAgentError:
    return PersonalAgentError("invalid_input", message)


def normalize_client_payload(raw: Any) -> dict[str, Any]:
    """Normalize only caller-owned input; computed execution identities stay outside."""
    required = {"team_id", "team_version", "request_id", "content"}
    optional = {"context", "source_ids", "member_grants"}
    if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
        raise _invalid("团队消息缺少必填字段或包含不支持的字段")
    request = dispatch(
        {
            "recipient_agent_id": raw["team_id"],
            "recipient_agent_version": raw["team_version"],
            "request_id": raw["request_id"],
            "content": raw["content"],
            "context": raw.get("context", ""),
        }
    )
    source_ids = raw.get("source_ids", [])
    if not isinstance(source_ids, list) or len(source_ids) > 32:
        raise _invalid("团队消息最多附带 32 项资料编号")
    sources = []
    for value in source_ids:
        value = text(value, "资料编号", 128)
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise _invalid("资料编号包含空白或控制字符")
        if value in sources:
            raise _invalid("团队消息包含重复资料编号")
        sources.append(value)
    result = {
        "team_id": request["recipient_agent_id"],
        "team_version": request["recipient_agent_version"],
        "request_id": request["request_id"],
        "content": request["content"],
        "context": request["context"],
        "source_ids": sorted(sources),
    }
    if raw.get("member_grants") is not None:
        # Missing/None means default read-only roster; [] explicitly grants nobody.
        result["member_grants"] = grants(raw["member_grants"])
    return result


def _catalog_available(personal: Any) -> None:
    store = getattr(personal, "store", None)
    path = getattr(store, "db_path", None)
    if path is not None and not path.is_file():
        raise PersonalAgentError("catalog_unavailable", "团队资料目录不可用，请恢复后继续")


def _profile(personal: Any, identity: str, *, require_active: bool = True):
    _catalog_available(personal)
    result = personal.get(identity)
    if result is None:
        raise PersonalAgentError("not_found", "团队或成员资料缺失，请刷新目录")
    if agent_id(result.id) != identity:
        raise PersonalAgentError("binding_conflict", "团队或成员资料归属不一致")
    version(result.version)
    if result.kind not in {"team", "specialist"}:
        raise PersonalAgentError("binding_conflict", "团队或成员类型资料不正确")
    if require_active and result.status != "active":
        raise PersonalAgentError("agent_inactive", "团队或成员已停用，请刷新目录后继续")
    return result


def _member_ids(value: Any) -> list[str]:
    if not isinstance(value, (tuple, list)) or len(value) > _MAX_AGENTS:
        raise _invalid("团队成员列表必须是最多 128 项的列表")
    selected = [agent_id(item) for item in value]
    if len(selected) != len(set(selected)):
        raise _invalid("团队成员列表包含重复编号")
    return sorted(selected)


def _graph_depth(teams: dict[str, dict], root: str) -> None:
    """Check all paths, including a shared team first seen through a shorter path."""
    heights: dict[str, int] = {}
    visiting: set[str] = set()

    def height(identity: str) -> int:
        if identity not in teams:
            return 0
        if identity in visiting:
            raise PersonalAgentError("member_cycle", "团队成员形成循环，请调整成员关系")
        if identity in heights:
            return heights[identity]
        visiting.add(identity)
        result = max((1 + height(item) for item in teams[identity]["member_ids"]), default=0)
        visiting.remove(identity)
        heights[identity] = result
        return result

    if height(root) > _MAX_DEPTH:
        raise PersonalAgentError("team_depth", "团队嵌套超过 8 层，请调整成员关系")


def capture_roster(
    personal: Any, team: Any, registry: Any, member_grants: Any = None
) -> dict[str, list[dict]]:
    """Freeze all team ancestors and the exact authorized leaf versions and tools."""
    if not isinstance(team, PersonalAgent):
        raise _invalid("团队执行入口需要完整的团队档案")
    identity = agent_id(team.id)
    selected_version = version(team.version)
    current = _profile(personal, identity)
    if team.kind != "team" or current.kind != "team":
        raise _invalid("团队执行入口需要选择团队")
    if current.version != selected_version:
        raise PersonalAgentError("version_conflict", "团队配置已更新，请刷新后重新发送")
    if _member_ids(team.member_ids) != _member_ids(current.member_ids):
        raise PersonalAgentError("binding_conflict", "团队成员与当前职责版本不一致")
    teams: dict[str, dict] = {}
    leaves: dict[str, Any] = {}
    visiting: set[str] = set()

    def visit(profile: Any, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise PersonalAgentError("team_depth", "团队嵌套超过 8 层，请调整成员关系")
        if profile.id in visiting:
            raise PersonalAgentError("member_cycle", "团队成员形成循环，请调整成员关系")
        if profile.kind == "specialist":
            if profile.member_ids:
                raise PersonalAgentError("binding_conflict", "专员资料包含不支持的嵌套成员")
            leaves[profile.id] = profile
            if len(leaves) > _MAX_AGENTS:
                raise PersonalAgentError("team_limit", "团队展开后超过 128 位专员，请调整成员范围")
            return
        if profile.status != "active":
            raise PersonalAgentError("agent_inactive", "嵌套团队已停用，请刷新目录后继续")
        if profile.id in teams:
            return
        teams[profile.id] = {
            "agent_id": profile.id,
            "agent_version": profile.version,
            "member_ids": _member_ids(profile.member_ids),
        }
        if len(teams) > _MAX_TEAMS:
            raise PersonalAgentError("team_limit", "团队展开后超过 128 个团队，请调整成员范围")
        visiting.add(profile.id)
        for member_id in teams[profile.id]["member_ids"]:
            # Teams always remain active; leaf status belongs to the exact grant.
            visit(_profile(personal, member_id, require_active=False), depth + 1)
        visiting.remove(profile.id)

    visit(current, 0)
    _graph_depth(teams, identity)
    selected_grants = (
        [{"agent_id": key, "agent_version": item.version} for key, item in sorted(leaves.items())]
        if member_grants is None
        else grants(member_grants)
    )
    agents = []
    for grant in selected_grants:
        profile = leaves.get(grant["agent_id"])
        if profile is None:
            raise PersonalAgentError("team_scope_denied", "授权对象不是本团队中的具体专员")
        if profile.status != "active":
            raise PersonalAgentError("agent_inactive", "本次授权成员已停用，请调整成员范围后继续")
        if profile.version != grant["agent_version"]:
            raise PersonalAgentError("version_conflict", "成员配置已更新，请刷新授权范围")
        if "tool_names" in grant:
            selected_tools = grant["tool_names"]
        else:
            selected_tools = sorted(
                name
                for name in profile.tool_names
                if (tool := registry.get(name)) is not None
                and (
                    getattr(tool.metadata, "read_only", False) is True or name == "personal_message"
                )
            )
        if "personal_team" in selected_tools or not set(selected_tools) <= set(profile.tool_names):
            raise PersonalAgentError("team_scope_denied", "成员授权包含未分配的工具或团队协调权限")
        personal.validate_tools(selected_tools)
        agents.append(
            {
                "agent_id": profile.id,
                "agent_version": profile.version,
                "tool_names": list(selected_tools),
            }
        )
    result = {"teams": [teams[key] for key in sorted(teams)], "agents": agents}
    # A second catalog read detects configuration changes made while flattening.
    validate_current(personal, result)
    return result


def _validated_roster(value: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    if not isinstance(value, dict) or set(value) != {"teams", "agents"}:
        raise _invalid("团队成员快照格式不正确")
    if not isinstance(value["teams"], list) or not 1 <= len(value["teams"]) <= _MAX_TEAMS:
        raise _invalid("团队快照需要包含 1 至 128 个团队")
    if not isinstance(value["agents"], list) or len(value["agents"]) > _MAX_AGENTS:
        raise _invalid("团队快照最多包含 128 位专员")
    teams, agents = {}, {}
    for raw in value["teams"]:
        if not isinstance(raw, dict) or set(raw) != {"agent_id", "agent_version", "member_ids"}:
            raise _invalid("团队版本快照字段不完整或包含无关字段")
        identity = agent_id(raw["agent_id"])
        if identity in teams:
            raise _invalid("团队快照包含重复团队")
        teams[identity] = {
            "agent_id": identity,
            "agent_version": version(raw["agent_version"]),
            "member_ids": _member_ids(raw["member_ids"]),
        }
    for raw in value["agents"]:
        if not isinstance(raw, dict) or set(raw) != {"agent_id", "agent_version", "tool_names"}:
            raise _invalid("成员授权快照字段不完整或包含无关字段")
        identity = agent_id(raw["agent_id"])
        if identity in agents or identity in teams:
            raise _invalid("团队快照包含重复或冲突的成员身份")
        tools = tool_names(raw["tool_names"])
        if "personal_team" in tools:
            raise PersonalAgentError("team_scope_denied", "具体专员不获得团队协调权限")
        agents[identity] = {
            "agent_id": identity,
            "agent_version": version(raw["agent_version"]),
            "tool_names": tools,
        }
    referenced = {item for team in teams.values() for item in team["member_ids"]}
    roots = set(teams) - referenced
    if len(roots) != 1:
        raise _invalid("团队成员快照缺少唯一的根团队")
    root = next(iter(roots))
    _graph_depth(teams, root)
    reachable: set[str] = set()
    pending = [root]
    while pending:
        identity = pending.pop()
        if identity in reachable:
            continue
        reachable.add(identity)
        if identity in teams:
            pending.extend(teams[identity]["member_ids"])
    if set(teams) - reachable or set(agents) - reachable:
        raise PersonalAgentError("team_scope_denied", "成员授权不属于根团队的成员关系")
    return teams, agents


def validate_current(personal: Any, roster: Any) -> None:
    """Revalidate every ancestor and authorized leaf before a model/tool boundary."""
    teams, agents = _validated_roster(roster)
    member_profiles: dict[str, Any] = {}
    for entry in teams.values():
        current = _profile(personal, entry["agent_id"])
        if current.kind != "team":
            raise PersonalAgentError("binding_conflict", "团队成员快照的身份类型已改变")
        if (
            current.version != entry["agent_version"]
            or _member_ids(current.member_ids) != entry["member_ids"]
        ):
            raise PersonalAgentError("version_conflict", "团队或嵌套团队已更新，已停止旧版本执行")
        member_profiles[current.id] = current
        # Missing entries may be ungranted leaves, never omitted team ancestors.
        # Resolve the actual catalog kind rather than guessing from a partial grant.
        for member_id in entry["member_ids"]:
            member = member_profiles.get(member_id)
            if member is None:
                member = _profile(personal, member_id, require_active=False)
                member_profiles[member_id] = member
            if member.kind == "team":
                nested = teams.get(member_id)
                if nested is None:
                    raise PersonalAgentError("team_scope_denied", "嵌套团队缺少确切版本快照")
                if member.status != "active":
                    raise PersonalAgentError("agent_inactive", "嵌套团队已停用，已停止旧版本执行")
                if member.version != nested["agent_version"]:
                    raise PersonalAgentError("version_conflict", "嵌套团队已更新，已停止旧版本执行")
            elif member_id in teams or member.member_ids:
                raise PersonalAgentError("binding_conflict", "团队成员快照的身份类型已改变")
    for entry in agents.values():
        current = _profile(personal, entry["agent_id"])
        if current.kind != "specialist" or current.member_ids:
            raise PersonalAgentError("binding_conflict", "成员快照不属于具体专员")
        if current.version != entry["agent_version"]:
            raise PersonalAgentError("version_conflict", "成员配置已更新，已停止旧版本执行")
        if not set(entry["tool_names"]) <= set(current.tool_names):
            raise PersonalAgentError("team_scope_denied", "成员工具范围已失效，已停止执行")
        personal.validate_tools(entry["tool_names"])


def normalize_plan(value: Any, roster: Any) -> list[dict]:
    """Use the existing DAG validator, then bind every step to an authorized leaf."""
    _, agents = _validated_roster(roster)
    if not isinstance(value, list) or any(
        not isinstance(step, dict) or set(step) != _STEP_FIELDS for step in value
    ):
        raise _invalid("团队计划每一步仅接受编号、成员版本、任务与前置依赖")
    # Import at the call boundary: collaboration storage may use these contracts.
    from naumi_agent.personal.collaboration_store import _steps

    result = _steps(value)
    for step in result:
        entry = agents.get(step["agent_id"])
        if entry is None:
            raise PersonalAgentError("team_scope_denied", "计划只能分配给本次已授权的团队专员")
        if entry["agent_version"] != step["agent_version"]:
            raise PersonalAgentError("version_conflict", "计划成员版本与本次授权不一致")
    return result


def _counter(value: Any, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{label}必须是非负整数")
    return value


def _amount(value: Any, label: str, *, optional: bool = False) -> Decimal | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{label}必须是非负有限数字")
    try:
        if not math.isfinite(value) or value < 0:
            raise _invalid(f"{label}必须是非负有限数字")
        return Decimal(str(value))
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _invalid(f"{label}必须是非负有限数字") from exc


def phase_budget(policy: Any, spent: Any, slots: int = 1) -> dict[str, float | int | None]:
    """Allocate only unspent budget; reserve another slot for a later summary if asked."""
    if (
        not isinstance(policy, dict)
        or not _POLICY_BUDGET_FIELDS <= set(policy)
        or set(policy) - _POLICY_BUDGET_FIELDS - {"max_turns", "permission_mode"}
    ):
        raise _invalid("团队预算策略缺少限额字段或包含不支持的字段")
    if not isinstance(spent, dict) or set(spent) != _SPENT_FIELDS:
        raise _invalid("团队已用预算需要确切费用及输入、输出用量")
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
        raise _invalid("预算分配份数必须是正整数")
    if "max_turns" in policy and (
        isinstance(policy["max_turns"], bool)
        or not isinstance(policy["max_turns"], int)
        or not 1 <= policy["max_turns"] <= 1000
    ):
        raise _invalid("团队每次执行轮数必须为 1 至 1000 的整数")
    if "permission_mode" in policy and (
        not isinstance(policy["permission_mode"], str)
        or policy["permission_mode"]
        not in {"bypass", "permissive", "moderate", "strict", "lockdown"}
    ):
        raise _invalid("团队权限策略不正确")
    limit = _amount(policy["max_budget_usd"], "团队费用限额", optional=True)
    used = _amount(spent["total_cost_usd"], "团队已用费用")
    token_limits = {
        name: _counter(policy[name], "团队 token 限额", optional=True)
        for name in ("max_input_tokens", "max_output_tokens")
    }
    token_used = {
        name: _counter(spent[name], "团队已用 token") for name in ("input_tokens", "output_tokens")
    }
    cost = None
    if limit is not None:
        # The worker request's cost field supports at most 10**15 microusd.
        if limit > Decimal("1000000000"):
            raise _invalid("团队费用限额超过执行合同上限")
        with localcontext() as context:
            context.prec = 400
            available = max(Decimal(0), limit - used)
            micros = int((available * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
            cost = float(Decimal(micros // slots) / 1_000_000)
    return {
        "max_budget_usd": cost,
        **{
            name: None if maximum is None else max(0, maximum - token_used[name[4:]]) // slots
            for name, maximum in token_limits.items()
        },
    }
