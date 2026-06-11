"""子 Agent 调度器 — 管理、选择、并行执行、生命周期."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import signature
from typing import TYPE_CHECKING, Any

from naumi_agent.agents.base import AgentCapability, AgentConfig, AgentResult, BaseAgent
from naumi_agent.agents.factory import DynamicAgentFactory
from naumi_agent.agents.message_bus import AgentMessageBus
from naumi_agent.agents.presets import ALL_AGENT_CONFIGS
from naumi_agent.hooks import HookContext, HookManager, HookPoint

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

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

    # --- 生命周期状态机 ---

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

    async def delegate(
        self,
        task: SubTask,
        extra_context: str = "",
        event_callback: EventCallback | None = None,
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

        logger.info("Delegating task %s to agent %s", task.id, agent_name)
        self._ensure_lifecycle(agent_name)
        self._transition(agent_name, AgentState.RUNNING)
        await self._emit_subagent_event(
            event_callback,
            status="started",
            task_id=task.id,
            agent_name=agent_name,
            description=task.description,
            message="子 Agent 已开始执行。",
        )

        await self._hooks.fire(HookContext(
            point=HookPoint.DELEGATE_START,
            data={"task_id": task.id, "agent_name": agent_name, "description": task.description},
            agent_name=agent_name,
        ))
        result: AgentResult
        try:
            await self._hooks.fire(HookContext(
                point=HookPoint.AGENT_EXECUTE_START,
                data={"task_id": task.id, "agent_name": agent_name, "task": task.description},
                agent_name=agent_name,
            ))
            execute_kwargs: dict[str, Any] = {
                "task": task.description,
                "context": context,
            }
            if (
                event_callback is not None
                and "event_callback" in signature(agent.execute).parameters
            ):
                execute_kwargs["event_callback"] = event_callback
            timeout_seconds = _agent_timeout_seconds(agent)
            if timeout_seconds > 0 and math.isfinite(timeout_seconds):
                result = await asyncio.wait_for(
                    agent.execute(**execute_kwargs),
                    timeout=timeout_seconds,
                )
            else:
                result = await agent.execute(**execute_kwargs)
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
            self._transition(agent_name, AgentState.IDLE)

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
        callback: EventCallback | None,
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
        """并行执行多个独立子任务（分治模式）."""
        coros = [self.delegate(task) for task in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

        processed: list[AgentResult] = []
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                processed.append(
                    AgentResult(
                        status="error",
                        error=f"{type(result).__name__}: {result}",
                    )
                )
            else:
                processed.append(result)

        return processed

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


async def _emit_subagent_event(
    callback: EventCallback | None,
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
    callback: EventCallback | None,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    await callback("subagent_event", payload)


def _agent_timeout_seconds(agent: Any) -> float:
    config = getattr(agent, "config", None)
    timeout = getattr(config, "timeout_seconds", 300.0)
    return float(timeout) if isinstance(timeout, int | float) else 300.0


def _agent_result_message(result: AgentResult) -> str:
    if result.status == "completed":
        return "子 Agent 已完成任务。"
    return result.error or result.response[:300] or "子 Agent 未完成任务。"
