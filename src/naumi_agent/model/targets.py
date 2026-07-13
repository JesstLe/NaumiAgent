"""Pure model-target resolution for immutable provider catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from naumi_agent.model.catalog import ProviderCatalog, ProviderModelSpec, ProviderSpec


class ModelResolutionError(ValueError):
    """Raised when a model target cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedModelTarget:
    requested_model: str
    canonical_model: str
    upstream_model: str
    provider: ProviderSpec | None
    model: ProviderModelSpec | None
    source: Literal["catalog", "legacy"]


def resolve_model_target(
    model: str,
    *,
    provider: str | None,
    catalog: ProviderCatalog | None,
    dynamic_models: Mapping[str, Mapping[str, ProviderModelSpec]] | None = None,
) -> ResolvedModelTarget:
    """Resolve a requested model without performing I/O or credential lookup."""
    requested = model.strip()
    if not requested:
        raise ModelResolutionError("模型名称不能为空。")
    if catalog is None:
        return _legacy_target(requested)

    prefix, separator, alias = requested.partition("/")
    selected = catalog.providers.get(prefix.lower()) if separator else None
    if selected is not None:
        return _catalog_target(
            selected,
            alias,
            requested,
            dynamic_models=_provider_dynamic_models(dynamic_models, selected.id),
        )

    active = catalog.providers.get((provider or "").strip().lower())
    if active is None:
        return _legacy_target(requested)
    active_dynamic = _provider_dynamic_models(dynamic_models, active.id)
    if requested in active.models or requested in active_dynamic:
        return _catalog_target(
            active,
            requested,
            requested,
            dynamic_models=active_dynamic,
        )
    if separator:
        return _legacy_target(requested)
    return _catalog_target(
        active,
        requested,
        requested,
        dynamic_models=active_dynamic,
    )


def _catalog_target(
    provider: ProviderSpec,
    alias: str,
    requested: str,
    *,
    dynamic_models: Mapping[str, ProviderModelSpec],
) -> ResolvedModelTarget:
    model = provider.models.get(alias) or dynamic_models.get(alias)
    if model is None:
        raise ModelResolutionError(
            f'provider "{provider.id}" 未声明模型别名 "{alias}"。'
        )

    allowed = set(provider.whitelist)
    blocked = set(provider.blacklist)
    if alias in blocked or (allowed and alias not in allowed):
        raise ModelResolutionError(
            f'provider "{provider.id}" 的模型别名 "{alias}" 已被可见性规则过滤。'
        )

    return ResolvedModelTarget(
        requested_model=requested,
        canonical_model=f"{provider.id}/{alias}",
        upstream_model=model.upstream_id,
        provider=provider,
        model=model,
        source="catalog",
    )


def _provider_dynamic_models(
    dynamic_models: Mapping[str, Mapping[str, ProviderModelSpec]] | None,
    provider_id: str,
) -> Mapping[str, ProviderModelSpec]:
    if dynamic_models is None:
        return {}
    return dynamic_models.get(provider_id, {})


def _legacy_target(requested: str) -> ResolvedModelTarget:
    return ResolvedModelTarget(
        requested_model=requested,
        canonical_model=requested,
        upstream_model=requested,
        provider=None,
        model=None,
        source="legacy",
    )
