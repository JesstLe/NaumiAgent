"""Pure model-target resolution for immutable provider catalogs."""

from __future__ import annotations

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
        return _catalog_target(selected, alias, requested)

    active = catalog.providers.get((provider or "").strip().lower())
    if active is None:
        return _legacy_target(requested)
    if requested in active.models:
        return _catalog_target(active, requested, requested)
    if separator:
        return _legacy_target(requested)
    return _catalog_target(active, requested, requested)


def _catalog_target(
    provider: ProviderSpec,
    alias: str,
    requested: str,
) -> ResolvedModelTarget:
    model = provider.models.get(alias)
    if model is None:
        raise ModelResolutionError(
            f'provider "{provider.id}" 未声明模型别名 "{alias}"。'
        )

    visible_ids = {visible.id for visible in provider.visible_models()}
    if alias not in visible_ids:
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


def _legacy_target(requested: str) -> ResolvedModelTarget:
    return ResolvedModelTarget(
        requested_model=requested,
        canonical_model=requested,
        upstream_model=requested,
        provider=None,
        model=None,
        source="legacy",
    )
