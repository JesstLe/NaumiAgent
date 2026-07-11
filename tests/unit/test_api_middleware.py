from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from naumi_agent.api.middleware import RateLimitMiddleware


def test_rate_limit_uses_runtime_api_configuration() -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(api=SimpleNamespace(rate_limit_rpm=2))
    app.add_middleware(RateLimitMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 429
