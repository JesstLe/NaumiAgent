"""Shared slash and REST surfaces for provider model discovery."""

from __future__ import annotations

from types import SimpleNamespace

from rich.text import Text

from naumi_agent.api.routes.tools import get_config
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.model.discovery import (
    AvailableModel,
    ModelDiscoveryError,
    ProviderModelListing,
)
from naumi_agent.model.router import ModelRuntimeIdentity


def _model(
    model_id: str,
    *,
    source: str = "discovered",
    name: str | None = None,
) -> AvailableModel:
    return AvailableModel(
        provider_id="vendor",
        id=model_id,
        upstream_id=f"upstream/{model_id}",
        name=name or model_id,
        source=source,  # type: ignore[arg-type]
        max_context=128_000,
        max_output=8_192,
        supports_tools=True,
        supports_reasoning=False,
        supports_vision=True,
    )


def _listing(
    models: tuple[AvailableModel, ...],
    *,
    warning: str | None = None,
    stale: bool = False,
) -> ProviderModelListing:
    return ProviderModelListing(
        provider_id="vendor",
        provider_name="Vendor Gateway",
        models=models,
        cache_status="stale" if stale else "refreshed",
        stale=stale,
        warning=warning,
    )


class _FakeRouter:
    def __init__(
        self,
        listings: tuple[ProviderModelListing, ...] = (),
        *,
        error: ModelDiscoveryError | None = None,
    ) -> None:
        self.listings = listings
        self.error = error
        self.calls: list[tuple[str | None, bool]] = []

    async def list_available_models(
        self,
        provider_id: str | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[ProviderModelListing, ...]:
        self.calls.append((provider_id, refresh))
        if self.error is not None:
            raise self.error
        return self.listings

    @staticmethod
    def resolve_model(tier: str) -> str:
        return {
            "fast": "fast-model",
            "capable": "remote-model",
            "reasoning": "reasoning-model",
        }[tier]

    @staticmethod
    def get_runtime_identity(model: str) -> ModelRuntimeIdentity:
        return ModelRuntimeIdentity(
            requested_model=model,
            canonical_model=model,
            upstream_model=model,
            provider="legacy-provider",
            api_format="legacy",
            source="legacy",
        )


def _plain(ansi: str) -> str:
    return Text.from_ansi(ansi).plain


async def test_models_slash_lists_provider_models_and_forwards_refresh() -> None:
    router = _FakeRouter(
        (
            _listing(
                (
                    _model("static-model", source="static", name="Static Model"),
                    _model("remote-model", name="Remote Model"),
                )
            ),
        )
    )

    output = _plain(
        await execute_slash_command(
            SimpleNamespace(router=router),
            "/models vendor --refresh",
        )
    )

    assert router.calls == [("vendor", True)]
    assert "Vendor Gateway (vendor)" in output
    assert "vendor/static-model" in output
    assert "Static Model" in output
    assert "静态" in output
    assert "vendor/remote-model" in output
    assert "发现" in output


async def test_models_slash_accepts_refresh_before_provider() -> None:
    router = _FakeRouter((_listing((_model("remote-model"),)),))

    await execute_slash_command(
        SimpleNamespace(router=router),
        "/models --refresh vendor",
    )

    assert router.calls == [("vendor", True)]


async def test_models_slash_rejects_extra_arguments_without_discovery() -> None:
    router = _FakeRouter()

    output = _plain(
        await execute_slash_command(
            SimpleNamespace(router=router),
            "/models vendor extra",
        )
    )

    assert "用法: /models [provider] [--refresh]" in output
    assert router.calls == []


async def test_models_slash_limits_each_provider_to_100_rows() -> None:
    models = tuple(_model(f"model-{index:03d}") for index in range(105))
    router = _FakeRouter((_listing(models),))

    output = _plain(
        await execute_slash_command(SimpleNamespace(router=router), "/models")
    )

    assert "vendor/model-000" in output
    assert "vendor/model-099" in output
    assert "vendor/model-100" not in output
    assert "另有 5 个模型未显示" in output


async def test_models_slash_shows_safe_stale_warning() -> None:
    router = _FakeRouter(
        (
            _listing(
                (_model("remote-model"),),
                warning="模型发现请求超时，正在使用旧缓存。",
                stale=True,
            ),
        )
    )

    output = _plain(
        await execute_slash_command(SimpleNamespace(router=router), "/models")
    )

    assert "旧缓存" in output
    assert "模型发现请求超时" in output


async def test_models_slash_reports_unknown_provider_without_traceback() -> None:
    router = _FakeRouter(error=ModelDiscoveryError('provider "missing" 不存在。'))

    output = _plain(
        await execute_slash_command(
            SimpleNamespace(router=router),
            "/models missing",
        )
    )

    assert 'provider "missing" 不存在' in output


async def test_rest_config_uses_discovered_models_and_no_hardcoded_kimi() -> None:
    router = _FakeRouter(
        (
            _listing(
                (
                    _model("static-model", source="static", name="Static Model"),
                    _model("remote-model", name="Remote Model"),
                ),
                warning="远程结果忽略 1 条无效记录",
            ),
        )
    )
    engine = SimpleNamespace(
        router=router,
        config=SimpleNamespace(
            safety=SimpleNamespace(
                permission_mode="bypass",
                max_budget_usd=None,
                max_turns=50,
            )
        ),
        tool_registry=SimpleNamespace(all=lambda: []),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=engine))
    )

    response = await get_config(request, auth="test")

    assert [model.id for model in response.models] == [
        "vendor/static-model",
        "vendor/remote-model",
    ]
    assert all(model.id != "kimi-for-coding" for model in response.models)
    remote = response.models[1]
    assert remote.upstream_id == "upstream/remote-model"
    assert remote.source == "discovered"
    assert remote.tier == "capable"
    assert remote.max_context == 128_000
    assert remote.supports_tools is True
    assert response.model_warnings == [
        "vendor: 远程结果忽略 1 条无效记录"
    ]
    assert response.max_budget_usd is None
    assert response.max_turns == 50


async def test_rest_config_falls_back_to_configured_legacy_models() -> None:
    router = _FakeRouter()
    engine = SimpleNamespace(
        router=router,
        config=SimpleNamespace(
            safety=SimpleNamespace(
                permission_mode="default",
                max_budget_usd=1.5,
                max_turns=50,
            )
        ),
        tool_registry=SimpleNamespace(all=lambda: []),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=engine))
    )

    response = await get_config(request, auth="test")

    assert [model.id for model in response.models] == [
        "fast-model",
        "remote-model",
        "reasoning-model",
    ]
    assert [model.tier for model in response.models] == [
        "fast",
        "capable",
        "reasoning",
    ]
    assert all(model.source == "legacy" for model in response.models)

