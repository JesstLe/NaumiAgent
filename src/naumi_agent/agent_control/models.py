"""Strict bounded value objects for the Agent Control Center."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

AGENT_CONTROL_SCHEMA_VERSION = 7
AGENT_CONTROL_SECTIONS = (
    "summary",
    "agents",
    "executions",
    "results",
    "recovery_catalog",
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
    "starting", "waiting_capacity", "running", "preparing_tool",
    "running_tool", "stopping", "finished",
})
_WORKER_BACKENDS = frozenset({"embedded", "independent"})
_HEARTBEAT_PHASES = frozenset({
    "starting", "running", "waiting", "draining", "stopped", "failed",
})
_WORKER_JOB_STATES = frozenset({
    "admitted", "claimed", "running", "completed", "error", "timeout",
    "max_turns", "cancelled", "unknown",
})
_RESULT_STATUSES = frozenset({
    "completed", "error", "timeout", "max_turns", "cancelled",
})
_RECOVERY_KINDS = frozenset({"job", "publication"})
_RECOVERY_STATES = frozenset({
    "claim_active",
    "worker_active",
    "reclaimable_prestart",
    "recovery_required",
    "outcome_unknown",
    "publication_pending",
    "publication_claim_expired",
    "publication_quarantined",
})
_SESSION_SCOPES = frozenset({"current", "other", "unknown"})
_PRIORITIES = frozenset({"low", "normal", "high", "critical"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _sha256(value: Any, name: str, *, optional: bool = True) -> str:
    result = _text(value, name)
    if not result and optional:
        return ""
    if not _SHA256_RE.fullmatch(result):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return result


def _timestamp(value: Any, name: str, *, optional: bool = False) -> str:
    result = _text(value, name, required=not optional)
    if not result and optional:
        return ""
    try:
        parsed = datetime.fromisoformat(result)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result


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
    durable_capacity_configured: bool = False
    durable_active_jobs: int = 0
    durable_max_active_jobs: int = 0
    durable_waiting_jobs: int = 0
    durable_max_waiters: int = 0
    durable_reclaimable_jobs: int = 0
    durable_recovery_required_jobs: int = 0
    durable_results_visible: int = 0
    durable_unread_results: int = 0
    durable_publications_pending: int = 0
    durable_publications_claimed: int = 0
    durable_publications_expired: int = 0
    durable_publications_quarantined: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> AgentControlSummary:
        data = _mapping(value, "summary")
        _only(data, {
            "total_agents", "active_agents", "attention_agents",
            "stoppable_executions", "pending_messages",
            "durable_capacity_configured", "durable_active_jobs",
            "durable_max_active_jobs", "durable_waiting_jobs",
            "durable_max_waiters", "durable_reclaimable_jobs",
            "durable_recovery_required_jobs",
            "durable_results_visible", "durable_unread_results",
            "durable_publications_pending",
            "durable_publications_claimed", "durable_publications_expired",
            "durable_publications_quarantined",
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
            durable_capacity_configured=_boolean(
                data.get("durable_capacity_configured", False),
                "summary.durable_capacity_configured",
            ),
            durable_active_jobs=_integer(
                data.get("durable_active_jobs", 0),
                "summary.durable_active_jobs",
            ),
            durable_max_active_jobs=_integer(
                data.get("durable_max_active_jobs", 0),
                "summary.durable_max_active_jobs",
            ),
            durable_waiting_jobs=_integer(
                data.get("durable_waiting_jobs", 0),
                "summary.durable_waiting_jobs",
            ),
            durable_max_waiters=_integer(
                data.get("durable_max_waiters", 0),
                "summary.durable_max_waiters",
            ),
            durable_reclaimable_jobs=_integer(
                data.get("durable_reclaimable_jobs", 0),
                "summary.durable_reclaimable_jobs",
            ),
            durable_recovery_required_jobs=_integer(
                data.get("durable_recovery_required_jobs", 0),
                "summary.durable_recovery_required_jobs",
            ),
            durable_results_visible=_integer(
                data.get("durable_results_visible", 0),
                "summary.durable_results_visible",
            ),
            durable_unread_results=_integer(
                data.get("durable_unread_results", 0),
                "summary.durable_unread_results",
            ),
            durable_publications_pending=_integer(
                data.get("durable_publications_pending", 0),
                "summary.durable_publications_pending",
            ),
            durable_publications_claimed=_integer(
                data.get("durable_publications_claimed", 0),
                "summary.durable_publications_claimed",
            ),
            durable_publications_expired=_integer(
                data.get("durable_publications_expired", 0),
                "summary.durable_publications_expired",
            ),
            durable_publications_quarantined=_integer(
                data.get("durable_publications_quarantined", 0),
                "summary.durable_publications_quarantined",
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
    worker_backend: str
    started_at: float
    finished_at: float | None = None
    elapsed_ms: int = 0
    heartbeat_age_ms: int = 0
    heartbeat_subject_id: str = ""
    heartbeat_phase: str = ""
    heartbeat_failure_code: str = ""
    worker_request_sha256: str = ""
    worker_result_sha256: str = ""
    worker_tool_scope: tuple[str, ...] = ()
    worker_contract_failure_code: str = ""
    worker_job_id: str = ""
    worker_job_state: str = ""
    worker_claim_epoch: int = 0
    worker_job_failure_code: str = ""
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
            "worker_backend",
            "started_at", "finished_at", "elapsed_ms", "heartbeat_age_ms", "current_tool",
            "heartbeat_subject_id", "heartbeat_phase", "heartbeat_failure_code",
            "worker_request_sha256", "worker_result_sha256", "worker_tool_scope",
            "worker_contract_failure_code",
            "worker_job_id", "worker_job_state", "worker_claim_epoch",
            "worker_job_failure_code",
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
            worker_backend=_choice(
                data.get("worker_backend"),
                "execution.worker_backend",
                _WORKER_BACKENDS,
            ),
            started_at=_number(data.get("started_at", 0), "execution.started_at"),
            finished_at=(
                None if finished is None else _number(finished, "execution.finished_at")
            ),
            elapsed_ms=_integer(data.get("elapsed_ms", 0), "execution.elapsed_ms"),
            heartbeat_age_ms=_integer(
                data.get("heartbeat_age_ms", 0), "execution.heartbeat_age_ms"
            ),
            heartbeat_subject_id=_text(
                data.get("heartbeat_subject_id"),
                "execution.heartbeat_subject_id",
            ),
            heartbeat_phase=(
                _choice(
                    data.get("heartbeat_phase"),
                    "execution.heartbeat_phase",
                    _HEARTBEAT_PHASES,
                )
                if data.get("heartbeat_phase")
                else ""
            ),
            heartbeat_failure_code=_text(
                data.get("heartbeat_failure_code"),
                "execution.heartbeat_failure_code",
            ),
            worker_request_sha256=_sha256(
                data.get("worker_request_sha256"),
                "execution.worker_request_sha256",
            ),
            worker_result_sha256=_sha256(
                data.get("worker_result_sha256"),
                "execution.worker_result_sha256",
            ),
            worker_tool_scope=_texts(
                data.get("worker_tool_scope"),
                "execution.worker_tool_scope",
                256,
            ),
            worker_contract_failure_code=_text(
                data.get("worker_contract_failure_code"),
                "execution.worker_contract_failure_code",
            ),
            worker_job_id=_text(
                data.get("worker_job_id"),
                "execution.worker_job_id",
            ),
            worker_job_state=(
                _choice(
                    data.get("worker_job_state"),
                    "execution.worker_job_state",
                    _WORKER_JOB_STATES,
                )
                if data.get("worker_job_state")
                else ""
            ),
            worker_claim_epoch=_integer(
                data.get("worker_claim_epoch", 0),
                "execution.worker_claim_epoch",
            ),
            worker_job_failure_code=_text(
                data.get("worker_job_failure_code"),
                "execution.worker_job_failure_code",
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
class AgentResultDescriptor:
    delivery_id: str
    publication_id: str
    job_id: str
    task_id: str
    agent_name: str
    status: str
    delivered_at: str
    result_sha256: str
    delivery_sha256: str
    task_excerpt: str = ""
    response_excerpt: str = ""
    error_excerpt: str = ""
    content_truncated: bool = False
    response_bytes: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    reason_code: str = ""
    acknowledged: bool = False
    acknowledged_at: str = ""
    acknowledgement_receipt_sha256: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> AgentResultDescriptor:
        data = _mapping(value, "result")
        _only(data, {
            "delivery_id", "publication_id", "job_id", "task_id", "agent_name",
            "status", "delivered_at", "result_sha256", "delivery_sha256",
            "task_excerpt", "response_excerpt", "error_excerpt",
            "content_truncated", "response_bytes", "total_tokens",
            "total_cost_usd", "turns", "reason_code",
            "acknowledged", "acknowledged_at",
            "acknowledgement_receipt_sha256",
        }, "result")
        acknowledged = _boolean(
            data.get("acknowledged", False),
            "result.acknowledged",
        )
        acknowledged_at = _timestamp(
            data.get("acknowledged_at"),
            "result.acknowledged_at",
            optional=True,
        )
        acknowledgement_receipt_sha256 = _sha256(
            data.get("acknowledgement_receipt_sha256"),
            "result.acknowledgement_receipt_sha256",
        )
        if acknowledged != bool(acknowledged_at):
            raise ValueError("result acknowledged time is inconsistent")
        if acknowledged != bool(acknowledgement_receipt_sha256):
            raise ValueError("result acknowledgement receipt is inconsistent")
        return cls(
            delivery_id=_text(
                data.get("delivery_id"), "result.delivery_id", required=True
            ),
            publication_id=_text(
                data.get("publication_id"), "result.publication_id", required=True
            ),
            job_id=_text(data.get("job_id"), "result.job_id", required=True),
            task_id=_text(data.get("task_id"), "result.task_id", required=True),
            agent_name=_text(
                data.get("agent_name"), "result.agent_name", required=True
            ),
            status=_choice(data.get("status"), "result.status", _RESULT_STATUSES),
            delivered_at=_text(
                data.get("delivered_at"), "result.delivered_at", required=True
            ),
            result_sha256=_sha256(
                data.get("result_sha256"), "result.result_sha256", optional=False
            ),
            delivery_sha256=_sha256(
                data.get("delivery_sha256"),
                "result.delivery_sha256",
                optional=False,
            ),
            task_excerpt=_text(data.get("task_excerpt"), "result.task_excerpt"),
            response_excerpt=_text(
                data.get("response_excerpt"), "result.response_excerpt"
            ),
            error_excerpt=_text(data.get("error_excerpt"), "result.error_excerpt"),
            content_truncated=_boolean(
                data.get("content_truncated", False), "result.content_truncated"
            ),
            response_bytes=_integer(
                data.get("response_bytes", 0), "result.response_bytes"
            ),
            total_tokens=_integer(
                data.get("total_tokens", 0), "result.total_tokens"
            ),
            total_cost_usd=_number(
                data.get("total_cost_usd", 0), "result.total_cost_usd"
            ),
            turns=_integer(data.get("turns", 0), "result.turns"),
            reason_code=_text(data.get("reason_code"), "result.reason_code"),
            acknowledged=acknowledged,
            acknowledged_at=acknowledged_at,
            acknowledgement_receipt_sha256=(
                acknowledgement_receipt_sha256
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentRecoveryDescriptor:
    kind: str
    item_id: str
    job_id: str
    publication_id: str
    agent_name: str
    job_state: str
    recovery_state: str
    session_scope: str
    claim_epoch: int
    claim_expires_at: str
    attempt_count: int
    occurred_at: str
    request_sha256: str
    receipt_sha256: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.kind == "job":
            if self.publication_id:
                raise ValueError("job recovery 不得携带 publication_id")
            if self.job_state not in {"claimed", "running", "unknown"}:
                raise ValueError("job recovery 的 job_state 无效")
            if self.recovery_state not in {
                "claim_active",
                "worker_active",
                "reclaimable_prestart",
                "recovery_required",
                "outcome_unknown",
            }:
                raise ValueError("job recovery 的 recovery_state 无效")
        elif self.kind == "publication":
            if not self.publication_id:
                raise ValueError("publication recovery 缺少 publication_id")
            if self.recovery_state not in {
                "publication_pending",
                "publication_claim_expired",
                "publication_quarantined",
            }:
                raise ValueError("publication recovery 的 recovery_state 无效")
        if self.item_id != (
            self.job_id if self.kind == "job" else self.publication_id
        ):
            raise ValueError("recovery item_id 与 kind 标识不一致")

    @classmethod
    def from_dict(cls, value: Any) -> AgentRecoveryDescriptor:
        data = _mapping(value, "recovery")
        _only(data, {
            "kind", "item_id", "job_id", "publication_id", "agent_name",
            "job_state", "recovery_state", "session_scope", "claim_epoch",
            "claim_expires_at", "attempt_count", "occurred_at",
            "request_sha256", "receipt_sha256", "reason_code",
        }, "recovery")
        return cls(
            kind=_choice(data.get("kind"), "recovery.kind", _RECOVERY_KINDS),
            item_id=_text(
                data.get("item_id"), "recovery.item_id", required=True
            ),
            job_id=_text(data.get("job_id"), "recovery.job_id", required=True),
            publication_id=_text(
                data.get("publication_id"), "recovery.publication_id"
            ),
            agent_name=_text(
                data.get("agent_name"), "recovery.agent_name", required=True
            ),
            job_state=_choice(
                data.get("job_state"), "recovery.job_state", _WORKER_JOB_STATES
            ),
            recovery_state=_choice(
                data.get("recovery_state"),
                "recovery.recovery_state",
                _RECOVERY_STATES,
            ),
            session_scope=_choice(
                data.get("session_scope"),
                "recovery.session_scope",
                _SESSION_SCOPES,
            ),
            claim_epoch=_integer(
                data.get("claim_epoch", 0), "recovery.claim_epoch"
            ),
            claim_expires_at=_timestamp(
                data.get("claim_expires_at"),
                "recovery.claim_expires_at",
                optional=True,
            ),
            attempt_count=_integer(
                data.get("attempt_count", 0), "recovery.attempt_count"
            ),
            occurred_at=_timestamp(
                data.get("occurred_at"), "recovery.occurred_at"
            ),
            request_sha256=_sha256(
                data.get("request_sha256"),
                "recovery.request_sha256",
                optional=False,
            ),
            receipt_sha256=_sha256(
                data.get("receipt_sha256"),
                "recovery.receipt_sha256",
                optional=False,
            ),
            reason_code=_text(
                data.get("reason_code"), "recovery.reason_code", required=True
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentRecoveryCatalog:
    assessed_at: str = ""
    items: tuple[AgentRecoveryDescriptor, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        identities = tuple((item.kind, item.item_id) for item in self.items)
        if len(set(identities)) != len(identities):
            raise ValueError("recovery_catalog 包含重复条目")
        if (self.items or self.truncated) and not self.assessed_at:
            raise ValueError("非空 recovery_catalog 缺少 assessed_at")

    @classmethod
    def from_dict(cls, value: Any) -> AgentRecoveryCatalog:
        data = _mapping(value, "recovery_catalog")
        _only(data, {"assessed_at", "items", "truncated"}, "recovery_catalog")
        return cls(
            assessed_at=_timestamp(
                data.get("assessed_at"),
                "recovery_catalog.assessed_at",
                optional=True,
            ),
            items=tuple(
                AgentRecoveryDescriptor.from_dict(item)
                for item in _sequence(
                    data.get("items"), "recovery_catalog.items", _MAX_SMALL_ITEMS
                )
            ),
            truncated=_boolean(
                data.get("truncated", False), "recovery_catalog.truncated"
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
    results: tuple[AgentResultDescriptor, ...] = ()
    recovery_catalog: AgentRecoveryCatalog = field(
        default_factory=AgentRecoveryCatalog
    )
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
            "agents", "executions", "results", "recovery_catalog",
            "team_messages", "blackboard",
            "warnings",
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
            results=tuple(
                AgentResultDescriptor.from_dict(item)
                for item in _sequence(data.get("results"), "results", _MAX_SMALL_ITEMS)
            ),
            recovery_catalog=AgentRecoveryCatalog.from_dict(
                data.get("recovery_catalog", {})
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
    "AgentResultDescriptor",
    "AgentRecoveryCatalog",
    "AgentRecoveryDescriptor",
    "BlackboardDescriptor",
    "ExecutionDescriptor",
    "TeamMessageDescriptor",
]
