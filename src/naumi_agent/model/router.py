"""模型路由 — 通过 LiteLLM 统一调用所有模型."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

import litellm

from naumi_agent.config.settings import ModelConfig

logger = logging.getLogger(__name__)

# 关闭 LiteLLM 默认的冗长日志
litellm.suppress_debug_info = True


class ModelTier(str, Enum):
    FAST = "fast"
    CAPABLE = "capable"
    REASONING = "reasoning"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ModelResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str = ""
    reasoning_content: str = ""


@dataclass(frozen=True)
class StreamChunk:
    token: str = ""
    tool_call: dict[str, Any] | None = None
    thinking: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None


# 每百万 token 价格 (USD)
_COST_TABLE: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _COST_TABLE.get(model, {"input": 3.0, "output": 15.0})
    return input_tokens * rates["input"] / 1_000_000 + output_tokens * rates["output"] / 1_000_000


class ModelRouter:
    """统一模型调用入口."""

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._tier_map: dict[ModelTier, str] = {
            ModelTier.FAST: config.fast_model,
            ModelTier.CAPABLE: config.default_model,
            ModelTier.REASONING: config.reasoning_model,
        }

    def _base_kwargs(self) -> dict[str, Any]:
        """构建底层 API 调用的公共参数（api_base、api_key）."""
        kw: dict[str, Any] = {}
        if self._config.api_base:
            kw["api_base"] = self._config.api_base
        if self._config.api_key:
            kw["api_key"] = self._config.api_key
        if self._config.api_base and "kimi.com" in self._config.api_base:
            kw["extra_headers"] = {"User-Agent": "Kilo-Code/1.0"}
        return kw

    def resolve_model(self, tier: ModelTier | str) -> str:
        if isinstance(tier, str):
            tier = ModelTier(tier)
        return self._tier_map[tier]

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tier: ModelTier = ModelTier.CAPABLE,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: str | dict | None = None,
    ) -> ModelResponse:
        """非流式调用."""
        resolved = model or self.resolve_model(tier)
        kwargs: dict[str, Any] = {
            "model": resolved,
            "messages": messages,
            "max_tokens": max_tokens or self._config.max_tokens,
            "temperature": temperature if temperature is not None else self._config.temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        kwargs.update(self._base_kwargs())
        response = await litellm.acompletion(**kwargs)

        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = self._extract_tool_calls(choice.message.tool_calls)
        usage = self._build_usage(response.usage, resolved)
        reasoning = getattr(choice.message, "reasoning_content", None) or ""

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=resolved,
            finish_reason=choice.finish_reason or "",
            reasoning_content=reasoning,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tier: ModelTier = ModelTier.CAPABLE,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用，yield StreamChunk."""
        resolved = model or self.resolve_model(tier)
        kwargs: dict[str, Any] = {
            "model": resolved,
            "messages": messages,
            "max_tokens": max_tokens or self._config.max_tokens,
            "temperature": temperature if temperature is not None else self._config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        kwargs.update(self._base_kwargs())
        response = await litellm.acompletion(**kwargs)

        collected_tool_calls: dict[int, dict[str, Any]] = {}
        final_usage: TokenUsage | None = None

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                final_usage = self._build_usage(chunk.usage, resolved)

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            # 思维链
            thinking = ""
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                thinking = delta.reasoning_content

            # 文本内容
            token = delta.content or ""

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = collected_tool_calls[idx]
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] = tc.function.name
                        if tc.function.arguments:
                            entry["arguments"] += tc.function.arguments

            tool_call = None
            if finish_reason == "tool_calls" and collected_tool_calls:
                tool_call = collected_tool_calls  # type: ignore[assignment]

            if token or thinking or tool_call or finish_reason:
                yield StreamChunk(
                    token=token,
                    thinking=thinking,
                    tool_call=tool_call,
                    finish_reason=finish_reason,
                    usage=None,
                )

        # 最终 usage
        if final_usage:
            yield StreamChunk(usage=final_usage, finish_reason="stop")

    def _extract_tool_calls(
        self, raw: list[litellm.utils.Function] | None
    ) -> list[dict[str, Any]]:
        if not raw:
            return []
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in raw
        ]

    def _build_usage(
        self, usage: litellm.utils.Usage | None, model: str
    ) -> TokenUsage:
        if not usage:
            return TokenUsage()
        inp = usage.prompt_tokens or 0
        out = usage.completion_tokens or 0
        return TokenUsage(
            input_tokens=inp,
            output_tokens=out,
            total_tokens=inp + out,
            cost_usd=round(_calculate_cost(model, inp, out), 6),
        )
