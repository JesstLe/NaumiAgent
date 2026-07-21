"""子 Agent 调度器 — 管理、选择、并行执行、生命周期."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import signature
from typing import TYPE_CHECKING, Any

from naumi_agent.agents.base import (
    AgentCapability,
    AgentConfig,
    AgentResult,
    BaseAgent,
    resolve_agent_tool_names,
)
from naumi_agent.agents.factory import DynamicAgentFactory
from naumi_agent.agents.message_bus import AgentMessageBus
from naumi_agent.agents.presets import ALL_AGENT_CONFIGS
from naumi_agent.hooks import HookContext, HookManager, HookPoint
from naumi_agent.runtime.ports.events import LegacyEventCallback, RuntimeEventType

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SECONDS = 300  # 5 minutes
_REAPER_INTERVAL_SECONDS = 30

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


@dataclass(frozen=True)
class StopExecutionResult:
    """Deterministic outcome of an execution stop request."""

    task_id: str
    accepted: bool
    code: str
    message: str


@dataclass
class _ActiveExecution:
    task_id: str
    session_id: str
    agent_name: str
    description: str
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


class SubAgentManager:
    """管理和调度子 Agent（含生命周期状态机 + 自动回收）."""

    def __init__(self, engine: AgentEngine) -> None:
        self._engine = engine
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
        self._parallel_agent_slots = asyncio.Semaphore(self._max_parallel_agents)
        self._queued_parallel_agents = 0

    # --- 生命周期状态机 ---

    @property
    def max_parallel_agents(self) -> int:
        return self._max_parallel_agents

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
    ) -> bool:
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
            )
            return True

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
                should_cancel = execution.stop_requested
        if should_cancel and not execute_task.done():
            execute_task.cancel()

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
    ) -> None:
        async with self._execution_lock:
            execution = self._active_executions.pop(task_id, None)
            if execution is None:
                return
            now_mono = time.monotonic()
            record = _execution_record(
                execution,
                now=now_mono,
                result=result,
                finished_at=time.time(),
            )
            self._execution_history.append(record)
            self._execution_history = self._execution_history[-100:]

            if any(
                item.agent_name == execution.agent_name
                for item in self._active_executions.values()
            ):
                lifecycle = self._lifecycle.get(execution.agent_name)
                if lifecycle is not None:
                    lifecycle.state = AgentState.RUNNING
                    lifecycle.last_updated = time.monotonic()
                    lifecycle.idle_since = None
            else:
                self._transition(execution.agent_name, AgentState.IDLE)

    async def delegate(
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

        if not await self._register_execution(task, agent_name):
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
                message="子 Agent 已开始执行。",
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
            execute_task = asyncio.create_task(agent.execute(**execute_kwargs))
            await self._attach_execution_task(task.id, execute_task)
            timeout_seconds = _agent_timeout_seconds(agent)
            if timeout_seconds > 0 and math.isfinite(timeout_seconds):
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
                await self._finish_execution(task.id, terminal_result)

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

        # Auto-publish completed results to the bus
        if result.status == "completed" and result.response:
            from naumi_agent.agents.message_bus import AgentMessage

            bus_msg = AgentMessage(
                sender=agent_name,
                topic=f"task.{task.id}.completed",
                content=result.response[:2000],
                metadata={
                    "task_id": task.id,
                    "tokens": result.total_tokens,
                    "cost": result.total_cost_usd,
                },
            )
            await self.message_bus.publish(bus_msg)

        return result

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
        """Execute independent tasks with bounded FIFO backpressure."""
        if not tasks:
            return []
        results: list[AgentResult | None] = [None] * len(tasks)
        next_index = 0
        queued_remaining = len(tasks)
        self._queued_parallel_agents += queued_remaining

        async def worker() -> None:
            nonlocal next_index
            nonlocal queued_remaining
            while next_index < len(tasks):
                index = next_index
                next_index += 1
                task = tasks[index]
                try:
                    async with self._parallel_agent_slots:
                        queued_remaining -= 1
                        self._queued_parallel_agents -= 1
                        results[index] = await self.delegate(task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    results[index] = AgentResult(
                        status="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )

        worker_count = min(self._max_parallel_agents, len(tasks))
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(worker_count):
                    group.create_task(worker())
        finally:
            if queued_remaining:
                self._queued_parallel_agents -= queued_remaining
                queued_remaining = 0

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


def _agent_result_message(result: AgentResult) -> str:
    if result.status == "completed":
        return "子 Agent 已完成任务。"
    return result.error or result.response[:300] or "子 Agent 未完成任务。"


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
    return AgentExecutionRecord(
        task_id=execution.task_id,
        session_id=execution.session_id,
        agent_name=execution.agent_name,
        description=execution.description,
        status=status,
        phase="finished" if finished_at is not None else execution.phase,
        started_at=execution.started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        heartbeat_age_ms=heartbeat_age_ms,
        current_tool=execution.current_tool,
        recent_tools=tuple(execution.recent_tools),
        total_tokens=result.total_tokens if result is not None else 0,
        total_cost_usd=result.total_cost_usd if result is not None else 0.0,
        turns=result.turns if result is not None else 0,
        error=(result.error or "") if result is not None else "",
        stop_supported=finished_at is None,
        stop_requested=execution.stop_requested,
    )
