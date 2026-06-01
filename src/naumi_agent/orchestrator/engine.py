"""Agent 核心引擎 — ReAct 主循环."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path
from typing import Any

from naumi_agent.background import BackgroundRunner, BackgroundTaskStore, create_background_tools
from naumi_agent.config.settings import AppConfig
from naumi_agent.hooks import HookContext, HookManager, HookPoint
from naumi_agent.mcp.client import MCPClientManager, setup_mcp_servers
from naumi_agent.memory.compactor import ContextCompactor
from naumi_agent.memory.long_term import LongTermMemory
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelRouter, ModelTier, TokenUsage
from naumi_agent.orchestrator.context_assembly import (
    HARNESS_CONTEXT_MARKER,
    HarnessContextAssembler,
    HarnessContextInput,
    is_harness_context_message,
)
from naumi_agent.orchestrator.planner import AdaptivePlanner, ExecutionMode, Plan
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.safety.behavior import BehaviorMonitor
from naumi_agent.safety.budget import BudgetTracker, TokenBudget
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.scheduler import SchedulerRunner, SchedulerStore, create_scheduler_tools
from naumi_agent.skills.loader import SkillLoader
from naumi_agent.skills.tool import create_skill_tools
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime
from naumi_agent.tools.browser.tools import create_browser_tools
from naumi_agent.tools.browser_daemon import BrowserDaemonClient, create_browser_daemon_tools
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.memory import create_memory_tools
from naumi_agent.tools.sandbox import create_sandbox_tools
from naumi_agent.tools.web import create_web_tools
from naumi_agent.worktree import WorktreeManager, create_worktree_tools

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

_TASK_EVENT_TOOLS = {
    "delegate_task",
    "todo_write",
    "task_create",
    "task_update",
    "task_list",
    "task_delete",
}

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
2. Verify results after each action — but do NOT repeat the same action \
to verify. If a tool returns "Successfully", the action is done. Move on.
3. Use tools precisely — provide exact file paths and commands
4. Explain what you're doing before taking actions
5. If something fails, analyze the error and fix it within the current \
approach. Only switch approaches if the current one is provably impossible.
6. Use memory_store to save important user preferences, facts, or decisions
7. Use memory_recall to check if relevant information was discussed before
8. For complex subtasks (coding, research, browsing), consider delegating to specialized agents

## Task Management (use tools to self-track progress)
- **todo_write**: Batch-sync the todo list before complex work and after each step.
- **task_create**: Create a task with subject and optional dependencies.
- **task_update**: Mark a task in_progress (with active_form) or completed.
- **task_list**: View all tasks and their status.
- **task_delete**: Remove a task that's no longer needed.
- Always create tasks BEFORE starting work on multi-step problems.
- Mark tasks completed immediately when done; use todo_write for multiple related changes.

## Browser Tool Usage
- **browser_goto**: Navigate to a URL. Call ONCE per URL. Returns SoM elements \
and page data. Do NOT call again for the same URL.
- **browser_observe**: Re-examine the current page without navigating. Use this \
to refresh after clicks, scrolls, or dynamic content changes.
- **browser_click/type/hover/scroll**: Interact with elements by their SoM ID \
(from goto or observe results).
- After a user asks to "open a website" or "go to a URL", call browser_goto \
ONCE, then immediately respond to the user. Do NOT call goto again.

## Decision Commitment (CRITICAL — obey strictly)

1. Once you choose an approach, COMMIT to it. Do NOT switch to a different \
approach mid-execution.
2. If something fails, fix it within the current approach — do NOT start over \
with a new approach.
3. "Done and good enough" is ALWAYS better than "perfect but never finished". \
Complete your current work and present it.
4. After completing the task, STOP. Do not add extra polish, try alternatives, \
or explore tangential ideas.
5. If you catch yourself thinking "let me try X instead", STOP — finish your \
current approach first.
6. One complete solution > three half-finished attempts. Always prefer \
completing what you started over starting something new.
"""


@dataclass
class AgentUsage:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    cache_tokens: int = 0


@dataclass
class AgentResult:
    status: str  # "completed" | "max_turns" | "error"
    response: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    error: str | None = None
    task_summary: str | None = None


class AgentEngine:
    """Agent 主引擎 — 管理 LLM 循环和工具调用."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self.workspace_root = config.resolve_workspace_root()
        self._router = ModelRouter(config.models)
        self._tool_registry = ToolRegistry()
        self._messages: list[dict[str, Any]] = []
        self._full_history: list[dict[str, Any]] = []  # untruncated display history
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
            allowed_dirs=[*config.safety.allowed_dirs, str(self.workspace_root)],
            workspace_root=str(self.workspace_root),
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
        self._browser_session = BrowserRuntime(
            Path(config.memory.session_db_path).parent / "browser"
        )
        self.browser_daemon = BrowserDaemonClient(
            config.browser_daemon,
            log_dir=Path(config.memory.session_db_path).parent / "browser-daemon",
        )
        self._planner = AdaptivePlanner(
            self._router,
            usage_callback=self._track_model_usage,
        )
        self._harness_context = HarnessContextAssembler()

        self.task_store = TaskStore(config.memory.session_db_path)
        self.background_runner = BackgroundRunner(
            BackgroundTaskStore(Path(config.memory.session_db_path).parent / "background")
        )
        self.scheduler_runner = SchedulerRunner(
            SchedulerStore(Path(config.memory.session_db_path).parent / "scheduler")
        )
        self.pursuit_store = PursuitStore(
            Path(config.memory.session_db_path).parent / "pursuit"
        )
        self.worktree_manager = WorktreeManager(
            repo_root=self.workspace_root,
            storage_dir=Path(config.memory.session_db_path).parent / "worktrees",
            task_store=self.task_store,
        )

        self._mcp_manager: MCPClientManager | None = None

        self._task_runner: Any | None = None
        self._security_auditor: Any | None = None

        self.skill_loader = SkillLoader()

        self._register_builtin_tools()
        self._register_subagent_manager()
        self._register_shell_hooks()
        self._register_skills()

    def _register_builtin_tools(self) -> None:
        for tool in create_builtin_tools(self.workspace_root):
            self._tool_registry.register(tool)
        for tool in create_browser_tools(self._browser_session):
            self._tool_registry.register(tool)
        for tool in create_browser_daemon_tools(self.browser_daemon):
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

        # Task management tools
        from naumi_agent.tasks.tools import create_task_tools

        for tool in create_task_tools(self.task_store):
            self._tool_registry.register(tool)

        # Background task tools
        for tool in create_background_tools(self.background_runner):
            self._tool_registry.register(tool)

        # Scheduler / reminder tools
        for tool in create_scheduler_tools(self.scheduler_runner):
            self._tool_registry.register(tool)

        # Worktree isolation tools
        for tool in create_worktree_tools(self.worktree_manager):
            self._tool_registry.register(tool)

        # Runtime status tools
        from naumi_agent.tools.runtime import create_runtime_tools

        for tool in create_runtime_tools(self):
            self._tool_registry.register(tool)

        # Hot-reload tool
        from naumi_agent.tools.hotreload import HotReloadTool

        self._tool_registry.register(HotReloadTool())

        # Self-modification tool
        from naumi_agent.tools.self_modify import SelfModifyTool

        self._tool_registry.register(SelfModifyTool())

        # Self-evolution tool
        from naumi_agent.tools.self_evolve import SelfEvolveTool

        self._tool_registry.register(SelfEvolveTool())

        # Tool forge
        from naumi_agent.tools.forge import ForgeTool, load_all_generated_tools

        self._tool_registry.register(ForgeTool())

        # Load previously generated tools
        for tool in load_all_generated_tools():
            self._tool_registry.register(tool)
            logger.info("Loaded generated tool: %s", tool.name)

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
            store=self.pursuit_store,
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

    @property
    def task_runner(self) -> Any:
        if self._task_runner is None:
            from naumi_agent.tools.browser.orchestrator.task_runner import (
                TaskRunner,
            )

            browser_dir = str(
                Path(self._config.memory.session_db_path).parent / "browser"
            )
            self._task_runner = TaskRunner(
                base_dir=browser_dir,
                options={
                    "runtime": self._browser_session,
                    "model_router": self._router,
                },
            )
        return self._task_runner

    @property
    def security_auditor(self) -> Any:
        if self._security_auditor is None:
            from naumi_agent.tools.browser.security import SecurityAuditor

            self._security_auditor = SecurityAuditor(
                self._browser_session
            )
        return self._security_auditor

    def reset(self) -> None:
        self._messages.clear()
        self._full_history.clear()
        self._usage = AgentUsage()
        self._budget_tracker.reset()
        self._session = None
        self.task_store.set_session("")
        self._behavior_monitor.reset()
        self._permission_checker.reset_counts()

    async def shutdown(self) -> None:
        """释放资源（关闭数据库连接、浏览器、MCP 连接等）."""
        if hasattr(self, "subagent_manager"):
            await self.subagent_manager.stop_reaper()
            self.subagent_manager.destroy_all_dynamic()
        if self._task_runner is not None:
            for run in self._task_runner.runs:
                if run.get("status") in ("running", "queued"):
                    self._task_runner.abort_run(
                        run["id"], reason="Engine shutdown"
                    )
        if hasattr(self, "background_runner"):
            await self.background_runner.shutdown()
        if hasattr(self, "scheduler_runner"):
            await self.scheduler_runner.shutdown()
        await self._browser_session.stop()
        if self._mcp_manager:
            await self._mcp_manager.disconnect_all()
        if hasattr(self, "task_store"):
            self.task_store.set_session("")
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
        self._full_history = [m for m in self._full_history if m.get("role") != "system"]
        msg = {"role": "system", "content": prompt}
        self._messages.insert(0, msg)
        self._full_history.insert(0, msg)

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
        """加载已有会话，恢复上下文.

        清理不完整的工具调用序列，避免 LLM API 拒绝续接。
        """
        session = await self.session_store.load(session_id)
        if session is None:
            return False
        self._session = session
        cleaned_messages = self._sanitize_messages(session.messages)
        self._messages = cleaned_messages
        self._full_history = list(cleaned_messages)
        self._usage = AgentUsage(
            total_input_tokens=session.total_tokens,
            total_cost_usd=session.total_cost_usd,
        )
        return True

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """修复消息序列中的不完整工具调用对.

        保留所有 tool_calls 和已有的 tool 结果（包括报错信息），
        对缺失的 tool 结果补一条占位消息，确保 LLM API 不会拒绝。
        """
        cleaned: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "assistant" and msg.get("tool_calls"):
                tool_call_ids = []
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        tool_call_ids.append(tc_id)

                # Collect matching tool results into a dict keyed by tool_call_id
                tool_results: dict[str, dict[str, Any]] = {}
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    r = messages[j]
                    tc_id = r.get("tool_call_id")
                    if tc_id:
                        tool_results[tc_id] = r
                    j += 1

                # Always keep the assistant message with tool_calls
                cleaned.append(msg)

                # Emit results: existing ones preserved, missing ones get a placeholder
                for tc_id in tool_call_ids:
                    if tc_id in tool_results:
                        cleaned.append(tool_results[tc_id])
                    else:
                        cleaned.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[工具调用结果缺失 — 会话恢复时未能找到对应结果]",
                        })

                i = j
            elif role == "tool":
                # Orphan tool result without preceding assistant tool_calls — skip
                i += 1
            else:
                cleaned.append(msg)
                i += 1

        return cleaned

    async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[Session], int]:
        """列出历史会话."""
        return await self.session_store.list_sessions(page=page, page_size=page_size)

    async def delete_session(self, session_id: str) -> bool:
        """删除指定会话."""
        return await self.session_store.delete(session_id)

    async def _save_session(self) -> None:
        """将完整历史写入持久化存储（不丢失压缩前的消息）."""
        session = await self.get_or_create_session()
        session.messages = list(self._full_history) if self._full_history else list(self._messages)
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

    def _append_message(self, msg: dict[str, Any]) -> None:
        """Append to both _messages and _full_history."""
        self._messages.append(msg)
        self._full_history.append(msg)

    def _inject_background_notifications(self) -> None:
        """Inject newly completed background task notifications into context."""
        notifications = self.background_runner.collect_notifications()
        if not notifications:
            return
        self._append_message({
            "role": "user",
            "content": "\n\n".join(notifications),
        })

    def _inject_scheduler_notifications(self) -> None:
        """Inject due schedule notifications into context."""
        self.scheduler_runner.start()
        notifications = self.scheduler_runner.collect_notifications()
        if not notifications:
            return
        self._append_message({
            "role": "user",
            "content": "\n\n".join(notifications),
        })

    def _ensure_system_prompt(self) -> None:
        """Ensure the system prompt is present in active and persisted history."""
        if any(m.get("role") == "system" for m in self._messages):
            return
        self._append_message({"role": "system", "content": SYSTEM_PROMPT})

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
        runtime_snapshot, preserved_sections, warnings = (
            await self._build_compaction_runtime_snapshot()
        )
        self._messages = await self._compactor.compact(
            self._messages,
            max_tokens,
            runtime_snapshot=runtime_snapshot,
        )
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
                        "preserved_sections": preserved_sections,
                        "warnings": warnings,
                    },
                )

    async def _build_compaction_runtime_snapshot(self) -> tuple[str, list[str], list[str]]:
        """Build deterministic state that must survive history compaction."""
        sections: list[str] = []
        preserved: list[str] = []
        warnings: list[str] = []

        task_section, task_warnings = await self._compaction_task_section()
        if task_section:
            sections.append(task_section)
            preserved.append("todo")
            warnings.extend(task_warnings)

        team_section, team_warnings = await self._compaction_team_section()
        if team_section:
            sections.append(team_section)
            preserved.append("team_protocol")
            warnings.extend(team_warnings)

        subagent_section = self._compaction_subagent_section()
        if subagent_section:
            sections.append(subagent_section)
            preserved.append("subagent_events")

        constraint_section = self._compaction_user_constraint_section()
        if constraint_section:
            sections.append(constraint_section)
            preserved.append("recent_user_constraints")

        hook_section = self._compaction_hook_section()
        if hook_section:
            sections.append(hook_section)
            preserved.append("hook_trace")

        if not sections:
            return "当前没有需要额外保留的运行时状态。", [], []
        return "\n\n".join(sections), preserved, warnings

    async def _compaction_task_section(self) -> tuple[str, list[str]]:
        try:
            tasks = await self.task_store.list_tasks()
        except Exception as e:
            logger.debug("Compaction task snapshot failed: %s", e)
            return "### Todo 状态\n- 任务状态读取失败。", ["任务状态读取失败"]
        if not tasks:
            return "", []

        warnings: list[str] = []
        active = [
            task for task in tasks
            if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
        ]
        if active:
            warnings.append(f"有 {len(active)} 个未完成/阻塞 todo")

        counts = {
            status: sum(1 for task in tasks if task.status == status)
            for status in TaskStatus
        }
        lines = [
            "### Todo 状态",
            "- 汇总："
            f"{counts[TaskStatus.COMPLETED]} 完成，"
            f"{counts[TaskStatus.IN_PROGRESS]} 进行中，"
            f"{counts[TaskStatus.BLOCKED]} 阻塞，"
            f"{counts[TaskStatus.PENDING]} 待处理",
        ]
        for task in tasks[:8]:
            active_form = f" | 当前：{task.active_form}" if task.active_form else ""
            blocked_by = f" | blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(
                f"- #{task.id} [{task.status.value}] {task.subject}"
                f"{active_form}{blocked_by}"
            )
        if len(tasks) > 8:
            lines.append(f"- ... 还有 {len(tasks) - 8} 个")
        return "\n".join(lines), warnings

    async def _compaction_team_section(self) -> tuple[str, list[str]]:
        bus = self.subagent_manager.message_bus
        warnings: list[str] = []
        history = [
            msg for msg in bus.get_history(limit=12)
            if msg.topic.startswith("team.")
            or "team_event_type" in msg.metadata
        ]
        try:
            blackboard = await bus.blackboard_get_all()
        except Exception as e:
            logger.debug("Compaction team blackboard snapshot failed: %s", e)
            blackboard = {}
            warnings.append("团队黑板读取失败")

        team_entries = [
            (key, entry) for key, entry in sorted(blackboard.items())
            if key.startswith("team/")
        ]
        if not history and not team_entries:
            return "", warnings

        blockers = [
            msg for msg in history
            if msg.metadata.get("team_event_type") == "blocker"
            or msg.priority.value == "critical"
        ]
        if blockers:
            warnings.append(f"有 {len(blockers)} 条团队阻塞/高危事件")

        lines = ["### Team Protocol"]
        if history:
            lines.append("- 最近团队消息：")
            for msg in history[-8:]:
                event = msg.metadata.get("team_event_type", msg.topic)
                target = msg.recipient or "广播"
                lines.append(
                    f"  - {event}: {msg.sender} → {target} "
                    f"[{msg.priority.value}] {msg.content[:140]}"
                )
        if team_entries:
            lines.append("- 团队黑板：")
            for key, entry in team_entries[-8:]:
                value = entry.value
                content = (
                    value.get("content", value)
                    if isinstance(value, dict)
                    else value
                )
                lines.append(
                    f"  - {key} (v{entry.version}, {entry.author}): "
                    f"{str(content)[:140]}"
                )
        return "\n".join(lines), warnings

    def _compaction_subagent_section(self) -> str:
        events = self.subagent_manager.get_recent_events(limit=8)
        if not events:
            return ""
        lines = ["### Subagent 事件"]
        for event in events:
            agent = str(event.get("agent_name") or "未匹配")
            status = str(event.get("status") or "?")
            task_id = str(event.get("task_id") or "?")
            message = str(event.get("message") or "")
            lines.append(f"- {status}: {agent} / {task_id} {message[:140]}")
        return "\n".join(lines)

    def _compaction_user_constraint_section(self) -> str:
        keywords = ("不要", "先不", "最后", "注意", "必须", "禁止", "可以", "不要全量")
        constraints: list[str] = []
        for message in self._messages[-24:]:
            if message.get("role") != "user":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if any(keyword in content for keyword in keywords):
                constraints.append(content[:180])
        if not constraints:
            return ""
        lines = ["### 最近用户约束"]
        for item in constraints[-6:]:
            lines.append(f"- {item}")
        return "\n".join(lines)

    def _compaction_hook_section(self) -> str:
        trace = self.hooks.get_trace()[-8:]
        interesting = [
            entry for entry in trace
            if entry.aborted or entry.error
        ]
        if not interesting:
            return ""
        lines = ["### Hook 风险"]
        for entry in interesting:
            status = "aborted" if entry.aborted else "error"
            detail = entry.error or ""
            lines.append(f"- {entry.point}:{entry.callback} {status} {detail[:120]}")
        return "\n".join(lines)

    async def _fire_hook(
        self,
        ctx: HookContext,
        on_event: EventCallback | None = None,
    ) -> HookContext:
        """Fire hooks and optionally emit user-visible trace events."""
        trace = self.hooks.get_trace()
        last_sequence = trace[-1].sequence if trace else 0
        result = await self.hooks.fire(ctx)
        if on_event is not None:
            for entry in self.hooks.get_trace():
                if entry.sequence <= last_sequence:
                    continue
                await on_event("hook_trace", {
                    "point": entry.point,
                    "callback": entry.callback,
                    "duration_ms": entry.duration_ms,
                    "aborted": entry.aborted,
                    "error": entry.error,
                })
        return result

    async def _inject_harness_context_snapshot(
        self,
        on_event: EventCallback | None = None,
    ) -> None:
        """Inject one ephemeral system snapshot of current harness state."""
        self._messages = [
            message for message in self._messages
            if not is_harness_context_message(message)
        ]
        session_id = self._session.id if self._session else ""
        start_ctx = await self._fire_hook(HookContext(
            point=HookPoint.CONTEXT_ASSEMBLE_START,
            data={"message_count": len(self._messages)},
            session_id=session_id,
        ), on_event)
        if start_ctx.should_abort:
            return
        snapshot = await self._harness_context.assemble(
            HarnessContextInput(
                tool_registry=self._tool_registry,
                skill_loader=self.skill_loader,
                task_store=self.task_store,
                background_runner=self.background_runner,
                scheduler_runner=self.scheduler_runner,
                worktree_manager=self.worktree_manager,
                pursuit_store=self.pursuit_store,
                mcp_manager=self._mcp_manager,
                context_info=self.get_context_info(),
                budget_info=self.get_budget_info(),
            )
        )
        end_ctx = await self._fire_hook(HookContext(
            point=HookPoint.CONTEXT_ASSEMBLE_END,
            data={"snapshot": snapshot, "extra_sections": []},
            session_id=session_id,
        ), on_event)
        snapshot = str(end_ctx.data.get("snapshot", snapshot))
        extra_sections = end_ctx.data.get("extra_sections", [])
        if isinstance(extra_sections, str):
            extra_sections = [extra_sections]
        if isinstance(extra_sections, list) and extra_sections:
            snapshot = _append_harness_context_sections(
                snapshot,
                [str(section) for section in extra_sections if str(section).strip()],
            )
        self._messages.append({"role": "system", "content": snapshot})

    async def _fire_user_prompt_submit(
        self,
        task: str,
        *,
        streaming: bool = False,
        on_event: EventCallback | None = None,
    ) -> str | None:
        """Fire user prompt hook and return the possibly rewritten prompt."""
        session_id = self._session.id if self._session else ""
        ctx = await self._fire_hook(HookContext(
            point=HookPoint.USER_PROMPT_SUBMIT,
            data={"prompt": task, "streaming": streaming},
            session_id=session_id,
        ), on_event)
        if ctx.should_abort:
            return None
        return str(ctx.data.get("prompt", task))

    async def _fire_agent_stop(
        self,
        *,
        status: str,
        response: str,
        reason: str = "",
        streaming: bool = False,
        on_event: EventCallback | None = None,
    ) -> None:
        """Fire agent stop hook with final status metadata."""
        session_id = self._session.id if self._session else ""
        await self._fire_hook(HookContext(
            point=HookPoint.AGENT_STOP,
            data={
                "status": status,
                "response_length": len(response or ""),
                "reason": reason,
                "streaming": streaming,
            },
            session_id=session_id,
        ), on_event)

    async def _emit_task_snapshot(
        self,
        on_event: EventCallback | None,
        *,
        source: str,
    ) -> None:
        """Emit a user-visible task list after task tools mutate state."""
        if on_event is None or source not in _TASK_EVENT_TOOLS:
            return
        try:
            from naumi_agent.tasks.store import format_task_list

            tasks = await self.task_store.list_tasks()
            await on_event("task_snapshot", {
                "source": source,
                "count": len(tasks),
                "summary": format_task_list(tasks),
            })
        except Exception as e:
            logger.debug("Task snapshot event failed: %s", e)

    def _check_budget(self) -> AgentResult | None:
        if PermissionMode(self._config.safety.permission_mode) == PermissionMode.BYPASS:
            return None
        if not self._budget_tracker.is_exceeded():
            return None

        summary = self._budget_tracker.get_summary()
        budget = self._budget_tracker.budget
        reasons: list[str] = []
        if summary.total_input_tokens > budget.max_input_tokens:
            reasons.append(
                f"输入 token {summary.total_input_tokens:,}/{budget.max_input_tokens:,}"
            )
        if summary.total_output_tokens > budget.max_output_tokens:
            reasons.append(
                f"输出 token {summary.total_output_tokens:,}/{budget.max_output_tokens:,}"
            )
        if summary.total_cost_usd > budget.max_usd:
            reasons.append(f"费用 ${summary.total_cost_usd:.4f}/${budget.max_usd:.2f}")

        detail = "，".join(reasons) if reasons else "已超过配置上限"
        message = (
            "预算已耗尽，已停止继续执行，避免产生额外消耗。\n\n"
            f"超限项：{detail}"
        )
        logger.warning("Budget exceeded: %s", detail)
        return AgentResult(
            status="budget_exceeded",
            response=message,
            usage=self._usage,
        )

    async def run(self, task: str) -> AgentResult:
        """执行任务 — 自适应规划 + ReAct 主循环."""
        self._ensure_system_prompt()

        session = await self.get_or_create_session()
        self.task_store.set_session(session.id)

        hooked_task = await self._fire_user_prompt_submit(task)
        if hooked_task is None:
            await self._fire_agent_stop(
                status="error",
                response="用户输入已被 hook 拦截。",
                reason="user_prompt_submit_aborted",
            )
            return AgentResult(
                status="error",
                error="用户输入已被 hook 拦截。",
            )
        task = hooked_task
        self._append_message({"role": "user", "content": task})
        await self._inject_relevant_memories(task)
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        session_id = self._session.id if self._session else ""
        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task},
            session_id=session_id,
        ))

        try:
            plan = await self._planner.plan(task)
            exceeded = self._check_budget()
            if exceeded:
                result = exceeded
            elif plan.mode == ExecutionMode.ORCHESTRATOR and hasattr(self, "subagent_manager"):
                result = await self._run_orchestrated(plan, tools)
            else:
                result = await self._react_loop(tools, plan=plan)
        except Exception as e:
            logger.exception("Agent loop failed")
            result = AgentResult(status="error", error=self._format_error(e))

        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task},
            session_id=session_id,
        ))

        await self._save_session()

        # Attach task summary if tasks exist
        tasks = await self.task_store.list_tasks()
        if tasks:
            from naumi_agent.tasks.store import format_task_list
            result.task_summary = format_task_list(tasks)

        return result

    async def run_streaming(
        self,
        task: str,
        on_event: EventCallback,
    ) -> AgentResult:
        """执行任务 — 流式 ReAct 主循环，通过回调实时推送事件."""
        self._ensure_system_prompt()

        session = await self.get_or_create_session()
        self.task_store.set_session(session.id)

        hooked_task = await self._fire_user_prompt_submit(
            task,
            streaming=True,
            on_event=on_event,
        )
        if hooked_task is None:
            message = "用户输入已被 hook 拦截。"
            await on_event("error", {"message": message})
            await self._fire_agent_stop(
                status="error",
                response=message,
                reason="user_prompt_submit_aborted",
                streaming=True,
                on_event=on_event,
            )
            return AgentResult(status="error", error=message)
        task = hooked_task
        self._append_message({"role": "user", "content": task})
        await self._inject_relevant_memories(task)
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        session_id = self._session.id if self._session else ""
        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task, "streaming": True},
            session_id=session_id,
        ), on_event)

        try:
            plan = await self._planner.plan(task)
            exceeded = self._check_budget()
            if exceeded:
                result = exceeded
            elif plan.mode == ExecutionMode.ORCHESTRATOR and hasattr(self, "subagent_manager"):
                result = await self._run_orchestrated(plan, tools)
            else:
                result = await self._react_loop_streaming(tools, on_event, plan=plan)
        except Exception as e:
            logger.exception("Agent streaming loop failed")
            error_msg = self._format_error(e)
            await on_event("error", {"message": error_msg})
            result = AgentResult(status="error", error=error_msg)

        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task, "streaming": True},
            session_id=session_id,
        ), on_event)

        await self._save_session()

        # Attach task summary if tasks exist
        tasks = await self.task_store.list_tasks()
        if tasks:
            from naumi_agent.tasks.store import format_task_list
            result.task_summary = format_task_list(tasks)

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
        self._append_message({"role": "assistant", "content": response})
        return AgentResult(
            status="completed",
            response=response,
            usage=self._usage,
        )

    def _is_repeated_tool_call(
        self, tool_name: str, args: str, history: list[str]
    ) -> bool:
        """Detect if the same tool+args has been called 3+ consecutive times."""
        sig = f"{tool_name}:{args}"
        if len(history) < 2:
            return False
        return history[-1] == sig and history[-2] == sig

    def _format_plan_as_guidance(self, plan: Plan) -> str | None:
        """Format a plan as system message guidance for decision commitment."""
        if plan.approach == "直接执行" or len(plan.steps) <= 1:
            return None
        lines = ["## 执行计划指导"]
        lines.append(f"策略：{plan.approach}")
        lines.append(f"任务理解：{plan.understanding}")
        lines.append("执行步骤：")
        for i, step in enumerate(plan.steps, 1):
            tool_hint = f"（工具：{step.tool}）" if step.tool else ""
            lines.append(f"  {i}. {step.description} {tool_hint}")
        if plan.potential_issues:
            lines.append(f"注意事项：{'、'.join(plan.potential_issues)}")
        lines.append("")
        lines.append("请按此计划逐步执行。一旦开始执行某一步骤，请完成它，不要中途切换到其他方案。")
        return "\n".join(lines)

    async def _react_loop(
        self,
        tools: list[dict[str, Any]] | None,
        plan: Plan | None = None,
    ) -> AgentResult:
        """ReAct 循环：推理 → 行动 → 观察."""
        max_turns = self._config.safety.max_turns
        tool_call_history: list[str] = []
        convergence_injected = False

        # Inject plan as guidance to prevent approach oscillation
        if plan:
            plan_guidance = self._format_plan_as_guidance(plan)
            if plan_guidance:
                self._append_message({"role": "system", "content": plan_guidance})

        for turn in range(max_turns):
            self._behavior_monitor.begin_turn(turn)
            self._usage.turns = turn + 1
            self._inject_background_notifications()
            self._inject_scheduler_notifications()

            exceeded = self._check_budget()
            if exceeded:
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                )
                return exceeded

            await self._maybe_compact()
            await self._inject_harness_context_snapshot()

            # --- 推理：调用 LLM ---
            session_id = self._session.id if self._session else ""
            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "message_count": len(self._messages)},
                session_id=session_id,
            ))
            response = await self._router.call(
                messages=self._messages,
                tier=ModelTier.CAPABLE,
                tools=tools,
            )
            self._track_model_usage(response.usage, response.model)
            await self._fire_hook(HookContext(
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

            exceeded = self._check_budget()
            if exceeded:
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                )
                return exceeded

            # --- 行动：处理工具调用 ---
            if response.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": response.tool_calls,
                }
                if response.reasoning_content:
                    assistant_msg["reasoning_content"] = response.reasoning_content
                self._append_message(assistant_msg)

                cur_calls: list[str] = []
                skip_remaining_reason = ""
                for tc_raw in response.tool_calls:
                    tc = self._parse_tool_call(tc_raw)
                    if tc is None:
                        call_id = self._extract_tool_call_id(tc_raw)
                        if call_id:
                            self._append_message(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": "工具调用格式无效，无法解析函数名称或参数。",
                                }
                            )
                        continue

                    call_sig = f"{tc.name}:{tc.arguments}"
                    cur_calls.append(call_sig)

                    if skip_remaining_reason:
                        self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": skip_remaining_reason,
                            }
                        )
                        continue

                    if self._is_repeated_tool_call(
                        tc.name, tc.arguments, tool_call_history
                    ):
                        logger.warning(
                            "Repeated tool call detected: %s, injecting stop",
                            tc.name,
                        )
                        skip_remaining_reason = (
                            "This action has already been completed successfully. "
                            "Do NOT repeat it. Provide your final response to the user now."
                        )
                        self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": skip_remaining_reason,
                            }
                        )
                        continue

                    hook_ctx = await self._fire_hook(HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={"tool_name": tc.name, "arguments": tc.arguments},
                        session_id=session_id,
                    ))
                    if hook_ctx.should_abort:
                        self._append_message(
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
                    await self._fire_hook(HookContext(
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
                    self._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.content,
                        }
                    )

                tool_call_history.extend(cur_calls)

                # Active intervention: break approach oscillation
                intervention = self._behavior_monitor.check_intervention()
                if intervention is not None:
                    self._append_message({
                        "role": "system",
                        "content": intervention.message,
                    })
                    if intervention.action == "force_converge":
                        if convergence_injected:
                            last_text = ""
                            for m in reversed(self._messages):
                                if m.get("role") == "assistant" and m.get("content"):
                                    last_text = m["content"]
                                    break
                            safe_text = self._output_guardrail.redact(
                                last_text or "任务执行被强制收敛（检测到方案振荡）。"
                            )
                            await self._fire_agent_stop(
                                status="completed",
                                response=safe_text,
                                reason="force_converge",
                            )
                            return AgentResult(
                                status="completed",
                                response=safe_text,
                                usage=self._usage,
                            )
                        convergence_injected = True

                exceeded = self._check_budget()
                if exceeded:
                    await self._fire_agent_stop(
                        status=exceeded.status,
                        response=exceeded.response,
                        reason="budget_exceeded",
                    )
                    return exceeded

                continue

            # --- 无工具调用：最终回答 ---
            tool_call_history.clear()
            safe_content = self._output_guardrail.redact(response.content)
            self._append_message({"role": "assistant", "content": response.content})
            await self._fire_agent_stop(
                status="completed",
                response=safe_content,
                reason="final_response",
            )
            return AgentResult(
                status="completed",
                response=safe_content,
                usage=self._usage,
            )

        await self._fire_agent_stop(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            reason="max_turns",
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
        plan: Plan | None = None,
    ) -> AgentResult:
        """流式 ReAct 循环：通过 router.stream() 逐 token 输出."""
        max_turns = self._config.safety.max_turns
        model_str = self._router.resolve_model(ModelTier.CAPABLE)
        session_id = self._session.id if self._session else ""
        tool_call_history: list[str] = []
        convergence_injected = False

        # Inject plan as guidance to prevent approach oscillation
        if plan:
            plan_guidance = self._format_plan_as_guidance(plan)
            if plan_guidance:
                self._append_message({"role": "system", "content": plan_guidance})

        for turn in range(max_turns):
            self._behavior_monitor.begin_turn(turn)
            self._usage.turns = turn + 1
            self._inject_background_notifications()
            self._inject_scheduler_notifications()

            exceeded = self._check_budget()
            if exceeded:
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                    streaming=True,
                    on_event=on_event,
                )
                return exceeded

            await self._maybe_compact(on_event)
            await self._inject_harness_context_snapshot(on_event)
            await on_event("turn_start", {"turn": turn + 1, "model": model_str})

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            collected_tool_calls: dict[int, dict[str, Any]] = {}
            got_response = False
            got_thinking = False
            stream_tokens = 0

            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "streaming": True, "message_count": len(self._messages)},
                session_id=session_id,
            ), on_event)

            try:
                async for chunk in self._router.stream(
                    messages=self._messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                ):
                    if chunk.usage:
                        self._track_model_usage(chunk.usage, model_str)
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
                self._track_model_usage(response.usage, model_str)
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

            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_END,
                data={
                    "turn": turn + 1,
                    "model": model_str,
                    "total_tokens": stream_tokens,
                    "has_tool_calls": bool(collected_tool_calls),
                    "streaming": True,
                },
                session_id=session_id,
            ), on_event)

            if got_thinking:
                await on_event("thinking_end", {"content": "".join(thinking_parts)})

            text_content = "".join(text_parts)
            thinking_content = "".join(thinking_parts)

            exceeded = self._check_budget()
            if exceeded:
                await on_event("error", {"message": exceeded.response})
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                    streaming=True,
                    on_event=on_event,
                )
                return exceeded

            # --- 工具调用 ---
            if collected_tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": list(collected_tool_calls.values()),
                }
                if thinking_content:
                    assistant_msg["reasoning_content"] = thinking_content
                self._append_message(assistant_msg)

                cur_calls: list[str] = []
                skip_remaining_reason = ""
                for tc_raw in collected_tool_calls.values():
                    tc = self._parse_tool_call(tc_raw)
                    if tc is None:
                        call_id = self._extract_tool_call_id(tc_raw)
                        if call_id:
                            self._append_message(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": "工具调用格式无效，无法解析函数名称或参数。",
                                }
                            )
                        continue

                    call_sig = f"{tc.name}:{tc.arguments}"
                    cur_calls.append(call_sig)

                    if skip_remaining_reason:
                        await on_event("tool_end", {
                            "name": tc.name,
                            "status": "skipped",
                            "duration_ms": 0,
                            "content": skip_remaining_reason,
                        })
                        self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": skip_remaining_reason,
                            }
                        )
                        continue

                    if self._is_repeated_tool_call(
                        tc.name, tc.arguments, tool_call_history
                    ):
                        logger.warning(
                            "Repeated tool call detected: %s, injecting stop",
                            tc.name,
                        )
                        skip_remaining_reason = (
                            "This action has already been completed successfully. "
                            "Do NOT repeat it. Provide your final response to the user now."
                        )
                        self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": skip_remaining_reason,
                            }
                        )
                        continue

                    await on_event("tool_start", {"name": tc.name, "args": tc.arguments})

                    hook_ctx = await self._fire_hook(HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={"tool_name": tc.name, "arguments": tc.arguments},
                        session_id=session_id,
                    ), on_event)
                    if hook_ctx.should_abort:
                        abort_reason = hook_ctx.data.get("abort_reason", "no reason")
                        await on_event("tool_end", {
                            "name": tc.name,
                            "status": "aborted",
                            "duration_ms": 0,
                            "content": f"Aborted by hook: {abort_reason}",
                        })
                        self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Aborted by hook: {abort_reason}",
                            }
                        )
                        continue

                    result = await self._execute_tool(tc, on_event=on_event)
                    await on_event(
                        "tool_end",
                        {
                            "name": tc.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content": result.content[:2000] if result.content else "",
                        },
                    )
                    await self._fire_hook(HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": tc.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content_length": len(result.content) if result.content else 0,
                        },
                        session_id=session_id,
                    ), on_event)
                    self._behavior_monitor.record_tool_call(
                        tc.name, is_error=(result.status == "error")
                    )
                    self._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.content,
                        }
                    )

                tool_call_history.extend(cur_calls)

                # Active intervention: break approach oscillation
                intervention = self._behavior_monitor.check_intervention()
                if intervention is not None:
                    self._append_message({
                        "role": "system",
                        "content": intervention.message,
                    })
                    if intervention.action == "force_converge":
                        if convergence_injected:
                            last_text = ""
                            for m in reversed(self._messages):
                                if m.get("role") == "assistant" and m.get("content"):
                                    last_text = m["content"]
                                    break
                            await on_event("response_start", {})
                            force_msg = last_text or "任务执行被强制收敛（检测到方案振荡）。"
                            await on_event("token", {"content": force_msg})
                            await on_event("response_end", {})
                            await self._fire_agent_stop(
                                status="completed",
                                response=force_msg,
                                reason="force_converge",
                                streaming=True,
                                on_event=on_event,
                            )
                            return AgentResult(
                                status="completed",
                                response=self._output_guardrail.redact(force_msg),
                                usage=self._usage,
                            )
                        convergence_injected = True

                exceeded = self._check_budget()
                if exceeded:
                    return exceeded
                continue

            # --- 最终回答 ---
            tool_call_history.clear()
            if got_response:
                await on_event("response_end", {})
            self._append_message({"role": "assistant", "content": text_content})
            safe_content = self._output_guardrail.redact(text_content)
            await self._fire_agent_stop(
                status="completed",
                response=safe_content,
                reason="final_response",
                streaming=True,
                on_event=on_event,
            )
            return AgentResult(
                status="completed",
                response=safe_content,
                usage=self._usage,
            )

        await self._fire_agent_stop(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            reason="max_turns",
            streaming=True,
            on_event=on_event,
        )
        return AgentResult(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            usage=self._usage,
        )

    async def _execute_tool(
        self,
        tc: ToolCall,
        on_event: EventCallback | None = None,
    ) -> ToolResult:
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

        session_id = self._session.id if self._session else ""
        before_ctx = await self._fire_hook(HookContext(
            point=HookPoint.TOOL_PERMISSION_CHECK,
            data={
                "phase": "before",
                "tool_name": tc.name,
                "arguments": args,
            },
            session_id=session_id,
        ), on_event)
        if before_ctx.should_abort:
            reason = before_ctx.data.get("abort_reason", "hook policy")
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Permission denied by hook: {reason}",
            )

        decision = self._permission_checker.check(tc.name, args)
        after_ctx = await self._fire_hook(HookContext(
            point=HookPoint.TOOL_PERMISSION_CHECK,
            data={
                "phase": "after",
                "tool_name": tc.name,
                "arguments": args,
                "allowed": decision.allowed,
                "requires_confirmation": decision.requires_confirmation,
                "reason": decision.reason,
            },
            session_id=session_id,
        ), on_event)
        if after_ctx.should_abort:
            reason = after_ctx.data.get("abort_reason", "hook policy")
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Permission denied by hook: {reason}",
            )
        if not decision.allowed:
            logger.warning("Tool %s blocked: %s", tc.name, decision.reason)
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Permission denied: {decision.reason}",
            )
        if decision.requires_confirmation:
            logger.warning("Tool %s requires confirmation and was blocked", tc.name)
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=(
                    "Permission denied: 该工具需要用户确认，当前自动执行链路未提供确认步骤。"
                    "请在 bypass 模式下运行，或使用更安全的替代工具完成任务。"
                ),
            )

        try:
            start = time.time()
            if on_event is not None and "event_callback" in signature(tool.execute).parameters:
                args["event_callback"] = on_event
            output = await tool.execute(**args)
            duration = int((time.time() - start) * 1000)

            logger.info("Tool %s executed in %dms", tc.name, duration)
            await self._emit_task_snapshot(on_event, source=tc.name)
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

    @staticmethod
    def _extract_tool_call_id(raw: dict[str, Any]) -> str:
        """Best-effort extraction of tool_call id for protocol-complete error replies."""
        try:
            return str(raw.get("id") or "")
        except Exception:
            return ""

    def _accumulate_usage(self, usage: TokenUsage) -> None:
        """累加 token 用量."""
        self._usage.total_input_tokens += usage.input_tokens
        self._usage.total_output_tokens += usage.output_tokens
        self._usage.total_cost_usd += usage.cost_usd
        self._usage.cache_tokens += usage.cache_tokens

    def _track_model_usage(self, usage: TokenUsage, model: str) -> None:
        """Record model usage in both user-facing stats and budget enforcement."""
        self._accumulate_usage(usage)
        self._budget_tracker.track(usage, model)

    def get_context_info(self) -> dict[str, Any]:
        """Return context window usage estimate."""
        model = self._router.resolve_model(ModelTier.CAPABLE)
        window = self._router.get_context_window(model)
        used = self._usage.total_input_tokens
        return {
            "model": model,
            "window": window,
            "used": used,
            "percentage": min(100, round(used / window * 100, 1)) if window > 0 else 0,
        }

    def get_budget_info(self) -> dict[str, Any]:
        """Return budget consumption info."""
        summary = self._budget_tracker.get_summary()
        return {
            "max_usd": self._budget_tracker.budget.max_usd,
            "used_usd": summary.total_cost_usd,
            "remaining_usd": summary.remaining_usd,
            "percentage": round(
                summary.total_cost_usd / self._budget_tracker.budget.max_usd * 100, 1
            ) if self._budget_tracker.budget.max_usd > 0 else 0,
        }

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


def _append_harness_context_sections(snapshot: str, sections: list[str]) -> str:
    """Append hook-provided sections before the closing harness marker."""
    if not sections:
        return snapshot
    extra = "\n\n".join(sections)
    closing = f"\n{HARNESS_CONTEXT_MARKER}"
    if snapshot.endswith(closing):
        return snapshot[: -len(closing)] + f"\n\n{extra}{closing}"
    return f"{snapshot}\n\n{extra}"
