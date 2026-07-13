"""Strict bounded value objects for the Agent Control Center."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

AGENT_CONTROL_SCHEMA_VERSION = 1
AGENT_CONTROL_SECTIONS = (
    "summary",
    "agents",
    "executions",
    "team_messages",
    "blackboard",
    "warnings",
)

_MAX_TEXT = 2000
_MAX_ITEMS = 100
_MAX_SMALL_ITEMS = 50
_MAX_WARNINGS = 20
_AGENT_KINDS = frozenset({"preset", "dynamic"})
_AGENT_STATES = frozenset({
    "uninitialized", "spawned", "ready", "running", "idle", "destroyed",
})
_EXECUTION_STATUSES = frozenset({
    "running", "stopping", "completed", "error", "failed", "timeout",
    "max_turns", "cancelled",
})
_EXECUTION_PHASES = frozenset({
    "starting", "running", "preparing_tool", "running_tool", "stopping", "finished",
})
_PRIORITIES = frozenset({"low", "normal", "high", "critical"})


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _only(data: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _text(value: Any, name: str, *, required: bool = False) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{name} must be a string")
    if required and not result:
        raise ValueError(f"{name} must not be blank")
    return result[:_MAX_TEXT]


def _choice(value: Any, name: str, allowed: frozenset[str]) -> str:
    result = _text(value, name, required=True)
    if result not in allowed:
        raise ValueError(f"invalid {name}: {result}")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _sequence(value: Any, name: str, limit: int = _MAX_ITEMS) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be an array")
    if len(value) > limit:
        raise ValueError(f"{name} must contain at most {limit} items")
    return tuple(value)


def _texts(value: Any, name: str, limit: int = _MAX_SMALL_ITEMS) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{name} item", required=True)
        for item in _sequence(value, name, limit)
    )


@dataclass(frozen=True, slots=True)
class AgentControlSummary:
    total_agents: int = 0
    active_agents: int = 0
    attention_agents: int = 0
    stoppable_executions: int = 0
    pending_messages: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> AgentControlSummary:
        data = _mapping(value, "summary")
        _only(data, {
            "total_agents", "active_agents", "attention_agents",
            "stoppable_executions", "pending_messages",
        }, "summary")
        return cls(
            total_agents=_integer(data.get("total_agents", 0), "summary.total_agents"),
            active_agents=_integer(data.get("active_agents", 0), "summary.active_agents"),
            attention_agents=_integer(
                data.get("attention_agents", 0), "summary.attention_agents"
            ),
            stoppable_executions=_integer(
                data.get("stoppable_executions", 0), "summary.stoppable_executions"
            ),
            pending_messages=_integer(
                data.get("pending_messages", 0), "summary.pending_messages"
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    name: str
    description: str
    kind: str
    state: str
    task_count: int = 0
    model_tier: str = ""
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permission_level: str = ""
    age_ms: int = 0
    heartbeat_age_ms: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> AgentDescriptor:
        data = _mapping(value, "agent")
        _only(data, {
            "name", "description", "kind", "state", "task_count", "model_tier",
            "capabilities", "tools", "permission_level", "age_ms", "heartbeat_age_ms",
        }, "agent")
        return cls(
            name=_text(data.get("name"), "agent.name", required=True),
            description=_text(data.get("description"), "agent.description"),
            kind=_choice(data.get("kind"), "agent.kind", _AGENT_KINDS),
            state=_choice(data.get("state"), "agent.state", _AGENT_STATES),
            task_count=_integer(data.get("task_count", 0), "agent.task_count"),
            model_tier=_text(data.get("model_tier"), "agent.model_tier"),
            capabilities=_texts(data.get("capabilities"), "agent.capabilities"),
            tools=_texts(data.get("tools"), "agent.tools"),
            permission_level=_text(data.get("permission_level"), "agent.permission_level"),
            age_ms=_integer(data.get("age_ms", 0), "agent.age_ms"),
            heartbeat_age_ms=_integer(
                data.get("heartbeat_age_ms", 0), "agent.heartbeat_age_ms"
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionDescriptor:
    task_id: str
    session_id: str
    agent_name: str
    description: str
    status: str
    phase: str
    started_at: float
    finished_at: float | None = None
    elapsed_ms: int = 0
    heartbeat_age_ms: int = 0
    current_tool: str = ""
    recent_tools: tuple[str, ...] = ()
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    stop_supported: bool = False
    stop_requested: bool = False

    @classmethod
    def from_dict(cls, value: Any) -> ExecutionDescriptor:
        data = _mapping(value, "execution")
        _only(data, {
            "task_id", "session_id", "agent_name", "description", "status", "phase",
            "started_at", "finished_at", "elapsed_ms", "heartbeat_age_ms", "current_tool",
            "recent_tools", "total_tokens", "total_cost_usd", "turns", "error",
            "stop_supported", "stop_requested",
        }, "execution")
        finished = data.get("finished_at")
        return cls(
            task_id=_text(data.get("task_id"), "execution.task_id", required=True),
            session_id=_text(data.get("session_id"), "execution.session_id"),
            agent_name=_text(data.get("agent_name"), "execution.agent_name", required=True),
            description=_text(data.get("description"), "execution.description"),
            status=_choice(data.get("status"), "execution.status", _EXECUTION_STATUSES),
            phase=_choice(data.get("phase"), "execution.phase", _EXECUTION_PHASES),
            started_at=_number(data.get("started_at", 0), "execution.started_at"),
            finished_at=(
                None if finished is None else _number(finished, "execution.finished_at")
            ),
            elapsed_ms=_integer(data.get("elapsed_ms", 0), "execution.elapsed_ms"),
            heartbeat_age_ms=_integer(
                data.get("heartbeat_age_ms", 0), "execution.heartbeat_age_ms"
            ),
            current_tool=_text(data.get("current_tool"), "execution.current_tool"),
            recent_tools=_texts(data.get("recent_tools"), "execution.recent_tools", 20),
            total_tokens=_integer(data.get("total_tokens", 0), "execution.total_tokens"),
            total_cost_usd=_number(
                data.get("total_cost_usd", 0), "execution.total_cost_usd"
            ),
            turns=_integer(data.get("turns", 0), "execution.turns"),
            error=_text(data.get("error"), "execution.error"),
            stop_supported=_boolean(
                data.get("stop_supported", False), "execution.stop_supported"
            ),
            stop_requested=_boolean(
                data.get("stop_requested", False), "execution.stop_requested"
            ),
        )


@dataclass(frozen=True, slots=True)
class TeamMessageDescriptor:
    sender: str
    recipient: str
    topic: str
    priority: str
    timestamp: float
    content: str

    @classmethod
    def from_dict(cls, value: Any) -> TeamMessageDescriptor:
        data = _mapping(value, "team_message")
        _only(
            data,
            {"sender", "recipient", "topic", "priority", "timestamp", "content"},
            "team_message",
        )
        return cls(
            sender=_text(data.get("sender"), "team_message.sender", required=True),
            recipient=_text(data.get("recipient"), "team_message.recipient"),
            topic=_text(data.get("topic"), "team_message.topic", required=True),
            priority=_choice(data.get("priority"), "team_message.priority", _PRIORITIES),
            timestamp=_number(data.get("timestamp", 0), "team_message.timestamp"),
            content=_text(data.get("content"), "team_message.content"),
        )


@dataclass(frozen=True, slots=True)
class BlackboardDescriptor:
    key: str
    author: str
    version: int
    timestamp: float
    value_summary: str

    @classmethod
    def from_dict(cls, value: Any) -> BlackboardDescriptor:
        data = _mapping(value, "blackboard")
        _only(data, {"key", "author", "version", "timestamp", "value_summary"}, "blackboard")
        return cls(
            key=_text(data.get("key"), "blackboard.key", required=True),
            author=_text(data.get("author"), "blackboard.author", required=True),
            version=_integer(data.get("version", 0), "blackboard.version"),
            timestamp=_number(data.get("timestamp", 0), "blackboard.timestamp"),
            value_summary=_text(data.get("value_summary"), "blackboard.value_summary"),
        )


@dataclass(frozen=True, slots=True)
class AgentControlSnapshot:
    schema_version: int
    session_id: str
    revision: int
    generated_at: str
    summary: AgentControlSummary = field(default_factory=AgentControlSummary)
    agents: tuple[AgentDescriptor, ...] = ()
    executions: tuple[ExecutionDescriptor, ...] = ()
    team_messages: tuple[TeamMessageDescriptor, ...] = ()
    blackboard: tuple[BlackboardDescriptor, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def empty(cls, *, session_id: str = "") -> AgentControlSnapshot:
        return cls(AGENT_CONTROL_SCHEMA_VERSION, session_id[:_MAX_TEXT], 0, "")

    def with_revision(self, revision: int, generated_at: str) -> AgentControlSnapshot:
        return replace(self, revision=revision, generated_at=generated_at[:_MAX_TEXT])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> AgentControlSnapshot:
        data = _mapping(value, "agent_control")
        _only(data, {
            "schema_version", "session_id", "revision", "generated_at", "summary",
            "agents", "executions", "team_messages", "blackboard", "warnings",
        }, "agent_control")
        if data.get("schema_version") != AGENT_CONTROL_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {data.get('schema_version')!r}; "
                f"expected {AGENT_CONTROL_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=AGENT_CONTROL_SCHEMA_VERSION,
            session_id=_text(data.get("session_id"), "session_id"),
            revision=_integer(data.get("revision", 0), "revision"),
            generated_at=_text(data.get("generated_at"), "generated_at"),
            summary=AgentControlSummary.from_dict(data.get("summary", {})),
            agents=tuple(
                AgentDescriptor.from_dict(item)
                for item in _sequence(data.get("agents"), "agents")
            ),
            executions=tuple(
                ExecutionDescriptor.from_dict(item)
                for item in _sequence(data.get("executions"), "executions")
            ),
            team_messages=tuple(
                TeamMessageDescriptor.from_dict(item)
                for item in _sequence(data.get("team_messages"), "team_messages")
            ),
            blackboard=tuple(
                BlackboardDescriptor.from_dict(item)
                for item in _sequence(data.get("blackboard"), "blackboard")
            ),
            warnings=_texts(data.get("warnings"), "warnings", _MAX_WARNINGS),
        )


__all__ = [
    "AGENT_CONTROL_SCHEMA_VERSION",
    "AGENT_CONTROL_SECTIONS",
    "AgentControlSnapshot",
    "AgentControlSummary",
    "AgentDescriptor",
    "BlackboardDescriptor",
    "ExecutionDescriptor",
    "TeamMessageDescriptor",
]
