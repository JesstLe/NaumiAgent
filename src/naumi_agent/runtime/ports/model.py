"""Complete model capability boundary consumed by the Agent runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from naumi_agent.model.discovery import ProviderModelListing
from naumi_agent.model.reasoning import ReasoningEffortSetting, ReasoningEffortStatus
from naumi_agent.model.router import (
    ModelCapabilityContract,
    ModelResponse,
    ModelRuntimeIdentity,
    ModelTier,
    StreamChunk,
)
from naumi_agent.model.targets import ResolvedModelTarget


@runtime_checkable
class ModelPort(Protocol):
    """Route, describe, discover, and invoke configured model capabilities."""

    def get_model_info(self, model: str) -> dict[str, Any]: ...
    def get_context_window(self, model: str) -> int: ...
    def get_max_output(self, model: str) -> int: ...
    def get_cost_rates(self, model: str) -> dict[str, float]: ...
    def get_model_capability_contract(
        self, model: str | None = None,
    ) -> ModelCapabilityContract: ...
    def resolve_model(self, tier: ModelTier | str) -> str: ...
    def resolve_target(self, model: str) -> ResolvedModelTarget: ...
    def get_runtime_identity(self, model: str) -> ModelRuntimeIdentity: ...
    async def list_available_models(
        self, provider_id: str | None = None, *, refresh: bool = False,
    ) -> tuple[ProviderModelListing, ...]: ...
    def get_reasoning_effort_status(
        self, model: str | None = None,
    ) -> ReasoningEffortStatus: ...
    def set_reasoning_effort(
        self,
        value: str | ReasoningEffortSetting,
        *,
        model: str | None = None,
    ) -> ReasoningEffortStatus: ...
    def reset_reasoning_effort(
        self, *, model: str | None = None,
    ) -> ReasoningEffortStatus: ...
    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tier: ModelTier = ModelTier.CAPABLE,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: str | dict[str, Any] | None = None,
        thinking: dict[str, str] | None = None,
    ) -> ModelResponse: ...
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tier: ModelTier = ModelTier.CAPABLE,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


__all__ = ["ModelPort"]
