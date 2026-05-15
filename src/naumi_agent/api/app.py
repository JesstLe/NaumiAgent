"""FastAPI 应用入口."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = AppConfig.from_yaml("config.yaml")
    engine = AgentEngine(config)
    app.state.engine = engine
    app.state.config = config
    yield
    await engine.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NaumiAgent API",
        version="0.1.0",
        description="通用智能 Agent 的 REST API 与 WebSocket 接口",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from naumi_agent.api.routes import health, messages, tools, ws

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(messages.router, prefix="/api/v1")
    app.include_router(tools.router, prefix="/api/v1")
    app.include_router(ws.router, prefix="/api/v1")

    from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    app.add_middleware(AuthMiddleware)

    return app


app = create_app()
