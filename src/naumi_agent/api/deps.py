"""依赖注入."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from naumi_agent.config.settings import AppConfig


def get_engine(request: Request):
    return request.app.state.engine


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def extract_api_key_from_connection(connection: Any) -> str | None:
    """从请求中提取 API key，优先级：X-API-Key > Authorization Bearer > api_key 查询参数.

    仅接受格式正确的 ``Authorization: Bearer <token>``；其他 scheme 或空 token
    会被忽略，不会返回任何凭证。
    """
    if api_key := connection.headers.get("X-API-Key"):
        return api_key

    auth_header = connection.headers.get("Authorization")
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
            return parts[1]

    if api_key := connection.query_params.get("api_key"):
        return api_key

    return None


def extract_api_key(request: Request) -> str | None:
    return extract_api_key_from_connection(request)


async def verify_api_key(request: Request) -> str:
    api_key = extract_api_key(request)
    config: AppConfig | None = getattr(request.app.state, "config", None)

    if not config or not config.api.api_keys:
        return "anonymous"

    if not api_key or api_key not in config.api.api_keys:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return api_key


EngineDep = Depends(get_engine)
ConfigDep = Depends(get_config)
AuthDep = Depends(verify_api_key)
