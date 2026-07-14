"""Provider model discovery parsing, safety, merge, and cache tests."""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType

import httpx
import pytest

from naumi_agent.model.catalog import (
    APIFormat,
    AuthType,
    ModelDiscoverySpec,
    ProviderAuthSpec,
    ProviderCatalog,
    ProviderModelSpec,
    ProviderSpec,
)
from naumi_agent.model.discovery import ModelDiscoveryError, ModelDiscoveryService
from naumi_agent.model.reasoning import ReasoningEffort


def _provider(
    provider_id: str = "gateway",
    *,
    api_format: APIFormat = APIFormat.OPENAI_CHAT,
    base_url: str = "https://provider.example/v1",
    path: str = "/models",
    models: tuple[ProviderModelSpec, ...] = (),
    enabled: bool = True,
    whitelist: tuple[str, ...] = (),
    blacklist: tuple[str, ...] = (),
    ttl_seconds: int = 60,
) -> ProviderSpec:
    return ProviderSpec(
        id=provider_id,
        name=provider_id.title(),
        api_format=api_format,
        base_url=base_url,
        auth=ProviderAuthSpec(type=AuthType.NONE),
        headers=MappingProxyType({}),
        models=MappingProxyType({model.id: model for model in models}),
        discovery=ModelDiscoverySpec(
            enabled=enabled,
            path=path,
            ttl_seconds=ttl_seconds,
        ),
        whitelist=whitelist,
        blacklist=blacklist,
    )


def _catalog(*providers: ProviderSpec) -> ProviderCatalog:
    return ProviderCatalog(
        providers=MappingProxyType({provider.id: provider for provider in providers}),
        source="/tmp/providers.json",
    )


def _service(
    provider: ProviderSpec,
    handler,
    **kwargs,
) -> ModelDiscoveryService:
    return ModelDiscoveryService(
        _catalog(provider),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def test_openai_discovery_merges_static_first_and_sorts_remote_models() -> None:
    static = ProviderModelSpec(
        id="stable-alias",
        upstream_id="model-z",
        name="Stable model",
        max_context=128_000,
        supports_tools=True,
        supports_reasoning=True,
        reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
        default_reasoning_effort=ReasoningEffort.LOW,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://provider.example/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "model-z", "object": "model"},
                    {"id": "model-b", "object": "model"},
                    {"id": "model-a", "object": "model"},
                ],
            },
        )

    listing = await _service(_provider(models=(static,)), handler).list_provider(
        "gateway"
    )

    assert [model.canonical_id for model in listing.models] == [
        "gateway/stable-alias",
        "gateway/model-a",
        "gateway/model-b",
    ]
    assert listing.models[0].source == "static"
    assert listing.models[0].max_context == 128_000
    assert listing.models[0].supports_tools is True
    assert listing.models[0].reasoning_efforts == (
        ReasoningEffort.LOW,
        ReasoningEffort.HIGH,
    )
    assert listing.models[0].default_reasoning_effort is ReasoningEffort.LOW
    assert listing.models[1].source == "discovered"
    assert listing.models[1].reasoning_efforts == ()
    assert listing.models[1].default_reasoning_effort is None
    assert listing.cache_status == "refreshed"
    assert listing.stale is False
    assert listing.warning is None


async def test_ollama_discovery_uses_tags_and_accepts_model_or_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:11434/api/tags"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "models": [
                    {"model": "qwen3:8b", "name": "ignored-name"},
                    {"name": "llama3.3:latest"},
                ]
            },
        )

    provider = _provider(
        provider_id="ollama",
        api_format=APIFormat.OLLAMA,
        base_url="http://127.0.0.1:11434",
        path="/api/tags",
    )
    listing = await _service(provider, handler).list_provider("ollama")

    assert [model.id for model in listing.models] == [
        "llama3.3:latest",
        "qwen3:8b",
    ]


async def test_discovery_appends_path_before_safe_base_url_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == (
            "https://provider.example/v1/models?api-version=2026-01-01"
        )
        return httpx.Response(200, json={"data": [{"id": "query-model"}]})

    provider = _provider(
        base_url="https://provider.example/v1?api-version=2026-01-01"
    )
    listing = await _service(provider, handler).list_provider("gateway")

    assert listing.models[0].id == "query-model"


async def test_discovery_deduplicates_and_reports_invalid_rows() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "valid-model"},
                    {"id": "valid-model"},
                    {"id": "  "},
                    {"id": "bad\nmodel"},
                    {"not_id": "ignored"},
                    "invalid-row",
                ]
            },
        )

    listing = await _service(_provider(), handler).list_provider("gateway")

    assert [model.id for model in listing.models] == ["valid-model"]
    assert listing.warning is not None
    assert "无效" in listing.warning
    assert "重复" in listing.warning


async def test_discovery_applies_whitelist_and_blacklist_after_merge() -> None:
    static_allowed = ProviderModelSpec("static", "upstream-static", "Static")
    static_blocked = ProviderModelSpec("blocked", "upstream-blocked", "Blocked")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "remote"}, {"id": "blocked"}, {"id": "other"}]},
        )

    provider = _provider(
        models=(static_allowed, static_blocked),
        whitelist=("static", "remote", "blocked"),
        blacklist=("blocked",),
    )
    listing = await _service(provider, handler).list_provider("gateway")

    assert [model.id for model in listing.models] == ["static", "remote"]


async def test_discovery_caps_remote_models_at_500() -> None:
    payload = {"data": [{"id": f"model-{index:04d}"} for index in range(700)]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    listing = await _service(_provider(), handler).list_provider("gateway")

    assert len(listing.models) == 500
    assert listing.models[0].id == "model-0000"
    assert listing.models[-1].id == "model-0499"
    assert listing.warning is not None
    assert "500" in listing.warning


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, text="secret upstream body"), "认证失败"),
        (httpx.Response(404), "接口不存在"),
        (httpx.Response(429), "频率受限"),
        (httpx.Response(503, text="private failure"), "HTTP 503"),
        (httpx.Response(200, content=b"not-json"), "JSON"),
        (httpx.Response(200, json={"data": "wrong"}), "结构"),
    ],
)
async def test_discovery_returns_safe_static_fallback_warnings(
    response: httpx.Response,
    message: str,
) -> None:
    static = ProviderModelSpec("static", "upstream-static", "Static")

    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    listing = await _service(_provider(models=(static,)), handler).list_provider(
        "gateway"
    )

    assert [model.id for model in listing.models] == ["static"]
    assert listing.cache_status == "error"
    assert listing.warning is not None
    assert message in listing.warning
    assert "secret upstream body" not in listing.warning
    assert "private failure" not in listing.warning
    assert "provider.example" not in listing.warning


@pytest.mark.parametrize(
    ("exception_type", "message"),
    [
        (httpx.ReadTimeout, "请求超时"),
        (httpx.ConnectError, "连接失败"),
    ],
)
async def test_discovery_maps_transport_failures_without_leaking_request_url(
    exception_type: type[httpx.RequestError],
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("private transport detail", request=request)

    listing = await _service(_provider(), handler).list_provider("gateway")

    assert listing.cache_status == "error"
    assert listing.warning is not None
    assert message in listing.warning
    assert "private transport detail" not in listing.warning
    assert "provider.example" not in listing.warning


async def test_discovery_rejects_unsupported_format_without_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    provider = _provider(api_format=APIFormat.GOOGLE_GENAI)
    listing = await _service(provider, handler).list_provider("gateway")

    assert listing.models == ()
    assert listing.warning is not None
    assert "google_genai" in listing.warning
    assert "尚未实现" in listing.warning
    assert calls == 0


async def test_discovery_rejects_response_larger_than_two_mib() -> None:
    oversized = json.dumps({"data": [{"id": "x" * (2 * 1024 * 1024)}]}).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    listing = await _service(_provider(), handler).list_provider("gateway")

    assert listing.models == ()
    assert listing.warning is not None
    assert "2 MiB" in listing.warning


async def test_static_only_provider_never_creates_a_network_request() -> None:
    calls = 0
    static = ProviderModelSpec("static", "upstream-static", "Static")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    listing = await _service(
        _provider(models=(static,), enabled=False),
        handler,
    ).list_provider("gateway")

    assert [model.id for model in listing.models] == ["static"]
    assert listing.cache_status == "static"
    assert calls == 0


async def test_unknown_provider_is_an_explicit_safe_error() -> None:
    service = ModelDiscoveryService(_catalog(_provider()))

    with pytest.raises(ModelDiscoveryError, match="unknown.*不存在"):
        await service.list_provider("unknown")


async def test_nonempty_payload_with_no_valid_models_is_a_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": ""}, {}]})

    listing = await _service(_provider(), handler).list_provider("gateway")

    assert listing.models == ()
    assert listing.cache_status == "error"
    assert listing.warning is not None
    assert "有效模型" in listing.warning


async def test_list_all_preserves_provider_order() -> None:
    first = _provider("first", enabled=False, models=(ProviderModelSpec("a", "a", "A"),))
    second = _provider("second", enabled=False, models=(ProviderModelSpec("b", "b", "B"),))
    service = ModelDiscoveryService(_catalog(first, second))

    listings = await service.list_all()

    assert [listing.provider_id for listing in listings] == ["first", "second"]


async def test_success_cache_honors_ttl_expiry_and_explicit_refresh() -> None:
    clock = _FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"id": f"model-{calls}"}]})

    service = _service(_provider(ttl_seconds=60), handler, clock=clock)

    first = await service.list_provider("gateway")
    cached = await service.list_provider("gateway")
    clock.advance(61)
    expired = await service.list_provider("gateway")
    refreshed = await service.list_provider("gateway", refresh=True)

    assert first.models[0].id == "model-1"
    assert first.cache_status == "refreshed"
    assert cached.models[0].id == "model-1"
    assert cached.cache_status == "fresh"
    assert expired.models[0].id == "model-2"
    assert refreshed.models[0].id == "model-3"
    assert calls == 3


async def test_first_failure_is_negatively_cached_for_30_seconds() -> None:
    clock = _FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    service = _service(_provider(), handler, clock=clock)

    first = await service.list_provider("gateway")
    clock.advance(29)
    cached = await service.list_provider("gateway")
    clock.advance(2)
    retried = await service.list_provider("gateway")

    assert first.cache_status == "error"
    assert cached.cache_status == "error"
    assert retried.cache_status == "error"
    assert calls == 2


async def test_expired_success_falls_back_to_stale_models_after_failure() -> None:
    clock = _FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"data": [{"id": "stable-remote"}]})
        return httpx.Response(503, text="private upstream message")

    service = _service(_provider(ttl_seconds=60), handler, clock=clock)

    await service.list_provider("gateway")
    clock.advance(61)
    stale = await service.list_provider("gateway")
    clock.advance(29)
    stale_cached = await service.list_provider("gateway")

    assert [model.id for model in stale.models] == ["stable-remote"]
    assert stale.cache_status == "stale"
    assert stale.stale is True
    assert stale.warning is not None
    assert "HTTP 503" in stale.warning
    assert "private upstream message" not in stale.warning
    assert stale_cached.cache_status == "stale"
    assert calls == 2


async def test_explicit_refresh_failure_keeps_previous_cache_marked_stale() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"data": [{"id": "stable-remote"}]})
        return httpx.Response(503)

    service = _service(_provider(ttl_seconds=60), handler)

    await service.list_provider("gateway")
    refreshed = await service.list_provider("gateway", refresh=True)
    cached = await service.list_provider("gateway")

    assert refreshed.cache_status == "stale"
    assert cached.cache_status == "stale"
    assert cached.stale is True
    assert [model.id for model in cached.models] == ["stable-remote"]
    assert calls == 2


async def test_50_concurrent_calls_share_one_refresh_request() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"data": [{"id": "shared-model"}]})

    service = _service(_provider(), handler)

    listings = await asyncio.gather(
        *(service.list_provider("gateway") for _ in range(50))
    )

    assert calls == 1
    assert all(listing.models == listings[0].models for listing in listings)


async def test_cancelling_one_waiter_does_not_cancel_shared_refresh() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"id": "surviving-model"}]})

    service = _service(_provider(), handler)
    cancelled_waiter = asyncio.create_task(service.list_provider("gateway"))
    await started.wait()
    surviving_waiter = asyncio.create_task(service.list_provider("gateway"))
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release.set()
    listing = await surviving_waiter
    cached = await service.list_provider("gateway")

    assert listing.models[0].id == "surviving-model"
    assert cached.cache_status == "fresh"
    assert calls == 1


async def test_list_all_runs_at_most_four_provider_refreshes_concurrently() -> None:
    active = 0
    maximum_active = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"data": [{"id": "remote"}]})

    providers = tuple(_provider(f"provider-{index}") for index in range(8))
    service = ModelDiscoveryService(
        _catalog(*providers),
        transport=httpx.MockTransport(handler),
    )

    listings = await service.list_all()

    assert len(listings) == 8
    assert maximum_active == 4
