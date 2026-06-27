"""认证与限流中间件."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from naumi_agent.api.deps import extract_api_key


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
        now = time.time()

        if client_id not in self._buckets:
            self._buckets[client_id] = []
        self._buckets[client_id] = [t for t in self._buckets[client_id] if now - t < 60]

        if len(self._buckets[client_id]) >= self._rpm:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )

        self._buckets[client_id].append(now)
        return await call_next(request)
