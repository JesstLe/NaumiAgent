"""Agent 核心引擎 — ReAct 主循环."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from naumi_agent.config.settings import AppConfig
from naumi_agent.hooks import HookContext, HookManager, HookPoint
from naumi_agent.mcp.client import MCPClientManager, setup_mcp_servers
from naumi_agent.memory.compactor import ContextCompactor
from naumi_agent.memory.long_term import LongTermMemory
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelRouter, ModelTier, TokenUsage
from naumi_agent.orchestrator.planner import AdaptivePlanner, ExecutionMode
from naumi_agent.safety.behavior import BehaviorMonitor
from naumi_agent.safety.budget import BudgetTracker, TokenBudget
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.skills.loader import SkillLoader
from naumi_agent.skills.tool import create_skill_tools
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.browser import BrowserSession, create_browser_tools
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.memory import create_memory_tools
from naumi_agent.tools.sandbox import create_sandbox_tools
from naumi_agent.tools.web import create_web_tools

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are NaumiAgent, a general-purpose AI assistant with tool access.

## Your Capabilities
- Read, write, and edit files
- Execute shell commands
- Browse the web (navigate, click, type, extract content)
- Search the web and fetch web pages
- Execute code in sandboxed environments
- Store important facts in long-term memory for future sessions
- Recall relevant memories from past conversations
- Delegate subtasks to specialized agents (coder, researcher, browser)

## Analysis Modes (use tools autonomously when appropriate)
- **analysis_chaos**: Disaster drill — find SPOFs, simulate failures, \
produce hardening roadmap
- **analysis_scale**: Concurrency stress test — identify bottlenecks, \
produce remediation plan
- **analysis_state**: Cloud-native audit — find stateful violations, \
provide distributed solutions
- **analysis_vibe**: Rapid prototyping — generate working demo code fast
- **analysis_eval**: Eval-Driven Development (EDD) — statically scan code \
structure and generate runnable pytest covering all branches & edge cases
- **analysis_page**: LLM OS memory paging — analyze context window pressure, \
produce register snapshot, page_out/page_in recommendations
- **analysis_heal**: Self-healing code — diagnose error logs, locate root \
cause, generate minimal hotfix + defensive guards + regression test
- **analysis_dspy**: DSPy prompt compiler — scan prompt templates, \
few-shot coverage, evaluation metrics, and generate optimization plan
- **analysis_graph**: GraphRAG topology analysis — extract entity nodes \
and relationship edges from code, compute centrality/cycles/components, \
trace risk propagation paths
- **analysis_mcts**: Monte Carlo Tree Search — explore multiple solution \
paths, simulate disasters on each, prune bad branches, output verified best
- **analysis_route**: MoE expert routing — decompose complex tasks, \
instantiate 3-5 domain experts, distribute sub-problems, synthesize
- **analysis_speculate**: Speculative Decoding — fast intern draft + slow \
architect review, identify boilerplate vs high-risk zones, dual-pass
- **analysis_jit**: JIT tool generation — when LLM reasoning is unreliable, \
generate runnable Python/C++ scripts, show execution trace, verify with tests
- **analysis_pointer**: Semantic Pointer Architecture — separate reasoning \
space (AI logic) from physical space (precise computation), define pointer \
protocol to eliminate hallucination on precise data
- **analysis_cooe**: Cognitive Out-of-Order Execution — decompose tasks \
into DAG, identify data dependencies vs parallelizable steps, design \
scheduler + reservation stations + reorder buffer pipeline
- **analysis_sleep**: Circadian Synaptic Pruning — offline compression \
of session knowledge, extract core insights, prune redundancy, \
generate evolution patch for system prompt
- **analysis_entropy**: Dissipative Structure Valve — force entropy \
reduction when reasoning drifts, condense to 3-sentence anchor, \
purge context, restart from anchor
- **analysis_ooda**: OODA Loop Mission Command — analyze code fragility, \
design intent-driven self-correcting architecture with observe/orient/\
decide/act loop and self-healing mechanisms
- **analysis_probe**: Black-Box Probe — anti-hallucination protocol for \
unknown/closed-source systems, generate reconnaissance scripts first, \
collect real data, then develop based on verified information
- **analysis_hook**: Reverse Engineering & Instrumentation — dynamic \
analysis for black-box targets (memory scanning, API hooking, IL \
reflection), anti-debug evasion, data extraction pipeline
- **analysis_vision**: AI Vision Data Extraction — when APIs are blocked \
by anti-scraping, design screen-level vision pipeline (capture→detect→\
OCR→validate→output) to bypass software-layer restrictions
- **analysis_spar**: Adversarial Self-Play (GAN for Code) — blue team writes \
code, red team breaks it, physical sandbox as oracle, iterate N rounds \
until hardened. Prevents reward hacking and nihilism
- **analysis_world**: World Model Audit — treat the system as a miniature \
physics engine: inventory state entities, map transitions, trace causal \
chains, audit object permanence, find counterfactual gaps, score \
world model completeness
- **analysis_fusion**: Deterministic-Probabilistic Fusion Audit — scan \
the boundary between AI (probabilistic) and traditional code \
(deterministic), detect dangerous fusion points where AI output feeds \
into precision-critical operations without validation, identify \
over-determined code that could benefit from AI
- **analysis_consensus**: Byzantine Consensus — multi-model voting \
system for high-risk decisions, detect single-point-of-decision risks, \
design heterogeneous model deployment + quorum arbitration + circuit \
breaker mechanism
- **analysis_pid**: PID Closed-Loop Control — transform open-loop \
pipelines into P(real-time correction) + I(historical learning) + \
D(trend prediction) feedback control, monitor→evaluate→actuate cycle
- **analysis_zkp**: Zero-Knowledge Proof & Verifiable Computation — \
audit AI outputs for traceability, detect unverified outputs and \
claim-fact gaps, design citation trace tree + deterministic verifier, \
turn AI from black-box magician into auditable worker
- **analysis_genesis**: Genesis Self-Evolution — scan code rigidity \
vs meta-programming capability, design self-modifying architecture \
with plugin system, hot-reload, sandbox verification, and automatic \
rollback for continuous self-improvement
- **analysis_macro**: Agentic Economy & Market Equilibrium — transform \
centralized AI into free-market ecosystem with 1000+ micro-agents, \
Token economy, natural selection, price discovery, and macro \
regulation. Emergent collective intelligence via selfish competition
- **analysis_cosmos**: Computational Cosmology — evaluate a system's \
genesis potential: state dimension richness, procedural generation \
capacity, multi-agent social simulation readiness, observer-effect \
reactivity. Design the path from code to world
- **analysis_watchdog**: Watchdog & Disaster Isolation — prevent AI \
from bricking itself during self-modification. Audit in-place surgery \
risks, heartbeat coverage, rollback infrastructure, isolation level. \
Design watchdog timer + A/B blue-green deployment + Ring -1 god node

When the user's request involves reviewing code quality, scalability, \
resilience, rapid prototyping, testing, context management, or bug fixing, \
proactively use the appropriate analysis tool. You can also chain them \
(e.g., use analysis_chaos after writing code to verify it's resilient, \
or use analysis_eval after implementing a feature to generate tests).

## Guidelines
1. Break complex tasks into steps
2. Verify results after each action
3. Use tools precisely — provide exact file paths and commands
4. Explain what you're doing before taking actions
5. If something fails, analyze the error and try a different approach
6. Use memory_store to save important user preferences, facts, or decisions
7. Use memory_recall to check if relevant information was discussed before
8. For complex subtasks (coding, research, browsing), consider delegating to specialized agents
"""


@dataclass
class AgentUsage:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0


@dataclass
class AgentResult:
    status: str  # "completed" | "max_turns" | "error"
    response: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    error: str | None = None


class AgentEngine:
    """Agent 主引擎 — 管理 LLM 循环和工具调用."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._router = ModelRouter(config.models)
        self._tool_registry = ToolRegistry()
        self._messages: list[dict[str, Any]] = []
        self._usage = AgentUsage()
        self._budget_tracker = BudgetTracker(
            TokenBudget(
                max_input_tokens=config.safety.max_input_tokens,
                max_usd=config.safety.max_budget_usd,
            )
        )
        self._behavior_monitor = BehaviorMonitor()
        self._output_guardrail = OutputGuardrail()
        self._permission_checker = PermissionChecker(
            mode=PermissionMode(config.safety.permission_mode),
            allowed_dirs=config.safety.allowed_dirs,
        )
        self.session_store = SessionStore(config.memory)
        self.long_term_memory = LongTermMemory(config.memory)
        self._compactor = ContextCompactor(
            config.memory,
            self._router,
            threshold=config.memory.compaction_threshold,
            long_term_memory=self.long_term_memory,
        )

        self.emitter = EventEmitter()
        self.hooks = HookManager()
        self._session: Session | None = None
        self._browser_session = BrowserSession()
        self._planner = AdaptivePlanner(self._router)

        self._mcp_manager: MCPClientManager | None = None

        self.skill_loader = SkillLoader()

        self._register_builtin_tools()
        self._register_subagent_manager()
        self._register_shell_hooks()
        self._register_skills()

    def _register_builtin_tools(self) -> None:
        for tool in create_builtin_tools():
            self._tool_registry.register(tool)
        for tool in create_browser_tools(self._browser_session):
            self._tool_registry.register(tool)
        for tool in create_sandbox_tools():
            self._tool_registry.register(tool)
        try:
            for tool in create_web_tools():
                self._tool_registry.register(tool)
        except Exception:
            pass  # web tools optional (may need API keys)

        # 分析模式工具（chaos/scale/state/vibe）
        from naumi_agent.tools.analysis import (
            create_analysis_tools,
            set_analysis_router,
        )

        set_analysis_router(self._router)
        for tool in create_analysis_tools():
            self._tool_registry.register(tool)

        try:
            for tool in create_memory_tools(self.long_term_memory):
                self._tool_registry.register(tool)
        except Exception:
            pass  # memory tools optional (chromadb may not be installed)

        # Hot-reload tool
        from naumi_agent.tools.hotreload import HotReloadTool

        self._tool_registry.register(HotReloadTool())

        # Self-modification tool
        from naumi_agent.tools.self_modify import SelfModifyTool

        self._tool_registry.register(SelfModifyTool())

        # Self-evolution tool
        from naumi_agent.tools.self_evolve import SelfEvolveTool

        self._tool_registry.register(SelfEvolveTool())

    def _register_subagent_manager(self) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager
        from naumi_agent.tools.analysis import set_analysis_subagent_manager
        from naumi_agent.tools.pursuit import set_pursuit_dependencies
        from naumi_agent.tools.subagent import create_subagent_tools

        self.subagent_manager = SubAgentManager(self)
        set_analysis_subagent_manager(self.subagent_manager)
        for tool in create_subagent_tools(self.subagent_manager):
            self._tool_registry.register(tool)

        # Goal pursuit tool
        set_pursuit_dependencies(
            router=self._router,
            tool_registry=self._tool_registry,
            subagent_manager=self.subagent_manager,
        )
        from naumi_agent.tools.pursuit import create_pursuit_tool
        for tool in create_pursuit_tool():
            self._tool_registry.register(tool)

        self._reaper_started = False

    def _register_shell_hooks(self) -> None:
        """从 config.yaml 的 hooks 段注册 shell 命令 hook."""
        from naumi_agent.hooks.shell_hook import ShellHookConfig, create_shell_hook_runner

        hooks_cfg = self._config.hooks
        registered = 0
        for point_name in HookPoint:
            entries = getattr(hooks_cfg, point_name.value, None)
            if not entries:
                continue
            for entry in entries:
                if not isinstance(entry, dict) or "command" not in entry:
                    logger.warning("Invalid shell hook config for %s: %s", point_name.value, entry)
                    continue
                shell_cfg = ShellHookConfig.from_dict(entry)
                runner = create_shell_hook_runner(shell_cfg)
                self.hooks.register(point_name, runner)
                registered += 1
        if registered:
            logger.info("Registered %d shell hooks from config", registered)

    def _register_skills(self) -> None:
        """从配置的搜索路径加载 Skill 并注册为 Tool."""
        search_paths = self._config.skills.search_paths

        # 默认搜索路径：项目 .naumi/skills/ 和用户 ~/.naumi/skills/
        default_paths = [
            str(Path.cwd() / ".naumi" / "skills"),
            str(Path.home() / ".naumi" / "skills"),
        ]
        all_paths = default_paths + search_paths

        self.skill_loader = SkillLoader(search_paths=all_paths)
        skills = self.skill_loader.load_all()

        if not skills:
            return

        for tool in create_skill_tools(skills):
            self._tool_registry.register(tool)

        logger.info("Registered %d skills from %d search paths", len(skills), len(all_paths))

    async def setup_mcp_tools(self) -> None:
        """从配置连接 MCP Server 并注册工具（需在异步上下文中调用）."""
        server_configs = self._config.mcp.servers
        if not server_configs:
            return

        manager, tools = await setup_mcp_servers(server_configs)
        self._mcp_manager = manager
        for tool in tools:
            self._tool_registry.register(tool)

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def router(self) -> ModelRouter:
        return self._router

    @property
    def usage(self) -> AgentUsage:
        return self._usage

    def reset(self) -> None:
        self._messages.clear()
        self._usage = AgentUsage()
        self._session = None
        self._behavior_monitor.reset()
        self._permission_checker.reset_counts()

    async def shutdown(self) -> None:
        """释放资源（关闭数据库连接、浏览器、MCP 连接等）."""
        if hasattr(self, "subagent_manager"):
            await self.subagent_manager.stop_reaper()
            self.subagent_manager.destroy_all_dynamic()
        await self._browser_session.close()
        if self._mcp_manager:
            await self._mcp_manager.disconnect_all()
        await self.session_store.close()

    async def reload_tools(self, domain: str = "tools") -> dict[str, Any]:
        """热重载指定域的模块并重新注册工具.

        Args:
            domain: "tools", "memory", "skills", "all"

        Returns:
            重载结果统计
        """
        from naumi_agent.tools.hotreload import reload_domain

        results = reload_domain(domain)

        reloaded = sum(1 for r in results if r["status"] == "reloaded")
        errors = sum(1 for r in results if r["status"] == "error")

        # If tools were reloaded, re-register analysis tools (most common case)
        if domain in ("tools", "all") and reloaded > 0:
            try:
                from naumi_agent.tools.analysis import (
                    create_analysis_tools,
                    set_analysis_router,
                )

                set_analysis_router(self._router)
                for tool in create_analysis_tools():
                    self._tool_registry.register(tool)
            except Exception as e:
                logger.warning("Failed to re-register analysis tools: %s", e)

        # If skills were reloaded, re-register
        if domain in ("skills", "all") and reloaded > 0:
            self._register_skills()

        logger.info(
            "Hot-reload complete: %d reloaded, %d errors", reloaded, errors,
        )

        return {
            "reloaded": reloaded,
            "errors": errors,
            "details": results,
        }

    def set_system_prompt(self, prompt: str) -> None:
        """设置/更新系统提示词."""
        # 移除旧的 system message
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        self._messages.insert(0, {"role": "system", "content": prompt})

    # --- 会话持久化 ---

    async def get_or_create_session(self, title: str | None = None) -> Session:
        """获取当前会话，不存在则创建."""
        if self._session is not None:
            return self._session
        self._session = await self.session_store.create_session(
            title=title,
            model=self._router.resolve_model(ModelTier.CAPABLE),
            system_prompt=next(
                (m["content"] for m in self._messages if m.get("role") == "system"),
                SYSTEM_PROMPT,
            ),
        )
        return self._session

    async def load_session(self, session_id: str) -> bool:
        """加载已有会话，恢复上下文."""
        session = await self.session_store.load(session_id)
        if session is None:
            return False
        self._session = session
        self._messages = list(session.messages)
        self._usage = AgentUsage(
            total_input_tokens=session.total_tokens,
            total_cost_usd=session.total_cost_usd,
        )
        return True

    async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[Session], int]:
        """列出历史会话."""
        return await self.session_store.list_sessions(page=page, page_size=page_size)

    async def delete_session(self, session_id: str) -> bool:
        """删除指定会话."""
        return await self.session_store.delete(session_id)

    async def _save_session(self) -> None:
        """将当前上下文写入持久化存储."""
        session = await self.get_or_create_session()
        session.messages = list(self._messages)
        session.total_tokens = self._usage.total_input_tokens + self._usage.total_output_tokens
        session.total_cost_usd = self._usage.total_cost_usd

        # 自动标题：从第一条用户消息中提取
        if not session.title or session.title == "新会话":
            for m in self._messages:
                if m.get("role") == "user":
                    session.title = m.get("content", "")[:50].split("\n")[0]
                    break

        await self.session_store.save(session)

    # --- 记忆注入 ---

    async def _inject_relevant_memories(self, user_message: str) -> None:
        """自动召回与用户消息相关的长期记忆，注入到上下文中."""
        try:
            results = await self.long_term_memory.recall(
                user_message, top_k=3, min_relevance=0.4,
            )
        except Exception as e:
            logger.debug("Memory recall for injection failed: %s", e)
            return

        if not results:
            return

        lines = ["## 相关记忆"]
        for r in results:
            lines.append(f"- [{r.entry.category}] {r.entry.content}")
        memory_block = "\n".join(lines)

        # Remove any previous memory injection to avoid accumulation
        self._messages = [
            m for m in self._messages
            if not (
                m.get("role") == "system"
                and "## 相关记忆" in m.get("content", "")
            )
        ]

        self._messages.append({"role": "system", "content": memory_block})
        logger.info("Injected %d relevant memories into context", len(results))

    # --- 上下文压缩 ---

    async def _maybe_compact(self, on_event: EventCallback | None = None) -> None:
        """检查并执行上下文压缩."""
        model = self._router.resolve_model(ModelTier.CAPABLE)
        context_window = self._router.get_context_window(model)
        # 用户配置的 max_input_tokens 作为硬上限兜底
        hard_cap = self._config.safety.max_input_tokens
        max_tokens = min(context_window, hard_cap)

        if not self._compactor.should_compact(self._messages, max_tokens):
            return

        before = len(self._messages)
        self._messages = await self._compactor.compact(self._messages, max_tokens)
        after = len(self._messages)

        if after < before:
            logger.info(
                "Context compacted: %d → %d messages (window=%d, cap=%d)",
                before,
                after,
                context_window,
                hard_cap,
            )
            if on_event:
                await on_event(
                    "context_compacted",
                    {
                        "before": before,
                        "after": after,
                    },
                )

    def _check_budget(self) -> AgentResult | None:
        return None

    async def _ensure_reaper(self) -> None:
        if not self._reaper_started and hasattr(self, "subagent_manager"):
            self._reaper_started = True
            await self.subagent_manager.start_reaper()

    async def run(self, task: str) -> AgentResult:
        """执行任务 — 自适应规划 + ReAct 主循环."""
        await self._ensure_reaper()
        if not any(m.get("role") == "system" for m in self._messages):
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": task})
        await self._inject_relevant_memories(task)
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        session_id = self._session.id if self._session else ""
        await self.hooks.fire(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task},
            session_id=session_id,
        ))

        try:
            plan = await self._planner.plan(task)
            if plan.mode == ExecutionMode.ORCHESTRATOR and hasattr(self, "subagent_manager"):
                result = await self._run_orchestrated(plan, tools)
            else:
                result = await self._react_loop(tools)
        except Exception as e:
            logger.exception("Agent loop failed")
            result = AgentResult(status="error", error=self._format_error(e))

        await self.hooks.fire(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task},
            session_id=session_id,
        ))

        await self._save_session()
        return result

    async def run_streaming(
        self,
        task: str,
        on_event: EventCallback,
    ) -> AgentResult:
        """执行任务 — 流式 ReAct 主循环，通过回调实时推送事件."""
        await self._ensure_reaper()
        if not any(m.get("role") == "system" for m in self._messages):
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": task})
        await self._inject_relevant_memories(task)
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        session_id = self._session.id if self._session else ""
        await self.hooks.fire(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task, "streaming": True},
            session_id=session_id,
        ))

        try:
            result = await self._react_loop_streaming(tools, on_event)
        except Exception as e:
            logger.exception("Agent streaming loop failed")
            error_msg = self._format_error(e)
            await on_event("error", {"message": error_msg})
            result = AgentResult(status="error", error=error_msg)

        await self.hooks.fire(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task, "streaming": True},
            session_id=session_id,
        ))

        await self._save_session()
        return result

    async def _run_orchestrated(
        self, plan: Any, tools: list[dict[str, Any]] | None
    ) -> AgentResult:
        """执行编排模式：按 DAG 依赖关系委派子任务给专用 Agent."""
        from naumi_agent.orchestrator.subagent_manager import SubTask

        tasks = [
            SubTask(
                id=step.id,
                description=step.description,
                agent_name=None,
                depends_on=step.depends_on,
            )
            for step in plan.steps
        ]

        results = await self.subagent_manager.execute_dag(tasks)

        combined_parts = []
        total_tokens = 0
        total_cost = 0.0
        for step in plan.steps:
            r = results.get(step.id)
            if r and r.status == "completed":
                combined_parts.append(f"## {step.description}\n{r.response[:2000]}")
                total_tokens += r.total_tokens
                total_cost += r.total_cost_usd
            elif r:
                combined_parts.append(
                    f"## {step.description}\n⚠️ {r.status}: {r.error or ''}"
                )

        self._accumulate_usage(
            TokenUsage(
                input_tokens=0,
                output_tokens=total_tokens,
                total_tokens=total_tokens,
                cost_usd=total_cost,
            )
        )

        response = "\n\n".join(combined_parts)
        self._messages.append({"role": "assistant", "content": response})
        return AgentResult(
            status="completed",
            response=response,
            usage=self._usage,
        )

    async def _react_loop(self, tools: list[dict[str, Any]] | None) -> AgentResult:
        """ReAct 循环：推理 → 行动 → 观察."""
        max_turns = self._config.safety.max_turns

        for turn in range(max_turns):
            self._usage.turns = turn + 1

            exceeded = self._check_budget()
            if exceeded:
                return exceeded

            await self._maybe_compact()

            # --- 推理：调用 LLM ---
            session_id = self._session.id if self._session else ""
            await self.hooks.fire(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "message_count": len(self._messages)},
                session_id=session_id,
            ))
            response = await self._router.call(
                messages=self._messages,
                tier=ModelTier.CAPABLE,
                tools=tools,
            )
            self._accumulate_usage(response.usage)
            self._budget_tracker.track(response.usage, response.model)
            await self.hooks.fire(HookContext(
                point=HookPoint.LLM_CALL_END,
                data={
                    "turn": turn + 1,
                    "model": response.model,
                    "total_tokens": response.usage.total_tokens,
                    "cost_usd": response.usage.cost_usd,
                    "has_tool_calls": bool(response.tool_calls),
                },
                session_id=session_id,
            ))

            # 行为监控
            warnings = self._behavior_monitor.check_anomalous_behavior()
            if warnings:
                logger.warning("Behavior warnings: %s", warnings)

            # --- 行动：处理工具调用 ---
            if response.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": response.tool_calls,
                }
                if response.reasoning_content:
                    assistant_msg["reasoning_content"] = response.reasoning_content
                self._messages.append(assistant_msg)

                for tc_raw in response.tool_calls:
                    tc = self._parse_tool_call(tc_raw)
                    if tc is None:
                        continue

                    hook_ctx = await self.hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={"tool_name": tc.name, "arguments": tc.arguments},
                        session_id=session_id,
                    ))
                    if hook_ctx.should_abort:
                        self._messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": (
                                    "Aborted by hook: "
                                    f"{hook_ctx.data.get('abort_reason', 'no reason')}"
                                ),
                            }
                        )
                        continue

                    result = await self._execute_tool(tc)
                    await self.hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": tc.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content_length": len(result.content) if result.content else 0,
                        },
                        session_id=session_id,
                    ))
                    self._behavior_monitor.record_tool_call(
                        tc.name, is_error=(result.status == "error")
                    )
                    self._messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.content,
                        }
                    )

                exceeded = self._check_budget()
                if exceeded:
                    return exceeded

                continue

            # --- 无工具调用：最终回答 ---
            safe_content = self._output_guardrail.redact(response.content)
            self._messages.append({"role": "assistant", "content": response.content})
            return AgentResult(
                status="completed",
                response=safe_content,
                usage=self._usage,
            )

        return AgentResult(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            usage=self._usage,
        )

    async def _react_loop_streaming(
        self,
        tools: list[dict[str, Any]] | None,
        on_event: EventCallback,
    ) -> AgentResult:
        """流式 ReAct 循环：通过 router.stream() 逐 token 输出."""
        max_turns = self._config.safety.max_turns
        model_str = self._router.resolve_model(ModelTier.CAPABLE)
        session_id = self._session.id if self._session else ""

        for turn in range(max_turns):
            self._usage.turns = turn + 1

            exceeded = self._check_budget()
            if exceeded:
                return exceeded

            await self._maybe_compact(on_event)
            await on_event("turn_start", {"turn": turn + 1})

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            collected_tool_calls: dict[int, dict[str, Any]] = {}
            got_response = False
            got_thinking = False
            stream_tokens = 0

            await self.hooks.fire(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "streaming": True, "message_count": len(self._messages)},
                session_id=session_id,
            ))

            try:
                async for chunk in self._router.stream(
                    messages=self._messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                ):
                    if chunk.usage:
                        self._accumulate_usage(chunk.usage)
                        self._budget_tracker.track(chunk.usage, model_str)
                        stream_tokens = chunk.usage.total_tokens

                    if chunk.thinking:
                        if not got_thinking:
                            got_thinking = True
                            await on_event("thinking_start", {})
                        thinking_parts.append(chunk.thinking)
                        await on_event("thinking_delta", {"content": chunk.thinking})

                    if chunk.token:
                        if not got_response:
                            got_response = True
                            await on_event("response_start", {})
                        text_parts.append(chunk.token)
                        await on_event("token", {"content": chunk.token})

                    if chunk.tool_call and isinstance(chunk.tool_call, dict):
                        collected_tool_calls.update(chunk.tool_call)
            except Exception as e:
                logger.warning("Streaming failed, fallback to non-streaming: %s", e)
                response = await self._router.call(
                    messages=self._messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                )
                self._accumulate_usage(response.usage)
                self._budget_tracker.track(response.usage, model_str)
                stream_tokens = response.usage.total_tokens
                if response.content:
                    if not got_response:
                        got_response = True
                        await on_event("response_start", {})
                    text_parts.append(response.content)
                if response.reasoning_content:
                    got_thinking = True
                    thinking_parts.append(response.reasoning_content)
                if response.tool_calls:
                    collected_tool_calls = {i: tc for i, tc in enumerate(response.tool_calls)}

            await self.hooks.fire(HookContext(
                point=HookPoint.LLM_CALL_END,
                data={
                    "turn": turn + 1,
                    "model": model_str,
                    "total_tokens": stream_tokens,
                    "has_tool_calls": bool(collected_tool_calls),
                    "streaming": True,
                },
                session_id=session_id,
            ))

            if got_thinking:
                await on_event("thinking_end", {"content": "".join(thinking_parts)})

            text_content = "".join(text_parts)
            thinking_content = "".join(thinking_parts)

            # --- 工具调用 ---
            if collected_tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": list(collected_tool_calls.values()),
                }
                if thinking_content:
                    assistant_msg["reasoning_content"] = thinking_content
                self._messages.append(assistant_msg)

                for tc_raw in collected_tool_calls.values():
                    tc = self._parse_tool_call(tc_raw)
                    if tc is None:
                        continue

                    await on_event("tool_start", {"name": tc.name, "args": tc.arguments})

                    hook_ctx = await self.hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={"tool_name": tc.name, "arguments": tc.arguments},
                        session_id=session_id,
                    ))
                    if hook_ctx.should_abort:
                        abort_reason = hook_ctx.data.get("abort_reason", "no reason")
                        await on_event("tool_end", {
                            "name": tc.name,
                            "status": "aborted",
                            "duration_ms": 0,
                            "content": f"Aborted by hook: {abort_reason}",
                        })
                        self._messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Aborted by hook: {abort_reason}",
                            }
                        )
                        continue

                    result = await self._execute_tool(tc)
                    await on_event(
                        "tool_end",
                        {
                            "name": tc.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content": result.content[:2000] if result.content else "",
                        },
                    )
                    await self.hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": tc.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content_length": len(result.content) if result.content else 0,
                        },
                        session_id=session_id,
                    ))
                    self._behavior_monitor.record_tool_call(
                        tc.name, is_error=(result.status == "error")
                    )
                    self._messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.content,
                        }
                    )

                exceeded = self._check_budget()
                if exceeded:
                    return exceeded
                continue

            # --- 最终回答 ---
            if got_response:
                await on_event("response_end", {})
            self._messages.append({"role": "assistant", "content": text_content})
            safe_content = self._output_guardrail.redact(text_content)
            return AgentResult(
                status="completed",
                response=safe_content,
                usage=self._usage,
            )

        return AgentResult(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            usage=self._usage,
        )

    async def _execute_tool(self, tc: ToolCall) -> ToolResult:
        """执行单个工具调用（含权限检查）."""
        tool = self._tool_registry.get(tc.name)
        if tool is None:
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Unknown tool: {tc.name}",
            )

        try:
            args = tool.parse_arguments(tc.arguments)
        except ValueError as e:
            return ToolResult(call_id=tc.id, status="error", content=str(e))

        decision = self._permission_checker.check(tc.name, args)
        if not decision.allowed:
            logger.warning("Tool %s blocked: %s", tc.name, decision.reason)
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Permission denied: {decision.reason}",
            )

        try:
            start = time.time()
            output = await tool.execute(**args)
            duration = int((time.time() - start) * 1000)

            logger.info("Tool %s executed in %dms", tc.name, duration)
            return ToolResult(
                call_id=tc.id,
                status="success",
                content=output,
                duration_ms=duration,
            )
        except Exception as e:
            logger.warning("Tool %s failed: %s", tc.name, e)
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Tool error: {type(e).__name__}: {e}",
            )

    def _parse_tool_call(self, raw: dict[str, Any]) -> ToolCall | None:
        """从 LLM 响应中提取 ToolCall."""
        try:
            func = raw.get("function", {})
            return ToolCall(
                id=raw.get("id", ""),
                name=func.get("name", ""),
                arguments=func.get("arguments", "{}"),
            )
        except Exception:
            logger.warning("Failed to parse tool call: %s", raw)
            return None

    def _accumulate_usage(self, usage: TokenUsage) -> None:
        """累加 token 用量."""
        self._usage.total_input_tokens += usage.input_tokens
        self._usage.total_output_tokens += usage.output_tokens
        self._usage.total_cost_usd += usage.cost_usd

    @staticmethod
    def _format_error(e: Exception) -> str:
        """将异常转为用户友好的错误信息."""
        error_type = type(e).__name__
        msg = str(e)
        if "AuthenticationError" in error_type or "api_key" in msg.lower():
            return (
                "API Key 未设置或无效。请通过环境变量设置:\n"
                "  export NAUMI_MODELS__API_KEY=your-key\n"
                "或在 config.yaml 中配置 api_key"
            )
        if "RateLimitError" in error_type:
            return "API 调用频率超限，请稍后重试。"
        return f"{error_type}: {msg}"
