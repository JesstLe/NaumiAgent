"""Engine catalog endpoint for web frontends."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Request

from naumi_agent.api.deps import AuthDep

router = APIRouter(tags=["engines"])

_ENGINE_DESCRIPTIONS = {
    "naumi": "内置 AgentEngine：完整工具链、记忆、多 Agent 与治理面板。",
    "pi": "外部 pi coding agent：成熟的编码代理，经 RPC 协议驱动。",
}


@router.get("/engines")
async def list_engines(request: Request, auth: str = AuthDep):
    config = getattr(request.app.state, "config", None)
    default_provider = getattr(getattr(config, "engine", None), "provider", "naumi")
    if default_provider not in {"naumi", "pi"}:
        default_provider = "naumi"
    pi_config = getattr(getattr(config, "engine", None), "pi", None)
    pi_binary = getattr(pi_config, "binary", "pi") or "pi"
    pi_available = shutil.which(pi_binary) is not None
    engines = [
        {
            "id": "naumi",
            "name": "NaumiAgent 引擎",
            "available": True,
            "default": default_provider == "naumi",
            "description": _ENGINE_DESCRIPTIONS["naumi"],
        },
        {
            "id": "pi",
            "name": "pi coding agent",
            "available": pi_available,
            "default": default_provider == "pi",
            "provider": getattr(pi_config, "provider", None) or "",
            "model": getattr(pi_config, "model", None) or "",
            "description": _ENGINE_DESCRIPTIONS["pi"],
        },
    ]
    return {"engines": engines, "default": default_provider}
