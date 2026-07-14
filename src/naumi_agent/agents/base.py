"""Agent 基类与配置."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from naumi_agent.hooks import HookContext, HookPoint
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
DEFAULT_AGENT_MAX_TURNS = 50


class AgentCapability(StrEnum):
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
    max_turns: int = DEFAULT_AGENT_MAX_TURNS
    max_budget_usd: float | None = None
    timeout_seconds: float = 300.0
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
        "browser_goto",
        "browser_click",
        "browser_type",
        "browser_observe",
        "browser_screenshot",
    ],
    AgentCapability.WEB_SEARCH: ["web_search", "web_fetch"],
    AgentCapability.SHELL_EXEC: ["bash_run"],
}


def resolve_agent_tool_names(
    config: AgentConfig,
    available_names: list[str] | tuple[str, ...] | set[str],
) -> tuple[str, ...]:
    """Resolve stable registered tools without instantiating an Agent."""
    configured = set(config.tools)
    for capability in config.capabilities:
        configured.update(_CAPABILITY_TOOLS.get(capability, ()))
    return tuple(sorted(configured & set(available_names)))


class BaseAgent:
    """所有子 Agent 的基类."""

    def __init__(self, config: AgentConfig, engine: AgentEngine) -> None:
        self.config = config
        self.engine = engine
        self._tool_names = self._resolve_tools()
        self._permission_checker = self._build_permission_checker()

    def _build_permission_checker(self) -> PermissionChecker:
        """Build the sub-agent permission boundary from its config."""
        app_config = getattr(self.engine, "_config", None)
        safety_config = getattr(app_config, "safety", None)
        allowed_dirs = list(getattr(safety_config, "allowed_dirs", []) or [])
        workspace_root = str(getattr(self.engine, "workspace_root", "") or "")
        if workspace_root:
            allowed_dirs.append(workspace_root)
        return PermissionChecker(
            PermissionMode(self.config.permission_level),
            allowed_dirs=allowed_dirs or None,
            workspace_root=workspace_root or None,
        )

    def _resolve_tools(self) -> list[str]:
        """根据能力和显式配置解析工具列表."""
        return list(resolve_agent_tool_names(
            self.config,
            self.engine.tool_registry.names,
        ))

    def _get_tool_schemas(self) -> list[dict[str, Any]]:
        """获取允许的工具 schema."""
        tools = []
        for name in self._tool_names:
            tool = self.engine.tool_registry.get(name)
            if tool:
                tools.append(tool.to_openai_tool())
        return tools

    async def execute(
        self,
        task: str,
        context: str = "",
        event_callback: EventCallback | None = None,
    ) -> AgentResult:
        """执行子任务."""
        from naumi_agent.model.router import ModelTier

        hooks = self.engine.hooks

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

        total_tokens = 0
        total_cost = 0.0

        for turn in range(self.config.max_turns):
            max_budget_usd = self.config.max_budget_usd
            if max_budget_usd is not None and total_cost >= max_budget_usd:
                return AgentResult(
                    status="error",
                    response=messages[-1].get("content", "") if messages else "",
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost,
                    turns=turn,
                    error=(
                        "子 Agent 预算已耗尽："
                        f"{total_cost:.6f} / {max_budget_usd:.6f} USD"
                    ),
                )

            try:
                await hooks.fire(HookContext(
                    point=HookPoint.LLM_CALL_START,
                    data={"turn": turn + 1, "agent": self.config.name},
                    agent_name=self.config.name,
                ))
                response = await self.engine.router.call(
                    messages=messages,
                    tier=tier,
                    tools=tools,
                )
                total_tokens += response.usage.total_tokens
                total_cost += response.usage.cost_usd
                await hooks.fire(HookContext(
                    point=HookPoint.LLM_CALL_END,
                    data={
                        "turn": turn + 1,
                        "model": response.model,
                        "total_tokens": response.usage.total_tokens,
                        "agent": self.config.name,
                    },
                    agent_name=self.config.name,
                ))
            except Exception as e:
                return AgentResult(status="error", error=str(e))

            if response.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or None,
                        "tool_calls": response.tool_calls,
                    }
                )

                for tc_raw in response.tool_calls:
                    func = tc_raw.get("function", {})
                    tool_name = func.get("name", "")
                    args_str = func.get("arguments", "{}")
                    call_id = tc_raw.get("id", "")

                    tool = self.engine.tool_registry.get(tool_name)
                    if not tool:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": f"未知工具：{tool_name}",
                            }
                        )
                        continue

                    try:
                        args = tool.parse_arguments(args_str)
                    except ValueError as e:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": str(e),
                            }
                        )
                        continue

                    decision = self._permission_checker.check(tool_name, args, tool=tool)
                    if not decision.allowed:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": f"子 Agent 权限拒绝：{decision.reason}",
                            }
                        )
                        continue

                    hook_ctx = await hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={"tool_name": tool_name, "arguments": args_str},
                        agent_name=self.config.name,
                    ))
                    if hook_ctx.should_abort:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": (
                                    "被 Hook 中止："
                                    f"{hook_ctx.data.get('abort_reason', '')}"
                                ),
                            }
                        )
                        continue

                    result = await self.engine.execute_tool(
                        ToolCall(id=call_id, name=tool_name, arguments=args_str),
                        on_event=event_callback,
                        agent_name=self.config.name,
                    )
                    output = result.content

                    await hooks.fire(HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": tool_name,
                            "agent": self.config.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "permission_bubble": True,
                        },
                        agent_name=self.config.name,
                    ))

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": output,
                        }
                    )

                continue

            # 无工具调用 → 最终回答
            await hooks.fire(HookContext(
                point=HookPoint.MESSAGE_OUT,
                data={"agent": self.config.name, "content_length": len(response.content or "")},
                agent_name=self.config.name,
            ))
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
