"""工具与配置路由."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from naumi_agent.api.deps import AuthDep
from naumi_agent.api.schemas import (
    ConfigResponse,
    ModelInfo,
    ReasoningEffortInfo,
    ToolInfo,
)
from naumi_agent.model.discovery import ModelDiscoveryError

router = APIRouter(tags=["tools", "config"])


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    return [
        ToolInfo(name=t.name, description=t.description, parameters=t.schema.parameters)
        for t in engine.tool_registry.all()
    ]


@router.get("/tools/{tool_name}", response_model=ToolInfo)
async def get_tool(tool_name: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    tool = engine.tool_registry.get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return ToolInfo(name=tool.name, description=tool.description, parameters=tool.schema.parameters)


@router.get("/config", response_model=ConfigResponse)
async def get_config(request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    config = engine.config
    model_warnings: list[str] = []
    try:
        listings = await engine.router.list_available_models()
    except ModelDiscoveryError as exc:
        listings = ()
        model_warnings.append(str(exc))

    configured_tiers = {
        tier: engine.router.resolve_model(tier)
        for tier in ("fast", "capable", "reasoning")
    }
    models: list[ModelInfo] = []
    for listing in listings:
        if listing.warning:
            model_warnings.append(f"{listing.provider_id}: {listing.warning}")
        for model in listing.models:
            matching_tiers = [
                tier
                for tier, configured in configured_tiers.items()
                if configured
                in {model.id, model.canonical_id, model.upstream_id}
            ]
            models.append(
                ModelInfo(
                    id=model.canonical_id,
                    name=model.name,
                    provider=model.provider_id,
                    tier=",".join(matching_tiers) or "available",
                    upstream_id=model.upstream_id,
                    source=model.source,
                    max_context=model.max_context,
                    max_output=model.max_output,
                    supports_tools=model.supports_tools,
                    supports_reasoning=model.supports_reasoning,
                    reasoning_efforts=[value.value for value in model.reasoning_efforts],
                    default_reasoning_effort=(
                        model.default_reasoning_effort.value
                        if model.default_reasoning_effort is not None
                        else None
                    ),
                    supports_vision=model.supports_vision,
                )
            )

    if not models:
        tiers_by_model: dict[str, list[str]] = {}
        for tier, configured in configured_tiers.items():
            tiers_by_model.setdefault(configured, []).append(tier)
        for configured, tiers in tiers_by_model.items():
            identity = engine.router.get_runtime_identity(configured)
            models.append(
                ModelInfo(
                    id=identity.canonical_model,
                    name=identity.requested_model,
                    provider=identity.provider,
                    tier=",".join(tiers),
                    upstream_id=identity.upstream_model,
                    source=identity.source,
                )
            )

    effort_status = engine.router.get_reasoning_effort_status()
    return ConfigResponse(
        models=models,
        model_warnings=model_warnings,
        reasoning_effort=ReasoningEffortInfo(**effort_status.to_dict()),
        tools=[
            ToolInfo(name=t.name, description=t.description, parameters=t.schema.parameters)
            for t in engine.tool_registry.all()
        ],
        permission_mode=config.safety.permission_mode,
        max_budget_usd=config.safety.max_budget_usd,
        max_turns=config.safety.max_turns,
    )
