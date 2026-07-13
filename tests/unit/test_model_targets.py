from __future__ import annotations

import json

import pytest

from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.targets import ModelResolutionError, resolve_model_target


def _catalog_json() -> str:
    return json.dumps(
        {
            "provider": {
                "nvidia": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "https://integrate.api.nvidia.com/v1"},
                    "models": {
                        "local/glm": {"upstreamId": "z-ai/glm4.7"},
                        "local/glm-copy": {"upstreamId": "z-ai/glm4.7"},
                        "hidden/glm": {"upstreamId": "z-ai/hidden-glm"},
                    },
                    "blacklist": ["hidden/glm"],
                },
                "builtin": {
                    "models": {
                        "echo": {"upstreamId": "builtin/echo"},
                    },
                },
            }
        }
    )


@pytest.fixture
def catalog():
    return parse_provider_catalog_json(_catalog_json())


def test_resolves_qualified_active_and_legacy_targets(catalog) -> None:
    target = resolve_model_target(
        "nvidia/local/glm",
        provider=None,
        catalog=catalog,
    )
    assert target.requested_model == "nvidia/local/glm"
    assert target.source == "catalog"
    assert target.canonical_model == "nvidia/local/glm"
    assert target.upstream_model == "z-ai/glm4.7"
    assert target.provider is catalog.providers["nvidia"]
    assert target.model is catalog.providers["nvidia"].models["local/glm"]

    active = resolve_model_target("local/glm", provider="nvidia", catalog=catalog)
    assert active.canonical_model == "nvidia/local/glm"

    legacy = resolve_model_target("openai/gpt-4o", provider="openai", catalog=catalog)
    assert legacy.source == "legacy"
    assert legacy.canonical_model == "openai/gpt-4o"
    assert legacy.upstream_model == "openai/gpt-4o"
    assert legacy.provider is None
    assert legacy.model is None


def test_unknown_alias_raises_resolution_error(catalog) -> None:
    with pytest.raises(ModelResolutionError, match="未声明"):
        resolve_model_target("nvidia/unknown", provider=None, catalog=catalog)


def test_qualified_legacy_model_bypasses_active_catalog_provider(catalog) -> None:
    target = resolve_model_target(
        "openai/gpt-4o",
        provider="nvidia",
        catalog=catalog,
    )

    assert target.source == "legacy"
    assert target.requested_model == "openai/gpt-4o"
    assert target.canonical_model == "openai/gpt-4o"
    assert target.upstream_model == "openai/gpt-4o"
    assert target.provider is None
    assert target.model is None


def test_filtered_alias_raises_distinct_resolution_error(catalog) -> None:
    with pytest.raises(ModelResolutionError, match="过滤"):
        resolve_model_target("hidden/glm", provider="nvidia", catalog=catalog)


def test_empty_input_raises_chinese_resolution_error(catalog) -> None:
    with pytest.raises(ModelResolutionError, match="模型名称不能为空"):
        resolve_model_target(" \t\n", provider="nvidia", catalog=catalog)


def test_aliases_sharing_one_upstream_remain_distinct(catalog) -> None:
    primary = resolve_model_target("local/glm", provider="nvidia", catalog=catalog)
    copy = resolve_model_target("local/glm-copy", provider="nvidia", catalog=catalog)

    assert primary.upstream_model == copy.upstream_model == "z-ai/glm4.7"
    assert primary.canonical_model == "nvidia/local/glm"
    assert copy.canonical_model == "nvidia/local/glm-copy"
    assert primary.model is not copy.model


def test_provider_without_api_format_still_resolves(catalog) -> None:
    target = resolve_model_target("builtin/echo", provider=None, catalog=catalog)

    assert target.source == "catalog"
    assert target.canonical_model == "builtin/echo"
    assert target.upstream_model == "builtin/echo"
    assert target.provider is not None
    assert target.provider.api_format is None


def test_catalog_none_preserves_trimmed_legacy_model() -> None:
    target = resolve_model_target("  openai/gpt-4o  ", provider="nvidia", catalog=None)

    assert target.requested_model == "openai/gpt-4o"
    assert target.canonical_model == "openai/gpt-4o"
    assert target.upstream_model == "openai/gpt-4o"
    assert target.source == "legacy"
