"""Authoritative Agent Control Center snapshot assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from naumi_agent.agent_control.models import (
    AGENT_CONTROL_SCHEMA_VERSION,
    AGENT_CONTROL_SECTIONS,
    AgentControlSnapshot,
    AgentControlSummary,
    AgentDescriptor,
    BlackboardDescriptor,
    ExecutionDescriptor,
    TeamMessageDescriptor,
)
from naumi_agent.agents.presets import ALL_AGENT_CONFIGS
from naumi_agent.safety.guardrails import OutputGuardrail

_ATTENTION_EXECUTION_STATES = frozenset({"error", "failed", "timeout", "max_turns"})
_ACTIVE_AGENT_STATES = frozenset({"spawned", "ready", "running"})


class AgentControlService:
    """Build monotonic session-scoped snapshots from engine-owned state."""

    def __init__(
        self,
        engine: Any,
        *,
        session_id_getter: Callable[[], str] | None = None,
    ) -> None:
        self._engine = engine
        self._session_id_getter = session_id_getter or self._engine_session_id
        self._lock = asyncio.Lock()
        self._fingerprint = ""
        self._revision = 0
        self._bound_session_id: str | None = None
        self._session_cutoff = 0.0

    async def snapshot(self) -> AgentControlSnapshot:
        async with self._lock:
            session_id = str(self._session_id_getter() or "")[:2000]
            self._bind_session(session_id)
            snapshot = await self._build_snapshot(session_id)
            fingerprint = _fingerprint(snapshot)
            if fingerprint != self._fingerprint:
                self._revision += 1
                self._fingerprint = fingerprint
            return snapshot.with_revision(self._revision, _now_iso())

    @staticmethod
    def changed_sections(
        previous: AgentControlSnapshot,
        current: AgentControlSnapshot,
    ) -> tuple[str, ...]:
        return tuple(
            section
            for section in AGENT_CONTROL_SECTIONS
            if _section_value(previous, section) != _section_value(current, section)
        )

    def _bind_session(self, session_id: str) -> None:
        if self._bound_session_id is None:
            self._bound_session_id = session_id
            return
        if session_id != self._bound_session_id:
            self._bound_session_id = session_id
            self._session_cutoff = time.time()
            self._fingerprint = ""

    async def _build_snapshot(self, session_id: str) -> AgentControlSnapshot:
        warnings: list[str] = []
        agents: tuple[AgentDescriptor, ...] = ()
        executions: tuple[ExecutionDescriptor, ...] = ()
        team_messages: tuple[TeamMessageDescriptor, ...] = ()
        blackboard: tuple[BlackboardDescriptor, ...] = ()
        pending_messages = 0
        manager = getattr(self._engine, "subagent_manager", None)

        try:
            if manager is not None:
                agents = self._agents(manager)
                executions = tuple(
                    ExecutionDescriptor(**asdict(record))
                    for record in manager.list_executions(limit=100)
                    if record.session_id == session_id
                )
        except Exception as exc:
            warnings.append(f"Agent 数据读取失败：{type(exc).__name__}: {exc}")

        try:
            if manager is not None:
                bus = manager.message_bus
                history = bus.get_history(limit=100)
                team_messages = tuple(
                    TeamMessageDescriptor(
                        sender=_public(message.sender),
                        recipient=_public(message.recipient or ""),
                        topic=_public(message.topic),
                        priority=str(message.priority),
                        timestamp=max(0.0, float(message.timestamp)),
                        content=_public(message.content),
                    )
                    for message in history
                    if self._message_belongs_to_session(message, session_id)
                )
                board = await bus.blackboard_get_all()
                blackboard = tuple(
                    BlackboardDescriptor(
                        key=_public(key),
                        author=_public(entry.author),
                        version=max(0, int(entry.version)),
                        timestamp=max(0.0, float(entry.timestamp)),
                        value_summary=_value_summary(entry.value),
                    )
                    for key, entry in sorted(board.items())[:100]
                    if float(entry.timestamp) >= self._session_cutoff
                )
                for config in manager.list_agent_configs()[:100]:
                    pending_messages += sum(
                        self._message_belongs_to_session(message, session_id)
                        for message in await bus.peek(config.name, limit=100)
                    )
        except Exception as exc:
            warnings.append(f"团队数据读取失败：{type(exc).__name__}: {exc}")

        active_agents = sum(item.state in _ACTIVE_AGENT_STATES for item in agents)
        attention_agents = len({
            item.agent_name
            for item in executions
            if item.status in _ATTENTION_EXECUTION_STATES
        })
        summary = AgentControlSummary(
            total_agents=len(agents),
            active_agents=active_agents,
            attention_agents=attention_agents,
            stoppable_executions=sum(item.stop_supported for item in executions),
            pending_messages=pending_messages,
        )
        return AgentControlSnapshot(
            schema_version=AGENT_CONTROL_SCHEMA_VERSION,
            session_id=session_id,
            revision=0,
            generated_at="",
            summary=summary,
            agents=agents,
            executions=executions,
            team_messages=team_messages,
            blackboard=blackboard,
            warnings=tuple(dict.fromkeys(warnings))[:20],
        )

    def _agents(self, manager: Any) -> tuple[AgentDescriptor, ...]:
        raw_agents = manager.list_agents()
        by_name = {str(item.get("name") or ""): item for item in raw_agents}
        preset_names = set(ALL_AGENT_CONFIGS)
        now = time.monotonic()
        descriptors: list[AgentDescriptor] = []
        for config in manager.list_agent_configs()[:100]:
            raw = by_name.get(config.name, {})
            lifecycle = manager.get_lifecycle(config.name)
            descriptors.append(AgentDescriptor(
                name=_public(config.name),
                description=_public(config.description),
                kind="preset" if config.name in preset_names else "dynamic",
                state=str(raw.get("state") or "uninitialized"),
                task_count=_nonnegative_int(raw.get("tasks")),
                model_tier=_public(config.model_tier),
                capabilities=tuple(str(item) for item in config.capabilities[:50]),
                tools=tuple(
                    _public(item)
                    for item in manager.agent_tool_names(config.name)[:50]
                ),
                permission_level=_public(config.permission_level),
                age_ms=(
                    max(0, round((now - lifecycle.spawned_at) * 1000))
                    if lifecycle is not None
                    else 0
                ),
                heartbeat_age_ms=(
                    max(0, round((now - lifecycle.last_updated) * 1000))
                    if lifecycle is not None
                    else 0
                ),
            ))
        return tuple(descriptors)

    def _message_belongs_to_session(self, message: Any, session_id: str) -> bool:
        if float(getattr(message, "timestamp", 0.0)) < self._session_cutoff:
            return False
        metadata = getattr(message, "metadata", {})
        message_session = str(
            metadata.get("session_id", "") if isinstance(metadata, dict) else ""
        )
        return not message_session or message_session == session_id

    def _engine_session_id(self) -> str:
        return str(getattr(getattr(self._engine, "_session", None), "id", "") or "")


def _fingerprint(snapshot: AgentControlSnapshot) -> str:
    comparable = snapshot.to_dict()
    comparable["revision"] = 0
    comparable["generated_at"] = ""
    for agent in comparable["agents"]:
        agent["age_ms"] = 0
        agent["heartbeat_age_ms"] = 0
    for execution in comparable["executions"]:
        execution["elapsed_ms"] = 0
        execution["heartbeat_age_ms"] = 0
    encoded = json.dumps(
        comparable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _section_value(snapshot: AgentControlSnapshot, section: str) -> Any:
    value = snapshot.to_dict()[section]
    if section == "agents":
        for item in value:
            item["age_ms"] = 0
            item["heartbeat_age_ms"] = 0
    elif section == "executions":
        for item in value:
            item["elapsed_ms"] = 0
            item["heartbeat_age_ms"] = 0
    return value


def _value_summary(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return _public(text)


def _public(value: Any) -> str:
    return OutputGuardrail.redact(str(value or "")).strip()[:2000]


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["AgentControlService"]
