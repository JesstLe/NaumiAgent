"""依赖注入."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from naumi_agent.config.settings import AppConfig


def get_engine(request: Request):
    return request.app.state.engine


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


async def verify_api_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    config: AppConfig = request.app.state.config

    if not config.api.api_keys:
        return "anonymous"

    if not api_key or api_key not in config.api.api_keys:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return api_key


EngineDep = Depends(get_engine)
ConfigDep = Depends(get_config)
AuthDep = Depends(verify_api_key)
