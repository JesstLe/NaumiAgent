"""子 Agent 调度器 — 管理、选择、并行执行."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from naumi_agent.agents.base import AgentConfig, AgentResult, BaseAgent
from naumi_agent.agents.presets import ALL_AGENT_CONFIGS

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

logger = logging.getLogger(__name__)

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
    """管理和调度子 Agent."""

    def __init__(self, engine: AgentEngine) -> None:
        self._engine = engine
        self._agents: dict[str, BaseAgent] = {}
        self._configs: dict[str, AgentConfig] = dict(ALL_AGENT_CONFIGS)

    def get_agent(self, name: str) -> BaseAgent | None:
        """获取或创建 Agent 实例."""
        if name in self._agents:
            return self._agents[name]

        config = self._configs.get(name)
        if not config:
            return None

        agent = BaseAgent(config, self._engine)
        self._agents[name] = agent
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
        logger.info("Spawned dynamic agent: %s", name)
        return agent

    def destroy(self, name: str) -> bool:
        """销毁一个动态 Agent，释放资源.

        返回 True 表示成功销毁，False 表示 Agent 不存在或属于预设 Agent。
        预设 Agent（coder/researcher/browser）不可销毁。
        """
        from naumi_agent.agents.presets import ALL_AGENT_CONFIGS

        if name in ALL_AGENT_CONFIGS:
            logger.warning("Cannot destroy preset agent: %s", name)
            return False

        removed_agent = self._agents.pop(name, None)
        self._configs.pop(name, None)

        if removed_agent is not None:
            logger.info("Destroyed dynamic agent: %s", name)
            return True
        return False

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

    async def delegate(self, task: SubTask, extra_context: str = "") -> AgentResult:
        """将子任务委派给合适的 Agent."""
        agent_name = task.agent_name or self.select_agent(task.description)
        if not agent_name:
            return AgentResult(
                status="error",
                error=f"No suitable agent for: {task.description[:100]}",
            )

        agent = self.get_agent(agent_name)
        if not agent:
            return AgentResult(status="error", error=f"Agent not found: {agent_name}")

        context_parts = []
        if task.context:
            context_parts.append(task.context)
        if extra_context:
            context_parts.append(extra_context)

        context = "\n\n".join(context_parts) if context_parts else ""

        logger.info("Delegating task %s to agent %s", task.id, agent_name)
        return await agent.execute(task=task.description, context=context)

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

            # 找到所有依赖已完成的任务
            ready = [t for t in remaining if all(dep in results for dep in (t.depends_on or []))]

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
        """列出可用的 Agent."""
        return [
            {"name": config.name, "description": config.description}
            for config in self._configs.values()
        ]
