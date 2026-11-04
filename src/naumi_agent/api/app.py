"""FastAPI 应用入口."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from naumi_agent import __version__
from naumi_agent.api.permission_broker import PermissionApprovalBroker
from naumi_agent.config.settings import AppConfig
from naumi_agent.runtime.composition import create_agent_engine


def resolve_config_path() -> str:
    """Return the API config path, allowing container entrypoints to override it."""
    return os.environ.get("NAUMI_CONFIG", "config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = AppConfig.from_yaml(resolve_config_path())
    engine = create_agent_engine(config)
    permission_broker: PermissionApprovalBroker | None = None
    try:
        permission_broker = PermissionApprovalBroker()
        engine.set_permission_confirmer(permission_broker.confirm)
        app.state.session_reconciliation_recovery = await engine.start_long_running_services()
        app.state.engine = engine
        app.state.chat_run_store = engine.chat_run_store
        app.state.active_chat_run_tasks = {}
        app.state.permission_broker = permission_broker
        app.state.config = config
        app.state.engine_lock = asyncio.Lock()
        app.state.started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        yield
    finally:
        pi_web_engine = getattr(app.state, "pi_web_engine", None)
        if pi_web_engine is not None:
            await pi_web_engine.stop()
        if permission_broker is not None:
            await permission_broker.close()
        await engine.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NaumiAgent API",
        version=__version__,
        description="通用智能 Agent 的 REST API 与 WebSocket 接口",
        lifespan=lifespan,
    )
    from fastapi.responses import JSONResponse

    from naumi_agent.personal.models import PersonalAgentError

    @app.exception_handler(PersonalAgentError)
    async def personal_error_handler(request, exc):
        details = {"detail": exc.message, "code": exc.code, "error": exc.code}
        for field in ("accepted", "artifact_id", "version"):
            if hasattr(exc, field):
                details[field] = getattr(exc, field)
        status = {"not_found": 404, "storage_error": 503, "version_conflict": 409,
                  "idempotency_conflict": 409, "artifact_access_revoked": 409,
                  "binding_conflict": 409, "execution_session_readonly": 409}.get(exc.code, 422)
        return JSONResponse(
            status_code=status, content=details, headers={"Cache-Control": "no-store"}
        )

    from naumi_agent.api.routes import (
        artifacts,
        commands,
        engines,
        health,
        items,
        messages,
        output_assets,
        personal,
        tools,
        workbench,
        workspace_controls,
        ws,
    )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(messages.router, prefix="/api/v1")
    app.include_router(engines.router, prefix="/api/v1")
    app.include_router(output_assets.router, prefix="/api/v1")
    app.include_router(personal.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    app.include_router(items.router, prefix="/api/v1")
    app.include_router(tools.router, prefix="/api/v1")
    app.include_router(workbench.router, prefix="/api/v1")
    app.include_router(workspace_controls.router, prefix="/api/v1")
    app.include_router(commands.router, prefix="/api/v1")
    app.include_router(ws.router, prefix="/api/v1")

    from naumi_agent.api.middleware import (
        AuthMiddleware,
        ConfiguredCORSMiddleware,
        RateLimitMiddleware,
    )

    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ConfiguredCORSMiddleware)

    return app


app = create_app()
