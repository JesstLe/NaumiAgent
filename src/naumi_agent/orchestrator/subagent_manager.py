"""子 Agent 调度器 — 管理、选择、并行执行、生命周期."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import signature
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from naumi_agent.agents.base import (
    AgentCapability,
    AgentConfig,
    AgentResult,
    BaseAgent,
    resolve_agent_tool_names,
)
from naumi_agent.agents.factory import DynamicAgentFactory
from naumi_agent.agents.message_bus import AgentMessage, AgentMessageBus
from naumi_agent.agents.presets import ALL_AGENT_CONFIGS
from naumi_agent.daemons.agent_jobs import (
    AgentJobCapacityExhaustedError,
    AgentJobCapacitySnapshot,
    AgentJobError,
    AgentJobKeyUnavailableError,
    AgentJobLifecycleConflictError,
    AgentJobPayload,
    AgentJobPublicationBacklog,
    AgentJobPublicationContent,
    AgentJobPublicationDeliveryTransition,
    AgentJobRecoveryCatalog,
    AgentJobResultAcknowledgementTransition,
    AgentJobState,
    AgentJobStore,
    AgentJobTerminalPayload,
    AgentJobTransitionResult,
    StoredAgentJob,
    StoredAgentJobPublicationDelivery,
    StoredAgentJobResultAcknowledgement,
)
from naumi_agent.daemons.agent_worker_contract import (
    AgentWorkerRequest,
    AgentWorkerResult,
    issue_agent_worker_request,
    issue_agent_worker_result,
)
from naumi_agent.daemons.agent_worker_process import (
    AgentWorkerProcessError,
    AgentWorkerProcessFactory,
    AgentWorkerProcessState,
)
from naumi_agent.hooks import HookContext, HookManager, HookPoint
from naumi_agent.runtime.agent_heartbeat import (
    AgentExecutionHeartbeatFactory,
    AgentExecutionHeartbeatLifecycle,
)
from naumi_agent.runtime.ports.events import LegacyEventCallback, RuntimeEventType
from naumi_agent.tools.base import ToolCall, ToolResult

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SECONDS = 300  # 5 minutes
_REAPER_INTERVAL_SECONDS = 30
_AGENT_JOB_LEASE_SECONDS = 90
_AGENT_JOB_RENEW_INTERVAL_SECONDS = 30
_AGENT_PUBLICATION_LEASE_SECONDS = 60
_AGENT_PUBLICATION_RECOVERY_LIMIT = 100
_AGENT_JOB_CAPACITY_POLL_MIN_SECONDS = 0.05
_AGENT_JOB_CAPACITY_POLL_MAX_SECONDS = 0.5
_AGENT_ADMISSION_STACK: ContextVar[tuple[int, ...]] = ContextVar(
    "naumi_agent_admission_stack",
    default=(),
)

# 关键词 → Agent 映射
_KEYWORD_AGENT_MAP: dict[str, str] = {
    "code": "coder",
    "write_code": "coder",
    "debug": "coder",
    "test": "coder",
    "refactor": "coder",
    "implement": "coder",
    "fix": "coder",
    "program": "coder",
    "research": "researcher",
    "search": "researcher",
    "analyze": "researcher",
    "investigate": "researcher",
    "browse": "browser",
    "navigate": "browser",
    "fill_form": "browser",
    "scrape": "browser",
    "click": "browser",
}


class AgentState(StrEnum):
    SPAWNED = "spawned"
    READY = "ready"
    RUNNING = "running"
    IDLE = "idle"
    DESTROYED = "destroyed"


class _AgentCapacityWaitCancelledError(RuntimeError):
    """Internal control flow for a user-cancelled durable capacity wait."""


@dataclass
class AgentLifecycle:
    name: str
    state: AgentState = AgentState.SPAWNED
    spawned_at: float = 0.0
    last_updated: float = field(default_factory=time.monotonic)
    idle_since: float | None = None
    task_count: int = 0

    def __post_init__(self) -> None:
        if not self.spawned_at:
            self.spawned_at = time.monotonic()


@dataclass(frozen=True)
class SubTask:
    """子任务定义."""

    id: str
    description: str
    agent_name: str | None = None
    depends_on: list[str] | None = None
    context: str = ""

    def __post_init__(self) -> None:
        if self.depends_on is None:
            object.__setattr__(self, "depends_on", [])


@dataclass(frozen=True)
class AgentExecutionRecord:
    """Public immutable snapshot of one delegated execution."""

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


@dataclass(frozen=True)
class StopExecutionResult:
    """Deterministic outcome of an execution stop request."""

    task_id: str
    accepted: bool
    code: str
    message: str


@dataclass(frozen=True)
class AgentRecoveryActionResult:
    """Content-free result of one exact durable recovery action."""

    action: str
    job_id: str
    accepted: bool
    applied: bool
    code: str
    message: str
    job_state: str
    claim_epoch: int
    receipt_sha256: str


@dataclass(frozen=True)
class AgentResultAcknowledgementActionResult:
    """Public result of one current-session durable result acknowledgement."""

    delivery_id: str
    accepted: bool
    applied: bool
    code: str
    message: str
    acknowledgement_id: str
    acknowledged_at: str
    receipt_sha256: str


@dataclass(frozen=True)
class AgentPublicationRecoverySummary:
    """Content-free summary of one bounded durable publication recovery pass."""

    scanned: int = 0
    delivered: int = 0
    notification_failures: int = 0
    quarantined: int = 0
    failed: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentPublicationInboxEntry:
    """Authenticated durable result content paired with its delivery fence."""

    delivery: StoredAgentJobPublicationDelivery
    content: AgentJobPublicationContent
    acknowledgement: StoredAgentJobResultAcknowledgement | None = None


@dataclass
class _ActiveExecution:
    task_id: str
    session_id: str
    agent_name: str
    description: str
    worker_request: AgentWorkerRequest
    started_at: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    last_updated_mono: float = field(default_factory=time.monotonic)
    status: str = "running"
    phase: str = "starting"
    current_tool: str = ""
    recent_tools: list[str] = field(default_factory=list)
    stop_requested: bool = False
    stop_reason: str = ""
    execute_task: asyncio.Task[AgentResult] | None = None
    heartbeat_lifecycle: AgentExecutionHeartbeatLifecycle | None = None
    heartbeat_failure_code: str = ""
    worker_result: AgentWorkerResult | None = None
    worker_contract_failure_code: str = ""
    worker_job_id: str = ""
    worker_job_state: str = ""
    worker_claim_epoch: int = 0
    worker_job_failure_code: str = ""
    worker_job_renewal_task: asyncio.Task[None] | None = None
    worker_backend: str = "embedded"


class SubAgentManager:
    """管理和调度子 Agent（含生命周期状态机 + 自动回收）."""

    def __init__(
        self,
        engine: AgentEngine,
        *,
        heartbeat_factory: AgentExecutionHeartbeatFactory | None = None,
        agent_job_store: AgentJobStore | None = None,
        agent_worker_process_factory: AgentWorkerProcessFactory | None = None,
    ) -> None:
        if heartbeat_factory is not None and not isinstance(
            heartbeat_factory,
            AgentExecutionHeartbeatFactory,
        ):
            raise TypeError(
                "heartbeat_factory 必须是 AgentExecutionHeartbeatFactory。"
            )
        self._engine = engine
        self._heartbeat_factory = heartbeat_factory
        resolved_agent_job_store = agent_job_store
        if resolved_agent_job_store is None:
            resolved_agent_job_store = getattr(
                getattr(engine, "_resources", None),
                "agent_job_store",
                None,
            )
        if not isinstance(resolved_agent_job_store, AgentJobStore):
            raise TypeError("agent_job_store 必须是 AgentJobStore。")
        self._agent_job_store = resolved_agent_job_store
        if agent_worker_process_factory is not None and not isinstance(
            agent_worker_process_factory,
            AgentWorkerProcessFactory,
        ):
            raise TypeError(
                "agent_worker_process_factory 必须是 AgentWorkerProcessFactory。"
            )
        self._agent_worker_process_factory = agent_worker_process_factory
        self._agent_job_owner_id = f"embedded-agent-{uuid4().hex}"
        self._agent_publication_owner_id = (
            f"embedded-agent-publisher-{uuid4().hex}"
        )
        self._last_publication_recovery = AgentPublicationRecoverySummary()
        self._publication_recovery_wake: Callable[[], bool] | None = None
        self._agents: dict[str, BaseAgent] = {}
        self._configs: dict[str, AgentConfig] = dict(ALL_AGENT_CONFIGS)
        self._factory = DynamicAgentFactory(engine.router)
        self.message_bus = AgentMessageBus()
        self._lifecycle: dict[str, AgentLifecycle] = {}
        self._event_history: list[dict[str, Any]] = []
        self._reaper_task: asyncio.Task[None] | None = None
        self._hooks: HookManager = engine.hooks
        self._execution_lock = asyncio.Lock()
        self._active_executions: dict[str, _ActiveExecution] = {}
        self._execution_history: list[AgentExecutionRecord] = []
        self._max_parallel_agents = engine._config.safety.max_parallel_agents
        self._max_queued_agents = engine._config.safety.max_queued_agents
        self._admitted_parallel_agents = 0
        self._parallel_agent_slots = asyncio.BoundedSemaphore(
            self._max_parallel_agents
        )
        self._queued_parallel_agents = 0

    # --- 生命周期状态机 ---

    @property
    def max_parallel_agents(self) -> int:
        return self._max_parallel_agents

    @property
    def max_queued_agents(self) -> int:
        return self._max_queued_agents

    @property
    def active_execution_count(self) -> int:
        return len(self._active_executions)

    @property
    def queued_parallel_agent_count(self) -> int:
        return self._queued_parallel_agents

    def get_lifecycle(self, name: str) -> AgentLifecycle | None:
        return self._lifecycle.get(name)

    def get_state(self, name: str) -> AgentState | None:
        lc = self._lifecycle.get(name)
        return lc.state if lc else None

    def get_recent_events(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return recent subagent lifecycle events for context preservation."""
        safe_limit = max(1, min(limit, 50))
        return list(self._event_history[-safe_limit:])

    def _transition(self, name: str, new_state: AgentState) -> None:
        lc = self._lifecycle.get(name)
        if not lc:
            return
        old = lc.state
        lc.state = new_state
        lc.last_updated = time.monotonic()
        if new_state == AgentState.IDLE:
            lc.idle_since = time.monotonic()
        else:
            lc.idle_since = None
        if new_state == AgentState.RUNNING:
            lc.task_count += 1
        logger.debug("Agent %s: %s → %s", name, old.value, new_state.value)

    def _ensure_lifecycle(self, name: str) -> AgentLifecycle:
        if name not in self._lifecycle:
            self._lifecycle[name] = AgentLifecycle(name=name)
        return self._lifecycle[name]

    async def start_reaper(self) -> None:
        """启动后台回收协程."""
        if self._reaper_task and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        logger.info("Agent reaper started (interval=%ds, idle_timeout=%ds)",
                     _REAPER_INTERVAL_SECONDS, _IDLE_TIMEOUT_SECONDS)

    async def stop_reaper(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            logger.info("Agent reaper stopped")

    async def _reaper_loop(self) -> None:
        """定期扫描并回收空闲超时的动态 Agent."""
        while True:
            await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
            try:
                now = time.monotonic()
                preset_names = set(ALL_AGENT_CONFIGS.keys())
                to_reap = [
                    name for name, lc in self._lifecycle.items()
                    if name not in preset_names
                    and lc.state == AgentState.IDLE
                    and lc.idle_since
                    and (now - lc.idle_since) > _IDLE_TIMEOUT_SECONDS
                ]
                for name in to_reap:
                    logger.info("Reaping idle agent '%s' (idle %.0fs)",
                                name, now - (self._lifecycle[name].idle_since or now))
                    self.destroy(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reaper error")

    def _start_reaper_if_possible(self) -> None:
        """Start the dynamic-agent reaper from sync creation paths when a loop exists."""
        if self._reaper_task and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper_task = loop.create_task(self._reaper_loop())
        logger.info("Agent reaper started lazily for dynamic agents")

    def get_agent(self, name: str) -> BaseAgent | None:
        """获取或创建 Agent 实例."""
        if name in self._agents:
            return self._agents[name]

        config = self._configs.get(name)
        if not config:
            return None

        agent = BaseAgent(config, self._engine)
        self._agents[name] = agent
        self._ensure_lifecycle(name)
        self._transition(name, AgentState.IDLE)
        return agent

    def spawn(self, config: AgentConfig) -> BaseAgent:
        """动态创建并注册一个新 Agent（用于 MoE 专家等临时 Agent）."""
        name = config.name
        if name in self._agents:
            return self._agents[name]

        if name not in self._configs:
            self._configs[name] = config

        agent = BaseAgent(config, self._engine)
        self._agents[name] = agent
        lc = self._ensure_lifecycle(name)
        lc.spawned_at = time.monotonic()
        self._transition(name, AgentState.SPAWNED)
        self._transition(name, AgentState.READY)
        self._start_reaper_if_possible()
        logger.info("Spawned dynamic agent: %s", name)
        return agent

    def spawn_for_task(
        self,
        name: str,
        task_description: str,
        *,
        role: str = "expert_analyst",
        focus: str = "",
        domain: str = "",
        model_tier: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        extra_capabilities: list[AgentCapability] | None = None,
    ) -> BaseAgent:
        """基于任务描述自动生成 AgentConfig 并 spawn.

        Uses DynamicAgentFactory to infer capabilities, domain, model tier,
        and generate a specialized system prompt from the task description.
        """
        config = self._factory.create_config(
            name=name,
            task_description=task_description,
            role=role,
            focus=focus,
            domain=domain,
            model_tier=model_tier,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_capabilities=extra_capabilities,
        )
        return self.spawn(config)

    async def spawn_for_task_with_llm(
        self,
        name: str,
        task_description: str,
        *,
        role: str = "expert_analyst",
        focus: str = "",
        domain: str = "",
        model_tier: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        extra_capabilities: list[AgentCapability] | None = None,
    ) -> BaseAgent:
        """基于任务描述自动生成 AgentConfig（LLM 生成 system prompt）并 spawn."""
        config = await self._factory.create_config_with_llm_prompt(
            name=name,
            task_description=task_description,
            role=role,
            focus=focus,
            domain=domain,
            model_tier=model_tier,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_capabilities=extra_capabilities,
        )
        return self.spawn(config)

    def destroy_all_dynamic(self) -> list[str]:
        """销毁所有动态 Agent（保留预设 Agent）.

        Returns list of destroyed agent names.
        """
        from naumi_agent.agents.presets import ALL_AGENT_CONFIGS

        preset_names = set(ALL_AGENT_CONFIGS.keys())
        dynamic_names = [
            name for name in self._agents if name not in preset_names
        ]
        destroyed: list[str] = []
        for name in dynamic_names:
            if self.destroy(name):
                destroyed.append(name)
        return destroyed

    def destroy(self, name: str) -> bool:
        """销毁一个动态 Agent，释放资源.

        返回 True 表示成功销毁，False 表示 Agent 不存在或属于预设 Agent。
        预设 Agent（coder/researcher/browser）不可销毁。
        """
        from naumi_agent.agents.presets import ALL_AGENT_CONFIGS

        if name in ALL_AGENT_CONFIGS:
            logger.warning("Cannot destroy preset agent: %s", name)
            return False

        if name not in self._agents:
            return False

        self._transition(name, AgentState.DESTROYED)
        self._agents.pop(name, None)
        self._configs.pop(name, None)
        self._lifecycle.pop(name, None)
        logger.info("Destroyed dynamic agent: %s", name)
        return True

    def select_agent(self, task_description: str) -> str | None:
        """根据任务描述选择最合适的 Agent."""
        lower = task_description.lower()
        best_match: str | None = None
        best_len = 0

        for keyword, agent_name in _KEYWORD_AGENT_MAP.items():
            if keyword in lower and len(keyword) > best_len:
                best_match = agent_name
                best_len = len(keyword)

        return best_match

    def list_executions(self, limit: int = 100) -> list[AgentExecutionRecord]:
        """Return active and recent executions without exposing task handles."""
        safe_limit = max(1, min(int(limit), 100))
        now = time.monotonic()
        active = [
            _execution_record(item, now=now)
            for item in self._active_executions.values()
        ]
        active.sort(key=lambda item: item.started_at, reverse=True)
        history = list(reversed(self._execution_history))
        return (active + history)[:safe_limit]

    async def capacity_snapshot(self) -> AgentJobCapacitySnapshot | None:
        """Expose the durable embedded capacity authority without raw jobs."""
        return await self._agent_job_store.capacity_snapshot()

    async def list_result_inbox(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> tuple[AgentPublicationInboxEntry, ...]:
        """Recover a bounded session inbox through authenticated delivery fences."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是整数。")
        safe_limit = max(1, min(limit, 50))
        records = await self._agent_job_store.list_result_inbox_records(
            session_id,
            limit=safe_limit,
            newest_first=True,
        )
        entries: list[AgentPublicationInboxEntry] = []
        for record in records:
            delivery = record.delivery
            content = await self._agent_job_store.recover_delivered_result(
                delivery.delivery_id,
                expected_delivery_sha256=delivery.delivery_sha256,
            )
            entries.append(AgentPublicationInboxEntry(
                delivery,
                content,
                record.acknowledgement,
            ))
        return tuple(entries)

    async def acknowledge_result_inbox(
        self,
        *,
        session_id: str,
        delivery_id: str,
        expected_delivery_sha256: str,
    ) -> AgentResultAcknowledgementActionResult:
        """Acknowledge one exact current-session result without deleting it."""
        normalized_delivery = str(delivery_id or "").strip()
        try:
            transition: AgentJobResultAcknowledgementTransition = (
                await self._agent_job_store.acknowledge_result_inbox_delivery(
                    str(session_id or "").strip(),
                    normalized_delivery,
                    expected_delivery_sha256=str(
                        expected_delivery_sha256 or ""
                    ).strip(),
                )
            )
        except AgentJobLifecycleConflictError:
            return AgentResultAcknowledgementActionResult(
                normalized_delivery,
                False,
                False,
                "result_ack_fence_changed",
                "结果投递事实或当前会话已变化，请刷新后重试。",
                "",
                "",
                "",
            )
        except (AgentJobError, TypeError, ValueError):
            return AgentResultAcknowledgementActionResult(
                normalized_delivery,
                False,
                False,
                "result_ack_unavailable",
                "当前无法确认该结果；持久结果和已读状态均未被改写。",
                "",
                "",
                "",
            )
        acknowledgement = transition.acknowledgement
        return AgentResultAcknowledgementActionResult(
            delivery_id=acknowledgement.delivery_id,
            accepted=True,
            applied=transition.applied,
            code=(
                "result_acknowledged"
                if transition.applied
                else "result_already_acknowledged"
            ),
            message=(
                "已将该持久结果标记为已读；加密结果仍完整保留。"
                if transition.applied
                else "该持久结果已经确认过；未重复写入回执。"
            ),
            acknowledgement_id=acknowledgement.acknowledgement_id,
            acknowledged_at=acknowledgement.acknowledged_at,
            receipt_sha256=acknowledgement.receipt.receipt_sha256,
        )

    async def publication_backlog(self) -> AgentJobPublicationBacklog:
        """Expose content-free durable publication backlog counters."""
        return await self._agent_job_store.publication_backlog()

    async def recovery_catalog(
        self,
        *,
        limit: int = 50,
    ) -> AgentJobRecoveryCatalog:
        """Expose bounded authenticated recovery facts without raw content."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是整数。")
        return await self._agent_job_store.recovery_catalog(
            limit=max(1, min(limit, 50)),
        )

    async def resolve_recovery_unknown(
        self,
        *,
        session_id: str,
        job_id: str,
        expected_request_sha256: str,
        expected_claim_epoch: int,
        expected_latest_receipt_sha256: str,
    ) -> AgentRecoveryActionResult:
        """Fence one expired running Job into unknown without replaying it."""
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            return AgentRecoveryActionResult(
                "resolve_unknown", str(job_id or ""), False, False,
                "missing_session", "Agent 恢复请求缺少当前会话。", "", 0, "",
            )
        session_sha256 = hashlib.sha256(
            normalized_session.encode("utf-8")
        ).hexdigest()
        try:
            transition = await self._agent_job_store.mark_recovery_unknown(
                str(job_id or "").strip(),
                expected_request_sha256=str(expected_request_sha256 or "").strip(),
                expected_session_id_sha256=session_sha256,
                expected_claim_epoch=expected_claim_epoch,
                expected_latest_receipt_sha256=str(
                    expected_latest_receipt_sha256 or ""
                ).strip(),
            )
        except AgentJobLifecycleConflictError:
            return AgentRecoveryActionResult(
                "resolve_unknown", str(job_id or "").strip(), False, False,
                "recovery_fence_changed",
                "恢复事实已变化，请刷新后重新检查。", "", 0, "",
            )
        except (AgentJobError, TypeError, ValueError):
            return AgentRecoveryActionResult(
                "resolve_unknown", str(job_id or "").strip(), False, False,
                "recovery_unavailable",
                "当前无法完成恢复裁决；持久权威未被改写。", "", 0, "",
            )
        job = transition.job
        return AgentRecoveryActionResult(
            action="resolve_unknown",
            job_id=job.job_id,
            accepted=True,
            applied=transition.applied,
            code=(
                "recovery_resolved_unknown"
                if transition.applied
                else "already_resolved_unknown"
            ),
            message=(
                "已将过期 running Agent Job 收口为 unknown；不会自动重放模型。"
                if transition.applied
                else "该 Agent Job 已按同一恢复 fence 收口为 unknown。"
            ),
            job_state=str(job.state),
            claim_epoch=job.claim_epoch,
            receipt_sha256=job.latest_receipt.receipt_sha256,
        )

    def publication_recovery_status(self) -> dict[str, object]:
        """Expose only bounded counters and stable failure codes."""
        summary = self._last_publication_recovery
        return {
            "scanned": summary.scanned,
            "delivered": summary.delivered,
            "notification_failures": summary.notification_failures,
            "quarantined": summary.quarantined,
            "failed": summary.failed,
            "failure_codes": list(summary.failure_codes),
        }

    def set_publication_recovery_wake(
        self,
        wake: Callable[[], bool] | None,
    ) -> None:
        """Register the Engine-owned periodic worker wake port."""
        if wake is not None and not callable(wake):
            raise TypeError("publication recovery wake 必须可调用。")
        self._publication_recovery_wake = wake

    async def recover_pending_publications(
        self,
        *,
        limit: int = _AGENT_PUBLICATION_RECOVERY_LIMIT,
        max_attempts: int = 5,
    ) -> AgentPublicationRecoverySummary:
        """Deliver a bounded FIFO prefix of terminal publications after restart."""
        safe_limit = max(1, min(int(limit), 1000))
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 1000
        ):
            raise ValueError("Agent publication max_attempts 必须在 1 到 1000 之间。")
        scanned = 0
        delivered = 0
        notification_failures = 0
        quarantined = 0
        failure_codes: list[str] = []
        for _ in range(safe_limit):
            try:
                claimed = await self._agent_job_store.claim_next_publication(
                    owner_id=self._agent_publication_owner_id,
                    lease_seconds=_AGENT_PUBLICATION_LEASE_SECONDS,
                )
            except Exception as exc:
                logger.warning(
                    "Agent publication recovery claim failed: %s",
                    type(exc).__name__,
                )
                failure_codes.append("agent_publication_recovery_claim_failed")
                break
            if claimed is None:
                break
            scanned += 1
            publication = claimed.publication
            try:
                content = await self._agent_job_store.recover_publication_content(
                    publication.publication_id,
                    owner_id=self._agent_publication_owner_id,
                    claim_epoch=publication.claim_epoch,
                )
                transition = (
                    await self._agent_job_store.deliver_publication_to_inbox(
                        publication.publication_id,
                        owner_id=self._agent_publication_owner_id,
                        claim_epoch=publication.claim_epoch,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Agent publication recovery delivery failed [%s]: %s",
                    publication.publication_id,
                    type(exc).__name__,
                )
                failure_code = "agent_publication_recovery_delivery_failed"
                if publication.attempt_count >= max_attempts:
                    try:
                        await self._agent_job_store.quarantine_publication(
                            publication.publication_id,
                            owner_id=self._agent_publication_owner_id,
                            claim_epoch=publication.claim_epoch,
                            max_attempts=max_attempts,
                            failure_code=failure_code,
                        )
                    except Exception as quarantine_exc:
                        logger.warning(
                            "Agent publication quarantine failed [%s]: %s",
                            publication.publication_id,
                            type(quarantine_exc).__name__,
                        )
                        failure_codes.append(
                            "agent_publication_recovery_quarantine_failed"
                        )
                        await self._release_publication_after_failure(
                            publication.publication_id,
                            claim_epoch=publication.claim_epoch,
                        )
                        break
                    quarantined += 1
                    continue
                failure_codes.append(failure_code)
                await self._release_publication_after_failure(
                    publication.publication_id,
                    claim_epoch=publication.claim_epoch,
                )
                break
            delivered += 1
            if not await self._publish_publication_notification(
                content,
                transition,
            ):
                notification_failures += 1

        summary = AgentPublicationRecoverySummary(
            scanned=scanned,
            delivered=delivered,
            notification_failures=notification_failures,
            quarantined=quarantined,
            failed=len(failure_codes),
            failure_codes=tuple(sorted(set(failure_codes))),
        )
        self._last_publication_recovery = summary
        return summary

    async def stop_execution(
        self,
        task_id: str,
        reason: str = "用户请求停止子 Agent。",
    ) -> StopExecutionResult:
        """Stop exactly one active execution by task ID."""
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return StopExecutionResult(
                task_id="",
                accepted=False,
                code="missing_task_id",
                message="停止 Agent 执行时缺少 task_id。",
            )

        execute_task: asyncio.Task[AgentResult] | None = None
        async with self._execution_lock:
            execution = self._active_executions.get(normalized_id)
            if execution is None:
                finished = any(
                    item.task_id == normalized_id
                    for item in self._execution_history
                )
                code = "already_finished" if finished else "not_found"
                message = (
                    f"Agent 执行 {normalized_id} 已结束。"
                    if finished
                    else f"未找到 Agent 执行 {normalized_id}。"
                )
                return StopExecutionResult(normalized_id, False, code, message)
            if execution.stop_requested:
                return StopExecutionResult(
                    normalized_id,
                    False,
                    "already_requested",
                    f"Agent 执行 {normalized_id} 已在停止中。",
                )
            execution.stop_requested = True
            execution.stop_reason = str(reason or "用户请求停止子 Agent。").strip()
            execution.status = "stopping"
            execution.phase = "stopping"
            execution.last_updated_mono = time.monotonic()
            execute_task = execution.execute_task

        if execute_task is not None and not execute_task.done():
            execute_task.cancel()
        return StopExecutionResult(
            normalized_id,
            True,
            "accepted",
            f"已请求停止 Agent 执行 {normalized_id}。",
        )

    async def _register_execution(
        self,
        task: SubTask,
        agent_name: str,
        worker_request: AgentWorkerRequest,
        *,
        worker_backend: str = "embedded",
    ) -> bool:
        if worker_backend not in {"embedded", "independent"}:
            raise ValueError("worker_backend 无效。")
        async with self._execution_lock:
            if task.id in self._active_executions:
                return False
            self._active_executions[task.id] = _ActiveExecution(
                task_id=task.id,
                session_id=str(
                    getattr(getattr(self._engine, "_session", None), "id", "") or ""
                ),
                agent_name=agent_name,
                description=task.description,
                worker_request=worker_request,
                worker_backend=worker_backend,
            )
            return True

    async def _admit_independent_agent_job(
        self,
        task_id: str,
        *,
        request: AgentWorkerRequest,
        payload: AgentJobPayload,
    ) -> None:
        admitted = await self._agent_job_store.admit(
            request=request,
            payload=payload,
        )
        if admitted.state is not AgentJobState.ADMITTED:
            raise AgentJobError(
                "独立 Agent Worker 只接受 admitted Job；现有 Job 已进入其他 owner。"
            )
        await self._record_agent_job_transition(task_id, admitted)

    async def _admit_and_claim_agent_job(
        self,
        task_id: str,
        *,
        request: AgentWorkerRequest,
        payload: AgentJobPayload,
    ) -> None:
        admission = await self._agent_job_store.admit_for_capacity(
            request=request,
            payload=payload,
            owner_id=self._agent_job_owner_id,
            lease_seconds=_AGENT_JOB_LEASE_SECONDS,
            max_active_jobs=self._max_parallel_agents,
            max_waiters=self._max_queued_agents,
        )
        await self._record_agent_job_transition(task_id, admission.job)
        claimed = admission
        if admission.job.state is AgentJobState.ADMITTED:
            claimed = await self._await_agent_job_capacity(
                task_id,
                job_id=admission.job.job_id,
            )
        if claimed.job.state is not AgentJobState.CLAIMED:
            raise AgentJobError(
                "AgentJob capacity admission 未取得可执行 claim。"
            )
        recovered = await self._agent_job_store.recover_payload(
            claimed.job.job_id,
            owner_id=self._agent_job_owner_id,
            claim_epoch=claimed.job.claim_epoch,
        )
        if recovered != payload:
            raise AgentJobError(
                "AgentJob 恢复 payload 与本次执行不一致。"
            )
        await self._record_agent_job_transition(task_id, claimed.job)

    async def _await_agent_job_capacity(
        self,
        task_id: str,
        *,
        job_id: str,
    ) -> AgentJobTransitionResult:
        delay = _AGENT_JOB_CAPACITY_POLL_MIN_SECONDS
        while True:
            async with self._execution_lock:
                execution = self._active_executions.get(task_id)
                if execution is None:
                    raise AgentJobError("AgentJob 对应的活动执行已消失。")
                execution.phase = "waiting_capacity"
                execution.last_updated_mono = time.monotonic()
                stop_requested = execution.stop_requested
                stop_reason = execution.stop_reason
            if stop_requested:
                cancelled = await self._agent_job_store.cancel_before_claim(job_id)
                await self._record_agent_job_transition(
                    task_id,
                    cancelled.job,
                )
                raise _AgentCapacityWaitCancelledError(
                    stop_reason or "用户请求停止等待中的子 Agent。"
                )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                cancelled = await self._agent_job_store.cancel_before_claim(job_id)
                await self._record_agent_job_transition(
                    task_id,
                    cancelled.job,
                )
                raise
            transition = await self._agent_job_store.claim_for_capacity(
                job_id,
                owner_id=self._agent_job_owner_id,
                lease_seconds=_AGENT_JOB_LEASE_SECONDS,
                max_active_jobs=self._max_parallel_agents,
                max_waiters=self._max_queued_agents,
            )
            await self._record_agent_job_transition(
                task_id,
                transition.job,
            )
            if transition.job.state is AgentJobState.CLAIMED:
                return transition
            delay = min(
                _AGENT_JOB_CAPACITY_POLL_MAX_SECONDS,
                delay * 2,
            )

    async def _record_agent_job_transition(
        self,
        task_id: str,
        job: StoredAgentJob,
    ) -> None:
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                raise AgentJobError("AgentJob 对应的活动执行已消失。")
            execution.worker_job_id = job.job_id
            execution.worker_job_state = job.state.value
            execution.worker_claim_epoch = job.claim_epoch
            if job.state is AgentJobState.CLAIMED:
                execution.phase = "starting"
            execution.last_updated_mono = time.monotonic()

    async def _mark_agent_job_running(self, task_id: str) -> None:
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None or not execution.worker_job_id:
                raise AgentJobError("AgentJob 活动执行绑定缺失。")
            job_id = execution.worker_job_id
            claim_epoch = execution.worker_claim_epoch
        running = await self._agent_job_store.mark_running(
            job_id,
            owner_id=self._agent_job_owner_id,
            claim_epoch=claim_epoch,
        )
        renewal = asyncio.create_task(
            self._agent_job_renewal_loop(
                task_id,
                job_id=job_id,
                claim_epoch=claim_epoch,
            )
        )
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                renewal.cancel()
            else:
                execution.worker_job_state = running.job.state.value
                execution.worker_job_renewal_task = renewal
                execution.last_updated_mono = time.monotonic()
        if execution is None:
            await asyncio.gather(renewal, return_exceptions=True)

    async def _execute_independent_agent_job(
        self,
        *,
        task_id: str,
        agent: BaseAgent,
        event_callback: LegacyEventCallback | None,
    ) -> AgentResult:
        factory = self._agent_worker_process_factory
        if factory is None or not factory.model_execution_enabled:
            raise AgentWorkerProcessError(
                "独立 Agent Worker 模型执行能力未配置。"
            )
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None or not execution.worker_job_id:
                raise AgentJobError("独立 Agent Worker 缺少 admitted Job。")
            job_id = execution.worker_job_id
            request = execution.worker_request
            agent_name = execution.agent_name

        process = factory.create(
            worker_id=f"agent-worker-local-{uuid4().hex}",
            max_concurrent_jobs=1,
        )
        try:
            await process.start()
            await process.bind_job(job_id)
            await self._sync_independent_agent_job(task_id, job_id)

            async def observed_event(
                event: str,
                data: dict[str, Any],
            ) -> None:
                try:
                    event_type = RuntimeEventType(event)
                except ValueError as exc:
                    raise ValueError(f"未知 Runtime 事件：{event}") from exc
                await self._observe_execution_event(
                    task_id,
                    event_type.value,
                    data,
                )
                if event_callback is not None:
                    await event_callback(event_type.value, data)

            async def execute_authoritatively(
                call: ToolCall,
                requested_agent_name: str,
            ) -> ToolResult:
                if requested_agent_name != agent_name:
                    raise AgentWorkerProcessError(
                        "Agent Worker Tool RPC agent identity fence 无效。"
                    )
                hook_ctx = await self._hooks.fire(
                    HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={
                            "tool_name": call.name,
                            "arguments": call.arguments,
                        },
                        agent_name=agent_name,
                    )
                )
                if hook_ctx.should_abort:
                    return ToolResult(
                        call_id=call.id,
                        status="aborted",
                        content=(
                            "被 Hook 中止："
                            f"{hook_ctx.data.get('abort_reason', '')}"
                        ),
                    )
                tool_event = {
                    "name": call.name,
                    "call_id": call.id,
                    "agent_name": agent_name,
                    "worker_backend": "independent",
                }
                await observed_event(
                    RuntimeEventType.TOOL_START.value,
                    tool_event,
                )
                try:
                    result = await self._engine.execute_tool(
                        call,
                        on_event=observed_event,
                        agent_name=agent_name,
                    )
                except BaseException:
                    await observed_event(
                        RuntimeEventType.TOOL_ERROR.value,
                        tool_event,
                    )
                    raise
                await observed_event(
                    RuntimeEventType.TOOL_END.value,
                    {
                        **tool_event,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
                    },
                )
                await self._hooks.fire(
                    HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": call.name,
                            "agent": agent_name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "permission_bubble": True,
                            "worker_backend": "independent",
                        },
                        agent_name=agent_name,
                    )
                )
                return result

            tool_schemas: list[dict[str, object]] = []
            for tool_name in request.tool_scope:
                tool = self._engine.tool_registry.get(tool_name)
                if tool is None:
                    raise AgentWorkerProcessError(
                        "Agent Worker request scope 中的工具已不再注册。"
                    )
                tool_schemas.append(tool.to_openai_tool())

            outcome = await process.execute_bound_agent_job(
                tool_schemas=tool_schemas,
                tool_executor=(
                    execute_authoritatively if request.tool_scope else None
                ),
                on_running=lambda: self._sync_independent_agent_job(
                    task_id,
                    job_id,
                ),
            )
            async with self._execution_lock:
                execution = self._active_executions.get(task_id)
                if execution is None:
                    raise AgentJobError("独立 Agent Worker 活动执行已消失。")
                execution.worker_job_state = outcome.transition.job.state.value
                execution.worker_claim_epoch = outcome.transition.job.claim_epoch
                execution.worker_result = outcome.result
                # Keep the public phase inside the Agent Control closed set until
                # _finish_execution publishes the terminal "finished" record.
                execution.phase = "running"
                execution.last_updated_mono = time.monotonic()
            terminal = await self._agent_job_store.recover_terminal_payload(
                job_id,
                expected_result_sha256=outcome.result.result_sha256,
            )
            result = AgentResult(
                status=outcome.result.status.value,
                response=terminal.response,
                total_tokens=outcome.result.total_tokens,
                total_cost_usd=(
                    outcome.result.total_cost_microusd / 1_000_000
                ),
                turns=outcome.result.turns,
                error=terminal.error or None,
            )
            try:
                await self._deliver_execution_publication(execution)
            except Exception as exc:
                logger.warning(
                    "Independent AgentJob publication delivery failed [%s]: %s",
                    task_id,
                    type(exc).__name__,
                )
                await self._set_agent_job_failure(
                    task_id,
                    "agent_job_publication_delivery_failed",
                )
                self._wake_publication_recovery()
            return result
        except asyncio.CancelledError:
            await self._sync_independent_agent_job(
                task_id,
                job_id,
                failure_code="agent_worker_interrupted_recovery_required",
            )
            raise
        except Exception:
            await self._sync_independent_agent_job(
                task_id,
                job_id,
                failure_code="agent_worker_execution_failed",
            )
            raise
        finally:
            if (
                process.snapshot().state is AgentWorkerProcessState.RUNNING
                and process.snapshot().bound_job_id
            ):
                try:
                    await process.release_job()
                except Exception as exc:
                    logger.warning(
                        "Independent Agent Worker pre-start release failed [%s]: %s",
                        task_id,
                        type(exc).__name__,
                    )
            await self._sync_independent_agent_job(task_id, job_id)
            if process.snapshot().state is AgentWorkerProcessState.RUNNING:
                try:
                    await process.close()
                except Exception as exc:
                    logger.warning(
                        "Independent Agent Worker shutdown failed [%s]: %s",
                        task_id,
                        type(exc).__name__,
                    )

    async def _sync_independent_agent_job(
        self,
        task_id: str,
        job_id: str,
        *,
        failure_code: str = "",
    ) -> None:
        try:
            stored = await self._agent_job_store.get(job_id)
        except Exception as exc:
            logger.warning(
                "Independent AgentJob state sync failed [%s]: %s",
                task_id,
                type(exc).__name__,
            )
            return
        if stored is None:
            return
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                return
            execution.worker_job_state = stored.state.value
            execution.worker_claim_epoch = stored.claim_epoch
            if failure_code:
                execution.worker_job_failure_code = failure_code
            execution.last_updated_mono = time.monotonic()

    async def _agent_job_renewal_loop(
        self,
        task_id: str,
        *,
        job_id: str,
        claim_epoch: int,
    ) -> None:
        while True:
            await asyncio.sleep(_AGENT_JOB_RENEW_INTERVAL_SECONDS)
            try:
                renewed = await self._agent_job_store.renew_claim(
                    job_id,
                    owner_id=self._agent_job_owner_id,
                    claim_epoch=claim_epoch,
                    lease_seconds=_AGENT_JOB_LEASE_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "AgentJob claim renewal failed [%s]: %s",
                    task_id,
                    type(exc).__name__,
                )
                execute_task: asyncio.Task[AgentResult] | None = None
                async with self._execution_lock:
                    execution = self._active_executions.get(task_id)
                    if execution is not None:
                        execution.worker_job_failure_code = (
                            "agent_job_claim_renewal_failed"
                        )
                        execution.stop_reason = (
                            "Agent 持久执行 claim 续期失败，"
                            "已在下一安全边界停止模型执行。"
                        )
                        execution.status = "stopping"
                        execution.phase = "stopping"
                        execution.last_updated_mono = time.monotonic()
                        execute_task = execution.execute_task
                if execute_task is not None and not execute_task.done():
                    execute_task.cancel()
                return
            async with self._execution_lock:
                execution = self._active_executions.get(task_id)
                if execution is None:
                    return
                execution.worker_job_state = renewed.job.state.value
                execution.last_updated_mono = time.monotonic()

    async def _set_agent_job_failure(
        self,
        task_id: str,
        code: str,
    ) -> None:
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is not None:
                execution.worker_job_failure_code = code
                execution.last_updated_mono = time.monotonic()

    async def _attach_execution_task(
        self,
        task_id: str,
        execute_task: asyncio.Task[AgentResult],
    ) -> None:
        should_cancel = False
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is not None:
                execution.execute_task = execute_task
                execution.phase = "running"
                execution.last_updated_mono = time.monotonic()
                should_cancel = (
                    execution.stop_requested
                    or bool(execution.worker_job_failure_code)
                )
        if should_cancel and not execute_task.done():
            execute_task.cancel()

    async def _agent_job_preflight_failure(self, task_id: str) -> str:
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                return "Agent 持久执行已不再处于活动状态。"
            if execution.worker_job_failure_code:
                return (
                    execution.stop_reason
                    or "Agent 持久执行 fence 已失效，模型尚未调用。"
                )
            return ""

    async def _start_execution_heartbeat(self, task_id: str) -> None:
        factory = self._heartbeat_factory
        if factory is None:
            return
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                return
            session_id = execution.session_id
            agent_name = execution.agent_name
        try:
            lifecycle = await factory.create(
                session_id=session_id,
                task_id=task_id,
                agent_name=agent_name,
            )
            await lifecycle.start()
        except Exception as exc:
            logger.warning(
                "Agent heartbeat startup failed [%s]: %s",
                task_id,
                type(exc).__name__,
            )
            async with self._execution_lock:
                execution = self._active_executions.get(task_id)
                if execution is not None:
                    execution.heartbeat_failure_code = (
                        "agent_heartbeat_start_failed"
                    )
            return
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is not None:
                execution.heartbeat_lifecycle = lifecycle
                return
        try:
            await lifecycle.finish("cancelled")
        except Exception:
            logger.warning(
                "Detached Agent heartbeat terminal write failed [%s]",
                task_id,
            )

    async def _observe_execution_event(
        self,
        task_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        async with self._execution_lock:
            execution = self._active_executions.get(task_id)
            if execution is None:
                return
            execution.last_updated_mono = time.monotonic()
            tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
            tool_finished = event in {
                RuntimeEventType.TOOL_END.value,
                RuntimeEventType.TOOL_ERROR.value,
            }
            if event.startswith("tool_prepare"):
                execution.phase = "preparing_tool"
            elif event == RuntimeEventType.TOOL_START.value:
                execution.phase = "running_tool"
            elif tool_finished:
                execution.phase = "running"
                execution.current_tool = ""
            if tool_name:
                if not tool_finished:
                    execution.current_tool = tool_name
                if not execution.recent_tools or execution.recent_tools[-1] != tool_name:
                    execution.recent_tools.append(tool_name)
                    execution.recent_tools = execution.recent_tools[-20:]

    async def _finish_execution(
        self,
        task_id: str,
        result: AgentResult,
    ) -> AgentResult:
        async with self._execution_lock:
            execution = self._active_executions.pop(task_id, None)
            if execution is None:
                return result

        renewal = execution.worker_job_renewal_task
        if renewal is not None and not renewal.done():
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

        effective_result = result
        if (
            execution.worker_job_id
            and execution.worker_job_state == AgentJobState.ADMITTED.value
        ):
            try:
                cancelled = await self._agent_job_store.cancel_before_claim(
                    execution.worker_job_id,
                )
                execution.worker_job_state = cancelled.job.state.value
            except Exception as exc:
                logger.warning(
                    "AgentJob waiting cleanup failed [%s]: %s",
                    task_id,
                    type(exc).__name__,
                )
                execution.worker_job_failure_code = (
                    "agent_job_waiting_cleanup_failed"
                )
                effective_result = AgentResult(
                    status="error",
                    error=(
                        "Agent 持久等待任务清理失败；"
                        "已停止本地执行，请在 Agent Control 中检查恢复状态。"
                    ),
                )
        independent_recovery_required = (
            execution.worker_backend == "independent"
            and execution.worker_job_state == AgentJobState.RUNNING.value
        )
        if execution.worker_result is None and not independent_recovery_required:
            try:
                execution.worker_result = issue_agent_worker_result(
                    request=execution.worker_request,
                    status=effective_result.status,
                    response=effective_result.response,
                    error=effective_result.error,
                    total_tokens=effective_result.total_tokens,
                    total_cost_usd=effective_result.total_cost_usd,
                    turns=effective_result.turns,
                    completed_at=datetime.now(UTC).isoformat(),
                )
            except (TypeError, ValueError) as exc:
                execution.worker_contract_failure_code = "agent_worker_result_invalid"
                logger.warning(
                    "Agent Worker terminal contract rejected [%s]: %s",
                    task_id,
                    type(exc).__name__,
                )

        if (
            execution.worker_job_id
            and execution.worker_job_state == AgentJobState.RUNNING.value
            and execution.worker_backend == "embedded"
        ):
            if execution.worker_result is None:
                execution.worker_job_failure_code = (
                    "agent_job_terminal_receipt_invalid"
                )
                effective_result = _isolated_agent_job_result(result)
            else:
                terminal_transition = None
                for attempt in range(2):
                    try:
                        terminal_transition = await self._agent_job_store.finish(
                            execution.worker_job_id,
                            owner_id=self._agent_job_owner_id,
                            claim_epoch=execution.worker_claim_epoch,
                            result=execution.worker_result,
                            terminal_payload=AgentJobTerminalPayload(
                                response=effective_result.response,
                                error=effective_result.error or "",
                            ),
                        )
                        break
                    except Exception as exc:
                        logger.warning(
                            "AgentJob terminal commit failed [%s, attempt=%d]: %s",
                            task_id,
                            attempt + 1,
                            type(exc).__name__,
                        )
                if terminal_transition is None:
                    execution.worker_job_failure_code = (
                        "agent_job_terminal_commit_failed"
                    )
                    effective_result = _isolated_agent_job_result(result)
                else:
                    execution.worker_job_state = (
                        terminal_transition.job.state.value
                    )
                    recovered_payload = None
                    for attempt in range(2):
                        try:
                            recovered_payload = await (
                                self._agent_job_store.recover_terminal_payload(
                                    execution.worker_job_id,
                                    expected_result_sha256=(
                                        execution.worker_result.result_sha256
                                    ),
                                )
                            )
                            break
                        except Exception as exc:
                            logger.warning(
                                "AgentJob terminal payload recovery failed "
                                "[%s, attempt=%d]: %s",
                                task_id,
                                attempt + 1,
                                type(exc).__name__,
                            )
                    if recovered_payload is None:
                        execution.worker_job_failure_code = (
                            "agent_job_terminal_payload_recovery_failed"
                        )
                        effective_result = (
                            _isolated_agent_terminal_payload_result(result)
                        )
                    else:
                        effective_result = AgentResult(
                            status=effective_result.status,
                            response=recovered_payload.response,
                            total_tokens=effective_result.total_tokens,
                            total_cost_usd=effective_result.total_cost_usd,
                            turns=effective_result.turns,
                            error=recovered_payload.error or None,
                        )
                        try:
                            await self._deliver_execution_publication(
                                execution,
                            )
                        except Exception as exc:
                            logger.warning(
                                "AgentJob publication delivery failed [%s]: %s",
                                task_id,
                                type(exc).__name__,
                            )
                            execution.worker_job_failure_code = (
                                "agent_job_publication_delivery_failed"
                            )
                            self._wake_publication_recovery()

        lifecycle = execution.heartbeat_lifecycle
        if lifecycle is not None:
            try:
                await lifecycle.finish(effective_result.status)
            except Exception as exc:
                logger.warning(
                    "Agent heartbeat terminal write failed [%s]: %s",
                    task_id,
                    type(exc).__name__,
                )
                execution.heartbeat_failure_code = (
                    "agent_heartbeat_terminal_failed"
                )

        async with self._execution_lock:
            record = _execution_record(
                execution,
                now=time.monotonic(),
                result=effective_result,
                finished_at=time.time(),
            )
            self._execution_history.append(record)
            self._execution_history = self._execution_history[-100:]
            if any(
                item.agent_name == execution.agent_name
                for item in self._active_executions.values()
            ):
                agent_lifecycle = self._lifecycle.get(execution.agent_name)
                if agent_lifecycle is not None:
                    agent_lifecycle.state = AgentState.RUNNING
                    agent_lifecycle.last_updated = time.monotonic()
                    agent_lifecycle.idle_since = None
            else:
                self._transition(execution.agent_name, AgentState.IDLE)
        return effective_result

    async def delegate(
        self,
        task: SubTask,
        extra_context: str = "",
        event_callback: LegacyEventCallback | None = None,
    ) -> AgentResult:
        """Admit every direct delegation through the shared process-local limit."""
        stack = _AGENT_ADMISSION_STACK.get()
        if id(self) in stack and self._parallel_agent_slots.locked():
            return AgentResult(
                status="error",
                error=(
                    "Agent 并发容量已满；嵌套委派会形成自等待，已安全拒绝。"
                ),
            )
        if (
            self._admitted_parallel_agents + self._queued_parallel_agents
            >= self._max_parallel_agents + self._max_queued_agents
        ):
            result = self._queue_full_result()
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=task.agent_name or "",
                description=task.description,
                message=result.error,
            )
            return result

        self._queued_parallel_agents += 1
        acquired = False
        try:
            await self._parallel_agent_slots.acquire()
            acquired = True
            self._queued_parallel_agents -= 1
            self._admitted_parallel_agents += 1
            return await self._run_admitted_delegation(
                task,
                extra_context=extra_context,
                event_callback=event_callback,
            )
        finally:
            if acquired:
                self._admitted_parallel_agents -= 1
                self._parallel_agent_slots.release()
            else:
                self._queued_parallel_agents -= 1

    def _queue_full_result(self) -> AgentResult:
        return AgentResult(
            status="error",
            error=(
                "Agent 等待队列已满"
                f"（上限 {self._max_queued_agents}）；请等待现有任务完成后重试。"
            ),
        )

    async def _run_admitted_delegation(
        self,
        task: SubTask,
        *,
        extra_context: str = "",
        event_callback: LegacyEventCallback | None = None,
    ) -> AgentResult:
        """Run one delegation whose caller already owns a capacity slot."""
        stack = _AGENT_ADMISSION_STACK.get()
        token = _AGENT_ADMISSION_STACK.set((*stack, id(self)))
        try:
            return await self._delegate_admitted(
                task,
                extra_context=extra_context,
                event_callback=event_callback,
            )
        finally:
            _AGENT_ADMISSION_STACK.reset(token)

    async def _delegate_admitted(
        self,
        task: SubTask,
        extra_context: str = "",
        event_callback: LegacyEventCallback | None = None,
    ) -> AgentResult:
        """将子任务委派给合适的 Agent."""
        agent_name = task.agent_name or self.select_agent(task.description)
        if not agent_name:
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name="",
                description=task.description,
                message="没有找到合适的子 Agent。",
            )
            return AgentResult(
                status="error",
                error=f"No suitable agent for: {task.description[:100]}",
            )

        agent = self.get_agent(agent_name)
        if not agent:
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=f"Agent {agent_name} 不存在。",
            )
            return AgentResult(status="error", error=f"Agent not found: {agent_name}")

        independent_worker = bool(
            self._agent_worker_process_factory is not None
            and self._agent_worker_process_factory.model_execution_enabled
        )

        context_parts = []
        if task.context:
            context_parts.append(task.context)
        if extra_context:
            context_parts.append(extra_context)

        # Inject blackboard state into context if available
        blackboard = await self.message_bus.blackboard_get_all()
        if blackboard:
            bb_lines = ["## 共享状态 (Blackboard)"]
            for key, entry in blackboard.items():
                val_str = (
                    str(entry.value)[:200] if entry.value is not None else "None"
                )
                bb_lines.append(
                    f"- **{key}** (by {entry.author}, v{entry.version}): "
                    f"{val_str}"
                )
            context_parts.append("\n".join(bb_lines))

        # Inject pending messages for this agent
        pending = await self.message_bus.receive(agent_name)
        if pending:
            msg_lines = [f"## 待处理消息 ({len(pending)} 条)"]
            for msg in pending:
                if msg.priority == "critical":
                    prefix = "🔴"
                elif msg.priority == "high":
                    prefix = "🟡"
                else:
                    prefix = "📨"
                msg_lines.append(
                    f"{prefix} **来自 {msg.sender}** [{msg.topic}]: "
                    f"{msg.content[:300]}"
                )
            context_parts.append("\n".join(msg_lines))

        context = "\n\n".join(context_parts) if context_parts else ""
        worker_context = (
            _agent_model_system_context(agent, context)
            if independent_worker
            else context
        )

        try:
            worker_request = issue_agent_worker_request(
                task_id=task.id,
                session_id=str(
                    getattr(getattr(self._engine, "_session", None), "id", "") or ""
                ),
                agent_name=agent_name,
                task=task.description,
                context=worker_context,
                tool_scope=agent.tool_names,
                permission_mode=agent.config.permission_level,
                model_tier=agent.config.model_tier,
                max_turns=agent.config.max_turns,
                max_budget_usd=agent.config.max_budget_usd,
                timeout_seconds=_agent_timeout_seconds(agent),
                message_topic=f"task.{task.id}.completed",
                issued_at=datetime.now(UTC).isoformat(),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Agent Worker request contract rejected [%s]: %s",
                task.id,
                type(exc).__name__,
            )
            result = AgentResult(
                status="error",
                error="Agent Worker 请求合同无效，已在模型调用前安全拒绝。",
            )
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result

        if not await self._register_execution(
            task,
            agent_name,
            worker_request,
            worker_backend=("independent" if independent_worker else "embedded"),
        ):
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=f"任务 ID {task.id} 已有正在运行的 Agent 执行。",
            )
            return AgentResult(
                status="error",
                error=f"Duplicate active sub-agent task id: {task.id}",
            )

        job_payload = AgentJobPayload(
            task_id=task.id,
            session_id=str(
                getattr(getattr(self._engine, "_session", None), "id", "") or ""
            ),
            task=task.description,
            context=worker_context,
            message_topic=f"task.{task.id}.completed",
        )
        try:
            if independent_worker:
                await self._admit_independent_agent_job(
                    task.id,
                    request=worker_request,
                    payload=job_payload,
                )
            else:
                await self._admit_and_claim_agent_job(
                    task.id,
                    request=worker_request,
                    payload=job_payload,
                )
        except asyncio.CancelledError:
            result = AgentResult(
                status="cancelled",
                error="父运行已取消等待中的 Agent 执行。",
            )
            await self._finish_execution(task.id, result)
            raise
        except _AgentCapacityWaitCancelledError as exc:
            result = AgentResult(
                status="cancelled",
                error=str(exc),
            )
            result = await self._finish_execution(task.id, result)
            await self._emit_subagent_event(
                event_callback,
                status="cancelled",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result
        except AgentJobCapacityExhaustedError:
            await self._set_agent_job_failure(
                task.id,
                "agent_job_capacity_exhausted",
            )
            result = AgentResult(
                status="error",
                error=(
                    "Agent 共享持久等待队列已满"
                    f"（上限 {self._max_queued_agents}）；"
                    "请等待其他 Runtime 的 Agent 任务完成后重试。"
                ),
            )
            result = await self._finish_execution(task.id, result)
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result
        except AgentJobKeyUnavailableError:
            await self._set_agent_job_failure(
                task.id,
                "agent_job_key_unavailable",
            )
            result = AgentResult(
                status="error",
                error=(
                    "Agent 持久执行密钥尚未就绪；请先运行 "
                    "`naumi runtime-key init`，或在自动化环境注入 "
                    "NAUMI_RUNTIME_PAYLOAD_KEY。模型尚未调用。"
                ),
            )
            result = await self._finish_execution(task.id, result)
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result
        except Exception as exc:
            logger.warning(
                "AgentJob admission failed [%s]: %s",
                task.id,
                type(exc).__name__,
            )
            await self._set_agent_job_failure(
                task.id,
                "agent_job_admission_failed",
            )
            result = AgentResult(
                status="error",
                error="Agent 持久执行 admission 失败，已在模型调用前安全拒绝。",
            )
            result = await self._finish_execution(task.id, result)
            await self._emit_subagent_event(
                event_callback,
                status="failed",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result

        if not independent_worker:
            try:
                await self._mark_agent_job_running(task.id)
            except Exception as exc:
                logger.warning(
                    "AgentJob start fence failed [%s]: %s",
                    task.id,
                    type(exc).__name__,
                )
                await self._set_agent_job_failure(
                    task.id,
                    "agent_job_start_fenced",
                )
                result = AgentResult(
                    status="error",
                    error="Agent 持久执行 start fence 失败，模型尚未调用。",
                )
                result = await self._finish_execution(task.id, result)
                await self._emit_subagent_event(
                    event_callback,
                    status="failed",
                    task_id=task.id,
                    agent_name=agent_name,
                    description=task.description,
                    message=result.error,
                )
                return result

        await self._start_execution_heartbeat(task.id)
        preflight_failure = await self._agent_job_preflight_failure(task.id)
        if preflight_failure:
            result = AgentResult(
                status="cancelled",
                error=preflight_failure,
            )
            result = await self._finish_execution(task.id, result)
            await self._emit_subagent_event(
                event_callback,
                status=result.status,
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=result.error,
            )
            return result

        logger.info("Delegating task %s to agent %s", task.id, agent_name)
        self._ensure_lifecycle(agent_name)
        self._transition(agent_name, AgentState.RUNNING)
        try:
            await self._emit_subagent_event(
                event_callback,
                status="started",
                task_id=task.id,
                agent_name=agent_name,
                description=task.description,
                message=(
                    "独立 Agent Worker 已开始执行。"
                    if independent_worker
                    else "子 Agent 已开始执行。"
                ),
            )
        except BaseException as exc:
            startup_result = AgentResult(
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._finish_execution(task.id, startup_result)
            raise

        result: AgentResult
        terminal_result: AgentResult | None = None
        try:
            await self._hooks.fire(HookContext(
                point=HookPoint.DELEGATE_START,
                data={
                    "task_id": task.id,
                    "agent_name": agent_name,
                    "description": task.description,
                    "worker_request_sha256": worker_request.request_sha256,
                },
                agent_name=agent_name,
            ))
            await self._hooks.fire(HookContext(
                point=HookPoint.AGENT_EXECUTE_START,
                data={"task_id": task.id, "agent_name": agent_name, "task": task.description},
                agent_name=agent_name,
            ))
            execute_kwargs: dict[str, Any] = {
                "task": task.description,
                "context": context,
            }
            if "event_callback" in signature(agent.execute).parameters:
                async def observed_event(
                    event: str,
                    data: dict[str, Any],
                ) -> None:
                    try:
                        event_type = RuntimeEventType(event)
                    except ValueError as exc:
                        raise ValueError(f"未知 Runtime 事件：{event}") from exc
                    await self._observe_execution_event(
                        task.id,
                        event_type.value,
                        data,
                    )
                    if event_callback is not None:
                        await event_callback(event_type.value, data)

                execute_kwargs["event_callback"] = observed_event
            preflight_failure = await self._agent_job_preflight_failure(task.id)
            if preflight_failure:
                result = AgentResult(
                    status="cancelled",
                    error=preflight_failure,
                )
            else:
                execution_operation = (
                    self._execute_independent_agent_job(
                        task_id=task.id,
                        agent=agent,
                        event_callback=event_callback,
                    )
                    if independent_worker
                    else agent.execute(**execute_kwargs)
                )
                execute_task = asyncio.create_task(execution_operation)
                await self._attach_execution_task(task.id, execute_task)
                timeout_seconds = _agent_timeout_seconds(agent)
                if (
                    not independent_worker
                    and timeout_seconds > 0
                    and math.isfinite(timeout_seconds)
                ):
                    result = await asyncio.wait_for(
                        execute_task,
                        timeout=timeout_seconds,
                    )
                else:
                    result = await execute_task
            terminal_result = result
            await self._hooks.fire(HookContext(
                point=HookPoint.AGENT_EXECUTE_END,
                data={
                    "task_id": task.id,
                    "agent_name": agent_name,
                    "status": result.status,
                    "tokens": result.total_tokens,
                    "cost": result.total_cost_usd,
                },
                agent_name=agent_name,
            ))
        except asyncio.CancelledError:
            parent_task = asyncio.current_task()
            if parent_task is not None and parent_task.cancelling():
                terminal_result = AgentResult(
                    status="cancelled",
                    error="父运行已取消。",
                )
                raise
            execution = self._active_executions.get(task.id)
            reason = (
                execution.stop_reason
                if execution is not None and execution.stop_reason
                else "用户请求停止子 Agent。"
            )
            result = AgentResult(status="cancelled", error=reason)
            terminal_result = result
        except TimeoutError:
            timeout_seconds = _agent_timeout_seconds(agent)
            logger.warning(
                "Agent %s timed out while executing task %s after %.2fs",
                agent_name,
                task.id,
                timeout_seconds,
            )
            result = AgentResult(
                status="timeout",
                error=f"子 Agent 执行超时：超过 {timeout_seconds:g} 秒未完成。",
            )
            terminal_result = result
            await self._hooks.fire(HookContext(
                point=HookPoint.AGENT_EXECUTE_END,
                data={
                    "task_id": task.id,
                    "agent_name": agent_name,
                    "status": result.status,
                    "tokens": result.total_tokens,
                    "cost": result.total_cost_usd,
                    "error": result.error,
                },
                agent_name=agent_name,
            ))
        except Exception as exc:
            logger.exception("Agent %s failed while executing task %s", agent_name, task.id)
            result = AgentResult(status="error", error=f"{type(exc).__name__}: {exc}")
            terminal_result = result
            await self._hooks.fire(HookContext(
                point=HookPoint.AGENT_EXECUTE_END,
                data={
                    "task_id": task.id,
                    "agent_name": agent_name,
                    "status": result.status,
                    "tokens": result.total_tokens,
                    "cost": result.total_cost_usd,
                    "error": result.error,
                },
                agent_name=agent_name,
            ))
        finally:
            if terminal_result is not None:
                result = await self._finish_execution(
                    task.id,
                    terminal_result,
                )

        await self._emit_subagent_event(
            event_callback,
            status=result.status,
            task_id=task.id,
            agent_name=agent_name,
            description=task.description,
            message=_agent_result_message(result),
            tokens=result.total_tokens,
            cost=result.total_cost_usd,
        )

        await self._hooks.fire(HookContext(
            point=HookPoint.DELEGATE_END,
            data={
                "task_id": task.id,
                "agent_name": agent_name,
                "status": result.status,
                "tokens": result.total_tokens,
            },
            agent_name=agent_name,
        ))

        return result

    async def _deliver_execution_publication(
        self,
        execution: _ActiveExecution,
    ) -> AgentJobPublicationDeliveryTransition:
        publication = await self._agent_job_store.get_job_publication(
            execution.worker_job_id,
        )
        if publication is None:
            raise AgentJobError("AgentJob terminal publication 不存在。")
        claimed = await self._agent_job_store.claim_publication(
            publication.publication_id,
            owner_id=self._agent_publication_owner_id,
            lease_seconds=_AGENT_PUBLICATION_LEASE_SECONDS,
        )
        try:
            content = await self._agent_job_store.recover_publication_content(
                publication.publication_id,
                owner_id=self._agent_publication_owner_id,
                claim_epoch=claimed.publication.claim_epoch,
            )
            _validate_execution_publication(execution, content)
            transition = (
                await self._agent_job_store.deliver_publication_to_inbox(
                    publication.publication_id,
                    owner_id=self._agent_publication_owner_id,
                    claim_epoch=claimed.publication.claim_epoch,
                )
            )
        except Exception:
            await self._release_publication_after_failure(
                publication.publication_id,
                claim_epoch=claimed.publication.claim_epoch,
            )
            raise
        await self._publish_publication_notification(content, transition)
        return transition

    async def _release_publication_after_failure(
        self,
        publication_id: str,
        *,
        claim_epoch: int,
    ) -> None:
        try:
            await self._agent_job_store.release_publication_claim(
                publication_id,
                owner_id=self._agent_publication_owner_id,
                claim_epoch=claim_epoch,
            )
        except Exception as exc:
            logger.warning(
                "Agent publication claim release failed [%s]: %s",
                publication_id,
                type(exc).__name__,
            )

    def _wake_publication_recovery(self) -> bool:
        wake = self._publication_recovery_wake
        if wake is None:
            return False
        try:
            return bool(wake())
        except Exception as exc:
            logger.warning(
                "Agent publication worker wake failed: %s",
                type(exc).__name__,
            )
            return False

    async def _publish_publication_notification(
        self,
        content: AgentJobPublicationContent,
        transition: AgentJobPublicationDeliveryTransition,
    ) -> bool:
        """Best-effort wake-up only; the durable inbox is the ACK boundary."""
        result = content.result
        delivery = transition.delivery
        try:
            await self.message_bus.publish(AgentMessage(
                sender=content.request.agent_name,
                topic=content.payload.message_topic,
                content=content.terminal_payload.response[:2000],
                metadata={
                    "task_id": content.payload.task_id,
                    "status": result.status.value,
                    "tokens": result.total_tokens,
                    "cost": result.total_cost_microusd / 1_000_000,
                    "publication_id": delivery.publication_id,
                    "delivery_id": delivery.delivery_id,
                    "delivery_sha256": delivery.delivery_sha256,
                    "result_sha256": delivery.result_sha256,
                    "durable_inbox": True,
                },
            ))
            return True
        except Exception as exc:
            logger.warning(
                "Agent publication notification failed [%s]: %s",
                delivery.publication_id,
                type(exc).__name__,
            )
            return False

    async def _emit_subagent_event(
        self,
        callback: LegacyEventCallback | None,
        *,
        status: str,
        task_id: str,
        agent_name: str,
        description: str,
        message: str,
        tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        payload = {
            "status": status,
            "task_id": task_id,
            "agent_name": agent_name,
            "description": description,
            "message": message,
            "tokens": tokens,
            "cost": cost,
            "timestamp": time.time(),
        }
        self._event_history.append(payload)
        if len(self._event_history) > 100:
            self._event_history = self._event_history[-100:]
        await _emit_subagent_event_payload(callback, payload)

    async def execute_sequential(
        self,
        tasks: list[SubTask],
        accumulate_context: bool = True,
    ) -> list[AgentResult]:
        """顺序执行子任务（管道模式）."""
        results: list[AgentResult] = []
        accumulated = ""

        for task in tasks:
            result = await self.delegate(task, extra_context=accumulated)
            results.append(result)

            if accumulate_context and result.status == "completed":
                accumulated += f"\n\n## {task.description}\n{result.response[:2000]}"

            if result.status == "error":
                logger.warning("Task %s failed: %s", task.id, result.error)

        return results

    async def execute_parallel(self, tasks: list[SubTask]) -> list[AgentResult]:
        """Execute independent tasks through the same bounded admission gate."""
        if not tasks:
            return []
        results: list[AgentResult | None] = [None] * len(tasks)
        total_limit = self._max_parallel_agents + self._max_queued_agents
        outstanding = (
            self._admitted_parallel_agents + self._queued_parallel_agents
        )
        accepted_count = min(len(tasks), max(0, total_limit - outstanding))
        for index in range(accepted_count, len(tasks)):
            results[index] = self._queue_full_result()

        async def run_one(index: int) -> None:
            try:
                results[index] = await self.delegate(tasks[index])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results[index] = AgentResult(
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )

        async with asyncio.TaskGroup() as group:
            for index in range(accepted_count):
                group.create_task(run_one(index))

        if any(result is None for result in results):
            raise RuntimeError("Agent 集群调度结束时存在未完成任务。")
        return [result for result in results if result is not None]

    async def execute_dag(self, tasks: list[SubTask]) -> dict[str, AgentResult]:
        """按 DAG 依赖关系执行任务.

        同一层级的无依赖任务并行执行，层级间顺序执行。
        """
        results: dict[str, AgentResult] = {}
        remaining = list(tasks)

        max_iterations = len(tasks) + 5
        iteration = 0

        while remaining and iteration < max_iterations:
            iteration += 1

            blocked: list[SubTask] = []
            for task in remaining:
                failed_deps = [
                    dep_id
                    for dep_id in (task.depends_on or [])
                    if dep_id in results and results[dep_id].status != "completed"
                ]
                if failed_deps:
                    results[task.id] = AgentResult(
                        status="error",
                        error=f"Failed dependencies: {failed_deps}",
                    )
                    blocked.append(task)

            for task in blocked:
                remaining.remove(task)

            # 找到所有依赖已成功完成的任务
            ready = [
                t
                for t in remaining
                if all(
                    dep in results and results[dep].status == "completed"
                    for dep in (t.depends_on or [])
                )
            ]

            if not ready:
                # 死锁：所有剩余任务都有未满足的依赖
                for t in remaining:
                    results[t.id] = AgentResult(
                        status="error",
                        error=f"Unresolved dependencies: {t.depends_on}",
                    )
                break

            # 构建上下文
            for task in ready:
                dep_contexts = []
                for dep_id in task.depends_on or []:
                    dep_result = results.get(dep_id)
                    if dep_result and dep_result.status == "completed":
                        dep_contexts.append(f"## 前置任务 {dep_id}\n{dep_result.response[:1000]}")
                if dep_contexts:
                    object.__setattr__(task, "context", "\n\n".join(dep_contexts))

            # 并行执行就绪任务
            batch_results = await self.execute_parallel(ready)

            for task, result in zip(ready, batch_results):
                results[task.id] = result
                remaining.remove(task)

        return results

    async def execute_review_loop(
        self,
        task: str,
        *,
        max_rounds: int = 3,
        coder_agent: str = "coder",
    ) -> AgentResult:
        """代码编写 + 审查循环模式."""
        accumulated_feedback = ""

        for round_num in range(max_rounds):
            # 编写/修改代码
            coder = self.get_agent(coder_agent)
            if not coder:
                return AgentResult(status="error", error=f"Agent not found: {coder_agent}")

            task_with_feedback = task
            if accumulated_feedback:
                task_with_feedback += (
                    f"\n\n## 审查反馈（第 {round_num} 轮）\n{accumulated_feedback}"
                )

            result = await coder.execute(task=task_with_feedback)

            if result.status != "completed":
                return result

            # 审查
            review_prompt = (
                f"审查以下代码变更：\n\n{result.response}\n\n"
                f"原始需求：{task}\n\n"
                "请检查：\n"
                "1. 功能正确性\n"
                "2. 代码质量\n"
                "3. 边界情况\n\n"
                "如果一切良好，回复 APPROVED。\n"
                "如果有问题，给出具体修改建议。"
            )

            review = await self._engine.router.call(
                messages=[{"role": "user", "content": review_prompt}],
                tier="capable",
                max_tokens=1000,
            )

            if "APPROVED" in review.content:
                return result

            accumulated_feedback = review.content

        # 达到最大轮次，返回最后结果
        return result

    def list_agents(self) -> list[dict[str, str]]:
        """列出可用的 Agent（含生命周期状态）."""
        result = []
        for config in self._configs.values():
            lc = self._lifecycle.get(config.name)
            entry: dict[str, str] = {
                "name": config.name,
                "description": config.description,
            }
            if lc:
                entry["state"] = lc.state.value
                entry["tasks"] = str(lc.task_count)
                age = time.monotonic() - (lc.spawned_at or lc.last_updated)
                entry["age_s"] = f"{age:.0f}"
                if lc.idle_since:
                    idle = time.monotonic() - lc.idle_since
                    entry["idle_s"] = f"{idle:.0f}"
            else:
                entry["state"] = "uninitialized"
            result.append(entry)
        return result

    def list_agent_configs(self) -> tuple[AgentConfig, ...]:
        """Return immutable Agent configs without instantiating idle presets."""
        return tuple(self._configs.values())

    def agent_tool_names(self, name: str) -> tuple[str, ...]:
        """Return effective registered tools without changing lifecycle state."""
        config = self._configs.get(name)
        if config is None:
            return ()
        return resolve_agent_tool_names(config, self._engine.tool_registry.names)


async def _emit_subagent_event(
    callback: LegacyEventCallback | None,
    *,
    status: str,
    task_id: str,
    agent_name: str,
    description: str,
    message: str,
    tokens: int = 0,
    cost: float = 0.0,
) -> None:
    await _emit_subagent_event_payload(callback, {
        "status": status,
        "task_id": task_id,
        "agent_name": agent_name,
        "description": description,
        "message": message,
        "tokens": tokens,
        "cost": cost,
    })


async def _emit_subagent_event_payload(
    callback: LegacyEventCallback | None,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    await callback(RuntimeEventType.SUBAGENT_EVENT.value, payload)


def _agent_timeout_seconds(agent: Any) -> float:
    config = getattr(agent, "config", None)
    timeout = getattr(config, "timeout_seconds", 300.0)
    return float(timeout) if isinstance(timeout, int | float) else 300.0


def _agent_model_system_context(agent: BaseAgent, context: str) -> str:
    """Build the exact system content that BaseAgent would send to the model."""
    parts: list[str] = []
    if context:
        parts.append(f"## 前置上下文\n{context}")
    if agent.config.system_prompt:
        parts.append(agent.config.system_prompt)
    return "\n\n".join(parts)


def _agent_result_message(result: AgentResult) -> str:
    if result.status == "completed":
        return "子 Agent 已完成任务。"
    return result.error or result.response[:300] or "子 Agent 未完成任务。"


def _isolated_agent_job_result(result: AgentResult) -> AgentResult:
    return AgentResult(
        status="error",
        response="",
        total_tokens=result.total_tokens,
        total_cost_usd=result.total_cost_usd,
        turns=result.turns,
        error=(
            "子 Agent 已生成结果，但持久终态提交失败；"
            "为避免展示未经认证的结果，已安全隔离。"
        ),
    )


def _isolated_agent_terminal_payload_result(
    result: AgentResult,
) -> AgentResult:
    return AgentResult(
        status="error",
        response="",
        total_tokens=result.total_tokens,
        total_cost_usd=result.total_cost_usd,
        turns=result.turns,
        error=(
            "子 Agent 终态已持久提交，但结果原文未能通过认证恢复；"
            "为避免展示不可信内容，已安全隔离。"
        ),
    )


def _validate_execution_publication(
    execution: _ActiveExecution,
    content: AgentJobPublicationContent,
) -> None:
    if content.request != execution.worker_request:
        raise AgentJobError(
            "AgentJob publication request 与当前执行不一致。"
        )
    if execution.worker_result is None or content.result != execution.worker_result:
        raise AgentJobError(
            "AgentJob publication result 与当前执行不一致。"
        )
    if (
        content.payload.task_id != execution.task_id
        or content.payload.session_id != execution.session_id
        or content.request.agent_name != execution.agent_name
        or content.payload.message_topic
        != f"task.{execution.task_id}.completed"
    ):
        raise AgentJobError(
            "AgentJob publication routing 与当前执行不一致。"
        )


def _execution_record(
    execution: _ActiveExecution,
    *,
    now: float,
    result: AgentResult | None = None,
    finished_at: float | None = None,
) -> AgentExecutionRecord:
    elapsed_ms = max(0, round((now - execution.started_mono) * 1000))
    heartbeat_age_ms = max(0, round((now - execution.last_updated_mono) * 1000))
    status = result.status if result is not None else execution.status
    heartbeat_subject_id = ""
    heartbeat_phase = ""
    heartbeat_failure_code = execution.heartbeat_failure_code
    if execution.heartbeat_lifecycle is not None:
        heartbeat = execution.heartbeat_lifecycle.snapshot()
        heartbeat_subject_id = heartbeat.subject_id
        heartbeat_phase = heartbeat.phase
        heartbeat_failure_code = heartbeat_failure_code or heartbeat.failure_code
    worker_result_sha256 = (
        execution.worker_result.result_sha256
        if execution.worker_result is not None
        else ""
    )
    return AgentExecutionRecord(
        task_id=execution.task_id,
        session_id=execution.session_id,
        agent_name=execution.agent_name,
        description=execution.description,
        status=status,
        phase="finished" if finished_at is not None else execution.phase,
        worker_backend=execution.worker_backend,
        started_at=execution.started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        heartbeat_age_ms=heartbeat_age_ms,
        heartbeat_subject_id=heartbeat_subject_id,
        heartbeat_phase=heartbeat_phase,
        heartbeat_failure_code=heartbeat_failure_code,
        worker_request_sha256=execution.worker_request.request_sha256,
        worker_result_sha256=worker_result_sha256,
        worker_tool_scope=execution.worker_request.tool_scope,
        worker_contract_failure_code=execution.worker_contract_failure_code,
        worker_job_id=execution.worker_job_id,
        worker_job_state=execution.worker_job_state,
        worker_claim_epoch=execution.worker_claim_epoch,
        worker_job_failure_code=execution.worker_job_failure_code,
        current_tool=execution.current_tool,
        recent_tools=tuple(execution.recent_tools),
        total_tokens=result.total_tokens if result is not None else 0,
        total_cost_usd=result.total_cost_usd if result is not None else 0.0,
        turns=result.turns if result is not None else 0,
        error=(result.error or "") if result is not None else "",
        stop_supported=finished_at is None,
        stop_requested=execution.stop_requested,
    )
