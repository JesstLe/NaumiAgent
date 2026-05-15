"""Agent 核心引擎 — ReAct 主循环."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from naumi_agent.config.settings import AppConfig
from naumi_agent.memory.compactor import ContextCompactor
from naumi_agent.memory.long_term import LongTermMemory
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelResponse, ModelRouter, ModelTier, TokenUsage
from naumi_agent.safety.behavior import BehaviorMonitor
from naumi_agent.safety.budget import BudgetTracker, TokenBudget
from naumi_agent.safety.guardrails import OutputGuardrail, SecurityError
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.tools.base import Tool, ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.browser import create_browser_tools
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
        self._budget_tracker = BudgetTracker(TokenBudget(
            max_input_tokens=config.safety.max_input_tokens,
            max_usd=config.safety.max_budget_usd,
        ))
        self._behavior_monitor = BehaviorMonitor()
        self._output_guardrail = OutputGuardrail()
        self._permission_checker = PermissionChecker(
            mode=PermissionMode(config.safety.permission_mode),
            allowed_dirs=config.safety.allowed_dirs,
        )
        self._compactor = ContextCompactor(
            config.memory,
            self._router,
            threshold=config.memory.compaction_threshold,
        )

        self.session_store = SessionStore(config.memory)
        self.long_term_memory = LongTermMemory(config.memory)
        self.emitter = EventEmitter()
        self._session: Session | None = None

        self._register_builtin_tools()
        self._register_subagent_manager()

    def _register_builtin_tools(self) -> None:
        for tool in create_builtin_tools():
            self._tool_registry.register(tool)
        for tool in create_browser_tools():
            self._tool_registry.register(tool)
        for tool in create_sandbox_tools():
            self._tool_registry.register(tool)
        try:
            for tool in create_web_tools():
                self._tool_registry.register(tool)
        except Exception:
            pass  # web tools optional (may need API keys)

        try:
            for tool in create_memory_tools(self.long_term_memory):
                self._tool_registry.register(tool)
        except Exception:
            pass  # memory tools optional (chromadb may not be installed)

    def _register_subagent_manager(self) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager
        from naumi_agent.tools.subagent import create_subagent_tools

        self.subagent_manager = SubAgentManager(self)
        for tool in create_subagent_tools(self.subagent_manager):
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
        """释放资源（关闭数据库连接等）."""
        await self.session_store.close()

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

    async def _save_session(self) -> None:
        """将当前上下文写入持久化存储."""
        session = await self.get_or_create_session()
        session.messages = list(self._messages)
        session.total_tokens = self._usage.total_input_tokens + self._usage.total_output_tokens
        session.total_cost_usd = self._usage.total_cost_usd
        await self.session_store.save(session)

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
                before, after, context_window, hard_cap,
            )
            if on_event:
                await on_event("context_compacted", {
                    "before": before,
                    "after": after,
                })

    def _check_budget(self) -> AgentResult | None:
        """检查预算是否超限，超限则返回错误结果."""
        if self._budget_tracker.is_exceeded():
            return AgentResult(
                status="error",
                response="预算已达上限，停止执行。",
                usage=self._usage,
                error="budget_exceeded",
            )
        return None

    async def run(self, task: str) -> AgentResult:
        """执行任务 — ReAct 主循环."""
        if not any(m.get("role") == "system" for m in self._messages):
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": task})
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        try:
            result = await self._react_loop(tools)
        except Exception as e:
            logger.exception("Agent loop failed")
            result = AgentResult(status="error", error=str(e))

        await self._save_session()
        return result

    async def run_streaming(
        self,
        task: str,
        on_event: EventCallback,
    ) -> AgentResult:
        """执行任务 — 流式 ReAct 主循环，通过回调实时推送事件."""
        if not any(m.get("role") == "system" for m in self._messages):
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": task})
        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        try:
            result = await self._react_loop_streaming(tools, on_event)
        except Exception as e:
            logger.exception("Agent streaming loop failed")
            await on_event("error", {"message": str(e)})
            result = AgentResult(status="error", error=str(e))

        await self._save_session()
        return result

    async def _react_loop(
        self, tools: list[dict[str, Any]] | None
    ) -> AgentResult:
        """ReAct 循环：推理 → 行动 → 观察."""
        max_turns = self._config.safety.max_turns

        for turn in range(max_turns):
            self._usage.turns = turn + 1

            exceeded = self._check_budget()
            if exceeded:
                return exceeded

            await self._maybe_compact()

            # --- 推理：调用 LLM ---
            response = await self._router.call(
                messages=self._messages,
                tier=ModelTier.CAPABLE,
                tools=tools,
            )
            self._accumulate_usage(response.usage)
            self._budget_tracker.track(response.usage, response.model)

            # 行为监控
            warnings = self._behavior_monitor.check_anomalous_behavior()
            if warnings:
                logger.warning("Behavior warnings: %s", warnings)

            # --- 行动：处理工具调用 ---
            if response.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": response.tool_calls,
                }
                if response.reasoning_content:
                    assistant_msg["reasoning_content"] = response.reasoning_content
                self._messages.append(assistant_msg)

                for tc_raw in response.tool_calls:
                    tc = self._parse_tool_call(tc_raw)
                    if tc is None:
                        continue

                    result = await self._execute_tool(tc)
                    self._behavior_monitor.record_tool_call(
                        tc.name, is_error=(result.status == "error")
                    )
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content,
                    })

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

            try:
                async for chunk in self._router.stream(
                    messages=self._messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                ):
                    if chunk.usage:
                        self._accumulate_usage(chunk.usage)
                        self._budget_tracker.track(chunk.usage, model_str)

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

            if got_thinking:
                await on_event("thinking_end", {"content": "".join(thinking_parts)})

            text_content = "".join(text_parts)
            thinking_content = "".join(thinking_parts)

            # --- 工具调用 ---
            if collected_tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content,
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
                    result = await self._execute_tool(tc)
                    await on_event("tool_end", {
                        "name": tc.name,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
                    })
                    self._behavior_monitor.record_tool_call(
                        tc.name, is_error=(result.status == "error")
                    )
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content,
                    })

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
