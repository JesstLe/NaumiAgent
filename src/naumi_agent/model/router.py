"""模型路由 — 通过 LiteLLM 统一调用所有模型."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import litellm

from naumi_agent.config.settings import ModelConfig, ModelMeta
from naumi_agent.model.catalog import (
    APIFormat,
    ProviderCatalog,
    ProviderModelSpec,
    ProviderSpec,
)
from naumi_agent.model.discovery import (
    ModelDiscoveryService,
    ProviderModelListing,
)
from naumi_agent.model.provider_runtime import (
    ProviderModelRegistration,
    ProviderRuntimeError,
    build_provider_transport,
)
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortError,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
    reasoning_effort_values,
)
from naumi_agent.model.targets import (
    ModelResolutionError,
    ResolvedModelTarget,
    resolve_model_target,
)

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

_FALLBACK_CONTEXT = 128_000
_FALLBACK_MAX_OUTPUT = 4_096
_FALLBACK_COST = {"input": 3.0, "output": 15.0}
_MISSING_TOOL_RESULT_PLACEHOLDER = "[工具调用结果缺失 — 会话恢复时未能找到对应结果]"


class ModelTier(StrEnum):
    FAST = "fast"
    CAPABLE = "capable"
    REASONING = "reasoning"


@dataclass(frozen=True)
class ModelRuntimeIdentity:
    """Safe, side-effect-free identity for one configured model target."""

    requested_model: str
    canonical_model: str
    upstream_model: str
    provider: str
    api_format: str
    source: str


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
    tool_call_snapshot: Any = None
    tool_call_started: bool = False
    thinking: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None


def _calculate_cost(
    model: str, input_tokens: int, output_tokens: int, rates: dict[str, float]
) -> float:
    return input_tokens * rates["input"] / 1_000_000 + output_tokens * rates["output"] / 1_000_000


def _copy_tool_call_snapshot(
    tool_calls: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Return a detached snapshot of streamed tool-call arguments."""
    return {
        idx: {
            "id": call.get("id", ""),
            "type": call.get("type", "function"),
            "function": {
                "name": str((call.get("function") or {}).get("name", "")),
                "arguments": str((call.get("function") or {}).get("arguments", "")),
            },
        }
        for idx, call in tool_calls.items()
    }


class ModelRouter:
    """统一模型调用入口."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        catalog: ProviderCatalog | None = None,
        discovery_service: ModelDiscoveryService | None = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        if discovery_service is not None:
            self._discovery = discovery_service
        elif catalog is not None:
            self._discovery = ModelDiscoveryService(catalog)
        else:
            self._discovery = None
        self._dynamic_models: dict[str, Mapping[str, ProviderModelSpec]] = {}
        self._tier_map: dict[ModelTier, str] = {
            ModelTier.FAST: config.fast_model,
            ModelTier.CAPABLE: config.default_model,
            ModelTier.REASONING: config.reasoning_model,
        }
        self._info_cache: dict[str, dict[str, Any]] = {}
        self._registered_transport_models: set[str] = set()
        self._transport_model_registration_lock = threading.Lock()
        self._registered_native_stream_models: set[str] = set()
        self._native_stream_registration_lock = threading.Lock()
        self._runtime_reasoning_effort: ReasoningEffortSetting | None = None

    # --- 模型元数据 ---

    def get_model_info(self, model: str) -> dict[str, Any]:
        """逐字段合并元数据：请求 config > canonical config > catalog > LiteLLM > fallback."""
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
        target = self.resolve_target(model)
        info: dict[str, Any] = {
            "max_input_tokens": _FALLBACK_CONTEXT,
            "max_output_tokens": _FALLBACK_MAX_OUTPUT,
            "input_cost_per_token": _FALLBACK_COST["input"] / 1_000_000,
            "output_cost_per_token": _FALLBACK_COST["output"] / 1_000_000,
        }

        try:
            raw = litellm.get_model_info(target.upstream_model)
            for field_name in (
                "max_input_tokens",
                "max_output_tokens",
                "input_cost_per_token",
                "output_cost_per_token",
            ):
                if raw.get(field_name) is not None:
                    info[field_name] = raw[field_name]
        except Exception:
            logger.info(
                "Model %s not in litellm registry, using fallback",
                target.upstream_model,
            )

        if target.model is not None:
            if target.model.max_context is not None:
                info["max_input_tokens"] = target.model.max_context
            if target.model.max_output is not None:
                info["max_output_tokens"] = target.model.max_output

        canonical_meta = self._config.model_info.get(target.canonical_model)
        if canonical_meta is not None:
            self._merge_config_meta(info, canonical_meta)
        if target.requested_model != target.canonical_model:
            requested_meta = self._config.model_info.get(target.requested_model)
            if requested_meta is not None:
                self._merge_config_meta(info, requested_meta)
        return info

    @staticmethod
    def _merge_config_meta(info: dict[str, Any], meta: ModelMeta) -> None:
        if meta.max_context is not None:
            info["max_input_tokens"] = meta.max_context
        if meta.max_output is not None:
            info["max_output_tokens"] = meta.max_output
        if meta.input_cost_per_million is not None:
            info["input_cost_per_token"] = meta.input_cost_per_million / 1_000_000
        if meta.output_cost_per_million is not None:
            info["output_cost_per_token"] = meta.output_cost_per_million / 1_000_000

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

    def resolve_target(self, model: str) -> ResolvedModelTarget:
        return resolve_model_target(
            model,
            provider=self._config.provider,
            catalog=self._catalog,
            dynamic_models=self._dynamic_models,
        )

    def get_reasoning_effort_status(
        self,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        """Resolve capability and selected effort for one model without network I/O."""
        requested = model or self.resolve_model(ModelTier.CAPABLE)
        try:
            target = self.resolve_target(requested)
        except ModelResolutionError:
            target = None

        supported: tuple[ReasoningEffort, ...] = ()
        default: ReasoningEffort | None = None
        if target is not None and target.model is not None:
            supported = target.model.reasoning_efforts
            default = target.model.default_reasoning_effort

        canonical = target.canonical_model if target is not None else requested
        metadata = self._reasoning_metadata(canonical, requested)
        for meta in metadata:
            if meta.reasoning_efforts is not None:
                supported = meta.reasoning_efforts
                default = meta.default_reasoning_effort

        if self._runtime_reasoning_effort is not None:
            effective = self._runtime_reasoning_effort
            source = "runtime"
        else:
            selected = next(
                (
                    meta.reasoning_effort
                    for meta in reversed(metadata)
                    if meta.reasoning_effort is not None
                ),
                None,
            )
            if selected is not None:
                effective = selected
                source = "model"
            elif self._config.reasoning_effort is not ReasoningEffortSetting.AUTO:
                effective = self._config.reasoning_effort
                source = "global"
            else:
                effective = ReasoningEffortSetting.AUTO
                source = "auto"

        warning = self._reasoning_effort_warning(
            requested,
            effective,
            supported,
        )
        return ReasoningEffortStatus(
            model=requested,
            effective=effective,
            source=source,
            supported=supported,
            default=default,
            warning=warning,
        )

    def set_reasoning_effort(
        self,
        value: str | ReasoningEffortSetting,
        *,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        """Set and validate one process-local effort override."""
        try:
            selected = (
                value
                if isinstance(value, ReasoningEffortSetting)
                else ReasoningEffortSetting(value.strip().lower())
            )
        except (AttributeError, ValueError) as exc:
            raise ReasoningEffortError(
                "无效的思考强度；可选值：auto、"
                f"{reasoning_effort_values()}。"
            ) from exc
        previous = self._runtime_reasoning_effort
        self._runtime_reasoning_effort = selected
        status = self.get_reasoning_effort_status(model)
        if selected is not ReasoningEffortSetting.AUTO and status.warning:
            self._runtime_reasoning_effort = previous
            raise ReasoningEffortError(status.warning)
        return status

    def reset_reasoning_effort(
        self,
        *,
        model: str | None = None,
    ) -> ReasoningEffortStatus:
        """Clear the process-local effort override and return config resolution."""
        self._runtime_reasoning_effort = None
        return self.get_reasoning_effort_status(model)

    def _reasoning_metadata(
        self,
        canonical_model: str,
        requested_model: str,
    ) -> tuple[ModelMeta, ...]:
        metadata: list[ModelMeta] = []
        canonical = self._config.model_info.get(canonical_model)
        if canonical is not None:
            metadata.append(canonical)
        if requested_model != canonical_model:
            requested = self._config.model_info.get(requested_model)
            if requested is not None:
                metadata.append(requested)
        return tuple(metadata)

    @staticmethod
    def _reasoning_effort_warning(
        model: str,
        effective: ReasoningEffortSetting,
        supported: tuple[ReasoningEffort, ...],
    ) -> str | None:
        explicit = effective.explicit
        if explicit is None:
            return None
        if explicit in supported:
            return None
        if not supported:
            return (
                f'模型 "{model}" 未声明可选思考强度，无法发送 "{effective.value}"。'
            )
        available = "、".join(value.value for value in supported)
        return (
            f'模型 "{model}" 不支持思考强度 "{effective.value}"；'
            f"可选值：{available}。"
        )

    @staticmethod
    def _apply_reasoning_effort(
        kwargs: dict[str, Any],
        status: ReasoningEffortStatus,
        *,
        thinking: dict[str, str] | None,
    ) -> bool:
        """Validate and apply explicit effort; return whether one was applied."""
        explicit = status.effective.explicit
        if explicit is None:
            return False
        if status.warning:
            raise ReasoningEffortError(status.warning)
        if thinking is not None:
            raise ReasoningEffortError(
                "模型思考强度与显式 thinking 参数不能同时使用；"
                "请将 /effort 设为 auto 或移除 thinking 参数。"
            )
        kwargs["reasoning_effort"] = explicit.value
        kwargs.pop("temperature", None)
        return True

    async def list_available_models(
        self,
        provider_id: str | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[ProviderModelListing, ...]:
        """Discover visible models and atomically refresh the runtime overlay."""
        if self._discovery is None:
            return ()
        if provider_id is None:
            listings = await self._discovery.list_all(refresh=refresh)
        else:
            listing = await self._discovery.list_provider(
                provider_id.strip().lower(),
                refresh=refresh,
            )
            listings = (listing,)
        for listing in listings:
            dynamic = {
                model.id: model.to_provider_model_spec()
                for model in listing.models
                if model.source == "discovered"
            }
            self._dynamic_models[listing.provider_id] = MappingProxyType(dynamic)
        return listings

    def get_runtime_identity(self, model: str) -> ModelRuntimeIdentity:
        """Return display-safe routing facts without resolving credentials or I/O."""
        try:
            target = self.resolve_target(model)
        except ModelResolutionError:
            pending = self._pending_runtime_identity(model)
            if pending is None:
                raise
            return pending
        if target.source == "legacy":
            pending = self._pending_runtime_identity(model)
            if pending is not None:
                return pending
        provider = target.provider
        if provider is None:
            provider_id = self._config.provider or ""
            api_format = "legacy"
        else:
            provider_id = provider.id
            api_format = provider.api_format.value if provider.api_format is not None else ""
        return ModelRuntimeIdentity(
            requested_model=target.requested_model,
            canonical_model=target.canonical_model,
            upstream_model=target.upstream_model,
            provider=provider_id,
            api_format=api_format,
            source=target.source,
        )

    def _pending_runtime_identity(self, model: str) -> ModelRuntimeIdentity | None:
        candidate = self._discovery_candidate(model)
        if candidate is None:
            return None
        provider, model_id = candidate
        requested = model.strip()
        return ModelRuntimeIdentity(
            requested_model=requested,
            canonical_model=f"{provider.id}/{model_id}",
            upstream_model=model_id,
            provider=provider.id,
            api_format=provider.api_format.value if provider.api_format else "",
            source="catalog_pending",
        )

    def _discovery_candidate(self, model: str) -> tuple[ProviderSpec, str] | None:
        if self._catalog is None:
            return None
        requested = model.strip()
        if not requested:
            return None
        prefix, separator, alias = requested.partition("/")
        selected = self._catalog.providers.get(prefix.lower()) if separator else None
        if selected is not None:
            if self._can_discover_model(selected, alias):
                return selected, alias
            return None
        active = self._catalog.providers.get(
            (self._config.provider or "").strip().lower()
        )
        if active is None or not self._can_discover_model(active, requested):
            return None
        return active, requested

    def _can_discover_model(self, provider: ProviderSpec, model_id: str) -> bool:
        if not provider.discovery.enabled or not model_id:
            return False
        if model_id in provider.models:
            return False
        if model_id in self._dynamic_models.get(provider.id, {}):
            return False
        if model_id in provider.blacklist:
            return False
        return not provider.whitelist or model_id in provider.whitelist

    def _resolve_transport(self, model: str) -> tuple[str, dict[str, Any]]:
        """Resolve one public model name into explicit LiteLLM transport args."""
        target = self.resolve_target(model)
        return self._transport_for_target(target, requested_model=model)

    async def _resolve_transport_async(self, model: str) -> tuple[str, dict[str, Any]]:
        """Resolve transport, proving unknown catalog models through discovery."""
        candidate = self._discovery_candidate(model)
        try:
            target = self.resolve_target(model)
        except ModelResolutionError:
            if candidate is None:
                raise
            target = await self._discover_target(model, candidate=candidate)
        else:
            if target.source == "legacy" and candidate is not None:
                target = await self._discover_target(model, candidate=candidate)
        return self._transport_for_target(target, requested_model=model)

    async def _discover_target(
        self,
        model: str,
        *,
        candidate: tuple[ProviderSpec, str],
    ) -> ResolvedModelTarget:
        provider, model_id = candidate
        if self._discovery is None:
            raise ModelResolutionError(
                f'provider "{provider.id}" 无法执行模型发现。'
            )
        listings = await self.list_available_models(provider.id)
        listing = listings[0]
        try:
            return self.resolve_target(model)
        except ModelResolutionError:
            detail = f"（{listing.warning}）" if listing.warning else ""
            raise ModelResolutionError(
                f'provider "{provider.id}" 未发现模型 "{model_id}"。{detail}'
            ) from None

    def _transport_for_target(
        self,
        target: ResolvedModelTarget,
        *,
        requested_model: str,
    ) -> tuple[str, dict[str, Any]]:
        if target.source == "legacy":
            return requested_model, self._base_kwargs()
        if self._catalog is None:
            raise ProviderRuntimeError("catalog 模型缺少 catalog 来源。")
        provider = target.provider
        if provider is None:
            raise ProviderRuntimeError("catalog 模型缺少 provider 定义。")

        transport = build_provider_transport(
            target,
            catalog_source=self._catalog.source,
        )
        self._ensure_transport_model_registration(
            provider.id,
            transport.registration,
        )
        kwargs = dict(transport.kwargs)
        if "extra_headers" in kwargs:
            kwargs["extra_headers"] = dict(kwargs["extra_headers"])
        return transport.model, kwargs

    def _ensure_transport_model_registration(
        self,
        provider_id: str,
        registration: ProviderModelRegistration | None,
    ) -> None:
        """Register transport-declared LiteLLM capabilities once per Router."""
        if registration is None:
            return

        with self._transport_model_registration_lock:
            if registration.model in self._registered_transport_models:
                return
            try:
                litellm.register_model(
                    {
                        registration.model: dict(registration.metadata),
                    }
                )
            except Exception:
                raise ProviderRuntimeError(
                    f'provider "{provider_id}" 无法注册模型能力。'
                ) from None
            self._registered_transport_models.add(registration.model)

    def _ensure_native_responses_streaming(self, model: str) -> None:
        """Prevent LiteLLM from fake-streaming unknown catalog Responses models."""
        target = self.resolve_target(model)
        provider = target.provider
        if provider is None or provider.api_format is not APIFormat.OPENAI_RESPONSES:
            return

        registry_model = f"openai/{target.upstream_model}"
        with self._native_stream_registration_lock:
            if registry_model in self._registered_native_stream_models:
                return
            try:
                litellm.register_model(
                    {
                        registry_model: {
                            "litellm_provider": "openai",
                            "mode": "responses",
                            "supports_native_streaming": True,
                        }
                    }
                )
            except Exception:
                raise ProviderRuntimeError(
                    f'provider "{provider.id}" 无法启用 Responses 原生流式。'
                ) from None
            self._registered_native_stream_models.add(registry_model)

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
        sanitized = ModelRouter._repair_middle_tool_call_gaps(sanitized)

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
    def _repair_middle_tool_call_gaps(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fill historical tool-call gaps while leaving trailing partial turns trimable."""
        repaired: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                if msg.get("role") != "tool":
                    repaired.append(msg)
                i += 1
                continue

            expected_ids = [
                tc.get("id")
                for tc in msg.get("tool_calls", [])
                if isinstance(tc, dict) and tc.get("id")
            ]
            expected_set = set(expected_ids)
            tool_results: dict[str, dict[str, Any]] = {}
            ordered_tool_messages: list[dict[str, Any]] = []

            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msg = messages[j]
                tc_id = tool_msg.get("tool_call_id")
                if tc_id in expected_set and tc_id not in tool_results:
                    tool_results[tc_id] = tool_msg
                    ordered_tool_messages.append(tool_msg)
                j += 1

            missing_ids = [
                tc_id for tc_id in expected_ids if tc_id not in tool_results
            ]
            if expected_ids and missing_ids and j < len(messages):
                repaired.append(msg)
                for tc_id in expected_ids:
                    repaired.append(
                        tool_results.get(tc_id)
                        or {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": _MISSING_TOOL_RESULT_PLACEHOLDER,
                        }
                    )
            else:
                repaired.append(msg)
                repaired.extend(ordered_tool_messages)

            i = j

        return repaired

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
        transport_model, transport_kwargs = await self._resolve_transport_async(
            resolved
        )
        effort_status = self.get_reasoning_effort_status(resolved)
        preserve_reasoning = self._should_preserve_reasoning_content(
            resolved, thinking,
        )
        kwargs: dict[str, Any] = {
            "model": transport_model,
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

        kwargs.update(transport_kwargs)

        explicit_effort = self._apply_reasoning_effort(
            kwargs,
            effort_status,
            thinking=thinking,
        )

        # Kimi k2.6 thinking support
        if thinking is not None:
            self._apply_thinking(kwargs, thinking)
        elif not explicit_effort and self._is_kimi_thinking_model(resolved):
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
        transport_model, transport_kwargs = await self._resolve_transport_async(
            resolved
        )
        effort_status = self.get_reasoning_effort_status(resolved)
        self._ensure_native_responses_streaming(resolved)
        preserve_reasoning = self._should_preserve_reasoning_content(
            resolved, thinking,
        )
        kwargs: dict[str, Any] = {
            "model": transport_model,
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

        kwargs.update(transport_kwargs)

        explicit_effort = self._apply_reasoning_effort(
            kwargs,
            effort_status,
            thinking=thinking,
        )

        # Kimi k2.6 thinking support
        if thinking is not None:
            self._apply_thinking(kwargs, thinking)
        elif not explicit_effort and self._is_kimi_thinking_model(resolved):
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

            tool_call_snapshot = None
            if delta.tool_calls and collected_tool_calls:
                tool_call_snapshot = _copy_tool_call_snapshot(collected_tool_calls)

            tool_call = None
            if finish_reason == "tool_calls" and collected_tool_calls:
                tool_call = _copy_tool_call_snapshot(collected_tool_calls)

            if (
                token
                or thinking_text
                or tool_call
                or tool_call_snapshot
                or tool_call_started
                or finish_reason
            ):
                yield StreamChunk(
                    token=token,
                    thinking=thinking_text,
                    tool_call=tool_call,
                    tool_call_snapshot=tool_call_snapshot,
                    tool_call_started=tool_call_started,
                    finish_reason=finish_reason,
                    usage=None,
                )

        # 最终 usage
        if final_usage:
            yield StreamChunk(usage=final_usage, finish_reason="stop")

    def _is_kimi_thinking_model(self, model: str) -> bool:
        """Check if the model is a kimi thinking model that supports the thinking param."""
        identities, _api_base = self._model_protocol_identity(model)
        return any(
            "kimi-k2" in identity or "kimi-latest" in identity
            for identity in identities
        )

    def _uses_kimi_protocol(self, model: str) -> bool:
        """Check whether requests go through Kimi's OpenAI-compatible protocol."""
        identities, api_base = self._model_protocol_identity(model)
        return any("kimi" in identity for identity in identities) or "kimi.com" in api_base

    def _model_protocol_identity(self, model: str) -> tuple[tuple[str, ...], str]:
        """Return public/upstream identities and selected base URL for protocol checks."""
        identities = [model.lower()]
        api_base = (self._config.api_base or "").lower()
        try:
            target = self.resolve_target(model)
        except ModelResolutionError:
            return tuple(identities), api_base
        upstream = target.upstream_model.lower()
        if upstream not in identities:
            identities.append(upstream)
        if target.provider is not None and target.provider.base_url:
            api_base = target.provider.base_url.lower()
        return tuple(identities), api_base

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
