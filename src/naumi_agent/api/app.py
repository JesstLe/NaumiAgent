"""FastAPI 应用入口."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from naumi_agent import __version__
from naumi_agent.api.permission_broker import PermissionApprovalBroker
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine


def resolve_config_path() -> str:
    """Return the API config path, allowing container entrypoints to override it."""
    return os.environ.get("NAUMI_CONFIG", "config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = AppConfig.from_yaml(resolve_config_path())
    engine = AgentEngine(config)
    permission_broker = PermissionApprovalBroker()
    engine.set_permission_confirmer(permission_broker.confirm)
    app.state.engine = engine
    app.state.chat_run_store = engine.chat_run_store
    app.state.active_chat_run_tasks = {}
    app.state.permission_broker = permission_broker
    app.state.config = config
    app.state.engine_lock = asyncio.Lock()
    app.state.started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    yield
    await permission_broker.close()
    await engine.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NaumiAgent API",
        version=__version__,
        description="通用智能 Agent 的 REST API 与 WebSocket 接口",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from naumi_agent.api.routes import health, messages, tools, workbench, ws

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(messages.router, prefix="/api/v1")
    app.include_router(tools.router, prefix="/api/v1")
    app.include_router(workbench.router, prefix="/api/v1")
    app.include_router(ws.router, prefix="/api/v1")

    from naumi_agent.api.middleware import AuthMiddleware, RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    app.add_middleware(AuthMiddleware)

    return app


app = create_app()
