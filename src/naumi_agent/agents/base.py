"""Agent 基类与配置."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine
    from naumi_agent.tools.base import ToolRegistry


class AgentCapability(str, Enum):
    FILE_OPS = "file_operations"
    CODE_EXEC = "code_execution"
    WEB_BROWSE = "web_browsing"
    WEB_SEARCH = "web_search"
    SHELL_EXEC = "shell_execution"


@dataclass(frozen=True)
class AgentConfig:
    """Agent 配置."""
    name: str
    description: str
    capabilities: list[AgentCapability]
    model_tier: str = "capable"
    system_prompt: str = ""
    max_turns: int = 20
    max_budget_usd: float = 1.0
    tools: list[str] = field(default_factory=list)
    permission_level: str = "moderate"


@dataclass(frozen=True)
class AgentResult:
    """Agent 执行结果."""
    status: str  # "completed" | "error" | "timeout" | "max_turns"
    response: str = ""
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    error: str | None = None


# 能力 → 工具名 映射
_CAPABILITY_TOOLS: dict[AgentCapability, list[str]] = {
    AgentCapability.FILE_OPS: ["file_read", "file_write", "file_edit"],
    AgentCapability.CODE_EXEC: ["code_execute"],
    AgentCapability.WEB_BROWSE: [
        "browser_navigate", "browser_click", "browser_type",
        "browser_screenshot", "browser_extract", "browser_get_html",
    ],
    AgentCapability.WEB_SEARCH: ["web_search", "web_fetch"],
    AgentCapability.SHELL_EXEC: ["bash_run"],
}


class BaseAgent:
    """所有子 Agent 的基类."""

    def __init__(self, config: AgentConfig, engine: AgentEngine) -> None:
        self.config = config
        self.engine = engine
        self._tool_names = self._resolve_tools()

    def _resolve_tools(self) -> list[str]:
        """根据能力和显式配置解析工具列表."""
        tools: set[str] = set()

        for cap in self.config.capabilities:
            for tool_name in _CAPABILITY_TOOLS.get(cap, []):
                tools.add(tool_name)

        for tool_name in self.config.tools:
            tools.add(tool_name)

        # 验证工具存在
        available = set(self.engine.tool_registry.names)
        return [t for t in tools if t in available]

    def _get_tool_schemas(self) -> list[dict[str, Any]]:
        """获取允许的工具 schema."""
        tools = []
        for name in self._tool_names:
            tool = self.engine.tool_registry.get(name)
            if tool:
                tools.append(tool.to_openai_tool())
        return tools

    async def execute(self, task: str, context: str = "") -> AgentResult:
        """执行子任务."""
        from naumi_agent.model.router import ModelTier

        messages: list[dict[str, Any]] = []

        # 系统提示
        system_parts = []
        if context:
            system_parts.append(f"## 前置上下文\n{context}")
        if self.config.system_prompt:
            system_parts.append(self.config.system_prompt)

        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        messages.append({"role": "user", "content": task})

        tools = self._get_tool_schemas() or None
        tier_map = {
            "fast": ModelTier.FAST,
            "capable": ModelTier.CAPABLE,
            "reasoning": ModelTier.REASONING,
        }
        tier = tier_map.get(self.config.model_tier, ModelTier.CAPABLE)
        tier_map = {
            "fast": RouterTier.FAST,
            "capable": RouterTier.CAPABLE,
            "reasoning": RouterTier.REASONING,
        }
        tier = tier_map.get(self.config.model_tier, RouterTier.CAPABLE)

        total_tokens = 0
        total_cost = 0.0

        for turn in range(self.config.max_turns):
            try:
                response = await self.engine.router.call(
                    messages=messages,
                    tier=tier,
                    tools=tools,
                )
            except Exception as e:
                return AgentResult(status="error", error=str(e))

            total_tokens += response.usage.total_tokens
            total_cost += response.usage.cost_usd

            if total_cost >= self.config.max_budget_usd:
                return AgentResult(
                    status="error",
                    response=messages[-1].get("content", "") if messages else "",
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost,
                    turns=turn + 1,
                    error="budget_exceeded",
                )

            if response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": response.tool_calls,
                })

                for tc_raw in response.tool_calls:
                    func = tc_raw.get("function", {})
                    tool_name = func.get("name", "")
                    args_str = func.get("arguments", "{}")
                    call_id = tc_raw.get("id", "")

                    tool = self.engine.tool_registry.get(tool_name)
                    if not tool:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": f"Unknown tool: {tool_name}",
                        })
                        continue

                    try:
                        args = tool.parse_arguments(args_str)
                        output = await tool.execute(**args)
                    except Exception as e:
                        output = f"Error: {type(e).__name__}: {e}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output,
                    })

                continue

            # 无工具调用 → 最终回答
            messages.append({"role": "assistant", "content": response.content})
            return AgentResult(
                status="completed",
                response=response.content,
                total_tokens=total_tokens,
                total_cost_usd=total_cost,
                turns=turn + 1,
            )

        return AgentResult(
            status="max_turns",
            response=messages[-1].get("content", "") if messages else "",
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            turns=self.config.max_turns,
        )
