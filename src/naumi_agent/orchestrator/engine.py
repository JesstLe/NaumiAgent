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
from naumi_agent.model.router import ModelResponse, ModelRouter, ModelTier, TokenUsage
from naumi_agent.safety.behavior import BehaviorMonitor
from naumi_agent.safety.budget import BudgetTracker, TokenBudget
from naumi_agent.safety.guardrails import OutputGuardrail, SecurityError
from naumi_agent.tools.base import Tool, ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.browser import create_browser_tools
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

## Guidelines
1. Break complex tasks into steps
2. Verify results after each action
3. Use tools precisely — provide exact file paths and commands
4. Explain what you're doing before taking actions
5. If something fails, analyze the error and try a different approach
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

        from naumi_agent.memory.session import SessionStore
        from naumi_agent.streaming.event_bus import EventEmitter

        self.session_store = SessionStore(config.memory)
        self.emitter = EventEmitter()

        self._register_builtin_tools()

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

    def set_system_prompt(self, prompt: str) -> None:
        """设置/更新系统提示词."""
        # 移除旧的 system message
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        self._messages.insert(0, {"role": "system", "content": prompt})

    async def run(self, task: str) -> AgentResult:
        """执行任务 — ReAct 主循环."""
        start_time = time.time()

        # 确保 system prompt 存在
        if not any(m.get("role") == "system" for m in self._messages):
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": task})

        tools = self._tool_registry.get_openai_tools() if len(self._tool_registry) > 0 else None

        try:
            return await self._react_loop(tools)
        except Exception as e:
            logger.exception("Agent loop failed")
            return AgentResult(status="error", error=str(e))

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
            return await self._react_loop_streaming(tools, on_event)
        except Exception as e:
            logger.exception("Agent streaming loop failed")
            await on_event("error", {"message": str(e)})
            return AgentResult(status="error", error=str(e))

    async def _react_loop(
        self, tools: list[dict[str, Any]] | None
    ) -> AgentResult:
        """ReAct 循环：推理 → 行动 → 观察."""
        max_turns = self._config.safety.max_turns

        for turn in range(max_turns):
            self._usage.turns = turn + 1

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

                    # 检查预算
                    if self._usage.total_cost_usd >= self._config.safety.max_budget_usd:
                        return AgentResult(
                            status="error",
                            response="预算已达上限，停止执行。",
                            usage=self._usage,
                            error="budget_exceeded",
                        )

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
                        collected_tool_calls = chunk.tool_call
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

                    if self._usage.total_cost_usd >= self._config.safety.max_budget_usd:
                        return AgentResult(
                            status="error",
                            response="预算已达上限，停止执行。",
                            usage=self._usage,
                            error="budget_exceeded",
                        )
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
        """执行单个工具调用."""
        tool = self._tool_registry.get(tc.name)
        if tool is None:
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Unknown tool: {tc.name}",
            )

        try:
            args = tool.parse_arguments(tc.arguments)
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
