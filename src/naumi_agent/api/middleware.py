"""认证与限流中间件."""

from __future__ import annotations

import time
from collections.abc import Sequence

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from naumi_agent.api.deps import extract_api_key


class ConfiguredCORSMiddleware:
    """Apply the active ``api.cors_origins`` without loading config at import time."""

    def __init__(self, app) -> None:
        self.app = app
        self._origins: tuple[str, ...] | None = None
        self._delegate = None

    def _configured_origins(self, scope) -> tuple[str, ...]:
        application = scope.get("app")
        config = getattr(getattr(application, "state", None), "config", None)
        origins = getattr(getattr(config, "api", None), "cors_origins", ["*"])
        if not isinstance(origins, Sequence) or isinstance(origins, (str, bytes)):
            return ("*",)
        return tuple(str(origin).strip() for origin in origins if str(origin).strip())

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        origins = self._configured_origins(scope)
        if origins != self._origins or self._delegate is None:
            self._origins = origins
            self._delegate = CORSMiddleware(
                self.app,
                allow_origins=list(origins),
                allow_methods=["*"],
                allow_headers=["*"],
            )
        await self._delegate(scope, receive, send)


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = {"/health", "/api/v1/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        config = getattr(request.app.state, "config", None)
        if config is None:
            return await call_next(request)

        api_key = extract_api_key(request)

        if request.url.path.startswith("/api/v1/ws"):
            if config.api.api_keys and api_key not in config.api.api_keys:
                return JSONResponse(status_code=401, content={"error": "Invalid API key"})
            return await call_next(request)

        if config.api.api_keys:
            if not api_key or api_key not in config.api.api_keys:
                return JSONResponse(
                    status_code=401, content={"error": "Invalid or missing API key"}
                )

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self._rpm = requests_per_minute
        self._buckets: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/ws"):
            return await call_next(request)

        client_id = request.client.host if request.client else "unknown"
        configured_rpm = getattr(
            getattr(getattr(request.app, "state", None), "config", None),
            "api",
            None,
        )
        requests_per_minute = getattr(configured_rpm, "rate_limit_rpm", self._rpm)
        try:
            rpm = max(int(requests_per_minute), 1)
        except (TypeError, ValueError):
            rpm = self._rpm
        now = time.time()

        if client_id not in self._buckets:
            self._buckets[client_id] = []
        self._buckets[client_id] = [t for t in self._buckets[client_id] if now - t < 60]

        if len(self._buckets[client_id]) >= rpm:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )

        self._buckets[client_id].append(now)
        return await call_next(request)
