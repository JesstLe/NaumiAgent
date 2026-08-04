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
    AgentRecoveryCatalog,
    AgentRecoveryDescriptor,
    AgentResultDescriptor,
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
        results: tuple[AgentResultDescriptor, ...] = ()
        recovery_catalog = AgentRecoveryCatalog()
        team_messages: tuple[TeamMessageDescriptor, ...] = ()
        blackboard: tuple[BlackboardDescriptor, ...] = ()
        pending_messages = 0
        capacity = None
        publication_backlog = None
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

        try:
            if manager is not None:
                capacity = await manager.capacity_snapshot()
                if (
                    capacity is not None
                    and capacity.recovery_required_jobs
                ):
                    warnings.append(
                        "共享 Agent capacity 中有 "
                        f"{capacity.recovery_required_jobs} 个 running Job "
                        "需要恢复裁决，容量不会自动释放。"
                    )
        except Exception as exc:
            warnings.append(
                f"Agent capacity 读取失败：{type(exc).__name__}: {exc}"
            )

        try:
            if manager is not None:
                inbox = await manager.list_result_inbox(session_id, limit=50)
                result_items: list[AgentResultDescriptor] = []
                for entry in inbox:
                    content = entry.content
                    delivery = entry.delivery
                    task_excerpt, task_truncated = _public_excerpt(
                        content.payload.task
                    )
                    response_excerpt, response_truncated = _public_excerpt(
                        content.terminal_payload.response
                    )
                    error_excerpt, error_truncated = _public_excerpt(
                        content.terminal_payload.error
                    )
                    result_items.append(AgentResultDescriptor(
                        delivery_id=_public(delivery.delivery_id),
                        publication_id=_public(delivery.publication_id),
                        job_id=_public(delivery.job_id),
                        task_id=_public(content.payload.task_id),
                        agent_name=_public(content.request.agent_name),
                        status=str(content.result.status),
                        delivered_at=_public(delivery.delivered_at),
                        result_sha256=delivery.result_sha256,
                        delivery_sha256=delivery.delivery_sha256,
                        task_excerpt=task_excerpt,
                        response_excerpt=response_excerpt,
                        error_excerpt=error_excerpt,
                        content_truncated=(
                            task_truncated
                            or response_truncated
                            or error_truncated
                        ),
                        response_bytes=content.result.response_bytes,
                        total_tokens=content.result.total_tokens,
                        total_cost_usd=(
                            content.result.total_cost_microusd / 1_000_000
                        ),
                        turns=content.result.turns,
                        reason_code=_public(content.result.reason_code),
                    ))
                results = tuple(result_items)
        except Exception as exc:
            warnings.append(
                f"Agent 结果收件箱读取失败：{type(exc).__name__}: {exc}"
            )

        try:
            if manager is not None:
                publication_backlog = await manager.publication_backlog()
                if publication_backlog.expired_claims:
                    warnings.append(
                        "Agent publication 中有 "
                        f"{publication_backlog.expired_claims} 个过期 claim "
                        "等待恢复。"
                    )
                if publication_backlog.quarantined:
                    warnings.append(
                        "Agent publication 中有 "
                        f"{publication_backlog.quarantined} 个结果已隔离；"
                        "自动恢复不会静默删除这些证据。"
                    )
        except Exception as exc:
            warnings.append(
                f"Agent publication backlog 读取失败：{type(exc).__name__}: {exc}"
            )

        try:
            if manager is not None:
                durable_catalog = await manager.recovery_catalog(limit=50)
                recovery_catalog = _project_recovery_catalog(
                    durable_catalog,
                    session_id=session_id,
                    limit=50,
                )
                if recovery_catalog.truncated:
                    warnings.append(
                        "Agent 恢复目录已达到 50 项展示上限；"
                        "当前视图只展示高优先级有界前缀。"
                    )
        except Exception as exc:
            warnings.append(
                f"Agent 恢复目录读取失败：{type(exc).__name__}: {exc}"
            )

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
            durable_capacity_configured=capacity is not None,
            durable_active_jobs=capacity.active_jobs if capacity else 0,
            durable_max_active_jobs=(
                capacity.policy.max_active_jobs if capacity else 0
            ),
            durable_waiting_jobs=capacity.waiting_jobs if capacity else 0,
            durable_max_waiters=capacity.policy.max_waiters if capacity else 0,
            durable_reclaimable_jobs=(
                capacity.reclaimable_prestart_jobs if capacity else 0
            ),
            durable_recovery_required_jobs=(
                capacity.recovery_required_jobs if capacity else 0
            ),
            durable_results_visible=len(results),
            durable_publications_pending=(
                publication_backlog.pending if publication_backlog else 0
            ),
            durable_publications_claimed=(
                publication_backlog.live_claimed if publication_backlog else 0
            ),
            durable_publications_expired=(
                publication_backlog.expired_claims if publication_backlog else 0
            ),
            durable_publications_quarantined=(
                publication_backlog.quarantined if publication_backlog else 0
            ),
        )
        return AgentControlSnapshot(
            schema_version=AGENT_CONTROL_SCHEMA_VERSION,
            session_id=session_id,
            revision=0,
            generated_at="",
            summary=summary,
            agents=agents,
            executions=executions,
            results=results,
            recovery_catalog=recovery_catalog,
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
    comparable["recovery_catalog"]["assessed_at"] = ""
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
    elif section == "recovery_catalog":
        value["assessed_at"] = ""
    return value


def _value_summary(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return _public(text)


def _public(value: Any) -> str:
    return OutputGuardrail.redact(str(value or "")).strip()[:2000]


def _public_excerpt(value: Any) -> tuple[str, bool]:
    raw = str(value or "")
    redacted = OutputGuardrail.redact(raw)
    normalized = redacted.strip()
    return normalized[:2000], redacted != raw or len(normalized) > 2000


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _project_recovery_catalog(
    catalog: Any,
    *,
    session_id: str,
    limit: int,
) -> AgentRecoveryCatalog:
    assessed_at = str(catalog.assessed_at)
    assessed = datetime.fromisoformat(assessed_at)
    session_sha256 = (
        hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        if session_id
        else ""
    )
    items: list[AgentRecoveryDescriptor] = []
    for job in catalog.jobs:
        state = str(job.state)
        expiry = (
            datetime.fromisoformat(job.claim_expires_at)
            if job.claim_expires_at
            else None
        )
        if state == "claimed":
            recovery_state = (
                "reclaimable_prestart"
                if expiry is not None and expiry <= assessed
                else "claim_active"
            )
        elif state == "running":
            recovery_state = (
                "recovery_required"
                if expiry is not None and expiry <= assessed
                else "worker_active"
            )
        else:
            recovery_state = "outcome_unknown"
        items.append(AgentRecoveryDescriptor(
            kind="job",
            item_id=_public(job.job_id),
            job_id=_public(job.job_id),
            publication_id="",
            agent_name=_public(job.request.agent_name),
            job_state=state,
            recovery_state=recovery_state,
            session_scope=_session_scope(
                job.request.session_id_sha256,
                session_sha256=session_sha256,
            ),
            claim_epoch=max(0, int(job.claim_epoch)),
            claim_expires_at=_public(job.claim_expires_at),
            attempt_count=0,
            occurred_at=_public(job.latest_receipt.occurred_at),
            request_sha256=job.request_sha256,
            receipt_sha256=job.latest_receipt.receipt_sha256,
            reason_code=_public(job.latest_receipt.reason_code),
        ))
    for entry in catalog.publications:
        publication = entry.publication
        job = entry.job
        quarantine = entry.quarantine
        recovery_state = (
            "publication_quarantined"
            if quarantine is not None
            else (
                "publication_pending"
                if str(publication.state) == "pending"
                else "publication_claim_expired"
            )
        )
        items.append(AgentRecoveryDescriptor(
            kind="publication",
            item_id=_public(publication.publication_id),
            job_id=_public(publication.job_id),
            publication_id=_public(publication.publication_id),
            agent_name=_public(job.request.agent_name),
            job_state=str(job.state),
            recovery_state=recovery_state,
            session_scope=_session_scope(
                job.request.session_id_sha256,
                session_sha256=session_sha256,
            ),
            claim_epoch=max(0, int(publication.claim_epoch)),
            claim_expires_at=_public(publication.claim_expires_at),
            attempt_count=max(0, int(publication.attempt_count)),
            occurred_at=_public(
                quarantine.quarantined_at
                if quarantine is not None
                else publication.latest_receipt.occurred_at
            ),
            request_sha256=publication.request_sha256,
            receipt_sha256=(
                quarantine.receipt.receipt_sha256
                if quarantine is not None
                else publication.latest_receipt.receipt_sha256
            ),
            reason_code=_public(
                quarantine.failure_code
                if quarantine is not None
                else publication.latest_receipt.reason_code
            ),
        ))
    priority = {
        "recovery_required": 0,
        "publication_quarantined": 1,
        "outcome_unknown": 2,
        "publication_claim_expired": 3,
        "reclaimable_prestart": 4,
        "publication_pending": 5,
        "worker_active": 6,
        "claim_active": 7,
    }
    items.sort(key=lambda item: (
        priority[item.recovery_state],
        item.occurred_at,
        item.kind,
        item.item_id,
    ))
    return AgentRecoveryCatalog(
        assessed_at=assessed_at,
        items=tuple(items[:limit]),
        truncated=(
            bool(catalog.jobs_truncated)
            or bool(catalog.publications_truncated)
            or len(items) > limit
        ),
    )


def _session_scope(request_session_sha256: str, *, session_sha256: str) -> str:
    if not session_sha256:
        return "unknown"
    return "current" if request_session_sha256 == session_sha256 else "other"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["AgentControlService"]
