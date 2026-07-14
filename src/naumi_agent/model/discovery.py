"""Bounded, cached model discovery for catalog-backed providers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

import httpx

from naumi_agent.model.catalog import (
    APIFormat,
    ProviderCatalog,
    ProviderModelSpec,
    ProviderSpec,
)
from naumi_agent.model.provider_runtime import (
    ProviderRuntimeError,
    build_provider_http_config,
    normalize_google_model_id,
)
from naumi_agent.model.reasoning import ReasoningEffort

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_REMOTE_MODELS = 500
_FAILURE_TTL_SECONDS = 30.0
_LIST_ALL_CONCURRENCY = 4

ModelSource = Literal["static", "discovered"]
CacheStatus = Literal["static", "fresh", "refreshed", "stale", "error"]


class ModelDiscoveryError(ValueError):
    """Raised for safe, user-facing provider discovery failures."""


@dataclass(frozen=True)
class AvailableModel:
    """One visible static or remotely discovered provider model."""

    provider_id: str
    id: str
    upstream_id: str
    name: str
    source: ModelSource
    max_context: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_structured_output: bool | None = None
    supports_reasoning: bool | None = None
    reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    default_reasoning_effort: ReasoningEffort | None = None
    supports_vision: bool | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    @property
    def canonical_id(self) -> str:
        return f"{self.provider_id}/{self.id}"

    def to_provider_model_spec(self) -> ProviderModelSpec:
        """Return the immutable catalog model shape consumed by routing."""
        return ProviderModelSpec(
            id=self.id,
            upstream_id=self.upstream_id,
            name=self.name,
            max_context=self.max_context,
            max_output=self.max_output,
            supports_tools=self.supports_tools,
            supports_streaming=self.supports_streaming,
            supports_parallel_tools=self.supports_parallel_tools,
            supports_structured_output=self.supports_structured_output,
            supports_reasoning=self.supports_reasoning,
            reasoning_efforts=self.reasoning_efforts,
            default_reasoning_effort=self.default_reasoning_effort,
            supports_vision=self.supports_vision,
            input_modalities=self.input_modalities,
            output_modalities=self.output_modalities,
            input_cost_per_million=self.input_cost_per_million,
            output_cost_per_million=self.output_cost_per_million,
        )


@dataclass(frozen=True)
class ProviderModelListing:
    """Visible model snapshot plus cache and fallback state."""

    provider_id: str
    provider_name: str
    models: tuple[AvailableModel, ...]
    cache_status: CacheStatus
    stale: bool = False
    refreshed_at: datetime | None = None
    warning: str | None = None


@dataclass(frozen=True)
class _ParsedModels:
    ids: tuple[str, ...]
    invalid_count: int = 0
    duplicate_count: int = 0
    unsupported_count: int = 0
    truncated: bool = False

    @property
    def warning(self) -> str | None:
        messages: list[str] = []
        if self.invalid_count:
            messages.append(f"忽略 {self.invalid_count} 条无效模型记录")
        if self.duplicate_count:
            messages.append(f"忽略 {self.duplicate_count} 条重复模型记录")
        if self.unsupported_count:
            messages.append(
                f"忽略 {self.unsupported_count} 条不支持 generateContent 的模型记录"
            )
        if self.truncated:
            messages.append(f"远程模型超过 {_MAX_REMOTE_MODELS} 项，已截断")
        return "；".join(messages) or None


@dataclass(frozen=True)
class _CacheEntry:
    remote_ids: tuple[str, ...] | None
    refreshed_at: datetime | None
    expires_at: float
    retry_after: float
    parse_warning: str | None = None
    error_warning: str | None = None


class ModelDiscoveryService:
    """Lazily discover provider models with bounded single-flight refreshes."""

    def __init__(
        self,
        catalog: ProviderCatalog,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._catalog = catalog
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task[ProviderModelListing]] = {}
        self._inflight_lock = asyncio.Lock()

    async def list_provider(
        self,
        provider_id: str,
        *,
        refresh: bool = False,
    ) -> ProviderModelListing:
        """Return one provider listing, refreshing only when policy requires it."""
        provider = self._catalog.providers.get(provider_id)
        if provider is None:
            raise ModelDiscoveryError(f'provider "{provider_id}" 不存在。')
        if not provider.discovery.enabled:
            return self._build_listing(provider, cache_status="static")

        now = self._clock()
        cached = self._cache.get(provider_id)
        if not refresh and cached is not None:
            if cached.error_warning is not None and now < cached.retry_after:
                status: CacheStatus = (
                    "stale" if cached.remote_ids is not None else "error"
                )
                return self._listing_from_cache(
                    provider,
                    cached,
                    cache_status=status,
                )
            if cached.remote_ids is not None and now < cached.expires_at:
                return self._listing_from_cache(provider, cached, cache_status="fresh")
            if now < cached.retry_after:
                status: CacheStatus = (
                    "stale" if cached.remote_ids is not None else "error"
                )
                return self._listing_from_cache(
                    provider,
                    cached,
                    cache_status=status,
                )

        task = await self._get_or_create_refresh(provider)
        return await asyncio.shield(task)

    async def list_all(
        self,
        *,
        refresh: bool = False,
    ) -> tuple[ProviderModelListing, ...]:
        """List all providers in catalog order with at most four refreshes."""
        semaphore = asyncio.Semaphore(_LIST_ALL_CONCURRENCY)

        async def list_one(provider_id: str) -> ProviderModelListing:
            async with semaphore:
                return await self.list_provider(provider_id, refresh=refresh)

        return tuple(
            await asyncio.gather(
                *(list_one(provider_id) for provider_id in self._catalog.providers)
            )
        )

    async def _get_or_create_refresh(
        self,
        provider: ProviderSpec,
    ) -> asyncio.Task[ProviderModelListing]:
        async with self._inflight_lock:
            existing = self._inflight.get(provider.id)
            if existing is not None:
                return existing
            task = asyncio.create_task(self._refresh_provider(provider))
            self._inflight[provider.id] = task
            task.add_done_callback(
                lambda completed, provider_id=provider.id: self._forget_refresh(
                    provider_id,
                    completed,
                )
            )
            return task

    def _forget_refresh(
        self,
        provider_id: str,
        completed: asyncio.Task[ProviderModelListing],
    ) -> None:
        if self._inflight.get(provider_id) is completed:
            self._inflight.pop(provider_id, None)

    async def _refresh_provider(self, provider: ProviderSpec) -> ProviderModelListing:
        previous = self._cache.get(provider.id)
        now = self._clock()
        try:
            parsed = await self._fetch_remote_models(provider)
        except (ModelDiscoveryError, ProviderRuntimeError) as exc:
            warning = str(exc)
            retry_after = now + _FAILURE_TTL_SECONDS
            if previous is not None and previous.remote_ids is not None:
                stale = replace(
                    previous,
                    retry_after=retry_after,
                    error_warning=warning,
                )
                self._cache[provider.id] = stale
                return self._listing_from_cache(
                    provider,
                    stale,
                    cache_status="stale",
                )
            failed = _CacheEntry(
                remote_ids=None,
                refreshed_at=None,
                expires_at=0.0,
                retry_after=retry_after,
                error_warning=warning,
            )
            self._cache[provider.id] = failed
            return self._listing_from_cache(
                provider,
                failed,
                cache_status="error",
            )

        refreshed_at = datetime.now(UTC)
        entry = _CacheEntry(
            remote_ids=parsed.ids,
            refreshed_at=refreshed_at,
            expires_at=now + provider.discovery.ttl_seconds,
            retry_after=0.0,
            parse_warning=parsed.warning,
        )
        self._cache[provider.id] = entry
        return self._listing_from_cache(
            provider,
            entry,
            cache_status="refreshed",
        )

    async def _fetch_remote_models(self, provider: ProviderSpec) -> _ParsedModels:
        if provider.api_format not in {
            APIFormat.OPENAI_CHAT,
            APIFormat.OPENAI_RESPONSES,
            APIFormat.GOOGLE_GENAI,
            APIFormat.OLLAMA,
        }:
            api_format = provider.api_format.value if provider.api_format else "未声明"
            raise ModelDiscoveryError(
                f'provider "{provider.id}" 的 {api_format} 模型发现适配器尚未实现。'
            )

        config = build_provider_http_config(
            provider,
            catalog_source=self._catalog.source,
        )
        base_url = httpx.URL(config.base_url)
        discovery_path = (
            f"{base_url.path.rstrip('/')}"
            f"/{provider.discovery.path.lstrip('/')}"
        )
        url = base_url.copy_with(path=discovery_path)
        try:
            async with httpx.AsyncClient(
                headers=config.headers,
                timeout=config.timeout_seconds,
                transport=self._transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    self._raise_for_status(provider, response.status_code)
                    raw = await _read_bounded_response(response)
        except httpx.TimeoutException:
            raise ModelDiscoveryError(
                f'provider "{provider.id}" 模型发现请求超时。'
            ) from None
        except httpx.RequestError:
            raise ModelDiscoveryError(
                f'provider "{provider.id}" 模型发现连接失败。'
            ) from None

        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelDiscoveryError(
                f'provider "{provider.id}" 模型发现返回的 JSON 无效。'
            ) from None

        if provider.api_format in {APIFormat.OPENAI_CHAT, APIFormat.OPENAI_RESPONSES}:
            return _parse_openai_models(provider.id, payload)
        if provider.api_format is APIFormat.GOOGLE_GENAI:
            return _parse_google_models(provider.id, payload)
        return _parse_ollama_models(provider.id, payload)

    @staticmethod
    def _raise_for_status(provider: ProviderSpec, status_code: int) -> None:
        if status_code < 400:
            return
        if status_code in {401, 403}:
            message = "认证失败"
        elif status_code == 404:
            message = "接口不存在"
        elif status_code == 429:
            message = "请求频率受限"
        else:
            message = f"HTTP {status_code} 状态异常"
        raise ModelDiscoveryError(
            f'provider "{provider.id}" 模型发现{message}。'
        )

    def _listing_from_cache(
        self,
        provider: ProviderSpec,
        cached: _CacheEntry,
        *,
        cache_status: CacheStatus,
    ) -> ProviderModelListing:
        return self._build_listing(
            provider,
            remote_ids=cached.remote_ids or (),
            cache_status=cache_status,
            stale=cache_status == "stale",
            refreshed_at=cached.refreshed_at,
            warning=_join_warnings(cached.parse_warning, cached.error_warning),
        )

    @staticmethod
    def _build_listing(
        provider: ProviderSpec,
        *,
        remote_ids: tuple[str, ...] = (),
        cache_status: CacheStatus,
        stale: bool = False,
        refreshed_at: datetime | None = None,
        warning: str | None = None,
    ) -> ProviderModelListing:
        return ProviderModelListing(
            provider_id=provider.id,
            provider_name=provider.name,
            models=_merge_models(provider, remote_ids),
            cache_status=cache_status,
            stale=stale,
            refreshed_at=refreshed_at,
            warning=warning,
        )


async def _read_bounded_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ModelDiscoveryError("模型发现响应超过 2 MiB 限制。")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_openai_models(provider_id: str, payload: object) -> _ParsedModels:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现返回结构无效。'
        )
    rows = payload["data"]
    return _parse_rows(provider_id, rows, keys=("id",))


def _parse_ollama_models(provider_id: str, payload: object) -> _ParsedModels:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现返回结构无效。'
        )
    rows = payload["models"]
    return _parse_rows(provider_id, rows, keys=("model", "name"))


def _parse_google_models(provider_id: str, payload: object) -> _ParsedModels:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现返回结构无效。'
        )

    rows = payload["models"]
    normalized: list[object] = []
    invalid_count = 0
    unsupported_count = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        raw_name = row.get("name")
        if not isinstance(raw_name, str) or not raw_name.startswith("models/"):
            invalid_count += 1
            continue
        try:
            model_id = normalize_google_model_id(raw_name)
        except ProviderRuntimeError:
            invalid_count += 1
            continue

        methods = row.get("supportedGenerationMethods")
        if methods is not None:
            if not isinstance(methods, list) or not all(
                isinstance(method, str) for method in methods
            ):
                invalid_count += 1
                continue
            if "generateContent" not in methods:
                unsupported_count += 1
                continue
        normalized.append({"id": model_id})

    if rows and not normalized:
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现结果不含有效模型。'
        )
    parsed = _parse_rows(provider_id, normalized, keys=("id",))
    return replace(
        parsed,
        invalid_count=parsed.invalid_count + invalid_count,
        unsupported_count=unsupported_count,
    )


def _parse_rows(
    provider_id: str,
    rows: list[object],
    *,
    keys: tuple[str, ...],
) -> _ParsedModels:
    ids: list[str] = []
    seen: set[str] = set()
    invalid_count = 0
    duplicate_count = 0
    truncated = False

    for row in rows:
        model_id = _model_id_from_row(row, keys=keys)
        if model_id is None:
            invalid_count += 1
            continue
        if model_id in seen:
            duplicate_count += 1
            continue
        seen.add(model_id)
        if len(ids) >= _MAX_REMOTE_MODELS:
            truncated = True
            continue
        ids.append(model_id)

    if rows and not ids:
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现结果不含有效模型。'
        )
    return _ParsedModels(
        ids=tuple(ids),
        invalid_count=invalid_count,
        duplicate_count=duplicate_count,
        truncated=truncated,
    )


def _model_id_from_row(
    row: object,
    *,
    keys: tuple[str, ...],
) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in keys:
        raw = row.get(key)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if (
            value
            and len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return value
    return None


def _merge_models(
    provider: ProviderSpec,
    remote_ids: tuple[str, ...],
) -> tuple[AvailableModel, ...]:
    allowed = set(provider.whitelist)
    blocked = set(provider.blacklist)

    def visible(model_id: str) -> bool:
        return model_id not in blocked and (not allowed or model_id in allowed)

    static_models = tuple(
        _available_static(provider.id, model)
        for model in provider.models.values()
        if visible(model.id)
    )
    static_upstream_ids = _static_upstream_ids(provider)
    static_local_ids = set(provider.models)
    discovered = tuple(
        AvailableModel(
            provider_id=provider.id,
            id=model_id,
            upstream_id=model_id,
            name=model_id,
            source="discovered",
        )
        for model_id in sorted(remote_ids)
        if model_id not in static_upstream_ids
        and model_id not in static_local_ids
        and visible(model_id)
    )
    return static_models + discovered


def _static_upstream_ids(provider: ProviderSpec) -> set[str]:
    upstream_ids: set[str] = set()
    for model in provider.models.values():
        upstream_id = model.upstream_id
        if provider.api_format is APIFormat.GOOGLE_GENAI:
            try:
                upstream_id = normalize_google_model_id(upstream_id)
            except ProviderRuntimeError:
                pass
        upstream_ids.add(upstream_id)
    return upstream_ids


def _available_static(provider_id: str, model: ProviderModelSpec) -> AvailableModel:
    return AvailableModel(
        provider_id=provider_id,
        id=model.id,
        upstream_id=model.upstream_id,
        name=model.name,
        source="static",
        max_context=model.max_context,
        max_output=model.max_output,
        supports_tools=model.supports_tools,
        supports_streaming=model.supports_streaming,
        supports_parallel_tools=model.supports_parallel_tools,
        supports_structured_output=model.supports_structured_output,
        supports_reasoning=model.supports_reasoning,
        reasoning_efforts=model.reasoning_efforts,
        default_reasoning_effort=model.default_reasoning_effort,
        supports_vision=model.supports_vision,
        input_modalities=model.input_modalities,
        output_modalities=model.output_modalities,
        input_cost_per_million=model.input_cost_per_million,
        output_cost_per_million=model.output_cost_per_million,
    )


def _join_warnings(*warnings: str | None) -> str | None:
    present = [warning for warning in warnings if warning]
    return "；".join(present) or None
