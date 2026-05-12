"""工具与配置路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from naumi_agent.api.schemas import ToolInfo, ConfigResponse, ModelInfo
from naumi_agent.api.deps import AuthDep

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
    return ConfigResponse(
        models=[
            ModelInfo(id="kimi-for-coding", name="Kimi for Coding", provider="moonshot", tier="capable"),
        ],
        tools=[
            ToolInfo(name=t.name, description=t.description, parameters=t.schema.parameters)
            for t in engine.tool_registry.all()
        ],
        permission_mode=config.safety.permission_mode,
        max_budget_usd=config.safety.max_budget_usd,
        max_turns=config.safety.max_turns,
    )
