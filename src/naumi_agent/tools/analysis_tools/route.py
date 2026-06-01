"""MoE route analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools.analysis_support.route import build_route_report, scan_route
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
ResolveTarget = Callable[[str], list[Path]]
ReadSources = Callable[[list[Path]], str]
SubagentManagerGetter = Callable[[Any], Any | None]

ROUTE_SYSTEM = """\
You are a Mixture-of-Experts (MoE) orchestrator with semantic routing.

## Core Principle
DO NOT answer complex problems from a single perspective. Instead:
1. **Decompose** the problem into domain-specific sub-problems
2. **Instantiate** 3-5 specialized virtual experts
3. **Distribute** sub-problems to each expert for independent analysis
4. **Synthesize** their outputs into a unified, multi-dimensional solution

## Your Tasks

### 1. Expert Panel Formation
Based on the scan evidence and the task, declare your expert team:
- Each expert must have a specific domain, NOT a generic title
- Each expert must have a clear analytical lens (what they focus on)
- Minimum 3 experts, maximum 5

### 2. Individual Expert Analysis
For EACH expert, provide their independent analysis:
- **Expert Name & Domain**
- **Their Perspective**: What this expert sees as the key issues
- **Their Recommendations**: Specific, actionable advice
- **Their Concerns**: What could go wrong from their domain
- **Confidence**: X/10

### 3. Cross-Expert Conflict Resolution
If experts disagree:
- Identify the conflict explicitly
- Present both sides
- Make a ruling with justification
- If uncertain, propose an experiment to resolve it

### 4. Synthesized Solution
Combine all expert outputs into a single actionable plan:
- Priority-ordered action items
- Each item tagged with the responsible expert domain
- Dependencies between items
- Risk assessment for the overall plan

### 5. Resource Estimation
- Estimated complexity (S/M/L/XL)
- Recommended team size and skill requirements
- Suggested phasing (what to do first, what to defer)

Be thorough. Each expert's analysis should be substantive, not perfunctory.
"""


class MoERouteTool(Tool):
    """MoE 混合专家调度工具."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
        resolve_target: ResolveTarget | None = None,
        read_sources: ReadSources | None = None,
        subagent_manager_getter: SubagentManagerGetter | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis
        self._resolve_target = resolve_target or (lambda _target: [])
        self._read_sources = read_sources or (lambda _files: "")
        self._subagent_manager_getter = subagent_manager_getter or (lambda _router: None)

    @property
    def name(self) -> str:
        return "analysis_route"

    @property
    def description(self) -> str:
        return (
            "MoE 混合专家调度：面对复杂跨学科任务时，实例化 3-5 个垂直领域"
            "虚拟专家，将问题拆解分发给各专家独立分析，"
            "最后汇总为多维度统一方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要分析的任务描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        source_text = ""
        files: list[Path] = []
        if target:
            files = self._resolve_target(target)
            if files:
                source_text = self._read_sources(files)

        scan_evidence = scan_route(files, source_text, task)
        deterministic = build_route_report(task, scan_evidence, source_text)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 MoE 专家路由。"

        manager = self._subagent_manager_getter(router)
        if manager is not None:
            agent_report = await self._execute_with_agents(
                router,
                manager,
                task,
                scan_evidence,
                source_text,
            )
            return deterministic + "\n\n## SubAgent MoE 执行结果\n" + agent_report

        user_msg = f"## 任务描述\n{task}\n"
        user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
        user_msg += f"\n## 确定性 MoE 骨架\n{deterministic}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:50000]}\n"

        enhanced = await self._run_analysis(router, ROUTE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM MoE 综合增强\n" + enhanced

    async def _execute_with_agents(
        self,
        router: Any,
        manager: Any,
        task: str,
        scan_evidence: str,
        source_text: str,
    ) -> str:
        """Use SubAgentManager + Factory + MessageBus for multi-agent MoE."""
        from naumi_agent.agents.message_bus import AgentMessage
        from naumi_agent.orchestrator.subagent_manager import SubTask

        await manager.message_bus.reset()

        await manager.message_bus.blackboard_set(
            "task",
            task,
            author="orchestrator",
        )
        await manager.message_bus.blackboard_set(
            "scan_evidence",
            scan_evidence,
            author="orchestrator",
        )
        if source_text:
            await manager.message_bus.blackboard_set(
                "source_summary",
                source_text[:4000],
                author="orchestrator",
            )

        planning_prompt = (
            "Based on the task and scan evidence below, identify 3-5 expert domains.\n"
            "For each expert, output EXACTLY this format (one per line):\n"
            "EXPERT|<name>|<domain>|<one-line-focus>\n\n"
            "Only output EXPERT lines, nothing else.\n\n"
            f"## 任务\n{task}\n\n## 扫描结果\n{scan_evidence}"
        )
        planning_resp = await self._run_analysis(router, planning_prompt, task)
        expert_lines = [ln for ln in planning_resp.strip().splitlines() if ln.startswith("EXPERT|")]

        if not expert_lines:
            user_msg = f"## 任务描述\n{task}\n"
            user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
            if source_text:
                user_msg += f"\n## 相关源代码\n{source_text[:50000]}\n"
            return await self._run_analysis(router, ROUTE_SYSTEM, user_msg)

        spawned_names: list[str] = []
        subtasks: list[SubTask] = []

        for i, line in enumerate(expert_lines[:5]):
            parts = line.split("|")
            if len(parts) < 4:
                continue
            raw_name = parts[1].strip()
            domain = parts[2].strip()
            focus = parts[3].strip()
            safe_name = f"moe_{raw_name.replace(' ', '_').replace('/', '_')[:25]}"

            manager.spawn_for_task(
                name=safe_name,
                task_description=task,
                role="expert_analyst",
                focus=focus,
                domain=domain,
                max_turns=3,
                max_budget_usd=0.15,
            )
            spawned_names.append(safe_name)

            expert_task = f"从{domain}专家视角分析以下任务:\n\n{task}\n"
            if source_text:
                expert_task += f"\n## 相关代码（摘要）\n{source_text[:8000]}\n"

            subtasks.append(
                SubTask(
                    id=f"expert_{i}",
                    description=expert_task,
                    agent_name=safe_name,
                )
            )

        if not subtasks:
            user_msg = f"## 任务描述\n{task}\n"
            user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
            return await self._run_analysis(router, ROUTE_SYSTEM, user_msg)

        results = await manager.execute_parallel(subtasks)

        expert_reports: list[str] = []
        for st, result in zip(subtasks, results):
            agent_name = st.agent_name or "unknown"
            if result.status == "completed" and result.response:
                expert_reports.append(f"### {agent_name}\n{result.response}")
                await manager.message_bus.blackboard_set(
                    f"expert_{agent_name}",
                    result.response[:2000],
                    author=agent_name,
                )
                await manager.message_bus.publish(
                    AgentMessage(
                        sender=agent_name,
                        topic="moe.expert.completed",
                        content=result.response[:500],
                        metadata={"domain": st.description[:100]},
                    )
                )
            else:
                expert_reports.append(
                    f"### {agent_name}\n⚠️ 分析未完成: {result.error or '未知错误'}"
                )

        bb_state = await manager.message_bus.blackboard_get_all()
        bb_summary = ""
        if bb_state:
            bb_lines = ["### 共享状态摘要"]
            for k, entry in bb_state.items():
                if k.startswith("expert_"):
                    bb_lines.append(f"- **{k}** (v{entry.version}): {str(entry.value)[:100]}...")
            bb_summary = "\n".join(bb_lines)

        synthesis_msg = f"## 原始任务\n{task}\n\n"
        synthesis_msg += f"## 静态扫描\n{scan_evidence}\n\n"
        synthesis_msg += "## 各专家独立分析\n\n"
        synthesis_msg += "\n\n---\n\n".join(expert_reports)
        if bb_summary:
            synthesis_msg += f"\n\n---\n\n{bb_summary}"

        synthesis = await self._run_analysis(router, ROUTE_SYSTEM, synthesis_msg)

        for name in spawned_names:
            manager.destroy(name)
        await manager.message_bus.reset()

        bus_stats = manager.message_bus.stats()

        total_tok = sum(r.total_tokens for r in results if hasattr(r, "total_tokens"))
        total_usd = sum(r.total_cost_usd for r in results if hasattr(r, "total_cost_usd"))
        header = (
            f"## MoE 混合专家调度报告\n\n"
            f"**任务**: {task[:200]}\n"
            f"**专家组**: {len(spawned_names)} 位专家并行分析\n"
            f"**总 Token 消耗**: {total_tok}\n"
            f"**总成本**: ${total_usd:.4f}\n"
            f"**消息总线**: {bus_stats['total_messages']} 条消息, "
            f"{bus_stats['blackboard_entries']} 条共享状态\n\n"
            f"---\n\n"
        )
        return header + synthesis
