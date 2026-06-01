"""模型路由 — 通过 LiteLLM 统一调用所有模型."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import litellm

from naumi_agent.config.settings import ModelConfig

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

_FALLBACK_CONTEXT = 128_000
_FALLBACK_MAX_OUTPUT = 4_096
_FALLBACK_COST = {"input": 3.0, "output": 15.0}


class ModelTier(StrEnum):
    FAST = "fast"
    CAPABLE = "capable"
    REASONING = "reasoning"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cache_tokens: int = 0


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
    tool_call: Any = None
    tool_call_started: bool = False
    thinking: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None


def _calculate_cost(
    model: str, input_tokens: int, output_tokens: int, rates: dict[str, float]
) -> float:
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
        self._info_cache: dict[str, dict[str, Any]] = {}

    # --- 模型元数据 ---

    def get_model_info(self, model: str) -> dict[str, Any]:
        """三级查找: config 覆盖 → litellm 内置 → fallback."""
        if model in self._info_cache:
            return self._info_cache[model]

        info = self._resolve_model_info(model)
        self._info_cache[model] = info
        return info

    def get_context_window(self, model: str) -> int:
        """获取模型上下文窗口大小（input token 上限）."""
        info = self.get_model_info(model)
        return info.get("max_input_tokens", _FALLBACK_CONTEXT)

    def get_max_output(self, model: str) -> int:
        """获取模型单次输出上限."""
        info = self.get_model_info(model)
        return info.get("max_output_tokens", _FALLBACK_MAX_OUTPUT)

    def get_cost_rates(self, model: str) -> dict[str, float]:
        """获取模型每百万 token 价格 {"input": x, "output": y}."""
        info = self.get_model_info(model)
        inp = info.get("input_cost_per_token")
        out = info.get("output_cost_per_token")
        if inp is not None and out is not None:
            return {"input": inp * 1_000_000, "output": out * 1_000_000}
        return info.get("cost_rates", _FALLBACK_COST)

    def _resolve_model_info(self, model: str) -> dict[str, Any]:
        # 1. 用户配置覆盖
        meta = self._config.model_info.get(model)
        if meta and meta.max_context:
            info: dict[str, Any] = {"max_input_tokens": meta.max_context}
            if meta.max_output:
                info["max_output_tokens"] = meta.max_output
            if (
                meta.input_cost_per_million
                and meta.output_cost_per_million
                and (meta.input_cost_per_million > 0 or meta.output_cost_per_million > 0)
            ):
                info["cost_rates"] = {
                    "input": meta.input_cost_per_million,
                    "output": meta.output_cost_per_million,
                }
            return info

        # 2. litellm 内置
        try:
            raw = litellm.get_model_info(model)
            return {
                "max_input_tokens": raw.get("max_input_tokens", _FALLBACK_CONTEXT),
                "max_output_tokens": raw.get("max_output_tokens", _FALLBACK_MAX_OUTPUT),
                "input_cost_per_token": raw.get("input_cost_per_token"),
                "output_cost_per_token": raw.get("output_cost_per_token"),
            }
        except Exception:
            logger.info("Model %s not in litellm registry, using fallback", model)

        # 3. Fallback
        return {
            "max_input_tokens": _FALLBACK_CONTEXT,
            "max_output_tokens": _FALLBACK_MAX_OUTPUT,
            "cost_rates": _FALLBACK_COST,
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

    def _resolve_max_tokens(self, model: str, requested: int | None) -> int:
        """确定 max_tokens: 调用方指定 > 配置值 > 模型输出上限."""
        if requested:
            return requested
        config_val = self._config.max_tokens
        model_limit = self.get_max_output(model)
        return min(config_val, model_limit)

    @staticmethod
    def _sanitize_messages(
        messages: list[dict[str, Any]],
        *,
        preserve_reasoning_content: bool = False,
    ) -> list[dict[str, Any]]:
        """清理消息列表中不合法的格式，避免 API 报错.

        - assistant + tool_calls: content 不能是空字符串，必须为 None
        - reasoning_content: 普通兼容模型不接受此字段，Kimi thinking 工具续接必须保留
        - 移除引用了不存在 tool_call_id 的 tool 消息
        - 裁剪末尾不完整的 assistant/tool 序列
        """
        sanitized: list[dict[str, Any]] = []
        for msg in messages:
            m = dict(msg)
            if m.get("role") == "assistant":
                if "tool_calls" in m and m.get("content") == "":
                    m["content"] = None
                if preserve_reasoning_content:
                    if "tool_calls" in m and "reasoning_content" not in m:
                        m["reasoning_content"] = ""
                else:
                    m.pop("reasoning_content", None)
            sanitized.append(m)

        # 收集所有有效的 tool_call_id
        valid_tool_call_ids = {
            tc.get("id")
            for msg in sanitized
            if msg.get("role") == "assistant" and "tool_calls" in msg
            for tc in msg.get("tool_calls", [])
            if isinstance(tc, dict) and tc.get("id")
        }

        # 移除引用了无效 tool_call_id 的 tool 消息
        sanitized = [
            msg
            for msg in sanitized
            if msg.get("role") != "tool" or msg.get("tool_call_id") in valid_tool_call_ids
        ]

        # Trim trailing incomplete assistant sequences, but keep complete
        # assistant(tool_calls) -> tool(result) pairs. A valid ReAct turn often
        # ends with tool messages right before the next LLM call.
        while sanitized:
            last = sanitized[-1]
            role = last.get("role", "")
            if role == "tool":
                if ModelRouter._has_complete_trailing_tool_results(sanitized):
                    break
                sanitized.pop()
                continue
            # Remove trailing assistant with tool_calls but no tool responses
            if role == "assistant" and "tool_calls" in last:
                sanitized.pop()
                continue
            # Remove trailing assistant with no content (interrupted)
            if role == "assistant" and not last.get("content"):
                sanitized.pop()
                continue
            break

        return sanitized

    @staticmethod
    def _has_complete_trailing_tool_results(messages: list[dict[str, Any]]) -> bool:
        """Return true when trailing tool messages complete the prior tool_calls."""
        start = len(messages) - 1
        while start >= 0 and messages[start].get("role") == "tool":
            start -= 1

        if start < 0:
            return False

        assistant_msg = messages[start]
        if assistant_msg.get("role") != "assistant" or not assistant_msg.get("tool_calls"):
            return False

        expected_ids = {
            tc.get("id")
            for tc in assistant_msg.get("tool_calls", [])
            if isinstance(tc, dict) and tc.get("id")
        }
        if not expected_ids:
            return False

        actual_ids = {
            msg.get("tool_call_id")
            for msg in messages[start + 1 :]
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }
        return expected_ids.issubset(actual_ids)

    def _should_preserve_reasoning_content(
        self,
        model: str,
        thinking: dict[str, str] | None,
    ) -> bool:
        """Kimi thinking requires prior assistant tool-call messages to retain reasoning."""
        if thinking is not None:
            return thinking.get("type") != "disabled"
        return self._uses_kimi_protocol(model)

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
        thinking: dict[str, str] | None = None,
    ) -> ModelResponse:
        """非流式调用."""
        resolved = model or self.resolve_model(tier)
        preserve_reasoning = self._should_preserve_reasoning_content(
            resolved, thinking,
        )
        kwargs: dict[str, Any] = {
            "model": resolved,
            "messages": self._sanitize_messages(
                messages,
                preserve_reasoning_content=preserve_reasoning,
            ),
            "max_tokens": self._resolve_max_tokens(resolved, max_tokens),
            "temperature": temperature if temperature is not None else self._config.temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        kwargs.update(self._base_kwargs())

        # Kimi k2.6 thinking support
        if thinking is not None:
            self._apply_thinking(kwargs, thinking)
        elif self._is_kimi_thinking_model(resolved):
            self._apply_thinking(kwargs, {"type": "enabled"})
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
        thinking: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用，yield StreamChunk."""
        resolved = model or self.resolve_model(tier)
        preserve_reasoning = self._should_preserve_reasoning_content(
            resolved, thinking,
        )
        kwargs: dict[str, Any] = {
            "model": resolved,
            "messages": self._sanitize_messages(
                messages,
                preserve_reasoning_content=preserve_reasoning,
            ),
            "max_tokens": self._resolve_max_tokens(resolved, max_tokens),
            "temperature": temperature if temperature is not None else self._config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        kwargs.update(self._base_kwargs())

        # Kimi k2.6 thinking support
        if thinking is not None:
            self._apply_thinking(kwargs, thinking)
        elif self._is_kimi_thinking_model(resolved):
            self._apply_thinking(kwargs, {"type": "enabled"})
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

            # 思维链 — check multiple fields for thinking content
            thinking_text = ""
            if getattr(delta, "reasoning_content", None):
                thinking_text = delta.reasoning_content
            elif getattr(delta, "thinking_blocks", None):
                for block in delta.thinking_blocks:
                    if isinstance(block, dict) and block.get("type") == "thinking":
                        thinking_text += block.get("thinking", "")
                    elif hasattr(block, "get") and block.get("type") == "thinking":
                        thinking_text += block.get("thinking", "")

            # 文本内容
            token = delta.content or ""

            # 工具调用
            tool_call_started = bool(delta.tool_calls)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {
                                "name": "",
                                "arguments": "",
                            },
                        }
                    entry = collected_tool_calls[idx]
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            entry["function"]["arguments"] += tc.function.arguments

            tool_call = None
            if finish_reason == "tool_calls" and collected_tool_calls:
                tool_call = collected_tool_calls

            if token or thinking_text or tool_call or tool_call_started or finish_reason:
                yield StreamChunk(
                    token=token,
                    thinking=thinking_text,
                    tool_call=tool_call,
                    tool_call_started=tool_call_started,
                    finish_reason=finish_reason,
                    usage=None,
                )

        # 最终 usage
        if final_usage:
            yield StreamChunk(usage=final_usage, finish_reason="stop")

    def _is_kimi_thinking_model(self, model: str) -> bool:
        """Check if the model is a kimi thinking model that supports the thinking param."""
        model_lower = model.lower()
        return "kimi-k2" in model_lower or "kimi-latest" in model_lower

    def _uses_kimi_protocol(self, model: str) -> bool:
        """Check whether requests go through Kimi's OpenAI-compatible protocol."""
        model_lower = model.lower()
        api_base = (self._config.api_base or "").lower()
        return "kimi" in model_lower or "kimi.com" in api_base

    def _apply_thinking(
        self, kwargs: dict[str, Any], thinking: dict[str, str],
    ) -> None:
        """Apply thinking parameter for models that support it.

        For kimi-k2.6 via OpenAI-compatible API, thinking is passed
        via extra_body since the OpenAI SDK doesn't natively support it.
        """
        existing = kwargs.get("extra_body", {})
        existing["thinking"] = thinking
        kwargs["extra_body"] = existing
        logger.debug("Applied thinking=%s for model=%s", thinking, kwargs["model"])

    def _extract_tool_calls(self, raw: list[litellm.utils.Function] | None) -> list[dict[str, Any]]:
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

    def _build_usage(self, usage: litellm.utils.Usage | None, model: str) -> TokenUsage:
        if not usage:
            return TokenUsage()
        inp = usage.prompt_tokens or 0
        out = usage.completion_tokens or 0
        # Extract cache tokens from prompt_tokens_details (OpenAI-compatible)
        cache = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            cache = getattr(details, "cached_tokens", 0) or 0
        rates = self.get_cost_rates(model)
        return TokenUsage(
            input_tokens=inp,
            output_tokens=out,
            total_tokens=inp + out,
            cost_usd=round(_calculate_cost(model, inp, out, rates), 6),
            cache_tokens=cache,
        )
